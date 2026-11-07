"""A message's origin survives from the adapter to the reader.

The Lark adapter parses ``chat_type`` off the event and hands it to the daemon
inside ``providerMetadata``.  Only ``senderId`` was ever read from that block,
so the value arrived and was dropped before storage -- a reader could not tell
a group from a direct message, and Feishu uses the same ``oc_`` prefix for
both, so nothing else in the row distinguished them either.

Absence is load-bearing here.  "This provider reports no origin" and "a direct
message" are different answers and only one of them is safe to act on, so these
tests pin absence as carefully as presence.
"""

from __future__ import annotations
from tests.support.runtime._owned_applications import owned_applications as owned_applications

import json
from pathlib import Path

from hyprial.daemon.impl.application import DaemonApplication, _message_origin
from hyprial.daemon.impl.inbox import (
    DeliveryLifecycle,
    InboxMessage,
    InboxService,
    MemoryDeliveryTransport,
)
from hyprial.shell.impl.daemon_wiring import daemon_dependencies as _deep_module_daemon_dependencies
from tests.support.links.gate import PermissiveLinkGate

READER = {
    "provider": "pi",
    "name": "reader",
    "headless": True,
    "args": [],
    "ownership": "managed",
}


class _Presence:
    def online_actors(self) -> tuple[str, ...]:
        return ()

    def actor_online(self, actor: str) -> bool:
        return False

    def online_mailboxes(self) -> tuple[str, ...]:
        return ()


class _Harnesses:
    def status(self) -> tuple[object, ...]:
        return ()


class _LoopbackTransport(MemoryDeliveryTransport):
    def __init__(self) -> None:
        super().__init__()
        self.service: InboxService | None = None

    def is_online(self, recipient: str) -> bool:
        return True

    def deliver(self, message: InboxMessage) -> bool:
        assert self.service is not None
        return self.service.receive(message).acknowledged


def _app(tmp_path: Path) -> DaemonApplication:
    state_dir = tmp_path / "state"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "desired-state.json.v1").write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "asMailbox": False,
                "providers": [READER],
                "interactiveSessions": [],
            }
        ),
        encoding="utf-8",
    )
    app = DaemonApplication(
        state_dir=state_dir,
        socket_path=state_dir / "daemon.sock",
        node_id="node-1",
        owner="op",
    **_deep_module_daemon_dependencies())
    transport = _LoopbackTransport()
    # This helper wires a daemon whose link slot has no real gate yet (WP2),
    # so it binds the explicit permissive test premise (review item 3).
    app._inbox = InboxService(
        tmp_path / "inbox.sqlite3", transport, link_admission=app.link_admission
    )
    app.link_admission.bind(PermissiveLinkGate())
    transport.service = app._inbox
    app._harnesses = _Harnesses()  # type: ignore[assignment]
    app._presence = _Presence()
    if app.agents.get("driver") is None:
        app.handle("agent.create", {"name": "driver"})
    app.handle(
        "session.register",
        {
            "actor": "driver",
            "sessionRef": "session-driver",
            "cwd": str(workspace),
            "command": ["test"],
            "source": "claude-channel",
            "runtime": "claude_interactive",
        },
    )
    app.handle(
        "session.register",
        {
            "actor": "reader",
            "sessionRef": "session-reader",
            "cwd": str(workspace),
            "command": ["test"],
            "source": "claude-channel",
            "runtime": "claude_interactive",
        },
    )
    return app


def _round_trip(
    app: DaemonApplication, **metadata: object
) -> tuple[dict[str, object], dict[str, object]]:
    params: dict[str, object] = {
        "from": "driver",
        "sessionRef": "session-driver",
        "to": ["reader"],
        "summary": "hi",
        "message": "hi",
    }
    if metadata:
        params["providerMetadata"] = {"provider": "lark", **metadata}
    app.handle("message.send", params)
    pending = app.handle(
        "message.pending.list",
        {"from": "reader", "sessionRef": "session-reader"},
    )
    (row,) = pending["messages"]
    # The row carries summary/bodyBytes only; the text is read on demand.
    body = app.handle(
        "message.read_body",
        {
            "from": "reader",
            "sessionRef": "session-reader",
            "messageId": row["messageId"],
        },
    )
    app._inbox.close()
    app.agents.close()
    return row, body


