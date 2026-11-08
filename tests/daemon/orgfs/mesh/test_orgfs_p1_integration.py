"""Cross-slice P1 acceptance scenarios over isolated loopback Zenoh."""

from __future__ import annotations

import base64
import json
import socket
import time

import pytest

from hyprial.daemon.impl.orgfs.api import OrgFsError
from hyprial.daemon.impl.orgfs.runtime import OrgFsRuntime
from hyprial.daemon.impl.orgfs.mesh import OrgFsMesh
from hyprial.daemon.impl.orgfs.storage.store import StoreError, assert_client_ids_registered
from hyprial.daemon.impl.transport import KeySpace, ZenohConfig, ZenohTransport


def _port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _eventually(action, *, timeout: float = 8.0):
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            return action()
        except (OrgFsError, StoreError, AssertionError) as exc:
            last_error = exc
            time.sleep(0.05)
    if last_error is not None:
        raise last_error
    raise AssertionError("condition did not become true")


def _read_with_content_wait(
    runtime: OrgFsRuntime, space_id: str, node_id: str
) -> str:
    try:
        return runtime.facade.read_text(space_id, f"id:{node_id}")[0]
    except OrgFsError as exc:
        if exc.code != "content-pending":
            raise
        runtime.await_content(
            space_id,
            node_id,
            deadline_monotonic=time.monotonic() + 1,
        )
        return runtime.facade.read_text(space_id, f"id:{node_id}")[0]


def _announce_until_seen(source: OrgFsRuntime, space_id: str, *targets: OrgFsRuntime) -> None:
    def announce_and_check() -> bool:
        source._mesh(space_id).announce()
        assert all(
            source.node_id in target._meshes[space_id]._announced_nodes
            for target in targets
        )
        return True

    _eventually(announce_and_check)


def _latest_envelope(runtime: OrgFsRuntime, space_id: str, doc_id: str) -> bytes:
    return bytes(
        runtime.stores[space_id]
        ._db.execute(
            "SELECT envelope_bytes FROM commits WHERE doc_id = ? ORDER BY rowid DESC LIMIT 1",
            (doc_id,),
        )
        .fetchone()[0]
    )


def _rewrite_origin(
    envelope: bytes, *, author: str, writer: str, seq: int = 0
) -> bytes:
    value = json.loads(envelope)
    value["seq"] = seq
    value["origin"]["author"] = author
    value["origin"]["writer"] = writer
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def _assert_client_ids_registered(
    space_id: str, *runtimes: OrgFsRuntime
) -> None:
    for runtime in runtimes:
        assert_client_ids_registered(runtime.stores.get(space_id))


