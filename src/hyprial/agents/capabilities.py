"""Build the current runtime facts stored on an Agent (old {} means unknown)."""

from __future__ import annotations

import time

from hyprial.dispatch.matrix import tier_for_model
from hyprial.harnesses.capabilities import Capability, SupportLevel, declare


def option_value(args: tuple[str, ...], option: str) -> str | None:
    value = None
    for index, item in enumerate(args):
        if item == option and index + 1 < len(args):
            value = args[index + 1]
        elif item.startswith(option + "="):
            value = item[len(option) + 1:]
    return value


def runtime_capabilities(
    harness: str,
    *,
    interactive: bool,
    provider: str | None = None,
    model: str | None = None,
    args: tuple[str, ...] = (),
) -> dict[str, object]:
    provider = option_value(args, "--provider") or provider
    model = option_value(args, "--model") or model
    try:
        row = declare(harness, headless=not interactive)
    except KeyError:
        # session.register intentionally accepts non-carrier sources so the
        # session protocol can reject their later refresh/heartbeat calls with
        # INVALID_SESSION_SOURCE.  Such a source is still known to be
        # interactive, but it has no harness-matrix row: preserve that fact as
        # null rather than guessing support or breaking registration.
        proactive_send = None
        tool_injection = None
    else:
        proactive_send = (
            row[Capability.PROACTIVE_SEND].level != SupportLevel.UNSUPPORTED
        )
        tool_injection = row[Capability.TOOL_INJECTION].level != SupportLevel.UNSUPPORTED
    return {
        "interactive": interactive,
        "harness": harness,
        "provider": provider,
        "model": model,
        "tier": tier_for_model(model),
        "proactive_send": proactive_send,
        "tool_injection": tool_injection,
        "updated_at_ms": time.time_ns() // 1_000_000,
    }
