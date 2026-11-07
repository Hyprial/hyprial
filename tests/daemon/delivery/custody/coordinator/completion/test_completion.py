from __future__ import annotations

import queue
import sys
import threading
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
import pytest
from hyprial.kernel.impl.actor_runtime import AdmissionResult, ActorState
from hyprial.kernel.impl.actor_runtime.scheduler import GenerationScheduler
from hyprial.kernel.impl.contracts.ports import PortAdmission, PortCommandRejected
from hyprial.daemon.impl.adapters.lark.contracts.reply_bridge import LarkReplyBridgeTransport
from hyprial.daemon.impl.inbox.actor import DeliveryCustodyCoordinator, DispatchIoCompleted, DispatchIoFailed, DispatchIoRequested, DispatchOutcome, DispatchOutcomeKind, InboxAuthorityTimeout
from hyprial.daemon.impl.inbox.authority import DeliveryCustodyFacade
from hyprial.daemon.impl.inbox.memory import MemoryDeliveryTransport
from hyprial.daemon.impl.inbox.contracts.ports import ReceiveMessageCommand, RetryDueCommand, RetryIoCompleted, SubmissionCompleted, SubmitMessageCommand

from ..support import (
    _BlockingTransport,
    _CompletionAdmissionRuntime,
    _Events,
    _OnlineTransport,
    _UNBUDGETED_RESTART_WAIT_SECONDS,
    _close,
    _message,
    _runtime,
    _wait_until,
)


def test_completion_overload_retries_without_repeating_transport(
    tmp_path: Path,
) -> None:
    events = _Events()
    runtime = _CompletionAdmissionRuntime(overloaded_before_accept=3)
    transport = _OnlineTransport()
    coordinator = DeliveryCustodyCoordinator(
        tmp_path / "inbox.sqlite3",
        transport,
        events,
        runtime=runtime,
        completion_deadline=0.5,
        completion_backoff=(0.001,),
    )
    assert coordinator.submit(SubmitMessageCommand("completion-retry", _message())) is (
        PortAdmission.ACCEPTED
    )
    _wait_until(
        lambda: any(
            isinstance(item, SubmissionCompleted)
            and item.correlation_id == "completion-retry"
            for item in events.snapshot()
        )
    )
    completed = [
        item
        for item in events.snapshot()
        if isinstance(item, SubmissionCompleted)
        and item.correlation_id == "completion-retry"
    ]
    assert runtime.completion_attempts == 4
    assert transport.deliveries == ["m1"]
    assert len(completed) == 1
    assert coordinator.read_counts().outbox_count == 0
    _close(coordinator, events)


