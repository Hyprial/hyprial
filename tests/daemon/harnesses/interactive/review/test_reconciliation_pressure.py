"""Independent review probes for deadline reconciliation under actor pressure."""

from hyprial.kernel import PortAdmission
from hyprial.daemon.impl.harnesses.carrier.interactive_carrier_runtime import (
    CarrierReconcileError,
)
from tests.daemon.harnesses.interactive.test_codex_carrier_replay import (
    delivery,
    rig as rig,
    wait_for,
)


def test_idle_deadline_retries_when_actor_and_fact_relay_were_full(rig, monkeypatch):
    carrier = rig.create(delivery(), started=True)
    wait_for(lambda: carrier._carrier_runtime.snapshot("a") is not None)
    carrier._reconcile_inflight_turns()
    runtime_submit = carrier._carrier_runtime.submit
    for i in range(carrier._carrier_fact_capacity):
        command = CarrierReconcileError(
            carrier._carrier_runtime.generation(), f"other-{i}", "pressure"
        )
        carrier._carrier_facts[carrier._carrier_fact_key(command)] = command
    monkeypatch.setattr(
        carrier._carrier_runtime,
        "submit",
        lambda *args, **kwargs: PortAdmission.OVERLOADED,
    )
    rig.now += 900
    carrier._reconcile_inflight_turns()
    assert carrier._carrier_runtime.snapshot("a").stage == "TURN_STARTED"
    assert any(
        event == "worker.carrier.fact.deferred"
        and fields.get("stage") == "CarrierStatusUnknown"
        for event, fields in rig.logs
    )
    monkeypatch.setattr(carrier._carrier_runtime, "submit", runtime_submit)
    carrier._carrier_facts.clear()
    for _ in range(3):
        rig.now += 900
        carrier._reconcile_inflight_turns()
        assert carrier._carrier_runtime.drain(3)
    assert carrier._carrier_runtime.snapshot("a").stage == "STATUS_UNKNOWN"
    assert carrier._inbox_hold_allowed()
    assert not any(method == "message.ack" for method, _ in rig.calls)
