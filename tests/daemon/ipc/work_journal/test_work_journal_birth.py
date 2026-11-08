"""Adversarial retained-writer birth diagnostic."""

import sqlite3

import pytest
from pycrdt import Doc, Text
from .test_work_journal_validity import (
    _journal_work_daemon as _journal_fixture,
    _added_item,
    _node,
    _restart_orgfs,
    _item,
    _journal_path,
)


@pytest.fixture
def birth_work_daemon(tmp_path, own_daemon_application, monkeypatch):
    return _journal_fixture.__wrapped__(tmp_path, own_daemon_application, monkeypatch)


def test_missing_birth_cannot_be_replaced_by_another_writers_sequence_zero(
    birth_work_daemon,
):
    app, space_id = birth_work_daemon
    item_id = _added_item(app, space_id)
    runtime = app._require_orgfs_runtime()
    node = _node(app, space_id, item_id)
    content = runtime.facade.read_text_snapshot(space_id, f"work/{item_id}.md").content
    independent = Doc()
    independent.get("text", type=Text).insert(0, content)
    update = independent.get_update()
    store = runtime.stores.get(space_id)

    def replace(working):
        target = working.get("text", type=Text)
        del target[:]
        working.apply_update(update)

    retained = store.commit(
        node.doc_id, replace, author=store._owner(), actor="agent:allen:node-1:second"
    )
    assert retained.seq == 0
    _restart_orgfs(app)
    store.close()
    with sqlite3.connect(_journal_path(app, space_id)) as database:
        removed = database.execute(
            "DELETE FROM commits WHERE doc_id = ? AND writer != ?",
            (node.doc_id, retained.writer),
        ).rowcount
    database.close()
    assert removed >= 1
    item = _item(app, space_id, item_id)
    assert item.get("invalid") is True, item
    inspection = app._work_inspect_history(space_id, item_id)
    assert inspection.error.code == "WORK_ITEM_INVALID"
    assert inspection.error.data["historyIncomplete"] is True
