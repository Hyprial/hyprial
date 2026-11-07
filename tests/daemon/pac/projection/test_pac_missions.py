"""Missions helper: parsing, the new-graph set, pre-filter, jev payloads, round state."""

from __future__ import annotations

import json
from typing import Any

import pytest

from hyprial.daemon.impl.pac.views import missions

DAY = 24 * 3600 * 1000
NOW = 100 * DAY


def mission_text(mission_id: str = "M-allen-0007", *, status: str = "active", pacs: str = "") -> str:
    return (
        f"## {mission_id} PAC overview rollout\n\n"
        "```yaml\n"
        f"id: {mission_id}\n"
        f"status: {status}\n"
        "owner: agent:x:y:user-exp-improver\n"
        'keywords: [pac-overview, "#996"]\n'
        f"{pacs}"
        "```\n\n背景：free text\n"
    )


def test_parse_accepts_block_and_inline_pacs() -> None:
    block = missions.parse_mission(mission_text(pacs="pacs:\n  - wf-a\n  - wf-b\n"))
    inline = missions.parse_mission(mission_text(pacs="pacs: [wf-a, wf-b]\n"))

    assert block["pacs"] == inline["pacs"] == ["wf-a", "wf-b"]
    assert block["title"] == "PAC overview rollout"
    assert block["keywords"] == ["pac-overview", "#996"]


def test_yaml_title_cannot_override_the_heading_title() -> None:
    """Review 1218 (2): the Markdown heading, not a YAML ``title:`` key, wins.

    ``WorkItem.to_dict`` spreads the YAML head after its own ``title``, so a
    stray ``title:`` key in the block used to override the heading the page
    and the model read.
    """

    text = mission_text().replace("keywords:", "title: YAML title\nkeywords:")
    parsed = missions.parse_mission(text)
    assert parsed["title"] == "PAC overview rollout"


@pytest.mark.parametrize(
    "text, reason",
    [
        ("no block here", "no ```yaml block"),
        (mission_text(status="pdonused"), "status must be one of"),
        (mission_text("X-1"), "id must be"),
        (mission_text(pacs="pacs: wf-a\n"), "must be lists"),
        ("```yaml\nid: [unclosed\n```\n", "yaml:"),
    ],
)
def test_parse_rejects_what_the_squire_must_repair(text: str, reason: str) -> None:
    with pytest.raises(missions.MissionParseError, match=reason.replace("[", r"\[")):
        missions.parse_mission(text)


class Daemon:
    """A fake daemon: orgfs files, workflow runs and round outputs."""

    def __init__(self, *, files=None, runs=None, outputs=None, denied=()) -> None:
        self.files = files or {}
        self.runs = runs or []
        self.outputs = outputs or {}
        self.denied = set(denied)
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def __call__(self, method: str, params: dict[str, Any]) -> Any:
        self.calls.append((method, dict(params)))
        if method == "orgfs.ls":
            return {
                "nodes": [
                    {"name": name, "node_id": name, "modified_by": "user:a", **meta}
                    for name, (_, meta) in self.files.items()
                ]
            }
        if method == "orgfs.read":
            text, _ = self.files[params["path"].removeprefix("id:")]
            return {"text": text, "version": "v1"}
        if method == "workflow.list":
            assert params.get("all") is True
            return {"runs": self.runs}
        if method == "workflow.node.inspect":
            if params["runId"] in self.denied:
                raise RuntimeError("CALLER_NOT_AUTHORIZED")
            return {"node": {"outputText": self.outputs.get(params["runId"])}}
        raise AssertionError(method)


def run(graph_id: str, name: str, *, age_days: float = 0, routine=None, state="running", sender="user:a"):
    return {
        "graphId": graph_id,
        "name": name,
        "sender": sender,
        "state": state,
        "createdAtMs": int(NOW - age_days * DAY),
        "routineName": routine,
    }


def test_load_reports_unparseable_and_name_conflicts_and_ignores_other_files() -> None:
    daemon = Daemon(
        files={
            "M-allen-0001.md": (mission_text("M-allen-0001", pacs="pacs:\n  - wf-a\n"), {}),
            "M-allen-0002.md": (mission_text("M-allen-0002", status="pdonused"), {}),
            "M-bo-0001.md": (mission_text("M-bo-0001"), {"name_conflict": True}),
            "M-bo-0002.md": (mission_text("M-bo-0002"), {"deleted": True}),
            "README.md": ("hello", {}),
        }
    )

    loaded = missions.load_missions(daemon, "space")

    assert [m["id"] for m in loaded["missions"]] == ["M-allen-0001"]
    assert loaded["missions"][0]["version"] == "v1"
    assert loaded["unparseable"][0]["path"] == "work/M-allen-0002.md"
    assert loaded["conflicts"] == [
        {"path": "work/M-bo-0001.md", "nodeIds": ["M-bo-0001.md"]}
    ]
    assert daemon.calls[0] == ("orgfs.ls", {"spaceId": "space", "path": "work"})


