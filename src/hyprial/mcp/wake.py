"""Fair wake scheduling with a hard preview/formal-delivery boundary."""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Literal, Protocol, runtime_checkable

from hyprial.daemon.desired_state import DesiredState, InteractiveSession
from hyprial.log import Logger


class WakeStatus(StrEnum):
    ACCEPTED = "accepted"
    SIGNALLED = "signalled"
    BUSY = "busy"
    OFFLINE = "offline"
    FAILED = "failed"


class WakeDeliveryState(StrEnum):
    """Daemon delivery lifecycle as observed by the wake edge.

    ``SIGNALLED`` means only that a harness transport accepted the edge.
    ``AWAITING_ACK`` means ``harness_read`` took custody in the model turn.  In
    neither state is the daemon delivery complete.
    """

    QUEUED = "queued"
    SIGNALLED = "signalled"
    AWAITING_ACK = "awaiting_ack"


@dataclass(frozen=True, slots=True)
class WakeAttempt:
    status: WakeStatus
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class WakeCommand:
    kind: Literal["formal"]
    actor: str
    delivery_id: str
    cwd: str
    command: tuple[str, ...]
    prompt: str


@dataclass(frozen=True, slots=True)
class WakeOutcome:
    actor: str
    delivery_id: str
    message_id: str
    status: WakeStatus
    detail: str | None = None


@runtime_checkable
class InteractiveSessionStore(Protocol):
    def load(self) -> DesiredState: ...


@runtime_checkable
class WakeDriver(Protocol):
    async def wake(self, command: WakeCommand) -> WakeAttempt: ...


@dataclass(frozen=True, slots=True)
class _QueuedWake:
    actor: str
    delivery_id: str
    message_id: str
    attempt: int = 0
    not_before: float = 0.0
    state: WakeDeliveryState = WakeDeliveryState.QUEUED


