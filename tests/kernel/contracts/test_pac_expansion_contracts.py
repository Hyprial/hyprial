"""Contracts-first checks for PAC expansion data and shared budgets."""

from dataclasses import FrozenInstanceError, replace

import pytest

from hyprial.daemon.impl.pac.contracts.expansion.types import PreparedExpansion
from hyprial.daemon.impl.pac.contracts.workflow import load_workflow_text
from hyprial.kernel import interface as kernel_contracts
from hyprial.kernel.impl.contracts.daemon import lifecycle_budgets


ORDINARY_CANONICAL = (
    '{"completion": "owner flag with current request evidence", "edges": [], '
    '"escalateTo": null, "name": "work", "nodes": [{"deadlineMs": null, '
    '"firstOutputEta": null, "humanGates": null, "id": "build", '
    '"kind": "task", "launch": {"args": [], "harness": "codex", '
    '"model": "gpt-5.6-luna", "provider": null, "tier": "fast"}, '
    '"owner": null, "ownership": "workflow", "role": "execute", '
    '"task": "implement", "timeoutMs": 3600000, "worker": "build"}], '
    '"onFailure": "terminate", "summary": "", "version": 2, '
    '"workerCleanup": "graph close; borrowed actors are never stopped"}'
)


def _ordinary_spec():
    return load_workflow_text(
        "version: 2\nname: work\nnodes:\n- id: build\n  task: implement\n"
    )


def test_ordinary_workflow_canonical_bytes_are_unchanged() -> None:
    spec = _ordinary_spec()

    assert spec.nodes[0].expands is None
    assert spec.nodes[0].limits is None
    assert spec.expansion_policy is None
    assert spec.canonical == ORDINARY_CANONICAL


def test_expansion_fields_are_conditionally_serialized_with_system_ownership() -> None:
    ordinary = _ordinary_spec()
    planner = replace(
        ordinary.nodes[0],
        expands="join",
        limits={"maxNodes": 2},
    )
    enabled = replace(
        ordinary,
        nodes=(planner,),
        expansion_policy={"version": 1, "workRoot": "/work"},
    )
    placeholder = replace(ordinary.nodes[0], kind="expansion")

    assert enabled.to_json()["nodes"][0]["expands"] == "join"
    assert enabled.to_json()["nodes"][0]["limits"] == {"maxNodes": 2}
    assert enabled.to_json()["expansionPolicy"] == {
        "version": 1,
        "workRoot": "/work",
    }
    assert placeholder.to_json()["ownership"] == "system"
    assert "expansionPolicy" not in replace(
        ordinary,
        expansion_policy={"version": 1},
    ).to_json()


def test_prepared_expansion_is_a_frozen_data_only_contract() -> None:
    spec = _ordinary_spec()
    prepared = PreparedExpansion(
        parent_graph_id="wf-parent",
        planner_node_id="plan",
        planner_request_id="workflow-request:one",
        placeholder_node_id="join",
        child_graph_id="wf-child",
        expansion_digest="sha256:digest",
        captured_input_token="sha256:input",
        effective_limits={"maxNodes": 2},
        policy_snapshot={"version": 1, "workRoot": "/work"},
        spec=spec,
        prepared_artifacts={"specificationRef": "artifact"},
        worktrees=({"nodeId": "build"},),
    )

    assert prepared.spec is spec
    assert prepared.worktrees == ({"nodeId": "build"},)
    with pytest.raises(FrozenInstanceError):
        prepared.child_graph_id = "wf-other"  # type: ignore[misc]


def test_pac_git_budget_is_registered_once_on_the_kernel_interface() -> None:
    assert lifecycle_budgets.PAC_WORKTREE_GIT_TIMEOUT_SECONDS == 30.0
    assert kernel_contracts.PAC_WORKTREE_GIT_TIMEOUT_SECONDS == 30.0
    assert "PAC_WORKTREE_GIT_TIMEOUT_SECONDS" in kernel_contracts.__all__
    assert not hasattr(lifecycle_budgets, "PAC_EXPANSION_PREFLIGHT_TIMEOUT_SECONDS")
