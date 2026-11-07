"""D3 (a): routes created during a rebuild window are re-established after it.

`_restore_interactive_routes` only walks ``read_sessions()`` (persisted
sessions), so a route created while the zenoh session is rebuilding is not in
that snapshot and used to stay broken until a full daemon restart.  The intent
recorded before the mutation — and drained by the rebuild-complete hook under
the same lock — closes that coverage gap.
"""
from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest

from hyprial.daemon.impl.application.actors.sessions import route_retry as routes_module
from hyprial.daemon.impl.application.actors.sessions.routes import _SessionRoutesMixin
from hyprial.daemon.impl.network.route_ports import RouteSpec
from hyprial.daemon.impl.route_registration import (
    RouteCommandError,
    RouteRegistrationClient,
)

ACTOR = "agent:x"
LEASE = "session:lease"
KEY = (ACTOR, LEASE)


class _Stub(_SessionRoutesMixin):
    def __init__(self, routes) -> None:
        self._interactive_route_lock = threading.Lock()
        self._interactive_route_resource_tokens: dict[tuple[str, str], str] = {}
        self._routes = routes
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


def _spec() -> RouteSpec:
    return RouteSpec(route_id=ACTOR, liveliness_key="liveliness", inbox_key="inbox")


def _ok():
    return SimpleNamespace(provenance=SimpleNamespace(resource_token="token"))


def _error() -> RouteCommandError:
    return RouteCommandError("ROUTE_IO_FAILED", "RuntimeError: ZError: session closed")


def _mutate(stub: _Stub, client: _Client, *, spec: RouteSpec):
    return stub._settled_route_mutation(
        client,
        operation_id="op",
        generation=1,
        version=1,
        actor=ACTOR,
        owner_lease=LEASE,
        ensure_spec=spec,
    )


def test_exhausted_ensure_stays_pending_and_replays_after_rebuild(monkeypatch):
    monkeypatch.setattr(routes_module, "_ROUTE_RETRY_MAX_ATTEMPTS", 2)
    spec = _spec()
    failing = _Client([_error()])
    stub = _Stub(failing)

    with pytest.raises(RouteCommandError) as caught:
        _mutate(stub, failing, spec=spec)

    assert caught.value.code == "session_route.route_exhausted"
    assert stub._session_route_intents()[KEY] is spec, (
        "an exhausted route must remain recorded for the rebuild replay"
    )

    ok = _Client([_ok()])
    stub._routes = ok
    stub._replay_pending_session_routes()

    assert KEY not in stub._session_route_intents()
    assert ok.calls == 1, "the rebuild replay must re-attempt the pending ensure"


def test_mid_creation_rebuild_does_not_lose_the_route():
    spec = _spec()
    ok = _Client([_ok()])
    stub = _Stub(ok)
    with stub._interactive_route_lock:
        # The create registered its intent, but its declare has not completed.
        stub._session_route_intents()[KEY] = spec

    stub._replay_pending_session_routes()  # rebuild completes first

    assert KEY not in stub._session_route_intents()
    assert ok.calls == 1
    # The in-flight create then completes; it must not resurrect the intent.
    _mutate(stub, ok, spec=spec)
    assert KEY not in stub._session_route_intents()


def test_rebuild_replay_logs_replayed_and_failed_counts(monkeypatch):
    monkeypatch.setattr(routes_module, "_ROUTE_RETRY_MAX_ATTEMPTS", 1)
    ok_stub = _Stub(_Client([_ok()]))
    with ok_stub._interactive_route_lock:
        ok_stub._session_route_intents()[KEY] = _spec()
    ok_stub._replay_pending_session_routes()

    replay = [event for event in ok_stub.events if event[1] == "session_route.rebuild_replay"]
    assert replay and replay[-1][2] == {"replayed": 1, "failed": 0, "pruned": 0}

    failing_stub = _Stub(_Client([_error()]))
    with failing_stub._interactive_route_lock:
        failing_stub._session_route_intents()[KEY] = _spec()
    failing_stub._replay_pending_session_routes()

    replay = [
        event for event in failing_stub.events if event[1] == "session_route.rebuild_replay"
    ]
    assert replay and replay[-1][2] == {"replayed": 0, "failed": 1, "pruned": 0}
    assert KEY in failing_stub._session_route_intents(), (
        "a replay that still fails leaves the intent for the next rebuild"
    )


def test_replay_prunes_a_route_whose_session_was_replaced():
    spec = _spec()
    ok = _Client([_ok()])
    stub = _Stub(ok)
    stub._agent_session_domains = SimpleNamespace(
        session=SimpleNamespace(
            read_session=lambda actor: SimpleNamespace(session_ref="new-ref")
        )
    )
    stale_key = (ACTOR, "session:old-ref")
    with stub._interactive_route_lock:
        stub._session_route_intents()[stale_key] = spec

    stub._replay_pending_session_routes()

    assert stale_key not in stub._session_route_intents()
    assert ok.calls == 0, "a replaced session's route must not be re-ensured"
    replay = [event for event in stub.events if event[1] == "session_route.rebuild_replay"]
    assert replay and replay[-1][2] == {"replayed": 0, "failed": 0, "pruned": 1}


def test_replay_re_ensures_a_route_whose_session_is_still_current():
    spec = _spec()
    ok = _Client([_ok()])
    stub = _Stub(ok)
    stub._agent_session_domains = SimpleNamespace(
        session=SimpleNamespace(
            read_session=lambda actor: SimpleNamespace(session_ref="ref")
        )
    )
    live_key = (ACTOR, "session:ref")
    with stub._interactive_route_lock:
        stub._session_route_intents()[live_key] = spec

    stub._replay_pending_session_routes()

    assert live_key not in stub._session_route_intents()
    assert ok.calls == 1
