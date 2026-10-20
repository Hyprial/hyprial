"""Independent bounded readers for the panes in ``org.network``."""

# hyprial-unbounded-wait-allow: get - fixed daemon workers consume until process exit

from __future__ import annotations

import queue
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class _ProjectionWork:
    reader: Callable[[], Any]
    ready: threading.Event = field(default_factory=threading.Event)
    value: Any = None
    error: Exception | None = None


class _ProjectionLane:
    """One fixed worker and one queued read for a status source."""

    def __init__(self, name: str) -> None:
        self._queue: queue.Queue[_ProjectionWork] = queue.Queue(maxsize=1)
        threading.Thread(
            target=self._run,
            name=f"org-network-{name}",
            daemon=True,
        ).start()

    def submit(self, reader: Callable[[], Any]) -> _ProjectionWork | None:
        work = _ProjectionWork(reader)
        try:
            self._queue.put_nowait(work)
        except queue.Full:
            return None
        return work

    def _run(self) -> None:
        while True:
            work = self._queue.get()
            try:
                work.value = work.reader()
            except Exception as error:  # noqa: BLE001 - pane failure is data
                work.error = error
            finally:
                work.ready.set()
                self._queue.task_done()


_PROJECTION_LANES = {
    "directory": _ProjectionLane("directory"),
    "sidecar": _ProjectionLane("sidecar"),
}


class BoundedProjection:
    """One local projection read isolated from the other status panes."""

    def __init__(
        self,
        reader: Callable[[], Any] | None,
        *,
        lane: str,
        timeout_seconds: float,
    ) -> None:
        self._deadline = time.monotonic() + timeout_seconds
        self._reason: str | None = None
        self._work: _ProjectionWork | None = None
        if reader is None:
            self._reason = "not-configured"
            return
        self._work = _PROJECTION_LANES[lane].submit(reader)
        if self._work is None:
            self._reason = "capacity"

    def result(self) -> tuple[Any, dict[str, Any]]:
        if self._reason is not None:
            return None, {"available": False, "reason": self._reason}
        assert self._work is not None
        if not self._work.ready.wait(max(0.0, self._deadline - time.monotonic())):
            return None, {"available": False, "reason": "timeout"}
        if self._work.error is not None:
            return None, {
                "available": False,
                "reason": "error",
                "errorType": type(self._work.error).__name__,
            }
        return self._work.value, {"available": True}


def start_bounded_projection(
    reader: Callable[[], Any] | None,
    *,
    lane: str,
    timeout_seconds: float,
) -> BoundedProjection:
    """Start one capacity-bounded local projection read with an absolute deadline."""

    return BoundedProjection(reader, lane=lane, timeout_seconds=timeout_seconds)
