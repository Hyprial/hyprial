from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path

from hyprial.kernel.impl.actor_runtime import AdmissionResult
from hyprial.kernel.impl.contracts.ports import PortAdmission
from hyprial.daemon.impl.api import HarnessDelivery
from hyprial.daemon.impl.harnesses.carrier.codex_carrier_store import CodexCarrierStore
from hyprial.daemon.impl.harnesses import codex
from hyprial.daemon.impl.harnesses.carrier.interactive_carrier_runtime import (
    CarrierFetched,
    CarrierFinalObserved,
    CarrierReconcileError,
    CarrierRemoved,
    CarrierSettled,
    CarrierTurnStarted,
    EnqueueTurnRequested,
    InteractiveCarrierRuntime,
    SupplyFinalRequested,
)


#: How long this suite lets a submission wait for mailbox room.  ``submit``'s
#: own default (0.25s) is the production back-off for an external I/O driver,
#: not a claim about how fast a loaded CI host drains one command: with the
#: shard slowed to 0.3s per command the pressure test below hit
#: PortAdmission.OVERLOADED, which is the shape CI failed on (tasks 15852,
#: 16015).  The test's subject is "pressure never crashes the authority", so
#: it waits for room instead of asserting the back-off budget.
SUBMIT_WAIT_SECONDS = 30.0


def _wait_until(predicate: Callable[[], bool], timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.005)
    raise AssertionError("condition was not satisfied before timeout")


def _delivery(identifier: str = "message-1") -> HarnessDelivery:
    return HarnessDelivery(
        delivery_id=identifier,
        conversation_id="conversation-1",
        sender="adapter:lark:test",
        recipient="agent:op:node:worker",
        message="durable prompt",
    )


def test_recovered_fetched_row_retains_prompt_and_reenters_turn_fifo(tmp_path) -> None:
    path = tmp_path / "carrier.sqlite3"
    delivery = _delivery()
    seed = CodexCarrierStore(path)
    seed.record_fetched(delivery.recipient, "session", delivery, "request")
    seed.close()

    store = CodexCarrierStore(path)
    enqueued: list[HarnessDelivery] = []
    runtime = InteractiveCarrierRuntime(
        name="recovery",
        actor=delivery.recipient,
        session_ref="session",
        store=store,
    )
    enqueued.extend(
        effect.delivery
        for effect in runtime.drain_effects()
        if isinstance(effect, EnqueueTurnRequested)
    )
    [snapshot] = runtime.snapshots()
    assert snapshot.delivery.message == "durable prompt"
    assert not hasattr(snapshot, "client")
    assert runtime.drain(1.0) is True
    store.close()


def test_final_emits_immutable_effect_instead_of_calling_client_in_actor(tmp_path) -> None:
    delivery = _delivery()
    store = CodexCarrierStore(tmp_path / "carrier.sqlite3")
    runtime = InteractiveCarrierRuntime(
        name="effects",
        actor=delivery.recipient,
        session_ref="session",
        store=store,
    )
    runtime.submit(CarrierFetched(delivery, "request"))
    _wait_until(lambda: runtime.snapshot(delivery.delivery_id) is not None)
    generation = runtime.generation()
    attempt = runtime.snapshot(delivery.delivery_id).attempt
    runtime.submit(
        CarrierTurnStarted(generation, delivery.delivery_id, "turn-1", attempt)
    )
    runtime.submit(
        CarrierFinalObserved(generation, delivery.delivery_id, "final answer")
    )
    _wait_until(
        lambda: any(
            isinstance(effect, SupplyFinalRequested)
            for effect in runtime.drain_effects()
        )
    )
    [snapshot] = runtime.snapshots()
    assert snapshot.turn_id == "turn-1"
    assert snapshot.final_output == "final answer"
    assert runtime.drain(1.0) is True
    store.close()


def _carrier_turn_started(
    generation: int, delivery_id: str, turn_id: str, attempt: int
) -> CarrierTurnStarted:
    return CarrierTurnStarted(generation, delivery_id, turn_id, attempt)


