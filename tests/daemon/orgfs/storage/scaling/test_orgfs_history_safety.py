"""Safety regressions for history-indexed OrgFS exports."""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path

import pytest
from pycrdt import Doc, Map, Text

from hyprial.daemon.impl.orgfs.storage.store import LocalSpaceStore, StoreError
from hyprial.daemon.impl.orgfs.storage.store.vocabulary import (
    _b64,
    _json_bytes,
    _state_covers,
)


_OLD = "doc-00000000-0000-0000-0000-000000000011"
_NEW = "doc-00000000-0000-0000-0000-000000000012"


def test_replacement_refuses_a_target_with_journal_history(tmp_path: Path) -> None:
    store = LocalSpaceStore(tmp_path, "space-prefix", node_id="node-prefix")
    try:
        store.commit(
            "meta",
            lambda doc: (
                doc["space"].__setitem__("owner", "user:owner"),
                doc["space"].__setitem__("spaceId", "space-prefix"),
                doc["members"].__setitem__(
                    "user:owner", Map({"mode": "rw", "removedAt": None})
                ),
            ),
            author="user:owner",
            actor=None,
        )
        store.commit(
            _OLD,
            lambda doc: doc.__setitem__("text", Text("secret")),
            author="user:owner",
            actor="agent:old",
        )
        store.commit(
            _NEW,
            lambda doc: doc.__setitem__("text", Text("pre-existing")),
            author="user:owner",
            actor="agent:collision",
        )

        replacement = Doc(client_id=123456)
        replacement["text"] = Text("sanitized")
        snapshot = replacement.get_update()
        digest = hashlib.sha256(snapshot).hexdigest()
        store.commit(
            "meta",
            lambda doc: doc["retirements"].__setitem__(
                _OLD,
                Map(
                    {
                        "replacementDocId": _NEW,
                        "planId": "plan-collision",
                        "snapshotId": digest,
                        "retiredAt": "2026-10-20T00:00:00Z",
                    }
                ),
            ),
            author="user:owner",
            actor=None,
        )
        before_rows = store._db.execute(  # noqa: SLF001
            "SELECT COUNT(*) FROM commits"
        ).fetchone()[0]
        before_new = store._docs[_NEW].get_update()  # noqa: SLF001

        with pytest.raises(StoreError, match="replacement target already has history") as error:
            store.install_replacement(
                _OLD,
                _NEW,
                snapshot,
                author="user:owner",
                actor="agent:installer",
            )

        assert error.value.code == "invalid-argument"
        assert store._db.execute(  # noqa: SLF001
            "SELECT COUNT(*) FROM commits"
        ).fetchone()[0] == before_rows
        assert store._docs[_NEW].get_update() == before_new  # noqa: SLF001
        assert _OLD in store._docs  # noqa: SLF001
    finally:
        store.close()


def _reference_pages(
    store: LocalSpaceStore, vv: bytes, max_bytes: int
) -> list[tuple[tuple[bytes, ...], str | None]]:
    rows = store._db.execute(  # noqa: SLF001
        "SELECT version, envelope_bytes FROM commits "
        "WHERE doc_id = ? ORDER BY rowid",
        (_NEW,),
    ).fetchall()
    candidates = [
        bytes(row["envelope_bytes"])
        for row in rows
        if not (vv and _state_covers(vv, bytes(row["version"])))
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
            store._cursor(_NEW, index, vv)  # noqa: SLF001
            if index < len(candidates)
            else None
        )
        pages.append((tuple(selected), cursor))
        if cursor is None:
            return pages


def _actual_pages(
    store: LocalSpaceStore, vv: bytes, max_bytes: int
) -> list[tuple[tuple[bytes, ...], str | None]]:
    pages: list[tuple[tuple[bytes, ...], str | None]] = []
    cursor = None
    while True:
        page = store.export_since(_NEW, vv, cursor=cursor, max_bytes=max_bytes)
        pages.append((page.envelopes, page.next))
        cursor = page.next
        if cursor is None:
            return pages


def test_existing_replacement_reset_history_uses_full_scan(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    target = LocalSpaceStore(tmp_path / "target", "space-legacy", node_id="target")
    replacement = LocalSpaceStore(
        tmp_path / "replacement", "space-legacy", node_id="replacement"
    )
    try:
        for store in (target, replacement):
            store.commit(
                "meta",
                lambda doc: (
                    doc["space"].__setitem__("owner", "user:owner"),
                    doc["space"].__setitem__("spaceId", "space-legacy"),
                    doc["members"].__setitem__(
                        "user:owner", Map({"mode": "rw", "removedAt": None})
                    ),
                ),
                author="user:owner",
                actor=None,
            )
        target.commit(
            _NEW,
            lambda doc: doc.__setitem__("text", Text("pre-existing")),
            author="user:owner",
            actor="agent:collision",
        )
        replacement.commit(
            _NEW,
            lambda doc: doc.__setitem__("text", Text("sanitized")),
            author="user:owner",
            actor="agent:replacement",
        )
        warm = target.export_since(
            _NEW, target.frontier(_NEW), cursor=None, max_bytes=4096
        )
        assert warm.envelopes == () and warm.next is None
        reset_row = replacement._db.execute(  # noqa: SLF001
            "SELECT writer, seq, doc_id, envelope_bytes, version, committed_at, "
            "broadcast_at FROM commits WHERE doc_id = ?",
            (_NEW,),
        ).fetchone()
        target._db.execute(  # noqa: SLF001
            "INSERT INTO commits(writer, seq, doc_id, envelope_bytes, version, "
            "committed_at, broadcast_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            tuple(reset_row),
        )
        target._db.commit()  # noqa: SLF001

        caplog.set_level(
            logging.WARNING,
            logger="hyprial.daemon.impl.orgfs.storage.store.vocabulary",
        )
        frontiers = (b"", target.frontier(_NEW), replacement.frontier(_NEW))
        for vv in frontiers:
            for max_bytes in (1, 900, 4096):
                assert _actual_pages(target, vv, max_bytes) == _reference_pages(
                    target, vv, max_bytes
                )
        assert sum("history is non-monotone" in row.message for row in caplog.records) == 1
    finally:
        replacement.close()
        target.close()
