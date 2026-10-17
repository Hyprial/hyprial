"""CodexInteractiveCarrier settlement behaviors (transitional mixin)."""
from __future__ import annotations


import sys as sys
import time

from hyprial.kernel import capped_exponential
from hyprial.kernel import PortAdmission
from hyprial.daemon.impl.api import HarnessDelivery

from hyprial.daemon.impl.harnesses.carrier.codex_carrier_support import HintPort
from hyprial.daemon.impl.harnesses.carrier.contract import settle_injected
from hyprial.daemon.impl.harnesses.carrier.interactive_carrier_runtime  import (
    FINAL_OBSERVED,
    TURN_STARTED,
    CarrierDeliverySnapshot,
    CarrierCommand,
    CarrierFetched,
    CarrierFinalObserved,
    CarrierKept,
    CarrierLogRequested,
    CarrierReconcileError,
    CarrierRemoved,
    CarrierSettled,
    CarrierSettlementDeferred,
    EnqueueTurnRequested,
    SupplyFinalRequested,
)
from hyprial.kernel import ipc_errors
from hyprial.daemon.impl.harnesses.codex.native_env import (
    _final_reply,
    _turn_error,
)
from hyprial.daemon.impl.harnesses.codex.process import (
    CodexAppServerRpcError,
)