def test_a_stale_turn_started_cannot_overwrite_a_reoffered_attempt(tmp_path) -> None:
    delivery = _delivery()
    store = CodexCarrierStore(tmp_path / "carrier.sqlite3")
    runtime = InteractiveCarrierRuntime(
        name="stale-turn",
        actor=delivery.recipient,
        session_ref="session",
        store=store,
    )
    try:
        runtime.submit(CarrierFetched(delivery, "request"))
        _wait_until(lambda: runtime.snapshot(delivery.delivery_id) is not None)
        first = runtime.snapshot(delivery.delivery_id)
        generation = runtime.generation()
        runtime.submit(
            _carrier_turn_started(
                generation,
                delivery.delivery_id,
                "turn-1",
                getattr(first, "attempt", 0),
            )
        )
        _wait_until(
            lambda: runtime.snapshot(delivery.delivery_id).turn_id == "turn-1"
        )
        runtime.submit(CarrierRemoved(generation, delivery.delivery_id))
        _wait_until(lambda: runtime.snapshot(delivery.delivery_id) is None)

        # A re-offer is a new attempt.  The old turn-start fact must not attach
        # to it, even though the generation and delivery id are unchanged.
        runtime.submit(CarrierFetched(delivery, "request"))
        _wait_until(lambda: runtime.snapshot(delivery.delivery_id) is not None)
        second = runtime.snapshot(delivery.delivery_id)
        runtime.submit(
            _carrier_turn_started(
                generation,
                delivery.delivery_id,
                "turn-1",
                getattr(first, "attempt", 0),
            )
        )
        time.sleep(0.1)
        assert runtime.snapshot(delivery.delivery_id).turn_id is None

        runtime.submit(
            _carrier_turn_started(
                generation,
                delivery.delivery_id,
                "turn-2",
                getattr(second, "attempt", 0),
            )
        )
        _wait_until(
            lambda: runtime.snapshot(delivery.delivery_id).turn_id == "turn-2"
        )
    finally:
        runtime.drain(2.0)
        store.close()



def test_sustained_actor_overload_has_hard_deadline_and_does_not_hang_stop(
    tmp_path,
) -> None:
    delivery = _delivery()
    store = CodexCarrierStore(tmp_path / "carrier.sqlite3")
    runtime = InteractiveCarrierRuntime(
        name="overload",
        actor=delivery.recipient,
        session_ref="session",
        store=store,
    )
    original_tell = runtime._runtime.tell  # type: ignore[attr-defined]
    runtime._runtime.tell = (  # type: ignore[method-assign,attr-defined]
        lambda handle, command: AdmissionResult.OVERLOADED
    )
    started = time.monotonic()
    assert runtime.submit(
        CarrierReconcileError(
            runtime.generation(), delivery.delivery_id, "still overloaded"
        ),
        timeout=0.02,
    ) is PortAdmission.OVERLOADED
    assert time.monotonic() - started < 0.2

    runtime._runtime.tell = original_tell  # type: ignore[method-assign,attr-defined]
    assert runtime.drain(1.0) is True
    store.close()


def test_codex_driver_retains_final_and_settled_facts_across_forced_overload(
    tmp_path,
) -> None:
    class Server:
        pid = 123

        def start_turn(self, prompt: str) -> str:
            raise AssertionError("turn I/O is not part of this fact-relay test")

        def read_turn(self, turn_id: str) -> dict[str, object]:
            raise AssertionError("turn I/O is not part of this fact-relay test")

        def interrupt_turn(self, turn_id: str) -> None:
            return None

    carrier = codex.CodexInteractiveCarrier(
        Server(),  # type: ignore[arg-type]
        actor="agent:op:node:worker",
        session_ref="session",
        cwd=Path("/tmp"),
        command=["codex"],
        daemon_request=lambda method, params: {"messages": []},
        state_path=tmp_path / "carrier.sqlite3",
        poll_seconds=0.01,
    )
    delivery = _delivery()
    try:
        assert carrier._submit_carrier_fact(CarrierFetched(delivery, "request"))
        _wait_until(lambda: carrier._carrier_runtime.snapshot(delivery.delivery_id) is not None)
        generation = carrier._carrier_runtime.generation()
        attempt = carrier._carrier_runtime.snapshot(delivery.delivery_id).attempt
        assert carrier._submit_carrier_fact(
            CarrierTurnStarted(generation, delivery.delivery_id, "turn-1", attempt)
        )
        _wait_until(
            lambda: carrier._carrier_runtime.snapshot(delivery.delivery_id).turn_id
            == "turn-1"  # type: ignore[union-attr]
        )

        original_submit = carrier._carrier_runtime.submit
        carrier._carrier_runtime.submit = (  # type: ignore[method-assign]
            lambda command, timeout=0.25: PortAdmission.OVERLOADED
        )
        assert not carrier._submit_carrier_fact(
            CarrierFinalObserved(generation, delivery.delivery_id, "durable final")
        )
        assert len(carrier._carrier_facts) == 1
        carrier._carrier_runtime.submit = original_submit  # type: ignore[method-assign]
        carrier._retry_carrier_facts()
        _wait_until(
            lambda: carrier._carrier_runtime.snapshot(delivery.delivery_id).final_output
            == "durable final"  # type: ignore[union-attr]
        )

        carrier._carrier_runtime.submit = (  # type: ignore[method-assign]
            lambda command, timeout=0.25: PortAdmission.OVERLOADED
        )
        assert not carrier._submit_carrier_fact(
            CarrierSettled(generation, delivery.delivery_id)
        )
        assert len(carrier._carrier_facts) == 1
        carrier._carrier_runtime.submit = original_submit  # type: ignore[method-assign]
        carrier._retry_carrier_facts()
        _wait_until(
            lambda: carrier._carrier_runtime.snapshot(delivery.delivery_id) is None
        )
        assert not carrier._carrier_facts
        assert carrier._store is not None
        assert carrier._store.load(delivery.recipient, "session") == ()
    finally:
        carrier._pump.stop()
        carrier._carrier_runtime.drain(1.0)
        if carrier._store is not None:
            carrier._store.close()
            carrier._store = None


