"""The shared launch paths refuse incomplete configuration before credentials."""

from types import SimpleNamespace

import pytest

from hyprial.kernel import DaemonRequestError, HarnessLaunchSpec
from hyprial.daemon.impl.application.netendpoints.endpoints import (
    compose_daemon_worker_launch,
)
from .test_destroy import app as app, prepare
from tests.identity.agents.home.roots.lifecycle.support import (
    _isolated_environment as _isolated_environment,
)


@pytest.mark.parametrize("state", ["cleanup", "prepared"])
@pytest.mark.parametrize("method", ["agent.start", "lifecycle.start", "composition"])
def test_incomplete_binding_refuses_before_launch_or_credential_resolution(
    app, monkeypatch, state, method
):
    import hyprial.identity as identity

    agent = app.agents.require("worker")
    if state == "cleanup":
        app.desired_state.model_binding_store.mark_cleanup(
            agent.agent_id, agent.entity_token, "destroy"
        )
    else:
        prepare(app, agent)
    calls = []

    def unexpected(*args, **kwargs):
        calls.append("credential-resolution")
        raise AssertionError("must refuse before credential resolution")

    monkeypatch.setattr(identity, "compose_worker_child_launch", unexpected)
    with pytest.raises(DaemonRequestError) as error:
        if method == "composition":
            compose_daemon_worker_launch(
                registry=app._agent_registry,
                hyprial_home=app.hyprial_home,
                spec=HarnessLaunchSpec("pi", "worker", True),
                channel=SimpleNamespace(),
                environ={},
                app=app,
            )
        elif method == "agent.start":
            app._ipc_agent_start({"name": "worker", "harness": "pi"})
        else:
            app._ipc_lifecycle_start(
                "lifecycle.start",
                {"name": "worker", "provider": "pi", "headless": True},
            )
    assert error.value.code == "MODEL_CONFIGURATION_INCOMPLETE"
    assert error.value.data["reason"] == (
        "destroy-cleanup-pending" if state == "cleanup" else "operation-in-progress"
    )
    assert calls == []


def test_pending_binding_refusal_survives_real_ipc(app):
    from tests.daemon.ipc.contracts.agent.inventory.enumeration import _wire_af_unix

    prepare(app, app.agents.require("worker"))
    response = _wire_af_unix(app, "agent.start", {"name": "worker", "harness": "pi"})
    assert "result" not in response
    assert response["error"]["code"] == "MODEL_CONFIGURATION_INCOMPLETE"
    assert response["error"]["data"] == {"phase": "bind", "reason": "operation-in-progress"}
