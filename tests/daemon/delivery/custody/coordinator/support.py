from __future__ import annotations

import threading
import time
from collections.abc import Callable

from tests.support.runtime.waiting_support import Budget


from hyprial.kernel.impl.actor_runtime import (
    STATE_AUTHORITY,
    AdmissionResult,
    ActorRuntime,
    SupervisionPolicy,
)
from hyprial.kernel.impl.actor_runtime.scheduler import GenerationScheduler
from hyprial.kernel.impl.contracts.ports import PortAdmission
from hyprial.daemon.impl.inbox.actor import (
    DeliveryCustodyCoordinator,
    DispatchIoCompleted,
    DispatchIoFailed,
)
from hyprial.daemon.impl.inbox.contracts.api import DeliveryLifecycle, InboxMessage
from hyprial.daemon.impl.inbox.memory import MemoryDeliveryTransport
from hyprial.daemon.impl.inbox.contracts.ports import (
    CloseInboxCommand,
    InboxClosed,
)


class _Events:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._items: list[object] = []

    def publish(self, event: object) -> None:
        with self._lock:
            self._items.append(event)

    def snapshot(self) -> tuple[object, ...]:
        with self._lock:
            return tuple(self._items)


class _OnlineTransport(MemoryDeliveryTransport):
    def __init__(self) -> None:
        super().__init__()
        self.deliveries: list[str] = []

    def is_online(self, recipient: str) -> bool:
        del recipient
        return True

    def deliver(self, message: InboxMessage) -> bool:
        self.deliveries.append(message.message_id)
        return True


class _BlockingTransport(_OnlineTransport):
    def __init__(self) -> None:
        super().__init__()
        self.entered = threading.Event()
        self.release = threading.Event()

    def deliver(self, message: InboxMessage) -> bool:
        self.entered.set()
        assert self.release.wait(1.0)
        return super().deliver(message)


class _CompletionAdmissionRuntime(ActorRuntime):
    def __init__(
        self,
        *,
        overloaded_before_accept: int = 0,
        closed: bool = False,
        hold_overloaded: bool = False,
        scheduler: GenerationScheduler | None = None,
    ) -> None:
        super().__init__(
            scheduler=scheduler,
            policies={
                STATE_AUTHORITY: SupervisionPolicy(
                    max_restarts=3,
                    restart_window=10.0,
                    base_backoff=0.0,
                    max_backoff=0.0,
                    jitter_ratio=0.0,
                )
            },
            random_source=lambda: 0.5,
        )
        self._admission_lock = threading.Lock()
        self._overloaded_before_accept = overloaded_before_accept
        self._closed_completion = closed
        self._hold_overloaded = hold_overloaded
        self.completion_attempts = 0

    def tell(self, handle: object, command: object) -> AdmissionResult:
        if isinstance(command, (DispatchIoCompleted, DispatchIoFailed)):
            with self._admission_lock:
                self.completion_attempts += 1
                if self._closed_completion:
                    return AdmissionResult.CLOSED
                if self._hold_overloaded:
                    return AdmissionResult.OVERLOADED
                if self._overloaded_before_accept > 0:
                    self._overloaded_before_accept -= 1
                    return AdmissionResult.OVERLOADED
        return super().tell(handle, command)  # type: ignore[arg-type]

    def allow_completions(self) -> None:
        with self._admission_lock:
            self._closed_completion = False
            self._hold_overloaded = False


def _message(message_id: str = "m1") -> InboxMessage:
    return InboxMessage(
        message_id=message_id,
        conversation_id="c1",
        sender="agent:owner:node-a:alice",
        recipient="agent:owner:node-b:bob",
        payload=b'{"message":"hello"}',
        intent="message",
        lifecycle=DeliveryLifecycle.DURABLE_SERVICE,
        created_at_ms=10,
        idempotency_key=f"idem-{message_id}",
    )


