"""Extended mission model and owner/checklist/log write guards."""

from __future__ import annotations

import pytest

from hyprial.daemon.impl.pac.views import work_items


def _text(*, status: str = "doing", checklist: str = "  - {text: ship, done: false}\n") -> str:
    return f"""## M-allen-0001 Ship the work CLI

```yaml
id: M-allen-0001
status: {status}
owner: agent:allen:node:worker
assignee: user:allen
checklist:
{checklist}depends: [M-allen-0000]
acceptance: all focused gates pass
opened: 1791370000000
closed: null
keywords: [work-items]
pacs: [wf-one]
future-key: {{nested: true}}
```

Background stays free-form.

## Log
- 2026-10-07T12:00:00+00:00 agent:allen:node:worker: opened
"""


def test_work_item_round_trip_preserves_unknown_head_keys() -> None:
    parsed = work_items.parse_work_item(_text())

    reparsed = work_items.parse_work_item(work_items.serialize_work_item(parsed))

    assert reparsed == parsed
    assert reparsed.head["future-key"] == {"nested": True}
    assert reparsed.title == "Ship the work CLI"


@pytest.mark.parametrize(
    ("legacy", "current"),
    [("active", "doing"), ("paused", "todo"), ("done", "done")],
)
def test_legacy_status_is_mapped_once(legacy: str, current: str) -> None:
    old = _text(status=legacy).replace("opened: 1791370000000", "opened: 2026-09-28")
    item = work_items.parse_work_item(old)

    assert item.head["status"] == current
    assert isinstance(item.head["opened"], int)
    assert work_items.parse_work_item(work_items.serialize_work_item(item)).head["status"] == current


def test_checklist_reorder_is_refused() -> None:
    before = work_items.parse_work_item(
        _text(checklist="  - {text: first, done: false}\n  - {text: second, done: false}\n")
    )
    after = work_items.replace_head(
        before,
        checklist=[
            {"text": "second", "done": False},
            {"text": "first", "done": False},
        ],
    )

    with pytest.raises(work_items.WorkItemError) as error:
        work_items.validate_work_item_update(before, after, caller=before.owner)

    assert error.value.code == "WORK_ITEM_CHECKLIST_REWRITE"


def test_checked_item_cannot_be_unticked() -> None:
    before = work_items.parse_work_item(
        _text(checklist="  - {text: ship, done: true}\n")
    )
    after = work_items.replace_head(
        before, checklist=[{"text": "ship", "done": False}]
    )

    with pytest.raises(work_items.WorkItemError) as error:
        work_items.validate_work_item_update(before, after, caller=before.owner)

    assert error.value.code == "WORK_ITEM_CHECKLIST_REWRITE"


def test_legacy_bare_owner_and_quoted_opened_are_normalized() -> None:
    old = _text().replace(
        "owner: agent:allen:node:worker", "owner: alicesmith"
    ).replace("opened: 1791370000000", "opened: '2026-09-29'")

    item = work_items.parse_work_item(old)

    assert item.owner == "user:alicesmith"
    assert isinstance(item.head["opened"], int)


def test_legacy_empty_owner_and_iso_datetime_opened_are_parsed() -> None:
    old = _text().replace(
        "owner: agent:allen:node:worker", "owner: ''"
    ).replace("opened: 1791370000000", "opened: '2026-09-29T10:00:00Z'")

    item = work_items.parse_work_item(old)

    assert item.owner == ""
    assert item.head["opened"] == 1790676000000


def test_log_rewrite_is_refused() -> None:
    before = work_items.parse_work_item(_text())
    after = work_items.replace_log(before, ["- rewritten"])

    with pytest.raises(work_items.WorkItemError) as error:
        work_items.validate_work_item_update(before, after, caller=before.owner)

    assert error.value.code == "WORK_ITEM_LOG_REWRITE"


def test_acceptance_is_owner_writable_only() -> None:
    before = work_items.parse_work_item(_text())
    after = work_items.replace_head(before, acceptance="different")

    with pytest.raises(work_items.WorkItemError) as error:
        work_items.validate_work_item_update(
            before, after, caller="agent:other:node:worker"
        )

    assert error.value.code == "WORK_ITEM_OWNER_REQUIRED"


def test_done_requires_owner_and_all_checklist_items() -> None:
    before = work_items.parse_work_item(_text())
    done = work_items.replace_head(before, status="done", closed=1791371000000)

    with pytest.raises(work_items.WorkItemError) as unticked:
        work_items.validate_work_item_update(before, done, caller=before.owner)
    assert unticked.value.code == "WORK_ITEM_CHECKLIST_INCOMPLETE"

    checked = work_items.replace_head(
        before, checklist=[{"text": "ship", "done": True}]
    )
    done = work_items.replace_head(checked, status="done", closed=1791371000000)
    with pytest.raises(work_items.WorkItemError) as wrong_owner:
        work_items.validate_work_item_update(
            checked, done, caller="agent:other:node:worker"
        )
    assert wrong_owner.value.code == "WORK_ITEM_OWNER_REQUIRED"

    work_items.validate_work_item_update(checked, done, caller=checked.owner)


def test_an_id_rename_is_history_only_as_an_exact_conflict_split() -> None:
    """Review 1157 minor: the id guard and is_conflict_split were unkilled."""

    before = work_items.parse_work_item(_text())
    line = "- 2026-10-08T00:00:00+00:00 agent:allen:node:squire: resolve split from M-allen-0001 (conflict)"
    split = work_items.rename_conflict_copy(before, "M-allen-0002", line)

    work_items.validate_work_item_history(before, split)  # the one allowed rename

    bare = work_items.parse_work_item(
        work_items.serialize_work_item(split).replace(line + "\n", "")
    )
    with pytest.raises(work_items.WorkItemError, match="id was rewritten"):
        work_items.validate_work_item_history(before, bare)

    edited = work_items.parse_work_item(
        work_items.serialize_work_item(split).replace("Background stays", "Background now")
    )
    assert not work_items.is_conflict_split(before, edited)
    with pytest.raises(work_items.WorkItemError, match="id was rewritten"):
        work_items.validate_work_item_history(before, edited)
