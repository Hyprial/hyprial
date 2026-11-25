"""Same-source Contract Mock cases for the Agent relations read methods.

The ``agent.home-census``, ``routine.list``, ``routine.status`` and
``workflow.list`` example sets, split out of the package module only to keep
both under the 1000-line gate.  The package's ``REQUEST_EXAMPLES`` still
aggregates them in their original order; the decision records live there.
The values are the same synthetic agent as the lifecycle sets.
"""

from __future__ import annotations

from hyprial.identity import PAC_PRINCIPAL_UNVERIFIED
from hyprial.kernel import (
    MethodExample,
    MethodExampleError,
    MethodExampleSet,
    ipc_errors,
)

__all__ = ["RELATIONS_EXAMPLES"]

_AGENT = "worker"
_AGENT_URI = "agent:acme:node-1:worker"
_PROVIDER = "claude"


_ROUTINE_ROW = {
    "name": "heartbeat",
    "owner": "user:acme",
    "enabled": True,
    "nextDueMs": 1760000060000,
    "sourceErrorStreak": 0,
    "outcomes": ["done"],
    "registrationId": None,
    "mode": "scheduled",
    "role": "dispatch",
    "actor": _AGENT_URI,
    "actorOwnership": "external",
    "launch": None,
    "overlap": None,
    "missedPeriods": None,
    "inFlight": [],
    "lastDispatchAtMs": 1760000000000,
    "lastOutcome": "done",
}

_SCHEDULE_EVENT = {
    "routine": "heartbeat",
    "slot_ms": 1760000060000,
    "disposition": "dispatched",
    "count": 1,
    "reason": "timer",
    "recorded_at_ms": 1760000060005,
}

_RUN_ROW = {
    "runId": "wf-0001",
    "graphId": "wf-0001",
    "backend": "pac",
    "name": "brief-relations",
    "state": "running",
    "sender": "user:acme",
    "createdAtMs": 1760000000000,
    "closedAtMs": None,
    "onFailure": "terminate",
    "routineName": None,
    "lastProgressAtMs": 1760000001000,
    "currentNode": {"nodeId": "work", "state": "requested"},
}

_CENSUS_ROW = {
    "actor": _AGENT,
    "uri": _AGENT_URI,
    "preferredHarness": _PROVIDER,
    "lastHarness": _PROVIDER,
    "harness": _PROVIDER,
    "homeProvisioned": True,
    "explicitConfig": True,
    "configInsideHome": True,
    "cwdInsideHome": True,
    "residentOrDsh": False,
    "transferReceived": False,
    "mode": "p2",
    "legacyReason": None,
}

_AGENT_HOME_CENSUS = MethodExampleSet(
    method="agent.home-census",
    cases=(
        MethodExample(
            name="whole-node",
            kind="success",
            description="the census of every registered agent on the node",
            params={},
            result={
                "ok": True,
                "readAtMs": 1760000000000,
                "registry": {
                    "path": "/tmp/hyprial-example/state/agents.sqlite3",
                    "version": 1,
                },
                "agents": [_CENSUS_ROW],
                "totals": {
                    "agents": 1,
                    "byModeHarness": [
                        {"mode": "p2", "harness": _PROVIDER, "count": 1}
                    ],
                    "homeUnprovisioned": 0,
                },
            },
        ),
        MethodExample(
            name="one-agent-by-actor",
            kind="success",
            description=(
                "the actor selector narrows the census to one agent and "
                "recomputes the totals"
            ),
            params={"actor": _AGENT},
            result={
                "ok": True,
                "readAtMs": 1760000000000,
                "registry": {
                    "path": "/tmp/hyprial-example/state/agents.sqlite3",
                    "version": 1,
                },
                "agents": [_CENSUS_ROW],
                "totals": {
                    "agents": 1,
                    "byModeHarness": [
                        {"mode": "p2", "harness": _PROVIDER, "count": 1}
                    ],
                    "homeUnprovisioned": 0,
                },
            },
        ),
        MethodExample(
            name="empty-registry",
            kind="empty",
            description="no agents are registered on this machine",
            params={},
            result={
                "ok": True,
                "readAtMs": 1760000000000,
                "registry": {
                    "path": "/tmp/hyprial-example/state/agents.sqlite3",
                    "version": 1,
                },
                "agents": [],
                "totals": {
                    "agents": 0,
                    "byModeHarness": [],
                    "homeUnprovisioned": 0,
                },
            },
            reason="an empty agent registry is a legal successful read",
        ),
        MethodExample(
            name="actor-wrong-type",
            kind="invalid-params",
            description="the actor selector must be a string or null",
            params={"actor": 5},
            error=MethodExampleError(
                ipc_errors.INVALID_ARGUMENT,
                "actor selector must be a bare agent name or a canonical local agent URI",
            ),
        ),
        MethodExample(
            name="unknown-agent",
            kind="domain-error",
            description="the actor selector names no registered agent",
            params={"actor": "missing-agent"},
            error=MethodExampleError(
                ipc_errors.AGENT_NOT_FOUND,
                "no agent with that name is registered on this machine",
            ),
        ),
    ),
)

