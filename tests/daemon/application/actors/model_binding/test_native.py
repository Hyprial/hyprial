"""Native bindings name one owned target and reject competing credential sources."""

from dataclasses import replace
import json
import os
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
from hyprial.identity import resolve_agent_runtime_context
from hyprial.daemon.impl.harnesses.worker_channel import build_worker_channel
from hyprial.daemon.impl.harnesses.runtime.launcher import HarnessLauncher
from hyprial.daemon.impl.harnesses.pi.rpc_client import PiRpcClient
from tests.daemon.harnesses.pi.connections.support import reservation, sdk_root
from tests.support.identity.tool_profile import TEST_TOOL_PROFILE


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


@pytest.fixture
def native_runtime(native, actor_host):
    app, home, request = native
    request["configuration"]["model"] = "gpt-4.1-mini"
    METHOD_REGISTRY.invoke(app, "agent.model.configure", request, trusted_message_origin="ipc")
    cwd = home / "workspace"
    context = resolve_agent_runtime_context(
        registry=app._agent_registry, agent_name="worker", harness="pi", cwd=str(cwd),
        tool_profile=TEST_TOOL_PROFILE,
    )
    assert context is not None
    channel = build_worker_channel(
        actor=app.agents.require("worker").uri, session_ref="native-runtime-test",
        hyprial_home=app.hyprial_home, state_dir=app.state_dir, runtime_context=context,
    )
    def compose(spec, channel):
        return compose_daemon_worker_launch(
            registry=app._agent_registry, hyprial_home=app.hyprial_home, spec=spec,
            channel=channel, environ={"PATH": os.environ["PATH"], "OPENAI_API_KEY": "ambient-decoy"}, app=app,
        )
    spec = pin(app, HarnessLaunchSpec("pi", "worker", True, cwd=str(cwd),
        command=(str(sdk_root() / "dist" / "cli.js"),)))
    launcher = HarnessLauncher(worker_channel_factory=lambda _: channel,
        child_environment_factory=compose, actor_host=actor_host)
    return app, spec, channel, compose, launcher


def test_native_rpc_client_does_not_require_or_project_connection_key(native_runtime):
    _, spec, channel, compose, _ = native_runtime
    complete = compose(spec, channel)
    client = PiRpcClient(replace(spec, args=("--approve",)), "native-client-test",
        command=spec.command, worker_channel=channel, complete_launch=complete,
        model_binding=complete.model_binding)
    assert client._provider_environment == {}
    assert "HYPRIAL_PI_CONNECTION_KEY" not in client._env
    assert "OPENAI_API_KEY" not in client._env
    assert "ambient-decoy" not in str(client.command)


def test_native_daemon_launcher_starts_and_stops_real_sdk(native_runtime, actor_host):
    _, spec, channel, compose, _ = native_runtime
    complete = compose(spec, channel)
    launcher = HarnessLauncher(worker_channel_factory=lambda _: channel,
        child_environment_factory=lambda *_: complete, actor_host=actor_host)
    process = launcher.start(replace(spec, args=("--approve",)))
    try:
        client = process._last_client
        payload = json.loads(client.command[3])
        child = client._process
        assert process._provider_environment == client._provider_environment == {}
        assert payload["modelBinding"]["authKind"] == "native"
        assert payload["model"] == "gpt-4.1-mini"
        assert payload["modelProvider"] == "openai"
        assert "HYPRIAL_PI_CONNECTION_KEY" not in client._env
        assert "OPENAI_API_KEY" not in client._env
        assert reservation(payload).is_dir()
        receipt = json.loads(next(Path(payload["sessionRoot"]).glob(".hyprial-loader-receipts/*.json")).read_text())
        assert receipt["modelBinding"] == payload["modelBinding"]
    finally:
        process.stop()
    assert child.returncode is not None
    assert not reservation(payload).exists()


def test_native_agent_start_approve_reaches_real_sdk(native_runtime, monkeypatch):
    app, spec, _, _, launcher = native_runtime
    original = METHOD_REGISTRY.invoke
    processes = []
    def invoke(owner, method, params, *args, **kwargs):
        if method == "agent.create":
            assert params["harness"] == "pi"
            return {}
        return original(owner, method, params, *args, **kwargs)
    def start(method, params):
        assert method == "lifecycle.start" and params["args"] == ["--approve"]
        selected = replace(spec, args=tuple(params["args"]), cwd=params["cwd"],
            model_binding=params["modelBinding"], model=params["model"], model_provider=params["modelProvider"])
        processes.append(launcher.start(pin(app, selected)))
        return {"readiness": "ready"}
    monkeypatch.setattr(type(METHOD_REGISTRY), "invoke", lambda _self, *a, **kw: invoke(*a, **kw))
    monkeypatch.setattr(app, "_ipc_lifecycle_start", start)
    try:
        result = METHOD_REGISTRY.invoke(app, "agent.start",
            {"name": "worker", "approve": True, "cwd": spec.cwd}, trusted_message_origin="ipc")
        assert result["running"] is True
        assert len(processes) == 1 and processes[0].running
    finally:
        for process in processes:
            process.stop()


@pytest.mark.parametrize("args", [("--model", "other"), ("--provider=openai",), ("--unknown",)])
def test_native_runtime_overrides_still_refuse(native_runtime, args):
    app, spec, _, _, _ = native_runtime
    with pytest.raises(DaemonRequestError) as error:
        pin(app, replace(spec, args=("--approve", *args)))
    assert error.value.code == "MODEL_COMBINATION_UNSUPPORTED"


def test_native_launcher_without_approval_still_refuses(native_runtime):
    from hyprial.daemon.impl.harnesses.pi.loader import PiProjectTrustError

    _, spec, _, _, launcher = native_runtime
    with pytest.raises(PiProjectTrustError):
        launcher.start(spec)


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
    with pytest.raises(DaemonRequestError) as unsupported:
        pin(app, replace(spec, args=("--approve",)))
    assert unsupported.value.code == "MODEL_COMBINATION_UNSUPPORTED"
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