def test_closed_completion_hands_back_durable_outbox_for_new_generation_retry(
    tmp_path: Path,
) -> None:
    # This tests durable generation takeover, not the wall-clock time taken
    # by a Timer thread to reopen SQLite under CI load. Execute the real
    # generation-fenced callback after observing its admission to the scheduler.
    scheduled: queue.Queue[Callable[[], None]] = queue.Queue()

    class RestartTimer:
        def __init__(self, _delay, callback, args):
            self.callback = callback
            self.args = args
            self.cancelled = False

        def start(self):
            scheduled.put(self.fire)

        def cancel(self):
            self.cancelled = True

        def fire(self):
            assert not self.cancelled, "restart was unexpectedly cancelled"
            self.callback(*self.args)

    events = _Events()
    runtime = _CompletionAdmissionRuntime(
        closed=True, scheduler=GenerationScheduler(timer_factory=RestartTimer),
    )
    transport = _OnlineTransport()
    coordinator = DeliveryCustodyCoordinator(
        tmp_path / "inbox.sqlite3",
        transport,
        events,
        runtime=runtime,
        completion_deadline=0.05,
        completion_backoff=(0.001,),
    )
    try:
        assert coordinator.submit(
            SubmitMessageCommand("closed-completion", _message(), now_ms=10)
        ) is PortAdmission.ACCEPTED
        _wait_until(
            lambda: any(
                isinstance(item, DispatchIoFailed)
                and item.correlation_id == "closed-completion"
                and item.code == "COMPLETION_DURABLY_HANDED_OFF"
                for item in events.snapshot()
            )
        )
        assert transport.deliveries == ["m1"]
        assert coordinator.read_counts().outbox_count == 1
        assert not any(
            isinstance(item, SubmissionCompleted)
            and item.correlation_id == "closed-completion"
            for item in events.snapshot()
        )

        assert runtime.tell(coordinator.handle, object()) is AdmissionResult.ACCEPTED
        scheduled.get(timeout=5.0)()
        # Record every (state, generation) the wait observes, so a timeout says
        # which of two identical-looking reds it was (#549 unit 11807): a slow
        # restart ends on RESTART_PENDING/2, a failed restart attempt moves past
        # generation 2 and never matches.  Diagnostics only; the budget is unchanged.
        trace: list[tuple[str, int]] = []

        def restarted_as_generation_two() -> bool:
            snapshot = runtime.snapshot(coordinator.handle)
            cell = (snapshot.state.name, snapshot.generation)
            if not trace or trace[-1] != cell:
                trace.append(cell)
            return snapshot.state is ActorState.RUNNING and snapshot.generation == 2

        def generation_trace() -> str:
            snapshot = runtime.snapshot(coordinator.handle)
            return (
                f"last state={snapshot.state.name} generation={snapshot.generation}; "
                f"observed={trace}"
            )

        _wait_until(
            restarted_as_generation_two,
            timeout=_UNBUDGETED_RESTART_WAIT_SECONDS,
            describe=generation_trace,
        )
        runtime.allow_completions()
        assert coordinator.submit(
            RetryDueCommand(
                correlation_id="retry-after-handoff",
                generation=coordinator.generation,
                version=coordinator.version,
                now_ms=10,
            )
        ) is PortAdmission.ACCEPTED
        _wait_until(
            lambda: any(
                isinstance(item, RetryIoCompleted)
                and item.correlation_id == "retry-after-handoff"
                for item in events.snapshot()
            )
        )
        assert transport.deliveries == ["m1", "m1"]
        assert coordinator.read_counts().outbox_count == 0
    finally:
        runtime.allow_completions()
        assert coordinator.drain(5.0).complete is True


@pytest.mark.parametrize("failed_wait", [1, 2], ids=["handoff", "retry"])
def test_closed_completion_assertion_failure_still_releases_storage(
    tmp_path: Path, monkeypatch, failed_wait: int,
) -> None:
    wait = _wait_until
    waits = 0

    def fail_after_event(predicate, timeout=1.0, describe=None):
        nonlocal waits
        wait(predicate, timeout, describe)
        waits += 1
        if waits == failed_wait:
            raise AssertionError("injected custody assertion failure")

    monkeypatch.setattr(sys.modules[__name__], "_wait_until", fail_after_event)
    with pytest.raises(AssertionError, match="injected custody assertion failure"):
        test_closed_completion_hands_back_durable_outbox_for_new_generation_retry(tmp_path)
    assert waits == failed_wait


def test_drain_waits_for_completion_admission_or_durable_handoff(
    tmp_path: Path,
) -> None:
    events = _Events()
    runtime = _CompletionAdmissionRuntime(hold_overloaded=True)
    transport = _OnlineTransport()
    coordinator = DeliveryCustodyCoordinator(
        tmp_path / "inbox.sqlite3",
        transport,
        events,
        runtime=runtime,
        completion_deadline=1.0,
        completion_backoff=(0.01,),
    )
    assert coordinator.submit(SubmitMessageCommand("drain-custody", _message())) is (
        PortAdmission.ACCEPTED
    )
    # NONMONOTONIC-WAIT: a second delivery appends to the same list, making the equality false again -- list equality is not monotone
    _wait_until(lambda: transport.deliveries == ["m1"])
    report = coordinator.drain(0.05)
    assert report.complete is False
    assert report.remaining == (coordinator.handle,)
    assert coordinator.read_counts().outbox_count == 1
    assert not any(
        isinstance(item, SubmissionCompleted)
        and item.correlation_id == "drain-custody"
        for item in events.snapshot()
    )

    runtime.allow_completions()
    _wait_until(
        lambda: any(
            isinstance(item, SubmissionCompleted)
            and item.correlation_id == "drain-custody"
            for item in events.snapshot()
        )
    )
    assert coordinator.drain(1.0).complete is True


