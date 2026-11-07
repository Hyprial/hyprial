"""Export equivalence for histories from independent writers."""

from __future__ import annotations

from pathlib import Path

from pycrdt import Map, Text

from hyprial.daemon.impl.orgfs.storage.store import LocalSpaceStore
from hyprial.daemon.impl.orgfs.storage.store.vocabulary import (
    _b64,
    _json_bytes,
    _state_covers,
)


_DOC = "doc-00000000-0000-0000-0000-000000000021"


def _all(store: LocalSpaceStore, doc_id: str) -> list[bytes]:
    return [
        bytes(row[0])
        for row in store._db.execute(  # noqa: SLF001
            "SELECT envelope_bytes FROM commits WHERE doc_id = ? ORDER BY rowid",
            (doc_id,),
        )
    ]


def _reference(
    store: LocalSpaceStore, vv: bytes, max_bytes: int
) -> list[tuple[tuple[bytes, ...], str | None]]:
    rows = store._db.execute(  # noqa: SLF001
        "SELECT version, envelope_bytes FROM commits "
        "WHERE doc_id = ? ORDER BY rowid",
        (_DOC,),
    ).fetchall()
    candidates = [
        bytes(row[1])
        for row in rows
        if not (vv and _state_covers(vv, bytes(row[0])))
    ]
    pages: list[tuple[tuple[bytes, ...], str | None]] = []
    index = 0
    while True:
        selected: list[bytes] = []
        while index < len(candidates):
            trial = selected + [candidates[index]]
            if (
                len(_json_bytes({"envelopes": [_b64(item) for item in trial]}))
                > max_bytes
            ):
                if not selected:
                    selected = trial
                    index += 1
                break
            selected = trial
            index += 1
        cursor = (
            store._cursor(_DOC, index, vv)  # noqa: SLF001
            if index < len(candidates)
            else None
        )
        pages.append((tuple(selected), cursor))
        if cursor is None:
            return pages


def _actual(
    store: LocalSpaceStore, vv: bytes, max_bytes: int
) -> list[tuple[tuple[bytes, ...], str | None]]:
    pages: list[tuple[tuple[bytes, ...], str | None]] = []
    cursor = None
    while True:
        page = store.export_since(_DOC, vv, cursor=cursor, max_bytes=max_bytes)
        pages.append((page.envelopes, page.next))
        cursor = page.next
        if cursor is None:
            return pages


def test_out_of_order_multi_writer_pages_match_reference(tmp_path: Path) -> None:
    seed = LocalSpaceStore(tmp_path / "seed", "space-order", node_id="seed")
    left = LocalSpaceStore(tmp_path / "left", "space-order", node_id="left")
    right = LocalSpaceStore(tmp_path / "right", "space-order", node_id="right")
    target = LocalSpaceStore(tmp_path / "target", "space-order", node_id="target")
    stores = (seed, left, right, target)
    try:
        seed.commit(
            "meta",
            lambda doc: (
                doc["space"].__setitem__("owner", "user:owner"),
                doc["space"].__setitem__("spaceId", "space-order"),
                doc["members"].__setitem__(
                    "user:owner", Map({"mode": "rw", "removedAt": None})
                ),
            ),
            author="user:owner",
            actor=None,
        )
        genesis = _all(seed, "meta")
        for store in (left, right, target):
            for envelope in genesis:
                result = store.import_envelope(envelope, supplier="seed")
                assert result.status == "applied", result

        left.commit(
            _DOC,
            lambda doc: doc.__setitem__("left", Text("l0")),
            author="user:owner",
            actor="agent:left",
        )
        right.commit(
            _DOC,
            lambda doc: doc.__setitem__("right", Text("r0")),
            author="user:owner",
            actor="agent:right",
        )
        for source in (right, left):
            for envelope in _all(source, "meta")[len(genesis) :]:
                result = target.import_envelope(envelope, supplier=source.node_id)
                assert result.status == "applied", result

        frontiers = [b""]
        right_rows = _all(right, _DOC)
        left_rows = _all(left, _DOC)
        for envelope, supplier in (
            (right_rows[0], "right"),
            (left_rows[0], "left"),
        ):
            result = target.import_envelope(envelope, supplier=supplier)
            assert result.status == "applied", result
            frontiers.append(target.frontier(_DOC))

        right.commit(
            _DOC,
            lambda doc: doc.__setitem__("right2", Text("r1")),
            author="user:owner",
            actor="agent:right",
        )
        left.commit(
            _DOC,
            lambda doc: doc.__setitem__("left2", Text("l1")),
            author="user:owner",
            actor="agent:left",
        )
        right_rows = _all(right, _DOC)
        left_rows = _all(left, _DOC)
        for envelope, supplier in (
            (left_rows[1], "left"),
            (right_rows[1], "right"),
        ):
            result = target.import_envelope(envelope, supplier=supplier)
            assert result.status == "applied", result
            frontiers.append(target.frontier(_DOC))

        for vv in (*frontiers, target.frontier(_DOC)):
            for max_bytes in (1, 900, 4096):
                assert _actual(target, vv, max_bytes) == _reference(
                    target, vv, max_bytes
                )
    finally:
        for store in stores:
            store.close()
