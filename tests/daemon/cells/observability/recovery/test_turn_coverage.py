"""Whole-terminal coverage is declared by the producer, never inferred by name.

Design §5 E2 and §3.1: a scope of `whole-session` is a claim about what the
producer can see. A carrier-only observation must not assert whole-session
idle, and a detached session without a producer must not look idle at all.
"""
from __future__ import annotations

import time

import pytest

from hyprial.kernel import DaemonRequestError, ipc_errors
from tests.daemon.cells.observability.mount.test_mount import composed as composed

ACTOR = "agent:owner:node:codex-terminal"
SESSION = "codex-session"
SOURCE = "codex-app-server"


def _register(app, *, source: str = SOURCE) -> None:
    if app.agents.get("codex-terminal") is None:
        app.agents.create("codex-terminal")
    app._ipc_session_register(
        {
            "actor": ACTOR,
            "sessionRef": SESSION,
            "cwd": "/tmp/codex",
            "command": ["codex", "--remote"],
            "source": source,
            "runtime": "codex_interactive",
        }
    )
    domains = app._agent_session_domains
    domains.liveness.release(ACTOR)
    domains.liveness.bind(
        ACTOR, harness="codex", runtime="interactive", session_id=SESSION
    )


def _declare(app, coverage: str) -> None:
    app._ipc_session_refresh(
        {"actor": ACTOR, "sessionRef": SESSION, "turnCoverage": coverage}
    )


def _facts(app):
    return next(iter(app._cell_interactive_facts().values()))


def _states(app):
    now = time.time_ns() // 1_000_000
    return app._agent_session_domains.cells.run_states(
        app._cell_interactive_facts(), {},
        now_ms=now,
        monotonic_ms=time.monotonic_ns() // 1_000_000,
    )


def test_an_undeclared_codex_session_reports_carrier_only_coverage(composed):
    app, *_ = composed
    _register(app)
    app._record_carrier_turn_started(ACTOR, SESSION, "turn-1")
    facts = _facts(app)
    assert facts.scope == "carrier-deliveries"
    row = _states(app)[ACTOR]
    assert row["state"] == "unknown"
    assert row["reason"] == "partial-turn-coverage"
    assert row["observationHealth"] == "partial"
    assert row["coverage"]["turnBoundaries"] is True


def test_a_declared_whole_session_producer_reports_in_turn_then_idle(composed):
    app, *_ = composed
    _register(app)
    _declare(app, "whole-session")
    app._record_carrier_turn_started(ACTOR, SESSION, "turn-typed")
    row = _states(app)[ACTOR]
    assert row["scope"] == "whole-session"
    assert row["state"] == "in-turn"
    assert row["reason"] == "turn-open"

    app._record_carrier_turn_ended(ACTOR, SESSION, "turn-typed", int(time.time() * 1000))
    row = _states(app)[ACTOR]
    assert row["state"] == "idle"
    assert row["reason"] == "no-open-turn"
    assert row["observationHealth"] == "current"


def test_a_carrier_only_session_never_claims_whole_session_idle(composed):
    app, *_ = composed
    _register(app)
    _declare(app, "carrier-deliveries")
    app._record_carrier_turn_started(ACTOR, SESSION, "delivery-turn")
    app._record_carrier_turn_ended(ACTOR, SESSION, "delivery-turn", int(time.time() * 1000))
    row = _states(app)[ACTOR]
    assert row["scope"] == "carrier-deliveries"
    assert row["state"] != "idle"
    assert row["reason"] == "partial-turn-coverage"


def test_legacy_channel_sources_keep_whole_session_coverage(composed):
    app, *_ = composed
    _register(app, source="claude-channel")
    app._record_carrier_turn_started(ACTOR, SESSION, "turn-1")
    assert _facts(app).scope == "whole-session"


def test_a_new_registration_drops_the_previous_declaration(composed):
    app, *_ = composed
    _register(app)
    _declare(app, "whole-session")
    assert _facts(app).scope == "whole-session"
    _register(app)
    assert _facts(app).scope == "carrier-deliveries"


def test_an_unknown_coverage_value_is_rejected(composed):
    app, *_ = composed
    _register(app)
    with pytest.raises(DaemonRequestError) as error:
        _declare(app, "whole-fleet")
    assert error.value.code == ipc_errors.INVALID_ARGUMENT
    assert _facts(app).scope == "carrier-deliveries"
