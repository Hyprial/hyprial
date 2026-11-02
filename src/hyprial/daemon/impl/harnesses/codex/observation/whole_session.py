"""Whole-terminal Codex turn observation from the app-server's own events.

Decision record (design §5 E2): the interactive launcher starts the app-server
and the TUI joins it as a client, so the carrier shares one app-server with the
user's terminal. Native ``turn/started``/``turn/completed`` notifications reach
another client only after it subscribes with ``thread/resume``; while
unsubscribed, ``thread/status/changed`` is still broadcast. Both are native
facts, never timing inference, and ``thread/read`` resynchronises identity after
a subscribe or a missed boundary.

One native turn is one observation identity: the native turn id is the report
id. Turns the carrier itself injected are excluded, because its settlement path
already reports them with the delivery receipt that must stay unchanged.
Nothing here interrupts, restarts or times out a turn, and an unobserved
terminal status is never reported as completed.
"""

from __future__ import annotations

import queue
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from typing import Literal

TurnReport = Callable[[str, str, "str | None"], None]
LogSink = Callable[[str, str], None]

_EVENT_QUEUE_CAPACITY = 4096
_REPORT_MEMORY = 512
_END_STATUSES = frozenset({"completed", "failed", "interrupted"})
_OWNERSHIP_WAIT_SECONDS = 30.0
_WAKE = object()


def native_turn_status(status: object) -> Literal["completed", "failed"]:
    """Map a native terminal status onto the daemon's two accepted end states."""

    return "completed" if status == "completed" else "failed"


class CarrierTurnOwnership:
    """Race-free "did the carrier itself start this native turn?".

    A start is claimed before the turn/start RPC is sent and released once the
    native turn id is known. An observer handed that turn's notification waits
    for the claim instead of racing it, so a carrier-injected turn is only ever
    reported by the settlement path that carries its delivery receipt.
    """

    def __init__(self, *, wait_seconds: float = _OWNERSHIP_WAIT_SECONDS) -> None:
        self._wait_seconds = wait_seconds
        self._cv = threading.Condition()
        self._pending = 0
        self._turns: OrderedDict[str, None] = OrderedDict()

    def begin_start(self) -> None:
        with self._cv:
            self._pending += 1

    def commit_start(self, turn_id: str) -> None:
        with self._cv:
            self._turns[turn_id] = None
            while len(self._turns) > _REPORT_MEMORY:
                self._turns.popitem(last=False)
            self._pending = max(0, self._pending - 1)
            self._cv.notify_all()

    def abort_start(self) -> None:
        with self._cv:
            self._pending = max(0, self._pending - 1)
            self._cv.notify_all()

    def owns(self, turn_id: str) -> bool:
        """Answer once no carrier start can still be in flight."""

        deadline = time.monotonic() + self._wait_seconds
        with self._cv:
            while self._pending:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return True
                self._cv.wait(min(remaining, 0.25))
            return turn_id in self._turns


