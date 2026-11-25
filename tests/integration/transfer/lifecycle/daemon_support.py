"""Daemon application fixtures for the agent-destroy home-cleanup tests.

``DaemonApplication.handle`` runs over a real ``ManagedHarnessRuntime`` with a
stub launcher, the real ``DesiredStateStore`` and ``AgentRegistry``; only the
transport-adjacent layers (inbox) are stubbed.  ``WORKSPACE`` is aimed at a real
directory by the ``make_app`` fixture because the launch contract rejects an
explicit cwd that does not exist (``HARNESS_CWD_MISSING``).  The module is a support module,
not a test module: the transfer methods it once served were removed.
"""

from __future__ import annotations


from pathlib import Path
from typing import Any

import pytest

from hyprial.kernel.impl.contracts.ports import PortAdmission
from hyprial.daemon.impl.application import DaemonApplication
from hyprial.kernel import HarnessLaunchSpec
from hyprial.daemon.impl.lifecycle_manager import (
    LifecycleKind,
    LifecycleOperation,
    LifecycleResult,
    LifecycleState)
from hyprial.daemon.impl.processes.supervisor import ManagedHarnessRuntime
from hyprial.shell.impl.daemon_wiring import daemon_dependencies as _deep_module_daemon_dependencies
from tests.support.runtime.environment import UNIT_USER_ID

OWNER = "alice"
SOURCE = "laptop"
TARGET = "server"
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
        self.ref_override: str | None = None
        self.not_ready: bool = False
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
        owner=OWNER, user_id=UNIT_USER_ID,
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
        "tests.integration.transfer.lifecycle.daemon_support.WORKSPACE",
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


def _seed_worker(
    app: DaemonApplication, name: str = "wk", session_ref: str | None = "session-1"
) -> None:
    app.handle(
        "lifecycle.start",
        {"provider": "pi", "name": name, "headless": True, "cwd": WORKSPACE},
    )
    launcher = app._harnesses._launcher  # type: ignore[attr-defined]
    launcher.processes[("pi", name)].session_ref = session_ref
    app._harnesses.reconcile_session_refs()
