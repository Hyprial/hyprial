"""A slow holder's chunk wins over fast non-holders' "not here" answers.

Regression for kanban f42edbd9: the blob key has no node segment, so every
mesh answers a blob query.  Non-holders answer ``unknown-blob`` at once; a
holder relayed over DERP answered after the old 3 s window, its chunk was
dropped, and the reader reported the first non-holder's ``unknown-blob`` as if
the holder had said it.  The bus below injects per-node answer latency and
honours the query window, so the failure is deterministic without sleeping.
"""

from __future__ import annotations

from fnmatch import fnmatch
import json
from types import SimpleNamespace

import pytest

from hyprial.daemon.impl.orgfs.storage.blobs import BlobPurged, BlobStore, encode_request
from hyprial.daemon.impl.orgfs.mesh import OrgFsMesh
from hyprial.daemon.impl.orgfs.network.protocol import _error
from hyprial.daemon.impl.orgfs.storage.store import ImportResult, StoreError
from hyprial.daemon.impl.transport import KeySpace, TransportSample

SPACE = "space-1"
CONTENT = b"png" * 300_000


class _LatencyBus:
    """Every handler answers after ``latency[node]`` seconds of simulated time."""

    def __init__(self) -> None:
        self.handlers: list[tuple[str, str, object]] = []
        self.latency: dict[str, float] = {}
        self.windows: list[float] = []

    def session(self, node: str):
        bus = self

        class Session:
            def subscribe(self, _key_expr, _callback):
                return SimpleNamespace(close=lambda: None)

            def declare_query_handler(self, key_expr, handler):
                row = (node, key_expr, handler)
                bus.handlers.append(row)
                return SimpleNamespace(close=lambda: bus.handlers.remove(row))

            def query(self, key_expr, payload, *, timeout=3.0, **_kwargs):
                bus.windows.append(timeout)
                answered = []
                for owner, expression, handler in tuple(bus.handlers):
                    if owner == node:
                        continue
                    if not fnmatch(key_expr, expression.replace("**", "*")):
                        continue
                    delay = bus.latency.get(owner, 0.0)
                    if delay > timeout:
                        continue  # arrives after the query window closed
                    answered.extend(
                        (delay, TransportSample(key, body))
                        for key, body in handler(key_expr, payload)
                    )
                answered.sort(key=lambda item: item[0])
                return [sample for _delay, sample in answered]

        return Session()


def _mesh(bus, tmp_path, node, *, holds: bool, logger=None) -> OrgFsMesh:
    blobs = BlobStore(tmp_path / node)
    digest = None
    if holds:
        digest = blobs.put(SPACE, CONTENT, reason="own-write")
    mesh = OrgFsMesh(
        bus.session(node),
        SimpleNamespace(space_id=SPACE),
        node,
        author="user:owner",
        # Latency-only bus models the admitted private sidecar path (§7).
        # Its stub store has no membership document; hash/error checks stay real.
        trust_tailnet_members=True,
        blob_store=blobs,
        logger=logger,
    )
    mesh._supplier_allowed = lambda _supplier: True
    mesh.test_digest = digest
    mesh.test_blobs = blobs
    return mesh


@pytest.fixture
def mesh_set(tmp_path):
    bus = _LatencyBus()
    events: list[tuple[str, str, dict]] = []
    holder = _mesh(bus, tmp_path, "node-hq", holds=True)
    others = [_mesh(bus, tmp_path, name, holds=False) for name in ("node-x", "node-y")]
    reader = _mesh(
        bus,
        tmp_path,
        "node-r",
        holds=False,
        logger=lambda level, event, **fields: events.append((level, event, fields)),
    )
    bus.latency.update({"node-x": 0.01, "node-y": 0.02})
    try:
        yield SimpleNamespace(
            bus=bus,
            holder=holder,
            reader=reader,
            digest=holder.test_digest,
            events=events,
        )
    finally:
        for mesh in (reader, *others, holder):
            mesh.close()
            mesh.test_blobs.close()


def test_slow_holder_chunk_wins_over_fast_non_holder_absence(mesh_set) -> None:
    # 5 s: past the old 3 s window, inside a DERP-relayed first chunk's reach.
    mesh_set.bus.latency["node-hq"] = 5.0

    assert mesh_set.reader.fetch_blob("node-hq", mesh_set.digest) == CONTENT


def test_no_holder_in_window_is_unavailable_not_unknown_blob(mesh_set) -> None:
    mesh_set.bus.latency["node-hq"] = 600.0

    with pytest.raises(StoreError) as caught:
        mesh_set.reader.fetch_blob("node-hq", mesh_set.digest)

    assert caught.value.code == "blob-unavailable"
    replies = caught.value.details["replies"]
    assert sorted(reply["nodeId"] for reply in replies) == ["node-x", "node-y"]
    assert {reply["code"] for reply in replies} == {"unknown-blob"}
    assert {reply["spaceId"] for reply in replies} == {SPACE}
    assert isinstance(caught.value.details["elapsedMs"], int)


def test_a_decisive_error_still_decides(mesh_set) -> None:
    mesh_set.bus.latency["node-hq"] = 600.0
    purged = _error(BlobPurged.code, "blob was purged", nodeId="node-p", spaceId=SPACE)
    [blob_key] = [
        key
        for owner, key, _handler in mesh_set.bus.handlers
        if owner == "node-hq"
        and fnmatch(KeySpace().orgfs_blob("f" * 64), key.replace("**", "*"))
    ]
    mesh_set.bus.handlers.append(
        ("node-p", blob_key, lambda _key, _payload: (("k", purged),))
    )

    with pytest.raises(StoreError) as caught:
        mesh_set.reader.fetch_blob("node-hq", mesh_set.digest)

    assert caught.value.code == BlobPurged.code == "purged"


def test_failure_log_names_who_answered_and_the_space(mesh_set) -> None:
    mesh_set.bus.latency["node-hq"] = 600.0
    initial = ImportResult("rejected", "unknown-blob", details={"digest": "d"})

    result = mesh_set.reader._recover_blob(
        b"envelope", supplier="node-hq", digest=mesh_set.digest, initial=initial
    )

    assert result is initial
    [(level, event, fields)] = [
        row for row in mesh_set.events if row[1] == "orgfs.update.blob-fetch-failed"
    ]
    assert level == "warn"
    assert fields["reason"] == "blob-unavailable"
    assert fields["supplier"] == "node-hq"
    assert fields["spaceId"] == SPACE
    assert sorted(reply["nodeId"] for reply in fields["replies"]) == [
        "node-x",
        "node-y",
    ]


def test_holder_error_answers_carry_node_and_space(mesh_set) -> None:
    replies = mesh_set.bus.session("node-q").query(
        KeySpace().orgfs_blob("f" * 64),
        encode_request(SPACE, "f" * 64, 0, None),
        timeout=1.0,
    )
    decoded = [json.loads(sample.payload) for sample in replies]
    assert {row["code"] for row in decoded} == {"unknown-blob"}
    assert sorted(row["details"]["nodeId"] for row in decoded) == [
        "node-hq",
        "node-r",
        "node-x",
        "node-y",
    ]
    assert {row["details"]["spaceId"] for row in decoded} == {SPACE}
