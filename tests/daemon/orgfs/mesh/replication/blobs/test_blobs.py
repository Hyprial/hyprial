# Decisions:
# - Phase D, package T2: the large-update tests run on ``OrgFsRuntime``.
#   Protocol 3 sends an update larger than the frame bound as a vector-only
#   broadcast; the peer pulls, and the supplier's reply carries the update as
#   a temporary blob (reason ``sync``) that the requester fetches and verifies
#   inside that pull round (design §4.3).  The journal's deferred
#   ``updateBlob`` recovery queue and re-import are gone, so the tests assert
#   what a user sees: the text arrives, the blob is fetched once, a failed
#   fetch leaves the document ``content-pending`` and is logged, and a blocked
#   fetch does not hold back later broadcasts.
# - The large text is 1.8 MB: its base64 exceeds the 2 MiB frame bound, so the
#   broadcast is vector-only and the content must come through a pull reply
#   blob (a 900 KB update would still travel inline in the broadcast).
# - ``test_review_fix_6_fetches_missing_update_blob_and_retries_once`` keeps
#   two real Zenoh loopback sessions: it is the one test here that carries a
#   >512000-byte update over the real transport.  The others use the
#   in-process network (``state_support``) so a fetch can be blocked or made
#   to fail at the holder's blob store.
# - ``test_round4_f3_blob_queue_is_bounded_coalesced_and_counted`` is deleted:
#   update blobs are no longer queued; the remaining blob queue (content blobs,
#   ``SpaceSync.request_blob``) is covered by
#   ``sync/test_lifecycle.py::test_request_blob_queue_is_bounded`` and
#   ``::test_request_blob_coalesces_fetches_and_stores_the_blob``.
# - ``test_round3_r5_zero_length_holder_reply_fails_bounded`` is unchanged
#   (it passes on both runtimes; package S renames ``OrgFsRuntime``).
from __future__ import annotations

from collections.abc import Iterator
import hashlib
import json
import threading
import time

import pytest

from hyprial.daemon.impl.orgfs.api import OrgFsError
from hyprial.daemon.impl.orgfs.composition.state import OrgFsRuntime
from hyprial.daemon.impl.transport import KeySpace, ZenohConfig, ZenohTransport
from tests.daemon.orgfs.mesh.replication.state_support import (
    StatePeers,
    open_state_peers,
    shared_space,
)
from tests.daemon.orgfs.runtime_state.support import Peer, eventually, holds, text

from ..support import (
    _port,
    isolated_orgfs_environment as isolated_orgfs_environment,
)

_LARGE = "x" * 1_800_000


@pytest.fixture
def state_peers(tmp_path, monkeypatch: pytest.MonkeyPatch) -> Iterator[StatePeers]:
    yield from open_state_peers(tmp_path, monkeypatch)


def _assert_text(peer: Peer, space_id: str, ref: str, expected: str) -> None:
    assert text(peer.fs, space_id, ref) == expected


def _pending(peer: Peer, space_id: str, ref: str) -> None:
    with pytest.raises(OrgFsError) as error:
        peer.fs.read_text(space_id, ref)
    assert error.value.code == "content-pending"


def test_review_fix_6_fetches_missing_update_blob_and_retries_once(tmp_path) -> None:
    """Review P1 fix 6: a >512000-byte write crosses two Zenoh loopback sessions."""

    endpoint = f"tcp/127.0.0.1:{_port()}"
    owner_transport = ZenohTransport(ZenohConfig(listen=(endpoint,)))
    reader_transport = ZenohTransport(ZenohConfig(connect=(endpoint,)))
    owner = OrgFsRuntime(tmp_path / "owner", node_id="node-a", author="user:owner", trust_tailnet_members=True)
    reader = OrgFsRuntime(tmp_path / "reader", node_id="node-b", author="user:reader", trust_tailnet_members=True)
    try:
        time.sleep(0.3)

        def online(node: str) -> bool:
            return node in {"node-a", "node-b"}

        owner.bind_transport(owner_transport, supplier_online=online)
        reader.bind_transport(reader_transport, supplier_online=online, holder_candidates=lambda: ("node-a",))
        space = owner.facade.create_space("team")
        owner.facade.invite(space.space_id, "user:reader", "rw")
        eventually(lambda: reader.facade.join(space.space_id))

        content = "mesh-update-" + _LARGE
        assert len(content.encode()) > 512_000
        created = owner.facade.write_text(space.space_id, "large.md", content)

        def arrived() -> None:
            assert reader.facade.read_text(space.space_id, f"id:{created.node_id}")[0] == content

        eventually(arrived, timeout=20.0)
        assert reader.facade.stat(space.space_id, f"id:{created.node_id}").content_state == "arrived"
    finally:
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


