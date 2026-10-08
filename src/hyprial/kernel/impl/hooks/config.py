"""Tier-A ``hooks.json``: per-agent hook registrations as data (design §5).

Parse and validate only.  Loading from an agent home, hot reload and handler
dispatch arrive with the slices that migrate a real consumer.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Literal

from hyprial.kernel.impl.primitives.uri import parse_agent_uri

from hyprial.kernel.impl.hooks.bus  import DEFAULT_LANE_QUEUE
from hyprial.kernel.impl.hooks.events  import EVENT_NAMES, INTERCEPT_EVENT_NAMES

HOOKS_CONFIG_NAME = "hooks.json"
DEFAULT_TIMEOUT_MS = 3_000
MAX_TIMEOUT_MS = 30_000
MAX_LANE_QUEUE = 4_096

HookMode = Literal["observe", "intercept"]


@dataclass(frozen=True, slots=True)
class HookRegistration:
    events: frozenset[str]
    mode: HookMode
    consumer: str | None
    handler: str | None
    timeout_ms: int
    queue: int
    options: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class HooksConfig:
    hooks: tuple[HookRegistration, ...]

    @classmethod
    def from_json(cls, value: object) -> HooksConfig:
        if not isinstance(value, dict) or value.get("schemaVersion") != 2:
            raise ValueError("hooks config requires schemaVersion 2")
        raw_hooks = value.get("hooks", [])
        if not isinstance(raw_hooks, list):
            raise ValueError("hooks config 'hooks' must be a list")
        return cls(tuple(_registration(item, index) for index, item in enumerate(raw_hooks)))


def _registration(value: object, index: int) -> HookRegistration:
    where = f"hooks[{index}]"
    if not isinstance(value, dict):
        raise ValueError(f"{where} must be an object")
    raw_events = value.get("events")
    if (
        not isinstance(raw_events, list)
        or not raw_events
        or not all(isinstance(item, str) for item in raw_events)
    ):
        raise ValueError(f"{where}.events must be a non-empty string list")
    events = frozenset(raw_events)
    unknown = events - EVENT_NAMES
    if unknown:
        raise ValueError(f"{where}: unknown hook events {sorted(unknown)!r}")
    mode = value.get("mode", "observe")
    if mode not in ("observe", "intercept"):
        raise ValueError(f"{where}.mode must be 'observe' or 'intercept'")
    if mode == "intercept" and not events <= INTERCEPT_EVENT_NAMES:
        raise ValueError(
            f"{where}: only {sorted(INTERCEPT_EVENT_NAMES)!r} can be intercepted"
        )
    consumer = value.get("consumer")
    handler = value.get("handler")
    if (consumer is None) == (handler is None):
        raise ValueError(f"{where} requires exactly one of consumer or handler")
    if consumer is not None and (
        not isinstance(consumer, str) or not consumer.strip()
    ):
        raise ValueError(f"{where}.consumer must be a non-blank name")
    if handler is not None and (
        not isinstance(handler, str) or parse_agent_uri(handler) is None
    ):
        raise ValueError(f"{where}.handler must be a canonical agent URI")
    timeout_ms = value.get("timeoutMs", DEFAULT_TIMEOUT_MS)
    if (
        not isinstance(timeout_ms, int)
        or isinstance(timeout_ms, bool)
        or not 1 <= timeout_ms <= MAX_TIMEOUT_MS
    ):
        raise ValueError(f"{where}.timeoutMs must be between 1 and {MAX_TIMEOUT_MS}")
    queue = value.get("queue", DEFAULT_LANE_QUEUE)
    if (
        not isinstance(queue, int)
        or isinstance(queue, bool)
        or not 1 <= queue <= MAX_LANE_QUEUE
    ):
        raise ValueError(f"{where}.queue must be between 1 and {MAX_LANE_QUEUE}")
    options = value.get("options", {})
    if not isinstance(options, dict) or any(
        not isinstance(key, str) for key in options
    ):
        raise ValueError(f"{where}.options must be an object with string keys")
    return HookRegistration(
        events,
        mode,
        consumer.strip() if isinstance(consumer, str) else None,
        handler,
        timeout_ms,
        queue,
        MappingProxyType(dict(options)),
    )


__all__ = [
    "HOOKS_CONFIG_NAME",
    "HookRegistration",
    "HooksConfig",
]
