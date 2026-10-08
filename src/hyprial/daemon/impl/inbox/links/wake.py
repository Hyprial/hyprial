"""Bounded recipient-online wake custody outside Presence and Inbox owners."""

from __future__ import annotations

import logging
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Protocol
from uuid import uuid4

from hyprial.kernel import PortAdmission, PortCommandRejected
from hyprial.daemon.impl.transport.presence_actor import (
    ActorOnlineTransition,
    PresenceProjection,
)

from hyprial.daemon.impl.inbox.contracts.ports  import (
    BoolMutationCompleted,
    PinnedEventClaim,
    WakeOutboxRecipientCommand,
)

logger = logging.getLogger(__name__)


class _WakeCommandSink(Protocol):
    def submit(self, command: WakeOutboxRecipientCommand) -> PortAdmission: ...


class _WakeEventRouter(Protocol):
    def claim_pinned(self, correlation_id: str) -> PinnedEventClaim: ...

    def wait(self, correlation_id: str, timeout: float) -> object | None: ...

    def release_pinned(self, correlation_id: str) -> None: ...


class _RecipientReader(Protocol):
    def outbox_recipient_page(
        self, *, after: str | None = None, limit: int = 64
    ) -> tuple[str, ...]: ...


class _WakePhase(StrEnum):
    READY = "ready"
    ADMITTING = "admitting"
    ADMITTED = "admitted"


@dataclass(frozen=True, slots=True)
class _WakeIntent:
    transition: ActorOnlineTransition
    command: WakeOutboxRecipientCommand
    phase: _WakePhase = _WakePhase.READY