class WholeTerminalTurns:
    """Subscribe to one TUI thread and relay its native turn boundaries."""

    def __init__(
        self,
        *,
        thread_id: str,
        subscribe: Callable[[], dict[str, object]],
        read_thread: Callable[[], dict[str, object]],
        record: TurnReport,
        owned: Callable[[str], bool] | None = None,
        retry_seconds: float = 1.0,
        log: LogSink | None = None,
    ) -> None:
        self._thread_id = thread_id
        self._subscribe = subscribe
        self._read_thread = read_thread
        self._record = record
        self._owned = owned
        self._retry_seconds = retry_seconds
        self._log = log
        self._events: queue.Queue[object] = queue.Queue(
            maxsize=_EVENT_QUEUE_CAPACITY
        )
        self._stop = threading.Event()
        self._subscribed = threading.Event()
        self._coverage_lock = threading.Lock()
        self._loss_revision = 0
        self._gap_started_ms: int | None = None
        self._coverage_gaps = 0
        self._next_subscribe_at = 0.0
        self._open: OrderedDict[str, None] = OrderedDict()
        self._reported: OrderedDict[tuple[str, str], None] = OrderedDict()
        self._pending_report: tuple[str, str, str | None] | None = None
        self._worker = threading.Thread(
            target=self._run,
            name="hyprial-codex-whole-terminal",
            daemon=True,
        )

    @property
    def subscribed(self) -> bool:
        return self._subscribed.is_set()

    @property
    def coverage_gaps(self) -> int:
        with self._coverage_lock:
            return self._coverage_gaps

    def _invalidate(self) -> None:
        with self._coverage_lock:
            self._subscribed.clear()
            self._loss_revision += 1
            if self._gap_started_ms is None:
                self._gap_started_ms = time.time_ns() // 1_000_000
                self._coverage_gaps += 1

    def start(self) -> None:
        self._worker.start()

    def stop(self) -> None:
        self._stop.set()
        self._invalidate()
        self._enqueue(_WAKE)
        if self._worker.is_alive():
            self._worker.join(timeout=5.0)

    def notify(self, message: dict[str, object]) -> None:
        """Transport listener entry point: non-blocking, ordered, bounded."""

        if message.get("method") == "_hyprial/connection-lost":
            self._invalidate()
            self._enqueue(None)
            return
        self._enqueue(message)

    def _enqueue(self, item: object) -> None:
        try:
            self._events.put_nowait(item)
        except queue.Full:
            self._invalidate()

    def _run(self) -> None:
        while not self._stop.is_set():
            if self._pending_report is not None and not self._apply(None):
                if self._stop.wait(self._retry_seconds):
                    return
                continue
            if not self._subscribed.is_set() and self._events.empty():
                if time.monotonic() >= self._next_subscribe_at:
                    self._subscribe_once()
                    self._next_subscribe_at = time.monotonic() + self._retry_seconds
                if self._stop.is_set():
                    return
                if self._pending_report is not None:
                    continue
            timeout = None if self._subscribed.is_set() else self._retry_seconds
            try:
                item = self._events.get(timeout=timeout)
            except queue.Empty:
                continue
            if item is _WAKE or self._stop.is_set():
                return
            if isinstance(item, dict):
                while not self._apply(item):
                    if self._stop.wait(self._retry_seconds):
                        return

    def _apply(self, message: dict[str, object] | None) -> bool:
        """Accept the pending boundary before applying any later live event."""

        try:
            self._redrive_pending()
            if message is not None:
                self._handle(message)
            return True
        except Exception as error:  # noqa: BLE001 - observation must survive a sink
            self._invalidate()
            self._log_event("codex.whole-terminal.event-failed", repr(error))
            return False

    def _subscribe_once(self) -> None:
        with self._coverage_lock:
            revision = self._loss_revision
        try:
            self._redrive_pending()
            thread = self._subscribe()
            self._reconcile(thread)
        except Exception as error:  # noqa: BLE001 - subscription and adoption boundary
            self._invalidate()
            self._log_event("codex.whole-terminal.unsubscribed", repr(error))
            return
        with self._coverage_lock:
            if revision != self._loss_revision or self._stop.is_set():
                return
            gap_started = self._gap_started_ms
            self._gap_started_ms = None
            self._subscribed.set()
        if gap_started is not None:
            self._log_event(
                "codex.whole-terminal.coverage-gap",
                f"startedAtMs={gap_started} endedAtMs={time.time_ns() // 1_000_000}",
            )

    def _handle(self, message: dict[str, object]) -> None:
        """Apply one native event for the registered thread only.

        An active status seen while unsubscribed retries the subscription at
        once: a brand-new thread has no rollout to resume yet, so its first
        turn would otherwise wait out the retry interval.
        """

        method = message.get("method")
        params = message.get("params")
        if not isinstance(params, dict) or params.get("threadId") != self._thread_id:
            return
        turn = params.get("turn")
        if method in {"turn/started", "turn/completed"} and isinstance(turn, dict):
            turn_id = turn.get("id")
            if not isinstance(turn_id, str):
                return
            if method == "turn/started":
                if turn.get("status") == "inProgress":
                    self._observe_start(turn_id)
            else:
                self._observe_end(turn_id, turn.get("status"))
            return
        if method != "thread/status/changed":
            return
        status = params.get("status")
        phase = status.get("type") if isinstance(status, dict) else None
        if phase == "active" and not self._subscribed.is_set():
            self._next_subscribe_at = 0.0
            return
        if phase == "active" and not self._open:
            self._resync(reason="active")
        elif phase != "active" and self._open:
            self._resync(reason="idle")

    def _adopt(self, thread: dict[str, object]) -> None:
        for turn in _turns(thread):
            turn_id = turn.get("id")
            if isinstance(turn_id, str) and turn.get("status") == "inProgress":
                self._observe_start(turn_id)

    def _resync(self, *, reason: str) -> None:
        if not self._subscribed.is_set():
            return
        thread = self._read_thread()
        self._reconcile(thread)

    def _reconcile(self, thread: dict[str, object]) -> None:
        current = {
            turn["id"]
            for turn in _turns(thread)
            if isinstance(turn.get("id"), str) and turn.get("status") == "inProgress"
        }
        for turn_id in tuple(self._open):
            if turn_id not in current:
                self._close_from_thread(thread, turn_id)
        self._adopt(thread)

    def _close_from_thread(self, thread: dict[str, object], turn_id: str) -> None:
        """Close a stale open turn from the native view, never as completed."""

        for turn in _turns(thread):
            if turn.get("id") == turn_id:
                status = turn.get("status")
                if status in _END_STATUSES:
                    self._observe_end(turn_id, status)
                return
        self._observe_end(turn_id, None)

    def _observe_start(self, turn_id: str) -> None:
        if self._owned is not None and self._owned(turn_id):
            return
        if (turn_id, "started") in self._reported or (turn_id, "ended") in self._reported:
            return
        self._submit(turn_id, "started", None)

    def _observe_end(self, turn_id: str, status: object) -> None:
        if turn_id not in self._open:
            return
        if (turn_id, "ended") in self._reported:
            return
        self._submit(turn_id, "ended", native_turn_status(status))

    def _redrive_pending(self) -> None:
        if self._pending_report is not None:
            self._submit(*self._pending_report)

    def _submit(self, turn_id: str, phase: str, status: str | None) -> None:
        report = (turn_id, phase, status)
        if self._pending_report is not None and self._pending_report != report:
            raise RuntimeError("pending turn boundary must be accepted first")
        self._pending_report = report
        self._record(turn_id, phase, status)
        self._remember(turn_id, phase)
        if phase == "started":
            self._open[turn_id] = None
            while len(self._open) > _REPORT_MEMORY:
                self._open.popitem(last=False)
        else:
            self._open.pop(turn_id, None)
        self._pending_report = None

    def _remember(self, turn_id: str, phase: str) -> bool:
        key = (turn_id, phase)
        if key in self._reported:
            return False
        self._reported[key] = None
        while len(self._reported) > _REPORT_MEMORY:
            self._reported.popitem(last=False)
        return True

    def _log_event(self, event: str, detail: str) -> None:
        if self._log is not None:
            self._log(event, detail)


def _turns(thread: dict[str, object]) -> list[dict[str, object]]:
    turns = thread.get("turns")
    if not isinstance(turns, list):
        return []
    return [turn for turn in turns if isinstance(turn, dict)]
