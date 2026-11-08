"""Work-item validity must survive a restart on the OrgFS journal alone.

The facade keeps version history only in process memory, so a clean restart
used to turn every earlier item into ``WORK_ITEM_INVALID`` with the
"after purge" text although no purge had happened.  These tests pin the
replacement evidence source: the document's committed journal chain.

Positive control: an item created before a restart is still valid afterwards.
Negative control: a journal that really lost its creation commit stays invalid.
"""

from __future__ import annotations

import sqlite3
import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from hyprial.daemon.impl.orgfs.api import OrgFsError

from ..work.test_work_ipc import (
    OWNER,
    _add,
    _register_worker,
    _session,
    work_daemon as _work_daemon_fixture,
)


@pytest.fixture(name="journal_work_daemon")
def _journal_work_daemon(tmp_path, own_daemon_application, monkeypatch):  # type: ignore[no-untyped-def]
    return _work_daemon_fixture.__wrapped__(
        tmp_path, own_daemon_application, monkeypatch
    )


def _restart_orgfs(app: Any) -> None:
    runtime = app._require_orgfs_runtime()
    runtime.close()
    app._orgfs_runtime = None
    _drop_validation_cache(app)


def _drop_validation_cache(app: Any) -> None:
    app._work_history_cache = None


def _item(app: Any, space_id: str, item_id: str) -> dict[str, Any]:
    listed = app.handle("work.ls", {"spaceId": space_id})
    return next(item for item in listed["items"] if item["id"] == item_id)


def _journal_path(app: Any, space_id: str) -> Path:
    return app.state_dir / "orgfs" / "spaces" / space_id / "journal.sqlite3"


def _node(app: Any, space_id: str, item_id: str) -> Any:
    return app._require_orgfs_runtime().facade.stat(space_id, f"work/{item_id}.md")


def _added_item(app: Any, space_id: str) -> str:
    _register_worker(app, OWNER, "owner-session")
    added = _add(app, space_id, identity=_session(OWNER, "owner-session"))
    return str(added["item"]["id"])


def _journal_rows(app: Any, space_id: str) -> list[tuple[Any, ...]]:
    with sqlite3.connect(_journal_path(app, space_id)) as database:
        return database.execute("SELECT * FROM commits ORDER BY rowid").fetchall()


@pytest.mark.parametrize("notes", [0, 2])
def test_item_created_before_a_restart_stays_valid(
    journal_work_daemon: Any, notes: int
) -> None:
    app, space_id = journal_work_daemon
    item_id = _added_item(app, space_id)
    for number in range(notes):
        app.handle(
            "work.note",
            {
                "spaceId": space_id,
                "itemId": item_id,
                "text": f"note {number}",
                **_session(OWNER, "owner-session"),
            },
        )
    assert _item(app, space_id, item_id).get("invalid") is not True
    rows = _journal_rows(app, space_id)

    _restart_orgfs(app)

    item = _item(app, space_id, item_id)
    assert item.get("invalid") is not True
    assert "reason" not in item
    assert app._work_inspect_history(space_id, item_id).error is None
    assert _journal_rows(app, space_id) == rows
    print(
        json.dumps(
            {
                "control": "positive",
                "priorNotes": notes,
                "valid": True,
                "newJournalWrites": 0,
            }
        )
    )


