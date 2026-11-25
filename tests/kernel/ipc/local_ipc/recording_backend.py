"""A pass-through local IPC backend that records every call it serves.

Injection sites prove they are behind the seam by asserting the recorded
endpoint instead of the transport: a call site that still binds or connects
with a raw socket leaves the recorder empty and the assertion fails.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from hyprial.kernel.impl.manysys.ipc import active_local_ipc_backend


class RecordingBackend:
    """Wrap the active default backend and record each entry-point call."""

    def __init__(self, inner: Any | None = None) -> None:
        self._inner = inner if inner is not None else active_local_ipc_backend()
        self.connects: list[Path] = []
        self.listens: list[tuple[Path, int]] = []

    def listen(
        self, endpoint: Path, *, backlog: int, gui_write_sid: str | None
    ) -> Any:
        self.listens.append((Path(endpoint), backlog))
        return self._inner.listen(
            endpoint, backlog=backlog, gui_write_sid=gui_write_sid
        )

    def connect(
        self, endpoint: Path, timeout: float, *, clock: Any | None = None
    ) -> Any:
        self.connects.append(Path(endpoint))
        return self._inner.connect(endpoint, timeout, clock=clock)

    def path_limit(self) -> int | None:
        return self._inner.path_limit()
