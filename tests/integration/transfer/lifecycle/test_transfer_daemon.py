"""Daemon-side transfer methods: plan/quiesce/precheck/receive/resume/complete.

The six handlers implement P0 cold migration (design:
``notes/design-hyprial-transfer.md``).  These tests drive ``DaemonApplication.handle``
with a real ``ManagedHarnessRuntime`` over a stub launcher, the real
``DesiredStateStore`` and ``AgentRegistry`` — the seams the handlers actually
own — and stub only transport-adjacent layers (inbox).
"""

from __future__ import annotations

from tests.support.runtime._tmp import hyprial_test_tmp_root

import json
import shutil
import socket
import tempfile
import threading
from pathlib import Path
from typing import Any

import pytest

from hyprial.kernel.impl.contracts.ports import PortAdmission
from hyprial.daemon.impl.application import DaemonApplication, DaemonRequestError
from hyprial.daemon.impl.desired_state import InteractiveSession
from hyprial.kernel import HarnessLaunchSpec
from hyprial.daemon.impl.lifecycle_manager import (
    LifecycleKind,
    LifecycleOperation,
    LifecycleResult,
    LifecycleState)
from hyprial.daemon.impl.processes.supervisor import ManagedHarnessRuntime
from hyprial.daemon.impl.transfer.execution import container as container
from hyprial.shell.impl.daemon_wiring import daemon_dependencies as _deep_module_daemon_dependencies

OWNER = "alice"
SOURCE = "laptop"
TARGET = "server"
# The launch contract rejects an explicit cwd that does not exist on this
# host (HARNESS_CWD_MISSING), so tests aim WORKSPACE at a real directory via
# the fixtures below; the literal only documents the historical default.
WORKSPACE = "/work"


class _StubProcess:
    """A managed streaming process whose readiness and ref are scripted."""

    def __init__(
        self,
        session_ref: str | None,
        *,
        ready: bool = True,
        last_error: str | None = None,
    ) -> None:
        self.session_ref = session_ref
        self._ready = ready
        self.running = True
        self.stopped = False
        # D3: a harness that refuses on a background thread leaves its real
        # cause here, not in the lifecycle.start response.
        self.last_error = last_error

    @property
    def pid(self) -> int | None:
        return None

    def wait_ready(self, *, timeout: float | None = None) -> bool:
        return self._ready

    def stop(self) -> None:
        self.stopped = True
        self.running = False


class _StubLauncher:
    """Answers ``start`` with a scripted process per (harness, name)."""

    def __init__(self) -> None:
        self.processes: dict[tuple[str, str], _StubProcess] = {}
        self.started: list[tuple[str, str]] = []
        # When set, the next start establishes this ref instead of the spec's
        # (the cold-start fallback firing).
        self.ref_override: str | None = None
        self.not_ready: bool = False
        #: The launch refusal a real background connect thread would publish.
        self.start_error: str | None = None

    def start(self, spec: HarnessLaunchSpec) -> _StubProcess:
        ref = self.ref_override if self.ref_override is not None else spec.session_ref
        process = _StubProcess(
            ref, ready=not self.not_ready, last_error=self.start_error
        )
        self.processes[(spec.harness, spec.name)] = process
        self.started.append((spec.harness, spec.name))
        return process


class _StubInbox:
    def __init__(self, pending: dict[str, int] | None = None) -> None:
        self._pending = pending or {}

    def pending_messages(self, recipient: str) -> tuple[Any, ...]:
        return tuple(object() for _ in range(self._pending.get(recipient, 0)))


