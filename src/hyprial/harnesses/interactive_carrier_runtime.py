"""Actor-owned state machine for the interactive Codex inbox carrier."""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass

from hyprial.actor_runtime import (
    STATE_AUTHORITY,
    ActorRuntime,
    ActorSpec,
    AdmissionResult,
    ExpectedActorError,
)
from hyprial.contracts.ports import PortAdmission
from hyprial.daemon.api import HarnessDelivery

from .codex_carrier_store import CodexCarrierStore


FETCHED = "FETCHED"
TURN_STARTED = "TURN_STARTED"
FINAL_OBSERVED = "FINAL_OBSERVED"
SETTLED = "SETTLED"
REMOTE_SETTLED = "REMOTE_SETTLED"


@dataclass(frozen=True, slots=True)
class CarrierDeliverySnapshot:
    generation: int
    version: int
    delivery: HarnessDelivery
    intent: str
    stage: str
    turn_id: str | None = None
    final_output: str | None = None
    settlement_attempts: int = 0
    next_settlement_at: float = 0.0
    last_reconcile_error: str | None = None


@dataclass(frozen=True, slots=True)
class CarrierFetched:
    delivery: HarnessDelivery
    intent: str


@dataclass(frozen=True, slots=True)
class CarrierTurnStarted:
    generation: int
    delivery_id: str
    turn_id: str


@dataclass(frozen=True, slots=True)
class CarrierFinalObserved:
    generation: int
    delivery_id: str
    output: str


@dataclass(frozen=True, slots=True)
class CarrierSettlementDeferred:
    generation: int
    delivery_id: str
    attempts: int
    next_settlement_at: float


@dataclass(frozen=True, slots=True)
class CarrierSettled:
    generation: int
    delivery_id: str


@dataclass(frozen=True, slots=True)
class CarrierRemoved:
    generation: int
    delivery_id: str


@dataclass(frozen=True, slots=True)
class CarrierReconcileError:
    generation: int
    delivery_id: str
    detail: str | None


CarrierCommand = (
    CarrierFetched
    | CarrierTurnStarted
    | CarrierFinalObserved
    | CarrierSettlementDeferred
    | CarrierSettled
    | CarrierRemoved
    | CarrierReconcileError
)
@dataclass(frozen=True, slots=True)
class SupplyFinalRequested:
    generation: int
    version: int
    delivery_id: str
    turn_id: str
    output: str


@dataclass(frozen=True, slots=True)
class CarrierLogRequested:
    level: str
    event: str
    state: CarrierDeliverySnapshot | None
    fields: tuple[tuple[str, object], ...]


@dataclass(frozen=True, slots=True)
class EnqueueTurnRequested:
    generation: int
    version: int
    delivery: HarnessDelivery


CarrierEffect = SupplyFinalRequested | EnqueueTurnRequested | CarrierLogRequested


@dataclass(slots=True)
class _State:
    delivery: HarnessDelivery
    intent: str
    stage: str = FETCHED
    turn_id: str | None = None
    final_output: str | None = None
    settlement_attempts: int = 0
    next_settlement_at: float = 0.0
    last_reconcile_error: str | None = None
    version: int = 0


class _Projection:
    def __init__(self, effect_capacity: int) -> None:
        self.lock = threading.RLock()
        self.generation = 0
        self.rows: dict[str, CarrierDeliverySnapshot] = {}
        self.effects: deque[CarrierEffect] = deque()
        self.effect_capacity = effect_capacity


