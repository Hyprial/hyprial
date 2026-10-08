"""GUI lifecycle error type; codes stay stable for scripts parsing --json."""

from __future__ import annotations

from typing import Any


class GuiError(RuntimeError):
    """Stable failure returned through the public CLI error envelope."""

    def __init__(self, code: str, message: str, data: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.data = data or {}
