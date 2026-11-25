"""Shared pytest builders for Antigravity ACP headless driver tests."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from hyprial.daemon.impl.api import HarnessDelivery
from hyprial.kernel import HarnessLaunchSpec

FAKE_ACP = Path(__file__).resolve().parent / "fake_acp_server.py"


def agy_spec(
    *,
    name: str = "agy-worker",
    cwd: str | None = None,
    args: tuple[str, ...] = (),
    session_ref: str | None = None,
    model_provider: str | None = None,
    model: str | None = None,
    effort: str | None = None,
) -> HarnessLaunchSpec:
    return HarnessLaunchSpec(
        harness="agy",
        name=name,
        headless=True,
        args=args,
        cwd=cwd,
        session_ref=session_ref,
        model_provider=model_provider,
        model=model,
        effort=effort,
    )


def agy_delivery(delivery_id: str, message: str) -> HarnessDelivery:
    return HarnessDelivery(
        delivery_id=delivery_id,
        conversation_id="conversation-1",
        sender="requester",
        recipient="agent:op:node-1:agy-worker",
        message=message,
    )


def fake_acp_command() -> tuple[str, ...]:
    return (sys.executable, str(FAKE_ACP))


def read_fake_log(path: Path) -> list[dict[str, object]]:
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