class RecipientWakeCoordinator:
    """Coalesce online transitions and retain one exact Inbox mutation each.

    Native RX and Presence only call :meth:`submit`, which never waits.  This
    worker owns bounded retry/join custody. Overflow becomes one paged scan
    intent rather than an unbounded recipient set.
    """

    def __init__(
        self,
        commands: _WakeCommandSink,
        events: _WakeEventRouter,
        recipients: _RecipientReader,
        presence: Callable[[], PresenceProjection],
        *,
        capacity: int = 128,
        page_size: int = 64,
        completion_timeout: float = 0.1,
        clock_ms: Callable[[], int] | None = None,
    ) -> None:
        if capacity < 1:
            raise ValueError("wake capacity must be positive")
        if page_size < 1 or page_size > 256:
            raise ValueError("wake page size must be in 1..256")
        if completion_timeout <= 0:
            raise ValueError("wake completion timeout must be positive")
        self._commands = commands
        self._events = events
        self._recipients = recipients
        self._presence = presence
        self._capacity = capacity
        self._page_size = page_size
        self._completion_timeout = completion_timeout
        self._clock_ms = clock_ms or (lambda: time.time_ns() // 1_000_000)
        self._condition = threading.Condition()
        self._pending: OrderedDict[str, _WakeIntent] = OrderedDict()
        self._successors: dict[str, _WakeIntent] = {}
        self._overflow_generation: int | None = None
        self._scan_after: str | None = None
        self._scan_epoch = 0
        self._closing = False
        self._closed = False
        self._worker = threading.Thread(
            target=self._run,
            name="hyprial-recipient-wake",
            daemon=True,
        )
        self._worker.start()

    def submit(self, transition: ActorOnlineTransition) -> PortAdmission:
        """Nonblocking committed-transition admission."""

        with self._condition:
            if self._closing:
                return PortAdmission.CLOSING
            projection = self._presence()
            if (
                not projection.complete
                or transition.generation != projection.generation
                or transition.actor not in projection.actors
            ):
                return PortAdmission.ACCEPTED
            if transition.actor in self._pending:
                self._coalesce_locked(transition)
                return PortAdmission.ACCEPTED
            if len(self._pending) >= self._capacity:
                self._request_scan_locked(transition.generation)
                return PortAdmission.ACCEPTED
            self._enqueue_locked(transition)
            return PortAdmission.ACCEPTED

    def _new_intent(self, transition: ActorOnlineTransition) -> _WakeIntent:
        return _WakeIntent(
            transition,
            WakeOutboxRecipientCommand(
                correlation_id=f"inbox:wake-online:{uuid4().hex}",
                recipient=transition.actor,
                now_ms=int(self._clock_ms()),
            ),
        )

    def _enqueue_locked(self, transition: ActorOnlineTransition) -> None:
        self._pending[transition.actor] = self._new_intent(transition)
        self._condition.notify_all()

    def _coalesce_locked(self, transition: ActorOnlineTransition) -> None:
        current = self._pending[transition.actor]
        successor = self._successors.get(transition.actor)
        latest = successor.transition if successor is not None else current.transition
        if (
            transition.generation < latest.generation
            or (
                transition.generation == latest.generation
                and transition.sequence <= latest.sequence
            )
        ):
            return
        intent = self._new_intent(transition)
        if current.phase is _WakePhase.READY:
            self._pending[transition.actor] = intent
        else:
            # One bounded successor is enough: it represents the latest
            # committed offline->online transition while the exact accepted
            # current command retains settlement custody.
            self._successors[transition.actor] = intent
        self._condition.notify_all()

    def _request_scan_locked(self, generation: int) -> None:
        self._overflow_generation = generation
        self._scan_after = None
        self._scan_epoch += 1
        self._condition.notify_all()

    def _run(self) -> None:
        while True:
            with self._condition:
                if (
                    self._closing
                    and not self._pending
                    and self._overflow_generation is None
                ):
                    self._closed = True
                    self._condition.notify_all()
                    return
                intent = next(iter(self._pending.values()), None)
                scan = intent is None and self._overflow_generation is not None
                if intent is None and not scan:
                    self._condition.wait(0.05)
                    continue
            if intent is not None:
                try:
                    self._settle(intent)
                except Exception:
                    logger.exception("recipient wake settlement failed")
                    self._wait_retry()
            else:
                try:
                    self._recover_overflow()
                except Exception:
                    logger.exception("recipient wake overflow recovery failed")
                    self._wait_retry()

    def _settle(self, intent: _WakeIntent) -> None:
        with self._condition:
            current = self._pending.get(intent.transition.actor)
            if current is not intent:
                return
            if intent.phase is _WakePhase.READY:
                intent = replace(intent, phase=_WakePhase.ADMITTING)
                self._pending[intent.transition.actor] = intent
        projection = self._presence()
        if (
            intent.phase is not _WakePhase.ADMITTED
            and (
                not projection.complete
                or intent.transition.generation != projection.generation
                or intent.transition.actor not in projection.actors
            )
        ):
            self._retire(intent.transition.actor, intent.command.correlation_id)
            return
        if intent.phase is _WakePhase.ADMITTING:
            claim = self._events.claim_pinned(intent.command.correlation_id)
            if claim is PinnedEventClaim.FULL:
                self._set_phase(intent, _WakePhase.READY)
                self._wait_retry()
                return
            if claim is PinnedEventClaim.OWNER:
                admission = self._commands.submit(intent.command)
                if admission is not PortAdmission.ACCEPTED:
                    self._events.release_pinned(intent.command.correlation_id)
                    self._set_phase(intent, _WakePhase.READY)
                    self._wait_retry()
                    return
                intent = self._set_phase(intent, _WakePhase.ADMITTED)
        event = self._events.wait(
            intent.command.correlation_id, self._completion_timeout
        )
        if event is None:
            self._wait_retry()
            return
        if isinstance(event, BoolMutationCompleted):
            if event.operation != "wake_outbox_recipient":
                raise RuntimeError("wake correlation received wrong completion")
            self._retire(intent.transition.actor, intent.command.correlation_id)
            return
        if isinstance(event, PortCommandRejected):
            self._events.release_pinned(intent.command.correlation_id)
            self._set_phase(intent, _WakePhase.READY)
            self._wait_retry()
            return
        raise RuntimeError("wake correlation received unsupported event")

    def _set_phase(
        self, intent: _WakeIntent, phase: _WakePhase
    ) -> _WakeIntent:
        with self._condition:
            current = self._pending.get(intent.transition.actor)
            if current is not None and current.command == intent.command:
                successor = (
                    self._successors.pop(intent.transition.actor, None)
                    if phase is _WakePhase.READY
                    else None
                )
                current = (
                    successor
                    if successor is not None
                    else replace(current, phase=phase)
                )
                self._pending[intent.transition.actor] = current
                self._condition.notify_all()
                return current
        return intent

    def _retire(self, recipient: str, correlation_id: str) -> None:
        self._events.release_pinned(correlation_id)
        with self._condition:
            current = self._pending.get(recipient)
            if current is not None and current.command.correlation_id == correlation_id:
                successor = self._successors.pop(recipient, None)
                if successor is None:
                    self._pending.pop(recipient, None)
                else:
                    self._pending[recipient] = successor
            self._condition.notify_all()

    def _recover_overflow(self) -> None:
        with self._condition:
            generation = self._overflow_generation
            after = self._scan_after
            scan_epoch = self._scan_epoch
        if generation is None:
            return
        projection = self._presence()
        if generation != projection.generation:
            with self._condition:
                if (
                    self._overflow_generation == generation
                    and self._scan_epoch == scan_epoch
                ):
                    self._overflow_generation = None
                    self._scan_after = None
                    self._condition.notify_all()
            return
        page = self._recipients.outbox_recipient_page(
            after=after, limit=self._page_size
        )
        with self._condition:
            if (
                self._overflow_generation != generation
                or self._scan_epoch != scan_epoch
            ):
                return
            if not page:
                self._overflow_generation = None
                self._scan_after = None
                self._condition.notify_all()
                return
            cursor = after
            for recipient in page:
                if recipient in projection.actors and recipient in self._pending:
                    self._coalesce_locked(
                        ActorOnlineTransition(
                            generation,
                            recipient,
                            projection.online_sequence,
                        )
                    )
                    cursor = recipient
                    continue
                if (
                    recipient in projection.actors
                    and len(self._pending) >= self._capacity
                ):
                    self._scan_after = cursor
                    return
                if recipient in projection.actors:
                    self._enqueue_locked(
                        ActorOnlineTransition(
                            generation,
                            recipient,
                            projection.online_sequence,
                        )
                    )
                cursor = recipient
            self._scan_after = cursor
            if len(page) < self._page_size:
                self._overflow_generation = None
                self._scan_after = None
            self._condition.notify_all()

    def _wait_retry(self) -> None:
        with self._condition:
            self._condition.wait(0.01)

    def close(self, timeout: float = 5.0) -> bool:
        deadline = time.monotonic() + max(0.0, timeout)
        with self._condition:
            self._closing = True
            self._condition.notify_all()
            while not self._closed:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._condition.wait(remaining)
        self._worker.join(max(0.0, deadline - time.monotonic()))
        return not self._worker.is_alive()

    @property
    def pending_count(self) -> int:
        with self._condition:
            return (
                len(self._pending)
                + len(self._successors)
                + int(self._overflow_generation is not None)
            )
