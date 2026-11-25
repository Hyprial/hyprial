"""Shared Telegram adapter contract types and sealed-payload decode helpers.

Narrow module boundary: the credential contract consumed by the transport,
media and runtime modules, plus the frozen-update/JSON decode helpers they
share. Sibling modules import from here instead of reaching into the Bot API
runtime module, keeping cross-module seams explicit.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

_TOKEN = re.compile(r"^[1-9][0-9]{4,}:[A-Za-z0-9_-]{30,}$")


@dataclass(frozen=True, slots=True)
class TelegramCredentials:
    bot_token: str

    def __post_init__(self) -> None:
        if not _TOKEN.fullmatch(self.bot_token):
            raise ValueError("invalid Telegram bot token")


class TelegramCredentialResolver(Protocol):
    async def resolve(self, credential_ref: str) -> TelegramCredentials: ...


def _thaw(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _thaw(nested) for key, nested in value.items()}
    if isinstance(value, tuple):
        if value and all(
            isinstance(entry, tuple) and len(entry) == 2 and isinstance(entry[0], str)
            for entry in value
        ):
            return {entry[0]: _thaw(entry[1]) for entry in value}
        return [_thaw(nested) for nested in value]
    if isinstance(value, list):
        return [_thaw(nested) for nested in value]
    return value


def _map(value: object, name: str) -> Mapping[str, object]:
    thawed = _thaw(value)
    if not isinstance(thawed, Mapping):
        raise ValueError(f"Telegram {name} must be an object")
    return thawed


def _text(value: object, name: str) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ValueError(f"Telegram {name} is required")
    result = str(value)
    if not result:
        raise ValueError(f"Telegram {name} is required")
    return result


def _integer(value: object, name: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"Telegram {name} must be an integer >= {minimum}")
    return value


__all__ = ["TelegramCredentialResolver", "TelegramCredentials"]
