from __future__ import annotations

from pathlib import Path

from hyprial.daemon.impl.application import DaemonApplication
from hyprial.biz.impl.routine.execution.coordinator import RoutineCoordinator
from hyprial.biz.impl.routine.execution.service import RoutineService
from hyprial.biz.impl.routine.templates import render_template
from hyprial.shell.impl.daemon_wiring import daemon_dependencies as _deep_module_daemon_dependencies
from hyprial.shell.impl.daemon_wiring.routine_service import RoutineServiceAdapter
from hyprial.shell.impl.daemon_wiring.routine_coordinator import RoutineCoordinatorAdapter


class _IdlePac:
    def rearm_boundary(self, *, routine_name: str) -> int:
        return 0


def test_composed_routine_mutations_use_durable_coordinator(tmp_path: Path) -> None:
    app = DaemonApplication(
        state_dir=tmp_path,
        socket_path=tmp_path / "daemon.sock",
        node_id="node",
        owner="owner",
    **_deep_module_daemon_dependencies())
    service = RoutineService(pac=_IdlePac(), alarm=object(), state_dir=tmp_path)
    coordinator = RoutineCoordinator(
        state_dir=tmp_path,
        routines=service,
        ensure_coordinator=lambda _routine: None,
        retire_coordinator=lambda _routine: None,
        close_graph=lambda _graph_id, _actor: None,
    )
    app._routine_service = RoutineServiceAdapter(service)
    app._routine_coordinator = RoutineCoordinatorAdapter(coordinator)
    app._inbox = object()  # type: ignore[assignment]
    app._harnesses = object()  # type: ignore[assignment]
    app._presence = object()  # type: ignore[assignment]
    app._transport = object()  # type: ignore[assignment]  # composed branch guard
    app._workflow_caller = lambda _params: "user:owner"  # type: ignore[method-assign]

    class LegacyLockForbidden:
        def __enter__(self):  # type: ignore[no-untyped-def]
            raise AssertionError("legacy routine lock used")

    app._routine_legacy_lock = LegacyLockForbidden()  # type: ignore[assignment]
    text = render_template(
        "selfdrive", owner="agent:owner:node:coordinator",
        escalate_to="user:owner",
    )
    try:
        added = app.handle(
            "routine.add", {"yaml": text, "operationId": "routine-add-test"}
        )
        assert added["enabled"] is True
        assert service.status(name="selfdrive")["enabled"] is True
        app.agents.create("coordinator")
        replacement = render_template(
            "agent-home-setup",
            owner="agent:owner:node:coordinator",
            escalate_to="user:owner",
            name="replacement",
        )
        assert app.handle(
            "routine.add",
            {"yaml": replacement, "operationId": "routine-add-replacement"},
        )["enabled"] is True
        removed = app.handle(
            "routine.remove", {"name": "selfdrive", "operationId": "routine-remove-test"}
        )
        assert removed["removed"] is True
        assert coordinator.stats()["operationsPending"] == 0
    finally:
        app._transport = None
        app._close()
        app._close_home_lease()
