"""Typed, secret-free Slack platform failures and HTTP outcomes."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..common.transport import HttpEffectObservation


@dataclass(frozen=True, slots=True)
class SlackHttpResult:
    observation: HttpEffectObservation
    retry_after_ms: int | None
    error_code: str | None


class SlackProtocolError(RuntimeError):
    """A stable platform-local failure that never embeds credentials."""

    def __init__(
        self,
        code: str,
        message: str | None = None,
        *,
        close_observation: Any = None,
    ) -> None:
        if not code:
            raise ValueError("Slack error code is required")
        super().__init__(f"{code}: {message}" if message else code)
        self.code = code
        self.close_observation = close_observation


__all__ = ["SlackHttpResult", "SlackProtocolError"]