def test_previous_state_takes_the_last_completed_round_and_passes_over_skipped() -> None:
    runs = [
        run("r1", "missions", age_days=0.3, routine="missions", state="completed"),
        run("r2", "missions", age_days=0.2, routine="missions", state="completed"),
        run("r3", "missions", age_days=0.1, routine="missions", state="failed"),
        run("r4", "missions", age_days=0.05, routine="missions", state="cancelled"),
        run("x", "other", age_days=0.01, routine="other", state="completed"),
    ]
    outputs = {
        "r1": json.dumps({"asked": ["wf-a"], "declined": ["wf-b"]}),
        "r2": json.dumps(missions.SKIPPED_STATE),
    }

    state = missions.previous_state(Daemon(runs=runs, outputs=outputs), runs, "missions")

    assert state == {"from": "r1", "asked": ["wf-a"], "declined": ["wf-b"]}
    assert missions.previous_state(Daemon(), [], "missions") == {"asked": [], "declined": []}


def test_dropped_missions_do_not_collect_new_graphs() -> None:
    dropped = {
        "id": "M-allen-0001",
        "title": "retired",
        "status": "dropped",
        "owner": "user:allen",
        "keywords": ["work"],
        "pacs": [],
    }

    assert missions.candidates(
        run("wf-new", "work item", sender="user:allen"),
        [dropped],
        {},
    ) == []


@pytest.mark.parametrize("output, denied", [(None, ()), ("not json", ()), ("[]", ()), ("{}", ("r1",))])
def test_an_unreadable_previous_round_is_an_error_not_an_empty_state(output, denied) -> None:
    runs = [run("r1", "missions", routine="missions", state="completed")]
    daemon = Daemon(runs=runs, outputs={"r1": output}, denied=denied)

    with pytest.raises(missions.PreviousRoundUnreadable, match="r1"):
        missions.previous_state(daemon, runs, "missions")


def test_new_graphs_leave_out_routines_recorded_asked_declined_and_old_graphs() -> None:
    runs = [
        run("wf-new", "fix-login", age_days=1),
        run("wf-old", "fix-login", age_days=8),
        run("wf-routine", "missions", routine="missions"),
        run("wf-linked", "x", age_days=1),
        run("wf-asked", "x", age_days=1),
        run("wf-declined", "x", age_days=1),
    ]
    state = {"asked": ["wf-asked"], "declined": ["wf-declined"]}

    fresh, deferred = missions.new_graphs(runs, [{"pacs": ["wf-linked"]}], state, now_ms=NOW)

    assert [r["graphId"] for r in fresh] == ["wf-new"]
    assert deferred == 0


def test_new_graphs_take_the_oldest_first_and_defer_the_rest() -> None:
    runs = [run(f"wf-{i:02d}", "x", age_days=i / 10) for i in range(25)]

    fresh, deferred = missions.new_graphs(runs, [], {}, now_ms=NOW, limit=20)

    assert [r["graphId"] for r in fresh][:2] == ["wf-24", "wf-23"]
    assert len(fresh) == 20 and deferred == 5


def test_candidates_rank_pr_keywords_and_reruns_and_skip_done_missions() -> None:
    by_id = {"wf-m1": run("wf-m1", "pac-overview-lanes-1009-abc123abc123")}
    mission_list = [
        {"id": "M-a-0001", "status": "active", "keywords": ["#1009"], "pacs": []},
        {"id": "M-a-0002", "status": "active", "keywords": ["lanes"], "pacs": []},
        {"id": "M-a-0003", "status": "active", "keywords": [], "pacs": ["wf-m1"]},
        {"id": "M-a-0004", "status": "done", "keywords": ["#1009", "lanes"], "pacs": []},
        {"id": "M-a-0005", "status": "active", "keywords": ["unrelated"], "pacs": []},
    ]

    found = missions.candidates(
        run("wf-n", "pac-overview-lanes-1009-def456def456"), mission_list, by_id
    )

    assert [c["id"] for c in found] == ["M-a-0001", "M-a-0003", "M-a-0002"]
    assert found[1]["reasons"][0] == "rerun of wf-m1"
    assert "same sender" in found[1]["reasons"]


def test_candidates_are_capped() -> None:
    mission_list = [
        {"id": f"M-a-{i:04d}", "status": "active", "keywords": ["login"], "pacs": []}
        for i in range(8)
    ]

    assert len(missions.candidates(run("wf", "fix-login"), mission_list, {})) == 5