class WakeCoordinator:
    """Schedule formal wakes fairly while leaving preview completely inert.

    One dispatch pass visits every item that was queued when the pass began at
    most once. A busy actor is put at the tail with backoff, so it cannot hold
    another actor's formal delivery behind repeated harness rejections.

    Renotify policy is state-dependent. ``SIGNALLED`` means the transport
    accepted the edge but nothing proves a model turn saw it, so the edge is
    resignalled on ``resignal_delay`` (default 30s, doubling, 300s cap).
    ``AWAITING_ACK`` means ``harness_read`` took custody — the session has
    demonstrably seen the delivery, so resignalling is suppressed entirely
    except one fallback nudge every ``read_retry_delay`` seconds (default 10
    minutes) in case the session died between read and reply/ack. A delivery
    that reached ``AWAITING_ACK`` never rejoins the aggressive signalled
    cadence; only a durable reply/ack clears its key.
    """

    def __init__(
        self,
        sessions: InteractiveSessionStore,
        driver: WakeDriver,
        *,
        logger: Logger | None = None,
        clock: Callable[[], float] = time.monotonic,
        retry_delay: Callable[[int], float] | None = None,
        resignal_delay: Callable[[int], float] | None = None,
        read_retry_delay: float = 600.0,
    ) -> None:
        if read_retry_delay <= 0:
            raise ValueError("read_retry_delay must be positive")
        self._sessions = sessions
        self._driver = driver
        self._logger = logger
        self._clock = clock
        self._retry_delay = retry_delay or _default_retry_delay
        self._resignal_delay = resignal_delay or _default_resignal_delay
        self._read_retry_delay = read_retry_delay
        self._queues: dict[str, deque[_QueuedWake]] = {}
        self._actors: deque[str] = deque()
        self._keys: set[tuple[str, str]] = set()
        self._pending_count = 0

    @property
    def pending_count(self) -> int:
        return self._pending_count

    def preview(self, actor: str, summaries: Sequence[Mapping[str, object]]) -> str:
        """Render observation-only text; never enqueue or invoke a wake driver."""

        lines = [
            "PREVIEW ONLY — this is not a formal Harness delivery and must not be replied to.",
            f"Actor {actor} may have durable messages. Call harness_read for daemon-authoritative state.",
        ]
        for summary in summaries:
            sender = _one_line(str(summary.get("from", "unknown")), 40)
            text = _one_line(str(summary.get("text", "")), 120)
            lines.append(f"- {sender}: {text}")
        return "\n".join(lines)

    def enqueue(
        self, actor: str, delivery_id: str, *, message_id: str | None = None
    ) -> bool:
        canonical_message_id = message_id or delivery_id
        if not actor or not delivery_id or not canonical_message_id:
            raise ValueError("formal wake requires actor, delivery_id, and message_id")
        key = (actor, delivery_id)
        if key in self._keys:
            # A transport notification may enqueue the opaque delivery edge
            # before the next daemon poll reveals its stable Harness message
            # identity. Enrich that existing item instead of emitting a
            # trajectory under the epoch-scoped delivery id.
            queue = self._queues.get(actor)
            if queue is not None:
                self._queues[actor] = deque(
                    replace(item, message_id=canonical_message_id)
                    if item.delivery_id == delivery_id
                    else item
                    for item in queue
                )
            return False
        self._keys.add(key)
        queue = self._queues.get(actor)
        if queue is None:
            queue = deque()
            self._queues[actor] = queue
            self._actors.append(actor)
        queue.append(
            _QueuedWake(
                actor=actor,
                delivery_id=delivery_id,
                message_id=canonical_message_id,
            )
        )
        self._pending_count += 1
        return True

    def delivery_state(self, actor: str, delivery_id: str) -> WakeDeliveryState | None:
        queue = self._queues.get(actor, ())
        return next(
            (item.state for item in queue if item.delivery_id == delivery_id),
            None,
        )

    def pending_deliveries(self, actor: str) -> tuple[str, ...]:
        """Snapshot the delivery ids currently keyed for one actor."""

        return tuple(item.delivery_id for item in self._queues.get(actor, ()))

    def rearm(self, actor: str, delivery_ids: Iterable[str]) -> int:
        """Make an existing backlog immediately eligible after session recovery.

        Recovery is not completion: every delivery retains its current state and
        still requires a durable reply/ack.  This only bypasses a stale
        ``not_before`` left by a notification sent to the pre-recovery session.
        """

        identifiers = set(delivery_ids)
        queue = self._queues.get(actor)
        if not identifiers or queue is None:
            return 0
        now = self._clock()
        changed = 0
        updated: deque[_QueuedWake] = deque()
        for item in queue:
            if item.delivery_id in identifiers and item.not_before > now:
                item = replace(item, not_before=now)
                changed += 1
            updated.append(item)
        self._queues[actor] = updated
        return changed

    def observe_read(self, actor: str, delivery_ids: Iterable[str]) -> int:
        """Mark daemon-returned deliveries as taken over, without completing them."""

        identifiers = set(delivery_ids)
        queue = self._queues.get(actor)
        if not identifiers or queue is None:
            return 0
        changed = 0
        updated: deque[_QueuedWake] = deque()
        for item in queue:
            if (
                item.delivery_id in identifiers
                and item.state != WakeDeliveryState.AWAITING_ACK
            ):
                item = replace(
                    item,
                    state=WakeDeliveryState.AWAITING_ACK,
                    not_before=self._clock() + self._read_retry_delay,
                )
                changed += 1
            updated.append(item)
        self._queues[actor] = updated
        return changed

    def complete(self, actor: str, delivery_id: str) -> bool:
        """Clear one key only after a successful daemon reply/ack."""

        key = (actor, delivery_id)
        queue = self._queues.get(actor)
        if key not in self._keys or queue is None:
            return False
        remaining = deque(item for item in queue if item.delivery_id != delivery_id)
        self._keys.remove(key)
        self._pending_count -= 1
        if remaining:
            self._queues[actor] = remaining
        else:
            del self._queues[actor]
            self._actors = deque(item for item in self._actors if item != actor)
        return True

    async def dispatch_due(self) -> list[WakeOutcome]:
        now = self._clock()
        sessions = {
            item.actor: item for item in self._sessions.load().interactive_sessions
        }
        outcomes: list[WakeOutcome] = []
        # Visit each actor once. A busy actor therefore consumes one scheduling
        # slot, regardless of how many deliveries it has accumulated.
        visits = len(self._actors)
        for _ in range(visits):
            actor = self._actors.popleft()
            queue = self._queues[actor]
            batch_size = len(queue)
            item = queue[0]
            # A read-owned delivery suppresses further notifications for this
            # session until terminal reply/ack clears it, except the
            # read_retry_delay fallback nudge for a session that died mid-turn.
            if item.state == WakeDeliveryState.AWAITING_ACK and item.not_before > now:
                self._actors.append(actor)
                continue
            if item.not_before > now:
                self._actors.append(actor)
                continue
            queue.popleft()
            session = sessions.get(item.actor)
            if session is None:
                attempt = WakeAttempt(
                    WakeStatus.OFFLINE, "actor has no interactive session registration"
                )
            else:
                attempt = await self._driver.wake(
                    _formal_command(item, session, batch_size=batch_size)
                )
            outcomes.append(
                WakeOutcome(
                    actor=item.actor,
                    delivery_id=item.delivery_id,
                    message_id=item.message_id,
                    status=attempt.status,
                    detail=attempt.detail,
                )
            )
            next_attempt = item.attempt + 1
            if item.state == WakeDeliveryState.AWAITING_ACK:
                # Read custody is proof the session saw this delivery; this
                # wake was the long-timeout fallback for a session that may
                # have died after read. Whatever the transport said, stay in
                # AWAITING_ACK on the long window — never fall back to the
                # aggressive signalled cadence.
                queue.appendleft(
                    replace(
                        item,
                        attempt=next_attempt,
                        not_before=now + self._read_retry_delay,
                    )
                )
                if attempt.status == WakeStatus.ACCEPTED:
                    outcomes[-1] = replace(outcomes[-1], status=WakeStatus.SIGNALLED)
            elif attempt.status == WakeStatus.ACCEPTED:
                queue.appendleft(
                    replace(
                        item,
                        state=WakeDeliveryState.SIGNALLED,
                        attempt=next_attempt,
                        not_before=now + self._resignal_delay(next_attempt),
                    )
                )
                outcomes[-1] = replace(outcomes[-1], status=WakeStatus.SIGNALLED)
            else:
                queue.appendleft(
                    replace(
                        item,
                        state=WakeDeliveryState.QUEUED,
                        attempt=next_attempt,
                        not_before=now + self._retry_delay(next_attempt),
                    )
                )
            if queue:
                self._actors.append(actor)
            else:
                del self._queues[actor]
            self._log_outcome(outcomes[-1], attempt=next_attempt)
        return outcomes

    def _log_outcome(self, outcome: WakeOutcome, *, attempt: int) -> None:
        logger = self._logger
        if logger is None:
            return
        event = f"wake.{outcome.status.value}"
        level = "info" if outcome.status is WakeStatus.SIGNALLED else "warn"
        try:
            logger.log(
                level,
                event,
                messageId=outcome.message_id,
                correlationId=outcome.message_id,
                node="wake",
                actorId=outcome.actor,
                deliveryId=outcome.delivery_id,
                attempt=attempt,
                **({"detail": outcome.detail} if outcome.detail is not None else {}),
            )
        except (NameError, ImportError):
            raise
        except OSError:
            # Wake custody/retry is authoritative. A local log I/O failure may
            # not change whether the delivery is signalled or retained.
            return