def test_telemetry_pressure_never_crashes_carrier_state_authority(tmp_path) -> None:
    store = CodexCarrierStore(tmp_path / "carrier.sqlite3")
    runtime = InteractiveCarrierRuntime(
        name="telemetry-pressure",
        actor="agent:op:node:worker",
        session_ref="session",
        store=store,
        capacity=1,
    )
    for index in range(32):
        delivery = _delivery(f"message-{index}")
        assert (
            runtime.submit(
                CarrierFetched(delivery, "request"), timeout=SUBMIT_WAIT_SECONDS
            )
            is PortAdmission.ACCEPTED
        )
    _wait_until(lambda: len(runtime.snapshots()) == 32, timeout=SUBMIT_WAIT_SECONDS)
    effects = runtime.drain_effects()
    logs = [effect for effect in effects if type(effect).__name__ == "CarrierLogRequested"]
    enqueues = [effect for effect in effects if isinstance(effect, EnqueueTurnRequested)]
    assert len(logs) <= 16
    assert len(enqueues) == 32
    assert runtime.drain(1.0) is True
    store.close()


def test_remote_dual_true_journal_prevents_reply_after_overload_and_rebuild(
    tmp_path,
) -> None:
    class Server:
        pid = 123

        def start_turn(self, prompt: str) -> str:
            raise AssertionError("no turn start expected")

        def read_turn(self, turn_id: str) -> dict[str, object]:
            raise AssertionError("no turn read expected")

        def interrupt_turn(self, turn_id: str) -> None:
            return None

    path = tmp_path / "carrier.sqlite3"
    replies: list[dict[str, object]] = []

    def first_daemon(method: str, params: dict[str, object]) -> dict[str, object]:
        if method == "message.settle_hint":
            return {"ok": True, "hint": "ack", "pending": True}
        if method in {"session.turn.started", "session.turn.ended"}:
            return {"ok": True}
        assert method == "message.ack"
        replies.append(params)
        return {"ok": True, "acknowledged": True}

    first = codex.CodexInteractiveCarrier(
        Server(),  # type: ignore[arg-type]
        actor="agent:op:node:worker",
        session_ref="session",
        cwd=Path("/tmp"),
        command=["codex"],
        daemon_request=first_daemon,
        state_path=path,
        poll_seconds=0.01,
    )
    delivery = _delivery("message-remote-settled")
    first._submit_carrier_fact(CarrierFetched(delivery, "request"))
    _wait_until(lambda: first._carrier_runtime.snapshot(delivery.delivery_id) is not None)
    generation = first._carrier_runtime.generation()
    attempt = first._carrier_runtime.snapshot(delivery.delivery_id).attempt
    first._submit_carrier_fact(
        CarrierTurnStarted(generation, delivery.delivery_id, "turn-1", attempt)
    )
    first._submit_carrier_fact(
        CarrierFinalObserved(generation, delivery.delivery_id, "one final")
    )
    _wait_until(
        lambda: first._carrier_runtime.snapshot(delivery.delivery_id).final_output
        == "one final"  # type: ignore[union-attr]
    )
    state = first._carrier_runtime.snapshot(delivery.delivery_id)
    assert state is not None
    original_submit = first._carrier_runtime.submit
    first._carrier_runtime.submit = (  # type: ignore[method-assign]
        lambda command, timeout=0.25: PortAdmission.OVERLOADED
    )
    first._settle_final(state)
    assert len(replies) == 1
    assert len(first._carrier_facts) == 1
    assert first._store is not None
    [journaled] = first._store.load(delivery.recipient, "session")
    assert journaled.state == "REMOTE_SETTLED"

    # Abrupt process teardown: intentionally never retry the in-memory fact.
    first._pump.stop()
    first._carrier_runtime.submit = original_submit  # type: ignore[method-assign]
    first._carrier_runtime.drain(1.0)
    first._store.close()
    first._store = None

    def rebuilt_daemon(method: str, params: dict[str, object]) -> dict[str, object]:
        if method == "message.ack":
            raise AssertionError("REMOTE_SETTLED recovery must not ack again")
        return {"messages": []}

    rebuilt = codex.CodexInteractiveCarrier(
        Server(),  # type: ignore[arg-type]
        actor=delivery.recipient,
        session_ref="session",
        cwd=Path("/tmp"),
        command=["codex"],
        daemon_request=rebuilt_daemon,
        state_path=path,
        poll_seconds=0.01,
    )
    try:
        assert rebuilt._store is not None
        assert rebuilt._store.load(delivery.recipient, "session") == ()
        assert len(replies) == 1
    finally:
        rebuilt._pump.stop()
        rebuilt._carrier_runtime.drain(1.0)
        if rebuilt._store is not None:
            rebuilt._store.close()
            rebuilt._store = None


