"""Reads of a long document history must not rescan that history.

2026-10-20: the group-internal org space (45k tree commits, 566 MB journal)
kept its per-space writer thread at 70-86% CPU with no new writes.  Every
peer sync page called ``export_since``, which fetched every journal row of
the document and decoded two state vectors per row; the directory scan
behind the ~1 s endpoint reconcile called ``document_authors``, which JSON
decoded every envelope of each device document.  Each such call was one
writer-lane section, so every reader of the space queued behind it.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Callable

import pytest
from pycrdt import Map, Text

from hyprial.daemon.impl.orgfs.storage.space_authority import (
    OrgSpaceAuthority,
    _ReadStore,
)
from hyprial.daemon.impl.orgfs.storage.store import LocalSpaceStore, vocabulary
from hyprial.daemon.impl.orgfs.storage.store.vocabulary import (
    ORGFS_PAGE_BYTES,
    _b64,
    _json_bytes,
    _state_covers,
)


_DOC_ID = "doc-00000000-0000-0000-0000-000000000002"
_TREE_ID = "tree-00000000-0000-0000-0000-000000000002"
_ACTORS = 4


def _store(path: Path, commits: int, *, body: int = 40) -> tuple[LocalSpaceStore, list[bytes]]:
    store = LocalSpaceStore(path, "space-reads", node_id="node-reads")
    store.commit(
        "meta",
        lambda doc: (
            doc["space"].__setitem__("owner", "user:reads"),
            doc["space"].__setitem__("spaceId", "space-reads"),
            doc["space"].__setitem__("name", "reads"),
            doc["members"].__setitem__(
                "user:reads", Map({"mode": "rw", "removedAt": None})
            ),
            doc["docs"].__setitem__(
                "tree",
                Map({"active": _TREE_ID, "activatedAt": "genesis", "planId": None}),
            ),
        ),
        author="user:reads",
        actor=None,
    )

    def replace(value: str) -> Callable[..., None]:
        def mutate(doc) -> None:
            text = doc.get("text", type=Text)
            del text[:]
            text += value

        return mutate

    frontiers: list[bytes] = []
    store.commit(
        _DOC_ID,
        lambda doc: doc.__setitem__("text", Text("0")),
        author="user:reads",
        actor="agent:0",
    )
    frontiers.append(store.frontier(_DOC_ID))
    for index in range(commits):
        store.commit(
            _DOC_ID,
            replace(f"{index:06d}" * (body // 6 + 1)),
            author="user:reads",
            actor=f"agent:{index % _ACTORS}",
        )
        frontiers.append(store.frontier(_DOC_ID))
    return store, frontiers


def _reference_pages(
    store: LocalSpaceStore, vv: bytes, max_bytes: int
) -> list[tuple[tuple[bytes, ...], str | None]]:
    """The pre-2026-10-20 export_since paging, evaluated from the raw rows."""

    rows = store._db.execute(  # noqa: SLF001
        "SELECT version, envelope_bytes FROM commits WHERE doc_id = ? ORDER BY rowid",
        (_DOC_ID,),
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
            encoded = _json_bytes({"envelopes": [_b64(item) for item in trial]})
            if len(encoded) > max_bytes:
                if not selected:
                    selected = trial
                    index += 1
                break
            selected = trial
            index += 1
        cursor = (
            store._cursor(_DOC_ID, index, vv)  # noqa: SLF001
            if index < len(candidates)
            else None
        )
        pages.append((tuple(selected), cursor))
        if cursor is None:
            return pages


def _pages(
    store: LocalSpaceStore, vv: bytes, max_bytes: int
) -> list[tuple[tuple[bytes, ...], str | None]]:
    pages: list[tuple[tuple[bytes, ...], str | None]] = []
    cursor: str | None = None
    while True:
        page = store.export_since(_DOC_ID, vv, cursor=cursor, max_bytes=max_bytes)
        pages.append((tuple(page.envelopes), page.next))
        cursor = page.next
        if cursor is None:
            return pages


def _counting(
    monkeypatch: pytest.MonkeyPatch, owner: object, name: str
) -> dict[str, int]:
    counts = {"calls": 0}
    original = getattr(owner, name)

    def counted(*args, **kwargs):
        counts["calls"] += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(owner, name, counted)
    return counts


@pytest.mark.parametrize("max_bytes", [900, 4096, ORGFS_PAGE_BYTES])
def test_export_pages_match_the_full_scan(tmp_path: Path, max_bytes: int) -> None:
    store, frontiers = _store(tmp_path, 40)
    try:
        foreign = vocabulary._encode_state_vector({7: 3})  # noqa: SLF001
        for vv in (b"", frontiers[0], frontiers[17], frontiers[-2], frontiers[-1], foreign):
            assert _pages(store, vv, max_bytes) == _reference_pages(
                store, vv, max_bytes
            ), (vv, max_bytes)
    finally:
        store.close()


def _export_decodes(
    path: Path, monkeypatch: pytest.MonkeyPatch, commits: int
) -> tuple[int, int]:
    store, frontiers = _store(path, commits)
    try:
        store.export_since(_DOC_ID, frontiers[-1], cursor=None, max_bytes=4096)
        decodes = _counting(monkeypatch, vocabulary, "_read_var_uint")
        current = store.export_since(
            _DOC_ID, frontiers[-1], cursor=None, max_bytes=4096
        )
        assert current.envelopes == () and current.next is None
        up_to_date = decodes["calls"]
        decodes["calls"] = 0
        behind = store.export_since(
            _DOC_ID, frontiers[-3], cursor=None, max_bytes=ORGFS_PAGE_BYTES
        )
        assert len(behind.envelopes) == 2
        monkeypatch.undo()
        return up_to_date, decodes["calls"]
    finally:
        store.close()


def test_export_since_does_not_rescan_the_document_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    small = _export_decodes(tmp_path / "small", monkeypatch, 40)
    large = _export_decodes(tmp_path / "large", monkeypatch, 640)

    # 16x the history: a full scan decodes ~16x the varints; a bounded
    # search only adds a few probes (log2 16 = 4).
    assert large[0] <= small[0] * 2, (small, large)
    assert large[1] <= small[1] * 3, (small, large)


def test_repeated_document_reads_decode_only_new_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, _frontiers = _store(tmp_path, 300)
    try:
        first = store.document_authors(_DOC_ID)
        assert first == ("user:reads",)
        decoded = _counting(monkeypatch, store, "_decode_envelope")
        for _ in range(20):
            assert store.document_authors(_DOC_ID) == first
            store.snapshot(_DOC_ID, shallow_since=None)
        assert decoded["calls"] == 0
        store.commit(
            _DOC_ID,
            lambda doc: doc.get("text", type=Text).__iadd__("x"),
            author="user:reads",
            actor="agent:0",
        )
        decoded["calls"] = 0
        assert store.document_authors(_DOC_ID) == first
        manifest = store.snapshot(_DOC_ID, shallow_since=None)
        assert decoded["calls"] <= 1
        assert sum(manifest.authors_before.values()) == 302
    finally:
        store.close()


def test_reads_stay_fast_while_a_peer_pulls_the_whole_history(
    tmp_path: Path,
) -> None:
    # A peer with an empty version vector pages through the whole document
    # (replica priming and a joining node's first sync do exactly this).
    # Each page is one writer-lane section; another read of the same space
    # must never wait behind a section that grows with the history.
    store, _frontiers = _store(tmp_path, 1200, body=600)
    authority = OrgSpaceAuthority("space-reads", store)
    pulled: list[int] = []

    def peer_catch_up() -> None:
        cursor: str | None = None
        count = 0
        while True:
            page = authority.read(
                _ReadStore(
                    "export_since",
                    doc_id=_DOC_ID,
                    vv=b"",
                    cursor=cursor,
                    max_bytes=ORGFS_PAGE_BYTES,
                )
            )
            count += len(page.envelopes)
            cursor = page.next
            if cursor is None:
                break
        pulled.append(count)

    puller = threading.Thread(target=peer_catch_up, daemon=True)
    try:
        authority.read(_ReadStore("document_authors", doc_id=_DOC_ID))
        puller.start()
        waits: list[float] = []
        deadline = time.monotonic() + 60.0
        while puller.is_alive() and time.monotonic() < deadline:
            started = time.monotonic()
            authority.read(_ReadStore("document_authors", doc_id=_DOC_ID))
            waits.append(time.monotonic() - started)
            time.sleep(0.005)
        puller.join(timeout=60)
        assert pulled == [1201]
        assert waits and max(waits) < 0.25, (len(waits), max(waits))
        assert authority.max_section_seconds < 0.25, authority.max_section_seconds
    finally:
        authority.close()
        store.close()


def test_the_space_authority_reports_its_longest_section(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, _frontiers = _store(tmp_path, 3)
    authority = OrgSpaceAuthority("space-reads", store)
    try:
        assert authority.max_section_seconds == 0.0
        original = store.document_authors

        def slow(doc_id: str) -> tuple[str, ...]:
            time.sleep(0.3)
            return original(doc_id)

        monkeypatch.setattr(store, "document_authors", slow)
        authority.read(_ReadStore("document_authors", doc_id=_DOC_ID))
        assert authority.max_section_seconds >= 0.3
        assert authority.max_section_kind == "document_authors"
    finally:
        authority.close()
        store.close()


