"""Persisted binding through the common launch seam and a real Pi request."""

from dataclasses import replace
import json
import pytest

from hyprial.kernel import HarnessLaunchSpec
from hyprial.daemon.impl.application.model_binding.admission import pin
from tests.daemon.harnesses.pi.connections.support import options, running, reservation
from tests.daemon.harnesses.pi.connections.test_requests import upstream, prompt
from .test_connection import configured as configured, compose
from .test_destroy import app as app
from tests.identity.agents.home.roots.lifecycle.support import (
    _isolated_environment as _isolated_environment,
)


def test_agent_start_uses_saved_binding_without_legacy_default(configured, monkeypatch):
    from hyprial.daemon.impl.request_catalog import METHOD_REGISTRY

    app, _, _, expected, _ = configured
    calls = []
    original = METHOD_REGISTRY.invoke

    def invoke(owner, method, params, *args, **kwargs):
        if method == "agent.create":
            calls.append((method, params))
            return {}
        return original(owner, method, params, *args, **kwargs)

    def start(method, params):
        calls.append((method, params))
        return {"readiness": "ready"}

    monkeypatch.setattr("shutil.which", lambda _: "/synthetic/pi")
    monkeypatch.setattr(
        type(METHOD_REGISTRY), "invoke", lambda _self, *a, **kw: invoke(*a, **kw)
    )
    monkeypatch.setattr(app, "_ipc_lifecycle_start", start)
    assert app._ipc_agent_start({"name": "worker"})["running"] is True
    assert "provider" not in calls[0][1] and "model" not in calls[0][1]
    assert calls[1][1]["model"] == "hy4-flash"
    assert calls[1][1]["modelProvider"] == expected.model_provider
    assert calls[1][1]["modelBinding"] == expected.model_binding


def test_launcher_passes_resolved_binding_to_pi_process(configured, monkeypatch):
    from hyprial.daemon.impl.harnesses.runtime.launcher import HarnessLauncher

    launch = compose(configured)
    captured = {}

    def process(spec, **kwargs):
        captured.update(kwargs)
        return captured

    monkeypatch.setattr(
        "hyprial.daemon.impl.harnesses.runtime.launcher.PiRpcProcess", process
    )
    launcher = HarnessLauncher(
        worker_channel_factory=lambda _: configured[2],
        child_environment_factory=lambda *_: launch,
    )
    launcher._make_pi_process(configured[3])
    assert captured["model_binding"] is launch.model_binding
    assert captured["env"] is None


def test_bound_launch_without_composition_cannot_use_ambient_fallback(configured, monkeypatch):
    from hyprial.daemon.impl.harnesses.runtime.launcher import HarnessLauncher
    from hyprial.daemon.impl.harnesses.pi.bindings.errors import PiModelBindingError

    processes = []
    monkeypatch.setattr("hyprial.daemon.impl.harnesses.runtime.launcher.PiRpcProcess",
                        lambda *args, **kwargs: processes.append(kwargs))
    with pytest.raises(PiModelBindingError) as error:
        HarnessLauncher()._make_pi_process(configured[3])
    assert error.value.code == "MODEL_AUTH_UNAVAILABLE"
    assert processes == []


def test_saved_connection_real_pi_emits_exact_endpoint_key_and_model(
    configured, tmp_path
):
    app, resolver, channel, _, cell = configured
    agent = app.agents.require("worker")
    with upstream() as (endpoint, requests):
        resolver.configure_model_connection(
            "gateway",
            field_name="key",
            expected_revision=1,
            connection={
                "version": 1,
                "endpoint": endpoint,
                "protocol": "openai-completions",
                "profile": "openai-chat-completions/v1",
            },
        )
        store = app.desired_state.model_binding_store
        old = store.status(agent.agent_id, agent.uri).binding
        candidate = replace(old.configuration, config_revision=2).to_json()
        operation = store.prepare(
            agent.agent_id, agent.uri, "endpoint-update", 1, candidate
        )
        store.finalize(
            agent.agent_id, agent.uri, "endpoint-update", operation.request_digest, 1
        )
        spec = pin(app, HarnessLaunchSpec("pi", "worker", True))
        selected = compose((app, resolver, channel, spec, cell)).model_binding
        payload, env = options(tmp_path / "pi-runtime", selected=selected)
        with running(payload, env) as (process, rows):
            events = prompt(process, rows)
            assert "fixture reply" in json.dumps(events)
        assert len(requests) == 1
        path, authorization, body = requests[0]
        assert path == "/v1/chat/completions"
        assert authorization == "Bearer synthetic-bound-key"
        assert body["model"] == "hy4-flash"
        assert not reservation(payload).exists()
