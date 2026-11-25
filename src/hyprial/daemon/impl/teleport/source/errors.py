"""The failure a teleport run reports: a code, a message and optional data."""

from __future__ import annotations

from typing import Any

from hyprial.daemon.impl.teleport.errors import TeleportError


class TeleportRunError(TeleportError):
    """A refused or failed ``teleport.run``.

    ``code`` is a frozen ``TELEPORT_*`` code, or a code the daemon already owns
    (``AGENT_NOT_FOUND``).  ``data`` is a JSON object for the two outcomes an
    operator must follow up by hand, ``TELEPORT_UNRESOLVED`` and
    ``TELEPORT_SOURCE_CLEANUP_FAILED``, and ``None`` otherwise.
    """

    def __init__(
        self, code: str, message: str, data: dict[str, Any] | None = None
    ) -> None:
        super().__init__(code, message)
        self.data = data
