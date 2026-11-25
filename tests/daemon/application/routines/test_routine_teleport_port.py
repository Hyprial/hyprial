# Decision record (file header; no inline comments by repository rule):
# - Agent teleport runs in the daemon layer, which reaches routines only
#   through RoutineServicePort (implemented in shell by
#   RoutineServiceAdapter). Slice 1 therefore adds three port methods that
#   mirror the RoutineFacade ones and keep the port's plain-dict style:
#     export(*, name) -> {"name", "yaml", "enabled", "owner"}
#     list_executed_by(*, agent) -> {"routines": [...]}
#     wait_in_flight_drained(*, names, timeout_seconds) -> list[str]
#   The adapter translates RoutineServiceError into RoutinePortError like
#   every other port method. Paused creation is the existing
#   add(yaml_text=, owner=, enabled=False).
# - These are internal only: no routine.* IPC method is added, and the
#   published routine.add keeps auto-resuming (still answers enabled=true).
"""Agent teleport slice 1: the routine capabilities at the daemon port."""

from __future__ import annotations

from pathlib import Path

import pytest

from hyprial.daemon import RoutinePortError
from hyprial.daemon.impl.application import DaemonRequestError
from hyprial.daemon.impl.application.ports import RoutineServicePort
from hyprial.shell.impl.daemon_wiring.routine_service import RoutineServiceAdapter
from tests.identity.agents.registry.entity.support import (
    _close_test_applications as _close_test_applications,
    _scheduled_routine,
    address,
    application,
    enable_routines,
)

TELEPORT_METHODS = ("export", "list_executed_by", "wait_in_flight_drained")


def _app_with_agent(tmp_path: Path):
    app = application(tmp_path)
    enable_routines(app, tmp_path)
    app.handle("agent.create", {"name": "foo"})
    default = app.handle("routine.list", {"actor": "user:op"})["routines"][0]["name"]
    return app, default


@pytest.mark.parametrize("method", TELEPORT_METHODS)
def test_port_and_adapter_declare_the_teleport_capability(method: str) -> None:
    assert callable(getattr(RoutineServicePort, method, None))
    assert callable(getattr(RoutineServiceAdapter, method, None))


def test_adapter_still_satisfies_the_port(tmp_path: Path) -> None:
    app, _default = _app_with_agent(tmp_path)
    assert isinstance(app._routine_service, RoutineServicePort)


def test_export_through_the_port(tmp_path: Path) -> None:
    app, default = _app_with_agent(tmp_path)
    port = app._routine_service
    exported = port.export(name=default)
    assert set(exported) == {"name", "yaml", "enabled", "owner"}
    assert exported["name"] == default
    assert exported["enabled"] is True
    assert address(app, "foo") in exported["yaml"]

    with pytest.raises(RoutinePortError) as failure:
        port.export(name="absent")
    assert failure.value.code == "ROUTINE_NOT_FOUND"


def test_list_executed_by_through_the_port(tmp_path: Path) -> None:
    app, default = _app_with_agent(tmp_path)
    port = app._routine_service
    app.handle("agent.create", {"name": "bar"})
    listed = port.list_executed_by(agent=address(app, "foo"))
    assert [item["name"] for item in listed["routines"]] == [default]
    assert all(
        item["actor"] == address(app, "bar")
        for item in port.list_executed_by(agent=address(app, "bar"))["routines"]
    )


def test_wait_in_flight_drained_through_the_port(tmp_path: Path) -> None:
    app, default = _app_with_agent(tmp_path)
    port = app._routine_service
    port.pause(name=default)
    assert port.wait_in_flight_drained(names=[default], timeout_seconds=1.0) == []
    with pytest.raises(RoutinePortError) as failure:
        port.wait_in_flight_drained(names=["absent"], timeout_seconds=1.0)
    assert failure.value.code == "ROUTINE_NOT_FOUND"


def test_paused_add_through_the_port_stays_paused(tmp_path: Path) -> None:
    app, _default = _app_with_agent(tmp_path)
    port = app._routine_service
    text = _scheduled_routine("teleported", address(app, "foo"))
    added = port.add(yaml_text=text, owner="user:op", enabled=False)
    assert added["enabled"] is False
    status = app.handle("routine.status", {"name": "teleported", "actor": "user:op"})
    assert status["enabled"] is False
    assert port.export(name="teleported") == {
        "name": "teleported",
        "yaml": text,
        "enabled": False,
        "owner": "user:op",
    }


def test_published_routine_add_still_auto_resumes(tmp_path: Path) -> None:
    app, _default = _app_with_agent(tmp_path)
    added = app.handle(
        "routine.add",
        {"yaml": _scheduled_routine("published", address(app, "foo")), "actor": "user:op"},
    )
    assert added["enabled"] is True
    status = app.handle("routine.status", {"name": "published", "actor": "user:op"})
    assert status["enabled"] is True


@pytest.mark.parametrize(
    "method",
    ["routine.export", "routine.listExecutedBy", "routine.waitInFlightDrained"],
)
def test_no_ipc_method_is_published(tmp_path: Path, method: str) -> None:
    app, default = _app_with_agent(tmp_path)
    with pytest.raises(DaemonRequestError) as failure:
        app.handle(method, {"name": default, "actor": "user:op"})
    assert failure.value.code == "METHOD_NOT_FOUND"
