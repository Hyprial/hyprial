"""SYSTEM wakes of a human are logged as would-suppress and still wake.

Executor decision Q6: the gate ships in observe mode.  The inbox receive path
is the engine's own human wake path: a request or reply accepted for a
``user:<owner>`` recipient wakes that human.  The class comes from the one
``message_class_of``; nothing is suppressed.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest

from hyprial.daemon.impl.delivery import WOULD_SUPPRESS_EVENT
from hyprial.daemon.impl.inbox import (
    DeliveryLifecycle,
    InboxMessage,
    InboxService,
    MemoryDeliveryTransport,
)
from hyprial.kernel import DISPATCH_MESSAGE_PREFIX

USER = "user:op"
AGENT = "agent:op:node-1:reader"


class _Logger:
    def __init__(self) -> None:
        self.events: list[tuple[str, str, dict[str, object]]] = []

    def log(self, level: str, event: str, **fields: object) -> None:
        self.events.append((level, event, fields))

    def gated(self) -> list[dict[str, object]]:
        return [fields for _l, event, fields in self.events if event == WOULD_SUPPRESS_EVENT]


def _message(message_id: str, recipient: str) -> InboxMessage:
    return InboxMessage(
        message_id=message_id,
        conversation_id="conversation-1",
        sender="agent:op:node-1:driver",
        recipient=recipient,
        payload=json.dumps({"message": "hi"}).encode(),
        intent="request",
        lifecycle=DeliveryLifecycle.DURABLE_SERVICE,
        created_at_ms=1_000,
    )


@pytest.fixture
def service(tmp_path: Path):
    logger = _Logger()
    inbox = InboxService(
        tmp_path / "inbox.sqlite3", MemoryDeliveryTransport(), logger=logger,
    )
    inbox.test_logger = logger
    try:
        yield inbox
    finally:
        inbox.close()


def test_a_system_wake_of_a_human_is_logged_and_still_wakes(service) -> None:
    system_id = f"{DISPATCH_MESSAGE_PREFIX}{uuid.uuid4()}"
    result = service.receive(_message(system_id, USER))

    assert result.accepted and result.wake is True
    assert service.test_logger.gated() == [{
        "messageId": system_id, "recipient": USER, "path": "inbox.receive",
        "messageClass": "system", "mode": "observe",
    }]


def test_interaction_and_agent_wakes_are_not_gated(service) -> None:
    assert service.receive(_message("m-1", USER)).wake is True
    system_id = f"{DISPATCH_MESSAGE_PREFIX}{uuid.uuid4()}"
    assert service.receive(_message(system_id, AGENT)).wake is True
    assert service.test_logger.gated() == []


def test_an_injected_gate_is_consulted_and_cannot_suppress(tmp_path: Path) -> None:
    seen: list[tuple[str, str]] = []

    class _Gate:
        mode = "observe"

        def observe(self, message, *, path):
            seen.append((message.message_id, path))
            return False

    inbox = InboxService(
        tmp_path / "inbox.sqlite3", MemoryDeliveryTransport(), human_wake_gate=_Gate(),
    )
    try:
        assert inbox.receive(_message("m-1", USER)).wake is True
    finally:
        inbox.close()
    assert seen == [("m-1", "inbox.receive")]
