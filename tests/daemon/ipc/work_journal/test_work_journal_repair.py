"""A repaired checklist keeps its validity when facade history is discarded."""

from typing import Any

import pytest
from pycrdt import Text

from hyprial.daemon.impl.pac.views import work_items

from ..work.test_work_ipc import OWNER, _add, _register_worker, _session
from ..work.test_work_r7 import _journal_ref, _parsed, _raw_write
from .test_work_journal_validity import (
    _item,
    _drop_validation_cache,
    _journal_rows,
    _journal_work_daemon,
    _restart_orgfs,
)


@pytest.fixture(name="repair_work_daemon")
def _repair_work_daemon(tmp_path, own_daemon_application, monkeypatch):
    return _journal_work_daemon.__wrapped__(
        tmp_path, own_daemon_application, monkeypatch
    )


def test_valid_checklist_repair_survives_runtime_restart(
    repair_work_daemon: Any,
) -> None:
    app, space_id = repair_work_daemon
    _register_worker(app, OWNER, "owner-session")
    added = _add(
        app,
        space_id,
        identity=_session(OWNER, "owner-session"),
        owner=OWNER,
        checklist=["ship"],
    )
    item_id = added["item"]["id"]
    checked = app.handle(
        "work.check",
        {
            "spaceId": space_id,
            "itemId": item_id,
            "number": 1,
            **_session(OWNER, "owner-session"),
        },
    )
    invalid = work_items.replace_head(
        _parsed(app, space_id, item_id),
        checklist=[{"text": "ship", "done": False}],
    )
    _raw_write(app, space_id, item_id, invalid, checked["version"])
    app.handle(
        "work.repair",
        {
            "spaceId": space_id,
            "itemId": item_id,
            **_session(OWNER, "owner-session"),
        },
    )
    assert _item(app, space_id, item_id).get("invalid") is not True
    rows_before = _journal_rows(app, space_id)

    _restart_orgfs(app)

    facade = app._require_orgfs_runtime().facade
    node = facade.stat(space_id, f"work/{item_id}.md")
    local_history = facade._spaces[space_id].history.get(node.node_id, ())
    assert len(local_history) == 0
    assert _journal_rows(app, space_id) == rows_before
    print("local_history_after_restart=0")
    print(f"journal_rows_unchanged={len(rows_before)}")
    item = _item(app, space_id, item_id)
    print(f"item_after_restart={item.get('invalid')!r}, reason={item.get('reason')!r}")
    assert item.get("invalid") is not True
    assert "reason" not in item
    assert len(rows_before) == 12
    shown = app.handle(
        "work.show",
        {"spaceId": space_id, "itemId": item_id, **_session(OWNER, "owner-session")},
    )
    assert shown["item"]["ownerActions"][-1]["verified"] is True
    journal = facade.journal_history(space_id, f"id:{node.node_id}")
    facade._spaces[space_id].history[node.node_id] = [object()]
    _drop_validation_cache(app)
    assert facade.journal_history(space_id, f"id:{node.node_id}") == journal
    assert _item(app, space_id, item_id).get("invalid") is not True
    assert _journal_rows(app, space_id) == rows_before


