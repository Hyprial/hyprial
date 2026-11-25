"""S8 refusals: each one is a declared code and changes nothing.

- attach of a name held by a daemon-managed agent: ``AGENT_EXISTS`` (a
  managed agent is never converted to external custody);
- attach of an invalid name, a name an adapter holds, or with an unknown
  parameter: ``INVALID_ARGUMENT``;
- detach of an unknown name: ``AGENT_NOT_FOUND``;
- detach of a daemon-managed agent: ``INVALID_ARGUMENT`` (detach is not
  stop or destroy).
"""

from __future__ import annotations

from typing import Any

import pytest

from hyprial.kernel import ipc_errors
from tests.daemon.attach.attach_support import refused
from tests.support.golden.app import ADAPTER, golden_daemon as golden_daemon


def _snapshot(app: Any) -> list[tuple[str, str, dict[str, Any]]]:
    return sorted(
        (agent.actor, agent.entity_token, dict(agent.capabilities))
        for agent in app.agents.list()
    )


def test_attach_never_converts_a_managed_agent(golden_daemon) -> None:
    app = golden_daemon().app
    before = _snapshot(app)
    error = refused("agent.attach", app, {"name": "alpha"})
    assert error.code == ipc_errors.AGENT_EXISTS
    assert error.data == {"actor": app.agents.get("alpha").uri, "agent": "alpha"}
    assert _snapshot(app) == before


@pytest.mark.parametrize(
    "params",
    [
        {"name": ""},
        {"name": "bad name"},
        {"name": "agent:op:node-1:ext"},
        {"name": ADAPTER},
        {"name": "ext", "harness": "claude"},
        {},
    ],
)
def test_attach_refuses_invalid_arguments(golden_daemon, params: dict[str, Any]) -> None:
    app = golden_daemon().app
    before = _snapshot(app)
    assert refused("agent.attach", app, params).code == ipc_errors.INVALID_ARGUMENT
    assert _snapshot(app) == before


def test_detach_refusals(golden_daemon) -> None:
    app = golden_daemon().app
    before = _snapshot(app)
    assert refused("agent.detach", app, {"name": "nobody"}).code == ipc_errors.AGENT_NOT_FOUND
    managed = refused("agent.detach", app, {"name": "alpha"})
    assert managed.code == ipc_errors.INVALID_ARGUMENT
    assert "custody=external" in str(managed)
    assert refused("agent.detach", app, {"name": "x", "force": True}).code == (
        ipc_errors.INVALID_ARGUMENT
    )
    assert _snapshot(app) == before
