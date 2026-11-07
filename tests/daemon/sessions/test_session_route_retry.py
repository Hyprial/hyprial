"""D3: a settled route mutation must not retry without a bound.

``_settled_route_mutation`` retried every retryable ``RouteCommandError`` in a
``while True`` loop with an exponential backoff capped at 0.5 s and no attempt
or deadline bound, so a persistent error blocked the caller forever -- the same
"unbounded wait" family as the shutdown join-hang.

The bound is a terminal ``session_route.route_exhausted`` error carrying the
attempt count and the deadline; ``PORT_CLOSING`` still propagates on the first
attempt because a closing port is not retryable.
"""
from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest

from hyprial.daemon.impl.application.actors.sessions import route_retry as routes_module
from hyprial.daemon.impl.application.actors.sessions.routes import _SessionRoutesMixin
from hyprial.daemon.impl.route_registration import (
    RouteCommandError,
    RouteRegistrationClient,
)


class _Stub(_SessionRoutesMixin):
    def __init__(self) -> None:
        self._interactive_route_lock = threading.Lock()
        self._interactive_route_resource_tokens: dict[tuple[str, str], str] = {}
        self.events: list[tuple[str, str, dict]] = []

    def _log(self, level: str, component: str, event: str, **fields) -> None:
        self.events.append((level, event, fields))


class _Client(RouteRegistrationClient):
    """A route client that replays a fixed outcome list (the last one repeats)."""

    def __init__(self, outcomes) -> None:
        self._outcomes = list(outcomes)
        self.calls = 0

    def call_settled(self, request):
        self.calls += 1
        outcome = self._outcomes[min(self.calls - 1, len(self._outcomes) - 1)]
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def _ok():
    return SimpleNamespace(provenance=SimpleNamespace(resource_token="token"))


def _error(code: str) -> RouteCommandError:
    return RouteCommandError(code, f"{code} simulated")


def _call(stub: _Stub, client: _Client):
    return stub._settled_route_mutation(
        client,
        operation_id="op",
        generation=1,
        version=1,
        actor="agent:x",
        owner_lease="session:lease",
    )


def test_persistent_retryable_error_terminates_as_exhausted(monkeypatch):
    monkeypatch.setattr(routes_module, "_ROUTE_RETRY_MAX_ATTEMPTS", 3)
    client = _Client([_error("ROUTE_IO_FAILED")])  # always retryable

    with pytest.raises(RouteCommandError) as caught:
        _call(_Stub(), client)

    assert caught.value.code == "session_route.route_exhausted"
    assert client.calls == 3


def test_exhaustion_records_attempts_and_deadline(monkeypatch):
    monkeypatch.setattr(routes_module, "_ROUTE_RETRY_MAX_ATTEMPTS", 2)
    stub = _Stub()

    with pytest.raises(RouteCommandError):
        _call(stub, _Client([_error("ROUTE_IO_FAILED")]))

    exhausted = [
        event for event in stub.events if event[1] == "session_route.route_exhausted"
    ]
    assert exhausted, "the terminal error must be recorded"
    level, _event, fields = exhausted[-1]
    assert level == "error"
    assert fields["attempts"] == 2
    assert fields["deadlineSeconds"] == routes_module._ROUTE_RETRY_DEADLINE_SECONDS
    assert fields["lastErrorCode"] == "ROUTE_IO_FAILED"


def test_port_closing_is_not_retried():
    client = _Client([_error("PORT_CLOSING")])

    with pytest.raises(RouteCommandError) as caught:
        _call(_Stub(), client)

    assert caught.value.code == "PORT_CLOSING"
    assert client.calls == 1


def test_two_retries_then_success_behaviour_is_unchanged():
    client = _Client([_error("ROUTE_IO_FAILED"), _error("ROUTE_IO_FAILED"), _ok()])

    completed = _call(_Stub(), client)

    assert completed.provenance.resource_token == "token"
    assert client.calls == 3
