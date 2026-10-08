from __future__ import annotations
import threading
import time
import sqlite3
from collections.abc import Callable
from pathlib import Path
from hyprial.kernel import (
    STATE_AUTHORITY,
    ActorHandle,
    ActorRuntime,
    ActorSpec,
    ActorState,
    AdmissionResult,
    DrainReport,
)
from hyprial.kernel import PortAdmission, PortCommandRejected
from hyprial.daemon.impl.inbox.contracts.api  import (
    DeliveryTransport,
    InboxMessage,
)
from hyprial.daemon.impl.inbox.contracts.ports  import (
    CloseInboxCommand,
    InboxCommand,
    InboxCountsProjection,
    InboxEventSink,
    InboxProjectionPort,
    WakeOutboxRecipientCommand,
)
from hyprial.daemon.impl.inbox.service.policy  import RetryPolicy


from .custody import DeliveryCustody
from .errors import InboxAuthorityTimeout, InboxAuthorityUnavailable
from .events import (
    CompletionReceiptClaim,
    CompletionReceiptState,
    DispatchIoCompleted,
    DispatchIoFailed,
    DispatchIoKind,
    DispatchIoRequested,
    ReassociateIoCompletion,
)
from .internal import _CompletionReceipts, _DurableCompletionHandoffs, _EventFanout, _ProjectionState
from .worker import DeliveryIoWorker

