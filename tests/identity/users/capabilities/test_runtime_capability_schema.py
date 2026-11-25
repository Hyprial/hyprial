"""Runtime capability schema: JSON round trip and validation (moved from the squire runtime tests)."""

from __future__ import annotations

from typing import Any

import pytest

from hyprial.identity import RuntimeCapability, RuntimeCapabilityError

PROBED_AT = "2026-08-06T12:00:00Z"


def test_runtime_capability_round_trip() -> None:
    capability = RuntimeCapability(
        harness="pi",
        provider="kimi-coding",
        model="k3",
        status="unavailable",
        reason="usage-limit",
        detail="You're out of extra usage",
        retry_at="2026-08-07T10:00:00Z",
        probed_at=PROBED_AT,
    )
    parsed = RuntimeCapability.from_json(capability.to_json(), "capability")
    assert parsed == capability
    assert parsed.combo == ("pi", "kimi-coding", "k3")


@pytest.mark.parametrize(
    "document, fragment",
    [
        ({"harness": "emacs"}, "harness"),
        ({"harness": "pi", "model": "k3"}, "provider"),
        ({"harness": "claude", "model": "sonnet", "status": "busy"}, "status"),
        (
            {"harness": "claude", "model": "sonnet", "status": "unavailable"},
            "reason",
        ),
        (
            {
                "harness": "claude",
                "model": "sonnet",
                "status": "available",
                "reason": "usage-limit",
            },
            "reason",
        ),
        (
            {"harness": "claude", "model": "sonnet", "status": "available"},
            "probedAt",
        ),
    ],
)
def test_runtime_capability_validation_rejects_bad_documents(
    document: dict[str, Any], fragment: str
) -> None:
    with pytest.raises(RuntimeCapabilityError, match=fragment):
        RuntimeCapability.from_json(document, "capability")
