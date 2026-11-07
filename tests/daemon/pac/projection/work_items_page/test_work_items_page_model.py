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
from hyprial.daemon.impl.pac.views.work_items import (
    new_work_item,
    replace_head,
    replace_log,
)

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

    item = new_work_item(
        item_id=item_id,
        title=title,
        owner=owner,
        assignee=assignee,
        checklist=[text for text, _ in checklist],
        depends=list(depends),
        acceptance=acceptance,
        opened=opened,
    )
    for index, (_, done) in enumerate(checklist):
        if done:
            entries = [dict(entry) for entry in item.head["checklist"]]
            entries[index]["done"] = True
            item = replace_head(item, checklist=entries)
    if status != "todo":
        item = replace_head(item, status=status)
    if closed is not None:
        item = replace_head(item, closed=closed)
    if pacs:
        item = replace_head(item, pacs=list(pacs))
    if log:
        item = replace_log(item, list(log))
    projected = item.to_dict()
    return {
        **projected,
        "pacs": [str(graph).strip() for graph in projected["pacs"] if str(graph).strip()],
    }


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
            # acceptance text no verified owner action covers yet
            "unverifiedOperator": True,
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


def test_unverified_operator_passes_through() -> None:
    flagged = _item("M-owner-0001", acceptance="must be accepted", status="doing")
    clean = _item("M-owner-0002")

    model = overview.build_model(
        _snapshot(), node=NODE, missions=[flagged, clean]
    )

    by_id = {entry["id"]: entry for entry in model["workItems"]}
    assert flagged["unverifiedOperator"] is True  # the input carries the flag
    assert by_id["M-owner-0001"]["unverifiedOperator"] is True
    assert by_id["M-owner-0002"]["unverifiedOperator"] is False


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
    assert parsed["unverifiedOperator"] is True  # acceptance never owner-verified

    model = overview.build_model(_snapshot(), node=NODE, missions=[parsed])
    assert model["workItems"][0]["checklist"] == {"done": 1, "total": 1}
    assert model["workItems"][0]["closed"] == 600
