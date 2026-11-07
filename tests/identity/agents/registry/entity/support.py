"""The Agent entity: identity, uniqueness, liveness, handover and destroy.

Design: docs/notes/design-agent-entity.md (decisions A1, A2, A5, A6, A7, A9).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from hyprial.daemon.impl.application import DaemonApplication
from hyprial.kernel import HarnessLaunchSpec
from hyprial.daemon.impl.lifecycle_manager import (
    LifecycleKind,
    LifecycleOperation,
    LifecycleResult,
    LifecycleState,
)
from hyprial.kernel.impl.contracts.ports import PortAdmission
from hyprial.daemon.impl.inbox import (
    AckResult,
    DeliveryLifecycle,
    InboxMessage,
    SubmissionResult,
)

from hyprial.shell.impl.daemon_wiring import daemon_dependencies as _deep_module_daemon_dependencies
from hyprial.shell.impl.daemon_wiring.routine_coordinator import RoutineCoordinatorAdapter
from hyprial.shell.impl.daemon_wiring.routine_service import RoutineServiceAdapter

OWNER = "op"
NODE = "node-1"
URI = f"agent:{OWNER}:{NODE}:foo"


# --------------------------------------------------------------------------
# doubles
# --------------------------------------------------------------------------


class FakeInbox:
    def __init__(self, pending: tuple[InboxMessage, ...] = ()) -> None:
        self.pending = list(pending)
        self.acks: list[tuple[str, str]] = []

    def pending_messages(self, actor: str) -> tuple[InboxMessage, ...]:
        return tuple(item for item in self.pending if item.recipient == actor)

    def prune_inbox(self):  # type: ignore[no-untyped-def]
        return []

    def pending_all(self) -> tuple[InboxMessage, ...]:
        return tuple(self.pending)

    def submit(self, message: InboxMessage) -> SubmissionResult:
        return SubmissionResult(message.message_id, accepted=True)

    def ack(self, actor: str, message_id: str) -> AckResult:
        self.acks.append((actor, message_id))
        before = len(self.pending)
        self.pending = [
            item
            for item in self.pending
            if not (item.recipient == actor and item.message_id == message_id)
        ]
        return AckResult(message_id, acknowledged=len(self.pending) != before)

    def outbox_count(self) -> int:
        return 0

    def custody_count(self) -> int:
        return 0

    def shutdown(self) -> None:
        return None


class FakeSupervisor:
    """Enough of ManagedHarnessRuntime for lifecycle.start and the liveness probe."""

    def __init__(self) -> None:
        self.running: dict[tuple[str, str], bool] = {}

    def start(self, spec: Any) -> bool:
        self.running[(spec.harness, spec.name)] = True
        return True

    def remove(self, harness: str, name: str) -> bool:
        return self.running.pop((harness, name), None) is not None

    def stop(self) -> None:
        self.running.clear()

    def session_refs(self) -> dict[tuple[str, str], str]:
        return {}

    def status(self) -> tuple[dict[str, object], ...]:
        return tuple(
            {
                "id": f"{harness}:{name}",
                "runtime": harness,
                "name": name,
                "running": running,
            }
            for (harness, name), running in sorted(self.running.items())
        )


class StubPresence:
    def __init__(self, online: tuple[str, ...] = ()) -> None:
        self._online = online

    def actor_online(self, actor: str) -> bool:
        return actor in self._online

    def online_actors(self) -> tuple[str, ...]:
        return self._online

    def online_mailboxes(self) -> tuple[str, ...]:
        return ()


class _InlineLifecycleManager:
    """Synchronous outer-shell double; manager semantics have dedicated tests.

    These tests exercise Agent identity policy in ``DaemonApplication``.  The
    production application never takes this path: it installs the durable
    process manager during composition.  Keeping a typed manager double here
    avoids reviving the removed direct lifecycle fallback merely for unit
    construction.
    """

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
            assert self._app._harnesses is not None
            self._app._harnesses.start(spec)
            effects = ("agent.create", "agent.bind", "harness.ensure", "route.ensure")
        elif operation.kind is LifecycleKind.DEACTIVATE:
            assert self._app._harnesses is not None
            self._app._harnesses.remove(spec.harness, spec.name)
            self._app.desired_state.remove_harness(spec.harness, spec.name)
            self._app._release_agent_binding(operation.target.actor)
            effects = ("harness.remove", "agent.release")
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

    def drain(self, _timeout: float) -> bool:
        # Every operation settles synchronously in submit().
        return True


_TEST_APPLICATIONS: list[DaemonApplication] = []


@pytest.fixture(autouse=True)
def _close_test_applications():  # type: ignore[no-untyped-def]
    yield
    while _TEST_APPLICATIONS:
        app = _TEST_APPLICATIONS.pop()
        app._close()
        app._close_home_lease()


def application(
    tmp_path: Path,
    *,
    inbox: FakeInbox | None = None,
    online: tuple[str, ...] = (),
) -> DaemonApplication:
    app = DaemonApplication(
        state_dir=tmp_path,
        socket_path=tmp_path / "daemon.sock",
        node_id=NODE,
        owner=OWNER,
        zenoh_listen=("tcp/127.0.0.1:0",),
        zenoh_connect=(),
    **_deep_module_daemon_dependencies())
    app.network_isolated = True
    app._inbox = inbox or FakeInbox()  # type: ignore[assignment]
    app._harnesses = FakeSupervisor()  # type: ignore[assignment]
    app._presence = StubPresence(online)  # type: ignore[assignment]
    app._lifecycle_manager = _InlineLifecycleManager(app)  # type: ignore[assignment]
    _TEST_APPLICATIONS.append(app)
    return app


def enable_routines(app: DaemonApplication, state_dir: Path) -> None:
    from hyprial.biz.impl.routine.execution.service import RoutineService

    class Pac:
        def rearm_boundary(self, *, routine_name: str) -> int:
            del routine_name
            return 0

    service = RoutineService(
        pac=Pac(), alarm=object(), state_dir=state_dir
    )
    app._routine_service = RoutineServiceAdapter(service)


def enable_routine_coordinator(app: DaemonApplication, state_dir: Path) -> None:
    from hyprial.biz.impl.routine.execution.coordinator import RoutineCoordinator

    assert app._routine_service is not None
    coordinator = RoutineCoordinator(
        state_dir=state_dir,
        routines=app._routine_service.service,
        ensure_coordinator=lambda _routine: None,
        retire_coordinator=lambda _routine: None,
        close_graph=lambda graph_id, actor: (
            app._pac_graph_authority.close_graph(graph_id, actor=actor)
            if app._pac_graph_authority is not None
            else None
        ),
    )
    app._routine_coordinator = RoutineCoordinatorAdapter(coordinator)


def start_headless(app: DaemonApplication, harness: str, name: str) -> dict[str, Any]:
    return app.handle(
        "lifecycle.start",
        {"provider": harness, "name": name, "headless": True, "cwd": "/work"},
    )






def register_interactive(
    app: DaemonApplication,
    actor: str,
    *,
    session_ref: str = "session-1",
    source: str = "claude-channel",
    runtime: str = "claude_interactive",
) -> dict[str, Any]:
    return app.handle(
        "session.register",
        {
            "actor": actor,
            "sessionRef": session_ref,
            "cwd": "/work",
            "command": ["claude"],
            "source": source,
            "runtime": runtime,
            "channelConfirmed": True,
        },
    )


def message_for(recipient: str, message_id: str = "message-1") -> InboxMessage:
    return InboxMessage(
        message_id=message_id,
        conversation_id="conversation-1",
        sender="agent:op:node-9:bob",
        recipient=recipient,
        payload=json.dumps({"message": "body"}).encode(),
        intent="request",
        lifecycle=DeliveryLifecycle.DURABLE_SERVICE,
        created_at_ms=1,
    )


def _scheduled_routine(
    name: str,
    actor: str,
    *,
    interval: str = "15m",
    task: str = "work",
) -> str:
    import yaml

    return yaml.safe_dump(
        {
            "version": 2,
            "mode": "scheduled",
            "name": name,
            "schedule": {"interval": interval},
            "actor": actor,
            "role": "dispatch",
            "task": task,
            "on_task_timeout": {
                "action": "escalate",
                "escalate_to": "user:op",
            },
        },
        allow_unicode=True,
        sort_keys=False,
    )
