"""The overview model's work-items page entry (design-work-items-...-2026-10-07 §4).

``model["workItems"]`` is one entry per loaded work item, whatever its status
or age — the whole backlog, not only items with graphs inside the run window
(that older, five-field projection stays ``model["missions"]`` untouched).
The view node builds against this exact shape; do not change names or types.
"""

from __future__ import annotations

from typing import Any

from hyprial.daemon.impl.pac.views import missions as mission_views
from hyprial.daemon.impl.pac.views import overview
import yaml

from hyprial.daemon.impl.pac.views.missions import parse_mission

NODE = "hq-node"
OWNER = "user:owner@example.test"
AGENT = "agent:owner@example.test:hq-node:worker-a"
DAY_MS = 24 * 3600 * 1000
NOW = 100 * DAY_MS  # observedAt; the 7-day window then starts at 93 * DAY_MS

SECRET = "ghp_" + "A" * 30


def _item(
    item_id: str,
    *,
    title: str = "A task",
    status: str = "todo",
    owner: str = OWNER,
    assignee: str | None = None,
    checklist: tuple[tuple[str, bool], ...] = (),
    depends: tuple[str, ...] = (),
    acceptance: str = "",
    pacs: tuple[str, ...] = (),
    log: tuple[str, ...] = (),
    opened: int = 1_000,
    closed: int | None = None,
) -> dict[str, Any]:
    """One loaded work item in the exact form ``load_missions`` hands over."""

    fields = {
        "id": item_id, "status": status, "owner": owner, "assignee": assignee,
        "checklist": [{"text": text, "done": done} for text, done in checklist],
        "depends": list(depends), "acceptance": acceptance, "pacs": list(pacs),
        "opened": opened, "closed": closed,
    }
    text = f"## {item_id} {title}\n\n```yaml\n{yaml.safe_dump(fields)}```\n\n## Log\n" + "\n".join(log)
    return parse_mission(text)


def _snapshot(*runs: dict[str, Any], observed_at: int = NOW) -> dict[str, Any]:
    return {"observedAt": observed_at, "runs": list(runs), "routines": []}


def _run(
    graph_id: str, *, created: int = 99 * DAY_MS, name: str = "fix-login"
) -> dict[str, Any]:
    return {
        "graphId": graph_id,
        "name": name,
        "sender": AGENT,
        "state": "completed",
        "createdAtMs": created,
        "closedAtMs": created + 1,
        "lastProgressAtMs": created + 1,
        "routineName": None,
        "nodes": [],
        "edges": [],
        "roster": [],
    }


def test_entry_shape_is_exactly_the_page_contract() -> None:
    item = _item(
        "M-owner-0003",
        title="Login hints",
        status="doing",
        assignee=AGENT,
        checklist=(("write spec", True), ("review", False)),
        depends=("M-owner-0001",),
        acceptance="login page shows hints",
        pacs=("wf-in",),
        log=("- 2026-10-07T12:00:00+00:00 user:owner: started",),
        opened=50 * DAY_MS,
    )

    model = overview.build_model(
        _snapshot(_run("wf-in")), node=NODE, missions=[item], window_days=7
    )

    assert model["workItems"] == [
        {
            "id": "M-owner-0003",
            "title": "Login hints",
            "status": "doing",
            "owner": OWNER,
            "assignee": "worker-a",
            "checklist": {"done": 1, "total": 2},
            "depends": ["M-owner-0001"],
            "lastLog": {
                "text": "started",
                "line": "- 2026-10-07T12:00:00+00:00 user:owner: started",
            },
            "pacs": ["wf-in"],
            "pacsInWindow": ["wf-in"],
            "acceptance": "login page shows hints",
            "checklistItems": [
                {"text": "write spec", "done": True},
                {"text": "review", "done": False},
            ],
            "log": ["- 2026-10-07T12:00:00+00:00 user:owner: started"],
            "opened": 50 * DAY_MS,
            "closed": None,
            "documentText": item["documentText"],
            "documentTruncated": False,
            "diagnostics": [],
            "path": "",
        }
    ]
    assert model["workItemsArchiveCutoffMs"] == NOW - 7 * DAY_MS


