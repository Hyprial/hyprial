"""WP7 D1-D4: ``message.send`` reports still-pending requests from its targets.

The field is advisory and read-only: it never changes ``ok``/``accepted``/
``queued``, never acks or replies, and its failure never blocks the send.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from tests.support.runtime._owned_applications import (
    owned_applications as owned_applications,
)

from hyprial.daemon.impl import HarnessResult, HarnessResultStatus  # noqa: F401
from hyprial.daemon.impl.application import DaemonApplication
from hyprial.daemon.impl.inbox import (
    InboxAuthorityTimeout,
    InboxAuthorityUnavailable,
)
from hyprial.daemon.impl.inbox.contracts.api import DeliveryLifecycle, InboxMessage
from hyprial.daemon.impl.inbox.service import InboxService
from hyprial.kernel import canonical_agent_uri
from hyprial.shell.impl.daemon_wiring import daemon_dependencies as _daemon_dependencies
from tests.support.links.gate import PermissiveLinkGate

NODE = "wp7-node"
OWNER = "wp7-owner"
A_NAME = "worker-a"
A_URI = canonical_agent_uri(OWNER, NODE, A_NAME)
X_NAME = "peer-x"
X_URI = canonical_agent_uri(OWNER, NODE, X_NAME)
Y_NAME = "peer-y"
Y_URI = canonical_agent_uri(OWNER, NODE, Y_NAME)


class LoopbackTransport:
    """Single-node loopback so submit() -> deliver() -> receive() lands local."""

    def __init__(self) -> None:
        self.service: InboxService | None = None
        self.online: set[str] = set()

    def bind(self, service: InboxService) -> None:
        self.service = service

    def is_online(self, recipient: str) -> bool:
        return recipient in self.online

    def online_mailboxes(self) -> tuple[str, ...]:
        return ()

    def deliver(self, message: InboxMessage) -> bool:
        assert self.service is not None
        self.service.receive(message)
        return self.service.has_received(message.message_id)

    def transfer_custody(self, mailbox: str, message: InboxMessage) -> bool:
        return False


def _inbox(tmp_path: Path) -> InboxService:
    transport = LoopbackTransport()
    service = InboxService(tmp_path / "inbox.sqlite3", transport)
    transport.bind(service)
    transport.online.update({NODE, A_URI, X_URI, Y_URI})
    return service


def _app(tmp_path: Path, inbox: InboxService) -> DaemonApplication:
    app = DaemonApplication(
        state_dir=tmp_path / "state",
        socket_path=tmp_path / "daemon.sock",
        node_id=NODE,
        owner=OWNER,
        **_daemon_dependencies(),
    )
    app._inbox = inbox
    app._presence = object()
    app._harnesses = object()
    app._adapters = None
    # Explicit premise (wp2.md §4.9 option a): the link mechanism is live and
    # this scenario is not about links, so a permissive gate keeps
    # agent-to-agent sends delivering instead of holding (ruling 54).
    app.link_admission.bind(PermissiveLinkGate())
    if app.agents.get(A_NAME) is None:
        app.handle("agent.create", {"name": A_NAME})
    return app


def _receive(
    inbox: InboxService,
    *,
    message_id: str,
    recipient: str = A_URI,
    sender: str = X_URI,
    intent: str = "request",
) -> InboxMessage:
    message = InboxMessage(
        message_id=message_id,
        conversation_id="conv-wp7",
        sender=sender,
        recipient=recipient,
        payload=b'{"message":"please answer"}',
        intent=intent,
        lifecycle=DeliveryLifecycle.DURABLE_SERVICE,
        created_at_ms=1,
    )
    inbox.receive(message)
    return message


def _send(
    app: DaemonApplication,
    *,
    to: tuple[str, ...],
    actor: str = A_NAME,
    reply_to: str | None = None,
) -> dict[str, Any]:
    params: dict[str, Any] = {
        "actor": actor,
        "to": list(to),
        "message": "hello",
        "summary": "hello",
        "conversationId": "conv-wp7-send",
        "idempotencyKey": uuid4().hex,
    }
    if reply_to is not None:
        params["replyTo"] = reply_to
    return app.handle("message.send", params)


def test_d1_send_reports_pending_requests_from_the_target(tmp_path: Path) -> None:
    inbox = _inbox(tmp_path)
    app = _app(tmp_path, inbox)
    _receive(inbox, message_id="r1")

    result = _send(app, to=(X_URI,))

    assert result["ok"] is True
    assert result["deliveries"][0]["accepted"] is True
    assert result["deliveries"][0]["openRequestsFrom"] == ["r1"]
    assert result["openRequestsFrom"] == ["r1"]
    # The original request is untouched and the new send is an ordinary request.
    pending_a = inbox.pending_messages(A_URI)
    assert [message.message_id for message in pending_a] == ["r1"]
    delivered = inbox.pending_messages(X_URI)
    assert len(delivered) == 1
    assert delivered[0].intent == "request"
    assert delivered[0].message_id != "r1"


def test_d1_send_never_acks_replies_or_settles_the_original_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review R3 (side-effect guard): the advisory lookup is read-only.  The
    original request is not acked or failure-settled by the send, and only an
    explicit ack takes it out of the candidates."""

    inbox = _inbox(tmp_path)
    app = _app(tmp_path, inbox)
    _receive(inbox, message_id="r1")
    mutations: list[tuple[str, tuple[Any, ...]]] = []
    for name in ("ack", "settle_harness_failure", "fetch_pending", "retry_due"):
        original = getattr(inbox, name)

        def _spy(*args: Any, _name: str = name, _original: Any = original, **kwargs: Any):  # type: ignore[no-untyped-def]
            mutations.append((_name, args))
            return _original(*args, **kwargs)

        monkeypatch.setattr(inbox, name, _spy)

    result = _send(app, to=(X_URI,))

    assert result["openRequestsFrom"] == ["r1"]
    assert not [call for call in mutations if A_URI in call[1] and "r1" in call[1]]
    assert [m.message_id for m in inbox.pending_messages(A_URI)] == ["r1"]

    inbox.ack(A_URI, "r1")  # the explicit close, not the send
    again = _send(app, to=(X_URI,))
    assert "openRequestsFrom" not in again


