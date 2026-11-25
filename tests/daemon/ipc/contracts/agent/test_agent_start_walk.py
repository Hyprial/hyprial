"""implB2: the agent.start walk — registration, behavior and evidence.

The daemon's background start (create-on-start through the registered
``agent.create`` authority, the running refusal, the lifecycle funnel, the
accepted/running result) and the reviewer-probe harness.  The enumeration of
record lives in the sibling ``inventory/`` package; this module walks the
start side: a new code literal on the start boundary must be declared on
``agent.start`` there, and the behavior here must never claim ``running``
without readiness evidence.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import tempfile
from hyprial.daemon.impl.request_catalog import METHOD_REGISTRY
from hyprial.daemon.impl.request_catalog.agent import lifecycle as lifecycle_catalog
from hyprial.kernel import HarnessLaunchSpec, ipc_errors, PortAdmission


class _Unused:
    pass


class _ProbeHarnesses:
    def status(self):
        return ()

    def session_refs(self):
        # 0.5.5.3.4 persists a headless Claude worker's native session ref
        # after start (merged into 0.5.5.4, per Allen 2026-11-07).
        return {}




def _application(root: Path):
    from hyprial.daemon.impl.application import DaemonApplication
    from hyprial.shell.impl.daemon_wiring import daemon_dependencies

    app = DaemonApplication(
        state_dir=root,
        socket_path=root / "d.sock",
        owner="inventory-owner",
        node_id="inventory-node",
        **daemon_dependencies(),
    )
    app._inbox = _Unused()  # type: ignore[assignment]
    app._harnesses = _ProbeHarnesses()  # type: ignore[assignment]
    app._presence = _Unused()  # type: ignore[assignment]
    return app




_HARNESS_BINARIES = frozenset({"claude", "pi", "codex", "agy"})


@pytest.fixture
def app(monkeypatch):
    import shutil as shutil_module

    # agent.start pins the harness binary from PATH (lifecycle_ops); the walk
    # must not depend on whether the host happens to have claude/pi/codex
    # installed (CI runners do not).  Tests that exercise the missing-binary
    # refusal patch ``which`` again themselves.
    real_which = shutil_module.which
    monkeypatch.setattr(
        shutil_module,
        "which",
        lambda name, *args, **kwargs: (
            f"/fake/bin/{name}"
            if name in _HARNESS_BINARIES
            else real_which(name, *args, **kwargs)
        ),
    )
    with tempfile.TemporaryDirectory(prefix="hyprial-start-walk-") as directory:
        application = _application(Path(directory))
        try:
            yield application
        finally:
            application._close()
            application._close_home_lease()


def _wire_af_unix(app, method: str, params: dict) -> dict:
    import json as json_module
    import socket
    import threading

    server, client = socket.socketpair()
    client.settimeout(10)
    thread = threading.Thread(target=app._serve_client, args=(server,))
    thread.start()
    try:
        client.sendall(
            json_module.dumps(
                dict(version=1, id="inventory", method=method, params=params)
            ).encode()
            + b"\n"
        )
        received = bytearray()
        while b"\n" not in received:
            data = client.recv(65536)
            if not data:
                break
            received.extend(data)
        return json_module.loads(bytes(received))
    finally:
        client.close()
        thread.join(10)
        assert not thread.is_alive()


def _desired_harness(app) -> None:
    from hyprial.kernel import HarnessLaunchSpec

    app.agents.create("alpha")
    app.desired_state.upsert_harness(
        HarnessLaunchSpec("claude", "alpha", headless=True, cwd=str(app.state_dir))
    )


class _SettledBase:
    crashed = False

    def __init__(self, _error_code: str | None = None) -> None:
        pass

    def submit(self, _operation):
        return PortAdmission.ACCEPTED

    def wait(self, operation_id: str, _timeout: float):
        from hyprial.daemon.impl.lifecycle_manager.vocabulary import (
            LifecycleResult,
            LifecycleState,
        )

        return LifecycleResult(
            operation_id=operation_id,
            state=LifecycleState.COMPLETED,
            completed_effects=(),
            compensated_effects=(),
            error=None,
            error_code=None,
        )

    def drain(self, _timeout: float) -> bool:
        return True


def test_agent_start_is_registered_with_its_real_handler(app) -> None:
    from hyprial.daemon import daemon_catalog

    assert lifecycle_catalog.AGENT_START in METHOD_REGISTRY
    binding = METHOD_REGISTRY.get("agent.start")
    assert binding is not None and binding.handler is not None
    names = {entry["name"] for entry in daemon_catalog()["methods"]}
    assert "agent.start" in names
    assert lifecycle_catalog.AGENT_START_SPEC is not None
    assert ipc_errors.DAEMON_NOT_READY not in {
        e.code for e in lifecycle_catalog.AGENT_START_SPEC.errors
    }



def test_agent_start_launches_a_registered_agent_through_the_funnel(app) -> None:
    class _SettlingManager(_SettledBase):
        def wait(self, operation_id: str, _timeout: float):
            from hyprial.daemon.impl.lifecycle_manager.vocabulary import (
                LifecycleResult,
                LifecycleState,
            )

            return LifecycleResult(
                operation_id=operation_id,
                state=LifecycleState.COMPLETED,
                completed_effects=(),
                compensated_effects=(),
                error=None,
                error_code=None,
            )

    class _ReadyHarnesses:
        def status(self):
            return ()

        def session_refs(self):
            return {}

        def wait_ready(self, harness, name, timeout):
            return True

    app._harnesses = _ReadyHarnesses()
    app.agents.create("alpha")
    app.desired_state.upsert_harness(
        HarnessLaunchSpec("claude", "alpha", headless=True, cwd=str(app.state_dir))
    )
    app._lifecycle_manager = _SettlingManager("DAEMON_START_FAILED")
    routed = METHOD_REGISTRY.invoke(app, "agent.start", {"name": "alpha"})
    assert routed == {"ok": True, "accepted": True, "running": True}


def test_agent_start_reports_running_false_without_readiness_evidence(app) -> None:
    from hyprial.daemon.impl.lifecycle_manager.vocabulary import (
        LifecycleResult,
        LifecycleState,
    )
    from hyprial.kernel import HarnessLaunchSpec

    class _UnconfirmedManager:
        crashed = False

        def submit(self, _operation):
            return PortAdmission.ACCEPTED

        def wait(self, operation_id: str, _timeout: float):
            return LifecycleResult(
                operation_id=operation_id,
                state=LifecycleState.COMPLETED,
                completed_effects=(),
                compensated_effects=(),
                error=None,
                error_code=None,
            )

        def drain(self, _timeout: float) -> bool:
            return True

    app.agents.create("alpha")
    app.desired_state.upsert_harness(
        HarnessLaunchSpec("claude", "alpha", headless=True, cwd=str(app.state_dir))
    )
    app._lifecycle_manager = _UnconfirmedManager()
    routed = METHOD_REGISTRY.invoke(app, "agent.start", {"name": "alpha"})
    assert routed == {"ok": True, "accepted": True, "running": False}


def test_agent_start_refuses_a_second_start_before_any_mutation(app) -> None:

    submissions: list[str] = []

    class _CountingManager:
        crashed = False

        def submit(self, operation):
            submissions.append(operation.operation_id)
            return PortAdmission.ACCEPTED

        def wait(self, _operation_id: str, _timeout: float):
            raise AssertionError("never waited in this test")

        def drain(self, _timeout: float) -> bool:
            return True

    app.agents.create("alpha")
    from hyprial.identity import AgentBinding, RUNTIME_HEADLESS

    live = AgentBinding(
        actor=app.agents.get("alpha").actor,
        harness="claude",
        runtime=RUNTIME_HEADLESS,
        session_id=None,
        bound_at_ms=1760000000000,
        revision=1,
    )
    app._agent_liveness.live_binding = lambda actor: live  # type: ignore[method-assign]
    app._lifecycle_manager = _CountingManager()
    with pytest.raises(Exception) as refused:
        METHOD_REGISTRY.invoke(app, "agent.start", {"name": "alpha", "harness": "claude"})
    assert refused.value.code == "AGENT_ALREADY_RUNNING"
    assert set(refused.value.data) == {
        "actor",
        "existing",
        "requestedHarness",
        "requestedRuntime",
    }
    assert submissions == []


def test_agent_start_needs_a_harness_for_an_unknown_agent(app) -> None:
    with pytest.raises(Exception) as refused:
        METHOD_REGISTRY.invoke(app, "agent.start", {"name": "ghost"})
    assert refused.value.code == ipc_errors.INVALID_ARGUMENT


def test_agent_start_refuses_an_adapter_harness(app) -> None:
    with pytest.raises(Exception) as refused:
        METHOD_REGISTRY.invoke(app, "agent.start", {"name": "x", "harness": "lark"})
    assert refused.value.code == "USE_ADAPTER_COMMAND"


def test_agent_start_creates_a_missing_agent_through_agent_create(app) -> None:
    class _SettlingManager:
        crashed = False

        def submit(self, _operation):
            return PortAdmission.ACCEPTED

        def wait(self, operation_id: str, _timeout: float):
            from hyprial.daemon.impl.lifecycle_manager.vocabulary import (
                LifecycleResult,
                LifecycleState,
            )

            return LifecycleResult(
                operation_id=operation_id,
                state=LifecycleState.COMPLETED,
                completed_effects=(),
                compensated_effects=(),
                error=None,
                error_code=None,
            )

        def drain(self, _timeout: float) -> bool:
            return True

    app._lifecycle_manager = _SettlingManager()
    routed = METHOD_REGISTRY.invoke(
        app, "agent.start", {"name": "fresh", "harness": "claude"}
    )
    assert routed["accepted"] is True
    assert app.agents.get("fresh") is not None
    assert app.agents.get("fresh").preferred_harness == "claude"


def test_agent_start_over_af_unix_is_accepted_with_evidence(app) -> None:
    from hyprial.kernel import HarnessLaunchSpec

    class _SettlingManager:
        crashed = False

        def submit(self, _operation):
            return PortAdmission.ACCEPTED

        def wait(self, operation_id: str, _timeout: float):
            from hyprial.daemon.impl.lifecycle_manager.vocabulary import (
                LifecycleResult,
                LifecycleState,
            )

            return LifecycleResult(
                operation_id=operation_id,
                state=LifecycleState.COMPLETED,
                completed_effects=(),
                compensated_effects=(),
                error=None,
                error_code=None,
            )

        def drain(self, _timeout: float) -> bool:
            return True

    class _ReadyHarnesses:
        def status(self):
            return ()

        def session_refs(self):
            return {}

        def wait_ready(self, harness, name, timeout):
            return True

    app._harnesses = _ReadyHarnesses()
    app.agents.create("alpha")
    app.desired_state.upsert_harness(
        HarnessLaunchSpec("claude", "alpha", headless=True, cwd=str(app.state_dir))
    )
    app._lifecycle_manager = _SettlingManager()
    response = _wire_af_unix(app, "agent.start", {"name": "alpha"})
    assert response["result"] == {"ok": True, "accepted": True, "running": True}


def test_agent_start_pins_the_harness_binary_into_the_launch(
    app, monkeypatch
) -> None:
    import shutil as shutil_module

    launched: list[dict] = []
    original = app._ipc_lifecycle_start

    def spy(method, params):
        launched.append(dict(params))
        return original(method, params)

    app._ipc_lifecycle_start = spy  # type: ignore[method-assign]
    monkeypatch.setattr(
        shutil_module, "which", lambda name: f"/fake/bin/{name}"
    )
    app._lifecycle_manager = _SettledBase()
    routed = METHOD_REGISTRY.invoke(app, "agent.start", {"name": "w", "harness": "codex"})
    assert routed["accepted"] is True
    assert launched[0]["command"] == ["/fake/bin/codex"]
    assert launched[0]["provider"] == "codex"
    assert launched[0]["headless"] is True


@pytest.mark.parametrize("harness", ["claude", "codex", "pi", "agy"])
@pytest.mark.parametrize("existing", [False, True])
def test_agent_start_refuses_a_missing_binary_before_mutating_the_agent(
    app, monkeypatch, harness, existing
) -> None:
    import shutil as shutil_module

    if existing:
        app.agents.create("w")
    before = app.agents.get("w")
    desired_before = app.desired_state.load()
    monkeypatch.setattr(shutil_module, "which", lambda name: None)
    with pytest.raises(Exception) as refused:
        METHOD_REGISTRY.invoke(app, "agent.start", {"name": "w", "harness": harness})
    assert refused.value.code == "HARNESS_BINARY_MISSING"
    assert "binary not found" in str(refused.value)
    assert refused.value.data is None
    wire = _wire_af_unix(app, "agent.start", {"name": "w", "harness": harness})
    assert wire["error"]["code"] == "HARNESS_BINARY_MISSING"
    assert app.agents.get("w") == before
    assert app.desired_state.load() == desired_before


# --- implB3 (revB2 B2-2): the existing-Agent path updates before the attempt --


def _existing_agent(app, **kwargs) -> None:
    from hyprial.kernel import HarnessLaunchSpec

    app.agents.create("alpha", **kwargs)
    app.desired_state.upsert_harness(
        HarnessLaunchSpec("claude", "alpha", headless=True, cwd=str(app.state_dir))
    )


class _FailingManager:
    crashed = False

    def __init__(self, error_code: str = "DAEMON_START_FAILED") -> None:
        self._error_code = error_code

    def submit(self, _operation):
        from hyprial.kernel import PortAdmission

        return PortAdmission.ACCEPTED

    def wait(self, operation_id: str, _timeout: float):
        from hyprial.daemon.impl.lifecycle_manager.vocabulary import (
            LifecycleResult,
            LifecycleState,
        )

        return LifecycleResult(
            operation_id=operation_id,
            state=LifecycleState.FAILED,
            completed_effects=(),
            compensated_effects=(),
            error=f"terminal failure: {self._error_code}",
            error_code=self._error_code,
        )

    def drain(self, _timeout: float) -> bool:
        return True


def test_failed_start_still_updates_the_requested_model(
    app, monkeypatch
) -> None:
    import shutil as shutil_module

    monkeypatch.setattr(shutil_module, "which", lambda name: f"/fake/bin/{name}")
    _existing_agent(app, provider="deepseek", model="m1")
    app._lifecycle_manager = _FailingManager()
    with pytest.raises(Exception) as failed:
        METHOD_REGISTRY.invoke(
            app, "agent.start", {"name": "alpha", "model": "m2"}
        )
    assert failed.value.code == "DAEMON_START_FAILED"
    agent = app.agents.get("alpha")
    assert agent.model == "m2", (
        "a failed start must keep the old create-before-attempt ordering: "
        "the requested model is stored before the launch attempt"
    )


def test_failed_start_still_updates_the_requested_provider(app, monkeypatch) -> None:
    import shutil as shutil_module

    monkeypatch.setattr(shutil_module, "which", lambda name: f"/fake/bin/{name}")
    _existing_agent(app, provider="deepseek", model="m1")
    app._lifecycle_manager = _FailingManager()
    with pytest.raises(Exception):
        METHOD_REGISTRY.invoke(
            app,
            "agent.start",
            {"name": "alpha", "provider": "openai", "model": "m1"},
        )
    agent = app.agents.get("alpha")
    assert agent.provider == "openai"
    assert agent.model == "m1"


def test_failed_start_still_updates_runtime_capabilities(app, monkeypatch) -> None:
    import shutil as shutil_module

    monkeypatch.setattr(shutil_module, "which", lambda name: f"/fake/bin/{name}")
    _existing_agent(app)
    app._lifecycle_manager = _FailingManager()
    with pytest.raises(Exception):
        METHOD_REGISTRY.invoke(
            app, "agent.start", {"name": "alpha", "harness": "codex"}
        )
    agent = app.agents.get("alpha")
    assert agent.capabilities.get("harness") == "codex", (
        "the runtime-capability update must precede the launch attempt"
    )


def test_start_with_another_harness_keeps_the_registered_default(app, monkeypatch) -> None:
    import shutil as shutil_module

    monkeypatch.setattr(shutil_module, "which", lambda name: f"/fake/bin/{name}")
    _existing_agent(app, preferred_harness="claude")
    app._lifecycle_manager = _FailingManager()
    with pytest.raises(Exception):
        METHOD_REGISTRY.invoke(
            app, "agent.start", {"name": "alpha", "harness": "codex"}
        )
    agent = app.agents.get("alpha")
    assert agent.capabilities.get("harness") == "codex"
    assert agent.preferred_harness == "claude", (
        "agent.start may move this run to another harness but must not "
        "rewrite the registered default"
    )


def test_running_refusal_still_precedes_every_mutation(app) -> None:
    from hyprial.identity import AgentBinding, RUNTIME_HEADLESS

    _existing_agent(app, model="m1")
    live = AgentBinding(
        actor=app.agents.get("alpha").actor,
        harness="claude",
        runtime=RUNTIME_HEADLESS,
        session_id=None,
        bound_at_ms=1760000000000,
        revision=1,
    )
    app._agent_liveness.live_binding = lambda actor: live  # type: ignore[method-assign]
    with pytest.raises(Exception) as refused:
        METHOD_REGISTRY.invoke(
            app, "agent.start", {"name": "alpha", "model": "m2"}
        )
    assert refused.value.code == "AGENT_ALREADY_RUNNING"
    assert app.agents.get("alpha").model == "m1"


# --- implB3 (revB2 B2-3): typed translation at the start boundary -------------


def test_agent_start_surfaces_invalid_name_as_a_typed_daemon_request_error(
    app,
) -> None:
    from hyprial.daemon.impl.request_catalog import METHOD_REGISTRY
    from hyprial.kernel import DaemonRequestError

    with pytest.raises(DaemonRequestError) as routed:
        METHOD_REGISTRY.invoke(app, "agent.start", {"name": "bad name!"})
    assert routed.value.code == "INVALID_AGENT_NAME"

    response = _wire_af_unix(app, "agent.start", {"name": "bad name!"})
    assert response["error"]["code"] == "INVALID_AGENT_NAME"


# --- I2 (UUID addressing merge): id-form agent.start inputs -------------------


def test_agent_start_resolves_every_spelling_of_a_registered_agent(app) -> None:
    """id URI, ``a.`` id, name and live display name reach the one record.

    The desired-state intent is keyed by the actor, so the durable cwd must
    survive every spelling, and no spelling may create a second record.
    """

    agent = app.agents.create("alpha")
    desired_cwd = app.state_dir / "desired-cwd"
    desired_cwd.mkdir()
    app.desired_state.upsert_harness(
        HarnessLaunchSpec("claude", "alpha", headless=True, cwd=str(desired_cwd))
    )
    app._lifecycle_manager = _SettledBase()
    launches: list[dict] = []

    def record_launch(method, launch):
        launches.append(dict(launch))
        return {"ok": True}

    app._ipc_lifecycle_start = record_launch  # type: ignore[method-assign]
    app.agents.rename("alpha", "renamed")
    for spelling in (agent.uri, agent.agent_id, "alpha", "renamed"):
        routed = METHOD_REGISTRY.invoke(app, "agent.start", {"name": spelling})
        assert routed["accepted"] is True, spelling
        assert launches[-1]["name"] == "alpha", spelling
        assert launches[-1]["cwd"] == str(desired_cwd), spelling
    assert [item.actor for item in app.agents.list()] == ["alpha"]


def test_agent_start_refuses_the_retired_user_proxy_kind(app) -> None:
    from hyprial.kernel import DaemonRequestError

    app.agents.create("relay")
    with pytest.raises(DaemonRequestError) as refused:
        METHOD_REGISTRY.invoke(app, "agent.start", {"name": "relay", "harness": "user-proxy"})
    assert refused.value.code == ipc_errors.INVALID_ARGUMENT
    assert "durable launch intent" not in str(refused.value)


def test_a_fenced_start_never_creates_a_record_early(app) -> None:
    from hyprial.kernel import DaemonRequestError

    with pytest.raises(DaemonRequestError) as refused:
        app._ipc_lifecycle_start(
            "lifecycle.start",
            {
                "provider": "claude",
                "name": "ghost",
                "headless": True,
                "args": [],
                "cwd": str(app.state_dir),
            },
            expected_entity_token="stale-token",
        )
    assert refused.value.code == ipc_errors.AGENT_INCARNATION_MISMATCH
    assert app.agents.get("ghost") is None
    # The record can also vanish between the fence read and the early create:
    # a fenced start still never mints one behind its refusal.
    spec = HarnessLaunchSpec("claude", "ghost", headless=True)
    app._create_record_before_start(spec, fenced=True)
    assert app.agents.get("ghost") is None
    app._create_record_before_start(spec)
    assert app.agents.get("ghost") is not None


def test_lifecycle_start_resolves_every_spelling_of_a_renamed_agent(app) -> None:
    """Raw lifecycle.start (interactive/terminal starts) keys on the actor too.

    A display name must not become the harness spec name: the desired-state
    intent, its learned session ref and the workspace are keyed by the actor.
    """

    agent = app.agents.create("alpha")
    app.desired_state.upsert_harness(
        HarnessLaunchSpec(
            "claude",
            "alpha",
            headless=True,
            cwd=str(app.state_dir),
            session_ref="learned-session",
        )
    )
    app.agents.rename("alpha", "renamed")

    class _Captured(Exception):
        pass

    specs: list = []

    def capture(spec):
        specs.append(spec)
        raise _Captured

    app._lifecycle_spec = capture  # type: ignore[method-assign]
    for spelling in ("renamed", agent.agent_id, agent.uri):
        with pytest.raises(_Captured):
            app._ipc_lifecycle_start(
                "lifecycle.start",
                {
                    "provider": "claude",
                    "name": spelling,
                    "headless": True,
                    "args": [],
                    "cwd": str(app.state_dir),
                },
            )
        assert specs[-1].name == "alpha", spelling
        assert specs[-1].session_ref == "learned-session", spelling
    assert [item.actor for item in app.agents.list()] == ["alpha"]
    assert [item.name for item in app.desired_state.load().harnesses] == ["alpha"]


def test_start_cwd_overrides_old_intent_and_survives_a_failed_launch(app) -> None:
    _existing_agent(app)
    project = app.state_dir / 'project'
    project.mkdir()
    launches = []

    def capture(method, params):
        launches.append(dict(params))
        raise RuntimeError('controlled launch failure')

    app._ipc_lifecycle_start = capture
    with pytest.raises(Exception):
        METHOD_REGISTRY.invoke(app, 'agent.start', {'name': 'alpha', 'cwd': str(project)})
    assert launches and launches[0]['cwd'] == str(project)
    assert app.agents.get('alpha').cwd == str(project)
    assert app.agents.get('alpha').actor == 'alpha'


@pytest.mark.parametrize('bad_cwd', ['relative', '~/project', 'missing', 'file'])
def test_start_rejects_bad_explicit_cwd_before_mutation(app, bad_cwd) -> None:
    _existing_agent(app)
    old = app.agents.get('alpha')
    candidate = bad_cwd
    if bad_cwd in {'missing', 'file'}:
        path = app.state_dir / bad_cwd
        if bad_cwd == 'file':
            path.write_text('not a directory')
        candidate = str(path)
    launches = []
    app._ipc_lifecycle_start = lambda *args: launches.append(args)
    with pytest.raises(Exception) as refused:
        METHOD_REGISTRY.invoke(app, 'agent.start', {'name': 'alpha', 'cwd': candidate})
    assert refused.value.code == (
        'HARNESS_CWD_MISSING' if bad_cwd in {'missing', 'file'} else ipc_errors.INVALID_ARGUMENT
    )
    assert app.agents.get('alpha') == old
    assert launches == []


def test_running_agent_refuses_new_cwd_before_record_update(app) -> None:
    from hyprial.identity import AgentBinding, RUNTIME_HEADLESS

    _existing_agent(app)
    old = app.agents.get('alpha')
    project = app.state_dir / 'new-project'
    project.mkdir()
    app._agent_liveness.live_binding = lambda actor: AgentBinding(
        actor=old.actor, harness='claude', runtime=RUNTIME_HEADLESS,
        session_id=None, bound_at_ms=1760000000000, revision=1,
    )
    with pytest.raises(Exception) as refused:
        METHOD_REGISTRY.invoke(app, 'agent.start', {'name': 'alpha', 'cwd': str(project)})
    assert refused.value.code == 'AGENT_ALREADY_RUNNING'
    assert app.agents.get('alpha') == old


@pytest.mark.parametrize("cwd_params", [{}, {"cwd": None}])
def test_start_without_cwd_keeps_prior_launch_directory(app, cwd_params) -> None:
    _existing_agent(app)
    launches = []
    app._ipc_lifecycle_start = lambda method, params: launches.append(dict(params)) or {"readiness": "unconfirmed"}
    result = _wire_af_unix(app, 'agent.start', {'name': 'alpha', **cwd_params})
    assert result['result']['accepted'] is True
    assert launches[0]['cwd'] == str(app.state_dir)
    assert app.agents.get('alpha').cwd == str(app.state_dir)


@pytest.mark.parametrize("stored", [(), ("--approve",)])
def test_pi_start_approval_carries_explicit_cwd_without_editing_stored_args(app, stored):
    app.agents.create("alpha", preferred_harness="pi", harness_args={"pi": list(stored)})
    original_args = app.agents.get("alpha").harness_args
    captured = []

    class Captured(Exception):
        pass

    def capture(spec):
        captured.append(spec)
        raise Captured

    app._lifecycle_spec = capture
    with pytest.raises(Captured):
        app._ipc_agent_start({"name": "alpha", "approve": True, "cwd": str(app.state_dir)})
    assert captured[0].args == ("--approve",)
    assert captured[0].cwd == str(app.state_dir.resolve(strict=True))
    assert app.agents.get("alpha").harness_args == original_args
    restored = HarnessLaunchSpec.from_json(captured[0].to_json(), "approved launch")
    assert restored.args == ("--approve",)
    assert restored.cwd == str(app.state_dir.resolve(strict=True))


@pytest.mark.parametrize("params,code", [
    ({"harness": "pi", "approve": False}, "PI_P2_LAUNCH_UNTRUSTED"),
    ({"harness": "pi", "approve": True}, "INVALID_ARGUMENT"),
    ({"harness": "pi", "approve": True, "cwd": None}, "INVALID_ARGUMENT"),
    ({"harness": "pi", "approve": True, "cwd": " "}, "INVALID_ARGUMENT"),
    ({"harness": "pi", "approve": True, "cwd": "relative"}, "INVALID_ARGUMENT"),
    ({"harness": "claude", "approve": True}, "INVALID_ARGUMENT"),
    ({"harness": "codex", "approve": False}, "INVALID_ARGUMENT"),
    ({"harness": "pi", "approve": "true"}, "INVALID_ARGUMENT"),
])
def test_pi_start_approval_refusal_precedes_create_and_launch(app, params, code):
    app.agents.create("alpha", preferred_harness="pi", harness_args={"pi": ["--approve"]})
    before = app.agents.get("alpha")
    launches = []
    app._ipc_lifecycle_start = lambda *args: launches.append(args)
    with pytest.raises(Exception) as refused:
        METHOD_REGISTRY.invoke(app, "agent.start", {"name": "alpha", **params})
    assert refused.value.code == code
    assert app.agents.get("alpha") == before
    assert launches == []


def test_pi_start_approved_request_is_admitted_by_declared_schema(app):
    project = app.state_dir / "project"
    project.mkdir()
    launches = []
    app._ipc_lifecycle_start = lambda method, params: launches.append(params) or {"readiness": "unconfirmed"}
    result = _wire_af_unix(app, "agent.start", {
        "name": "fresh", "harness": "pi", "approve": True, "cwd": str(project),
    })
    assert result.get("result") == {"ok": True, "accepted": True, "running": False}, result
    assert launches[0]["args"] == ["--approve"]
    assert app.agents.get("fresh").args_for("pi") == ()


def test_pi_start_approval_persists_canonical_cwd_not_a_retargetable_alias(app):
    app.agents.create("alpha", preferred_harness="pi")
    project = app.state_dir / "project"
    other = app.state_dir / "other"
    project.mkdir()
    other.mkdir()
    alias = app.state_dir / "selected"
    alias.symlink_to(project, target_is_directory=True)
    launches = []
    app._ipc_lifecycle_start = lambda method, params: launches.append(dict(params)) or {"readiness": "unconfirmed"}
    METHOD_REGISTRY.invoke(app, "agent.start", {"name": "alpha", "approve": True, "cwd": str(alias)})
    alias.unlink()
    alias.symlink_to(other, target_is_directory=True)
    assert launches[0]["cwd"] == str(project.resolve(strict=True))
    assert Path(launches[0]["cwd"]).resolve(strict=True) != alias.resolve(strict=True)
    assert app.agents.get("alpha").cwd == str(project.resolve(strict=True))
