"""Forward settlement: outcomes, requests, the ForwardSettlementCoordinator and its inflight records."""

from __future__ import annotations
import json
import threading
import time
from dataclasses import dataclass, replace
from typing import Callable, Protocol
from uuid import uuid4
from hyprial.daemon.impl.inbox import InboxAuthorityUnavailable, InboxMessage
from hyprial.daemon.impl.inbox.tracking.progress import COALESCE_KEPT_PHASES, ProgressEvent
from hyprial.daemon import (
    AttemptIdentity,
)
from hyprial.daemon.impl.api  import (
    HarnessResult,
)
from hyprial.kernel import ActorRuntime, ActorSpec, AdmissionResult
from hyprial.kernel import EffectCompleted, EffectLane, EffectRequest
from hyprial.kernel import DEFAULT_POLICIES, EXTERNAL_IO

_RETRY_PUMP_IDLE_SECONDS = DEFAULT_POLICIES[EXTERNAL_IO].max_backoff
_RETRY_PUMP_RETRY_FLOOR_SECONDS = DEFAULT_POLICIES[EXTERNAL_IO].base_backoff
_RETRY_PUMP_JOIN_SECONDS = 3.0
_RETRY_PUMP_SLOW_MS = 1000
HARNESS_FAILURE_MAX_ATTEMPTS = 3


HARNESS_FAILURE_BACKOFF_MS = (1_000, 5_000)


BLOCKING_FAILURE_CUSTODY_CAPACITY = 10_000


FORWARD_UNAVAILABLE = "FORWARD_UNAVAILABLE"


def _retry_pump_wait_seconds(inbox: object, *, now_ms: int) -> float:
    """Wait until the next durable due key, capped by the policy ceiling."""

    due_reader = getattr(inbox, "next_retry_due_ms", None)
    due_ms = due_reader() if callable(due_reader) else None
    if due_ms is None:
        return _RETRY_PUMP_IDLE_SECONDS
    return min(_RETRY_PUMP_IDLE_SECONDS, max(0.0, (due_ms - now_ms) / 1_000))


@dataclass(frozen=True, slots=True)
class ForwardOutcome:
    """What sending one forward AS the worker produced.

    ``accepted`` means the send boundary took the message (queued for an
    agent, or posted for a route).  Otherwise ``failure_code`` is the stable
    code the delivery is settled with.
    """

    accepted: bool
    failure_code: str | None = None
    error: str | None = None


Forwarder = Callable[[InboxMessage, str, str], ForwardOutcome]


@dataclass(frozen=True, slots=True)
class ForwardRequest:
    operation_id: str
    generation: int
    decision_token: str
    original: InboxMessage
    result: HarnessResult
    attempt: _InflightAttempt | None


@dataclass(frozen=True, slots=True)
class ForwardCompleted:
    operation_id: str
    generation: int
    decision_token: str
    delivery_id: str
    outcome: ForwardOutcome
    attempt: _InflightAttempt | None


@dataclass(frozen=True, slots=True)
class RetireForwardDecision:
    operation_id: str
    generation: int
    delivery_id: str
    decision_token: str


class _ForwardControlReply:
    def __init__(self) -> None:
        self.ready = threading.Event()
        self.value: bool | None = None


@dataclass(frozen=True, slots=True)
class ForwardSettlementProjection:
    active: int
    decisions: int
    completed: int
    overloaded: int
    closing: bool