@pytest.mark.parametrize("retained_content", [False, True])
def test_journal_that_lost_its_creation_commit_stays_invalid(
    journal_work_daemon: Any,
    retained_content: bool,
) -> None:
    app, space_id = journal_work_daemon
    item_id = _added_item(app, space_id)
    doc_id = _node(app, space_id, item_id).doc_id

    _restart_orgfs(app)
    with sqlite3.connect(_journal_path(app, space_id)) as database:
        original = database.execute(
            "SELECT * FROM commits WHERE doc_id = ? AND seq = 0", (doc_id,)
        ).fetchone()
        removed = database.execute(
            "DELETE FROM commits WHERE doc_id = ? AND seq = 0", (doc_id,)
        ).rowcount
        if retained_content:
            columns = [row[1] for row in database.execute("PRAGMA table_info(commits)")]
            record = dict(zip(columns, original, strict=True))
            envelope = json.loads(record["envelope_bytes"])
            envelope["seq"] = 1
            record["seq"] = 1
            record["envelope_bytes"] = json.dumps(envelope).encode()
            placeholders = ", ".join("?" for _ in columns)
            database.execute(
                f"INSERT INTO commits VALUES ({placeholders})",
                tuple(record[column] for column in columns),
            )
    assert removed == 1
    retained = [row for row in _journal_rows(app, space_id) if row[2] == doc_id]
    assert len(retained) == int(retained_content)
    assert all(row[1] != 0 for row in retained)

    item = _item(app, space_id, item_id)
    inspection = app._work_inspect_history(space_id, item_id)

    assert item["invalid"] is True
    assert "after purge" not in item["reason"]
    assert inspection.error is not None
    assert inspection.error.code == "WORK_ITEM_INVALID"
    assert inspection.error.data["historyIncomplete"] is True
    assert item["reason"].endswith(
        "journal-created" if retained_content else "journal-empty"
    )
    print(
        json.dumps(
            {
                "control": "negative",
                "journalRows": len(retained),
                "createdRows": 0,
                "code": inspection.error.code,
                "historyIncomplete": inspection.error.data["historyIncomplete"],
            }
        )
    )


def test_journal_head_must_match_the_inspected_snapshot(
    journal_work_daemon: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, space_id = journal_work_daemon
    item_id = _added_item(app, space_id)
    facade = app._require_orgfs_runtime().facade
    snapshot = facade.read_text_snapshot(space_id, f"work/{item_id}.md")
    monkeypatch.setattr(
        facade,
        "read_text_snapshot",
        lambda *_args: replace(snapshot, version="stale-snapshot"),
    )
    _drop_validation_cache(app)

    inspection = app._work_inspect_history(space_id, item_id)

    assert inspection.current is None
    assert inspection.error is not None
    assert inspection.error.code == "WORK_ITEM_INVALID"
    assert inspection.error.data["historyIncomplete"] is True


def test_unreadable_journal_is_not_named_a_purge(
    journal_work_daemon: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, space_id = journal_work_daemon
    item_id = _added_item(app, space_id)
    facade = app._require_orgfs_runtime().facade

    def unavailable(*_args: object, **_kwargs: object) -> object:
        raise OrgFsError("journal-unreadable", {"message": "unreadable"})

    monkeypatch.setattr(facade, "journal_history", unavailable)
    _drop_validation_cache(app)

    inspection = app._work_inspect_history(space_id, item_id)

    assert inspection.error is not None
    assert str(inspection.error).startswith("history_unavailable:")
    assert inspection.error.data["historyIncomplete"] is True


def test_committed_purge_is_the_only_after_purge_reason(
    journal_work_daemon: Any,
) -> None:
    app, space_id = journal_work_daemon
    item_id = _added_item(app, space_id)
    runtime = app._require_orgfs_runtime()
    node = runtime.facade.stat(space_id, f"work/{item_id}.md")
    assert node.doc_id is not None
    runtime.stores.get(space_id).snapshot(node.doc_id, shallow_since=None)
    plan = app._handle_orgfs(
        "orgfs.purge_plan",
        {
            "spaceId": space_id,
            "targets": [
                {
                    "kind": "doc-range",
                    "node": f"id:{node.node_id}",
                    "from": node.version,
                    "to": node.version,
                }
            ],
        },
    )
    app._handle_orgfs("orgfs.purge", {"spaceId": space_id, "planId": plan["plan_id"]})
    _drop_validation_cache(app)

    item = _item(app, space_id, item_id)

    assert item["invalid"] is True
    assert "after purge" in item["reason"]