# -- the reader's view ---------------------------------------------------------


def test_a_group_message_reaches_the_reader_as_a_group(tmp_path: Path) -> None:
    row, _body = _round_trip(_app(tmp_path), chatType="group", chatId="oc_group")
    assert row["origin"] == {
        "chatType": "group",
        "provider": "lark",
        "via": "session",
    }


def test_a_direct_message_reaches_the_reader_as_a_direct_message(
    tmp_path: Path,
) -> None:
    row, _body = _round_trip(_app(tmp_path), chatType="p2p", chatId="oc_dm")
    assert row["origin"] == {
        "chatType": "p2p",
        "provider": "lark",
        "via": "session",
    }


def test_a_session_without_adapter_metadata_still_reports_provenance(
    tmp_path: Path,
) -> None:
    """Absent, not empty and not "p2p": an unknown origin is not a direct one."""

    row, _body = _round_trip(_app(tmp_path))
    assert row["origin"] == {"via": "session"}


def test_an_origin_alone_does_not_disturb_the_rest_of_the_row(
    tmp_path: Path,
) -> None:
    """The fields a reader already depends on keep their values."""

    row, body = _round_trip(_app(tmp_path), chatType="group")
    assert body["message"] == "hi"
    assert row["summary"] == "hi"
    assert row["bodyBytes"] == len("hi".encode())
    # `from` is the canonicalized sender, not the bare name that was passed in.
    assert row["from"] == "agent:op:node-1:driver"
    assert row["conversationId"]
    assert row["intent"] == "request"
    assert row["messageId"] == row["deliveryId"]


def test_a_row_written_before_this_change_is_still_readable(tmp_path: Path) -> None:
    """Old payloads carry no ``origin`` and must not be rejected."""

    app = _app(tmp_path)
    app._inbox.receive(
        InboxMessage(
            message_id="m-old",
            conversation_id="c-old",
            sender="agent:op:node-1:driver",
            recipient="agent:op:node-1:reader",
            payload=b'{"message":"legacy","topic":null}',
            intent="request",
            lifecycle=DeliveryLifecycle.DURABLE_SERVICE,
            created_at_ms=1,
        )
    )
    try:
        pending = app.handle(
            "message.pending.list",
            {"from": "reader", "sessionRef": "session-reader"},
        )
        (row,) = pending["messages"]
        assert "message" not in row
        assert row["summary"] is None
        assert row["bodyBytes"] == len("legacy".encode())
        legacy_body = app.handle(
            "message.read_body",
            {
                "from": "reader",
                "sessionRef": "session-reader",
                "messageId": "m-old",
            },
        )
        assert legacy_body["message"] == "legacy"
        assert legacy_body["summary"] is None
        assert row["origin"] == {"via": "daemon"}
    finally:
        app._inbox.close()
        app.agents.close()


# -- the block itself ----------------------------------------------------------


def test_message_origin_ignores_a_metadata_block_without_a_chat_type() -> None:
    assert _message_origin({"senderId": "ou_x"}) is None
    assert _message_origin({"chatType": ""}) is None
    assert _message_origin({"chatType": 7}) is None
    assert _message_origin("not-a-dict") is None
    assert _message_origin(None) is None


def test_message_origin_omits_a_provider_it_was_not_given() -> None:
    """The block carries only what the sender actually said."""

    assert _message_origin({"chatType": "group"}) == {"chatType": "group"}
    assert _message_origin({"provider": "", "chatType": "group"}) == {
        "chatType": "group"
    }