class ForwardSettlementCoordinator:
    """Run ordered forwarding effects outside terminal result settlement.

    A Harness claim remains durable while an accepted forward is active.  The
    completed decision stays in this owner until inbox settlement confirms it,
    so an ACK timeout retries only settlement rather than the native send.
    """

    def __init__(
        self,
        forward: Forwarder,
        *,
        capacity: int = 64,
        runtime: ActorRuntime | None = None,
    ) -> None:
        if capacity < 1:
            raise ValueError("forward capacity must be positive")
        self._forward = forward
        self._capacity = capacity
        self._runtime = runtime or ActorRuntime()
        self._guard = threading.Lock()
        self._generation = 1
        self._active: dict[str, ForwardRequest] = {}
        self._active_operations: dict[tuple[str, int], ForwardRequest] = {}
        self._completed: dict[str, ForwardCompleted] = {}
        self._control_replies: dict[str, _ForwardControlReply] = {}
        self._closing = self._closed = False
        self._completed_count = self._overloaded = 0
        self._handle = self._runtime.start(
            ActorSpec(
                name="harness-forward-settlement",
                handler_factory=lambda: self._receive,
                mailbox_capacity=capacity,
            )
        )
        # One worker preserves the order in which Harness turns finished.
        self._effects: EffectLane[ForwardRequest, ForwardCompleted] = EffectLane(
            name="harness-forward-io",
            execute=self._execute,
            complete=lambda event: self._runtime.tell(self._handle, event),
            capacity=capacity,
        )

    def submit(
        self,
        original: InboxMessage,
        result: HarnessResult,
        attempt: _InflightAttempt | None,
    ) -> AdmissionResult:
        delivery_id = result.delivery_id
        with self._guard:
            if self._closing:
                return AdmissionResult.CLOSED
            if delivery_id in self._active or delivery_id in self._completed:
                return AdmissionResult.ACCEPTED
            if len(self._active) + len(self._completed) >= self._capacity:
                self._overloaded += 1
                return AdmissionResult.OVERLOADED
            request = ForwardRequest(
                uuid4().hex,
                self._generation,
                uuid4().hex,
                original,
                result,
                attempt,
            )
            self._active[delivery_id] = request
            self._active_operations[
                (request.operation_id, request.generation)
            ] = request
            admitted = self._runtime.tell(self._handle, request)
            if admitted is not AdmissionResult.ACCEPTED:
                self._active.pop(delivery_id, None)
                self._active_operations.pop(
                    (request.operation_id, request.generation), None
                )
                self._overloaded += 1
            return admitted

    def completed(self, delivery_id: str) -> ForwardCompleted | None:
        with self._guard:
            return self._completed.get(delivery_id)

    def retire(
        self, delivery_id: str, decision_token: str, *, timeout: float = 5.0
    ) -> bool:
        command = RetireForwardDecision(
            uuid4().hex, self._generation, delivery_id, decision_token
        )
        reply = _ForwardControlReply()
        with self._guard:
            if self._closed or len(self._control_replies) >= self._capacity:
                raise TimeoutError("forward retirement authority unavailable")
            self._control_replies[command.operation_id] = reply
            admitted = self._runtime.tell(self._handle, command)
            if admitted is not AdmissionResult.ACCEPTED:
                self._control_replies.pop(command.operation_id, None)
                raise TimeoutError(f"forward retirement admission {admitted.value}")
        if not reply.ready.wait(max(0.0, timeout)):
            raise TimeoutError(
                f"forward retirement {command.operation_id} remains accepted"
            )
        return bool(reply.value)

    def projection(self) -> ForwardSettlementProjection:
        with self._guard:
            return ForwardSettlementProjection(
                len(self._active), len(self._completed), self._completed_count,
                self._overloaded, self._closing,
            )

    def close(self, timeout: float = 5.0) -> bool:
        deadline = time.monotonic() + max(0.0, timeout)
        with self._guard:
            if self._closed:
                return True
            self._closing = True
        while True:
            with self._guard:
                pending = bool(self._active or self._control_replies)
            if not pending:
                break
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.005)
        if not self._effects.close(max(0.0, deadline - time.monotonic())):
            return False
        stopped = self._runtime.stop(
            self._handle, max(0.0, deadline - time.monotonic())
        )
        with self._guard:
            self._closed = stopped
            if stopped:
                self._generation += 1
        return stopped

    def _execute(self, request: ForwardRequest) -> ForwardCompleted:
        result = request.result
        assert result.forward_to is not None
        try:
            outcome = self._forward(
                request.original, result.forward_to, result.output
            )
        except Exception as error:  # noqa: BLE001 - one send never kills the lane
            outcome = ForwardOutcome(
                False,
                "HARNESS_TRANSIENT_FAILURE",
                f"forward raised {type(error).__name__}",
            )
        return ForwardCompleted(
            request.operation_id,
            request.generation,
            request.decision_token,
            result.delivery_id,
            outcome,
            request.attempt,
        )

    def _receive(self, command: object) -> None:
        if isinstance(command, ForwardRequest):
            admitted = self._effects.submit(
                EffectRequest(command.operation_id, command.generation, command)
            )
            if admitted is not AdmissionResult.ACCEPTED:
                with self._guard:
                    incumbent = self._active.get(command.result.delivery_id)
                    if incumbent == command:
                        self._active.pop(command.result.delivery_id, None)
                        self._active_operations.pop(
                            (command.operation_id, command.generation), None
                        )
                    self._overloaded += 1
            return
        if isinstance(command, RetireForwardDecision):
            with self._guard:
                completed = self._completed.get(command.delivery_id)
                retired = bool(
                    command.generation == self._generation
                    and completed is not None
                    and completed.decision_token == command.decision_token
                )
                if retired:
                    self._completed.pop(command.delivery_id, None)
                reply = self._control_replies.pop(command.operation_id, None)
                if reply is not None:
                    reply.value = retired
                    reply.ready.set()
            return
        if isinstance(command, EffectCompleted):
            result = command.result
            with self._guard:
                request = self._active_operations.pop(
                    (command.operation_id, command.generation), None
                )
                if request is not None:
                    incumbent = self._active.get(request.result.delivery_id)
                    if incumbent == request:
                        self._active.pop(request.result.delivery_id, None)
                    if not (
                        isinstance(result, ForwardCompleted)
                        and result.operation_id == request.operation_id
                        and result.generation == request.generation
                        and result.decision_token == request.decision_token
                        and result.delivery_id == request.result.delivery_id
                        and isinstance(result.outcome, ForwardOutcome)
                    ):
                        result = ForwardCompleted(
                            request.operation_id,
                            request.generation,
                            request.decision_token,
                            request.result.delivery_id,
                            ForwardOutcome(
                                False,
                                "HARNESS_TRANSIENT_FAILURE",
                                "forward effect failed: "
                                f"{command.error or 'invalid completion'}",
                            ),
                            request.attempt,
                        )
                    self._completed[result.delivery_id] = result
                    self._completed_count += 1
            self._effects.acknowledge(command.operation_id, command.generation)
            return
        raise TypeError(f"unsupported forward command {type(command).__name__}")


