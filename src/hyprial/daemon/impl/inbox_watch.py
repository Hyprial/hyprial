"""Inbox watch: one I/O-free state machine every client harness drives.

Allen, 2026-10-05: 「请现在去规划这个所有客户端都通用的组件」.  The Claude
Channel, the Codex carrier and pi attach each kept their own copy of the same
loop -- register, refresh after a daemon restart, list the inbox, hold on
``message.pending.wait``, decide what is new, re-wake the backlog -- and each
copy drifted (the Channel heartbeat refreshed but never re-woke; Codex
re-registered on STALE_SESSION while the Channel never did).  This module is
that loop once, with every input and output explicit so that the clients
differ only in how they perform a request and how they deliver a message.

Two lanes run concurrently in every client: the *poll* lane (register,
refresh, list, wait, sleep -- strictly one request at a time) and the
*heartbeat* lane (heartbeat, refresh).  Each lane asks ``*_next(now_ms)`` for
its next action, performs it, and reports ``*_done(action, outcome, now_ms)``.
Time is an input and sleeping is an output, so the golden cases in
``contract/inbox-watch/cases`` drive this exactly, with no clock.

Rules worth their history:

* Any response carrying a new ``daemonEpoch`` -- list, wait, heartbeat or
  refresh -- owes a refresh *and* a re-wake of the whole backlog.  The re-wake
  is emitted by the next list after the refresh succeeds, whichever lane saw
  the epoch first (the Channel dropped it when the heartbeat saw it first).
* A refused refresh never stops polling or delivery (0.5.0 silenced wakes for
  hours by gating every poll on the refresh); it is retried with backoff.
* ``SESSION_SUPERSEDED`` from any request makes both lanes quiet for good.
  ``STALE_SESSION`` re-registers -- for every client (Allen, 2026-10-05).
* The wait only decides *when* the next list runs; it is skipped while a
  refresh is owed or after a failed list, a refused/failed wait falls back to
  the ordinary interval, and ``METHOD_NOT_FOUND`` disables it until the
  daemon epoch changes (an upgraded daemon may have it).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from hyprial.kernel import ipc_errors

#: Longest delay between retries of a failing request (matches the Channel).
BACKOFF_CAP_MS = 5_000


def backoff_ms(base_ms: int, failures: int) -> int:
    """``base * 2**(failures-1)``, capped; the base itself with no failures."""

    if failures <= 0:
        return base_ms
    return min(BACKOFF_CAP_MS, base_ms * 2 ** min(failures - 1, 6))


# -- actions ------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Register:
    op: str = "register"


@dataclass(frozen=True, slots=True)
class Refresh:
    op: str = "refresh"


@dataclass(frozen=True, slots=True)
class Heartbeat:
    op: str = "heartbeat"


@dataclass(frozen=True, slots=True)
class ListInbox:
    op: str = "list"


@dataclass(frozen=True, slots=True)
class Wait:
    known_message_ids: tuple[str, ...]
    hold_ms: int | None
    op: str = "wait"


@dataclass(frozen=True, slots=True)
class Sleep:
    ms: int
    op: str = "sleep"


@dataclass(frozen=True, slots=True)
class Quiet:
    op: str = "quiet"


Request = Register | Refresh | Heartbeat | ListInbox | Wait
Action = Request | Sleep | Quiet


# -- outcomes -----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Ok:
    response: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class Rejected:
    """The daemon answered with an error code (normalized by the client)."""

    code: str


@dataclass(frozen=True, slots=True)
class Unreachable:
    """No answer: connection refused, reset or timed out."""


Outcome = Ok | Rejected | Unreachable


@dataclass(frozen=True, slots=True)
class InboxChange:
    """What one list means for delivery.

    ``new`` rows were never listed before and must be delivered.  ``rewake``
    rows were listed before and must be re-notified now, bypassing any
    pacing (``rewake_reason`` says why).  ``completed`` message ids left the
    inbox without this client settling them (another session, ``hyprial
    ack``), so any wake still pending for them must be dropped.
    """

    new: tuple[Mapping[str, Any], ...] = ()
    rewake: tuple[Mapping[str, Any], ...] = ()
    rewake_reason: str | None = None
    completed: tuple[str, ...] = ()

    @property
    def empty(self) -> bool:
        return not (self.new or self.rewake or self.completed)


# -- the machine --------------------------------------------------------------


@dataclass
class _Lane:
    outstanding: Request | None = None


@dataclass
class InboxWatch:
    """The loop's state for one session.

    Not thread-safe: a client that runs its lanes on separate threads (the
    Codex carrier) must serialize ``*_next``/``*_done`` calls itself; an
    anyio client runs them on one event loop.  Every request the client
    performs carries the session's ``sessionRef`` -- the supersede fence this
    machine relies on to learn SESSION_SUPERSEDED from any request.
    """

    poll_interval_ms: int
    heartbeat_interval_ms: int
    hold_ms: int | None = None
    use_wait: bool = True
    counters: dict[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.poll_interval_ms <= 0 or self.heartbeat_interval_ms <= 0:
            raise ValueError("inbox watch intervals must be positive")
        self._poll = _Lane()
        self._heartbeat = _Lane()
        self._quiet = False
        self._registered = False
        self._registered_once = False
        self._register_failures = 0
        self._register_at = 0
        self._epoch: str | None = None
        self._refresh_owed = False
        self._refresh_in_flight = False
        self._refresh_failures = 0
        self._refresh_at = 0
        self._rewake_reason: str | None = None
        self._list_failures = 0
        self._list_at = 0
        self._wait_next = False
        # None: not asked yet on this daemon; False: refused at that epoch.
        self._wait_supported: bool | None = None
        self._wait_refused_epoch: str | None = None
        self._seen: dict[str, Mapping[str, Any]] = {}
        self._listed_ids: tuple[str, ...] = ()
        self._heartbeat_at = 0
        self._connected_at: int | None = None

    # -- public surface --------------------------------------------------

    @property
    def quiet(self) -> bool:
        return self._quiet

    @property
    def registered(self) -> bool:
        return self._registered

    @property
    def daemon_epoch(self) -> str | None:
        """The daemon generation this watch last confirmed or adopted."""

        return self._epoch

    @property
    def refresh_owed(self) -> bool:
        return self._refresh_owed

    def adopt_registered_session(self, now_ms: int) -> None:
        """Adopt a session registered by the client's outer startup path.

        Some harnesses create and fence the daemon session before constructing
        their inbox carrier.  They still need the shared machine to own every
        later refresh, heartbeat, and STALE_SESSION re-registration.  Adoption
        starts with one refresh so the carrier confirms the current daemon
        generation without issuing a second register for the same session.
        """

        if self._registered or self._poll.outstanding or self._heartbeat.outstanding:
            raise RuntimeError("inbox watch session was already started")
        self._registered = True
        self._registered_once = True
        self._refresh_owed = True
        self._heartbeat_at = now_ms + self.heartbeat_interval_ms
        self._connected_at = now_ms

    def request_rewake(self, reason: str = "recovery", *, now: bool = True) -> None:
        """A client-local event (session start/resume, failed turn) asks for
        the backlog to be re-notified on the next list.

        ``now=False`` keeps the current pace: the re-wake waits for the next
        list that would happen anyway (a client re-owing one it could not
        apply must not turn every failure into an immediate re-list)."""

        self._rewake_reason = self._rewake_reason or reason
        if now:
            self._list_at = 0
            self._wait_next = False

    def list_now(self) -> None:
        """Make the next poll action a list (after any owed refresh) instead
        of a wait or a sleep -- for a client that must read now."""

        self._list_at = 0
        self._wait_next = False

    def observe(self, response: Mapping[str, Any]) -> None:
        """Feed a response the client got outside both lanes (a tool call
        such as harness_read): a new daemon epoch there owes the same
        refresh and re-wake as anywhere else."""

        if not self._quiet:
            self._observe_epoch(response)

    def observe_rejection(self, code: str) -> None:
        """Feed an error code a request outside both lanes got (a tool call,
        a turn report): SUPERSEDED silences both lanes, STALE_SESSION owes a
        re-register, anything else is that request's own business."""

        self._terminal(Rejected(code))

    def poll_next(self, now_ms: int) -> Action:
        lane = self._poll
        if lane.outstanding is not None:
            raise RuntimeError(f"poll lane already waits on {lane.outstanding.op}")
        if self._quiet:
            return Quiet()
        if not self._registered:
            if now_ms < self._register_at:
                return Sleep(self._register_at - now_ms)
            return self._issue(lane, Register())
        if self._refresh_due(now_ms):
            self._refresh_in_flight = True
            return self._issue(lane, Refresh())
        if self._wait_next:
            self._wait_next = False
            if not self._refresh_owed and self.use_wait:
                return self._issue(lane, Wait(self._listed_ids, self.hold_ms))
            # The heartbeat lane saw a new epoch after the last list, or the
            # client turned the wait off since (it became busy): a hold would
            # delay what it now needs, so pace by the interval instead.
            self._list_at = now_ms + self.poll_interval_ms
        if now_ms >= self._list_at:
            return self._issue(lane, ListInbox())
        wake_at = self._list_at
        if self._refresh_owed and not self._refresh_in_flight:
            wake_at = min(wake_at, self._refresh_at)
        return Sleep(wake_at - now_ms)

    def poll_done(self, action: Request, outcome: Outcome, now_ms: int) -> InboxChange | None:
        self._settle(self._poll, action)
        if self._terminal(outcome):
            return None
        if isinstance(action, Register):
            self._register_done(outcome, now_ms)
        elif isinstance(action, Refresh):
            self._refresh_done(outcome, now_ms)
        elif isinstance(action, ListInbox):
            return self._list_done(outcome, now_ms)
        elif isinstance(action, Wait):
            self._wait_done(outcome, now_ms)
        return None

    def heartbeat_next(self, now_ms: int) -> Action:
        lane = self._heartbeat
        if lane.outstanding is not None:
            raise RuntimeError(f"heartbeat lane already waits on {lane.outstanding.op}")
        if self._quiet:
            return Quiet()
        if not self._registered:
            return Sleep(self.heartbeat_interval_ms)
        # The heartbeat lane may refresh too: the poll lane can be inside a
        # 5 s hold, and liveness must not wait for it.
        if self._refresh_due(now_ms):
            self._refresh_in_flight = True
            return self._issue(lane, Refresh())
        if now_ms < self._heartbeat_at:
            return Sleep(self._heartbeat_at - now_ms)
        return self._issue(lane, Heartbeat())

    def heartbeat_done(self, action: Request, outcome: Outcome, now_ms: int) -> None:
        self._settle(self._heartbeat, action)
        if self._terminal(outcome):
            return
        if isinstance(action, Refresh):
            self._refresh_done(outcome, now_ms)
            return
        self._heartbeat_at = now_ms + self.heartbeat_interval_ms
        if isinstance(outcome, Ok):
            self._observe_epoch(outcome.response)
        elif isinstance(outcome, Rejected) and outcome.code == ipc_errors.STALE_DAEMON_GENERATION:
            self._owe_refresh("epoch")

    # -- transitions -----------------------------------------------------

    def _issue(self, lane: _Lane, action: Request) -> Request:
        lane.outstanding = action
        return action

    def _settle(self, lane: _Lane, action: Request) -> None:
        if lane.outstanding is not action:
            raise RuntimeError(f"{action.op} completed but was not outstanding")
        lane.outstanding = None
        if isinstance(action, Refresh):
            self._refresh_in_flight = False

    def _terminal(self, outcome: Outcome) -> bool:
        if not isinstance(outcome, Rejected):
            return False
        if outcome.code == ipc_errors.SESSION_SUPERSEDED:
            # A newer session owns the actor: re-registering would steal it
            # back and flap delivery between the two.
            self._quiet = True
            return True
        if outcome.code == ipc_errors.STALE_SESSION:
            self._registered = False
            self._register_at = 0
            self._refresh_owed = False
            return True
        return False

    def _refresh_due(self, now_ms: int) -> bool:
        return self._refresh_owed and not self._refresh_in_flight and now_ms >= self._refresh_at

    def _owe_refresh(self, reason: str) -> None:
        self._refresh_owed = True
        self._rewake_reason = self._rewake_reason or reason

    def _observe_epoch(self, response: Mapping[str, Any]) -> None:
        epoch = response.get("daemonEpoch")
        if not isinstance(epoch, str) or not epoch:
            return
        if self._wait_supported is False and epoch != self._wait_refused_epoch:
            self._wait_supported = None
        if self._epoch is None:
            # An idempotent register can answer without an epoch
            # (session_actor/generation.py): adopt the first one seen, or no
            # later restart could ever be detected (review 827 on #1148).
            # Registration itself overwrites the epoch, so no guard is needed
            # for an epoch seen before it.
            self._epoch = epoch
            return
        if epoch != self._epoch:
            self._owe_refresh("epoch")

    def _register_done(self, outcome: Outcome, now_ms: int) -> None:
        if not isinstance(outcome, Ok):
            self._register_failures += 1
            self._register_at = now_ms + backoff_ms(self.poll_interval_ms, self._register_failures)
            return
        epoch = outcome.response.get("daemonEpoch")
        self._epoch = epoch if isinstance(epoch, str) and epoch else None
        self._registered = True
        self._register_failures = 0
        self._refresh_owed = False
        self._refresh_failures = 0
        self._list_at = 0
        self._wait_next = False
        self._heartbeat_at = now_ms + self.heartbeat_interval_ms
        self._connected_at = now_ms
        if self._registered_once:
            self._rewake_reason = self._rewake_reason or "start"
        self._registered_once = True

    def _refresh_done(self, outcome: Outcome, now_ms: int) -> None:
        if not isinstance(outcome, Ok):
            # Polling and delivery go on; only the refresh backs off.
            self._refresh_failures += 1
            self._refresh_at = now_ms + backoff_ms(self.poll_interval_ms, self._refresh_failures)
            return
        epoch = outcome.response.get("daemonEpoch")
        if isinstance(epoch, str) and epoch:
            self._epoch = epoch
        self._refresh_owed = False
        self._refresh_failures = 0
        self._refresh_at = 0
        # The owed re-wake rides on the very next list.
        self._list_at = 0
        self._wait_next = False

    def _wait_done(self, outcome: Outcome, now_ms: int) -> None:
        if isinstance(outcome, Ok):
            self._wait_supported = True
            self._observe_epoch(outcome.response)
            if outcome.response.get("held") is True:
                self._count("wait.held_changed" if outcome.response.get("changed") is True else "wait.held_timeout")
                # A held wait already spent the pacing time.
                self._list_at = now_ms
                return
            self._count("wait.unheld")
        elif isinstance(outcome, Rejected) and outcome.code == ipc_errors.METHOD_NOT_FOUND:
            self._count("method_not_found")
            self._wait_supported = False
            self._wait_refused_epoch = self._epoch
        # Not held, refused or failed: the ordinary interval, never a spin.
        self._list_at = now_ms + self.poll_interval_ms

    def _list_done(self, outcome: Outcome, now_ms: int) -> InboxChange | None:
        if not isinstance(outcome, Ok):
            self._list_failures += 1
            self._list_at = now_ms + backoff_ms(self.poll_interval_ms, self._list_failures)
            return None
        self._count("list")
        self._list_failures = 0
        response = outcome.response
        self._observe_epoch(response)
        rows = response.get("messages", ())
        listed: dict[str, Mapping[str, Any]] = {}
        for row in rows if isinstance(rows, Sequence) and not isinstance(rows, str) else ():
            message_id = row.get("messageId") if isinstance(row, Mapping) else None
            if not isinstance(message_id, str) or not message_id:
                self._count("deliver.invalid")
                continue
            listed[message_id] = row
        new = tuple(row for message_id, row in listed.items() if message_id not in self._seen)
        rewake: tuple[Mapping[str, Any], ...] = ()
        reason = None
        if self._rewake_reason is not None and not self._refresh_owed:
            reason = self._rewake_reason
            self._rewake_reason = None
            self._count(f"rewake.{reason}")
            rewake = tuple(row for message_id, row in listed.items() if message_id in self._seen)
        completed = tuple(message_id for message_id in self._seen if message_id not in listed)
        self._count("deliver.new", len(new))
        self._count("deliver.dedup", len(listed) - len(new))
        self._seen = listed
        self._listed_ids = tuple(listed)
        if (new or rewake) and self._connected_at is not None:
            # The first registration only; a re-register does not reset it.
            self.counters.setdefault("first_wake_ms_after_connect", now_ms - self._connected_at)
            self._connected_at = None
        waitable = self.use_wait and self._wait_supported is not False and not self._refresh_owed
        if waitable:
            self._wait_next = True
        else:
            self._list_at = now_ms + self.poll_interval_ms
        return InboxChange(new, rewake, reason, completed)

    def _count(self, name: str, amount: int = 1) -> None:
        if amount:
            self.counters[name] = self.counters.get(name, 0) + amount
