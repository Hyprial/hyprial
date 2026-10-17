"""The recap writer: append-only ``state/recaps.jsonl`` owned per agent home.

Moved verbatim from the retired ``turn_delivery`` turn-hook service (H2 of the
unified-hooks design): one background writer thread, a bounded queue, per-path
append locks, ``0600`` files and ``fsync`` per line.  Behaviour guarded by the
design-turn-hooks-recap §7 suite is unchanged: append-only, one record per
enqueued item, never on the caller's thread, drained by ``close``.
"""

from __future__ import annotations

import json
import os
import queue
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

DEFAULT_RECAP_QUEUE_SIZE = 256
RECAP_OVERFLOW_LOG_INTERVAL_SECONDS = 60.0

LogSink = Callable[..., None]


@dataclass(frozen=True, slots=True)
class RecapWrite:
    """One pending recap line, with the fields logging needs on failure."""

    home: Path
    value: Mapping[str, Any]
    actor: str
    delivery_id: str


class RecapWriter:
    """Serialize recap appends off the caller's thread, bounded and counted."""

    def __init__(
        self,
        *,
        logger: LogSink | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        queue_size: int = DEFAULT_RECAP_QUEUE_SIZE,
    ) -> None:
        if queue_size < 1:
            raise ValueError("recap_queue_size must be positive")
        self._logger = logger
        self._monotonic = monotonic
        self._append_locks: dict[Path, threading.Lock] = {}
        self._append_locks_lock = threading.Lock()
        self._writes: "queue.Queue[RecapWrite]" = queue.Queue(maxsize=queue_size)
        self._writer: threading.Thread | None = None
        self._writer_lock = threading.Lock()
        self._wake = threading.Event()
        self._closing = False
        self.overflow_count = 0
        self._overflow_logged_at: float | None = None

    def enqueue(self, item: RecapWrite) -> bool:
        with self._writer_lock:
            if self._closing:
                closed = True
            else:
                closed = False
                try:
                    self._writes.put_nowait(item)
                except queue.Full:
                    now = self._monotonic()
                    self.overflow_count += 1
                    previous = self._overflow_logged_at
                    should_log = (
                        previous is None
                        or now - previous >= RECAP_OVERFLOW_LOG_INTERVAL_SECONDS
                    )
                    if should_log:
                        self._overflow_logged_at = now
                    dropped = self.overflow_count
                else:
                    self._wake.set()
                    if self._writer is None or not self._writer.is_alive():
                        self._writer = threading.Thread(
                            target=self._write_recaps,
                            name="hyprial-recap-writer",
                            daemon=True,
                        )
                        self._writer.start()
                    return True
        if closed:
            if self._logger is not None:
                self._logger(
                    "info",
                    "daemon",
                    "hooks.recap_after_close",
                    agent=item.actor,
                    deliveryId=item.delivery_id,
                )
            return False
        if should_log and self._logger is not None:
            self._logger(
                "warn",
                "daemon",
                "hooks.recap_overflow",
                agent=item.actor,
                deliveryId=item.delivery_id,
                dropped=dropped,
            )
        return False

    def close(self, timeout: float = 5.0) -> bool:
        """Drain queued writes and stop the writer within ``timeout``."""

        if timeout < 0:
            raise ValueError("recap writer close timeout must not be negative")
        deadline = time.monotonic() + timeout
        with self._writer_lock:
            self._closing = True
            writer = self._writer
            self._wake.set()
        if writer is None:
            return self._writes.unfinished_tasks == 0
        writer.join(max(0.0, deadline - time.monotonic()))
        with self._writer_lock:
            stopped = not writer.is_alive()
            if stopped and self._writer is writer:
                self._writer = None
        return stopped and self._writes.unfinished_tasks == 0

    def append(self, home: Path, value: Mapping[str, Any]) -> None:
        """Append one line synchronously (used by tests and diagnostics)."""

        self._append(home, value)

    def _write_recaps(self) -> None:
        while True:
            try:
                item = self._writes.get_nowait()
            except queue.Empty:
                self._wake.clear()
                with self._writer_lock:
                    # Re-check the queue before honouring close: recaps can be
                    # enqueued while this thread waits for the lock, and
                    # close() only stops new enqueues, it does not drop them.
                    if not self._writes.empty():
                        continue
                    if self._closing:
                        self._writer = None
                        return
                self._wake.wait(0.5)
                with self._writer_lock:
                    if self._writes.empty():
                        self._writer = None
                        return
                continue
            try:
                self._append(item.home, item.value)
            except OSError as error:
                if self._logger is not None:
                    self._logger(
                        "warn",
                        "daemon",
                        "hooks.recap_write_failed",
                        agent=item.actor,
                        deliveryId=item.delivery_id,
                        detail=str(error)[:300],
                    )
            finally:
                self._writes.task_done()

    def _append(self, home: Path, value: Mapping[str, Any]) -> None:
        path = home / "state" / "recaps.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self._append_locks_lock:
            lock = self._append_locks.setdefault(path, threading.Lock())
        encoded = (
            json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n"
        ).encode()
        with lock:
            descriptor = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
            with os.fdopen(descriptor, "ab", closefd=True) as target:
                os.fchmod(target.fileno(), 0o600)
                target.write(encoded)
                target.flush()
                os.fsync(target.fileno())


def recent_recaps(home: Path, count: int) -> list[object]:
    """Read the last ``count`` records, skipping torn lines."""

    if count == 0:
        return []
    path = home / "state" / "recaps.jsonl"
    try:
        with path.open(encoding="utf-8") as source:
            lines = tuple(deque(source, maxlen=count))
    except FileNotFoundError:
        return []
    except (OSError, UnicodeError):
        return []
    values: list[object] = []
    for line in lines:
        try:
            values.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return values


__all__ = [
    "DEFAULT_RECAP_QUEUE_SIZE",
    "RECAP_OVERFLOW_LOG_INTERVAL_SECONDS",
    "RecapWrite",
    "RecapWriter",
    "recent_recaps",
]