@pytest.mark.parametrize("variant", ["no-row", "replied", "reply-intent", "reply-to"])
def test_d2_no_field_when_there_is_nothing_open(
    tmp_path: Path, variant: str
) -> None:
    inbox = _inbox(tmp_path)
    app = _app(tmp_path, inbox)
    if variant == "no-row":
        pass
    elif variant == "replied":
        _receive(inbox, message_id="r1")
        inbox.ack(A_URI, "r1")
    elif variant == "reply-intent":
        _receive(inbox, message_id="r1", intent="reply")
    elif variant == "reply-to":
        _receive(inbox, message_id="r1")

    result = _send(app, to=(X_URI,), reply_to="some-request" if variant == "reply-to" else None)

    assert result["ok"] is True
    assert "openRequestsFrom" not in result
    assert "openRequestsFrom" not in result["deliveries"][0]


def test_d3_multiple_targets_merge_in_target_order(tmp_path: Path) -> None:
    inbox = _inbox(tmp_path)
    app = _app(tmp_path, inbox)
    _receive(inbox, message_id="r1")
    _receive(inbox, message_id="r2")

    result = _send(app, to=(X_URI, Y_URI))

    assert result["openRequestsFrom"] == ["r1", "r2"]
    assert result["deliveries"][0]["openRequestsFrom"] == ["r1", "r2"]
    assert "openRequestsFrom" not in result["deliveries"][1]


@pytest.mark.parametrize("failure", ["unavailable", "timeout", "operational"])
def test_d4_optional_lookup_failure_never_blocks_the_send(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    inbox = _inbox(tmp_path)
    app = _app(tmp_path, inbox)
    _receive(inbox, message_id="r1")

    error: BaseException
    if failure == "unavailable":
        error = InboxAuthorityUnavailable("authority down")
    elif failure == "timeout":
        error = InboxAuthorityTimeout("authority slow")
    else:
        error = sqlite3.OperationalError("database is locked")

    real_pending = inbox.pending_messages

    def _boom(recipient: str, *, now_ms: int | None = None):  # type: ignore[no-untyped-def]
        if recipient == A_URI:
            raise error
        return real_pending(recipient, now_ms=now_ms)

    monkeypatch.setattr(inbox, "pending_messages", _boom)
    logs: list[tuple[str, str, str, dict[str, Any]]] = []
    app._log = lambda level, component, event, **fields: logs.append(  # type: ignore[method-assign]
        (level, component, event, fields)
    )

    result = _send(app, to=(X_URI,))

    assert result["ok"] is True
    assert "openRequestsFrom" not in result
    assert "openRequestsFrom" not in result["deliveries"][0]
    # The send itself went through: X's inbox holds the new request.
    delivered = inbox.pending_messages(X_URI)
    assert len(delivered) == 1 and delivered[0].intent == "request"
    failed = [entry for entry in logs if entry[2] == "send.open_requests_unavailable"]
    assert failed, "the lookup failure must be logged"
    # The adapter converts a raw SQLite failure before the send boundary sees
    # it, so the boundary always reports an authority error.
    expected_error = (
        "InboxAuthorityUnavailable"
        if failure == "operational"
        else type(error).__name__
    )
    assert failed[0][3].get("error") == expected_error


def test_d4_adapter_converts_sqlite_operational_error(tmp_path: Path) -> None:
    """A real unreadable authority read is translated for the reminder adapter."""

    from hyprial.daemon.impl.application.messaging.inbox_surface.reply_reminders import (
        open_requests_by_sender,
    )
    from hyprial.daemon.impl.inbox.authority.read import InboxReadProjection

    projection = InboxReadProjection(tmp_path / "absent.sqlite3")

    with pytest.raises(InboxAuthorityUnavailable):
        open_requests_by_sender(
            projection, recipient=A_URI, senders=(X_URI,)
        )
