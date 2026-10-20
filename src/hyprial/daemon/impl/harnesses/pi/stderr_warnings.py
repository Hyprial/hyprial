"""Live forwarding of the controlled pi loader's stderr warnings.

The client keeps a bounded ``stderrTail`` for post-mortem records, but that
buffer is read only after the process exits.  A resident headless worker that
rebinds a session's provider must say so while an operator can still act, so
complete stderr lines are scanned as they arrive and the one known warning is
mirrored into the shared worker log.

The prefix is a stable contract with ``pi_controlled_loader.mjs``.  The JSON
body carries session/provider/model ids only; this reader copies named fields
and never echoes the raw line, so nothing else on stderr can smuggle a secret
into the log.
"""

from __future__ import annotations

import json
from typing import Any, Protocol

PI_LOADER_WARNING_PREFIX = b"PI_CONTROLLED_LOADER_WARNING "
#: The events this reader understands.  An unknown event stays on the tail
#: only: forwarding one this code does not name would let the loader add a
#: field that reaches the log without review.
_KNOWN_EVENTS = frozenset({"session.provider.rebound"})
_REBIND_FIELDS = (
    "sessionId",
    "recordedProvider",
    "recordedModel",
    "configuredProvider",
    "configuredModel",
)


class _WarningLogger(Protocol):
    def log(self, level: str, event: str, **fields: Any) -> None: ...


class PiStderrTail(bytearray):
    """A bounded stderr tail that forwards loader warnings as it fills.

    The tail is post-mortem only, so the forwarding rides the one place every
    chunk passes through: :meth:`extend`.  Slicing (``del tail[:n]``), the
    ``bytes(tail)`` snapshot and ``tail.decode()`` keep working unchanged.
    """

    def __init__(self, logger: _WarningLogger | None, *, limit: int) -> None:
        super().__init__()
        self._logger = logger
        self._pending = bytearray()
        self._limit = limit

    def extend(self, chunk: bytes) -> None:
        super().extend(chunk)
        if len(self) > self._limit:
            del self[: len(self) - self._limit]
        if self._logger is None:
            return
        pending = self._pending
        pending.extend(chunk)
        newline = pending.find(b"\n")
        while newline >= 0:
            line = bytes(pending[:newline])
            del pending[: newline + 1]
            self._record(line)
            newline = pending.find(b"\n")
        if len(pending) > self._limit:
            del pending[: len(pending) - self._limit]

    def _record(self, line: bytes) -> None:
        stripped = line.strip()
        if not stripped.startswith(PI_LOADER_WARNING_PREFIX):
            return
        payload = stripped[len(PI_LOADER_WARNING_PREFIX) :].strip()
        try:
            record = json.loads(payload)
        except json.JSONDecodeError:
            return
        if not isinstance(record, dict):
            return
        event = record.get("event")
        if not isinstance(event, str) or event not in _KNOWN_EVENTS:
            return
        fields = {
            key: record[key]
            for key in _REBIND_FIELDS
            if isinstance(record.get(key), str)
        }
        logger = self._logger
        if logger is None:
            return
        logger.log("warn", event, **fields)