def _formal_command(
    item: _QueuedWake, session: InteractiveSession, *, batch_size: int
) -> WakeCommand:
    noun = "delivery" if batch_size == 1 else "deliveries"
    prompt = (
        f"Harness backlog has {batch_size} pending {noun} for actor={item.actor} "
        f"(head delivery_id={item.delivery_id}). Call harness_read once and drain "
        "the entire FIFO batch; then use harness_reply or harness_ack for every item."
    )
    return WakeCommand(
        kind="formal",
        actor=item.actor,
        delivery_id=item.delivery_id,
        cwd=session.cwd,
        command=session.command,
        prompt=prompt,
    )


def _default_retry_delay(attempt: int) -> float:
    """Failure retry (busy/offline/crashed edge): fast, 1s doubling to 60s."""

    return min(60.0, float(2 ** min(attempt - 1, 6)))


def _default_resignal_delay(attempt: int) -> float:
    """Accepted-but-unread renotify cadence: 30s, 60s, 120s, 240s, 300s cap.

    Deliberately much steeper than the failure retry — an accepted edge that
    has not been read yet is probably a session mid-turn, and every resignal
    is user-visible noise (production observed 5 pushes for one message under
    the old shared 1s-start curve).
    """

    return min(300.0, 30.0 * float(2 ** min(attempt - 1, 4)))


def _one_line(value: str, limit: int) -> str:
    compact = " ".join(value.split())
    return compact if len(compact) <= limit else f"{compact[: limit - 1]}…"
