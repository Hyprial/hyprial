"""Native bindings name one owned target and reject competing credential sources."""

from dataclasses import replace
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from hyprial.identity import install_native_credential_set, remove_native_credentials
from hyprial.kernel import DaemonRequestError, HarnessLaunchSpec
from hyprial.daemon.impl.application.model_binding.admission import pin
from hyprial.daemon.impl.application.netendpoints.endpoints import compose_daemon_worker_launch
from hyprial.daemon.impl.cells.model import Cell, CellBinding, CellMember, CellScope
from hyprial.daemon.impl.request_catalog import METHOD_REGISTRY
from hyprial.daemon.impl.harnesses.pi.bindings.errors import PiModelBindingError
from .test_destroy import app as app
from tests.identity.agents.home.roots.lifecycle.support import _isolated_environment as _isolated_environment
from tests.daemon.cells.runtime.cell_runtime_support import authority_for


@pytest.fixture
def native(app):
    agent = app.agents.require("worker")
    home = Path(app._agent_registry.home_receipt(agent.actor).path)
    install_native_credential_set(home, "pi", [
        ("auth.json", json.dumps({"openai": {"type": "api_key", "key": "synthetic-native"},
                                "anthropic": {"type": "api_key", "key": "unselected"}}).encode()),
        ("models-store.json", b"{}"),
    ])
    cell = Cell(agent.cell_id, "agent", authority_for("user:owner"), CellScope.owner(),
                CellBinding.subject(agent.agent_id), members=(CellMember(agent.uri, "harness"),),
                params={"cell.members": {agent.uri: {"kind": "pi", "model": "gpt-5.4"}}})
    def update(cell_id, **kwargs):
        nonlocal cell
        assert kwargs["expect_revision"] == cell.revision
        cell = replace(cell, params=kwargs["params"], revision=cell.revision + 1)
        return cell
    app._cell_runtime = SimpleNamespace(lookup=lambda _: cell, update=update,
        health=lambda: SimpleNamespace(source_stale=False), identity=SimpleNamespace(authority=cell.authority))
    request = {"name": "worker", "operationId": "select-native", "expectedBindingRevision": 0,
        "expectedCellRevision": cell.revision, "configuration": {"authKind": "native", "harness": "pi",
        "mode": "headless", "model": "gpt-5.4", "nativeAuthRef": "native:v1:pi:api-key:openai"}}
    return app, home, request


def test_native_configure_and_launch_preserve_selected_target_without_key_projection(native):
    app, _, request = native
    result = METHOD_REGISTRY.invoke(app, "agent.model.configure", request, trusted_message_origin="ipc")
    assert result["binding"]["configuration"]["nativeAuthRef"] == request["configuration"]["nativeAuthRef"]
    assert "synthetic-native" not in str(result)
    spec = pin(app, HarnessLaunchSpec("pi", "worker", True))
    assert spec.model_provider == "openai"
    launch = compose_daemon_worker_launch(registry=app._agent_registry, hyprial_home=app.hyprial_home,
        spec=spec, channel=SimpleNamespace(actor=app.agents.require("worker").uri,
        runtime_context=None, identity_environment=lambda: {}), environ={}, app=app)
    assert launch.model_binding.provider_id == "openai"
    assert "synthetic-native" not in repr(launch.model_binding)
    assert "HYPRIAL_PI_CONNECTION_KEY" not in launch.environment.names


def test_logout_blocks_before_prepare(native):
    app, home, request = native
    remove_native_credentials(home, "pi")
    with pytest.raises(DaemonRequestError) as error:
        METHOD_REGISTRY.invoke(app, "agent.model.configure", request, trusted_message_origin="ipc")
    assert error.value.code == "MODEL_AUTH_UNAVAILABLE"
    assert error.value.data == {"phase": "authorize", "reason": "missing"}
    agent = app.agents.require("worker")
    assert not app.desired_state.model_binding_store.status(agent.agent_id, agent.uri).operations


def test_harness_target_mismatch_cannot_prepare(native):
    app, _, request = native
    request["configuration"]["nativeAuthRef"] = "native:v1:codex:api-key:openai"
    with pytest.raises(DaemonRequestError) as error:
        METHOD_REGISTRY.invoke(app, "agent.model.configure", request, trusted_message_origin="ipc")
    assert error.value.code == "MODEL_COMBINATION_UNSUPPORTED"


@pytest.mark.parametrize("harness,target,filename,credential", [
    ("claude", "anthropic", ".credentials.json", {"claudeAiOauth": {"accessToken": "synthetic-claude"}}),
    ("codex", "openai", "auth.json", {"tokens": {"access_token": "synthetic-codex"}}),
])
def test_native_official_target_is_pinned_and_resume_without_snapshot_refuses(
    native, harness, target, filename, credential
):
    app, home, request = native
    install_native_credential_set(home, harness, [(filename, json.dumps(credential).encode())])
    request["configuration"].update(harness=harness, nativeAuthRef=f"native:v1:{harness}:oauth:{target}")
    METHOD_REGISTRY.invoke(app, "agent.model.configure", request, trusted_message_origin="ipc")
    spec = pin(app, HarnessLaunchSpec(harness, "worker", True))
    launch = compose_daemon_worker_launch(registry=app._agent_registry, hyprial_home=app.hyprial_home,
        spec=spec, channel=SimpleNamespace(actor=app.agents.require("worker").uri,
        runtime_context=None, identity_environment=lambda: {}), environ={}, app=app)
    assert spec.model_provider == target
    assert launch.model_binding.target.reference == request["configuration"]["nativeAuthRef"]
    assert "HYPRIAL_PI_CONNECTION_KEY" not in launch.environment.names
    with pytest.raises(DaemonRequestError) as error:
        pin(app, replace(spec, session_ref="old-session"))
    assert error.value.code == "MODEL_RESUME_MISMATCH"
    assert error.value.data == {"phase": "resume", "reason": "binding-changed"}
    remove_native_credentials(home, harness)
    with pytest.raises(PiModelBindingError) as revoked:
        compose_daemon_worker_launch(registry=app._agent_registry, hyprial_home=app.hyprial_home,
            spec=spec, channel=SimpleNamespace(actor=app.agents.require("worker").uri,
            runtime_context=None, identity_environment=lambda: {}), environ={}, app=app)
    assert revoked.value.code == "MODEL_AUTH_UNAVAILABLE"