def test_payload_is_one_noul_per_candidate_with_minimal_mission_fields() -> None:
    by_id = {
        "M-a-0001": {
            "id": "M-a-0001", "title": "t", "keywords": ["k"], "status": "active",
            "owner": "o", "pacs": ["secret-graph"], "path": "p",
        }
    }

    payload = missions.jev_payload(run("wf", "fix"), [{"id": "M-a-0001"}], by_id)

    assert payload is not None
    assert payload["questions"]["c0"]["type"] == "noul"
    assert payload["state"]["pac"] == {"graphId": "wf", "name": "fix"}
    assert "user:a" not in json.dumps(payload)
    assert payload["state"]["candidates"] == [
        {"id": "M-a-0001", "title": "t", "keywords": ["k"], "status": "active"}
    ]
    assert missions.jev_payload(run("wf", "fix"), [{"id": "M-a-0001"}], by_id, limit=100) is None


def test_collect_skips_while_an_earlier_round_is_running() -> None:
    runs = [
        run("r-old", "missions", routine="missions", state="running"),
        run("r-me", "missions", routine="missions", state="running"),
    ]
    daemon = Daemon(runs=runs)

    collected = missions.collect(daemon, "s", routine="missions", current="r-me", now_ms=NOW)

    assert collected["skipped"] == {"running": ["r-old"]}
    assert collected["state"] == missions.SKIPPED_STATE
    assert [m for m, _ in daemon.calls] == ["workflow.list"]


def test_collect_end_to_end_prunes_state_and_reports_a_full_list() -> None:
    runs = [
        run("r-me", "missions", routine="missions"),
        run("r-1", "missions", age_days=0.1, routine="missions", state="completed"),
        run("wf-new", "pac-overview-fix", age_days=0.01),
        run("wf-quiet", "zzz", age_days=0.02),
        run("wf-asked", "x", age_days=1),
        run("wf-answered", "x", age_days=1),
    ]
    runs += [run(f"wf-pad{i}", "pad", age_days=2, routine="other", state="completed") for i in range(500 - len(runs))]
    daemon = Daemon(
        files={"M-a-0001.md": (mission_text("M-a-0001", pacs="pacs:\n  - wf-answered\n"), {})},
        runs=runs,
        outputs={"r-1": json.dumps({"asked": ["wf-asked", "wf-answered", "wf-gone"], "declined": []})},
    )

    collected = missions.collect(daemon, "s", routine="missions", current="r-me", now_ms=NOW)

    assert collected["previous"]["asked"] == ["wf-asked"]
    assert collected["coveredSinceMs"] == NOW - 2 * DAY
    graphs = {g["graphId"]: g for g in collected["graphs"]}
    assert graphs["wf-new"]["jev"]["questions"].keys() == {"c0"}
    assert graphs["wf-quiet"]["candidates"] == [] and graphs["wf-quiet"]["jev"] is None


def test_finish_picks_the_likeliest_candidate_and_carries_state() -> None:
    collected = {
        "previous": {"asked": ["wf-old"], "declined": ["wf-no"]},
        "graphs": [
            {"graphId": "wf-1", "candidates": [{"id": "M-a"}, {"id": "M-b"}]},
            {"graphId": "wf-2", "candidates": []},
            {"graphId": "wf-3", "candidates": [{"id": "M-a"}]},
            {"graphId": "wf-4", "candidates": [{"id": "M-a"}], "tooLarge": True},
            {"graphId": "wf-5", "candidates": [{"id": "M-a"}]},
        ],
    }
    replies = {
        "wf-1": json.dumps({"answers": {"c0": {"type": "noul", "noul": 0.2}, "c1": {"type": "noul", "noul": 0.93}}}),
        "wf-3": "not json",
        "wf-5": {"output": {"answers": {"c0": {"type": "noul", "noul": 1.5}}}},
    }

    finished = missions.finish(collected, replies, declined=["wf-old"])

    results = {r["graphId"]: r for r in finished["results"]}
    assert results["wf-1"]["match"] == "M-b" and results["wf-1"]["confidence"] == 0.93
    assert results["wf-2"]["reason"] == "no candidate" and results["wf-2"]["match"] is None
    assert results["wf-4"]["reason"].startswith("payload over limit")
    assert finished["state"] == {
        "asked": ["wf-1", "wf-2", "wf-4"],
        "declined": ["wf-no", "wf-old"],
        "failed": ["wf-3", "wf-5"],
    }


def test_state_is_bounded_by_dropping_the_oldest_entries() -> None:
    asked = [f"wf-{i:032d}" for i in range(3000)]

    state = missions.bounded_state(asked=asked, declined=["wf-d"], failed=[])

    assert len(json.dumps(state).encode()) <= missions.STATE_LIMIT
    assert state["asked"][-1] == asked[-1] and state["asked"][0] != asked[0]
    assert state["declined"] == ["wf-d"]