_ROUTINE_LIST = MethodExampleSet(
    method="routine.list",
    cases=(
        MethodExample(
            name="agent-selector",
            kind="success",
            description="the routines the selected agent owns or acts for",
            params={"agent": _AGENT},
            result={"routines": [_ROUTINE_ROW]},
        ),
        MethodExample(
            name="caller-scoped",
            kind="success",
            description=(
                "without the selector the caller scoping answers the caller's "
                "own routines"
            ),
            params={"actor": "user:acme"},
            result={"routines": [_ROUTINE_ROW]},
        ),
        MethodExample(
            name="none-visible",
            kind="empty",
            description="no routine matches the selector or the caller scope",
            params={"agent": _AGENT},
            result={"routines": []},
            reason="an agent with no routine rows is a legal successful read",
        ),
        MethodExample(
            name="all-wrong-type",
            kind="invalid-params",
            description="the legacy all switch must be a boolean",
            params={"actor": "user:acme", "all": "yes"},
            error=MethodExampleError(
                ipc_errors.INVALID_ARGUMENT, "all must be a boolean"
            ),
        ),
        MethodExample(
            name="unknown-agent-selector",
            kind="domain-error",
            description="the agent selector names no registered agent",
            params={"agent": "missing-agent"},
            error=MethodExampleError(
                ipc_errors.AGENT_NOT_FOUND,
                "no agent with that name is registered on this machine",
            ),
        ),
        MethodExample(
            name="unverified-principal",
            kind="domain-error",
            description="the actor claim has no matching live daemon session",
            params={"actor": _AGENT_URI, "sessionRef": "stale-session"},
            error=MethodExampleError(
                PAC_PRINCIPAL_UNVERIFIED,
                "workflow actor claim has no matching live daemon session",
                data={"cause": ipc_errors.CALLER_NOT_AUTHORIZED},
            ),
        ),
    ),
)

