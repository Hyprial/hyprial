"""actor_preserved_orgfs_mesh_rework: blob custody scenarios."""

from __future__ import annotations

from tests.support.tailnet_daemon import bind_admitted
import hashlib
import json
import threading
import time
import pytest
from hyprial.daemon.impl.orgfs.api import OrgFsError
from hyprial.daemon.impl.orgfs.network import recovery as recovery_module
from hyprial.daemon.impl.orgfs.runtime import OrgFsRuntime
from hyprial.daemon.impl.configuration.identity import classify_target_identity
from hyprial.daemon.impl.transport import (
    KeySpace,
    LivelinessDirectory,
    ZenohConfig,
    ZenohTransport,
)
from hyprial.kernel import TARGET_KIND_HOST

from .support import (
    _assert_equal,
    _assert_registered,
    _assert_true,
    _bind_with_daemon_presence,
    _eventually,
    _large_write_with_recovery_spies,
    _port,
    isolated_orgfs_environment as isolated_orgfs_environment,
)


def test_round4_f3_blob_queue_is_bounded_coalesced_and_counted(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F3: the loopback blob worker coalesces digests and counts queue drops."""

    monkeypatch.setattr(recovery_module, "ORGFS_BLOB_FETCH_QUEUE_LIMIT", 3)
    endpoint = f"tcp/127.0.0.1:{_port()}"
    owner_transport = ZenohTransport(ZenohConfig(listen=(endpoint,)))
    reader_transport = ZenohTransport(ZenohConfig(connect=(endpoint,)))
    events: list[tuple[str, str, dict[str, object]]] = []
    queue_full = threading.Event()
    dropped_logged = threading.Event()

    def record(level, event, **fields):
        events.append((level, event, fields))
        if event == "orgfs.update.blob-fetch-dropped":
            dropped_logged.set()

    owner = OrgFsRuntime(tmp_path / "owner", node_id="node-a", author="user:owner")
    reader = OrgFsRuntime(
        tmp_path / "reader",
        node_id="node-b",
        author="user:reader",
        logger=record,
    )
    fetch_started = threading.Event()
    release_fetch = threading.Event()
    offset_zero_requests: dict[str, int] = {}
    try:
        time.sleep(0.3)

        def online(node: str) -> bool:
            return node in {"node-a", "node-b"}

        # Cold replication over loopback models explicitly admitted peers.
        bind_admitted(owner, owner_transport, supplier_online=online)
        bind_admitted(reader, reader_transport, supplier_online=online)
        space = owner.facade.create_space("team")
        owner.facade.invite(space.space_id, "user:reader", "rw")
        owner._mesh(space.space_id).announce()
        _eventually(lambda: reader.facade.join(space.space_id))
        reader_mesh = reader._mesh(space.space_id)
        original_defer = reader_mesh._defer_blob_recovery

        def observed_defer(envelope, *, supplier, digest):
            original_defer(envelope, supplier=supplier, digest=digest)
            with reader_mesh._worker_lock:
                if (
                    reader_mesh._blob_fetch_pending_count
                    == recovery_module.ORGFS_BLOB_FETCH_QUEUE_LIMIT
                ):
                    queue_full.set()

        monkeypatch.setattr(reader_mesh, "_defer_blob_recovery", observed_defer)

        original_get_chunk = owner.blobs.get_chunk

        def blocking_get_chunk(request):
            value = json.loads(request)
            digest = str(value["digest"])
            if value.get("offset", 0) == 0:
                offset_zero_requests[digest] = offset_zero_requests.get(digest, 0) + 1
            fetch_started.set()
            release_fetch.wait(10.0)
            return original_get_chunk(request)

        monkeypatch.setattr(owner.blobs, "get_chunk", blocking_get_chunk)
        first = owner.facade.write_text(
            space.space_id, "first.md", "first-" + "a" * 900_000
        )
        assert fetch_started.wait(3.0)

        first_envelope = next(
            bytes(row[0])
            for row in reversed(
                owner.stores.get(space.space_id)
                ._db.execute("SELECT envelope_bytes FROM commits ORDER BY rowid")
                .fetchall()
            )
            if "updateBlob" in json.loads(bytes(row[0]))
        )
        first_value = json.loads(first_envelope)
        first_digest = str(first_value["updateBlob"])
        owner_transport.put(
            KeySpace().orgfs_log(
                space.space_id,
                str(first_value["docId"]),
                str(first_value["origin"]["writer"]),
                int(first_value["seq"]),
            ),
            first_envelope,
        )
        second = owner.facade.write_text(
            space.space_id, "second.md", "second-" + "b" * 900_000
        )
        assert queue_full.wait(3.0), "blob queue never reached its explicit limit"
        owner.facade.write_text(
            space.space_id, "dropped.md", "dropped-" + "c" * 900_000
        )

        assert dropped_logged.wait(3.0), "queue-full delivery did not log its drop"
        dropped = [
            item for item in events if item[1] == "orgfs.update.blob-fetch-dropped"
        ][-1]
        assert dropped[2]["droppedCount"] == 1
        assert dropped[2]["queueDepth"] == 3
        assert dropped[2]["queueLimit"] == 3

        release_fetch.set()
        _eventually(
            lambda: _assert_equal(
                reader.facade.read_text(space.space_id, f"id:{first.node_id}")[0],
                "first-" + "a" * 900_000,
            ),
            timeout=30.0,
        )
        _eventually(
            lambda: _assert_equal(
                reader.facade.read_text(space.space_id, f"id:{second.node_id}")[0],
                "second-" + "b" * 900_000,
            ),
            timeout=30.0,
        )
        assert offset_zero_requests[first_digest] == 1
        _assert_registered(space.space_id, owner, reader)
    finally:
        release_fetch.set()
        reader.close()
        owner.close()
        reader_transport.close()
        owner_transport.close()


def test_round3_r4a_announces_all_held_spaces_without_evicting_omissions(
    tmp_path,
) -> None:
    """D.3 R4a: a node holding X and Y keeps discovering and replicating both."""

    endpoint = f"tcp/127.0.0.1:{_port()}"
    owner_transport = ZenohTransport(ZenohConfig(listen=(endpoint,)))
    reader_transport = ZenohTransport(ZenohConfig(connect=(endpoint,)))
    owner = OrgFsRuntime(tmp_path / "owner", node_id="node-a", author="user:owner")
    reader = OrgFsRuntime(tmp_path / "reader", node_id="node-b", author="user:reader")
    owner_presence = reader_presence = None
    owner_token = reader_token = announce_registration = None
    announcements: list[dict[str, object]] = []
    try:
        time.sleep(0.3)
        announce_registration = reader_transport.subscribe(
            KeySpace().orgfs_announce("node-a"),
            lambda sample: announcements.append(json.loads(sample.payload)),
        )
        owner_presence, owner_token = _bind_with_daemon_presence(owner, owner_transport)
        reader_presence, reader_token = _bind_with_daemon_presence(
            reader, reader_transport
        )
        _eventually(lambda: _assert_true(owner_presence.actor_online("node-b")))
        _eventually(lambda: _assert_true(reader_presence.actor_online("node-a")))

        space_x = owner.facade.create_space("x")
        owner.facade.invite(space_x.space_id, "user:reader", "rw")
        space_y = owner.facade.create_space("y")
        owner.facade.invite(space_y.space_id, "user:reader", "rw")
        held = {space_x.space_id, space_y.space_id}

        def announced_both():
            return _assert_true(
                any(
                    {
                        str(space["spaceId"])
                        for space in announce.get("spaces", [])
                        if isinstance(space, dict) and "spaceId" in space
                    }
                    == held
                    for announce in announcements
                )
            )

        _eventually(announced_both)
        _eventually(lambda: reader.facade.join(space_x.space_id))
        _eventually(lambda: reader.facade.join(space_y.space_id))

        node_x = owner.facade.write_text(space_x.space_id, "x.md", "x")
        node_y = owner.facade.write_text(space_y.space_id, "y.md", "y")
        _eventually(
            lambda: _assert_equal(
                reader.facade.read_text(space_x.space_id, f"id:{node_x.node_id}")[0],
                "x",
            )
        )
        _eventually(
            lambda: _assert_equal(
                reader.facade.read_text(space_y.space_id, f"id:{node_y.node_id}")[0],
                "y",
            )
        )

        # A partial later announce is additive for discovery. It must not evict
        # an unmentioned held space.
        owner_transport.put(
            KeySpace().orgfs_announce("node-a"),
            json.dumps(
                {
                    "schemaVersion": 1,
                    "type": "orgfs-announce",
                    "node": "node-a",
                    "durable": True,
                    "spaces": [{"spaceId": space_x.space_id}],
                }
            ).encode(),
        )
        _eventually(
            lambda: _assert_true(
                "node-a" in reader._holders.get(space_y.space_id, set())
            )
        )
        _assert_registered(space_x.space_id, owner, reader)
        _assert_registered(space_y.space_id, owner, reader)
    finally:
        reader.close()
        owner.close()
        for registration in (announce_registration, reader_token, owner_token):
            if registration is not None:
                registration.close()
        for presence in (reader_presence, owner_presence):
            if presence is not None:
                presence.close()
        reader_transport.close()
        owner_transport.close()


def test_round3_r4b_restart_accepts_live_peer_without_join_or_sync(tmp_path) -> None:
    """D.3 R4b: restart accepts live broadcasts and prompts peer re-announce."""

    endpoint = f"tcp/127.0.0.1:{_port()}"
    owner_transport = ZenohTransport(ZenohConfig(listen=(endpoint,)))
    reader_transport = ZenohTransport(ZenohConfig(connect=(endpoint,)))
    owner = OrgFsRuntime(tmp_path / "owner", node_id="node-a", author="user:owner")
    reader = OrgFsRuntime(tmp_path / "reader", node_id="node-b", author="user:reader")
    owner_presence = reader_presence = None
    owner_token = reader_token = None
    restarted_transport = None
    restarted_presence = restarted_token = announce_registration = None
    restarted = None
    announcements: list[dict[str, object]] = []
    try:
        time.sleep(0.3)
        owner_presence, owner_token = _bind_with_daemon_presence(owner, owner_transport)
        reader_presence, reader_token = _bind_with_daemon_presence(
            reader, reader_transport
        )
        _eventually(lambda: _assert_true(owner_presence.actor_online("node-b")))
        _eventually(lambda: _assert_true(reader_presence.actor_online("node-a")))

        space = owner.facade.create_space("team")
        owner.facade.invite(space.space_id, "user:reader", "rw")
        _eventually(lambda: reader.facade.join(space.space_id))
        created = owner.facade.write_text(space.space_id, "shared.md", "before")
        _eventually(
            lambda: _assert_equal(
                reader.facade.read_text(space.space_id, f"id:{created.node_id}")[0],
                "before",
            )
        )

        reader.close()
        reader_token.close()
        reader_token = None
        reader_presence.close()
        reader_presence = None
        reader_transport.close()

        restarted_transport = ZenohTransport(ZenohConfig(connect=(endpoint,)))
        time.sleep(0.3)
        announce_registration = restarted_transport.subscribe(
            KeySpace().orgfs_announce("node-a"),
            lambda sample: announcements.append(json.loads(sample.payload)),
        )
        restarted = OrgFsRuntime(
            tmp_path / "reader", node_id="node-b", author="user:reader"
        )
        restarted_presence = LivelinessDirectory(restarted_transport)
        bind_admitted(
            restarted, restarted_transport,
            supplier_online=lambda supplier: (
                classify_target_identity(supplier) == TARGET_KIND_HOST
                and restarted_presence.actor_online(supplier)
            ),
            holder_candidates=lambda: restarted_presence.online_actors(),
        )
        _eventually(lambda: _assert_true(restarted_presence.actor_online("node-a")))
        before_revival = len(announcements)
        restarted_token = restarted_transport.declare_liveliness(
            KeySpace().actor_liveliness("node-b")
        )

        _eventually(
            lambda: _assert_true(
                len(announcements) > before_revival
                and any(
                    space.space_id
                    in {
                        str(item["spaceId"])
                        for item in announce.get("spaces", [])
                        if isinstance(item, dict) and "spaceId" in item
                    }
                    for announce in announcements[before_revival:]
                )
            )
        )

        # No join() and no sync_from(): this is only the established peer's
        # next live broadcast after the receiver restart.
        owner.facade.write_text(
            space.space_id, f"id:{created.node_id}", "after restart"
        )
        _eventually(
            lambda: _assert_equal(
                restarted.facade.read_text(space.space_id, f"id:{created.node_id}")[0],
                "after restart",
            )
        )
        _assert_registered(space.space_id, owner, restarted)
    finally:
        if restarted is not None:
            restarted.close()
        if reader._session is not None:
            reader.close()
        owner.close()
        for registration in (
            announce_registration,
            restarted_token,
            reader_token,
            owner_token,
        ):
            if registration is not None:
                registration.close()
        for presence in (restarted_presence, reader_presence, owner_presence):
            if presence is not None:
                presence.close()
        if restarted_transport is not None:
            restarted_transport.close()
        # reader_transport may already be closed; ZenohTransport.close is idempotent.
        reader_transport.close()
        owner_transport.close()


def test_round3_r5_zero_length_holder_reply_fails_bounded(tmp_path) -> None:
    """R5: a holder replying length 0 yields bounded blob-unavailable."""

    endpoint = f"tcp/127.0.0.1:{_port()}"
    holder_transport = ZenohTransport(ZenohConfig(listen=(endpoint,)))
    receiver_transport = ZenohTransport(ZenohConfig(connect=(endpoint,)))
    receiver = OrgFsRuntime(
        tmp_path / "receiver", node_id="node-b", author="user:owner"
    )
    registration = None
    try:
        time.sleep(0.3)
        digest = hashlib.sha256(b"data").hexdigest()
        response = json.dumps(
            {
                "schemaVersion": 1,
                "type": "orgfs-blob-chunk",
                "digest": digest,
                "offset": 0,
                "length": 0,
                "total": 4,
                "data": "",
            },
            separators=(",", ":"),
        ).encode()
        registration = holder_transport.declare_query_handler(
            KeySpace().orgfs_blob(digest),
            lambda selector, _payload: ((selector, response),),
        )
        space = receiver.facade.create_space("team")
        receiver.bind_transport(
            receiver_transport,
            supplier_online=lambda node: node == "node-a",
            holder_candidates=lambda: ("node-a",),
        )

        outcome: list[object] = []
        finished = threading.Event()

        def fetch() -> None:
            try:
                outcome.append(receiver.fetch_blob(space.space_id, digest))
            except Exception as exc:  # noqa: BLE001 - the assertion inspects it
                outcome.append(exc)
            finally:
                finished.set()

        worker = threading.Thread(target=fetch, daemon=True)
        worker.start()
        assert finished.wait(1.5), "zero-length reply left blob fetch looping"
        worker.join(0.1)
        assert len(outcome) == 1
        assert isinstance(outcome[0], OrgFsError)
        assert outcome[0].code == "blob-unavailable"
    finally:
        receiver.close()
        if registration is not None:
            registration.close()
        receiver_transport.close()
        holder_transport.close()


def test_round3_r5_inbound_processing_continues_while_blob_fetch_waits(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R5: pending blob fetch leaves the Zenoh subscriber free for later logs."""

    endpoint = f"tcp/127.0.0.1:{_port()}"
    owner_transport = ZenohTransport(ZenohConfig(listen=(endpoint,)))
    reader_transport = ZenohTransport(ZenohConfig(connect=(endpoint,)))
    owner = OrgFsRuntime(tmp_path / "owner", node_id="node-a", author="user:owner")
    reader = OrgFsRuntime(tmp_path / "reader", node_id="node-b", author="user:reader")
    fetch_started = threading.Event()
    release_fetch = threading.Event()
    try:
        time.sleep(0.3)

        def online(node: str) -> bool:
            return node in {"node-a", "node-b"}

        # Explicit admitted-path policy; blob scheduling stays under test.
        bind_admitted(owner, owner_transport, supplier_online=online)
        bind_admitted(reader, reader_transport, supplier_online=online)
        space = owner.facade.create_space("team")
        owner.facade.invite(space.space_id, "user:reader", "rw")
        _eventually(lambda: reader.facade.join(space.space_id))

        original_get_chunk = owner.blobs.get_chunk

        def blocking_get_chunk(request):
            fetch_started.set()
            release_fetch.wait(10.0)
            return original_get_chunk(request)

        monkeypatch.setattr(owner.blobs, "get_chunk", blocking_get_chunk)
        large = owner.facade.write_text(space.space_id, "large.md", "x" * 900_000)
        assert fetch_started.wait(3.0)

        small = owner.facade.write_text(space.space_id, "small.md", "still live")
        _eventually(
            lambda: _assert_equal(
                reader.facade.read_text(space.space_id, f"id:{small.node_id}")[0],
                "still live",
            ),
            timeout=10.0,
        )

        release_fetch.set()
        _eventually(
            lambda: _assert_equal(
                reader.facade.read_text(space.space_id, f"id:{large.node_id}")[0],
                "x" * 900_000,
            )
        )
        _assert_registered(space.space_id, owner, reader)
    finally:
        release_fetch.set()
        reader.close()
        owner.close()
        reader_transport.close()
        owner_transport.close()


def test_missing_update_blob_is_fetched_then_reimported_exactly_once(tmp_path) -> None:
    """Pins the contract in fix 6's name: fetch the missing blob, retry once."""

    content, read_back, fetches, reimports, _events = _large_write_with_recovery_spies(
        tmp_path, fetch_fails=False
    )
    assert fetches and all(supplier == "node-a" for supplier, _ in fetches)
    assert len({digest for _, digest in fetches}) == 1
    assert len(fetches) == 1, f"the missing blob was fetched {len(fetches)} times"
    assert reimports == [fetches[0][1]], (
        f"expected exactly one re-import after the fetch, got {reimports}"
    )
    assert read_back == content


def test_failed_blob_fetch_does_not_reimport_and_is_logged(tmp_path) -> None:
    """Negative leg: a failed fetch is final for that batch and says so."""

    _content, read_back, fetches, reimports, events = _large_write_with_recovery_spies(
        tmp_path, fetch_fails=True
    )
    assert len(fetches) == 1
    assert reimports == [], f"re-imported without the blob: {reimports}"
    assert any(
        event == "orgfs.update.blob-fetch-failed"
        and fields.get("digest") == fetches[0][1]
        for _level, event, fields in events
    ), "the failed fetch was not logged"
    assert read_back == ""