class DeliveryCustodyCoordinator(InboxProjectionPort):
    """Bounded, event-only command admission for the delivery authority."""

    def __init__(
        self,
        database: Path,
        transport: DeliveryTransport,
        event_sink: InboxEventSink,
        *,
        runtime: ActorRuntime | None = None,
        mailbox_capacity: int = 128,
        io_capacity: int = 32,
        completion_deadline: float = 1.0,
        completion_backoff: tuple[float, ...] = (0.005, 0.01, 0.02, 0.05),
        completion_receipt_timeout: float = 0.1,
        retry_policy: RetryPolicy | None = None,
        max_inbox_items: int = 10_000,
        max_custody_bytes: int = 1 << 30,
        node_id: str = "local",
        service_options: dict[str, object] | None = None,
    ) -> None:
        if completion_receipt_timeout <= 0:
            raise ValueError("completion receipt timeout must be positive")
        self._runtime = runtime or ActorRuntime()
        self._events = _EventFanout(event_sink)
        self._projection = _ProjectionState()
        self._lifecycle_lock = threading.Lock()
        self._closing = False
        self._factory_generation = 0
        self._active_handler: DeliveryCustody | None = None
        self._database = database
        self._handoffs = _DurableCompletionHandoffs(io_capacity + 1)
        self._receipts = _CompletionReceipts(mailbox_capacity + io_capacity * 8)
        self._completion_receipt_timeout = completion_receipt_timeout
        options = dict(service_options or {})
        alarm_human_delivery = options.pop("alarm_human_delivery", None)
        if alarm_human_delivery is not None and not callable(alarm_human_delivery):
            raise TypeError("alarm_human_delivery must be callable")
        self._io_worker = DeliveryIoWorker(
            transport,
            self._on_io_completion,
            self._record_durable_handoff,
            capacity=io_capacity,
            alarm_human_delivery=alarm_human_delivery,
            node_id=node_id,
            completion_deadline=completion_deadline,
            completion_backoff=completion_backoff,
        )

        def factory() -> Callable[[object], None]:
            with self._lifecycle_lock:
                self._factory_generation += 1
                generation = self._factory_generation
            handler = DeliveryCustody(
                database,
                self._events,
                self._projection,
                self._io_worker,
                self._handoffs,
                self._receipts,
                generation=generation,
                retry_policy=retry_policy,
                max_inbox_items=max_inbox_items,
                max_custody_bytes=max_custody_bytes,
                node_id=node_id,
                service_options=options,
            )
            with self._lifecycle_lock:
                self._active_handler = handler
            return handler

        self._handle: ActorHandle = self._runtime.start(
            ActorSpec(
                name="delivery-custody",
                handler_factory=factory,
                mailbox_capacity=mailbox_capacity,
                supervision_profile=STATE_AUTHORITY,
                undelivered_sink=self._undelivered,
            )
        )

    def _undelivered(self, command: object, reason: str) -> None:
        if not isinstance(command, WakeOutboxRecipientCommand):
            return
        self._events.publish(
            PortCommandRejected(
                correlation_id=command.correlation_id,
                domain="inbox",
                generation=self._runtime.snapshot(self._handle).generation,
                version=self._projection.version,
                code=reason,
                detail="accepted recipient wake never began",
                admission=PortAdmission.CLOSING,
            )
        )

    @property
    def handle(self) -> ActorHandle:
        return self._handle

    @property
    def generation(self) -> int:
        return self._runtime.snapshot(self._handle).generation

    @property
    def version(self) -> int:
        return self._projection.version

    def wait_for_version(self, version: int, timeout: float) -> int:
        return self._projection.wait_for_version(version, timeout)

    def submit(self, command: InboxCommand) -> PortAdmission:
        rejection: tuple[PortAdmission, str, str] | None = None
        with self._lifecycle_lock:
            if self._closing:
                rejection = (
                    PortAdmission.CLOSING,
                    "PORT_CLOSING",
                    "delivery coordinator is closing",
                )
            else:
                admission = self._runtime.tell(self._handle, command)
                if admission is AdmissionResult.ACCEPTED:
                    if isinstance(command, CloseInboxCommand):
                        self._closing = True
                    return PortAdmission.ACCEPTED
                if admission is AdmissionResult.OVERLOADED:
                    rejection = (
                        PortAdmission.OVERLOADED,
                        "PORT_OVERLOADED",
                        "delivery actor mailbox capacity is exhausted",
                    )
                else:
                    # CLOSED means "the current generation is not accepting",
                    # which a one-for-one restart produces transiently while
                    # the guardian rebuilds the handler (D12/D13).  Latching
                    # the port on that would make every supervised restart
                    # permanently unrecoverable, so only a terminal guardian
                    # state closes it for good.
                    terminal = self._runtime.snapshot(self._handle).state in {
                        ActorState.QUARANTINED,
                        ActorState.STOPPED,
                    }
                    self._closing = terminal
                    rejection = (
                        PortAdmission.CLOSING,
                        "PORT_CLOSING",
                        (
                            "delivery actor is not accepting commands"
                            if terminal
                            else "delivery actor is restarting"
                        ),
                    )
        assert rejection is not None
        mapped, code, detail = rejection
        self._publish_admission_rejection(
            command,
            admission=mapped,
            code=code,
            detail=detail,
        )
        return mapped

    def call(
        self,
        command: InboxCommand,
        expected: type[object],
        *,
        timeout: float,
    ) -> object:
        """Bounded synchronous wait permitted only at system protocol edges."""

        reply = self._events.expect(command.correlation_id, expected)
        started = time.monotonic()
        admission = self.submit(command)
        if admission is not PortAdmission.ACCEPTED:
            self._events.cancel(command.correlation_id)
            raise InboxAuthorityUnavailable(
                f"inbox command admission failed: {admission.value}"
            )
        try:
            event = reply.wait(timeout)
        except InboxAuthorityTimeout as exc:
            # Say what the actor was doing when the reply deadline passed:
            # busy with earlier commands, idle with this one still queued, or
            # a waiter that itself woke far past its deadline.
            raise InboxAuthorityTimeout(
                f"{exc}; {self._timeout_evidence(started, timeout)}"
            ) from exc
        finally:
            self._events.cancel(command.correlation_id)
        if isinstance(event, PortCommandRejected):
            if event.code == "MESSAGE_NOT_FOUND":
                raise KeyError(event.detail)
            raise InboxAuthorityUnavailable(f"{event.code}: {event.detail}")
        return event

    def _timeout_evidence(self, started: float, timeout: float) -> str:
        waited = time.monotonic() - started
        try:
            snapshot = self._runtime.snapshot(self._handle)
        except Exception as exc:  # noqa: BLE001 - evidence must not mask the timeout
            return f"waited {waited:.3f}s of {timeout:.3f}s; actor snapshot failed: {exc!r}"
        return (
            f"waited {waited:.3f}s of {timeout:.3f}s; actor state={snapshot.state.value} "
            f"generation={snapshot.generation} queued={snapshot.queued} "
            f"in_flight={snapshot.in_flight} failures={snapshot.failures_in_window}"
        )

    def read_counts(self) -> InboxCountsProjection:
        return self._projection.read_counts()

    def read_pending(self, recipient: str) -> tuple[InboxMessage, ...]:
        return self._projection.read_pending(recipient)

    def has_pending_work(self, recipient: str) -> bool:
        return self._projection.has_pending_work(recipient)

    def read_claimed_message_ids(self) -> frozenset[str]:
        return self._projection.read_claimed_message_ids()

    def drain(self, timeout: float = 5.0) -> DrainReport:
        """Drain state, then I/O, then fenced completions for this domain only."""

        started = time.monotonic()
        deadline = started + max(0.0, timeout)
        with self._lifecycle_lock:
            self._closing = True

        if not self._wait_state_idle(deadline):
            return DrainReport(
                complete=False,
                elapsed=time.monotonic() - started,
                remaining=(self._handle,),
            )
        if not self._io_worker.drain(max(0.0, deadline - time.monotonic())):
            return DrainReport(
                complete=False,
                elapsed=time.monotonic() - started,
                remaining=(self._handle,),
            )
        if not self._wait_state_idle(deadline):
            return DrainReport(
                complete=False,
                elapsed=time.monotonic() - started,
                remaining=(self._handle,),
            )
        complete = False
        complete = self._runtime.stop(
            self._handle,
            timeout=max(0.0, deadline - time.monotonic()),
        )
        if complete:
            with self._lifecycle_lock:
                handler = self._active_handler
            if handler is not None:
                handler.close_storage()
            self._handoffs.consume_all()
        return DrainReport(
            complete=complete,
            elapsed=time.monotonic() - started,
            remaining=() if complete else (self._handle,),
        )

    def _wait_state_idle(self, deadline: float) -> bool:
        while time.monotonic() < deadline:
            snapshot = self._runtime.snapshot(self._handle)
            if snapshot.queued == 0 and snapshot.in_flight == 0:
                return True
            time.sleep(min(0.005, max(0.0, deadline - time.monotonic())))
        snapshot = self._runtime.snapshot(self._handle)
        return snapshot.queued == 0 and snapshot.in_flight == 0

    def _on_io_completion(
        self,
        request: DispatchIoRequested,
        event: DispatchIoCompleted | DispatchIoFailed,
    ) -> AdmissionResult:
        generation = self._runtime.snapshot(self._handle).generation
        if generation != event.generation:
            if (
                request.kind is DispatchIoKind.DELIVERY
                and request.completion_kind != "submit"
            ):
                return AdmissionResult.CLOSED
            command: object = ReassociateIoCompletion(
                correlation_id=event.correlation_id,
                generation=generation,
                version=self._projection.version,
                request=request,
                completion=event,
            )
        else:
            command = event
        if not event.receipt_token:
            return AdmissionResult.CLOSED
        claim = self._receipts.claim(event.receipt_token, generation)
        if claim is CompletionReceiptClaim.FULL:
            return AdmissionResult.OVERLOADED
        if claim is CompletionReceiptClaim.SETTLED:
            return AdmissionResult.ACCEPTED
        if claim is CompletionReceiptClaim.NEW:
            admission = self._runtime.tell(self._handle, command)
            if admission is not AdmissionResult.ACCEPTED:
                self._receipts.acknowledge(
                    event.receipt_token,
                    CompletionReceiptState.RETRY,
                )
                return admission
        receipt = self._receipts.wait(
            event.receipt_token,
            self._completion_receipt_timeout,
        )
        if receipt is CompletionReceiptState.SETTLED:
            return AdmissionResult.ACCEPTED
        if receipt is CompletionReceiptState.RETRY:
            return AdmissionResult.CLOSED
        return AdmissionResult.OVERLOADED

    def _record_durable_handoff(
        self,
        request: DispatchIoRequested,
        completion: DispatchIoCompleted | DispatchIoFailed,
        reason: str,
    ) -> bool:
        if request.kind is not DispatchIoKind.DELIVERY:
            return False
        outbox_ids = tuple(
            item.message.message_id
            for item in request.items
            if not item.custody_retry
        )
        custody_ids = tuple(
            item.message.message_id
            for item in request.items
            if item.custody_retry
        )
        if not self._table_holds_all("outbox", outbox_ids):
            return False
        if not self._table_holds_all("custody", custody_ids):
            return False
        if not self._handoffs.record(request.correlation_id):
            return False
        try:
            self._events.publish(
                DispatchIoFailed(
                    correlation_id=completion.correlation_id,
                    generation=completion.generation,
                    version=completion.version,
                    code="COMPLETION_DURABLY_HANDED_OFF",
                    detail=reason,
                )
            )
        except Exception:
            pass
        if request.receipt_token:
            self._receipts.acknowledge(
                request.receipt_token,
                CompletionReceiptState.SETTLED,
            )
        return True

    def _table_holds_all(self, table: str, message_ids: tuple[str, ...]) -> bool:
        if not message_ids:
            return True
        if table not in {"outbox", "custody"}:
            raise ValueError(f"unsupported durable handoff table: {table}")
        try:
            connection = sqlite3.connect(
                f"{self._database.resolve().as_uri()}?mode=ro",
                uri=True,
                timeout=0.05,
            )
            try:
                placeholders = ",".join("?" for _ in message_ids)
                rows = connection.execute(
                    f"SELECT message_id FROM {table} "
                    f"WHERE message_id IN ({placeholders})",
                    message_ids,
                ).fetchall()
            finally:
                connection.close()
        except sqlite3.Error:
            return False
        held = {str(row[0]) for row in rows}
        return all(message_id in held for message_id in message_ids)

    def _publish_admission_rejection(
        self,
        command: InboxCommand,
        *,
        admission: PortAdmission,
        code: str,
        detail: str,
    ) -> None:
        self._events.publish(
            PortCommandRejected(
                correlation_id=command.correlation_id,
                domain="inbox",
                generation=self._runtime.snapshot(self._handle).generation,
                version=self._projection.version,
                code=code,
                detail=detail,
                admission=admission,
            )
        )