_ROUTINE_STATUS = MethodExampleSet(
    method="routine.status",
    empty_not_applicable=(
        "status answers exactly one routine row or fails; there is no legal "
        "empty result shape"
    ),
    cases=(
        MethodExample(
            name="agent-selector",
            kind="success",
            description="one routine of the selected agent with its schedule events",
            params={"name": "heartbeat", "agent": _AGENT},
            result={
                **{
                    key: value
                    for key, value in _ROUTINE_ROW.items()
                    if key not in {"lastDispatchAtMs", "lastOutcome"}
                },
                "scheduleEvents": [_SCHEDULE_EVENT],
            },
        ),
        MethodExample(
            name="owner-caller",
            kind="success",
            description=(
                "without the selector the legacy caller scoping and ownership "
                "gate apply"
            ),
            params={"name": "heartbeat", "actor": "user:acme"},
            result={
                **{
                    key: value
                    for key, value in _ROUTINE_ROW.items()
                    if key not in {"lastDispatchAtMs", "lastOutcome"}
                },
                "scheduleEvents": [],
            },
        ),
        MethodExample(
            name="no-such-routine",
            kind="domain-error",
            description=(
                "the name is unknown, or the agent selector does not cover it; "
                "both answer identically"
            ),
            params={"name": "missing", "agent": _AGENT},
            error=MethodExampleError(
                "ROUTINE_NOT_FOUND", "no such routine: missing"
            ),
        ),
        MethodExample(
            name="caller-does-not-own",
            kind="domain-error",
            description="a resolvable non-owner caller is refused",
            params={"name": "heartbeat", "actor": _AGENT_URI, "sessionRef": "live"},
            error=MethodExampleError(
                ipc_errors.CALLER_NOT_AUTHORIZED, "caller does not own this routine"
            ),
        ),
        MethodExample(
            name="agent-selector-wrong-type",
            kind="invalid-params",
            description="the agent selector must be a string",
            params={"name": "heartbeat", "agent": 5},
            error=MethodExampleError(
                ipc_errors.INVALID_ARGUMENT,
                "agent selector must be a bare agent name or a canonical local agent URI",
            ),
        ),
        MethodExample(
            name="name-missing",
            kind="semantic-param-error",
            description="the handler requires the name after the service check",
            params={"actor": "user:acme"},
            error=MethodExampleError(
                ipc_errors.INVALID_ARGUMENT, "name must be a non-empty string"
            ),
        ),
    ),
)

_WORKFLOW_LIST = MethodExampleSet(
    method="workflow.list",
    cases=(
        MethodExample(
            name="agent-selector",
            kind="success",
            description="graphs where the selected agent created the run or owns a node",
            params={"agent": _AGENT},
            result={"runs": [_RUN_ROW]},
        ),
        MethodExample(
            name="caller-scoped",
            kind="success",
            description=(
                "without the selector the caller scoping answers the caller's "
                "own graphs"
            ),
            params={"actor": "user:acme", "limit": 50},
            result={"runs": [_RUN_ROW]},
        ),
        MethodExample(
            name="finished-run",
            kind="success",
            description=(
                "a closed run reports its close time and no current node; a "
                "selector that matches only closed graphs is a legal "
                "successful read, not an empty result"
            ),
            params={"agent": _AGENT},
            result={
                "runs": [
                    {
                        **_RUN_ROW,
                        "runId": "wf-0002",
                        "graphId": "wf-0002",
                        "state": "completed",
                        "closedAtMs": 1760000100000,
                        "lastProgressAtMs": 1760000100000,
                        "currentNode": None,
                    }
                ]
            },
        ),
        MethodExample(
            name="no-matching-graph",
            kind="empty",
            description="the selector names an agent that created no graph and owns no node",
            params={"agent": _AGENT},
            result={"runs": []},
            reason="an agent with no workflow rows is a legal successful read",
        ),
        MethodExample(
            name="limit-wrong-type",
            kind="invalid-params",
            description="the limit must be an integer",
            params={"actor": "user:acme", "limit": "ten"},
            error=MethodExampleError(
                ipc_errors.INVALID_ARGUMENT, "limit must be an integer"
            ),
        ),
        MethodExample(
            name="unknown-agent-selector",
            kind="domain-error",
            description="the agent selector names no registered agent",
            params={"agent": "missing-agent"},
            error=MethodExampleError(
                ipc_errors.AGENT_NOT_FOUND,
                "no agent with that name is registered on this machine",
            ),
        ),
    ),
)

RELATIONS_EXAMPLES: tuple[MethodExampleSet, ...] = (
    _AGENT_HOME_CENSUS, _ROUTINE_LIST, _ROUTINE_STATUS, _WORKFLOW_LIST,
)
