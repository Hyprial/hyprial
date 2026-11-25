"""``teleport.run`` through the registry: one handler for run and dry-run.

The handler reaches the source service only through the module-level name
``source_service(app)`` in ``hyprial.daemon.impl.request_catalog.teleport``
(imported from ``hyprial.daemon.impl.teleport.wiring.source``); tests replace
that name with a fake service.
"""

from __future__ import annotations

import importlib
from typing import Any

import pytest

from hyprial.daemon.impl.request_catalog.registry import METHOD_REGISTRY
from hyprial.kernel import DaemonRequestError, ipc_errors

from ..fakes import source

CATALOG = "hyprial.daemon.impl.request_catalog.teleport"
WIRING = "hyprial.daemon.impl.teleport.wiring.source"

RESULT = {
    "ok": True,
    "operationId": "op-1",
    "dryRun": False,
    "agent": "w1",
    "from": {"node": "node-a", "uri": "agent://owner-1/node-a/w1"},
    "to": {"node": "node-b", "uri": "agent://owner-1/node-b/w1"},
    "bytes": 10,
    "routines": [],
    "commit": {"ok": True, "pausedRoutines": []},
}
DRY = {**RESULT, "dryRun": True, "to": {"node": "node-b", "uri": None}, "bytes": 0}
del DRY["commit"]


class FakeService:
    def __init__(self, outcome: Any = None) -> None:
        self.calls: list[tuple] = []
        self.outcome = outcome

    def run(self, agent: str, *, to: str, dry_run: bool = False) -> dict:
        self.calls.append((agent, to, dry_run))
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        if self.outcome is not None:
            return self.outcome
        return DRY if dry_run else RESULT


@pytest.fixture
def service(monkeypatch: pytest.MonkeyPatch) -> FakeService:
    fake = FakeService()
    catalog = importlib.import_module(CATALOG)
    seen: list[Any] = []

    def provide(app: Any) -> FakeService:
        seen.append(app)
        return fake

    monkeypatch.setattr(catalog, "source_service", provide)
    fake.apps = seen
    return fake


APP = object()


def _invoke(params: dict) -> Any:
    return METHOD_REGISTRY.invoke(APP, "teleport.run", params)


def test_wiring_entry_point_exists() -> None:
    wiring = importlib.import_module(WIRING)
    assert callable(wiring.source_service)
    catalog = importlib.import_module(CATALOG)
    assert catalog.TELEPORT_BINDINGS
    assert catalog.TELEPORT_BINDINGS[0][0] is catalog.TELEPORT_RUN_SPEC


def test_run_and_dry_run_reach_the_same_service(service: FakeService) -> None:
    assert _invoke({"agent": "w1", "to": "node-b"}) == RESULT
    assert _invoke({"agent": "w1", "to": "node-b", "dryRun": True}) == DRY
    assert _invoke({"agent": "w1", "to": "node-b", "dryRun": False}) == RESULT
    assert service.calls == [
        ("w1", "node-b", False),
        ("w1", "node-b", True),
        ("w1", "node-b", False),
    ]
    assert service.apps == [APP, APP, APP]


@pytest.mark.parametrize(
    "params",
    [{"agent": "w1"}, {"agent": "w1", "to": "node-b", "force": True}, {"agent": 1, "to": "x"}],
)
def test_bad_params_are_invalid_argument_before_the_service(
    service: FakeService, params: dict
) -> None:
    with pytest.raises(DaemonRequestError) as caught:
        _invoke(params)
    assert caught.value.code == ipc_errors.INVALID_ARGUMENT
    assert service.calls == []


def test_teleport_failure_becomes_a_declared_daemon_error(service: FakeService) -> None:
    service.outcome = source().TeleportRunError("TELEPORT_AGENT_EXISTS", "taken on node-b")
    with pytest.raises(DaemonRequestError) as caught:
        _invoke({"agent": "w1", "to": "node-b"})
    assert caught.value.code == "TELEPORT_AGENT_EXISTS"
    assert "taken on node-b" in str(caught.value)


def test_unresolved_failure_keeps_its_manual_check_data(service: FakeService) -> None:
    data = {
        "operationId": "op-1",
        "targetNode": "node-b",
        "agent": "w1",
        "sourceAgentUri": "agent://owner-1/node-a/w1",
        "bytesSent": 10,
        "sourceState": "stopped",
        "pausedRoutines": [],
        "manualCheck": "check node-b for w1",
    }
    service.outcome = source().TeleportRunError("TELEPORT_UNRESOLVED", "unknown", data)
    with pytest.raises(DaemonRequestError) as caught:
        _invoke({"agent": "w1", "to": "node-b"})
    assert caught.value.code == "TELEPORT_UNRESOLVED"
    assert caught.value.data == data


def test_unknown_agent_is_agent_not_found(service: FakeService) -> None:
    service.outcome = source().TeleportRunError(ipc_errors.AGENT_NOT_FOUND, "no w9")
    with pytest.raises(DaemonRequestError) as caught:
        _invoke({"agent": "w9", "to": "node-b"})
    assert caught.value.code == ipc_errors.AGENT_NOT_FOUND
