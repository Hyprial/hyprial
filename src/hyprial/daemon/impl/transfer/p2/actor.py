"""One P2 authority mailbox with one ordered SQLite effect worker.

No network, agent creation or identity API calls are enabled by this module.
Caller timeouts/cancellation abandon waiting, not accepted mutation custody.
ActorRuntime owns supervision; EffectLane owns execution and completion retry.
"""

from concurrent.futures import Future, InvalidStateError, TimeoutError
from dataclasses import dataclass
from enum import StrEnum
import threading
import time
from uuid import uuid4

from hyprial.kernel import ActorRuntime, ActorSpec, AdmissionResult
from hyprial.kernel import EffectCompleted, EffectLane, EffectRequest

from .model import Admission, AdmissionConflict, Binding, Reason, identifier
from .store import AdmissionStore
from .journal import AttentionEvent, StepState, StepStart
from .source_ports import (
    CustodyOverride,
    OverrideAudit,
    PendingQuiesce,
    SourceIntent,
    SourceFinish,
    apply_override,
    apply_source,
)
from .projection import CustodyProjection, CustodyQuery, SourceCustody, SourceHold


class Operation(StrEnum):
    PREPARE = "prepare"
    GET = "get"
    REQUIRE_LIVE = "require-live"
    RENEW = "renew"
    REVOKE = "revoke"
    CANCEL_PREPARED = "cancel-prepared"


@dataclass(frozen=True, slots=True)
class Command:
    operation: Operation
    operation_id: str
    binding: Binding | None = None
    expected_expiry: int | None = None
    expires_at_ms: int | None = None

    def __post_init__(self) -> None:
        identifier(self.operation_id)
        if not isinstance(self.operation, Operation):
            raise ValueError("unknown preparation operation")
        if (self.operation is Operation.PREPARE) != isinstance(self.binding, Binding):
            raise ValueError("only prepare requires a binding")
        if self.operation is Operation.RENEW:
            if type(self.expected_expiry) is not int or type(self.expires_at_ms) is not int:
                raise ValueError("renew requires integer expected and new expiry")
        elif self.expected_expiry is not None or self.expires_at_ms is not None:
            raise ValueError("only renew accepts expiry values")


@dataclass(frozen=True, slots=True)
class _Submit:
    request_id: str
    command: (Command | CustodyQuery | SourceIntent | SourceFinish
              | CustodyOverride | PendingQuiesce)
    result: Future


@dataclass(frozen=True, slots=True)
class _Outcome:
    record: (Admission | StepStart | StepState | AttentionEvent | OverrideAudit
             | tuple[StepState, ...] | None) = None
    rejected: Reason | None = None
    custody: SourceCustody | None = None


@dataclass(frozen=True, slots=True)
class _Drain:
    done: Future


def _finish(future: Future, value: object = None, error: Exception | None = None) -> None:
    """A cancelled observer never cancels custody or crashes its authority."""
    try:
        if error is None:
            future.set_result(value)
        else:
            future.set_exception(error)
    except InvalidStateError:
        if not future.cancelled():
            raise