@dataclass(frozen=True, slots=True)
class _InflightAttempt:
    """One request currently handed to (or waiting for) a worker.

    This is the clock for the no-event path: the deadline runs from the
    moment the request entered the worker's queue, and the *only* thing that
    resets it is a progress observation correlated with this delivery.  A
    process being alive, online, or reconnecting is not progress and does not
    reset it (see ``_publish_harness_progress``).
    """

    identity: AttemptIdentity
    agent_entity_token: str | None
    budget_ms: int
    last_progress_ms: int
    reported: bool = False


@dataclass(frozen=True, slots=True)
class _QueuedDeliveryHold:
    """Last hold refresh for one delivery accepted by a live worker queue."""

    worker: str
    refreshed_at_ms: int


class HarnessActorRegistration(Protocol):
    """Handle owning one harness actor's liveliness and inbox endpoint.

    ``healthy`` is the reconciliation lease: it becomes false once this
    handle, or any child route it owns, can no longer serve the actor.
    """

    @property
    def healthy(self) -> bool: ...

    def close(
        self,
        *,
        reason: str = "unspecified",
        initiator: str = "external-caller",
    ) -> None: ...


def _notice_kind(message: InboxMessage) -> str | None:
    """The ``notification`` kind of a fail-loud notice, for its log line."""

    try:
        body = json.loads(message.payload)
    except (TypeError, ValueError):
        return None
    kind = body.get("notification") if isinstance(body, dict) else None
    return kind if isinstance(kind, str) else None