def _blocking_holder(owner: Peer, *, fail: bool = False) -> tuple[threading.Event, threading.Event, dict[str, int]]:
    """Make ``owner``'s blob store block (or fail) every chunk request; count first chunks."""

    assert owner.runtime is not None
    blobs = owner.runtime.blobs
    original = blobs.get_chunk
    started = threading.Event()
    release = threading.Event()
    first_chunks: dict[str, int] = {}

    def get_chunk(request):
        digest = str(request["digest"])
        if int(request.get("offset", 0)) == 0:
            first_chunks[digest] = first_chunks.get(digest, 0) + 1
        started.set()
        release.wait(10.0)
        if fail:
            return json.dumps({"schemaVersion": 1, "type": "orgfs-blob-error",
                               "code": "blob-unavailable", "message": "holder lost the bytes"}).encode()
        return original(request)

    blobs.get_chunk = get_chunk
    return started, release, first_chunks


def test_round3_r5_inbound_processing_continues_while_blob_fetch_waits(state_peers: StatePeers) -> None:
    """R5: a pull blocked on a temporary blob leaves broadcast ingress free."""

    owner = state_peers.add("node-a", "user:owner")
    reader = state_peers.add("node-b", "user:reader")
    space_id = shared_space(state_peers, owner, reader)
    started, release, _first = _blocking_holder(owner)
    try:
        large = owner.fs.write_text(space_id, "large.md", _LARGE)
        assert started.wait(5.0)

        small = owner.fs.write_text(space_id, "small.md", "still live")
        eventually(lambda: _assert_text(reader, space_id, f"id:{small.node_id}", "still live"), timeout=2.0)
        _pending(reader, space_id, f"id:{large.node_id}")
    finally:
        release.set()
    eventually(lambda: _assert_text(reader, space_id, f"id:{large.node_id}", _LARGE))


def test_missing_update_blob_is_fetched_then_reimported_exactly_once(state_peers: StatePeers) -> None:
    """The temporary blob of one large update is fetched once and the text applied once."""

    owner = state_peers.add("node-a", "user:owner")
    reader = state_peers.add("node-b", "user:reader")
    space_id = shared_space(state_peers, owner, reader)
    _started, release, first_chunks = _blocking_holder(owner)
    release.set()
    watched: list[object] = []
    reader.fs.watch(space_id, "**", watched.append)

    created = owner.fs.write_text(space_id, "large.md", _LARGE)

    eventually(lambda: _assert_text(reader, space_id, f"id:{created.node_id}", _LARGE))
    holds(lambda: _assert_text(reader, space_id, f"id:{created.node_id}", _LARGE), duration=0.3)
    assert list(first_chunks.values()) == [1], first_chunks
    assert [getattr(event, "kind", None) for event in watched].count("created") == 1


def test_failed_blob_fetch_does_not_reimport_and_is_logged(state_peers: StatePeers) -> None:
    """A failed temporary-blob fetch applies nothing, keeps the text pending and is logged."""

    owner = state_peers.add("node-a", "user:owner")
    reader = state_peers.add("node-b", "user:reader")
    space_id = shared_space(state_peers, owner, reader)
    started, release, first_chunks = _blocking_holder(owner, fail=True)
    release.set()

    created = owner.fs.write_text(space_id, "large.md", _LARGE)
    assert started.wait(5.0)

    def logged() -> None:
        failures = [fields for event, fields in reader.events.seen if event == "orgfs.sync.blob-fetch-failed"]
        assert failures
        assert {fields.get("digest") for fields in failures} <= set(first_chunks)

    eventually(logged)
    _pending(reader, space_id, f"id:{created.node_id}")