#: The bound these waits are entitled to, taken from the product instead of
#: guessed here.  ``DeliveryCustodyCoordinator.call`` IS ``submit`` followed by
#: a wait for the completion event (actor.py:2492 -- ``expect``, ``submit``,
#: ``reply.wait(timeout)``), and production runs that wait at
#: ``DELIVERY_CUSTODY_CALL_TIMEOUT_SECONDS``.  Every wait below hand-rolls the
#: same round trip: submit a command, then wait for its completion event.
#:
#: The literal that used to sit here was ``1.0`` -- half the product's own
#: declared patience.  Any settle that took between 1.0s and 2.0s was therefore
#: legal product behaviour AND a test failure, which is what went red on dev at
#: f05e19fe (task 18169,
#: ``test_progress_storage_mutation_is_owned_by_state_actor``).  It is not a
#: flake: the test contracted for half of what the product promises.
#:
#: Derived through ``Budget.from_product`` rather than copied, so a change to
#: the product's bound moves this one and a rename fails loudly here.
_SETTLE_BUDGET = Budget.from_product(
    'hyprial.daemon.impl.inbox.authority.facade.DELIVERY_CUSTODY_CALL_TIMEOUT_SECONDS'
)

#: ⛔ NOT derived, and deliberately not a ``Budget``.
#:
#: Four waits here do not wait on a custody settle at all: they `tell` the
#: coordinator a bad message to fault it, then wait for the actor runtime to
#: bring it back at generation 2.  That is a supervision restart, and
#: ``src/hyprial/actor_runtime`` declares **no** module-level bound for it --
#: there is nothing to point ``Budget.from_product`` at.
#:
#: Letting those three inherit ``_SETTLE_BUDGET`` would repeat the exact
#: defect this change fixes.  ``1.0`` was a borrowed number; ``2.0`` used
#: somewhere it was never declared for is also a borrowed number, and the only
#: difference would be that this one happens to be borrowed from the right
#: place for the OTHER waits.  So it is named, kept separate, and labelled as
#: a guess, which is what ``Budget`` refuses to let anyone express implicitly.
#:
#: To retire it: give the runtime's restart path a module-level constant and
#: switch these four to ``Budget.from_product`` pointing at it.
_UNBUDGETED_RESTART_WAIT_SECONDS = 2.0


def _wait_until(
    predicate: Callable[[], bool],
    timeout: float | None = None,
    describe: Callable[[], str] | None = None,
) -> None:
    """Wait for a settle this module has not yet classified as monotonic.

    22 of the 29 call sites here wait on ``events.snapshot()``, an append-only
    typed event log, and those belong in ``waiting_support.await_recorded``.
    Seven do not, and they are the reason this helper still exists.  Anchored on
    what each one waits FOR, not on line numbers, which move:

      * ``transport.deliveries == ["m1"]`` -- list EQUALITY; a second delivery
        makes it false again
      * ``state is RUNNING and generation == 2`` (x4) -- the state can leave
        RUNNING and the generation can advance to 3.  These also wait on a
        supervision restart rather than a custody settle, so they carry
        ``_UNBUDGETED_RESTART_WAIT_SECONDS`` rather than the derived budget
      * ``bool(delivery_status.for_sender(...))`` -- probably monotonic, but it
        needs its own justification rather than a shared one
      * ``read_counts().outbox_count == 0`` -- a count that can rise again, and
        it carries its own larger explicit budget

    Moving those into ``await_recorded`` would put non-monotonic conditions
    inside a function that deliberately skips the vacuous-wait check, which
    would weaken them silently.  They are a separate batch.
    """

    deadline = time.monotonic() + (
        _SETTLE_BUDGET.seconds if timeout is None else timeout
    )
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.005)
    detail = f"; {describe()}" if describe is not None else ""
    raise AssertionError(f"condition was not satisfied before timeout{detail}")


def _runtime() -> ActorRuntime:
    return ActorRuntime(
        policies={
            STATE_AUTHORITY: SupervisionPolicy(
                max_restarts=3,
                restart_window=10.0,
                base_backoff=0.0,
                max_backoff=0.0,
                jitter_ratio=0.0,
            )
        },
        random_source=lambda: 0.5,
    )


def _close(coordinator: DeliveryCustodyCoordinator, events: _Events) -> None:
    if coordinator.submit(CloseInboxCommand("close")) is PortAdmission.ACCEPTED:
        _wait_until(lambda: any(isinstance(item, InboxClosed) for item in events.snapshot()))
    assert coordinator.drain(1.0).complete is True
















    # The suite's unchanged FD guard also runs on this failure-path test;
    # retaining the exception traceback must not retain open SQLite handles.


































# Shared builders/data and fixture registrations used by the focused modules.
__all__ = [
    '_BlockingTransport',
    '_CompletionAdmissionRuntime',
    '_Events',
    '_OnlineTransport',
    '_SETTLE_BUDGET',
    '_UNBUDGETED_RESTART_WAIT_SECONDS',
    '_close',
    '_message',
    '_runtime',
    '_wait_until',
]