def _notice_text(message: InboxMessage) -> str:
    """The human-readable ``message`` of a fail-loud notice (else the raw body)."""

    try:
        body = json.loads(message.payload)
    except (TypeError, ValueError):
        return str(message.payload)
    text = body.get("message") if isinstance(body, dict) else None
    return text if isinstance(text, str) else json.dumps(body, ensure_ascii=False)


_OWNER_HIDDEN_NOTICE_LABELS = ("- worker:", "- 静默预算:", "- attempt:")


def _owner_facing_notice_text(message: InboxMessage) -> str:
    """The notice text with its operator-only fields dropped."""

    return "\n".join(
        line
        for line in _notice_text(message).splitlines()
        if not line.startswith(_OWNER_HIDDEN_NOTICE_LABELS)
    )


def _stale_fence_rejection(error: BaseException) -> bool:
    """A ``retry_due`` command whose generation/version fence moved under it.

    The authority client reads the fence, then submits; since retry_due left
    the tick thread, the tick's own inbox commands can commit a version in
    that window and the actor rejects the stale fence as
    ``InboxAuthorityUnavailable("STALE_COMMAND: ...")``.  That is the
    expected loser of a race the two threads are supposed to run, not a
    fault: the pass yields and the next kick re-reads the fence.  Only the
    ``STALE_COMMAND`` raise shape matches here -- the admission-failure
    shape (``inbox command admission failed: ...``) stays a pump failure.
    """

    return isinstance(error, InboxAuthorityUnavailable) and str(error).startswith(
        "STALE_COMMAND"
    )


def _reply_already_answered(error: BaseException) -> bool:
    """A reply submit refused because this delivery's reply is already durable.

    ``_reply_and_ack`` derives the reply id from the delivery it answers, so
    the authority's ``SUBMISSION_RECEIPT_CONFLICT`` on it means a reply with
    different text already committed (typically: the first submit timed out
    after the reply was sent, and a re-run turn answered again).  Same raise
    shape as ``_stale_fence_rejection``.
    """

    return isinstance(error, InboxAuthorityUnavailable) and str(error).startswith(
        "SUBMISSION_RECEIPT_CONFLICT"
    )


def _coalesce_progress_events(
    events: tuple[ProgressEvent, ...],
) -> tuple[ProgressEvent, ...]:
    """Keep the newest tool-call, tool-result, and other event per delivery.

    Coalesced events count as drops, and their own ``dropped_since_seq``
    accounting carries forward, so consumers can still tell exactly how much
    producer-side information was elided during this tick.
    """

    grouped: dict[tuple[str, str], list[ProgressEvent]] = {}
    for event in events:
        grouped.setdefault((event.actor, event.delivery_id), []).append(event)
    output: list[ProgressEvent] = []
    for group in grouped.values():
        retained_by_bucket: dict[str, ProgressEvent] = {}
        for event in group:
            bucket = event.phase if event.phase in COALESCE_KEPT_PHASES else "other"
            retained_by_bucket[bucket] = event
        retained = sorted(retained_by_bucket.values(), key=lambda item: item.seq)
        retained_ids = {id(item) for item in retained}
        merged = [item for item in group if id(item) not in retained_ids]
        merged_count = len(merged) + sum(item.dropped_since_seq for item in merged)
        if retained and merged_count:
            retained[0] = replace(
                retained[0],
                dropped_since_seq=retained[0].dropped_since_seq + merged_count,
            )
        output.extend(retained)
    return tuple(output)