def test_fetched_custody_burst_is_staged_before_overloaded_actor_admission(
    tmp_path,
) -> None:
    class Server:
        pid = 123

        def start_turn(self, prompt: str) -> str:
            raise AssertionError("turn execution is outside custody staging")

        def read_turn(self, turn_id: str) -> dict[str, object]:
            raise AssertionError("turn execution is outside custody staging")

        def interrupt_turn(self, turn_id: str) -> None:
            return None

    carrier = codex.CodexInteractiveCarrier(
        Server(),  # type: ignore[arg-type]
        actor="agent:op:node:worker",
        session_ref="session",
        cwd=Path("/tmp"),
        command=["codex"],
        daemon_request=lambda method, params: {"messages": []},
        state_path=tmp_path / "carrier.sqlite3",
        poll_seconds=0.01,
    )
    original_submit = carrier._carrier_runtime.submit
    carrier._carrier_runtime.submit = (  # type: ignore[method-assign]
        lambda command, timeout=0.25: PortAdmission.OVERLOADED
    )
    count = carrier._carrier_runtime.capacity + 9
    try:
        for index in range(count):
            delivery = _delivery(f"custody-{index}")
            assert not carrier._stage_carrier_fetched(delivery, "request")
        assert carrier._store is not None
        staged = carrier._store.load(carrier.actor, carrier.session_ref)
        assert len(staged) == count
        assert all(row.state == "FETCHED" for row in staged)
        assert all(row.delivery.message == "durable prompt" for row in staged)
        assert len(carrier._carrier_facts) == carrier._carrier_runtime.capacity

        carrier._carrier_runtime.submit = original_submit  # type: ignore[method-assign]
        carrier._retry_carrier_facts()
        deadline = time.monotonic() + 3.0
        while len(carrier._carrier_runtime.snapshots()) < count:
            carrier._refill_staged_fetched()
            assert time.monotonic() < deadline
            time.sleep(0.005)
        effects = carrier._carrier_runtime.drain_effects()
        enqueues = [
            effect.delivery.delivery_id
            for effect in effects
            if isinstance(effect, EnqueueTurnRequested)
        ]
        assert len(enqueues) == count
        assert len(set(enqueues)) == count
    finally:
        carrier._carrier_runtime.submit = original_submit  # type: ignore[method-assign]
        carrier._pump.stop()
        carrier._carrier_runtime.drain(1.0)
        if carrier._store is not None:
            carrier._store.close()
            carrier._store = None
