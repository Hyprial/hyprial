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
    "HOOK_REQUEST_MARKER",
    "HookInvoker",
    "harness_supports_before_delivery",
    "is_hook_request",
]