class _CarrierShard:
    def __init__(
        self,
        *,
        generation: int,
        actor: str,
        session_ref: str,
        store: CodexCarrierStore,
        projection: _Projection,
    ) -> None:
        self._generation = generation
        self._actor = actor
        self._session_ref = session_ref
        self._store = store
        self._projection = projection
        self._states: dict[str, _State] = {}
        self._recover()

    def __call__(self, command: object) -> None:
        if isinstance(command, CarrierFetched):
            self._fetched(command)
        elif isinstance(command, CarrierTurnStarted):
            if self._current(command.generation, command.delivery_id) is not None:
                self._turn_started(command)
        elif isinstance(command, CarrierFinalObserved):
            if self._current(command.generation, command.delivery_id) is not None:
                self._final(command)
        elif isinstance(command, CarrierSettlementDeferred):
            if self._current(command.generation, command.delivery_id) is not None:
                self._deferred(command)
        elif isinstance(command, CarrierSettled):
            if self._current(command.generation, command.delivery_id) is not None:
                self._settled(command)
        elif isinstance(command, CarrierRemoved):
            if self._current(command.generation, command.delivery_id) is not None:
                self._remove(command.delivery_id)
        elif isinstance(command, CarrierReconcileError):
            state = self._current(command.generation, command.delivery_id)
            if state is not None:
                state.last_reconcile_error = command.detail
                self._publish(state)
        else:
            raise ExpectedActorError(
                "CARRIER_COMMAND_UNSUPPORTED",
                f"unsupported carrier command: {type(command).__name__}",
            )

    def _fetched(self, command: CarrierFetched) -> None:
        message_id = command.delivery.delivery_id
        if message_id in self._states:
            return
        self._store.record_fetched(
            self._actor, self._session_ref, command.delivery, command.intent
        )
        state = _State(delivery=command.delivery, intent=command.intent, version=1)
        self._states[message_id] = state
        self._publish(state)
        self._log_transition(state, None, FETCHED)

    def _turn_started(self, command: CarrierTurnStarted) -> None:
        state = self._states[command.delivery_id]
        self._store.record_turn_started(self._actor, command.delivery_id, command.turn_id)
        previous = state.stage
        state.stage = TURN_STARTED
        state.turn_id = command.turn_id
        state.version += 1
        self._publish(state)
        self._log_transition(state, previous, TURN_STARTED)

    def _final(self, command: CarrierFinalObserved) -> None:
        state = self._states[command.delivery_id]
        if state.stage == SETTLED:
            return
        self._store.record_final(self._actor, command.delivery_id, command.output)
        previous = state.stage
        state.stage = FINAL_OBSERVED
        state.final_output = command.output
        state.version += 1
        self._publish(state)
        if previous != FINAL_OBSERVED:
            self._log_transition(state, previous, FINAL_OBSERVED)

    def _deferred(self, command: CarrierSettlementDeferred) -> None:
        state = self._states[command.delivery_id]
        state.settlement_attempts = command.attempts
        state.next_settlement_at = command.next_settlement_at
        state.version += 1
        self._store.record_settlement_attempts(
            self._actor, command.delivery_id, command.attempts
        )
        self._publish(state)

    def _settled(self, command: CarrierSettled) -> None:
        state = self._states[command.delivery_id]
        previous = state.stage
        self._store.settle_and_delete(self._actor, command.delivery_id)
        state.stage = SETTLED
        state.version += 1
        self._log_transition(state, previous, SETTLED)
        self._states.pop(command.delivery_id, None)
        with self._projection.lock:
            self._projection.rows.pop(command.delivery_id, None)

    def _remove(self, delivery_id: str) -> None:
        self._store.delete(self._actor, delivery_id)
        self._states.pop(delivery_id, None)
        with self._projection.lock:
            self._projection.rows.pop(delivery_id, None)

    def _current(self, generation: int, delivery_id: str) -> _State | None:
        if generation != self._generation:
            return None
        return self._states.get(delivery_id)

    def _publish(self, state: _State) -> None:
        snapshot = self._snapshot(state)
        with self._projection.lock:
            self._projection.generation = self._generation
            self._projection.rows[state.delivery.delivery_id] = snapshot

    def _snapshot(self, state: _State) -> CarrierDeliverySnapshot:
        return CarrierDeliverySnapshot(
            generation=self._generation,
            version=state.version,
            delivery=state.delivery,
            intent=state.intent,
            stage=state.stage,
            turn_id=state.turn_id,
            final_output=state.final_output,
            settlement_attempts=state.settlement_attempts,
            next_settlement_at=state.next_settlement_at,
            last_reconcile_error=state.last_reconcile_error,
        )

    def _log_transition(
        self, state: _State, previous: str | None, target: str
    ) -> None:
        self._log(
            "info",
            "worker.carrier.transition",
            self._snapshot(state),
            {"fromState": previous, "toState": target},
        )

    def _log(
        self,
        level: str,
        event: str,
        state: CarrierDeliverySnapshot | None,
        fields: dict[str, object],
    ) -> None:
        self._effect(
            CarrierLogRequested(
                level=level,
                event=event,
                state=state,
                fields=tuple(fields.items()),
            )
        )

    def _effect(self, effect: CarrierEffect) -> None:
        with self._projection.lock:
            if len(self._projection.effects) >= self._projection.effect_capacity:
                # Telemetry is explicitly lossy and must never crash the state
                # authority. Required enqueue/supply effects are derived from
                # durable snapshots by drain_effects(), not stored here.
                self._projection.effects.popleft()
            self._projection.effects.append(effect)

    def _recover(self) -> None:
        for stored in self._store.load(self._actor, self._session_ref):
            if stored.state == SETTLED:
                self._store.delete(self._actor, stored.delivery.delivery_id)
                continue
            if stored.state == REMOTE_SETTLED:
                # Remote reply+ack already succeeded. Recovery is strictly a
                # local journal cleanup and must never ask the daemon to reply
                # to the now-unavailable pending message again.
                self._store.settle_and_delete(
                    self._actor, stored.delivery.delivery_id
                )
                self._log(
                    "info",
                    "worker.carrier.recovered",
                    None,
                    {
                        "messageId": stored.delivery.delivery_id,
                        "recoveredState": REMOTE_SETTLED,
                    },
                )
                continue
            if stored.state == FETCHED:
                if not stored.delivery.message:
                    self._log(
                        "warn",
                        "worker.carrier.recovered",
                        None,
                        {
                            "messageId": stored.delivery.delivery_id,
                            "recoveredState": "FETCHED_AWAITING_INBOX_REOFFER",
                        },
                    )
                    continue
                state = _State(
                    delivery=stored.delivery,
                    intent=stored.intent,
                    stage=FETCHED,
                    version=1,
                )
                self._states[state.delivery.delivery_id] = state
                self._publish(state)
                self._log(
                    "info",
                    "worker.carrier.recovered",
                    self._snapshot(state),
                    {"recoveredState": FETCHED},
                )
                continue
            if stored.state not in {TURN_STARTED, FINAL_OBSERVED}:
                self._log(
                    "error",
                    "worker.carrier.error",
                    None,
                    {
                        "stage": "state-recovery",
                        "error": f"unknown persisted carrier state {stored.state!r}",
                    },
                )
                continue
            if stored.state == FINAL_OBSERVED and stored.final_output is None:
                self._log(
                    "error",
                    "worker.carrier.error",
                    None,
                    {
                        "stage": "state-recovery",
                        "error": "persisted FINAL_OBSERVED state had no final output",
                    },
                )
                continue
            state = _State(
                delivery=stored.delivery,
                intent=stored.intent,
                stage=stored.state,
                turn_id=stored.turn_id,
                final_output=stored.final_output,
                settlement_attempts=stored.settlement_attempts,
                version=1,
            )
            self._states[state.delivery.delivery_id] = state
            self._publish(state)
            self._log(
                "info",
                "worker.carrier.recovered",
                self._snapshot(state),
                {"recoveredState": state.stage},
            )


