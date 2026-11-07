from __future__ import annotations

import hashlib
import json
import threading
import time
import pytest
from hyprial.daemon.impl.orgfs.network import recovery as recovery_owner
from hyprial.daemon.impl.orgfs.api import OrgFsError
from hyprial.daemon.impl.orgfs.runtime import OrgFsRuntime
from hyprial.daemon.impl.transport import KeySpace, ZenohConfig, ZenohTransport

from ..support import (
    _assert_equal,
    _assert_registered,
    _eventually,
    _large_write_with_recovery_spies,
    _port,
    isolated_orgfs_environment as isolated_orgfs_environment,
)


def test_review_fix_6_fetches_missing_update_blob_and_retries_once(
    tmp_path,
) -> None:
    """Review P1 fix 6: >512000-byte public writes cross two loopback meshes."""

    endpoint = f"tcp/127.0.0.1:{_port()}"
    owner_transport = ZenohTransport(ZenohConfig(listen=(endpoint,)))
    reader_transport = ZenohTransport(ZenohConfig(connect=(endpoint,)))
    # Large blob recovery on the private sidecar-admitted path (§7).
    owner = OrgFsRuntime(tmp_path / "owner", node_id="node-a", author="user:owner", trust_tailnet_members=True)
    reader = OrgFsRuntime(tmp_path / "reader", node_id="node-b", author="user:reader", trust_tailnet_members=True)
    try:
        time.sleep(0.3)

        def online(node: str) -> bool:
            return node in {"node-a", "node-b"}

        owner.bind_transport(owner_transport, supplier_online=online)
        reader.bind_transport(reader_transport, supplier_online=online)

        space = owner.facade.create_space("team")
        owner.facade.invite(space.space_id, "user:reader", "rw")
        owner._mesh(space.space_id).announce()
        _eventually(lambda: reader.facade.join(space.space_id))

        content = "mesh-update-" + "x" * 900_000
        assert len(content.encode()) > 512_000
        created = owner.facade.write_text(space.space_id, "large.md", content)

        # Wait for the content itself, not only for a read that does not raise:
        # the tree entry lands before the >512000-byte update, whose blob is
        # fetched and retried on the worker, and until then the read is ''.
        _eventually(
            lambda: _assert_equal(
                reader.facade.read_text(space.space_id, f"id:{created.node_id}")[0],
                content,
            )
        )
        replica_refs = reader.blobs.references(space_id=space.space_id)
        assert any(reason == "replica" for _, _, reason in replica_refs)
        _assert_registered(space.space_id, owner, reader)
    finally:
        reader.close()
        owner.close()
        reader_transport.close()
        owner_transport.close()


def test_round4_f3_blob_queue_is_bounded_coalesced_and_counted(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F3: the loopback blob worker coalesces digests and counts queue drops."""

    monkeypatch.setattr('hyprial.daemon.impl.orgfs.network.recovery.ORGFS_BLOB_FETCH_QUEUE_LIMIT', 3)
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

    # Fetch custody is independent of directory enrollment on the private path (§7).
    owner = OrgFsRuntime(tmp_path / "owner", node_id="node-a", author="user:owner", trust_tailnet_members=True)
    reader = OrgFsRuntime(
        tmp_path / "reader",
        node_id="node-b",
        author="user:reader",
        trust_tailnet_members=True,
        logger=record,
    )
    fetch_started = threading.Event()
    release_fetch = threading.Event()
    offset_zero_requests: dict[str, int] = {}
    try:
        time.sleep(0.3)

        def online(node: str) -> bool:
            return node in {"node-a", "node-b"}

        owner.bind_transport(owner_transport, supplier_online=online)
        reader.bind_transport(reader_transport, supplier_online=online)
        space = owner.facade.create_space("team")
        owner.facade.invite(space.space_id, "user:reader", "rw")
        owner._mesh(space.space_id).announce()
        _eventually(lambda: reader.facade.join(space.space_id))
        reader_mesh = reader._meshes[space.space_id]
        original_defer = reader_mesh._defer_blob_recovery

        def observed_defer(envelope, *, supplier, digest):
            original_defer(envelope, supplier=supplier, digest=digest)
            with reader_mesh._worker_lock:
                if (
                    reader_mesh._blob_fetch_pending_count
                    == recovery_owner.ORGFS_BLOB_FETCH_QUEUE_LIMIT
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
            )
        )
        _eventually(
            lambda: _assert_equal(
                reader.facade.read_text(space.space_id, f"id:{second.node_id}")[0],
                "second-" + "b" * 900_000,
            )
        )
        assert offset_zero_requests[first_digest] == 1
        _assert_registered(space.space_id, owner, reader)
    finally:
        release_fetch.set()
        reader.close()
        owner.close()
        reader_transport.close()
        owner_transport.close()


def test_round3_r5_zero_length_holder_reply_fails_bounded(tmp_path) -> None:
    """R5: a holder replying length 0 yields bounded blob-unavailable."""

    endpoint = f"tcp/127.0.0.1:{_port()}"
    holder_transport = ZenohTransport(ZenohConfig(listen=(endpoint,)))
    receiver_transport = ZenohTransport(ZenohConfig(connect=(endpoint,)))
    receiver = OrgFsRuntime(
        tmp_path / "receiver", node_id="node-b", author="user:owner",
        trust_tailnet_members=True,  # Test malformed blob data after private admission (§7).
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
    # Queue bounds after private sidecar admission (§7); hash checks remain real.
    owner = OrgFsRuntime(tmp_path / "owner", node_id="node-a", author="user:owner", trust_tailnet_members=True)
    reader = OrgFsRuntime(tmp_path / "reader", node_id="node-b", author="user:reader", trust_tailnet_members=True)
    fetch_started = threading.Event()
    release_fetch = threading.Event()
    try:
        time.sleep(0.3)

        def online(node: str) -> bool:
            return node in {"node-a", "node-b"}

        owner.bind_transport(owner_transport, supplier_online=online)
        reader.bind_transport(reader_transport, supplier_online=online)
        space = owner.facade.create_space("team")
        owner.facade.invite(space.space_id, "user:reader", "rw")

        # Isolate R5 from discovery: re-announce on every join attempt so the
        # create-time announce racing the reader's subscription cannot fail
        # this test.  That product race is tracked separately (80a6e231).
        def announce_and_join():
            owner._mesh(space.space_id).announce()
            return reader.facade.join(space.space_id)

        _eventually(announce_and_join)

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
            timeout=1.0,
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
