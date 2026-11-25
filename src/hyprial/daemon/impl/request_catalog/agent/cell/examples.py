"""Canonical examples for ``agent.cell.get`` and ``agent.cell.update`` (I6-3)."""

from __future__ import annotations

from hyprial.kernel import (
    MethodExample,
    MethodExampleError,
    MethodExampleSet,
    ipc_errors,
)

from .specs import AGENT_CELL_GET, AGENT_CELL_UPDATE

_AGENT_NAME = "worker"
_CELL_ID = "c.example000000000000000001"

_CELL_SNIPPET = {
    "cellId": _CELL_ID,
    "revision": 7,
    "members": [{"ref": _AGENT_NAME, "role": "harness"}],
    "params": {
        "cell.entry": _AGENT_NAME,
        "cell.members": {_AGENT_NAME: {"kind": "claude"}},
    },
}

CELL_EXAMPLES = (
    MethodExampleSet(
        method=AGENT_CELL_GET,
        empty_not_applicable="An existing agent always has a cell snippet; missing agents are refused.",
        cases=(
            MethodExample(
                name="active-cell",
                kind="success",
                description="Read an active agent cell document fragment and actor snapshot.",
                params={"name": _AGENT_NAME},
                result={"ok": True, "cell": _CELL_SNIPPET, "snapshot": None},
            ),
            MethodExample(
                name="missing-name",
                kind="invalid-params",
                description="Agent name is required.",
                params={},
                error=MethodExampleError(ipc_errors.INVALID_ARGUMENT, "name is required"),
            ),
            MethodExample(
                name="unknown-agent",
                kind="domain-error",
                description="The requested agent is not registered on this machine.",
                params={"name": "missing"},
                error=MethodExampleError(ipc_errors.AGENT_NOT_FOUND, "agent not found"),
            ),
        ),
    ),
    MethodExampleSet(
        method=AGENT_CELL_UPDATE,
        empty_not_applicable="A cell update either commits a revision or fails validation/CAS.",
        cases=(
            MethodExample(
                name="cas-updated",
                kind="success",
                description="Update cell members and params with matching expectedRevision.",
                params={
                    "name": _AGENT_NAME,
                    "expectedRevision": 7,
                    "members": [{"ref": _AGENT_NAME, "role": "harness"}],
                    "params": {
                        "cell.entry": _AGENT_NAME,
                        "cell.members": {_AGENT_NAME: {"kind": "codex"}},
                    },
                },
                result={
                    "ok": True,
                    "cellId": _CELL_ID,
                    "revision": 8,
                    "appliedRevision": 7,
                    "state": "active",
                },
            ),
            MethodExample(
                name="missing-expected-revision",
                kind="invalid-params",
                description="expectedRevision is required for every CAS cell update.",
                params={"name": _AGENT_NAME, "members": [], "params": {}},
                error=MethodExampleError(
                    ipc_errors.INVALID_ARGUMENT, "expectedRevision is required"
                ),
            ),
            MethodExample(
                name="revision-conflict",
                kind="domain-error",
                description="Stale expectedRevision is refused with expected and current revisions.",
                params={
                    "name": _AGENT_NAME,
                    "expectedRevision": 6,
                    "members": [{"ref": _AGENT_NAME, "role": "harness"}],
                },
                error=MethodExampleError(
                    ipc_errors.INVALID_ARGUMENT,
                    "cell revision conflict: expected 6, current 7",
                    data={"expected": 6, "current": 7},
                ),
            ),
        ),
    ),
)

__all__ = ["CELL_EXAMPLES"]
