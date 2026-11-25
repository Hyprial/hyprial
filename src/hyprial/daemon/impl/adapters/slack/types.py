"""Narrow shared contracts, frozen field names, and payload validators.

This is the seventh Slack module and it stays deliberately narrow: the
credential and transport ports every Slack module shares, the frozen wire
field names that sealed payloads carry, and the wire-payload validators
used by both the event codec and the socket runtime. The public protocol
module re-exports the public objects unchanged.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

# Frozen field name of the platform discriminator on NativeMediaScope
# (common/transport): the sealed-media wire contract fixes this exact
# name, so Slack code accesses it by name instead of renaming it.
NATIVE_SCOPE_PROVIDER_FIELD = "provider"

_CAPABILITY_REVISION = "slack-socket-v1"
_APP_TOKEN_PREFIXES = ("xapp-", "xoxe.xapp-")


@dataclass(frozen=True, slots=True)
class SlackCredentials:
    bot_token: str
    app_token: str

    def __post_init__(self) -> None:
        if not self.bot_token.startswith("xoxb-") or len(self.bot_token) < 21:
            raise ValueError("invalid Slack bot token")
        if (
            len(self.app_token) < 21
            or not self.app_token.startswith(_APP_TOKEN_PREFIXES)
        ):
            raise ValueError("invalid Slack app token")


class SlackCredentialResolver(Protocol):
    async def resolve(self, credential_ref: str) -> SlackCredentials: ...


class SlackCheckpointLedger(Protocol):
    async def load(self, instance_id: str, generation: int) -> str | None: ...

    async def commit(self, instance_id: str, generation: int, token: str) -> None: ...


class SlackTransport(Protocol):
    async def authenticate(self, credentials: SlackCredentials) -> object: ...

    async def open_socket(self, credentials: SlackCredentials) -> str: ...

    async def connect(self, url: str, *, max_frame_bytes: int) -> None: ...

    async def receive(self, *, max_frame_bytes: int) -> object: ...

    async def acknowledge(self, envelope_id: str) -> None: ...

    async def call(
        self, method: str, payload: object, idempotency_key: str
    ) -> object: ...

    async def reconcile(
        self, method: str, payload: object, idempotency_key: str
    ) -> object: ...

    async def close_socket(self, reason: str, timeout_at: float) -> object: ...

    async def aclose(self, timeout_at: float) -> object: ...


def _map(value: object, name: str) -> Mapping[str, object]:
    thawed = _thaw(value)
    if not isinstance(thawed, Mapping):
        raise ValueError(f"Slack {name} must be an object")
    return thawed


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


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"Slack {name} is required")
    return value


def _integer(value: object, name: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"Slack {name} must be an integer >= {minimum}")
    return value
