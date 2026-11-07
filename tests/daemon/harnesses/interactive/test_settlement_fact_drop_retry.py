"""Dropped settlement facts under actor pressure must be regenerated.

A one-shot ``_carrier_settled_pending`` mark recorded before the fact is
accepted or durably relayed wedges a FINAL_OBSERVED row in-process when the
relay is full, the same liveness family the revCR2 reject identified for
``_carrier_turn_expired``.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from hyprial.kernel import PortAdmission
from hyprial.daemon.impl.harnesses.carrier.codex_carrier_store import CodexCarrierStore
from hyprial.daemon.impl.harnesses.carrier.interactive_carrier_runtime import (
    CarrierReconcileError,
)
from hyprial.daemon.impl.harnesses.codex.carrier import CodexInteractiveCarrier
from tests.daemon.harnesses.interactive.test_codex_carrier_replay import (
    ACTOR as ACTOR,
)
from tests.daemon.harnesses.interactive.test_codex_carrier_replay import (
    SESSION as SESSION,
)
from tests.daemon.harnesses.interactive.test_codex_carrier_replay import (
    Pump,
    delivery,
    wait_for,
)

FINAL_TURN = {
    "status": "completed",
    "items": [
        {"type": "agentMessage", "phase": "final_answer", "text": "final-a"}
    ],
}


@pytest.fixture
def settle_rig(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HYPRIAL_HOME", str(tmp_path / "home" / ".hyprial"))
    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("HARNESS_SOCKET_PATH", str(tmp_path / "unused.sock"))
    monkeypatch.setenv("HYPRIAL_NETWORK_ISOLATED", "1")
    monkeypatch.setattr(
        "hyprial.daemon.impl.harnesses.streaming.process.StreamingTurnProcess", Pump
    )
    context = SimpleNamespace(now=1000.0, calls=[], hint="keep")

    def request(method, params):
        context.calls.append((method, params))
        if method == "message.settle_hint":
            return {"ok": True, "pending": True, "hint": context.hint}
        if method == "message.ack":
            return {"ok": True, "acknowledged": True}
        return {"ok": True}

    path = tmp_path / "carrier-settle.sqlite3"
    store = CodexCarrierStore(path)
    store.record_fetched(ACTOR, SESSION, delivery(), "request")
    store.record_turn_started(ACTOR, "a", "old-turn")
    store.close()
    carrier = CodexInteractiveCarrier(
        SimpleNamespace(pid=123, read_turn=lambda turn_id: FINAL_TURN),
        actor=ACTOR,
        session_ref=SESSION,
        cwd=tmp_path,
        command=["codex"],
        daemon_request=request,
        state_path=path,
        clock=lambda: context.now,
        logger=SimpleNamespace(
            info=lambda event, **fields: context.calls.append((event, fields)),
            warn=lambda event, **fields: context.calls.append((event, fields)),
            error=lambda event, **fields: context.calls.append((event, fields)),
        ),
    )
    carrier._reconcile_inflight_turns()
    wait_for(
        lambda: carrier._carrier_runtime.snapshot("a") is not None
        and carrier._carrier_runtime.snapshot("a").stage == "FINAL_OBSERVED"
    )
    yield context, carrier
    try:
        if not isinstance(carrier._pump, Pump):
            carrier._pump.stop()
        carrier._carrier_runtime.drain(3)
    finally:
        carrier._store.close()


def _fill_relay(carrier) -> None:
    generation = carrier._carrier_runtime.generation()
    for index in range(carrier._carrier_fact_capacity):
        command = CarrierReconcileError(generation, f"other-{index}", "pressure")
        carrier._carrier_facts[carrier._carrier_fact_key(command)] = command


def _overload(carrier, monkeypatch) -> None:
    monkeypatch.setattr(
        carrier._carrier_runtime,
        "submit",
        lambda *args, **kwargs: PortAdmission.OVERLOADED,
    )


def _final_row(carrier):
    return carrier._carrier_runtime.snapshot("a")


def test_dropped_kept_fact_is_regenerated_after_pressure_relief(settle_rig, monkeypatch):
    context, carrier = settle_rig
    context.hint = "keep"
    real_submit = carrier._carrier_runtime.submit
    _fill_relay(carrier)
    _overload(carrier, monkeypatch)
    carrier._settle_finals()
    assert _final_row(carrier).stage == "FINAL_OBSERVED"
    assert "a" not in carrier._carrier_settled_pending
    monkeypatch.setattr(carrier._carrier_runtime, "submit", real_submit)
    carrier._carrier_facts.clear()
    carrier._settle_finals()
    wait_for(lambda: _final_row(carrier) is None)
    assert "a" in carrier._store.kept_ids(ACTOR)


def test_dropped_settled_fact_is_regenerated_after_pressure_relief(settle_rig, monkeypatch):
    context, carrier = settle_rig
    context.hint = "ack"
    real_submit = carrier._carrier_runtime.submit
    _fill_relay(carrier)
    _overload(carrier, monkeypatch)
    carrier._settle_finals()
    assert _final_row(carrier).stage == "FINAL_OBSERVED"
    assert "a" not in carrier._carrier_settled_pending
    monkeypatch.setattr(carrier._carrier_runtime, "submit", real_submit)
    carrier._carrier_facts.clear()
    carrier._settle_finals()
    wait_for(lambda: _final_row(carrier) is None)
    assert carrier._inbox_hold_allowed()


def test_dropped_status_unknown_fact_is_regenerated_after_pressure_relief(
    settle_rig, monkeypatch
):
    context, carrier = settle_rig
    real_submit = carrier._carrier_runtime.submit
    _fill_relay(carrier)
    _overload(carrier, monkeypatch)
    carrier._settle_status_unknown(
        _final_row(carrier), detail="budget exhausted under pressure"
    )
    assert _final_row(carrier).stage == "FINAL_OBSERVED"
    assert "a" not in carrier._carrier_settled_pending
    monkeypatch.setattr(carrier._carrier_runtime, "submit", real_submit)
    carrier._carrier_facts.clear()
    context.hint = "keep"
    carrier._settle_finals()
    wait_for(lambda: _final_row(carrier) is None)
    assert "a" in carrier._store.kept_ids(ACTOR)


def test_relayed_kept_fact_still_suppresses_duplicate_settlement(settle_rig, monkeypatch):
    context, carrier = settle_rig
    context.hint = "keep"
    calls = {"submit": 0}
    real_submit = carrier._carrier_runtime.submit

    def once_only(command, *, timeout=0.25):
        calls["submit"] += 1
        return PortAdmission.OVERLOADED

    monkeypatch.setattr(carrier._carrier_runtime, "submit", once_only)
    carrier._settle_finals()
    assert "a" in carrier._carrier_settled_pending
    first_calls = len(context.calls)
    carrier._settle_finals()
    assert len(context.calls) == first_calls
    assert calls["submit"] == 1
    monkeypatch.setattr(carrier._carrier_runtime, "submit", real_submit)
    carrier._retry_carrier_facts()
    wait_for(lambda: _final_row(carrier) is None)
    assert "a" in carrier._store.kept_ids(ACTOR)