class _CarrierSettlementMixin:
        def _reconcile_inflight_turns(self) -> None:
            inflight = tuple(
                state
                for state in self._carrier_runtime.snapshots()
                if state.stage == TURN_STARTED and state.turn_id is not None
            )
            for state in inflight:
                assert state.turn_id is not None
                try:
                    turn = self.server.read_turn(state.turn_id)
                except CodexAppServerRpcError as error:
                    detail = str(error) or type(error).__name__
                    if state.last_reconcile_error != detail:
                        self._log(
                            "warn",
                            "worker.carrier.error",
                            state=state,
                            stage="turn-reconcile",
                            error=detail,
                        )
                        self._submit_carrier_fact(
                            CarrierReconcileError(
                                generation=state.generation,
                                delivery_id=state.delivery.delivery_id,
                                detail=detail,
                            )
                        )
                    continue
                except Exception as error:  # noqa: BLE001 - independent read retry
                    detail = str(error) or type(error).__name__
                    if state.last_reconcile_error != detail:
                        self._log(
                            "error",
                            "worker.carrier.error",
                            state=state,
                            stage="turn-reconcile",
                            error=detail,
                        )
                        self._submit_carrier_fact(
                            CarrierReconcileError(
                                generation=state.generation,
                                delivery_id=state.delivery.delivery_id,
                                detail=detail,
                            )
                        )
                    continue
                if state.last_reconcile_error is not None:
                    self._submit_carrier_fact(
                        CarrierReconcileError(
                            generation=state.generation,
                            delivery_id=state.delivery.delivery_id,
                            detail=None,
                        )
                    )
                reply = _final_reply(turn)
                final_phase = any(
                    isinstance(item, dict)
                    and item.get("type") == "agentMessage"
                    and item.get("phase") == "final_answer"
                    for item in turn.get("items", [])
                    if isinstance(turn.get("items"), list)
                )
                if turn.get("status") == "completed" or (
                    reply is not None and final_phase
                ):
                    if reply is None:
                        self._log(
                            "error",
                            "worker.carrier.error",
                            state=state,
                            stage="turn-reconcile",
                            error="completed Codex turn had no final agent message",
                        )
                        continue
                    self._submit_carrier_fact(
                        CarrierFinalObserved(
                            generation=state.generation,
                            delivery_id=state.delivery.delivery_id,
                            output=reply,
                        )
                    )
                elif turn.get("status") in {"failed", "interrupted"}:
                    self._report_turn_ended(state.turn_id, "failed", state.delivery.delivery_id)
                    self._log(
                        "error",
                        "worker.carrier.error",
                        state=state,
                        stage="turn-terminal",
                        error=_turn_error(turn, state.turn_id),
                    )
                    self._submit_carrier_fact(
                        CarrierRemoved(
                            generation=state.generation,
                            delivery_id=state.delivery.delivery_id,
                        )
                    )
                    with self._carrier_io_lock:
                        self._carrier_clients.pop(state.delivery.delivery_id, None)

        def _settle_finals(self) -> None:
            now = time.monotonic()
            ready = tuple(
                state
                for state in self._carrier_runtime.snapshots()
                if state.stage == FINAL_OBSERVED
                and state.final_output is not None
                and state.next_settlement_at <= now
                and state.delivery.delivery_id not in self._carrier_settled_pending
            )
            for state in ready:
                try:
                    self._settle_final(state)
                except Exception as error:  # noqa: BLE001 - isolate one row's settlement
                    # One row's settlement or journalling failure must not starve
                    # the other deliveries on this carrier; the row itself stays
                    # FINAL_OBSERVED and is retried on the next poll.
                    self._log(
                        "error",
                        "worker.carrier.error",
                        state=state,
                        stage="settlement",
                        error=str(error) or type(error).__name__,
                    )

        def _settle_final(self, state: CarrierDeliverySnapshot) -> None:
            """Report the turn's end, then do exactly what the daemon's hint says.

            Never answers on the model's behalf: ``ack`` acks, ``keep`` does
            nothing but remember the id.  An unconfirmed turn report does not
            block settlement; it is retried from the journal.
            """

            message_id = state.delivery.delivery_id
            if state.turn_id is not None:
                self._report_turn_ended(state.turn_id, "completed", message_id)
            port = HintPort(self.daemon_request, self._signed, self._daemon_error_code)
            outcome = settle_injected(port, [message_id])
            if outcome.acked:
                self._settle_confirmed(state)
            elif outcome.kept:
                self._settle_kept(state)
            elif message_id in port.ack_unavailable:
                self._settle_ack_unavailable(state)
            else:
                self._defer_settlement(
                    state,
                    method="message.settle_hint",
                    error=port.errors.get(message_id, "settlement not confirmed"),
                )

        def _settle_kept(self, state: CarrierDeliverySnapshot) -> None:
            message_id = state.delivery.delivery_id
            self._carrier_settled_pending.add(message_id)
            self._submit_carrier_fact(
                CarrierKept(generation=state.generation, delivery_id=message_id)
            )
            self._log("info", "worker.carrier.settlement.kept", state=state)
            with self._carrier_io_lock:
                self._carrier_clients.pop(message_id, None)

        def _report_turn_ended(self, turn_id: str, status: str, delivery_id: str) -> None:
            if turn_id not in self._carrier_reported_ended:
                self._turn_reports.record(turn_id, "ended", status, delivery_id)
                self._carrier_reported_ended.add(turn_id)
            self._turn_reports.flush()

        def _settle_ack_unavailable(self, state: CarrierDeliverySnapshot) -> None:
            """Require durable delivery evidence before terminalising an ack miss."""
    
            message_id = state.delivery.delivery_id
            try:
                status = self.daemon_request(
                    "message.status", self._signed({"messageId": message_id})
                )
            except Exception as status_error:  # noqa: BLE001 - unknown fate retries
                self._defer_settlement(
                    state,
                    method="message.ack",
                    error=f"MESSAGE_ACK_UNAVAILABLE; status unavailable: {status_error}",
                )
                return
            status_state = str(status.get("state") or "unknown")
            if status_state not in {"fetched", "expired"}:
                self._defer_settlement(
                    state,
                    method="message.ack",
                    error=f"MESSAGE_ACK_UNAVAILABLE; delivery status={status_state}",
                )
                return
            self._settle_elsewhere(
                state,
                method="message.ack",
                event="worker.carrier.settlement.superseded",
                code=ipc_errors.MESSAGE_ACK_UNAVAILABLE,
                deliveryStatus=status_state,
            )

        def _settle_elsewhere(
            self,
            state: CarrierDeliverySnapshot,
            *,
            method: str,
            event: str,
            code: str,
            level: str = "warn",
            **fields: object,
        ) -> None:
            self._log(
                level,
                event,
                state=state,
                stage=method,
                daemonCode=code,
                attempts=state.settlement_attempts,
                **fields,
            )
            self._settle_confirmed(state)

        def _settle_confirmed(self, state: CarrierDeliverySnapshot) -> None:
            message_id = state.delivery.delivery_id
            store = self._store
            if store is None:
                raise RuntimeError("carrier store closed before remote settlement journal")
            store.record_remote_settled(self.actor, message_id)
            self._carrier_settled_pending.add(message_id)
            self._submit_carrier_fact(
                CarrierSettled(
                    generation=state.generation,
                    delivery_id=message_id,
                )
            )
            with self._carrier_io_lock:
                self._carrier_clients.pop(message_id, None)

        def _defer_settlement(
            self,
            state: CarrierDeliverySnapshot,
            *,
            method: str,
            error: str,
            result: dict[str, object] | None = None,
        ) -> None:
            attempts = state.settlement_attempts + 1
            delay = min(
                5.0,
                max(
                    self.settlement_retry_seconds,
                    capped_exponential(
                        self.settlement_retry_seconds, 5.0, attempts - 1
                    ),
                ),
            )
            next_settlement_at = time.monotonic() + delay
            self._submit_carrier_fact(
                CarrierSettlementDeferred(
                    generation=state.generation,
                    delivery_id=state.delivery.delivery_id,
                    attempts=attempts,
                    next_settlement_at=next_settlement_at,
                )
            )
            self._log(
                "warn",
                "worker.carrier.settlement.pending",
                state=state,
                stage=method,
                attempt=attempts,
                retryInSeconds=delay,
                error=error or "unknown settlement error",
                **(
                    {
                        "acknowledged": result.get("acknowledged"),
                        "queued": result.get("queued"),
                    }
                    if result is not None
                    else {}
                ),
            )

        def _drain_carrier_effects(self) -> None:
            for effect in self._carrier_runtime.drain_effects():
                if isinstance(effect, EnqueueTurnRequested):
                    enqueue_key = (
                        effect.delivery.delivery_id,
                        effect.generation,
                        effect.version,
                    )
                    if enqueue_key in self._carrier_enqueued:
                        continue
                    if not self._pump.enqueue(effect.delivery):
                        self._log(
                            "warn",
                            "worker.carrier.effect.deferred",
                            stage="turn-enqueue",
                            messageId=effect.delivery.delivery_id,
                        )
                    else:
                        self._carrier_enqueued.add(enqueue_key)
                elif isinstance(effect, SupplyFinalRequested):
                    with self._carrier_io_lock:
                        client = self._carrier_clients.get(effect.delivery_id)
                    if client is not None:
                        client.supply_reconciled_final(effect.turn_id, effect.output)
                elif isinstance(effect, CarrierLogRequested):
                    self._log(
                        effect.level,
                        effect.event,
                        state=effect.state,
                        **dict(effect.fields),
                    )

        def _submit_carrier_fact(self, command: CarrierCommand) -> bool:
            admission = self._carrier_runtime.submit(command, timeout=0.05)
            if admission is PortAdmission.ACCEPTED:
                return True
            if admission is PortAdmission.CLOSING:
                self._log(
                    "error",
                    "worker.carrier.fact.rejected",
                    stage=type(command).__name__,
                    error="carrier actor is closing; durable store retains the fact",
                )
                return False
            with self._carrier_fact_lock:
                key = self._carrier_fact_key(command)
                if key in self._carrier_facts:
                    self._carrier_facts[key] = command
                    return False
                if len(self._carrier_facts) >= self._carrier_fact_capacity:
                    self._log(
                        "warn",
                        "worker.carrier.fact.deferred",
                        stage=type(command).__name__,
                        error="carrier durable fact relay is full; store retains fact",
                    )
                    return False
                self._carrier_facts[key] = command
            self._log(
                "warn",
                "worker.carrier.fact.deferred",
                stage=type(command).__name__,
                retryInSeconds=self.poll_seconds,
            )
            return False

        def _retry_carrier_facts(self) -> None:
            while True:
                with self._carrier_fact_lock:
                    item = next(iter(self._carrier_facts.items()), None)
                    key, command = item if item is not None else (None, None)
                if command is None:
                    return
                admission = self._carrier_runtime.submit(command, timeout=0.02)
                if admission is PortAdmission.OVERLOADED:
                    return
                with self._carrier_fact_lock:
                    if key is not None and self._carrier_facts.get(key) == command:
                        self._carrier_facts.pop(key, None)
                if admission is PortAdmission.CLOSING:
                    self._log(
                        "error",
                        "worker.carrier.fact.rejected",
                        stage=type(command).__name__,
                        error="carrier actor closed before durable fact replay",
                    )

        def _stage_carrier_fetched(
            self, delivery: HarnessDelivery, intent: str
        ) -> bool:
            store = self._store
            if store is None:
                raise RuntimeError("carrier store closed before fetched custody journal")
            # fetched=True has already transferred daemon custody. Persist the
            # complete prompt before actor admission so relay pressure or process
            # death cannot lose the only copy.
            store.record_fetched(
                self.actor,
                self.session_ref,
                delivery,
                intent,
            )
            return self._submit_carrier_fact(CarrierFetched(delivery, intent))

        def _refill_staged_fetched(self) -> None:
            store = self._store
            if store is None:
                return
            known = {
                state.delivery.delivery_id
                for state in self._carrier_runtime.snapshots()
            }
            with self._carrier_fact_lock:
                relayed = {
                    key[1]
                    for key in self._carrier_facts
                    if key[0] == CarrierFetched.__name__
                }
            for stored in store.load(self.actor, self.session_ref):
                message_id = stored.delivery.delivery_id
                if (
                    stored.state != "FETCHED"
                    or not stored.delivery.message
                    or stored.injection_format is None
                    or message_id in known
                    or message_id in relayed
                ):
                    continue
                self._submit_carrier_fact(
                    CarrierFetched(stored.delivery, stored.intent)
                )

        @staticmethod
        def _carrier_fact_key(command: CarrierCommand) -> tuple[str, str]:
            delivery_id = getattr(command, "delivery_id", None)
            if not isinstance(delivery_id, str):
                delivery = getattr(command, "delivery", None)
                delivery_id = getattr(delivery, "delivery_id", "")
            return type(command).__name__, str(delivery_id)

        def _log(
            self,
            level: str,
            event: str,
            *,
            state: CarrierDeliverySnapshot | None = None,
            **fields: object,
        ) -> None:
            logger = self.logger
            if logger is None:
                return
            try:
                emit = getattr(logger, level)
                emit(
                    event,
                    actorId=self.actor,
                    sessionRef=self.session_ref,
                    **(
                        {
                            "messageId": state.delivery.delivery_id,
                            "conversationId": state.delivery.conversation_id,
                            "deliveryState": state.stage,
                            **(
                                {"turnId": state.turn_id}
                                if state.turn_id is not None
                                else {}
                            ),
                        }
                        if state is not None
                        else {}
                    ),
                    **fields,
                )
            except (NameError, ImportError):
                raise
            except OSError:
                return
