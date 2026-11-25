"""Teleport wire constants and the ``TELEPORT_*`` error type.

Decisions:

- The 20-code set is frozen (slice 2 wire plus the 2026-11-12 ruling); later slices only
  raise codes from this set, so receivers and senders never invent new ones.
- ``TELEPORT_OPS`` is the ``KeySpace`` operation tuple, shared rather than
  copied so the key layout and the frame validator cannot drift apart.
"""

from __future__ import annotations

from hyprial.daemon.impl.transport.keys import AGENT_TELEPORT_OPS

TELEPORT_VERSION = 1
TELEPORT_CHUNK_BYTES = 768_000
TELEPORT_OPS = AGENT_TELEPORT_OPS
TELEPORT_ERROR_CODES = frozenset(
    {
        "TELEPORT_BAD_FRAME",
        "TELEPORT_WRONG_TARGET",
        "TELEPORT_UNKNOWN_OPERATION",
        "TELEPORT_OPERATION_CONFLICT",
        "TELEPORT_OFFSET_GAP",
        "TELEPORT_CHUNK_TOO_LARGE",
        "TELEPORT_CHUNK_DIGEST_MISMATCH",
        "TELEPORT_STREAM_MISMATCH",
        "TELEPORT_INCOMPLETE",
        "TELEPORT_DIGEST_MISMATCH",
        "TELEPORT_AGENT_EXISTS",
        "TELEPORT_ROUTINE_CONFLICT",
        "TELEPORT_RUNTIME_UNSUPPORTED",
        "TELEPORT_OWNER_MISMATCH",
        "TELEPORT_STRICT_RESUME_FAILED",
        "TELEPORT_IN_FLIGHT_TIMEOUT",
        "TELEPORT_SOURCE_CLEANUP_FAILED",
        "TELEPORT_UNRESOLVED",
        "TELEPORT_LANDING_FAILED",
        "TELEPORT_SOURCE_NOT_QUIESCED",
    }
)


class TeleportError(Exception):
    """A teleport failure carrying a frozen ``TELEPORT_*`` code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
