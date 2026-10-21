"""Bounded single-frame reads and rate limits for the pairing server.

The S1 parser already enforces the 8192-byte frame ceiling, duplicate-key and
unknown-field rejection.  This module adds the transport-side bound before
parsing, so a peer that never sends a newline cannot grow an unbounded
buffer.  Limits are deliberately conservative because the M11 contract does
not pin exact production values yet.
"""

from __future__ import annotations

import math
import ssl
import threading
import time
from collections import deque

from ..protocol import MAX_FRAME_BYTES, PairingError, parse_frame

DEFAULT_MAX_CONNECTIONS = 32
DEFAULT_HANDSHAKE_TIMEOUT_SECONDS = 10.0
DEFAULT_IDLE_TIMEOUT_SECONDS = 15.0
DEFAULT_FRAME_RATE_LIMIT = 120
DEFAULT_FRAME_RATE_WINDOW_SECONDS = 60.0
MAX_CLIENT_FRAMES_PER_CONNECTION = 3
MAX_CLIENT_OPERATIONS_PER_CONNECTION = 1
_READ_CHUNK_BYTES = 4096
_ERROR_DRAIN_SECONDS = 0.05


def _frame_invalid(field: str | None = None) -> PairingError:
    return PairingError(
        "pairing-frame-invalid", "pairing frame is invalid", field=field
    )


def _positive_seconds(value: object, field: str) -> float:
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(value)
        or value <= 0
    ):
        raise ValueError(f"{field} must be a positive finite number")
    return float(value)


def read_line(
    conn: object,
    *,
    timeout: object,
    max_bytes: int = MAX_FRAME_BYTES,
) -> bytes:
    """Read exactly one newline-terminated line with one hard byte ceiling.

    ``timeout`` bounds the whole read, not each ``recv``.  A connection that
    sends a partial or oversized line is refused with a protocol error; a
    peer that stalls is refused with :class:`TimeoutError`; a peer that
    closes before a complete line is refused with :class:`EOFError`.
    """

    deadline_seconds = _positive_seconds(timeout, "timeout")
    if type(max_bytes) is not int or max_bytes < 1:
        raise ValueError("max_bytes must be a positive integer")
    deadline = time.monotonic() + deadline_seconds
    buffer = bytearray()
    while True:
        if len(buffer) > max_bytes:
            raise _frame_invalid("frame")
        newline = buffer.find(b"\n")
        if newline != -1:
            if newline + 1 != len(buffer):
                raise _frame_invalid("trailing bytes")
            return bytes(buffer)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("pairing frame deadline elapsed")
        try:
            conn.settimeout(remaining)
            chunk = conn.recv(_READ_CHUNK_BYTES)
        except TimeoutError as error:
            raise TimeoutError("pairing frame deadline elapsed") from error
        if not chunk:
            raise EOFError("pairing peer closed")
        buffer.extend(chunk)


def read_frame(
    conn: object,
    *,
    timeout: object = DEFAULT_IDLE_TIMEOUT_SECONDS,
) -> dict:
    """Read and parse exactly one S1 frame from a TLS channel."""

    return parse_frame(read_line(conn, timeout=timeout))


def drain_bounded(
    conn: object,
    *,
    timeout: float = _ERROR_DRAIN_SECONDS,
    max_bytes: int = MAX_FRAME_BYTES,
) -> None:
    """Discard a bounded amount of pending input before closing.

    Sending an error and immediately closing a socket that still has unread
    input can turn the close into a TCP reset, hiding the error frame from the
    peer.  This best-effort drain is short, bounded and never logged.
    """

    try:
        deadline_seconds = _positive_seconds(timeout, "timeout")
    except ValueError:
        return
    deadline = time.monotonic() + deadline_seconds
    remaining_bytes = max_bytes
    while remaining_bytes > 0:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        try:
            conn.settimeout(remaining)
            chunk = conn.recv(min(_READ_CHUNK_BYTES, remaining_bytes))
        except (TimeoutError, OSError, ssl.SSLError):
            return
        if not chunk:
            return
        remaining_bytes -= len(chunk)


class FrameRateLimiter:
    """A thread-safe rolling-window cap shared by all pairing channels."""

    def __init__(self, *, limit: int, window_seconds: object) -> None:
        if type(limit) is not int or limit < 1:
            raise ValueError("limit must be a positive integer")
        self.limit = limit
        self.window_seconds = _positive_seconds(window_seconds, "window")
        self._lock = threading.Lock()
        self._events: deque[float] = deque()

    def _prune(self, now: float) -> None:
        cutoff = now - self.window_seconds
        while self._events and self._events[0] <= cutoff:
            self._events.popleft()

    def allow(self, now: float) -> bool:
        with self._lock:
            self._prune(now)
            if len(self._events) >= self.limit:
                return False
            self._events.append(now)
            return True

    def retry_after_seconds(self, now: float) -> int:
        with self._lock:
            self._prune(now)
            if len(self._events) < self.limit:
                return 1
            wait = self._events[0] + self.window_seconds - now
            return max(1, min(3_600, math.ceil(wait)))


__all__ = [
    "DEFAULT_FRAME_RATE_LIMIT",
    "DEFAULT_FRAME_RATE_WINDOW_SECONDS",
    "DEFAULT_HANDSHAKE_TIMEOUT_SECONDS",
    "DEFAULT_IDLE_TIMEOUT_SECONDS",
    "DEFAULT_MAX_CONNECTIONS",
    "FrameRateLimiter",
    "MAX_CLIENT_FRAMES_PER_CONNECTION",
    "MAX_CLIENT_OPERATIONS_PER_CONNECTION",
    "drain_bounded",
    "read_frame",
    "read_line",
]