def test_every_status_is_present_including_closed_far_before_the_window() -> None:
    items = [
        _item("M-owner-0001", title="todo one"),
        _item("M-owner-0002", status="doing", pacs=("wf-in",)),
        _item("M-owner-0003", status="blocked"),
        _item("M-owner-0004", status="done", closed=1_000, pacs=("wf-ancient",)),
        _item("M-owner-0005", status="dropped", closed=2_000),
    ]

    model = overview.build_model(
        _snapshot(_run("wf-in")), node=NODE, missions=items
    )

    assert [entry["id"] for entry in model["workItems"]] == [
        "M-owner-0001",
        "M-owner-0002",
        "M-owner-0003",
        "M-owner-0004",
        "M-owner-0005",
    ]
    by_id = {entry["id"]: entry for entry in model["workItems"]}
    assert [by_id[f"M-owner-000{i}"]["status"] for i in range(1, 6)] == [
        "todo",
        "doing",
        "blocked",
        "done",
        "dropped",
    ]
    # Closed far before the window start: archived, not recent.
    assert by_id["M-owner-0004"]["closed"] == 1_000
    assert by_id["M-owner-0004"]["closed"] < model["workItemsArchiveCutoffMs"]
    assert by_id["M-owner-0005"]["closed"] < model["workItemsArchiveCutoffMs"]
    assert by_id["M-owner-0001"]["opened"] == 1_000
    assert by_id["M-owner-0001"]["closed"] is None


def test_checklist_progress_counts_and_items_are_kept() -> None:
    item = _item(
        "M-owner-0001",
        checklist=(("write spec", True), ("review", False), ("ship it", True)),
    )

    model = overview.build_model(_snapshot(), node=NODE, missions=[item])

    entry = model["workItems"][0]
    assert entry["checklist"] == {"done": 2, "total": 3}
    assert entry["checklistItems"] == [
        {"text": "write spec", "done": True},
        {"text": "review", "done": False},
        {"text": "ship it", "done": True},
    ]
    assert model["workItems"][0]["depends"] == []
    assert model["workItems"][0]["assignee"] is None


def test_depends_and_assignee_owner_use_short_actor_names() -> None:
    item = _item(
        "M-owner-0001",
        owner=AGENT,
        assignee=AGENT,
        depends=("M-owner-0000", "M-owner-0009"),
    )

    model = overview.build_model(_snapshot(), node=NODE, missions=[item])

    entry = model["workItems"][0]
    assert entry["owner"] == "worker-a"
    assert entry["assignee"] == "worker-a"
    assert entry["depends"] == ["M-owner-0000", "M-owner-0009"]


def test_last_log_comes_from_the_tail_and_is_null_when_empty() -> None:
    lines = [
        f"- 2026-10-07T12:{i:02d}:00+00:00 user:owner: note {i}"
        for i in range(25)
    ]
    quiet = _item("M-owner-0001")
    chatty = _item("M-owner-0002", log=tuple(lines))

    model = overview.build_model(
        _snapshot(), node=NODE, missions=[quiet, chatty]
    )

    by_id = {entry["id"]: entry for entry in model["workItems"]}
    assert by_id["M-owner-0001"]["lastLog"] is None
    assert by_id["M-owner-0001"]["log"] == []
    chatty_entry = by_id["M-owner-0002"]
    assert chatty_entry["log"] == lines[-20:]
    assert all(len(line) <= overview.WORK_ITEM_LOG_LINE_LIMIT for line in chatty_entry["log"])
    assert chatty_entry["lastLog"] == {
        "text": "note 24",
        "line": "- 2026-10-07T12:24:00+00:00 user:owner: note 24",
    }