def test_completion_deadline_requires_explicit_durable_handoff(
    tmp_path: Path,
) -> None:
    events = _Events()
    runtime = _CompletionAdmissionRuntime(hold_overloaded=True)
    transport = _OnlineTransport()
    coordinator = DeliveryCustodyCoordinator(
        tmp_path / "inbox.sqlite3",
        transport,
        events,
        runtime=runtime,
        completion_deadline=0.02,
        completion_backoff=(0.001,),
    )
    assert coordinator.submit(SubmitMessageCommand("deadline", _message())) is (
        PortAdmission.ACCEPTED
    )
    _wait_until(
        lambda: any(
            isinstance(item, DispatchIoFailed)
            and item.correlation_id == "deadline"
            and item.code == "COMPLETION_DURABLY_HANDED_OFF"
            and item.detail == "completion-admission-deadline"
            for item in events.snapshot()
        )
    )
    assert runtime.completion_attempts > 1
    assert transport.deliveries == ["m1"]
    assert coordinator.read_counts().outbox_count == 1
    assert not any(
        isinstance(item, SubmissionCompleted)
        and item.correlation_id == "deadline"
        for item in events.snapshot()
    )
    runtime.allow_completions()
    _close(coordinator, events)


def test_crash_restart_reassociates_delivery_without_external_io_replay(
    tmp_path: Path,
) -> None:
    events = _Events()
    runtime = _runtime()
    transport = _BlockingTransport()
    coordinator = DeliveryCustodyCoordinator(
        tmp_path / "inbox.sqlite3", transport, events, runtime=runtime
    )
    assert coordinator.submit(
        SubmitMessageCommand("crash-submit", _message(), now_ms=10)
    ) is PortAdmission.ACCEPTED
    assert transport.entered.wait(1.0)
    assert coordinator.read_counts().outbox_count == 1
    assert runtime.tell(coordinator.handle, object()).value == "accepted"
    # NONMONOTONIC-WAIT: the state can leave RUNNING and the generation can advance to 3 -- a supervision restart, not a monotone record
    _wait_until(
        lambda: (
            runtime.snapshot(coordinator.handle).state is ActorState.RUNNING
            and runtime.snapshot(coordinator.handle).generation == 2
        ),
        # Supervision restart, not a custody settle -- see the constant.
        timeout=_UNBUDGETED_RESTART_WAIT_SECONDS,
    )
    assert coordinator.read_counts().outbox_count == 1
    transport.release.set()
    _wait_until(
        lambda: any(
            isinstance(item, SubmissionCompleted)
            and item.correlation_id == "crash-submit"
            for item in events.snapshot()
        )
    )
    assert transport.deliveries == ["m1"]
    assert coordinator.read_counts().outbox_count == 0
    assert not any(
        isinstance(item, DispatchIoFailed)
        and item.code == "COMPLETION_DURABLY_HANDED_OFF"
        for item in events.snapshot()
    )
    _close(coordinator, events)


