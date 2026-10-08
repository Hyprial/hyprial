"""Neutral per-agent delivery hooks: protocol, config, and command vocabulary."""

from __future__ import annotations

import json
import queue
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from hyprial.daemon.impl.api import HarnessDelivery, HarnessResult
from hyprial.daemon.impl.inbox.contracts.api import InboxMessage
from hyprial.kernel import parse_agent_uri

HOOK_CONFIG_NAME = "turn-hooks.json"
HOOK_REQUEST_MARKER = "turnHookRequest"
DEFAULT_TIMEOUT_MS = 3_000
MAX_TIMEOUT_MS = 30_000
MAX_RECENT_RECAPS = 100
OUTPUT_EXCERPT_CHARS = 1_000
CONFIG_REFRESH_SECONDS = 1.0
CONFIG_ERROR_LOG_INTERVAL_SECONDS = 60.0
DEFAULT_RECAP_QUEUE_SIZE = 256
RECAP_OVERFLOW_LOG_INTERVAL_SECONDS = 60.0
_HOOK_CONVERSATION_PREFIX = "turn-hook:"


class HookInvoker(Protocol):
    """One opaque request/reply exchange with a configured actor."""

    def invoke(
        self,
        handler: str,
        event: Mapping[str, Any],
        *,
        timeout_seconds: float,
    ) -> str | None: ...


@dataclass(frozen=True, slots=True)
class TurnHookConfig:
    events: frozenset[str]
    handler: str | None
    timeout_ms: int
    recent_recaps: int
    recap: bool

    @classmethod
    def from_json(cls, value: object) -> TurnHookConfig:
        if not isinstance(value, dict) or value.get("schemaVersion") != 1:
            raise ValueError("turn hook config requires schemaVersion 1")
        raw_events = value.get("events", [])
        if not isinstance(raw_events, list) or not all(
            isinstance(item, str) for item in raw_events
        ):
            raise ValueError("turn hook events must be a string list")
        events = frozenset(raw_events)
        unknown = events - {"before-delivery", "after-turn"}
        if unknown:
            raise ValueError(f"unknown turn hook events: {sorted(unknown)!r}")
        handler = value.get("handler")
        if handler is not None and (
            not isinstance(handler, str) or parse_agent_uri(handler) is None
        ):
            raise ValueError("turn hook handler must be a canonical agent URI")
        if events and handler is None:
            raise ValueError("turn hook events require a handler")
        timeout_ms = value.get("timeoutMs", DEFAULT_TIMEOUT_MS)
        if (
            not isinstance(timeout_ms, int)
            or isinstance(timeout_ms, bool)
            or not 1 <= timeout_ms <= MAX_TIMEOUT_MS
        ):
            raise ValueError(
                f"turn hook timeoutMs must be between 1 and {MAX_TIMEOUT_MS}"
            )
        recent_recaps = value.get("recentRecaps", 0)
        if (
            not isinstance(recent_recaps, int)
            or isinstance(recent_recaps, bool)
            or not 0 <= recent_recaps <= MAX_RECENT_RECAPS
        ):
            raise ValueError(
                f"turn hook recentRecaps must be between 0 and {MAX_RECENT_RECAPS}"
            )
        recap = value.get("recap", False)
        if not isinstance(recap, bool):
            raise ValueError("turn hook recap must be boolean")
        return cls(events, handler, timeout_ms, recent_recaps, recap)


@dataclass(slots=True)
class _PendingBefore:
    recipient: str
    deadline: float
    outcome: queue.Queue[tuple[str, object, float]]
    prepared: HarnessDelivery | None = None
    dispatched: bool = False


@dataclass(slots=True)
class _CachedConfiguration:
    home: Path | None
    path: Path | None
    mtime_ns: int | None
    size: int | None
    config: TurnHookConfig | None
    refresh_after: float


@dataclass(frozen=True, slots=True)
class _RecapWrite:
    home: Path
    value: Mapping[str, Any]
    delivery: HarnessDelivery
    event: str
    on_written: Callable[[], None] | None = None


@dataclass(frozen=True, slots=True)
class _PrepareHook:
    operation_id: str
    delivery: HarnessDelivery
    home: Path
    config: TurnHookConfig
    recent_recaps_json: str


@dataclass(frozen=True, slots=True)
class _MarkHook:
    operation_id: str
    delivery_id: str


@dataclass(frozen=True, slots=True)
class _ForgetHook:
    operation_id: str
    delivery_id: str


@dataclass(frozen=True, slots=True)
class _ForgetMissingHooks:
    operation_id: str
    recipient: str
    present_ids: frozenset[str]


@dataclass(frozen=True, slots=True)
class _TurnObserved:
    operation_id: str
    delivery: HarnessDelivery
    result: HarnessResult
    started_at_ms: int
    ended_at_ms: int
    tool_names: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _GetPrepareData:
    operation_id: str
    delivery_id: str
    agent: str


@dataclass(frozen=True, slots=True)
class _PrepareData:
    delivery_id: str
    agent: str
    home: Path | None
    config: TurnHookConfig | None
    recent_recaps_json: str



class HookServicePort(Protocol):
    """Port implemented by :class:`TurnHookService` for the actor coordinator."""

    _logger: Callable[..., None] | None

    def _configuration(
        self, agent: str
    ) -> tuple[Path | None, TurnHookConfig | None]: ...

    def _recent_recaps(self, home: Path, count: int) -> list[object]: ...

    def _prepare_after_config(
        self,
        delivery: HarnessDelivery,
        home: Path,
        config: TurnHookConfig,
        recent_recaps: list[object],
    ) -> HarnessDelivery | None: ...

    def _mark_dispatched_owned(self, delivery_id: str) -> None: ...

    def _forget_delivery_owned(self, delivery_id: str) -> None: ...

    def _forget_missing_owned(
        self, recipient: str, present_delivery_ids: frozenset[str]
    ) -> None: ...

    def _observe_turn_effect(
        self,
        delivery: HarnessDelivery,
        result: HarnessResult,
        *,
        started_at_ms: int,
        ended_at_ms: int,
        tool_names: tuple[str, ...] = (),
    ) -> None: ...


def is_hook_request(message: InboxMessage) -> bool:
    """Identify the mechanism's own request so it cannot recursively hook."""

    if HOOK_REQUEST_MARKER.encode() not in message.payload:
        return False
    try:
        body = json.loads(message.payload)
    except (json.JSONDecodeError, UnicodeError):
        return False
    return isinstance(body, dict) and body.get(HOOK_REQUEST_MARKER) is True


def harness_supports_before_delivery(harness: str) -> bool:
    """Whether the declared headless mechanism can inject prompt context."""

    # Deferred to avoid the daemon/harness import cycle during module loading.
    from hyprial.daemon.impl.harnesses.capabilities import support

    # Deferred to avoid the daemon/harness import cycle during module loading.
    from hyprial.kernel import Capability

    declared = support(
        harness,
        headless=True,
        capability=Capability.HEADLESS_EXEC,
    )
    return declared.mechanism != "python_worker"


__all__ = [
    "HOOK_CONFIG_NAME",
    "HookInvoker",
    "HookServicePort",
    "TurnHookConfig",
    "harness_supports_before_delivery",
    "is_hook_request",
]
