"""WP7 §2.1-2.3: the turn-end reply-reminder consumer.

One bus lane.  For each actor it remembers the current turn's start, the last
turn's end, the ids it already reminded about, and the query window of the
most recent completed event (so an event replay is answered from the same
window instead of a shrunken one -- review R2).

Known edge (design §2.3): after a daemon restart that also drained the
notice, the same unanswered group can be reminded once more.  The notice id
is deterministic, so before the drain the write is idempotent; after it
there is no persisted memory, and this consumer deliberately does not add a
second durable store.
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable
from typing import Any

from hyprial.daemon import SUMMARY_MAX_BYTES, ReplyReminderPort
from hyprial.kernel import HookEvent

from hyprial.biz.impl.hooks.reminder.text import reply_reminder_text

#: The three events this consumer subscribes to (design §2.1).  The daemon
#: side defines the same names without importing biz.
REPLY_REMINDER_EVENTS: tuple[str, ...] = (
    "turn.started",
    "turn.completed",
    "turn.failed",
)

#: Bounded in-memory state (review A4): at most 1024 actors, at most 256
#: already-reminded ids per actor (FIFO).
MAX_TRACKED_ACTORS = 1024
MAX_REMINDED_IDS_PER_ACTOR = 256

_UNSET: object = object()

#: ``(delivery_ids, fetched_since_ms, fetched_until_ms, since_inclusive)`` or
#: ``None`` when there is no window to query.
_Window = tuple[tuple[str, ...], int | None, int, bool] | None


class _ActorState:
    __slots__ = (
        "started_at_ms",
        "last_end_ms",
        "reminded",
        "window_key",
        "window",
        "handled",
    )

    def __init__(self) -> None:
        self.started_at_ms: int | None = None
        self.last_end_ms: int | None = None
        self.reminded: OrderedDict[str, None] = OrderedDict()
        self.window_key: object = _UNSET
        self.window: _Window = None
        #: ``(source, seq)`` of the newest completed event already folded
        #: into the state; older or equal events never advance it again.
        self.handled: tuple[str, int] | None = None


class ReplyReminderConsumer:
    """Records unanswered requests after a turn and reminds once per group."""

    def __init__(
        self,
        port: ReplyReminderPort,
        *,
        logger: Callable[..., None] | None = None,
    ) -> None:
        self._port = port
        self._logger = logger
        self._states: OrderedDict[str, _ActorState] = OrderedDict()

    def observe(self, event: HookEvent) -> None:
        actor = event.actor
        if actor is None or event.event not in REPLY_REMINDER_EVENTS:
            return
        if event.event == "turn.started":
            state = self._state(actor)
            state.started_at_ms = self._millis(event, "startedAtMs")
        elif event.event == "turn.failed":
            # A failed turn is retried or settled; it never reminds.  Its end
            # still becomes the start of the next completed window.
            state = self._state(actor)
            state.started_at_ms = None
            state.last_end_ms = self._millis(event, "endedAtMs")
            state.window_key = _UNSET
            state.window = None
        else:
            self._completed(actor, event)

    def _completed(self, actor: str, event: HookEvent) -> None:
        state = self._state(actor)
        ended_at_ms = self._millis(event, "endedAtMs")
        key = (event.source, event.seq)
        handled = state.handled
        if (
            handled is not None
            and handled[0] == event.source
            and (event.seq < handled[1] or (key == handled and state.window_key != key))
        ):
            # A stale replay: its turn is already folded in and a newer one
            # may be running.  Answering it from a rebuilt window, or letting
            # it touch started/last_end, would corrupt the current turn.
            self._log("debug", "reply_reminder.stale_replay", actor=actor)
            return
        replay = state.window_key == key
        try:
            if replay:
                window = state.window
            else:
                window = self._window(state, event, ended_at_ms)
                state.window_key = key
                state.window = window
            if window is None:
                return
            delivery_ids, since_ms, until_ms, since_inclusive = window
            unreplied = tuple(
                self._port.unreplied_requests(
                    actor,
                    delivery_ids=delivery_ids,
                    fetched_since_ms=since_ms,
                    fetched_until_ms=until_ms,
                    since_inclusive=since_inclusive,
                )
            )
            if not unreplied:
                return
            if set(unreplied) <= set(state.reminded):
                self._log(
                    "debug",
                    "reply_reminder.suppressed",
                    actor=actor,
                    requestIds=list(unreplied),
                )
                return
            text = reply_reminder_text(unreplied, max_bytes=SUMMARY_MAX_BYTES)
            try:
                posted = bool(
                    self._port.post_reply_reminder(actor, unreplied, text)
                )
            except Exception as error:  # noqa: BLE001 -- never abort the lane
                self._log(
                    "warn",
                    "reply_reminder.failed",
                    actor=actor,
                    error=type(error).__name__,
                )
                return
            if not posted:
                # Review R2: a False write did not happen, so it must not
                # enter the reminded set and must be observable.
                self._log(
                    "warn",
                    "reply_reminder.failed",
                    actor=actor,
                    error="post_rejected",
                )
                return
            for message_id in unreplied:
                state.reminded[message_id] = None
                while len(state.reminded) > MAX_REMINDED_IDS_PER_ACTOR:
                    state.reminded.popitem(last=False)
        except Exception as error:  # noqa: BLE001 -- never abort the lane
            self._log(
                "warn",
                "reply_reminder.failed",
                actor=actor,
                error=type(error).__name__,
            )
        finally:
            if not replay:
                state.started_at_ms = None
                state.last_end_ms = ended_at_ms
                state.handled = key

    @staticmethod
    def _window(
        state: _ActorState, event: HookEvent, ended_at_ms: int
    ) -> _Window:
        delivery_id = event.payload.get("deliveryId")
        delivery_ids = (
            (delivery_id,) if isinstance(delivery_id, str) and delivery_id else ()
        )
        if state.started_at_ms is not None:
            since_ms: int | None = state.started_at_ms
            since_inclusive = True
        elif state.last_end_ms is not None:
            since_ms = state.last_end_ms
            since_inclusive = False
        else:
            since_ms = None
            since_inclusive = False
        if not delivery_ids and since_ms is None:
            # No injected message and no window: nothing to ask about.
            return None
        return (delivery_ids, since_ms, ended_at_ms, since_inclusive)

    @staticmethod
    def _millis(event: HookEvent, field: str) -> int:
        value = event.payload.get(field)
        return int(value) if value is not None else int(event.emitted_at_ms)

    def _state(self, actor: str) -> _ActorState:
        state = self._states.get(actor)
        if state is None:
            state = _ActorState()
            self._states[actor] = state
        self._states.move_to_end(actor)
        while len(self._states) > MAX_TRACKED_ACTORS:
            self._states.popitem(last=False)
        return state

    def _log(self, level: str, event: str, **fields: Any) -> None:
        if self._logger is not None:
            self._logger(level, event, **fields)


__all__ = [
    "MAX_REMINDED_IDS_PER_ACTOR",
    "MAX_TRACKED_ACTORS",
    "REPLY_REMINDER_EVENTS",
    "ReplyReminderConsumer",
]