def test_submit_completion_reassociates_after_restart_and_records_terminal(
    tmp_path: Path,
) -> None:
    """The E2E-009/E2E-013 shape: external delivery finishes after gen2 starts."""

    events = _Events()
    runtime = _runtime()
    class ReplyClient:
        def __init__(self) -> None:
            self.entered = threading.Event()
            self.release = threading.Event()
            self.deliveries: list[str] = []

        def reply_online(self, _adapter: str) -> bool:
            return True

        def deliver_reply(self, _adapter: str, delivery: object) -> bool:
            self.entered.set()
            assert self.release.wait(1.0)
            self.deliveries.append(str(getattr(delivery, "delivery_id")))
            return True

    reply_client = ReplyClient()
    transport = LarkReplyBridgeTransport(
        MemoryDeliveryTransport(),
        reply_client,
    )
    database = tmp_path / "inbox.sqlite3"
    coordinator = DeliveryCustodyCoordinator(
        database,
        transport,
        events,
        runtime=runtime,
    )
    facade = DeliveryCustodyFacade(coordinator, database, timeout=1.0)
    message = replace(
        _message("lark-reply"),
        recipient="adapter:lark:e2e",
        intent="reply",
        idempotency_key="reply:om-inbound",
    )
    result: list[object] = []
    errors: list[BaseException] = []

    def submit() -> None:
        try:
            result.append(facade.submit(message, now_ms=10))
        except BaseException as error:
            errors.append(error)

    caller = threading.Thread(target=submit)
    caller.start()
    assert reply_client.entered.wait(1.0)
    assert runtime.tell(coordinator.handle, object()) is AdmissionResult.ACCEPTED
    # NONMONOTONIC-WAIT: the state can leave RUNNING and the generation can advance to 3 -- a supervision restart, not a monotone record
    _wait_until(
        lambda: (
            runtime.snapshot(coordinator.handle).state is ActorState.RUNNING
            and runtime.snapshot(coordinator.handle).generation == 2
        ),
        # Supervision restart, not a custody settle -- see the constant.
        timeout=_UNBUDGETED_RESTART_WAIT_SECONDS,
    )
    reply_client.release.set()
    caller.join(2.0)

    assert not caller.is_alive()
    assert errors == []
    assert len(result) == 1
    assert reply_client.deliveries == ["lark-reply"]
    assert coordinator.read_counts().outbox_count == 0
    (terminal,) = facade.delivery_status.for_sender(
        message.sender,
        message_id=message.message_id,
    )
    assert terminal.state.value == "fetched"
    assert terminal.reason == "ACK_RECEIVED"
    assert not any(
        isinstance(item, DispatchIoFailed)
        and item.code == "COMPLETION_DURABLY_HANDED_OFF"
        for item in events.snapshot()
    )
    _close(coordinator, events)


def test_reply_timeout_restart_retry_joins_receipt_without_duplicate_io(
    tmp_path: Path,
) -> None:
    """A caller timeout never permits a second native reply or early ACK."""

    events = _Events()
    runtime = _runtime()

    class ReplyClient:
        def __init__(self) -> None:
            self.entered = threading.Event()
            self.release = threading.Event()
            self.deliveries: list[str] = []

        def reply_online(self, _adapter: str) -> bool:
            return True

        def deliver_reply(self, _adapter: str, delivery: object) -> bool:
            self.entered.set()
            assert self.release.wait(2.0)
            self.deliveries.append(str(getattr(delivery, "delivery_id")))
            return True

    client = ReplyClient()
    database = tmp_path / "inbox.sqlite3"
    coordinator = DeliveryCustodyCoordinator(
        database,
        LarkReplyBridgeTransport(MemoryDeliveryTransport(), client),
        events,
        runtime=runtime,
    )
    facade = DeliveryCustodyFacade(coordinator, database)
    # The caller timeout under test is induced with a facade that gives up
    # after 50 ms -- and ONLY the induced step uses it.  A single 50 ms
    # facade for the whole test made the recovery `submit`/`ack` below run
    # on the same budget, which scheduler jitter alone exceeds under the
    # 15-minute full suite (CI #1517: InboxAuthorityTimeout on inbox:ack,
    # card 2389ace2).  Two facades over one coordinator and one database
    # observe the same state.
    impatient = DeliveryCustodyFacade(coordinator, database, timeout=0.05)
    actor = "agent:e2e:node:squire"
    original = replace(
        _message("om-inbound"),
        sender="adapter:lark:e2e",
        recipient=actor,
        intent="request",
    )
    assert facade.receive(original, now_ms=10).acknowledged is True
    reply = replace(
        _message("lark-reply"),
        sender=actor,
        recipient="adapter:lark:e2e",
        intent="reply",
        idempotency_key="reply:om-inbound",
    )
    errors: list[BaseException] = []

    def first_submit() -> None:
        try:
            impatient.submit(reply, now_ms=10)
        except BaseException as error:
            errors.append(error)

    caller = threading.Thread(target=first_submit)
    caller.start()
    assert client.entered.wait(1.0)
    assert runtime.tell(coordinator.handle, object()) is AdmissionResult.ACCEPTED
    # NONMONOTONIC-WAIT: the state can leave RUNNING and the generation can advance to 3 -- a supervision restart, not a monotone record
    _wait_until(
        lambda: (
            runtime.snapshot(coordinator.handle).state is ActorState.RUNNING
            and runtime.snapshot(coordinator.handle).generation == 2
        ),
        # Supervision restart, not a custody settle -- see the constant.
        timeout=_UNBUDGETED_RESTART_WAIT_SECONDS,
    )
    caller.join(1.0)
    assert not caller.is_alive()
    assert len(errors) == 1 and isinstance(errors[0], InboxAuthorityTimeout)
    assert [item.message_id for item in facade.pending_messages(actor, now_ms=10)] == [
        original.message_id
    ]

    client.release.set()
    # NONMONOTONIC-WAIT: the delivery-status projection can change as new events arrive; it is not an append-only record
    _wait_until(
        lambda: bool(
            facade.delivery_status.for_sender(
                reply.sender,
                message_id=reply.message_id,
            )
        )
    )
    retried = facade.submit(reply, now_ms=10)
    assert retried.accepted is True and retried.queued is False
    assert client.deliveries == [reply.message_id]
    assert facade.ack(actor, original.message_id).acknowledged is True
    assert facade.pending_messages(actor, now_ms=10) == ()
    _close(coordinator, events)


