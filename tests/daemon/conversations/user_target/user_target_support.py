"""Shared helpers for the UA2 tests: a message to ``user:<owner>`` is queued.

Every test boots the golden ``DaemonApplication`` like ``wiring/services.py``
(``bypass_support.boot``) with the owner's user agent created the way the
daemon's ensure creates it, so ``user:op`` is bound to local delivery.
"""

from __future__ import annotations

from typing import Any

import pytest

from tests.daemon.conversations.bypass.bypass_support import Booted, boot, recorded

__all__ = [
    "boot_with_user_agent",
    "operator_send",
    "record_ends",
    "user_agent_id_address",
]


def boot_with_user_agent(golden_daemon: Any, monkeypatch: pytest.MonkeyPatch) -> Booted:
    """``bypass_support.boot`` with the owner's user agent on this node."""

    return boot(golden_daemon, monkeypatch, user_agent=True)


def user_agent_id_address(app: Any, machine: str | None = None) -> str:
    """The id-form address of *app*'s own user agent, under *machine* when given.

    The golden daemon mints name-form record URIs, so the id form is composed
    from the record's registry-minted ``agent_id``.
    """

    from hyprial.daemon.impl.user_agent import USER_AGENT_NAME
    from hyprial.kernel import canonical_agent_uri

    agent = app.agents.get(USER_AGENT_NAME)
    assert agent is not None
    return canonical_agent_uri(app.owner, machine or app.node_id, agent.agent_id)


def operator_send(app: Any, to: str, key: str, *, text: str = "from the owner") -> dict[str, Any]:
    """One ``message.send`` from the operator principal ``user:op``."""

    return app.handle(
        "message.send",
        {
            "from": "user:op",
            "to": [to],
            "summary": f"summary {key}",
            "message": text,
            "idempotencyKey": f"key-{key}",
        },
    )


def record_ends(app: Any, message_id: str) -> set[tuple[str, str, str]]:
    """``(source, sender, recipient)`` of every record line of one message."""

    return {
        (str(line["source"]), str(line["sender"]), str(line["recipient"]))
        for line in recorded(app)
        if line.get("messageId") == message_id
    }
