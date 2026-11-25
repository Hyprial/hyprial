"""Optional current-text views never own or reject ordinary OrgFS documents."""

from __future__ import annotations

import pytest

from hyprial.daemon.impl.pac.views import work_items


def _text(extra: str = "") -> str:
    return (
        "## M-owner-0001 Ordinary task\n\n```yaml\n"
        "id: M-owner-0001\nstatus: done\nowner: someone\n"
        "checklist: [{text: unfinished, done: false}]\n"
        "acceptance: the author can edit this freely\n"
        "keywords: [ordinary]\npacs: [wf-a]\n" + extra + "```\n\n"
        "Background **stays unchanged**.\n\n## Log\nAny ordinary prose is allowed.\n"
    )


def test_current_text_preserves_unknown_fields_and_old_approval_data() -> None:
    text = _text(
        "future-key: {nested: [one, two]}\n"
        "verification: ordinary metadata\n"
        "ownerActions: [{action: custom, actor: arbitrary, verified: true, future: keep}]\n"
    )
    item = work_items.parse_work_item(text)
    assert item.text == text
    assert item.head["future-key"] == {"nested": ["one", "two"]}
    assert item.head["verification"] == "ordinary metadata"
    assert item.head["ownerActions"][0] == {
        "action": "custom", "actor": "arbitrary", "verified": True, "future": "keep",
    }
    view = item.to_dict()
    assert view["documentText"] == text
    assert view["status"] == "done"
    assert view["checklist"] == [{"text": "unfinished", "done": False}]
    assert "unverifiedOperator" not in view
    assert "ownerActions" not in view
    assert item.head["owner"] == "someone"


@pytest.mark.parametrize("text", ["ordinary prose only", "```yaml\nid: [broken\n```\nbody", "```yaml\n- a list\n```\nbody"])
def test_unparseable_optional_metadata_keeps_the_document_readable(text: str) -> None:
    item = work_items.parse_work_item(text)
    view = item.to_dict()
    assert item.text == view["documentText"] == text
    assert view["diagnostics"]
    assert view["pacs"] == []


def test_wrong_optional_field_shapes_are_diagnostic_without_destroying_source() -> None:
    text = _text().replace("status: done", "status: [not, scalar]").replace(
        "checklist: [{text: unfinished, done: false}]", "checklist: [{text: x, done: 'false'}, 4]"
    ).replace("pacs: [wf-a]", "pacs: 42")
    item = work_items.parse_work_item(text)
    view = item.to_dict()
    assert item.head["pacs"] == 42
    assert item.head["checklist"] == [{"text": "x", "done": "false"}, 4]
    assert view["documentText"] == text
    assert view["diagnostics"]
    assert view["checklist"] == []
    assert view["pacs"] == []
    assert view["status"] == ""


def test_dates_and_legacy_status_are_only_optional_display_projections() -> None:
    text = _text("opened: '2026-09-29T10:00:00Z'\n").replace("status: done", "status: active")
    item = work_items.parse_work_item(text)
    assert item.head["status"] == "active"
    assert item.head["opened"] == "2026-09-29T10:00:00Z"
    assert item.to_dict()["opened"] == 1790676000000
    assert item.to_dict()["status"] == "active"
    assert item.to_dict()["documentText"] == text


@pytest.mark.parametrize("metadata", ["opened: 2001-13-01", "future: " + "[" * 600 + "0" + "]" * 600, "future: " + "7" * 5_000], ids=["invalid-timestamp", "nested-metadata", "oversized-integer"])
def test_optional_yaml_constructor_failures_are_diagnostic(metadata: str) -> None:
    text = "# Readable document\n```yaml\n" + metadata + "\n```\nBody survives."
    item = work_items.parse_work_item(text)
    assert item.text == text
    assert item.to_dict()["documentText"] == text
    assert item.to_dict()["diagnostics"]


def test_recursive_alias_in_optional_fields_has_no_recursive_page_payload() -> None:
    text = _text().replace("pacs: [wf-a]", "pacs: &cycle [*cycle]")
    item = work_items.parse_work_item(text)
    view = item.to_dict()
    assert item.head["pacs"][0] is item.head["pacs"]
    assert view["documentText"] == text
    assert view["pacs"] == []
    assert view["diagnostics"]