class InteractiveCarrierRuntime:
    """One fixed actor shard for all deliveries of an interactive carrier."""

    def __init__(
        self,
        *,
        name: str,
        actor: str,
        session_ref: str,
        store: CodexCarrierStore,
        capacity: int = 128,
    ) -> None:
        self._capacity = capacity
        self._projection = _Projection(effect_capacity=max(16, capacity * 4))
        self._generation = 0
        self._runtime = ActorRuntime()

        def factory() -> _CarrierShard:
            self._generation += 1
            with self._projection.lock:
                self._projection.generation = self._generation
                self._projection.rows.clear()
            return _CarrierShard(
                generation=self._generation,
                actor=actor,
                session_ref=session_ref,
                store=store,
                projection=self._projection,
            )

        self._handle = self._runtime.start(
            ActorSpec(
                name=f"interactive-carrier:{name}",
                handler_factory=factory,
                mailbox_capacity=capacity,
                supervision_profile=STATE_AUTHORITY,
            )
        )

    @property
    def capacity(self) -> int:
        return self._capacity

    def submit(
        self, command: CarrierCommand, *, timeout: float = 0.25
    ) -> PortAdmission:
        # All carrier commands describe durable inbox/turn facts.  The caller
        # is an external I/O driver, so it may back off here; dropping one of
        # these completions would lose the only transition to settlement.
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            result = self._runtime.tell(self._handle, command)
            if result is AdmissionResult.ACCEPTED:
                return PortAdmission.ACCEPTED
            if result is AdmissionResult.CLOSED:
                return PortAdmission.CLOSING
            if time.monotonic() >= deadline:
                return PortAdmission.OVERLOADED
            time.sleep(0.002)

    def snapshots(self) -> tuple[CarrierDeliverySnapshot, ...]:
        with self._projection.lock:
            return tuple(self._projection.rows.values())

    def snapshot(self, delivery_id: str) -> CarrierDeliverySnapshot | None:
        with self._projection.lock:
            return self._projection.rows.get(delivery_id)

    def generation(self) -> int:
        with self._projection.lock:
            return self._projection.generation

    def drain_effects(self) -> tuple[CarrierEffect, ...]:
        with self._projection.lock:
            effects: list[CarrierEffect] = list(self._projection.effects)
            self._projection.effects.clear()
            for state in self._projection.rows.values():
                if state.stage == FETCHED:
                    effects.append(
                        EnqueueTurnRequested(
                            generation=state.generation,
                            version=state.version,
                            delivery=state.delivery,
                        )
                    )
                elif (
                    state.stage == FINAL_OBSERVED
                    and state.turn_id is not None
                    and state.final_output is not None
                ):
                    effects.append(
                        SupplyFinalRequested(
                            generation=state.generation,
                            version=state.version,
                            delivery_id=state.delivery.delivery_id,
                            turn_id=state.turn_id,
                            output=state.final_output,
                        )
                    )
            return tuple(effects)

    def drain(self, timeout: float) -> bool:
        return self._runtime.drain(timeout).complete


__all__ = [
    "FETCHED",
    "FINAL_OBSERVED",
    "SETTLED",
    "TURN_STARTED",
    "CarrierDeliverySnapshot",
    "CarrierFetched",
    "CarrierFinalObserved",
    "CarrierReconcileError",
    "CarrierRemoved",
    "CarrierSettled",
    "CarrierSettlementDeferred",
    "CarrierTurnStarted",
    "CarrierLogRequested",
    "EnqueueTurnRequested",
    "InteractiveCarrierRuntime",
    "SupplyFinalRequested",
]