def test_legacy_repair_aliases_are_readable_and_require_explicit_repair(
    repair_work_daemon: Any,
) -> None:
    app, space_id = repair_work_daemon
    _register_worker(app, OWNER, "owner-session")
    identity = _session(OWNER, "owner-session")
    added = _add(app, space_id, identity=identity, owner=OWNER, checklist=["ship"])
    item_id = added["item"]["id"]
    checked = app.handle(
        "work.check",
        {"spaceId": space_id, "itemId": item_id, "number": 1, **identity},
    )
    restored = _parsed(app, space_id, item_id)
    invalid = work_items.replace_head(
        restored, checklist=[{"text": "ship", "done": False}]
    )
    invalid_alias = _raw_write(app, space_id, item_id, invalid, checked["version"])
    legacy_action = {
        "action": "repair",
        "actor": OWNER,
        "invalidVersion": invalid_alias,
        "restoredVersion": checked["version"],
    }
    legacy = work_items.replace_head(restored, ownerActions=[legacy_action])
    legacy = work_items.replace_log(
        legacy,
        [*restored.log, f"- 2026-11-05T00:00:00Z {OWNER}: repair {invalid_alias} -> {checked['version']}"],
    )
    assert work_items.parse_work_item(work_items.serialize_work_item(legacy)) == legacy
    _raw_write(app, space_id, item_id, legacy, invalid_alias)
    _restart_orgfs(app)
    assert _item(app, space_id, item_id)["invalid"] is True

    repaired = app.handle(
        "work.repair", {"spaceId": space_id, "itemId": item_id, **identity}
    )

    action = repaired["item"]["ownerActions"][-1]
    assert action["invalidJournalRef"].startswith("journal:")
    assert "invalidVersion" not in action
    _restart_orgfs(app)
    assert _item(app, space_id, item_id).get("invalid") is not True


def test_journal_refs_identify_a_delete_only_commit(
    repair_work_daemon: Any,
) -> None:
    app, space_id = repair_work_daemon
    _register_worker(app, OWNER, "owner-session")
    added = _add(app, space_id, identity=_session(OWNER, "owner-session"))
    item_id = added["item"]["id"]
    runtime = app._require_orgfs_runtime()
    node = runtime.facade.stat(space_id, f"work/{item_id}.md")

    def erase(doc):
        target = doc.get("text", type=Text)
        del target[:]

    store = runtime.stores.get(space_id)
    deleted = store.commit(node.doc_id, erase, author=store._owner(), actor=OWNER)
    _restart_orgfs(app)
    journal = app._require_orgfs_runtime().facade.journal_history(
        space_id, f"work/{item_id}.md"
    )

    assert journal.head_current is True
    assert journal.commits[-1].text == ""
    assert journal.commits[-2].text
    assert journal.commits[-1].journal_ref != journal.commits[-2].journal_ref
    assert journal.commits[-1].journal_ref.endswith(f":{deleted.writer}:{deleted.seq}")


@pytest.mark.parametrize("field", ["invalidJournalRef", "restoredJournalRef"])
def test_repair_rejects_a_reference_to_a_different_durable_row(
    repair_work_daemon: Any, field: str,
) -> None:
    app, space_id = repair_work_daemon
    _register_worker(app, OWNER, "owner-session")
    identity = _session(OWNER, "owner-session")
    added = _add(app, space_id, identity=identity, owner=OWNER, checklist=["ship"])
    item_id = added["item"]["id"]
    checked = app.handle(
        "work.check",
        {"spaceId": space_id, "itemId": item_id, "number": 1, **identity},
    )
    restored = _parsed(app, space_id, item_id)
    restored_ref = _journal_ref(app, space_id, item_id)
    invalid = work_items.replace_head(
        restored, checklist=[{"text": "ship", "done": False}]
    )
    invalid_alias = _raw_write(app, space_id, item_id, invalid, checked["version"])
    invalid_ref = _journal_ref(app, space_id, item_id)
    refs = {"invalidJournalRef": invalid_ref, "restoredJournalRef": restored_ref}
    refs[field] = f"{refs[field]}-missing"
    forged = work_items.append_owner_action(
        restored,
        action="repair",
        actor=OWNER,
        invalid_journal_ref=refs["invalidJournalRef"],
        restored_journal_ref=refs["restoredJournalRef"],
    )
    forged = work_items.replace_log(
        forged,
        [*restored.log, f"- 2026-11-05T00:00:00Z {OWNER}: repair {refs['invalidJournalRef']} -> {refs['restoredJournalRef']}"],
    )
    _raw_write(app, space_id, item_id, forged, invalid_alias)
    _restart_orgfs(app)

    assert _item(app, space_id, item_id)["invalid"] is True