@pytest.fixture(autouse=True)
def isolated_orgfs_environment(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setenv("HYPRIAL_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("HYPRIAL_PEER_DISCOVERY", "0")


def test_revocation_offline_window_write_restore_and_reconverge(tmp_path) -> None:
    """review-p1 15f + Allen 2026-10-09 ruling: removal wins the offline window.

    The member is removed while offline, then writes from a stale restart.
    Under the replaced 09-26 frontier-only rule that envelope converged on
    every replica; under the removal-wins ruling (R1+R2, offline-edit loss
    accepted) the owner imports nothing of it, the peer never sees it, and
    the owner's own state stays "base". The restarted member still learns
    the removal through the meta log range, and its later local write is
    refused at its own daemon.
    """

    endpoint = f"tcp/127.0.0.1:{_port()}"
    online = {"node-owner", "node-member", "node-peer"}
    is_online = online.__contains__
    owner_transport = ZenohTransport(ZenohConfig(listen=(endpoint,)))
    member_transport = ZenohTransport(ZenohConfig(connect=(endpoint,)))
    peer_transport = ZenohTransport(ZenohConfig(connect=(endpoint,)))
    # Private-sidecar policy (§7): removal-wins author validation remains active.
    owner = OrgFsRuntime(tmp_path / "owner", node_id="node-owner", author="user:owner", trust_tailnet_members=True)
    member = OrgFsRuntime(tmp_path / "member", node_id="node-member", author="user:member", trust_tailnet_members=True)
    peer = OrgFsRuntime(tmp_path / "peer", node_id="node-peer", author="user:peer", trust_tailnet_members=True)
    restarted_member: OrgFsRuntime | None = None
    restarted_transport: ZenohTransport | None = None
    try:
        time.sleep(0.3)
        owner.bind_transport(owner_transport, supplier_online=is_online)
        member.bind_transport(
            member_transport,
            supplier_online=is_online,
            holder_candidates=lambda: ("node-owner",),
        )
        peer.bind_transport(
            peer_transport,
            supplier_online=is_online,
            holder_candidates=lambda: ("node-owner",),
        )
        space = owner.facade.create_space("team")
        owner.facade.invite(space.space_id, "user:member", "rw")
        owner.facade.invite(space.space_id, "user:peer", "rw")
        owner._mesh(space.space_id).announce()
        _eventually(lambda: member.facade.join(space.space_id))
        _eventually(lambda: peer.facade.join(space.space_id))
        initial = owner.facade.write_text(space.space_id, "shared.md", "base")
        _eventually(
            lambda: member.facade.read_text(
                space.space_id, f"id:{initial.node_id}"
            )[0]
            == "base"
            or (_ for _ in ()).throw(AssertionError())
        )

        member.close()
        online.remove("node-member")
        owner.facade.remove_member(space.space_id, "user:member")

        restarted_member = OrgFsRuntime(
            tmp_path / "member", node_id="node-member", author="user:member",
            trust_tailnet_members=True,  # Same admitted fixture after restart (§7).
        )
        restarted_member.facade.write_text(
            space.space_id, f"id:{initial.node_id}", "offline edit"
        )
        online.add("node-member")
        restarted_transport = member_transport
        restarted_member.bind_transport(restarted_transport, supplier_online=is_online)
        # Removal wins: pulling the stale member's journal imports nothing
        # (the pull itself may answer; every envelope is refused as
        # removed-author), and the owner keeps its own content.
        _eventually(
            lambda: owner._meshes[space.space_id].sync_from("node-member") == 0
            or (_ for _ in ()).throw(AssertionError())
        )
        _eventually(
            lambda: peer._meshes[space.space_id].sync_from("node-owner") >= 0
            or (_ for _ in ()).throw(AssertionError())
        )
        owner.await_content(
            space.space_id,
            initial.node_id,
            deadline_monotonic=time.monotonic() + 8,
        )
        peer.await_content(
            space.space_id,
            initial.node_id,
            deadline_monotonic=time.monotonic() + 8,
        )
        assert not any(
            entry.author == "user:member"
            for entry in owner.facade.history(
                space.space_id, f"id:{initial.node_id}"
            )
        )

        _announce_until_seen(owner, space.space_id, restarted_member, peer)
        owner_store = owner.stores[space.space_id]
        owner_writer = str(
            json.loads(
                bytes(
                    owner_store._db.execute(
                        "SELECT envelope_bytes FROM commits WHERE doc_id = 'meta' "
                        "AND json_extract(envelope_bytes, '$.origin.author') = 'user:owner' "
                        "ORDER BY rowid LIMIT 1"
                    ).fetchone()[0]
                )
            )["origin"]["writer"]
        )
        member_mesh = restarted_member._meshes[space.space_id]
        assert member_mesh.sync_log_range_from(
            "node-owner", doc_id="meta", writer=owner_writer, after=2
        ) > 0
        for runtime in (owner, peer):
            _eventually(
                lambda runtime=runtime: _read_with_content_wait(
                    runtime, space.space_id, initial.node_id
                )
                == "base"
                or (_ for _ in ()).throw(AssertionError())
            )
        # Once the removal reached it, the stale member's own daemon refuses
        # further writes for this space (gate (a) of the security model).
        with pytest.raises(OrgFsError) as refused:
            restarted_member.facade.write_text(
                space.space_id, f"id:{initial.node_id}", "post-restart edit"
            )
        assert refused.value.code == "not-a-member"
        _assert_client_ids_registered(
            space.space_id, owner, peer, restarted_member
        )
    finally:
        if restarted_member is not None:
            restarted_member.close()
        else:
            member.close()
        if restarted_transport is not None:
            restarted_transport.close()
        else:
            member_transport.close()
        peer.close()
        owner.close()
        peer_transport.close()
        owner_transport.close()


def test_cold_cache_blob_unavailable_then_fetches_when_holder_returns(tmp_path) -> None:
    """review-p1 15h: §3.5 cold-cache reads fail fast, then retry unchanged."""

    endpoint = f"tcp/127.0.0.1:{_port()}"
    online = {"node-owner", "node-reader"}
    is_online = online.__contains__
    owner_transport = ZenohTransport(ZenohConfig(listen=(endpoint,)))
    reader_transport = ZenohTransport(ZenohConfig(connect=(endpoint,)))
    # Cold blob hydration models private sidecar admission, not enrollment (§7).
    owner = OrgFsRuntime(tmp_path / "owner", node_id="node-owner", author="user:owner", trust_tailnet_members=True)
    reader = OrgFsRuntime(tmp_path / "reader", node_id="node-reader", author="user:reader", trust_tailnet_members=True)
    returned_owner: OrgFsRuntime | None = None
    returned_transport: ZenohTransport | None = None
    try:
        time.sleep(0.3)
        owner.bind_transport(owner_transport, supplier_online=is_online)
        reader.bind_transport(
            reader_transport,
            supplier_online=is_online,
            holder_candidates=lambda: tuple(online - {"node-reader"}),
        )
        space = owner.facade.create_space("team")
        owner.facade.invite(space.space_id, "user:reader", "rw")
        payload = b"only-the-offline-holder-has-this" * 40_000
        created = owner.facade.write_bytes(space.space_id, "archive.bin", payload)
        owner._mesh(space.space_id).announce()
        _eventually(lambda: reader.facade.join(space.space_id))
        assert created.blob_hash is not None

        owner.close()
        online.remove("node-owner")
        started = time.monotonic()
        with pytest.raises(OrgFsError) as unavailable:
            reader.facade.read_bytes(space.space_id, f"id:{created.node_id}")
        assert unavailable.value.code == "content-pending"
        assert unavailable.value.details["expectedSha256"] == created.blob_hash
        assert "node-owner" in unavailable.value.details["suggestedHolders"]
        assert time.monotonic() - started < 1.0

        online.add("node-owner")
        returned_transport = owner_transport
        returned_owner = OrgFsRuntime(
            tmp_path / "owner", node_id="node-owner", author="user:owner",
            trust_tailnet_members=True,  # Same admitted fixture after restart (§7).
        )
        time.sleep(0.2)
        returned_owner.bind_transport(returned_transport, supplier_online=is_online)
        returned_owner._mesh(space.space_id).announce()
        assert reader.await_content(
            space.space_id,
            created.node_id,
            deadline_monotonic=time.monotonic() + 8,
        )
        # Wait for the bytes themselves, not only for a read that does not raise.
        _eventually(
            lambda: reader.facade.read_bytes(space.space_id, f"id:{created.node_id}")
            == payload
            or (_ for _ in ()).throw(AssertionError())
        )
        _assert_client_ids_registered(space.space_id, returned_owner, reader)
    finally:
        if returned_owner is not None:
            returned_owner.close()
        else:
            owner.close()
        if returned_transport is not None:
            returned_transport.close()
        else:
            owner_transport.close()
        reader.close()
        reader_transport.close()


def test_revoked_writer_rejected_across_all_three_mesh_injection_paths(
    tmp_path,
) -> None:
    """review-p1 15g: revocation case ③ on two real mesh receivers."""

    endpoint = f"tcp/127.0.0.1:{_port()}"
    online = {"node-owner", "node-r1", "node-r2", "node-ro"}
    is_online = online.__contains__
    owner_transport = ZenohTransport(ZenohConfig(listen=(endpoint,)))
    r1_transport = ZenohTransport(ZenohConfig(connect=(endpoint,)))
    r2_transport = ZenohTransport(ZenohConfig(connect=(endpoint,)))
    # Private sidecar trust (§7) bypasses supplier membership, NOT envelope authorship.
    owner = OrgFsRuntime(tmp_path / "owner", node_id="node-owner", author="user:owner", trust_tailnet_members=True)
    r1 = OrgFsRuntime(tmp_path / "r1", node_id="node-r1", author="user:r1", trust_tailnet_members=True)
    r2 = OrgFsRuntime(tmp_path / "r2", node_id="node-r2", author="user:r2", trust_tailnet_members=True)
    registrations = []
    try:
        time.sleep(0.3)
        owner.bind_transport(owner_transport, supplier_online=is_online)
        for runtime, transport in ((r1, r1_transport), (r2, r2_transport)):
            runtime.bind_transport(
                transport,
                supplier_online=is_online,
                holder_candidates=lambda: ("node-owner",),
            )
        space = owner.facade.create_space("team")
        for user, mode in (
            ("user:r1", "rw"),
            ("user:r2", "rw"),
            ("user:b", "rw"),
            ("user:ro", "ro"),
        ):
            owner.facade.invite(space.space_id, user, mode)
        nodes = {
            name: owner.facade.write_text(space.space_id, f"{name}.md", "base")
            for name in (
                "live-bad",
                "live-good",
                "sync-bad",
                "sync-good",
                "range-bad",
                "range-good",
            )
        }
        owner._mesh(space.space_id).announce()
        _eventually(lambda: r1.facade.join(space.space_id))
        _eventually(lambda: r2.facade.join(space.space_id))
        owner.facade.remove_member(space.space_id, "user:b")
        for receiver in (r1, r2):
            _eventually(
                lambda receiver=receiver: "user:b"
                not in {member.user for member in receiver.facade.members(space.space_id)}
                or (_ for _ in ()).throw(AssertionError())
            )

        original_broadcast = owner.broadcast_pending
        owner.broadcast_pending = lambda _space_id: 0  # type: ignore[method-assign]
        try:
            updates: dict[str, bytes] = {}
            for name, node in nodes.items():
                owner.facade.write_text(
                    space.space_id, f"id:{node.node_id}", f"accepted-{name}"
                )
                updates[name] = _latest_envelope(
                    owner, space.space_id, node.doc_id or ""
                )
        finally:
            owner.broadcast_pending = original_broadcast  # type: ignore[method-assign]

        malicious = {
            path: _rewrite_origin(
                updates[path],
                author="user:b",
                writer=f"node-b.inject-{path}.lane-0",
            )
            for path in ("live-bad", "sync-bad", "range-bad")
        }
        valid = {
            path: _rewrite_origin(
                updates[path],
                author="user:owner",
                writer=f"node-owner.inject-{path}.lane-0",
            )
            for path in ("live-good", "sync-good", "range-good")
        }
        keys = KeySpace()
        _announce_until_seen(owner, space.space_id, r1, r2)

        for path, envelope in (
            ("live-bad", malicious["live-bad"]),
            ("live-good", valid["live-good"]),
        ):
            value = json.loads(envelope)
            owner_transport.put(
                keys.orgfs_log(
                    space.space_id,
                    str(value["docId"]),
                    str(value["origin"]["writer"]),
                    int(value["seq"]),
                ),
                envelope,
            )
        for receiver in (r1, r2):
            _eventually(
                lambda receiver=receiver: receiver.facade.read_text(
                    space.space_id, f"id:{nodes['live-good'].node_id}"
                )[0]
                == "accepted-live-good"
                or (_ for _ in ()).throw(AssertionError())
            )
            assert (
                receiver.facade.read_text(
                    space.space_id, f"id:{nodes['live-bad'].node_id}"
                )[0]
                == "base"
            )

        sync_payload = json.dumps(
            {
                "schemaVersion": 1,
                "type": "orgfs-sync-page",
                "supplier": "node-ro",
                "envelopes": [
                    base64.b64encode(malicious["sync-bad"]).decode(),
                    base64.b64encode(valid["sync-good"]).decode(),
                ],
                "next": None,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        registrations.append(
            owner_transport.declare_query_handler(
                keys.orgfs_sync(space.space_id, "node-ro"),
                lambda selector, _payload: ((selector, sync_payload),),
            )
        )
        for receiver in (r1, r2):
            _eventually(
                lambda receiver=receiver: receiver._meshes[
                    space.space_id
                ].sync_from("node-ro")
                == 1
                or (_ for _ in ()).throw(AssertionError())
            )
            assert (
                receiver.facade.read_text(
                    space.space_id, f"id:{nodes['sync-good'].node_id}"
                )[0]
                == "accepted-sync-good"
            )
            assert (
                receiver.facade.read_text(
                    space.space_id, f"id:{nodes['sync-bad'].node_id}"
                )[0]
                == "base"
            )

        range_values = {
            str(json.loads(envelope)["origin"]["writer"]): envelope
            for envelope in (malicious["range-bad"], valid["range-good"])
        }

        def range_handler(_selector, payload):
            request = json.loads(payload)
            envelope = range_values.get(str(request.get("writer")))
            if envelope is None:
                return ()
            value = json.loads(envelope)
            return (
                (
                    keys.orgfs_log(
                        space.space_id,
                        str(value["docId"]),
                        str(value["origin"]["writer"]),
                        int(value["seq"]),
                    ),
                    envelope,
                ),
            )

        registrations.append(
            owner_transport.declare_query_handler(
                keys.orgfs_log_any(space.space_id), range_handler
            )
        )
        for receiver in (r1, r2):
            mesh = receiver._meshes[space.space_id]
            bad_value = json.loads(malicious["range-bad"])
            _eventually(
                lambda mesh=mesh: mesh.sync_log_range_from(
                    "node-ro",
                    doc_id=str(bad_value["docId"]),
                    writer=str(bad_value["origin"]["writer"]),
                )
                == 0
                or (_ for _ in ()).throw(AssertionError())
            )
            good_value = json.loads(valid["range-good"])
            _eventually(
                lambda mesh=mesh: mesh.sync_log_range_from(
                    "node-ro",
                    doc_id=str(good_value["docId"]),
                    writer=str(good_value["origin"]["writer"]),
                )
                == 1
                or (_ for _ in ()).throw(AssertionError())
            )
            assert (
                receiver.facade.read_text(
                    space.space_id, f"id:{nodes['range-good'].node_id}"
                )[0]
                == "accepted-range-good"
            )
            assert (
                receiver.facade.read_text(
                    space.space_id, f"id:{nodes['range-bad'].node_id}"
                )[0]
                == "base"
            )
            for path in ("live-bad", "sync-bad", "range-bad"):
                value = json.loads(malicious[path])
                count = receiver.stores[space.space_id]._db.execute(
                    "SELECT COUNT(*) FROM commits WHERE writer = ? AND seq = ? AND doc_id = ?",
                    (
                        str(value["origin"]["writer"]),
                        int(value["seq"]),
                        str(value["docId"]),
                    ),
                ).fetchone()[0]
                assert count == 0
        _assert_client_ids_registered(space.space_id, owner, r1, r2)
    finally:
        for registration in reversed(registrations):
            registration.close()
        r2.close()
        r1.close()
        owner.close()
        r2_transport.close()
        r1_transport.close()
        owner_transport.close()


def test_delete_only_commit_catches_up_after_offline_live_broadcast(tmp_path) -> None:
    """Round-3 D.1(a): sync delivers a missed delete-only text commit."""

    endpoint = f"tcp/127.0.0.1:{_port()}"
    online = {"node-owner", "node-peer"}
    is_online = online.__contains__
    owner_transport = ZenohTransport(ZenohConfig(listen=(endpoint,)))
    peer_transport = ZenohTransport(ZenohConfig(connect=(endpoint,)))
    owner = OrgFsRuntime(
        tmp_path / "d1-owner", node_id="node-owner", author="user:owner",
        trust_tailnet_members=True,  # Delete catch-up after private admission (§7).
    )
    peer = OrgFsRuntime(
        tmp_path / "d1-peer", node_id="node-peer", author="user:peer",
        trust_tailnet_members=True,  # Delete catch-up after private admission (§7).
    )
    restarted_peer: OrgFsRuntime | None = None
    try:
        time.sleep(0.3)
        owner.bind_transport(owner_transport, supplier_online=is_online)
        peer.bind_transport(
            peer_transport,
            supplier_online=is_online,
            holder_candidates=lambda: ("node-owner",),
        )
        space = owner.facade.create_space("delete-only")
        owner.facade.invite(space.space_id, "user:peer", "rw")
        created = owner.facade.write_text(space.space_id, "note.md", "non-empty")
        owner._mesh(space.space_id).announce()
        _eventually(lambda: peer.facade.join(space.space_id))
        _eventually(
            lambda: peer.facade.read_text(
                space.space_id, f"id:{created.node_id}"
            )[0]
            == "non-empty"
            or (_ for _ in ()).throw(AssertionError())
        )
        owner._meshes[space.space_id].probe("node-peer")
        assert owner.stores[space.space_id].unconfirmed_commits() == 0

        peer.close()
        online.remove("node-peer")
        owner.facade.write_text(space.space_id, f"id:{created.node_id}", "")
        assert owner.stores[space.space_id].unconfirmed_commits() > 0

        restarted_peer = OrgFsRuntime(
            tmp_path / "d1-peer", node_id="node-peer", author="user:peer",
            trust_tailnet_members=True,  # Same private-path fixture after restart (§7).
        )
        online.add("node-peer")
        restarted_peer.bind_transport(
            peer_transport,
            supplier_online=is_online,
            holder_candidates=lambda: ("node-owner",),
        )
        assert restarted_peer._meshes[space.space_id].sync_from("node-owner") > 0
        assert (
            restarted_peer.facade.read_text(
                space.space_id, f"id:{created.node_id}"
            )[0]
            == ""
        )
        owner._meshes[space.space_id].probe("node-peer")
        assert owner.stores[space.space_id].unconfirmed_commits() == 0
        _assert_client_ids_registered(space.space_id, owner, restarted_peer)
    finally:
        if restarted_peer is not None:
            restarted_peer.close()
        else:
            peer.close()
        owner.close()
        peer_transport.close()
        owner_transport.close()


def test_restore_to_empty_catches_up_after_offline_live_broadcast(tmp_path, monkeypatch) -> None:
    """Round-4 F1: commit_many's marker keeps an offline empty restore visible."""

    endpoint = f"tcp/127.0.0.1:{_port()}"
    online = {"node-owner", "node-peer"}
    is_online = online.__contains__
    owner_transport = ZenohTransport(ZenohConfig(listen=(endpoint,)))
    peer_transport = ZenohTransport(ZenohConfig(connect=(endpoint,)))
    owner = OrgFsRuntime(
        tmp_path / "f1-owner", node_id="node-owner", author="user:owner",
        trust_tailnet_members=True,  # Restore catch-up after private admission (§7).
    )
    peer = OrgFsRuntime(
        tmp_path / "f1-peer", node_id="node-peer", author="user:peer",
        trust_tailnet_members=True,  # Restore catch-up after private admission (§7).
    )
    restarted_peer: OrgFsRuntime | None = None
    try:
        time.sleep(0.3)
        owner.bind_transport(owner_transport, supplier_online=is_online)
        peer.bind_transport(
            peer_transport,
            supplier_online=is_online,
            holder_candidates=lambda: ("node-owner",),
        )
        space = owner.facade.create_space("restore-empty")
        owner.facade.invite(space.space_id, "user:peer", "rw")
        empty = owner.facade.write_text(space.space_id, "note.md", "")
        owner.facade.write_text(
            space.space_id, f"id:{empty.node_id}", "non-empty"
        )
        owner._mesh(space.space_id).announce()
        _eventually(lambda: peer.facade.join(space.space_id))
        _eventually(
            lambda: peer.facade.read_text(
                space.space_id, f"id:{empty.node_id}"
            )[0]
            == "non-empty"
            or (_ for _ in ()).throw(AssertionError())
        )
        owner._meshes[space.space_id].probe("node-peer")
        assert owner.stores[space.space_id].unconfirmed_commits() == 0

        peer.close()
        online.remove("node-peer")
        owner.facade.restore(
            space.space_id, f"id:{empty.node_id}", empty.version
        )
        assert owner.facade.read_text(space.space_id, f"id:{empty.node_id}")[0] == ""
        # Mutation: removing commit_many's _advance_commit_clock marker makes
        # this delete-only batch look covered by the peer's stale state vector.
        assert owner.stores[space.space_id].unconfirmed_commits() > 0

        # Keep the measured explicit pull first: bind-time holder catch-up can
        # otherwise win the race and legitimately make sync_from return zero.
        monkeypatch.setattr(OrgFsMesh, "schedule_sync_from", lambda *_args: False)
        restarted_peer = OrgFsRuntime(
            tmp_path / "f1-peer", node_id="node-peer", author="user:peer",
            trust_tailnet_members=True,  # Same private-path fixture after restart (§7).
        )
        online.add("node-peer")
        restarted_peer.bind_transport(
            peer_transport,
            supplier_online=is_online,
            holder_candidates=lambda: ("node-owner",),
        )
        assert restarted_peer._meshes[space.space_id].sync_from("node-owner") > 0
        for runtime in (owner, restarted_peer):
            assert (
                runtime.facade.read_text(
                    space.space_id, f"id:{empty.node_id}"
                )[0]
                == ""
            )
        owner._meshes[space.space_id].probe("node-peer")
        assert owner.stores[space.space_id].unconfirmed_commits() == 0
        _assert_client_ids_registered(space.space_id, owner, restarted_peer)
    finally:
        if restarted_peer is not None:
            restarted_peer.close()
        else:
            peer.close()
        owner.close()
        peer_transport.close()
        owner_transport.close()


def test_restart_file_first_then_invite_preserves_member_writes_everywhere(
    tmp_path,
) -> None:
    """Round-3 D.2(a/b): restarted-owner attribution holds on three replicas."""

    endpoint = f"tcp/127.0.0.1:{_port()}"
    online = {"node-owner", "node-b", "node-third"}
    is_online = online.__contains__
    owner_transport = ZenohTransport(ZenohConfig(listen=(endpoint,)))
    b_transport = ZenohTransport(ZenohConfig(connect=(endpoint,)))
    third_transport = ZenohTransport(ZenohConfig(connect=(endpoint,)))
    owner = OrgFsRuntime(
        tmp_path / "d2-owner", node_id="node-owner", author="user:owner",
        trust_tailnet_members=True,  # Post-restart CRDT replication on private path (§7).
    )
    member = OrgFsRuntime(
        tmp_path / "d2-b", node_id="node-b", author="user:b",
        trust_tailnet_members=True,  # Post-restart CRDT replication on private path (§7).
    )
    third = OrgFsRuntime(
        tmp_path / "d2-third", node_id="node-third", author="user:third",
        trust_tailnet_members=True,  # Post-restart CRDT replication on private path (§7).
    )
    restarted_owner: OrgFsRuntime | None = None
    try:
        time.sleep(0.3)
        owner.bind_transport(owner_transport, supplier_online=is_online)
        member.bind_transport(
            b_transport,
            supplier_online=is_online,
            holder_candidates=lambda: ("node-owner",),
        )
        third.bind_transport(
            third_transport,
            supplier_online=is_online,
            holder_candidates=lambda: ("node-owner",),
        )
        space = owner.facade.create_space("restart-attribution")
        owner.facade.invite(space.space_id, "user:third", "rw")
        owner._mesh(space.space_id).announce()
        _eventually(lambda: third.facade.join(space.space_id))
        owner.close()

        restarted_owner = OrgFsRuntime(
            tmp_path / "d2-owner", node_id="node-owner", author="user:owner",
            trust_tailnet_members=True,  # Same private-path fixture after restart (§7).
        )
        restarted_owner.bind_transport(owner_transport, supplier_online=is_online)
        # Binding D.2(a): the owner's first post-restart mutation is a file write.
        restarted_owner.facade.write_text(
            space.space_id, "owner-after-restart.md", "owner"
        )
        restarted_owner.facade.invite(space.space_id, "user:b", "rw")
        assert "user:b" in {
            item.user for item in restarted_owner.facade.members(space.space_id)
        }
        assert_client_ids_registered(restarted_owner.stores.get(space.space_id))
        _eventually(lambda: member.facade.join(space.space_id))
        _eventually(
            lambda: "user:b"
            in {item.user for item in member.facade.members(space.space_id)}
        )
        by_member = member.facade.write_text(
            space.space_id, "member-after-invite.md", "from-b"
        )
        member_envelopes = [
            bytes(row[0])
            for row in member.stores[space.space_id]._db.execute(
                "SELECT envelope_bytes FROM commits "
                "WHERE json_extract(envelope_bytes, '$.origin.author') = 'user:b' "
                "ORDER BY rowid"
            )
        ]
        assert member_envelopes
        _eventually(
            lambda: "user:b"
            in {item.user for item in third.facade.members(space.space_id)}
        )
        for runtime in (restarted_owner, third):
            def apply_member_envelopes(runtime=runtime) -> bool:
                for envelope in member_envelopes:
                    result = runtime._meshes[space.space_id].receive(
                        envelope, supplier="node-b"
                    )
                    assert result.status == "applied", result.code
                return True

            _eventually(apply_member_envelopes)

        for runtime in (restarted_owner, member, third):
            _eventually(
                lambda runtime=runtime: runtime.facade.read_text(
                    space.space_id, f"id:{by_member.node_id}"
                )[0]
                == "from-b"
                or (_ for _ in ()).throw(AssertionError())
            )
        _assert_client_ids_registered(
            space.space_id, restarted_owner, member, third
        )
    finally:
        if restarted_owner is not None:
            restarted_owner.close()
        else:
            owner.close()
        third.close()
        member.close()
        third_transport.close()
        b_transport.close()
        owner_transport.close()