def test_stale_dispatch_completion_does_not_mutate_claimed_outbox(
    tmp_path: Path,
) -> None:
    events = _Events()
    runtime = _runtime()
    transport = _BlockingTransport()
    coordinator = DeliveryCustodyCoordinator(
        tmp_path / "inbox.sqlite3", transport, events, runtime=runtime
    )
    assert coordinator.submit(
        SubmitMessageCommand("dispatch", _message(), now_ms=10)
    ) is PortAdmission.ACCEPTED
    assert transport.entered.wait(1.0)
    request = next(
        item for item in events.snapshot() if isinstance(item, DispatchIoRequested)
    )
    stale = DispatchIoCompleted(
        correlation_id=request.correlation_id,
        generation=request.generation,
        version=request.version + 1,
        outcomes=(
            DispatchOutcome(
                message_id="m1",
                kind=DispatchOutcomeKind.DELIVERED,
                recipient_online=True,
                direct_attempted=True,
            ),
        ),
        completed_at_ms=20,
    )
    assert runtime.tell(coordinator.handle, stale).value == "accepted"
    _wait_until(
        lambda: any(
            isinstance(item, PortCommandRejected)
            and item.code == "STALE_IO_COMPLETION"
            for item in events.snapshot()
        )
    )
    assert coordinator.read_counts().outbox_count == 1
    transport.release.set()
    _wait_until(
        lambda: any(
            isinstance(item, SubmissionCompleted)
            and item.correlation_id == "dispatch"
            for item in events.snapshot()
        )
    )
    assert coordinator.read_counts().outbox_count == 0
    _close(coordinator, events)


def test_restart_window_rejection_does_not_close_the_port(tmp_path: Path) -> None:
    """A one-for-one restart must not permanently close the ingress port.

    ``tell`` answers CLOSED for the moment the guardian is rebuilding the
    handler.  Latching ``_closing`` on that would make every supervised
    restart unrecoverable, which is exactly what D12/D13 promise it is not.
    """

    events = _Events()
    coordinator = DeliveryCustodyCoordinator(
        tmp_path / "inbox.sqlite3",
        MemoryDeliveryTransport(),
        events,
        runtime=_runtime(),
    )
    live_tell = coordinator._runtime.tell
    attempts = {"count": 0}

    def restarting_once(handle: object, command: object) -> AdmissionResult:
        attempts["count"] += 1
        if attempts["count"] == 1:
            return AdmissionResult.CLOSED
        return live_tell(handle, command)  # type: ignore[arg-type]

    coordinator._runtime.tell = restarting_once  # type: ignore[assignment]

    assert coordinator.submit(ReceiveMessageCommand("during", _message())) is (
        PortAdmission.CLOSING
    )
    assert coordinator.submit(ReceiveMessageCommand("after", _message("m2"))) is (
        PortAdmission.ACCEPTED
    )
    _close(coordinator, events)
