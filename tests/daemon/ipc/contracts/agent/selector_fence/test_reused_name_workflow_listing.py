"""KP-117, pinned as it is: ``workflow.list`` lists by the live name URI.

A workflow YAML names its node owner by name URI, and ``workflow.list`` with
an agent selector also views at the live agent's name URI.  So after an
agent is destroyed and a new agent takes its name, a run whose YAML named the
earlier agent is listed for the new one.  It is a listing only; the proper
fix binds runs to an agent id when they start.
"""

from __future__ import annotations

from tests.daemon.ipc.contracts.agent.selector_fence.test_selector_fence import (
    AGENT,
    USER,
    WORKFLOW_YAML,
    _routed,
    app as app,
)


def test_kp117_workflow_list_lists_an_earlier_same_name_agents_run(app) -> None:
    first = app.agents.create(AGENT)
    app._workflow_service.start(
        yaml_text=WORKFLOW_YAML, sender=USER, operation_key="kp117-earlier"
    )
    assert len(_routed(app, "workflow.list", {"agent": AGENT})["runs"]) == 1
    assert app.agents.destroy(AGENT)

    second = app.agents.create(AGENT)

    assert second.agent_id != first.agent_id
    assert len(_routed(app, "workflow.list", {"agent": AGENT})["runs"]) == 1