class _InlineLifecycleManager:
    """Typed synchronous shell double; durable saga behavior is tested alone."""

    def __init__(self, app: DaemonApplication) -> None:
        self._app = app
        self._results: dict[str, LifecycleResult] = {}
        # Same surface as the real manager's ps-health projection.
        self.crashed = False
        self.is_running = True
        self.last_error: str | None = None

    def submit(self, operation: LifecycleOperation) -> PortAdmission:
        projection = operation.target.harness
        spec = HarnessLaunchSpec.from_json(projection.to_payload(), "provider")
        assert self._app._harnesses is not None
        if operation.kind is LifecycleKind.CREATE:
            self._app._bind_agent(
                operation.target.actor,
                harness=spec.harness,
                runtime="headless",
                session_id=spec.session_ref,
                cwd=spec.cwd,
                args=spec.args,
            )
            self._app.desired_state.upsert_harness(spec)
            self._app._harnesses.start(spec)
            effects = ("agent.create", "agent.bind", "harness.ensure", "route.ensure")
        elif operation.kind in {LifecycleKind.DEACTIVATE, LifecycleKind.REMOVE}:
            self._app._harnesses.remove(spec.harness, spec.name)
            self._app.desired_state.remove_harness(spec.harness, spec.name)
            self._app._release_agent_binding(operation.target.actor)
            effects = ("harness.remove", "agent.release")
            if operation.kind is LifecycleKind.REMOVE:
                agent = self._app.agents.get(spec.name)
                if agent is not None:
                    self._app.agents.destroy(agent.actor)
                effects = (*effects, "agent.destroy")
        else:
            raise AssertionError(f"unexpected unit lifecycle kind: {operation.kind}")
        self._results[operation.operation_id] = LifecycleResult(
            operation.operation_id,
            LifecycleState.COMPLETED,
            effects,
            (),
        )
        return PortAdmission.ACCEPTED

    def wait(self, operation_id: str, _timeout: float) -> LifecycleResult:
        return self._results[operation_id]


def _application(
    tmp_path: Path, node_id: str, launcher: _StubLauncher | None = None
) -> DaemonApplication:
    app = DaemonApplication(
        state_dir=tmp_path,
        socket_path=tmp_path / "daemon.sock",
        node_id=node_id,
        owner=OWNER,
    **_deep_module_daemon_dependencies())
    app._inbox = _StubInbox()  # type: ignore[assignment]
    app._presence = object()  # type: ignore[assignment]
    app._harnesses = ManagedHarnessRuntime(  # type: ignore[assignment]
        launcher or _StubLauncher(), desired_state=app.desired_state
    )
    app._lifecycle_manager = _InlineLifecycleManager(app)  # type: ignore[assignment]
    return app