class AdmissionActor:
    def __init__(self, store: AdmissionStore, *, capacity: int = 32) -> None:
        self._custody = CustodyProjection()
        self._custody_sequence = 0
        self._custody_requests: dict[str, int] = {}
        self._store = store
        self._capacity = capacity
        self._pending: dict[str, Future] = {}
        self._drain: Future | None = None
        self._closing: Future | None = None
        # This lock serializes external submit/close admission only. Database
        # and phase mutation are owned by the mailbox/effect worker, not it.
        self._admission = threading.Lock()
        self._runtime = ActorRuntime()
        self._handle = self._runtime.start(ActorSpec(
            name="transfer-p2-authority", handler_factory=lambda: self._receive,
            mailbox_capacity=capacity, undelivered_sink=self._undelivered,
        ))
        self._effects: EffectLane[
            Command | CustodyQuery | SourceIntent | SourceFinish | CustodyOverride
            | PendingQuiesce, _Outcome
        ] = EffectLane(
            name="transfer-p2-io", execute=self._execute,
            complete=lambda event: self._runtime.tell(self._handle, event),
            capacity=capacity, workers=1,
        )

    def source_custody(self, actor: str, *, expected_entity_token: str) -> SourceCustody:
        return self._custody.read(CustodyQuery(actor, expected_entity_token))

    def hydrate_custody(self, actor: str, *, expected_entity_token: str) -> Future:
        return self.submit(CustodyQuery(actor, expected_entity_token))

    def submit(self, command: Command | CustodyQuery | SourceIntent | SourceFinish
               | CustodyOverride | PendingQuiesce) -> Future:
        result: Future = Future()
        with self._admission:
            if self._closing is not None:
                result.set_exception(RuntimeError("P2 admission actor is closed"))
                return result
            admitted = self._runtime.tell(self._handle, _Submit(uuid4().hex, command, result))
            if admitted is not AdmissionResult.ACCEPTED:
                result.set_exception(RuntimeError(f"P2 admission actor is {admitted.value}"))
        return result

    @staticmethod
    def _undelivered(message: object, reason: str) -> None:
        if isinstance(message, _Submit) and not message.result.done():
            _finish(message.result, error=RuntimeError(f"P2 command not delivered: {reason}"))
        elif isinstance(message, _Drain) and not message.done.done():
            message.done.set_exception(RuntimeError(f"P2 drain not delivered: {reason}"))

    def _receive(self, message: object) -> None:
        if isinstance(message, _Submit):
            sequence = None
            query = (message.command if isinstance(message.command, CustodyQuery) else
                     message.command.query if isinstance(
                         message.command, (SourceIntent, SourceFinish, CustodyOverride))
                     else None)
            if query is not None:
                self._custody_sequence += 1
                sequence = self._custody_sequence * 2
                # A stale clear observation is not permission while its refresh
                # is unresolved (including queue overload or effect failure).
                self._custody.publish(SourceCustody(
                    query.actor, query.expected_entity_token,
                    reason="refresh-pending",
                ), sequence=sequence)
            if len(self._pending) >= self._capacity:
                if not message.result.done():
                    _finish(message.result, error=RuntimeError("P2 effect lane is overloaded"))
                return
            self._pending[message.request_id] = message.result
            if sequence is not None:
                self._custody_requests[message.request_id] = sequence + 1
            admitted = self._effects.submit(EffectRequest(message.request_id, 1, message.command))
            if admitted is not AdmissionResult.ACCEPTED:
                self._pending.pop(message.request_id)
                self._custody_requests.pop(message.request_id, None)
                if not message.result.done():
                    _finish(message.result, error=RuntimeError(f"P2 effect lane is {admitted.value}"))
        elif isinstance(message, EffectCompleted):
            result = self._pending.pop(message.operation_id, None)
            sequence = self._custody_requests.pop(message.operation_id, None)
            custody = None
            if sequence is not None and message.result is not None and message.result.custody is not None:
                custody = self._custody.publish(message.result.custody, sequence=sequence)
            if result is not None and not result.done():
                if message.error is not None:
                    _finish(result, error=RuntimeError(f"P2 effect outcome unknown: {message.error}"))
                elif message.result.rejected is not None:
                    _finish(result, error=AdmissionConflict(message.result.rejected))
                else:
                    _finish(result, message.result.record if message.result.record is not None else custody)
            # Duplicate completion is harmless. The effect is not released
            # until the authority has handled its result/rejection.
            self._effects.acknowledge(message.operation_id, message.generation)
        elif isinstance(message, _Drain):
            self._drain = message.done
        else:
            raise TypeError("unexpected P2 authority message")
        if self._drain is not None and not self._pending and not self._drain.done():
            self._drain.set_result(None)

    def _execute(self, command: Command | CustodyQuery | SourceIntent | SourceFinish
                 | CustodyOverride | PendingQuiesce) -> _Outcome:
        if isinstance(command, CustodyOverride):
            try:
                audit = apply_override(self._store, command)
            except AdmissionConflict as error:
                return _Outcome(rejected=error.reason)
            observation = self._execute(command.query)
            return _Outcome(record=audit, custody=observation.custody)
        if isinstance(command, (SourceIntent, SourceFinish)):
            try:
                result = apply_source(self._store, command)
                observation = self._execute(command.query)
                return _Outcome(record=result, custody=observation.custody)
            except AdmissionConflict as error:
                return _Outcome(rejected=error.reason)
        if isinstance(command, PendingQuiesce):
            return _Outcome(record=self._store.journal.pending_quiesce(command.limit))
        if isinstance(command, CustodyQuery):
            try:
                revision, states = self._store.journal.custody_snapshot(
                    command.actor, command.expected_entity_token)
                pending = any(s.phase != "quiesced" for s in states)
                # An operator override is part of the custody fact: the row
                # stays held, but every outstanding step must have been
                # explicitly audited before the guard stops suppressing.
                overridden = bool(states) and all(s.needs_attention for s in states)
                return _Outcome(custody=SourceCustody(
                    command.actor, command.expected_entity_token,
                    "unavailable" if pending else "held" if states else "clear",
                    tuple(s.operation_id for s in states),
                    "overridden" if overridden else
                    "pending-intent" if pending else "durable-custody" if states else "no-record",
                    revision,
                    holds=tuple(SourceHold(s.operation_id, s.generation) for s in states),
                    overridden=overridden,
                ))
            except Exception as error:
                return _Outcome(custody=SourceCustody(
                    command.actor, command.expected_entity_token,
                    reason=f"journal-query:{type(error).__name__}",
                ))
        try:
            match command.operation:
                case Operation.PREPARE:
                    result = self._store.prepare(command.operation_id, command.binding)
                case Operation.GET:
                    result = self._store.get(command.operation_id)
                case Operation.REQUIRE_LIVE:
                    result = self._store.require_live(command.operation_id)
                case Operation.RENEW:
                    result = self._store.renew(command.operation_id,
                        expected_expiry=command.expected_expiry, expires_at_ms=command.expires_at_ms)
                case Operation.REVOKE:
                    result = self._store.revoke(command.operation_id)
                case Operation.CANCEL_PREPARED:
                    result = self._store.cancel_prepared(command.operation_id)
            return _Outcome(result)
        except AdmissionConflict as error:
            return _Outcome(rejected=error.reason)

    def close(self, *, timeout: float) -> bool:
        """Drain before stopping the mailbox; False retains completion custody.

        The caller supplies the teardown budget. A failed drain is not permission
        to discard pending writes; a later call may finish the same drain.
        """
        deadline = time.monotonic() + max(0.0, timeout)
        with self._admission:
            if self._closing is None:
                drained: Future = Future()
                admitted = self._runtime.tell(self._handle, _Drain(drained))
                if admitted is not AdmissionResult.ACCEPTED:
                    return False
                self._closing = drained
            drained = self._closing
        try:
            drained.result(max(0.0, deadline - time.monotonic()))
        except (TimeoutError, RuntimeError):
            return False
        if not self._effects.close(max(0.0, deadline - time.monotonic())):
            return False
        return self._runtime.stop(self._handle, max(0.0, deadline - time.monotonic()))
