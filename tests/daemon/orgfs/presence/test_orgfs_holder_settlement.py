"""Loopback coverage for OrgFS holder settlement under liveliness flicker."""

from __future__ import annotations

import json
import socket
import threading
import time

import pytest

from hyprial.daemon.impl.orgfs.api import OrgFsError
from hyprial.daemon.impl.orgfs.runtime import OrgFsRuntime
from hyprial.daemon.impl.orgfs.storage.store import StoreError
from hyprial.daemon.impl.transport import KeySpace, ZenohConfig, ZenohTransport


def _port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@pytest.fixture(autouse=True)
def isolated_orgfs_environment(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setenv("HYPRIAL_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("HYPRIAL_PEER_DISCOVERY", "0")


def _eventually(action, *, timeout: float = 8.0):
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            return action()
        except (OrgFsError, AssertionError) as exc:
            last_error = exc
            time.sleep(0.05)
    if last_error is not None:
        raise last_error
    raise AssertionError("condition did not become true")


def _assert_true(value):
    assert value
    return value


def _assert_equal(actual, expected):
    assert actual == expected
    return actual


def test_d3_liveliness_flicker_settles_holder_and_fetches_blob(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D3: announce after liveliness but before the shared verdict must settle."""

    endpoint = f"tcp/127.0.0.1:{_port()}"
    owner_transport = ZenohTransport(ZenohConfig(listen=(endpoint,)))
    reader_transport = ZenohTransport(ZenohConfig(connect=(endpoint,)))
    # Holder settlement models private sidecar admission (§7), retaining presence gates.
    owner = OrgFsRuntime(tmp_path / "owner", node_id="node-a", author="user:owner", trust_tailnet_members=True)
    events: list[tuple[str, str, dict[str, object]]] = []
    reader = OrgFsRuntime(
        tmp_path / "reader",
        node_id="node-b",
        author="user:reader",
        trust_tailnet_members=True,
        logger=lambda level, event, **fields: events.append((level, event, fields)),
    )
    owner_token = reader_token = None
    try:
        time.sleep(0.3)
        owner.bind_transport(owner_transport, supplier_online=lambda _node: True)
        live = {"node-a": True}
        reader.bind_transport(
            reader_transport,
            supplier_online=lambda node: live.get(node, False),
        )
        owner_token = owner_transport.declare_liveliness(
            KeySpace().actor_liveliness("node-a")
        )
        reader_token = reader_transport.declare_liveliness(
            KeySpace().actor_liveliness("node-b")
        )

        space = owner.facade.create_space("flicker-blob")
        owner.facade.invite(space.space_id, "user:reader", "rw")
        _eventually(lambda: reader.facade.join(space.space_id))
        _eventually(
            lambda: _assert_true(
                "node-a" in reader._holder_snapshot(space.space_id)
            )
        )

        owner_transport.put(
            KeySpace().orgfs_announce("node-a"),
            json.dumps(
                {
                    "schemaVersion": 1,
                    "type": "orgfs-announce",
                    "node": "node-a",
                    "durable": True,
                    "spaces": [],
                }
            ).encode(),
        )
        _eventually(
            lambda: _assert_equal(reader._holder_snapshot(space.space_id), ())
        )

        live["node-a"] = False
        owner_token.close()
        owner_token = None
        blob = b"flicker-blob-content"
        written = owner.facade.write_bytes(space.space_id, "artifact.bin", blob)
        assert written.blob_hash is not None
        assert not reader.blobs.has(written.blob_hash)

        liveliness_processed = threading.Event()
        announce_processed = threading.Event()
        original_liveliness = reader._apply_liveliness_command
        original_announce = reader._apply_announcement_command

        def observe_liveliness(sample):
            try:
                return original_liveliness(sample)
            finally:
                liveliness_processed.set()

        def observe_announce(sample):
            try:
                return original_announce(sample)
            finally:
                announce_processed.set()

        monkeypatch.setattr(reader, "_apply_liveliness_command", observe_liveliness)
        monkeypatch.setattr(reader, "_apply_announcement_command", observe_announce)

        owner_token = owner_transport.declare_liveliness(
            KeySpace().actor_liveliness("node-a")
        )
        assert liveliness_processed.wait(2.0)
        owner._announce_all()
        assert announce_processed.wait(2.0)
        live["node-a"] = True

        _eventually(
            lambda: _assert_true(
                "node-a" in reader._holder_snapshot(space.space_id)
            ),
            timeout=1.0,
        )
        assert reader.fetch_blob(space.space_id, written.blob_hash) == blob
        assert not [item for item in events if item[1].endswith("blob-fetch-failed")]

        reader_mesh = reader._meshes[space.space_id]

        def timed_out_fetch(_supplier, _digest, *, timeout=3.0, size_hint=None):
            del timeout, size_hint
            raise StoreError("blob-unavailable", "blob supplier did not answer")

        monkeypatch.setattr(reader_mesh, "fetch_blob", timed_out_fetch)
        with pytest.raises(OrgFsError, match="blob-unavailable"):
            reader.fetch_blob(space.space_id, "0" * 64)
        assert any(
            level == "warn"
            and event == "orgfs.content.blob-fetch-failed"
            and fields["reason"] == "blob-unavailable"
            and fields["digest"] == "0" * 64
            for level, event, fields in events
        )
    finally:
        reader.close()
        owner.close()
        for registration in (reader_token, owner_token):
            if registration is not None:
                registration.close()
        reader_transport.close()
        owner_transport.close()


def test_d5_agent_liveliness_has_no_effect_before_host_syncs_over_loopback(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D5: only a host token may drive OrgFS announce and sync effects."""

    endpoint = f"tcp/127.0.0.1:{_port()}"
    owner_transport = ZenohTransport(ZenohConfig(listen=(endpoint,)))
    reader_transport = ZenohTransport(ZenohConfig(connect=(endpoint,)))
    owner = OrgFsRuntime(
        tmp_path / "owner", node_id="node-owner", author="user:owner",
        trust_tailnet_members=True,  # Host/agent presence on the private path (§7).
    )
    reader = OrgFsRuntime(
        tmp_path / "reader", node_id="node-reader", author="user:reader",
        trust_tailnet_members=True,  # Host/agent presence on the private path (§7).
    )
    agent = "agent:owner@example.com:node-owner:worker"
    agent_key = KeySpace().actor_liveliness(agent)
    host_key = KeySpace().actor_liveliness("node-owner")
    effects_by_key: dict[str, tuple[str, ...]] = {}
    agent_processed = threading.Event()
    host_processed = threading.Event()
    announced: list[bool] = []
    scheduled: list[str] = []
    agent_token = host_token = None
    try:
        time.sleep(0.3)
        owner.bind_transport(owner_transport, supplier_online=lambda _node: True)
        reader.bind_transport(reader_transport, supplier_online=lambda _node: True)
        space = owner.facade.create_space("team")
        owner.facade.invite(space.space_id, "user:reader", "rw")

        # Re-announce on every join attempt: a single announce can beat the
        # reader's announce subscription and is never re-sent, which would fail
        # this setup rather than D5 itself.  That product race is pinned by
        # test_join_recovers_when_create_time_announce_is_lost (80a6e231).
        def announce_and_join():
            owner._announce_all()
            return reader.facade.join(space.space_id)

        _eventually(announce_and_join)
        _eventually(
            lambda: _assert_true(
                "node-owner" in reader._holder_snapshot(space.space_id)
            )
        )

        original_apply = reader._apply_liveliness_command

        def observe_apply(sample):
            effects = original_apply(sample)
            effects_by_key[sample.key] = tuple(effect.action for effect in effects)
            if sample.key == agent_key:
                agent_processed.set()
            elif sample.key == host_key:
                host_processed.set()
            return effects

        monkeypatch.setattr(reader, "_apply_liveliness_command", observe_apply)
        mesh = reader._meshes[space.space_id]
        original_announce = reader._announce_all
        original_schedule = mesh.schedule_sync_from

        def observe_announce() -> None:
            announced.append(True)
            original_announce()

        def observe_schedule(peer: str) -> bool:
            scheduled.append(peer)
            return original_schedule(peer)

        monkeypatch.setattr(reader, "_announce_all", observe_announce)
        monkeypatch.setattr(mesh, "schedule_sync_from", observe_schedule)

        agent_token = owner_transport.declare_liveliness(agent_key)
        assert agent_processed.wait(2.0)
        assert effects_by_key[agent_key] == ()
        assert announced == []
        assert scheduled == []
        assert agent not in reader._lively_peers

        host_token = owner_transport.declare_liveliness(host_key)
        assert host_processed.wait(2.0)
        assert effects_by_key[host_key] == ("announce", "sync")
        _eventually(lambda: _assert_equal(scheduled, ["node-owner"]))
        assert announced == [True]
        assert "node-owner" in reader._lively_peers
    finally:
        reader.close()
        owner.close()
        for registration in (host_token, agent_token):
            if registration is not None:
                registration.close()
        reader_transport.close()
        owner_transport.close()