@pytest.fixture
def make_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Application factory that closes every registry (fd hygiene, card #77)."""

    apps: list[DaemonApplication] = []
    workspace = tmp_path / "work"
    workspace.mkdir()
    monkeypatch.setattr(
        "tests.integration.transfer.lifecycle.test_transfer_daemon.WORKSPACE",
        str(workspace),
    )

    def factory(
        node_id: str, launcher: _StubLauncher | None = None
    ) -> DaemonApplication:
        app = _application(tmp_path, node_id, launcher)
        apps.append(app)
        return app

    yield factory
    for app in apps:
        app.agents.close()


def _spec(
    name: str = "wk", harness: str = "pi", session_ref: str | None = "session-1"
) -> dict[str, Any]:
    spec = HarnessLaunchSpec(
        harness=harness, name=name, headless=True, cwd=WORKSPACE, session_ref=session_ref
    )
    return spec.to_json()


def _seed_worker(
    app: DaemonApplication, name: str = "wk", session_ref: str | None = "session-1"
) -> None:
    app.handle(
        "lifecycle.start",
        {"provider": "pi", "name": name, "headless": True, "cwd": WORKSPACE},
    )
    # Simulate the runtime learning the native ref (#190's actor write-back path).
    launcher = app._harnesses._launcher  # type: ignore[attr-defined]
    launcher.processes[("pi", name)].session_ref = session_ref
    app._harnesses.reconcile_session_refs()


# ---------------------------------------------------------------------------
# plan
# ---------------------------------------------------------------------------


def test_plan_returns_spec_agent_and_identity(make_app) -> None:
    app = make_app(SOURCE)
    _seed_worker(app)

    plan = app.handle("transfer.plan", {"name": "wk"})

    assert plan["ok"] is True
    assert plan["spec"]["sessionRef"] == "session-1"
    assert plan["spec"]["provider"] == "pi"
    assert plan["actor"] == f"agent:{OWNER}:{SOURCE}:wk"
    assert plan["nodeId"] == SOURCE
    assert plan["owner"] == OWNER
    assert plan["agent"]["actor"] == "wk"
    # entityToken is internal incarnation authority: the same strip as the
    # ps snapshot, and the receiving side never consumes it (receive mints a
    # fresh hosted incarnation).
    assert "entityToken" not in plan["agent"]
    assert plan["unreadInbox"] == 0


def test_plan_unknown_worker_fails(make_app) -> None:
    app = make_app(SOURCE)
    with pytest.raises(DaemonRequestError) as failure:
        app.handle("transfer.plan", {"name": "ghost"})
    assert failure.value.code == "TRANSFER_WORKER_NOT_FOUND"


def test_plan_rejects_untransferable_harnesses(make_app) -> None:
    app = make_app(SOURCE)
    app.desired_state.upsert_harness(
        HarnessLaunchSpec(harness="dsh", name="dw", headless=True)
    )
    with pytest.raises(DaemonRequestError) as failure:
        app.handle("transfer.plan", {"name": "dw"})
    assert failure.value.code == "TRANSFER_UNSUPPORTED_HARNESS"


def test_plan_ambiguous_name_requires_a_harness(make_app) -> None:
    app = make_app(SOURCE)
    app.desired_state.upsert_harness(HarnessLaunchSpec(harness="pi", name="wk", headless=True))
    app.desired_state.upsert_harness(
        HarnessLaunchSpec(harness="codex", name="wk", headless=True)
    )
    with pytest.raises(DaemonRequestError) as failure:
        app.handle("transfer.plan", {"name": "wk"})
    assert failure.value.code == "TRANSFER_AMBIGUOUS"
    plan = app.handle("transfer.plan", {"name": "wk", "provider": "codex"})
    assert plan["spec"]["provider"] == "codex"


def test_plan_reports_unread_inbox_rows(make_app) -> None:
    app = make_app(SOURCE)
    _seed_worker(app)
    app._inbox = _StubInbox({f"agent:{OWNER}:{SOURCE}:wk": 2, "wk": 1})  # type: ignore[assignment]
    plan = app.handle("transfer.plan", {"name": "wk"})
    assert plan["unreadInbox"] == 3


def _register_tui(
    app: DaemonApplication,
    name: str = "wk",
    *,
    source: str = "claude-channel",
    runtime: str = "claude_interactive",
) -> None:
    """Put a CLI-hosted TUI session in ``interactive_sessions`` (S1 table)."""

    app.desired_state.register_interactive(
        InteractiveSession(
            actor=name,
            cwd=WORKSPACE,
            command=(runtime.split("_", 1)[0], "--resume", "session-1"),
            source=source,
            session_ref="session-1",
            runtime=runtime,
            tmux_session=f"hyprial-{name}",
        )
    )


def test_plan_refuses_a_name_in_both_tables(make_app) -> None:
    """两表同查: 同名双命中必须显式报歧义, 不得先到先得。"""

    app = make_app(SOURCE)
    _seed_worker(app)  # state.harnesses: pi:wk (headless)
    _register_tui(app, "wk")  # interactive_sessions: claude:wk (tui)

    with pytest.raises(DaemonRequestError) as failure:
        app.handle("transfer.plan", {"name": "wk"})

    assert failure.value.code == "TRANSFER_AMBIGUOUS"
    text = str(failure.value)
    assert "both" in text
    assert "interactive" in text


def test_plan_refuses_codex_tui_with_explicit_text(make_app) -> None:
    """codex TUI 显式拒绝: 无 --tmux detached 形态, 不是静默回落。"""

    app = make_app(SOURCE)
    _register_tui(app, "wk", source="codex-app-server", runtime="codex_app_server")

    with pytest.raises(DaemonRequestError) as failure:
        app.handle("transfer.plan", {"name": "wk"})

    assert failure.value.code == "TRANSFER_UNSUPPORTED_HARNESS"
    assert failure.value.data["entrypoint"] == "tui"
    assert failure.value.data["harness"] == "codex"
    text = str(failure.value)
    assert "codex" in text and "--tmux" in text


def test_plan_refuses_an_unproven_tui_entrypoint(
    make_app, monkeypatch
) -> None:
    """未证明的 (harness, mode, tui) 组合先拒, 不静默降级为 headless。

    The example's headless row is pinned in-process: the arm reads the
    entrypoint axis (a row that proves headless must never widen to tui), not
    which rows production happens to carry.
    """

    from hyprial.daemon.impl.transfer.p2.orchestration import lift as lift_module

    monkeypatch.setattr(
        lift_module,
        "PROVEN_CHAINS",
        (lift_module.ProvenChain("claude", lift_module.P2_CHAIN_MODE, "test-only"),),
    )
    app = make_app(SOURCE)
    _register_tui(app, "wk")

    with pytest.raises(DaemonRequestError) as failure:
        app.handle("transfer.plan", {"name": "wk"})

    assert failure.value.code == "TRANSFER_UNSUPPORTED_HARNESS"
    assert failure.value.data == {
        "harness": "claude",
        "name": "wk",
        "entrypoint": "tui",
        "mode": "agent-home-p2",
    }
    text = str(failure.value)
    assert "entrypoint=tui" in text
    assert "not been proven" in text


# ---------------------------------------------------------------------------
# quiesce
# ---------------------------------------------------------------------------


def test_quiesce_stops_and_snapshots_with_the_freshest_ref(make_app) -> None:
    app = make_app(SOURCE)
    _seed_worker(app)
    # The runtime learned a NEWER ref than desired state's last write.
    launcher = app._harnesses._launcher  # type: ignore[attr-defined]
    launcher.processes[("pi", "wk")].session_ref = "session-2"

    payload = app.handle("transfer.quiesce", {"name": "wk"})

    assert payload["stopped"] is True
    assert payload["spec"]["sessionRef"] == "session-2"
    assert "entityToken" not in payload["agent"]
    # Desired state no longer holds the worker; the agent row survives.
    assert app.desired_state.load().harnesses == ()
    assert app.agents.get("wk") is not None
    # The process really was stopped.
    assert launcher.processes[("pi", "wk")].stopped is True


def test_public_unix_ipc_quiesce_awaits_actor_owned_ref_reconcile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = Path(tempfile.mkdtemp(prefix="hyprial-sr-", dir=hyprial_test_tmp_root()))
    workspace = root / "work"
    workspace.mkdir()
    monkeypatch.setattr(
        "tests.integration.transfer.lifecycle.test_transfer_daemon.WORKSPACE",
        str(workspace),
    )
    app = _application(root, SOURCE)

    class _IdleRuntime:
        def stop(self) -> None:
            pass

    app._runtime = _IdleRuntime()  # type: ignore[assignment]
    _seed_worker(app)
    launcher = app._harnesses._launcher  # type: ignore[attr-defined]
    launcher.processes[("pi", "wk")].session_ref = "session-from-ipc"
    app._maintenance_generation = 1
    app._start_server()
    dispatcher = threading.Thread(target=app._serve, daemon=True)
    dispatcher.start()
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(2.0)
    try:
        client.connect(str(app.socket_path))
        client.sendall(
            json.dumps(
                {
                    "version": 1,
                    "id": "quiesce-session-ref",
                    "method": "transfer.quiesce",
                    "params": {"name": "wk"},
                }
            ).encode()
            + b"\n"
        )
        response = json.loads(client.makefile("rb").readline())
        assert response["id"] == "quiesce-session-ref"
        assert response["result"]["spec"]["sessionRef"] == "session-from-ipc"
        assert response["result"]["stopped"] is True
    finally:
        client.close()
        app.stop_event.set()
        if app._server is not None:
            app._server.close()
        dispatcher.join(2.0)
        app._lifecycle_manager = None
        app._close()
        shutil.rmtree(root, ignore_errors=True)


def test_quiesce_unknown_worker_changes_nothing(make_app) -> None:
    app = make_app(SOURCE)
    with pytest.raises(DaemonRequestError):
        app.handle("transfer.quiesce", {"name": "ghost"})


# ---------------------------------------------------------------------------
# precheck
# ---------------------------------------------------------------------------


def test_precheck_reports_identity_and_no_conflicts(make_app) -> None:
    app = make_app(TARGET)
    result = app.handle("transfer.precheck", {"provider": "pi", "name": "wk"})
    assert result == {
        "ok": True,
        "nodeId": TARGET,
        "owner": OWNER,
        "conflicts": [],
    }


def test_precheck_lists_every_conflict_class(make_app) -> None:
    app = make_app(TARGET)
    _seed_worker(app)  # desired-state harness + agent row for "wk"
    result = app.handle("transfer.precheck", {"provider": "codex", "name": "wk"})
    assert any("managed harness" in item for item in result["conflicts"])
    assert any("registered agent" in item for item in result["conflicts"])


def test_precheck_rejects_untransferable_harness(make_app) -> None:
    app = make_app(TARGET)
    with pytest.raises(DaemonRequestError) as failure:
        app.handle("transfer.precheck", {"provider": "lark", "name": "wk"})
    assert failure.value.code == "TRANSFER_UNSUPPORTED_HARNESS"


# ---------------------------------------------------------------------------
# receive
# ---------------------------------------------------------------------------


def test_receive_adopts_worker_and_verifies_the_ref(make_app) -> None:
    app = make_app(TARGET)

    result = app.handle(
        "transfer.receive",
        {"spec": _spec(), "pins": [], "strictTimeoutSeconds": 5},
    )

    assert result["ok"] is True
    assert result["actor"] == f"agent:{OWNER}:{TARGET}:wk"
    assert result["sessionRef"] == "session-1"
    spec = app.desired_state.load().harnesses[0]
    assert spec.session_ref == "session-1"
    assert app.agents.get("wk") is not None


def test_receive_strict_resume_fails_when_the_ref_does_not_hold(
    make_app,
) -> None:
    launcher = _StubLauncher()
    launcher.ref_override = "fresh-cold-start-id"  # the quiet fallback fires
    app = make_app(TARGET, launcher)

    with pytest.raises(DaemonRequestError) as failure:
        app.handle(
            "transfer.receive",
            {"spec": _spec(), "pins": [], "strictTimeoutSeconds": 5},
        )

    assert failure.value.code == "STRICT_RESUME_FAILED"
    assert "fresh-cold-start-id" in str(failure.value)
    # Every trace is undone: no spec, no agent, no process left running.
    assert app.desired_state.load().harnesses == ()
    assert app.agents.get("wk") is None
    assert launcher.processes[("pi", "wk")].stopped is True


def test_receive_strict_resume_fails_when_never_ready(make_app) -> None:
    launcher = _StubLauncher()
    launcher.not_ready = True
    app = make_app(TARGET, launcher)

    with pytest.raises(DaemonRequestError) as failure:
        app.handle(
            "transfer.receive",
            {"spec": _spec(), "pins": [], "strictTimeoutSeconds": 1},
        )

    assert failure.value.code == "STRICT_RESUME_FAILED"
    assert app.desired_state.load().harnesses == ()
    assert app.agents.get("wk") is None


def test_receive_conflict_leaves_no_trace(make_app) -> None:
    app = make_app(TARGET)
    _seed_worker(app)  # "wk" already lives here

    with pytest.raises(DaemonRequestError) as failure:
        app.handle("transfer.receive", {"spec": _spec(), "pins": []})

    assert failure.value.code == "TRANSFER_CONFLICT"
    # The incumbent worker is untouched.
    assert app.desired_state.load().harnesses[0].name == "wk"


def test_receive_applies_pins_and_undoes_them_on_failure(make_app) -> None:
    app = make_app(TARGET)
    result = app.handle(
        "transfer.receive",
        {"spec": _spec(), "pins": ["lark-1"], "strictTimeoutSeconds": 5},
    )
    assert result["ok"] is True
    assert app.agents.pins() == {"lark-1": f"agent:{OWNER}:{TARGET}:wk"}


def test_receive_pin_conflict_undoes_the_agent_row(make_app) -> None:
    app = make_app(TARGET)
    app.agents.create("other")
    app.agents.pin("lark-1", "other")

    with pytest.raises(DaemonRequestError) as failure:
        app.handle("transfer.receive", {"spec": _spec(), "pins": ["lark-1"]})

    assert failure.value.code == "TRANSFER_PIN_CONFLICT"
    assert app.agents.get("wk") is None
    assert app.desired_state.load().harnesses == ()
    # The pre-existing pin belongs to its original holder, untouched.
    assert app.agents.pins() == {"lark-1": f"agent:{OWNER}:{TARGET}:other"}


def test_receive_without_a_ref_skips_strict_verification(make_app) -> None:
    app = make_app(TARGET)
    result = app.handle(
        "transfer.receive",
        {"spec": _spec(session_ref=None), "pins": [], "strictTimeoutSeconds": 5},
    )
    assert result["ok"] is True
    assert result["sessionRef"] is None


def test_receive_rejects_untransferable_spec(make_app) -> None:
    app = make_app(TARGET)
    with pytest.raises(DaemonRequestError) as failure:
        app.handle("transfer.receive", {"spec": _spec(harness="dsh"), "pins": []})
    assert failure.value.code == "TRANSFER_UNSUPPORTED_HARNESS"


def test_receive_refuses_when_the_sandbox_smoke_fails(make_app, monkeypatch) -> None:
    """Card 356 item 0: a sandbox that cannot enforce must refuse the landing."""

    from hyprial.daemon.impl.application.actors import transfer as transfer_module
    from hyprial.daemon.impl.transfer.execution.sandbox_smoke import SmokeResult

    monkeypatch.setattr(
        transfer_module,
        "run_sandbox_smoke",
        lambda harness, **kwargs: SmokeResult(
            harness, "FAIL", "the sandboxed write to the outside sentinel SUCCEEDED"
        ),
    )
    app = make_app(TARGET)

    with pytest.raises(DaemonRequestError) as failure:
        app.handle(
            "transfer.receive",
            {"spec": _spec(harness="codex"), "pins": [], "strictTimeoutSeconds": 5},
        )

    assert failure.value.code == "SANDBOX_SMOKE_FAILED"
    assert "SUCCEEDED" in str(failure.value)
    # Refused in mapping, before any row, pin or process: nothing to undo.
    assert app.desired_state.load().harnesses == ()
    assert app.agents.get("wk") is None
    assert app.agents.pins() == {}


def test_receive_carries_the_sandbox_smoke_verdict(make_app, monkeypatch) -> None:
    from hyprial.daemon.impl.application.actors import transfer as transfer_module
    from hyprial.daemon.impl.transfer.execution.sandbox_smoke import SmokeResult

    monkeypatch.setattr(
        transfer_module,
        "run_sandbox_smoke",
        lambda harness, **kwargs: SmokeResult(harness, "PASS", "denied the write"),
    )
    app = make_app(TARGET)

    result = app.handle(
        "transfer.receive",
        {"spec": _spec(harness="codex"), "pins": [], "strictTimeoutSeconds": 5},
    )

    assert result["ok"] is True
    assert result["sandboxSmoke"]["status"] == "PASS"
    assert result["sandboxSmoke"]["harness"] == "codex"


def test_receive_reports_not_run_for_a_harness_without_a_native_sandbox(
    make_app,
) -> None:
    """pi is admitted by the matrix; the smoke must say NOT_RUN, not PASS."""

    app = make_app(TARGET)

    result = app.handle(
        "transfer.receive", {"spec": _spec(), "pins": [], "strictTimeoutSeconds": 5}
    )

    assert result["ok"] is True
    assert result["sandboxSmoke"]["status"] == "NOT_RUN"
    assert result["sandboxSmoke"]["detail"]


# ---------------------------------------------------------------------------
# resume (source-side rollback)
# ---------------------------------------------------------------------------


def test_resume_restores_the_quiesced_worker(make_app) -> None:
    app = make_app(SOURCE)
    _seed_worker(app)
    payload = app.handle("transfer.quiesce", {"name": "wk"})

    result = app.handle("transfer.resume", {"spec": payload["spec"]})

    assert result["ok"] is True
    assert result["sessionRef"] == "session-1"
    spec = app.desired_state.load().harnesses[0]
    assert spec.session_ref == "session-1"
    # The agent row was never deleted, so the identity is intact.
    assert app.agents.get("wk") is not None


# ---------------------------------------------------------------------------
# complete
# ---------------------------------------------------------------------------


def test_complete_retires_the_identity_but_keeps_inbox_rows(make_app) -> None:
    app = make_app(SOURCE)
    _seed_worker(app)
    app.agents.pin("lark-1", "wk")
    app._inbox = _StubInbox({f"agent:{OWNER}:{SOURCE}:wk": 2})  # type: ignore[assignment]
    app.handle("transfer.quiesce", {"name": "wk"})

    result = app.handle("transfer.complete", {"name": "wk"})

    assert result["removedAgent"] is True
    assert result["actor"] == f"agent:{OWNER}:{SOURCE}:wk"
    assert result["unpinnedAdapters"] == ["lark-1"]
    # Inbox rows are reported, not drained (P0 keep-inbox semantics).
    assert result["unreadInbox"] == 2
    assert app.agents.get("wk") is None
    assert app.agents.pins() == {}


def test_complete_is_a_noop_without_an_agent_row(make_app) -> None:
    app = make_app(SOURCE)
    result = app.handle("transfer.complete", {"name": "ghost"})
    assert result == {"ok": True, "removedAgent": False}


# ---------------------------------------------------------------------------
# supervisor.wait_ready
# ---------------------------------------------------------------------------


def test_supervisor_wait_ready_delegates_to_streaming_processes() -> None:
    launcher = _StubLauncher()
    supervisor = ManagedHarnessRuntime(launcher)
    supervisor.start(HarnessLaunchSpec(harness="pi", name="wk", headless=True))
    assert supervisor.wait_ready("pi", "wk", 0.1) is True
    assert supervisor.wait_ready("pi", "ghost", 0.1) is False
    launcher.not_ready = True
    supervisor.start(HarnessLaunchSpec(harness="pi", name="slow", headless=True))
    assert supervisor.wait_ready("pi", "slow", 0.1) is False


def test_supervisor_wait_ready_falls_back_to_running_for_plain_processes() -> None:
    class _Plain:
        running = True
        pid = None

        def stop(self) -> None:
            self.running = False

    class _PlainLauncher:
        def start(self, spec: HarnessLaunchSpec) -> _Plain:
            return _Plain()

    supervisor = ManagedHarnessRuntime(_PlainLauncher())  # type: ignore[arg-type]
    supervisor.start(HarnessLaunchSpec(harness="pi", name="wk", headless=True))
    assert supervisor.wait_ready("pi", "wk", 0.1) is True


# Keep the threading import used by the stub honest about blocking semantics.
assert threading is not None


# ---------------------------------------------------------------------------
# containerized receive (docs/design/design-transfer-container.md)
# ---------------------------------------------------------------------------


def _container_spec(
    name: str = "wk", harness: str = "claude", session_ref: str | None = "session-1"
) -> dict[str, Any]:
    spec = HarnessLaunchSpec(
        harness=harness,
        name=name,
        headless=True,
        cwd=WORKSPACE,
        session_ref=session_ref,
        containerized=True,
        pinned_owner="amyws",
    )
    return spec.to_json()


def _fake_container_ops(monkeypatch: pytest.MonkeyPatch) -> dict[str, list]:
    calls: dict[str, list] = {"prepare": [], "prune": []}

    def fake_prepare(runner: object, **kwargs: object) -> str:
        calls["prepare"].append(kwargs)
        return "hyprial-xfer-cred-fake"

    def fake_prune(runner: object, **kwargs: object) -> list:
        calls["prune"].append(kwargs)
        return []

    monkeypatch.setattr(container, "prepare_worker", fake_prepare)
    monkeypatch.setattr(container, "prune_worker", fake_prune)
    return calls


def test_receive_containerized_pins_owner_and_prepares_volume(
    make_app, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    app = make_app(TARGET)
    calls = _fake_container_ops(monkeypatch)
    staging = container.staging_dir(tmp_path, "wk")
    staging.mkdir(parents=True)

    result = app.handle(
        "transfer.receive",
        {"spec": _container_spec(), "pins": [], "strictTimeoutSeconds": 5},
    )

    assert result["ok"] is True
    # Decision D-D: the daemon's ambient owner is OWNER, but the URI keeps
    # the pinned source owner; the machine segment stays the physical node.
    assert result["actor"] == f"agent:amyws:{TARGET}:wk"
    assert calls["prepare"][0]["name"] == "wk"
    assert calls["prune"] == []
    spec = app.desired_state.load().harnesses[0]
    assert spec.containerized is True
    assert spec.pinned_owner == "amyws"


@pytest.mark.parametrize(
    "owner",
    [
        "u.ou_" + "b" * 32,
        "u.6f8b1d21-33a8-4688-a32e-b1b23e87d5d9",
        "d." + "b" * 32,
        "a." + "a" * 26,
        # Case-insensitive reservation (APFS): uppercase variants are refused too.
        "U.OU_" + "B" * 32,
        "D." + "B" * 32,
        "A." + "A" * 26,
    ],
)
def test_receive_rejects_exact_id_pinned_owner_on_import(
    make_app, owner: str
) -> None:
    app = make_app(TARGET)
    spec = _container_spec()
    spec["pinnedOwner"] = owner

    with pytest.raises(DaemonRequestError) as failure:
        app.handle(
            "transfer.receive",
            {"spec": spec, "pins": [], "strictTimeoutSeconds": 5},
        )

    assert failure.value.code == "INVALID_ARGUMENT"
    assert "pinned owner" in str(failure.value)


@pytest.mark.parametrize("owner", ["d.lane@example.com", "a.smith@x", "u.zhang"])
def test_receive_accepts_id_prefix_pinned_owner(
    make_app, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, owner: str
) -> None:
    app = make_app(TARGET)
    _fake_container_ops(monkeypatch)
    container.staging_dir(tmp_path, "wk").mkdir(parents=True)
    spec = _container_spec()
    spec["pinnedOwner"] = owner
    result = app.handle(
        "transfer.receive", {"spec": spec, "pins": [], "strictTimeoutSeconds": 5}
    )
    assert result["ok"] is True


def test_receive_containerized_requires_staging(
    make_app, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = make_app(TARGET)
    _fake_container_ops(monkeypatch)

    with pytest.raises(DaemonRequestError) as failure:
        app.handle(
            "transfer.receive",
            {"spec": _container_spec(), "pins": [], "strictTimeoutSeconds": 5},
        )

    assert failure.value.code == "TRANSFER_CREDENTIALS"


def test_receive_containerized_failure_prunes_container_artifacts(
    make_app, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    launcher = _StubLauncher()
    launcher.ref_override = "other-ref"  # strict resume cannot hold
    app = make_app(TARGET, launcher)
    calls = _fake_container_ops(monkeypatch)
    staging = container.staging_dir(tmp_path, "wk")
    staging.mkdir(parents=True)

    with pytest.raises(DaemonRequestError) as failure:
        app.handle(
            "transfer.receive",
            {"spec": _container_spec(), "pins": [], "strictTimeoutSeconds": 5},
        )

    assert failure.value.code == "STRICT_RESUME_FAILED"
    assert calls["prune"][0]["name"] == "wk"
    assert list(app.desired_state.load().harnesses) == []
    assert app.agents.get("wk") is None


def test_complete_prunes_container_artifacts(
    make_app, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = make_app(SOURCE)
    _seed_worker(app)
    calls = _fake_container_ops(monkeypatch)

    result = app.handle("transfer.complete", {"name": "wk", "provider": "pi"})

    assert result["ok"] is True
    assert calls["prune"][0]["harness"] == "pi"
