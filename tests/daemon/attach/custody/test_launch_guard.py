"""S8 review: custody=external is enforced, not only documented.

- The daemon never launches a harness for an external-custody agent:
  ``agent.create`` bound to a harness (what ``hyprial agent start`` sends
  first), ``agent.start.preflight``, ``lifecycle.start`` (the launch,
  restore and routine-wake funnel) and the release's background
  ``agent.start`` (create-on-start through ``agent.create``, merge onto the
  release-synced cell-model line) refuse it with ``INVALID_ARGUMENT`` and
  change nothing.
- ``custody`` is a reserved capability: ``agent.create`` cannot forge an
  external-custody record, so S8 only ever treats agents created by
  ``agent.attach`` as external.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from hyprial.daemon.impl.application.actors.lifecycle_ops import _LifecycleOpsMixin
from hyprial.daemon.impl.desired_state import DesiredState
from hyprial.kernel import DaemonRequestError, ipc_errors
from tests.daemon.attach.attach_support import attach
from tests.support.golden.app import golden_daemon as golden_daemon


def _refused(app: Any, method: str, params: dict[str, Any]) -> DaemonRequestError:
    with pytest.raises(DaemonRequestError) as raised:
        app.handle(method, params)
    return raised.value


def _state(app: Any) -> tuple[Any, ...]:
    agents = sorted((a.actor, a.entity_token, dict(a.capabilities)) for a in app.agents.list())
    return tuple(agents), tuple(app.desired_state.load().harnesses)


@pytest.mark.parametrize(
    ("method", "extra"),
    [
        ("agent.create", {"existing": "reuse", "harness": "pi"}),
        ("agent.create", {"existing": "reuse", "harness": "claude", "runtime": "interactive"}),
        ("agent.start.preflight", {"harness": "pi"}),
        ("agent.start", {"harness": "jev"}),
    ],
)
def test_the_daemon_never_launches_an_attached_harness(
    golden_daemon, method: str, extra: dict[str, Any]
) -> None:
    app = golden_daemon().app
    attach(app, "ext")
    before = _state(app)
    error = _refused(app, method, {"name": "ext", **extra})
    assert error.code == ipc_errors.INVALID_ARGUMENT
    assert "custody=external" in str(error)
    assert _state(app) == before


def test_reusing_an_attached_identity_without_a_harness_is_still_allowed(golden_daemon) -> None:
    app = golden_daemon().app
    attach(app, "ext")
    reused = app.handle("agent.create", {"name": "ext", "existing": "reuse"})
    assert reused["ok"] is True and reused["created"] is False


def test_lifecycle_start_refuses_an_attached_agent_before_any_launch(tmp_path) -> None:
    attached = SimpleNamespace(
        actor="ext", uri="agent:op:node-1:ext",
        capabilities={"custody": "external", "attached": True},
    )
    app = SimpleNamespace(
        hyprial_home=tmp_path,
        agents=SimpleNamespace(native_actor=lambda name: name, get=lambda _name: attached),
        desired_state=SimpleNamespace(load=DesiredState),
    )
    params = {"provider": "pi", "name": "ext", "headless": True}
    with pytest.raises(DaemonRequestError) as raised:
        _LifecycleOpsMixin._ipc_lifecycle_start(app, "lifecycle.start", params)
    assert raised.value.code == ipc_errors.INVALID_ARGUMENT
    assert "custody=external" in str(raised.value)


@pytest.mark.parametrize(
    "capabilities",
    [{"custody": "external"}, {"custody": "external", "attached": True}, {"custody": "daemon"}],
)
def test_agent_create_cannot_forge_custody(golden_daemon, capabilities: dict[str, Any]) -> None:
    app = golden_daemon().app
    before = _state(app)
    error = _refused(app, "agent.create", {"name": "forged", "capabilities": capabilities})
    assert error.code == ipc_errors.INVALID_ARGUMENT
    assert "custody" in str(error)
    assert app.agents.get("forged") is None and _state(app) == before