def test_pacs_lists_every_graph_but_pacs_in_window_only_present_runs() -> None:
    item = _item("M-owner-0001", pacs=("wf-in-2", "wf-gone", "wf-in-1"))

    model = overview.build_model(
        _snapshot(_run("wf-in-1"), _run("wf-in-2")), node=NODE, missions=[item]
    )

    entry = model["workItems"][0]
    assert entry["pacs"] == ["wf-gone", "wf-in-1", "wf-in-2"]
    assert entry["pacsInWindow"] == ["wf-in-1", "wf-in-2"]


def test_body_text_is_capped_and_secrets_are_redacted() -> None:
    item = _item(
        "M-owner-0001",
        acceptance=f"pushed with {SECRET} " + "x" * 900,
        checklist=((f"rotate {SECRET} token", False),),
        log=(
            f"- 2026-10-07T12:00:00+00:00 user:owner: used {SECRET} to push",
            "- 2026-10-07T13:00:00+00:00 user:owner: "
            + "y" * 400,
        ),
    )

    model = overview.build_model(_snapshot(), node=NODE, missions=[item])

    entry = model["workItems"][0]
    assert SECRET not in str(model["workItems"])
    assert "[已隐去]" in entry["acceptance"]
    assert len(entry["acceptance"]) == overview.WORK_ITEM_ACCEPTANCE_LIMIT
    assert entry["checklistItems"][0]["text"].startswith("rotate [已隐去]")
    assert len(entry["checklistItems"][0]["text"]) <= overview.WORK_ITEM_CHECKLIST_TEXT_LIMIT
    long_prefix = "- 2026-10-07T13:00:00+00:00 user:owner: "
    assert entry["log"][1] == long_prefix + "y" * (
        overview.WORK_ITEM_LOG_LINE_LIMIT - len(long_prefix)
    )
    assert len(entry["log"][1]) == overview.WORK_ITEM_LOG_LINE_LIMIT
    assert entry["lastLog"]["text"] == "y" * overview.WORK_ITEM_LAST_LOG_TEXT_LIMIT
    assert entry["lastLog"]["line"] == entry["log"][1]
    assert "[已隐去]" in entry["log"][0]
    assert entry["log"][0].startswith(
        "- 2026-10-07T12:00:00+00:00 user:owner: used [已隐去]"
    )


def test_old_approval_flags_are_not_projected_as_authority() -> None:
    flagged = _item("M-owner-0001", acceptance="must be accepted", status="doing")
    flagged["unverifiedOperator"] = True
    flagged["ownerActions"] = [{"action": "done", "verified": True}]
    clean = _item("M-owner-0002")

    model = overview.build_model(
        _snapshot(), node=NODE, missions=[flagged, clean]
    )

    by_id = {entry["id"]: entry for entry in model["workItems"]}
    assert all("unverifiedOperator" not in entry and "ownerActions" not in entry for entry in by_id.values())


def test_missions_and_lines_keep_their_window_linked_behaviour() -> None:
    linked = _item(
        "M-owner-0001",
        title="Login hints",
        status="doing",
        pacs=("wf-a",),
    )
    archived = _item("M-owner-0002", title="Old thing", status="done", closed=1_000)

    model = overview.build_model(
        _snapshot(_run("wf-a", name="fix-login")), node=NODE, missions=[linked, archived]
    )

    # missions[]: five fields, window-linked items only — unchanged.
    assert model["missions"] == [
        {
            "id": "M-owner-0001",
            "title": "Login hints",
            "status": "doing",
            "owner": OWNER,
            "pacs": ["wf-a"],
        }
    ]
    lines = {line["id"]: line for line in model["lines"]}
    assert f"{NODE}|{OWNER}|m:M-owner-0001" in lines
    assert f"{NODE}|{OWNER}|m:M-owner-0002" not in lines
    assert lines[f"{NODE}|{OWNER}|m:M-owner-0001"]["runs"] == ["wf-a"]
    # workItems[]: both, whatever their window linkage.
    assert [entry["id"] for entry in model["workItems"]] == [
        "M-owner-0001",
        "M-owner-0002",
    ]


