"""The serving router admits only the listed router-only names and the registry's.

Decision: daemon-first-z4b-router-only-refusal-20261117.  A recording stand-in
for the application proves two things without a daemon: every listed name still
reaches its own branch, and a name that is neither listed, registered nor
``shutdown`` is refused with ``METHOD_NOT_FOUND`` before any branch runs.
"""

from __future__ import annotations

import types

import pytest

from hyprial.daemon.impl.application.actors.agents_admin import (
    migration as agents_migration,
)
from hyprial.daemon.impl.application.actors.agents_admin import (
    registry_ops as agents_registry_ops,
)
from hyprial.daemon.impl.ipc import router as ipc_router
from hyprial.daemon.impl.request_catalog.migration import ROUTER_ONLY_METHODS
from hyprial.kernel import DaemonRequestError, ipc_errors

SENTINEL = object()


class _Router:
    def __init__(self) -> None:
        self._restore_done = type("Done", (), {"is_set": staticmethod(lambda: True)})()
        self._inbox = self._harnesses = self._presence = object()
        self._routine_service = None
        self.reached: list[str] = []

    def __getattr__(self, name: str):
        if name in ("_handle_agent", "_handle_agent_migration"):
            mixin = _REAL_HANDLERS[name]
            return types.MethodType(mixin, self)
        if not name.startswith(("_ipc_", "_workflow_caller")):
            raise AssertionError(f"unexpected read of {name}")

        def reached(*args, **kwargs):
            self.reached.append(name)
            return SENTINEL

        return reached


class _EveryName:
    def __contains__(self, name: str) -> bool:
        return True


_REAL_HANDLERS = {
    "_handle_agent": agents_registry_ops._AgentRegistryOpsMixin._handle_agent,
    "_handle_agent_migration": agents_migration._AgentMigrationMixin._handle_agent_migration,
}


def _handle(method: str):
    router = _Router()
    return router, ipc_router._IpcDispatchMixin.handle(router, method, {})


def _outcome(method: str):
    try:
        return "answered", _handle(method)[1]
    except DaemonRequestError as error:
        return "refused", error.code, str(error)


@pytest.mark.parametrize("method", sorted(ROUTER_ONLY_METHODS))
def test_a_serving_router_still_reaches_the_branch_of_every_listed_name(
    method, monkeypatch
) -> None:
    router = _Router()
    monkeypatch.setitem(
        _REAL_HANDLERS, "_handle_agent", lambda self, name, params: SENTINEL
    )
    answer = ipc_router._IpcDispatchMixin.handle(router, method, {})
    assert answer is SENTINEL


UNKNOWN_NAMES = [
    "no.such.method",
    "agent.bogus",
    "agent.migrate.bogus",
    "workflow.bogus",
    "routine.bogus",
    "session.bogus",
    "pac.bogus",
    "message.bogus",
    "lifecycle.bogus",
    "outbox.bogus",
    "",
]


@pytest.mark.parametrize("method", UNKNOWN_NAMES)
def test_an_unknown_name_gets_the_answer_it_got_without_the_guard(
    method, monkeypatch
) -> None:
    guarded = _outcome(method)
    monkeypatch.setattr(ipc_router, "ROUTER_ONLY_METHODS", _EveryName())
    assert _outcome(method) == guarded


@pytest.mark.parametrize(
    ("method", "expected"),
    [
        ("no.such.method", ("refused", ipc_errors.METHOD_NOT_FOUND)),
        ("agent.bogus", ("refused", ipc_errors.METHOD_NOT_FOUND)),
        ("agent.migrate.bogus", ("refused", ipc_errors.INVALID_ARGUMENT)),
        ("routine.bogus", ("refused", ipc_errors.METHOD_NOT_FOUND)),
        ("workflow.bogus", ("refused", ipc_errors.METHOD_NOT_FOUND)),
    ],
)
def test_an_unknown_name_answers_as_the_serving_daemon_always_did(method, expected) -> None:
    assert _outcome(method)[:2] == expected


@pytest.mark.parametrize(
    "method", [n for n in UNKNOWN_NAMES if not n.startswith(("agent.migrate.", "routine."))]
)
def test_an_unknown_name_is_refused_before_any_branch_runs(method) -> None:
    router = _Router()
    with pytest.raises(DaemonRequestError) as unknown:
        ipc_router._IpcDispatchMixin.handle(router, method, {})
    assert unknown.value.code == ipc_errors.METHOD_NOT_FOUND
    assert str(unknown.value) == f"unknown daemon method {method}"
    assert router.reached == []


def test_a_registered_name_is_still_answered_by_the_registry(monkeypatch) -> None:
    class Registry:
        def __contains__(self, name: str) -> bool:
            return name == "agent.list"

        def invoke(self, app, name, params, **kwargs):
            return ("registry", name)

    monkeypatch.setattr(ipc_router, "METHOD_REGISTRY", Registry())
    router = _Router()
    assert ipc_router._IpcDispatchMixin.handle(router, "agent.list", {}) == (
        "registry",
        "agent.list",
    )
    assert router.reached == []


@pytest.mark.parametrize("method", ["orgfs.bogus", "identity.bogus", "link.bogus"])
def test_an_unregistered_name_of_a_migrated_family_is_still_unknown(method) -> None:
    with pytest.raises(DaemonRequestError) as unknown:
        _handle(method)
    assert unknown.value.code == ipc_errors.METHOD_NOT_FOUND
