"""Neutral per-agent delivery hook vocabulary shared by the hook mechanism.

The v1 per-agent hook configuration file and its turn-log machinery were
retired by the unified-hooks H2 migration: that consumer now lives under
``biz/hooks/`` and configuration is the kernel ``hooks.json`` schema v2.
What remains here is the inbox exchange vocabulary the H1 agent-handler
lane (and the future H3 intercept port) still needs.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any, Protocol

from hyprial.daemon.impl.inbox.contracts.api import InboxMessage
from hyprial.kernel import TURN_HOOKS_ACTOR_NAME, parse_agent_uri

#: The reserved daemon actor that runs an agent's hook handler exchanges
#: (rulings 45/51; registration guarded per ruling 63).  A hook request is
#: only a hook request when its sender really is this actor, never merely
#: because the payload carries the marker.
TURN_HOOK_CALLBACK_ACTOR = TURN_HOOKS_ACTOR_NAME

HOOK_REQUEST_MARKER = "turnHookRequest"
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


def is_hook_request(message: InboxMessage) -> bool:
    """Identify the mechanism's own request so it cannot recursively hook."""

    if HOOK_REQUEST_MARKER.encode() not in message.payload:
        return False
    try:
        body = json.loads(message.payload)
    except (json.JSONDecodeError, UnicodeError):
        return False
    return isinstance(body, dict) and body.get(HOOK_REQUEST_MARKER) is True


def is_mechanism_hook_request(message: InboxMessage) -> bool:
    """The hook mechanism's own request, authenticated by sender (ruling 58).

    All three must hold: the authoritative intent is ``request``, the payload
    carries the marker, and the canonical sender's actor segment is the
    reserved ``_turn-hooks`` name.  A forged marker on an ordinary sender gets
    the summary-only injection instead.
    """

    if message.intent != "request":
        return False
    if not is_hook_request(message):
        return False
    parsed = parse_agent_uri(message.sender)
    return parsed is not None and parsed[2] == TURN_HOOK_CALLBACK_ACTOR


def mechanism_of(harness: str) -> str | None:
    """The declared headless execution mechanism for a harness, or None."""

    # Deferred to avoid the daemon/harness import cycle during module loading.
    from hyprial.daemon.impl.harnesses.capabilities import support
    from hyprial.kernel import Capability

    declared = support(
        harness, headless=True, capability=Capability.HEADLESS_EXEC
    )
    return declared.mechanism if declared is not None else None


def harness_supports_before_delivery(harness: str) -> bool:
    """Whether prompt context is allowed; unknown kinds use safe model summaries."""

    # Deferred to avoid the daemon/harness import cycle during module loading.
    from hyprial.daemon.impl.harnesses.capabilities import support

    # Deferred to avoid the daemon/harness import cycle during module loading.
    from hyprial.kernel import Capability

    declared = support(
        harness,
        headless=True,
        capability=Capability.HEADLESS_EXEC,
    )
    return declared is None or declared.mechanism != "python_worker"


__all__ = [
    "HOOK_REQUEST_MARKER",
    "HookInvoker",
    "TURN_HOOK_CALLBACK_ACTOR",
    "harness_supports_before_delivery",
    "is_hook_request",
    "is_mechanism_hook_request",
    "mechanism_of",
]