def test_items_are_sorted_by_id_and_the_cutoff_follows_the_window() -> None:
    model = overview.build_model(
        _snapshot(),
        node=NODE,
        missions=[
            _item("M-owner-0009"),
            _item("M-owner-0002"),
            _item("M-owner-0010"),
        ],
        window_days=3,
    )

    assert [entry["id"] for entry in model["workItems"]] == [
        "M-owner-0002",
        "M-owner-0009",
        "M-owner-0010",
    ]
    assert model["workItemsArchiveCutoffMs"] == NOW - 3 * DAY_MS


def test_content_key_ignores_build_time_but_not_work_item_state() -> None:
    item = _item("M-owner-0001", checklist=(("review", False),))

    first = overview.build_model(_snapshot(), node=NODE, missions=[item])
    later = overview.build_model(
        {**_snapshot(), "observedAt": NOW + 123_456}, node=NODE, missions=[item]
    )

    # observedAt and the cutoff derived from it are build time, not state.
    assert overview.content_key(first) == overview.content_key(later)
    ticked = overview.build_model(
        _snapshot(),
        node=NODE,
        missions=[_item("M-owner-0001", checklist=(("review", True),))],
    )
    assert overview.content_key(first) != overview.content_key(ticked)


def test_loaded_work_items_reach_the_model_in_full_projection_form() -> None:
    """The loading side (parse_mission) carries every field the page needs."""

    parsed = mission_views.parse_mission(
        "## M-owner-0001 Login hints\n\n"
        "```yaml\n"
        "id: M-owner-0001\n"
        "status: doing\n"
        f"owner: {OWNER}\n"
        f"assignee: {AGENT}\n"
        "checklist:\n  - {text: write spec, done: true}\n"
        "depends: [M-owner-0000]\n"
        "acceptance: login page shows hints\n"
        "opened: 500\n"
        "closed: 600\n"
        "keywords: [login]\n"
        "pacs: [wf-a]\n"
        "```\n\n"
        "## Log\n"
        "- 2026-10-07T12:00:00+00:00 user:owner: started\n"
    )

    assert parsed["assignee"] == AGENT
    assert parsed["checklist"] == [{"text": "write spec", "done": True}]
    assert parsed["depends"] == ["M-owner-0000"]
    assert parsed["acceptance"] == "login page shows hints"
    assert parsed["opened"] == 500
    assert parsed["closed"] == 600
    assert parsed["log"] == ["- 2026-10-07T12:00:00+00:00 user:owner: started"]
    assert "unverifiedOperator" not in parsed

    model = overview.build_model(_snapshot(), node=NODE, missions=[parsed])
    assert model["workItems"][0]["checklist"] == {"done": 1, "total": 1}
    assert model["workItems"][0]["closed"] == 600


def test_plain_document_and_diagnostics_reach_the_page_without_graph_grouping() -> None:
    document = {
        "id": "orgfs:space:node", "title": "Notes", "associationEligible": False,
        "documentText": "unparsed body <script>bad()</script>",
        "path": "work/notes.md", "diagnostics": ["optional YAML unavailable"],
        "pacs": ["wf-a"], "checklist": 42, "depends": 42, "log": 42,
    }
    model = overview.build_model(_snapshot(_run("wf-a")), node=NODE, missions=[document])
    assert model["missions"] == []
    entry = model["workItems"][0]
    assert entry["documentText"] == document["documentText"]
    assert entry["path"] == "work/notes.md"
    assert entry["diagnostics"] == document["diagnostics"]
    assert entry["checklist"] == {"done": 0, "total": 0}
    assert entry["depends"] == entry["log"] == []


def test_document_preview_is_explicitly_bounded_and_redacted_without_mutating_source() -> None:
    document = {"id": "doc", "documentText": SECRET + " x" * 20_000, "diagnostics": [SECRET]}
    original = document["documentText"]
    entry = overview.build_model(_snapshot(), node=NODE, missions=[document])["workItems"][0]
    assert SECRET not in entry["documentText"]
    assert len(entry["documentText"]) == overview.WORK_ITEM_DOCUMENT_LIMIT
    assert entry["documentTruncated"] is True
    assert entry["diagnostics"] == ["[已隐去]"]
    assert document["documentText"] == original
