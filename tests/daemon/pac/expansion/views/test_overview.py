"""Overview relations are explicit and do not depend on misleading names."""

from __future__ import annotations

from hyprial.daemon.impl.pac.views import overview


def test_expansion_child_inherits_parent_line_and_creates_node_links() -> None:
    parent = {
        "graphId": "parent",
        "name": "unrelated-name",
        "sender": "agent:owner:node:planner",
        "state": "running",
        "createdAtMs": 1,
        "closedAtMs": None,
        "routineName": None,
        "nodes": [
            {"nodeId": "plan", "kind": "task", "state": "done", "expands": "build"},
            {
                "nodeId": "build",
                "kind": "expansion",
                "state": "requested",
                "systemOwned": True,
                "childGraphId": "child",
                "childNodes": [{"nodeId": "slice", "state": "done", "reasonRef": "ok"}],
            },
            {"nodeId": "integrate", "kind": "task", "state": "pending"},
        ],
        "edges": [{"from": "plan", "to": "build"}, {"from": "build", "to": "integrate"}],
        "roster": [],
        "graphReason": None,
    }
    child = {
        "graphId": "child",
        "name": "same-looking-name-as-other-unrelated-run",
        "sender": "agent:other:node:worker",
        "state": "completed",
        "createdAtMs": 2,
        "closedAtMs": 3,
        "routineName": None,
        "parentGraphId": "parent",
        "parentNodeId": "build",
        "nodes": [{"nodeId": "slice", "kind": "task", "state": "done"}],
        "edges": [],
        "roster": [],
        "graphReason": None,
    }
    model = overview.build_model(
        {"observedAt": 9, "runs": [parent, child], "routines": []}, node="node"
    )
    runs = {run["id"]: run for run in model["runs"]}
    assert runs["child"]["line"] == runs["parent"]["line"]
    assert model["nodeLinks"] == [
        {"from": {"graphId": "parent", "nodeId": "plan"}, "to": {"graphId": "child", "nodeId": "slice"}, "kind": "expand"},
        {"from": {"graphId": "child", "nodeId": "slice"}, "to": {"graphId": "parent", "nodeId": "integrate"}, "kind": "expand"},
    ]
    assert runs["parent"]["nodes"][1]["childNodes"][0]["nodeId"] == "slice"


def test_missing_parent_keeps_child_visible_and_redacts_output() -> None:
    child = {
        "graphId": "child",
        "name": "child",
        "sender": "agent:owner:node:worker",
        "state": "completed",
        "createdAtMs": 1,
        "closedAtMs": 2,
        "routineName": None,
        "parentGraphId": "filtered-parent",
        "parentNodeId": "build",
        "nodes": [{"nodeId": "slice", "kind": "task", "outputText": "ghp_" + "A" * 30}],
        "edges": [],
        "roster": [],
        "graphReason": None,
    }
    model = overview.build_model({"observedAt": 9, "runs": [child], "routines": []}, node="node")
    assert model["runs"][0]["line"]
    assert model["runs"][0]["nodes"][0]["output"] == "[已隐去]"


def test_expansion_child_summary_is_bounded_and_redacted() -> None:
    child_nodes = [
        {
            "nodeId": f"slice-{index}",
            "state": "failed",
            "reasonRef": "ghp_" + "S" * 30,
            "unexpected": "must not cross the surface",
        }
        for index in range(40)
    ]
    parent = {
        "graphId": "parent",
        "name": "parent",
        "sender": "agent:owner:node:planner",
        "state": "running",
        "createdAtMs": 1,
        "closedAtMs": None,
        "routineName": None,
        "nodes": [
            {
                "nodeId": "build",
                "kind": "expansion",
                "systemOwned": True,
                "childGraphId": "child",
                "childState": "failed",
                "childNodes": child_nodes,
            }
        ],
        "edges": [],
        "roster": [],
        "graphReason": None,
    }
    child = {
        "graphId": "child",
        "name": "child",
        "sender": "agent:owner:node:worker",
        "state": "failed",
        "createdAtMs": 2,
        "closedAtMs": 3,
        "routineName": None,
        "parentGraphId": "parent",
        "parentNodeId": "build",
        "nodes": [],
        "edges": [],
        "roster": [],
        "graphReason": None,
    }
    model = overview.build_model(
        {"observedAt": 9, "runs": [parent, child], "routines": []}, node="node"
    )
    summary = model["runs"][0]["nodes"][0]["childNodes"]
    assert len(summary) == 24
    assert summary[0] == {
        "nodeId": "slice-0",
        "state": "failed",
        "reasonRef": "[已隐去]",
    }


def test_explicit_mission_line_wins_for_expansion_child() -> None:
    parent = {
        "graphId": "parent",
        "name": "name-unrelated-to-child",
        "sender": "agent:owner:node:planner",
        "state": "running",
        "createdAtMs": 1,
        "closedAtMs": None,
        "routineName": None,
        "nodes": [],
        "edges": [],
        "roster": [],
        "graphReason": None,
    }
    child = {
        "graphId": "child",
        "name": "other-unrelated-name",
        "sender": "agent:worker:node:builder",
        "state": "completed",
        "createdAtMs": 2,
        "closedAtMs": 3,
        "routineName": None,
        "parentGraphId": "parent",
        "parentNodeId": "build",
        "nodes": [],
        "edges": [],
        "roster": [],
        "graphReason": None,
    }
    model = overview.build_model(
        {"observedAt": 9, "runs": [parent, child], "routines": []},
        node="node",
        missions=[
            {
                "id": "mission-1",
                "title": "Explicit mission",
                "owner": "user:owner",
                "status": "active",
                "pacs": ["parent"],
            }
        ],
    )
    lines = {line["id"]: line for line in model["lines"]}
    mission_line = next(line for line in lines.values() if line["kind"] == "mission")
    assert mission_line["runs"] == ["parent", "child"]
    assert model["runs"][1]["line"] == mission_line["id"]
