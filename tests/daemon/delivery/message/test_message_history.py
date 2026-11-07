"""``message.history``: a participant-scoped, read-only durable conversation read.

The gap this pins: consumed inbox rows are retained by the store but no IPC
read exposed them, so a GUI transcript lost every request once answered and
every reply once consumed.  ``message.history`` reads the retained inbox rows
(both directions) plus undelivered outbox rows, under the same authenticated
caller chain as the other message methods.

The remote-delivery tests below use a second durable inbox as the peer node:
the sender's own store receives no inbox row and the delivered outbox row is
deleted, exactly the cross-node shape that used to erase the sender's copy.
"""

from __future__ import annotations

import base64
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

import pytest

from tests.support.links.gate import PermissiveLinkGate

from hyprial.daemon.impl.application import DaemonApplication, DaemonRequestError
from hyprial.daemon.impl.inbox import (
    DeliveryLifecycle,
    InboxMessage,
    InboxService,
    MemoryDeliveryTransport,
)
from hyprial.daemon.impl.inbox.authority.read import InboxReadProjection
from hyprial.shell.impl.daemon_wiring import (
    daemon_dependencies as _deep_module_daemon_dependencies,
)

OWNER = "op"
NODE = "node-1"
ACTOR_NAME = "worker-b"
ACTOR = f"agent:{OWNER}:{NODE}:{ACTOR_NAME}"
SESSION = "session-worker-b"
READER_NAME = "reader"
READER = f"agent:{OWNER}:{NODE}:{READER_NAME}"
READER_SESSION = "session-reader"
OTHER_NAME = "other"
OTHER = f"agent:{OWNER}:{NODE}:{OTHER_NAME}"
OTHER_SESSION = "session-other"
REMOTE_READER = f"agent:{OWNER}:node-2:{READER_NAME}"
CONVERSATION = "conversation-history"
OTHER_CONVERSATION = "conversation-other"


class _Presence:
    def online_actors(self) -> tuple[str, ...]:
        return ()

    def actor_online(self, _actor: str) -> bool:
        return False

    def online_mailboxes(self) -> tuple[str, ...]:
        return ()


class _Harnesses:
    def status(self) -> tuple[object, ...]:
        return ()


class _LoopbackTransport(MemoryDeliveryTransport):
    """Direct ``receive`` for online recipients; anything else stays queued."""

    def __init__(self) -> None:
        super().__init__()
        self.service: InboxService | None = None
        self.offline: set[str] = set()

    def is_online(self, recipient: str) -> bool:
        return recipient not in self.offline

    def deliver(self, message: InboxMessage) -> bool:
        assert self.service is not None
        return self.service.receive(message).accepted


class _RemoteNodeTransport(MemoryDeliveryTransport):
    """A receipt from a peer node's own durable store.

    The message is committed to the peer's inbox only; the sender's store
    never receives a row for it.  This is the cross-node shape, not the
    same-daemon loopback above.
    """

    def __init__(self, remote: InboxService) -> None:
        super().__init__()
        self.remote = remote
        self.deliveries = 0
        self.offline: set[str] = set()

    def is_online(self, recipient: str) -> bool:
        return recipient not in self.offline

    def deliver(self, message: InboxMessage) -> bool:
        self.deliveries += 1
        result = self.remote.receive(message)
        # A duplicate means the peer already committed this message id; the
        # transport's "acknowledged" answer is a successful delivery.
        return result.accepted or result.acknowledged


@dataclass
class _HistoryHarness:
    app: DaemonApplication
    inbox: InboxService
    transport: _LoopbackTransport
    database: Path
    peers: list[InboxService]


@pytest.fixture
def harness(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _HistoryHarness:
    monkeypatch.setenv("HYPRIAL_SENDER_AUTH_MODE", "enforce")
    state = tmp_path / "state"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    app = DaemonApplication(
        state_dir=state,
        socket_path=state / "daemon.sock",
        node_id=NODE,
        owner=OWNER,
        **_deep_module_daemon_dependencies(),
    )
    database = tmp_path / "inbox.sqlite3"
    transport = _LoopbackTransport()
    # History tests assume admitted peers; link policy is tested separately.
    app.link_admission.bind(PermissiveLinkGate())
    inbox = InboxService(database, transport, link_admission=app.link_admission)
    transport.service = inbox
    app._inbox = inbox
    app._presence = _Presence()  # type: ignore[assignment]
    app._harnesses = _Harnesses()  # type: ignore[assignment]
    harness = _HistoryHarness(app, inbox, transport, database, [])
    try:
        app.handle("agent.create", {"name": ACTOR_NAME})
        app.handle("agent.create", {"name": READER_NAME})
        app.handle("agent.create", {"name": OTHER_NAME})
        for name, session in (
            (ACTOR_NAME, SESSION),
            (READER_NAME, READER_SESSION),
            (OTHER_NAME, OTHER_SESSION),
        ):
            app.handle(
                "session.register",
                {
                    "actor": name,
                    "sessionRef": session,
                    "cwd": str(workspace),
                    "command": ["test-carrier"],
                    "source": "claude-channel",
                    "runtime": "claude_interactive",
                },
            )
        yield harness
    finally:
        for peer in harness.peers:
            peer.close()
        app._harnesses = None
        try:
            app._close()
        finally:
            app._close_home_lease()


def _deliver(
    harness: _HistoryHarness,
    message_id: str,
    *,
    sender: str = READER,
    recipient: str = ACTOR,
    conversation: str = CONVERSATION,
    intent: str = "request",
    text: str | None = None,
    created_at_ms: int = 1000,
) -> InboxMessage:
    message = InboxMessage(
        message_id=message_id,
        conversation_id=conversation,
        sender=sender,
        recipient=recipient,
        payload=json.dumps(
            {"message": text if text is not None else message_id},
            separators=(",", ":"),
        ).encode(),
        intent=intent,
        lifecycle=DeliveryLifecycle.DURABLE_SERVICE,
        created_at_ms=created_at_ms,
    )
    result = harness.inbox.receive(message)
    assert result.accepted, result
    return message


def _history(
    harness: _HistoryHarness,
    *,
    actor: str = ACTOR,
    session: str | None = SESSION,
    conversation: str = CONVERSATION,
    **params: object,
) -> dict:
    request: dict[str, object] = {
        "from": actor,
        "conversationId": conversation,
        **params,
    }
    if session is not None:
        request["sessionRef"] = session
    return harness.app.handle(
        "message.history", request, _trusted_message_origin="ipc"
    )


def _ids(page: dict) -> list[str]:
    return [entry["messageId"] for entry in page["entries"]]


def _ids_in_full(harness: _HistoryHarness, **params: object) -> list[str]:
    return _ids(_history(harness, limit=200, **params))


def _remote_delivery(
    harness: _HistoryHarness, tmp_path: Path
) -> _RemoteNodeTransport:
    """Wire the sender's inbox to a separate durable peer node."""

    peer = InboxService(
        tmp_path / "peer-inbox.sqlite3", MemoryDeliveryTransport(),
        link_admission=harness.app.link_admission,
    )
    harness.peers.append(peer)
    transport = _RemoteNodeTransport(peer)
    harness.inbox._transport = transport
    return transport


def _count(database: Path, table: str) -> int:
    connection = sqlite3.connect(database)
    try:
        return int(
            connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        )
    finally:
        connection.close()


def _submit_remote(
    harness: _HistoryHarness,
    message_id: str,
    *,
    intent: str = "request",
    created_at_ms: int,
    text: str | None = None,
    queued: bool = False,
) -> InboxMessage:
    message = InboxMessage(
        message_id=message_id,
        conversation_id=CONVERSATION,
        sender=ACTOR,
        recipient=REMOTE_READER,
        payload=json.dumps(
            {"message": text if text is not None else message_id},
            separators=(",", ":"),
        ).encode(),
        intent=intent,
        lifecycle=DeliveryLifecycle.DURABLE_SERVICE,
        created_at_ms=created_at_ms,
    )
    result = harness.inbox.submit(message, now_ms=created_at_ms)
    assert result.accepted and result.queued is queued, result
    return message


def _send_remote(
    harness: _HistoryHarness,
    *,
    text: str,
    idempotency_key: str,
    to: str = REMOTE_READER,
) -> dict:
    result = harness.app.handle(
        "message.send",
        {
            "from": ACTOR,
            "sessionRef": SESSION,
            "to": [to],
            "message": text,
            "summary": text,
            "conversationId": CONVERSATION,
            "idempotencyKey": idempotency_key,
        },
        _trusted_message_origin="ipc",
    )
    assert result["ok"] is True, result
    return result


def _forge_cursor(**overrides: object) -> str:
    payload = {
        "v": 1,
        "conversationId": CONVERSATION,
        "participant": ACTOR,
        "createdAtMs": 2000,
        "messageId": "m-2",
    }
    payload.update(overrides)
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def test_history_keeps_consumed_requests_and_real_replies(
    harness: _HistoryHarness,
) -> None:
    _deliver(harness, "m-request", text="please answer", created_at_ms=1000)
    _deliver(harness, "m-acked", text="ack me", created_at_ms=2000)
    _deliver(harness, "m-pending", text="still pending", created_at_ms=3000)
    harness.inbox.receive_system_notice(
        InboxMessage(
            message_id="n-1",
            conversation_id=CONVERSATION,
            sender=f"system:hyprial:{NODE}",
            recipient=ACTOR,
            payload=b'{"message":"system notice"}',
            intent="system",
            lifecycle=DeliveryLifecycle.ONLINE_ONLY,
            created_at_ms=1500,
        )
    )

    acknowledged = harness.app.handle(
        "message.ack",
        {"from": ACTOR, "sessionRef": SESSION, "messageId": "m-acked"},
    )
    assert acknowledged["acknowledged"] is True
    replied = harness.app.handle(
        "message.reply",
        {
            "from": ACTOR,
            "sessionRef": SESSION,
            "messageId": "m-request",
            "message": "the real reply",
            "summary": "the real reply",
        },
    )
    assert replied["replied"] is True
    assert replied["acknowledged"] is True
    reply_id = replied["replyMessageId"]

    pending_before = harness.inbox.pending_count(ACTOR)
    outbox_before = harness.inbox.outbox_count()
    page = _history(harness, limit=200)

    by_id = {entry["messageId"]: entry for entry in page["entries"]}
    # Both consumed rows survive: the answered request and the acknowledged one.
    assert {"m-request", "m-acked", "m-pending", reply_id} <= set(by_id)
    assert "n-1" not in by_id
    assert by_id["m-request"]["message"] == "please answer"
    assert by_id["m-request"]["intent"] == "request"
    assert by_id["m-request"]["from"] == READER
    assert by_id["m-request"]["to"] == ACTOR
    assert by_id[reply_id]["intent"] == "reply"
    assert by_id[reply_id]["from"] == ACTOR
    assert by_id[reply_id]["to"] == READER
    assert by_id[reply_id]["message"] == "the real reply"
    assert [entry["createdAtMs"] for entry in page["entries"]] == sorted(
        entry["createdAtMs"] for entry in page["entries"]
    )
    # Reading consumed nothing and drained nothing.
    assert harness.inbox.pending_count(ACTOR) == pending_before
    assert harness.inbox.outbox_count() == outbox_before
    assert [notice.message_id for notice in harness.inbox.system_notices(ACTOR)] == [
        "n-1"
    ]


def test_history_is_scoped_to_the_authenticated_participant_and_conversation(
    harness: _HistoryHarness,
) -> None:
    _deliver(harness, "m-for-actor", created_at_ms=1000)
    _deliver(
        harness,
        "m-between-reader-and-other",
        recipient=OTHER,
        created_at_ms=2000,
    )
    _deliver(
        harness,
        "m-other-conversation",
        conversation=OTHER_CONVERSATION,
        created_at_ms=3000,
    )

    actor_page = _history(harness, limit=200)
    assert _ids(actor_page) == ["m-for-actor"]

    reader_page = _history(
        harness, actor=READER, session=READER_SESSION, limit=200
    )
    # The sender's copy lives in the recipient's local inbox: a second client
    # in the same conversation sees the same two messages.
    assert set(_ids(reader_page)) == {"m-for-actor", "m-between-reader-and-other"}

    other_page = _history(
        harness, actor=OTHER, session=OTHER_SESSION, limit=200
    )
    assert _ids(other_page) == ["m-between-reader-and-other"]

    assert all(
        entry["conversationId"] == CONVERSATION for entry in actor_page["entries"]
    )


def test_history_cursor_is_opaque_and_bound_to_conversation_and_participant(
    harness: _HistoryHarness,
) -> None:
    for index in range(1, 5):
        _deliver(harness, f"m-{index}", created_at_ms=index * 1000)

    first = _history(harness, limit=2)
    assert _ids(first) == ["m-1", "m-2"]
    cursor = first["nextCursor"]
    assert isinstance(cursor, str) and cursor
    assert cursor != "2000/m-2"  # not the adapter's old local page key

    rest = _history(harness, cursor=cursor, limit=2)
    assert _ids(rest) == ["m-3", "m-4"]
    assert rest["nextCursor"] is None

    with pytest.raises(DaemonRequestError) as wrong_conversation:
        harness.app.handle(
            "message.history",
            {
                "from": ACTOR,
                "sessionRef": SESSION,
                "conversationId": OTHER_CONVERSATION,
                "cursor": cursor,
            },
            _trusted_message_origin="ipc",
        )
    assert wrong_conversation.value.code == "INVALID_ARGUMENT"

    # The same cursor replayed by another participant is refused, even though
    # that participant can read this conversation.
    with pytest.raises(DaemonRequestError) as wrong_participant:
        harness.app.handle(
            "message.history",
            {
                "from": f"user:{OWNER}",
                "onBehalfOf": READER,
                "conversationId": CONVERSATION,
                "cursor": cursor,
            },
            _trusted_message_origin="ipc",
        )
    assert wrong_participant.value.code == "INVALID_ARGUMENT"

    for forged in (
        _forge_cursor(participant=READER),
        _forge_cursor(conversationId=OTHER_CONVERSATION),
        _forge_cursor(v=2),
        _forge_cursor(createdAtMs=0),
        _forge_cursor(messageId=""),
        "not-a-cursor",
        "",
    ):
        with pytest.raises(DaemonRequestError) as refused:
            _history(harness, cursor=forged)
        assert refused.value.code == "INVALID_ARGUMENT"


def test_history_paginates_without_duplicates_or_gaps(
    harness: _HistoryHarness,
) -> None:
    # Arrival order deliberately differs from the stamped ordering; two rows
    # share a stamp so the message-id tie-break is exercised.
    for message_id, stamp in (
        ("m-5", 5000),
        ("m-2b", 2000),
        ("m-4", 4000),
        ("m-2a", 2000),
        ("m-1", 1000),
        ("m-3", 3000),
    ):
        _deliver(harness, message_id, created_at_ms=stamp)

    expected = _ids_in_full(harness)
    assert expected == ["m-1", "m-2a", "m-2b", "m-3", "m-4", "m-5"]

    seen: list[str] = []
    cursor: str | None = None
    while True:
        page = _history(harness, cursor=cursor, limit=2)
        seen.extend(_ids(page))
        cursor = page["nextCursor"]
        if cursor is None:
            break
    assert seen == expected
    assert len(seen) == len(set(seen))


def test_history_defaults_to_fifty_and_bounds_the_page(
    harness: _HistoryHarness,
) -> None:
    empty = _history(harness)
    assert empty["entries"] == []
    assert empty["nextCursor"] is None

    for index in range(51):
        _deliver(harness, f"m-{index:02d}", created_at_ms=1000 + index)

    default_page = _history(harness)
    assert len(default_page["entries"]) == 50
    assert default_page["nextCursor"] is not None
    full = _history(harness, limit=200)
    assert len(full["entries"]) == 51
    assert full["nextCursor"] is None

    for bad_limit in (0, 201, True, "5", 1.5):
        with pytest.raises(DaemonRequestError) as refused:
            _history(harness, limit=bad_limit)
        assert refused.value.code == "INVALID_ARGUMENT"

    for request in (
        {"from": ACTOR, "sessionRef": SESSION},
        {"from": ACTOR, "sessionRef": SESSION, "conversationId": ""},
        {"from": ACTOR, "sessionRef": SESSION, "conversationId": 7},
        {
            "from": ACTOR,
            "sessionRef": SESSION,
            "conversationId": CONVERSATION,
            "participants": [READER],
        },
        {
            "from": ACTOR,
            "sessionRef": SESSION,
            "conversationId": CONVERSATION,
            "sender": READER,
        },
        {
            "from": ACTOR,
            "sessionRef": SESSION,
            "conversationId": CONVERSATION,
            "cursor": 7,
        },
    ):
        with pytest.raises(DaemonRequestError) as refused:
            harness.app.handle(
                "message.history", request, _trusted_message_origin="ipc"
            )
        assert refused.value.code == "INVALID_ARGUMENT"


def test_history_requires_an_authenticated_ipc_caller_even_in_observe_mode(
    harness: _HistoryHarness,
) -> None:
    _deliver(harness, "m-1", created_at_ms=1000)
    records: list[tuple[str, dict[str, object]]] = []
    harness.app._sender_auth_enforced = False  # type: ignore[attr-defined]
    harness.app._log = (  # type: ignore[method-assign]
        lambda _level, _component, event, **fields: records.append((event, fields))
    )

    with pytest.raises(DaemonRequestError) as unauthenticated:
        harness.app.handle(
            "message.history",
            {"from": ACTOR, "conversationId": CONVERSATION},
            _trusted_message_origin="ipc",
        )
    assert unauthenticated.value.code == "SENDER_NOT_AUTHENTICATED"
    assert any(
        event == "send.sender_unauthenticated"
        and fields["method"] == "message.history"
        for event, fields in records
    )

    with pytest.raises(DaemonRequestError) as superseded:
        harness.app.handle(
            "message.history",
            {
                "from": ACTOR,
                "sessionRef": READER_SESSION,
                "conversationId": CONVERSATION,
            },
            _trusted_message_origin="ipc",
        )
    assert superseded.value.code == "SESSION_SUPERSEDED"

    with pytest.raises(DaemonRequestError) as unresolved:
        harness.app.handle(
            "message.history",
            {
                "from": f"user:{OWNER}",
                "onBehalfOf": "ghost",
                "conversationId": CONVERSATION,
            },
            _trusted_message_origin="ipc",
        )
    assert unresolved.value.code == "SENDER_UNRESOLVED"

    # The operator relay is an authenticated principal and reads as the actor.
    page = harness.app.handle(
        "message.history",
        {
            "from": f"user:{OWNER}",
            "onBehalfOf": ACTOR,
            "conversationId": CONVERSATION,
        },
        _trusted_message_origin="ipc",
    )
    assert _ids(page) == ["m-1"]

    # The operator's own principal is not a participant of that row.
    own = harness.app.handle(
        "message.history",
        {"from": f"user:{OWNER}", "conversationId": CONVERSATION},
        _trusted_message_origin="ipc",
    )
    assert own["entries"] == []


def test_history_projects_sent_and_undelivered_sender_rows(
    harness: _HistoryHarness,
) -> None:
    delivered = harness.app.handle(
        "message.send",
        {
            "from": ACTOR,
            "sessionRef": SESSION,
            "to": [READER],
            "message": "hello reader",
            "summary": "hello reader",
            "conversationId": CONVERSATION,
            "idempotencyKey": "history-send-1",
        },
        _trusted_message_origin="ipc",
    )
    assert delivered["ok"] is True

    harness.transport.offline.add(READER)
    queued = harness.app.handle(
        "message.send",
        {
            "from": ACTOR,
            "sessionRef": SESSION,
            "to": [READER],
            "message": "still queued",
            "summary": "still queued",
            "conversationId": CONVERSATION,
            "idempotencyKey": "history-send-2",
        },
        _trusted_message_origin="ipc",
    )
    assert queued["ok"] is True
    assert harness.inbox.outbox_count() == 1

    page = _history(harness, limit=200)

    texts = {entry["message"]: entry for entry in page["entries"]}
    assert texts["hello reader"]["from"] == ACTOR
    assert texts["hello reader"]["to"] == READER
    assert texts["still queued"]["from"] == ACTOR
    assert harness.inbox.outbox_count() == 1


def test_history_projects_one_copy_when_inbox_and_outbox_hold_the_same_id(
    harness: _HistoryHarness,
) -> None:
    harness.transport.offline.add(READER)
    sent = harness.app.handle(
        "message.send",
        {
            "from": ACTOR,
            "sessionRef": SESSION,
            "to": [READER],
            "message": "outbox copy",
            "summary": "outbox copy",
            "conversationId": CONVERSATION,
            "idempotencyKey": "history-dedupe",
        },
        _trusted_message_origin="ipc",
    )
    message_id = sent["messageId"]
    # The delivery arrives while the sender's outbox row still exists.
    _deliver(
        harness,
        message_id,
        sender=ACTOR,
        recipient=READER,
        text="inbox copy",
        created_at_ms=2000,
    )
    harness.inbox.ack(READER, message_id)

    actor_page = _history(harness, limit=200)
    reader_page = _history(
        harness, actor=READER, session=READER_SESSION, limit=200
    )
    for page in (actor_page, reader_page):
        matching = [
            entry for entry in page["entries"] if entry["messageId"] == message_id
        ]
        assert len(matching) == 1
        # The retained inbox copy names both ends and is the stored fact.
        assert matching[0]["message"] == "inbox copy"


def test_history_reads_through_the_read_only_projection(
    harness: _HistoryHarness,
) -> None:
    _deliver(harness, "m-1", created_at_ms=1000)
    _deliver(harness, "m-2", created_at_ms=2000)
    assert harness.inbox.ack(ACTOR, "m-1").acknowledged

    projection = InboxReadProjection(harness.database)
    rows = projection.conversation_messages(
        ACTOR, CONVERSATION, after=None, limit=50
    )

    assert [row.message_id for row in rows] == ["m-1", "m-2"]
    assert all(isinstance(row, InboxMessage) for row in rows)
    assert rows[0].intent == "request"

    after = (1000, "m-1")
    tail = projection.conversation_messages(
        ACTOR, CONVERSATION, after=after, limit=50
    )
    assert [row.message_id for row in tail] == ["m-2"]


def test_remote_delivered_send_stays_in_the_senders_history(
    harness: _HistoryHarness, tmp_path: Path
) -> None:
    transport = _remote_delivery(harness, tmp_path)
    sent = _send_remote(
        harness, text="hello remote", idempotency_key="remote-send-1"
    )
    message_id = sent["messageId"]
    assert sent["deliveries"][0]["queued"] is False
    assert transport.deliveries == 1

    # The peer's durable store committed it; the sender's store has neither an
    # inbox row nor the (now delivered and deleted) outbox row.
    assert _count(tmp_path / "peer-inbox.sqlite3", "inbox") == 1
    assert _count(harness.database, "inbox") == 0
    assert harness.inbox.outbox_count() == 0

    page = _history(harness, limit=200)
    assert _ids(page) == [message_id]
    entry = page["entries"][0]
    assert entry["from"] == ACTOR
    assert entry["to"] == REMOTE_READER
    assert entry["message"] == "hello remote"


def test_remote_sender_copy_is_not_readable_by_other_participants(
    harness: _HistoryHarness, tmp_path: Path
) -> None:
    _remote_delivery(harness, tmp_path)
    sent = _send_remote(
        harness, text="sender only", idempotency_key="remote-send-2"
    )
    message_id = sent["messageId"]

    projection = InboxReadProjection(harness.database)
    assert [
        message.message_id
        for message in projection.conversation_messages(
            ACTOR, CONVERSATION, limit=50
        )
    ] == [message_id]
    # The peer named as recipient, and an uninvolved local actor, read their
    # own mailbox only; a sender-owned copy is not part of it.
    assert (
        projection.conversation_messages(
            REMOTE_READER, CONVERSATION, limit=50
        )
        == ()
    )
    assert (
        projection.conversation_messages(READER, CONVERSATION, limit=50) == ()
    )
    assert (
        _history(
            harness, actor=READER, session=READER_SESSION, limit=200
        )["entries"]
        == []
    )
    assert (
        _history(harness, actor=OTHER, session=OTHER_SESSION, limit=200)[
            "entries"
        ]
        == []
    )


def test_remote_history_reads_do_not_redeliver_or_mutate_state(
    harness: _HistoryHarness, tmp_path: Path
) -> None:
    transport = _remote_delivery(harness, tmp_path)
    sent = _send_remote(
        harness, text="read only", idempotency_key="remote-send-3"
    )
    message_id = sent["messageId"]
    assert _ids(_history(harness, limit=200)) == [message_id]

    tables = ("inbox", "outbox", "sender_copies", "dlq", "custody", "dedup")
    before = {table: _count(harness.database, table) for table in tables}
    peer_database = tmp_path / "peer-inbox.sqlite3"
    assert _count(peer_database, "inbox") == 1

    for limit in (1, 5, 200):
        assert _ids(_history(harness, limit=limit)) == [message_id]

    assert {table: _count(harness.database, table) for table in tables} == before
    assert transport.deliveries == 1
    assert _count(peer_database, "inbox") == 1

    # The delivery pump has no work: nothing is re-offered, drained or
    # re-inserted by the read or by the next sweep.
    harness.inbox.retry_due(now_ms=10**18)
    assert harness.inbox.outbox_count() == 0
    assert transport.deliveries == 1
    assert _count(peer_database, "inbox") == 1
    assert _ids(_history(harness, limit=200)) == [message_id]


def test_remote_sent_copies_paginate_in_order_with_received_replies(
    harness: _HistoryHarness, tmp_path: Path
) -> None:
    _remote_delivery(harness, tmp_path)
    _submit_remote(harness, "m-remote-1", created_at_ms=1000, text="first")
    _submit_remote(harness, "m-remote-2", created_at_ms=2000, text="second")
    _deliver(
        harness,
        "m-inbound-reply",
        sender=REMOTE_READER,
        recipient=ACTOR,
        intent="reply",
        text="answer",
        created_at_ms=2500,
    )
    _submit_remote(harness, "m-remote-3", created_at_ms=3000, text="third")

    seen: list[str] = []
    cursor: str | None = None
    while True:
        params: dict[str, object] = {"limit": 2}
        if cursor is not None:
            params["cursor"] = cursor
        page = _history(harness, **params)
        seen.extend(_ids(page))
        cursor = page["nextCursor"]
        if cursor is None:
            break

    expected = [
        "m-remote-1",
        "m-remote-2",
        "m-inbound-reply",
        "m-remote-3",
    ]
    assert seen == expected
    assert len(seen) == len(set(seen))

    full = _history(harness, limit=200)
    created = [entry["createdAtMs"] for entry in full["entries"]]
    assert created == sorted(created)


def test_remote_sender_copy_is_idempotent_across_replayed_sends(
    harness: _HistoryHarness, tmp_path: Path
) -> None:
    transport = _remote_delivery(harness, tmp_path)
    first = _send_remote(
        harness, text="once", idempotency_key="remote-idempotent"
    )
    second = _send_remote(
        harness, text="once", idempotency_key="remote-idempotent"
    )
    message_id = first["messageId"]
    assert second["messageId"] == message_id

    # The proxy replay is re-offered to the peer, whose durable dedup answers
    # it; neither side gains a second row.
    assert transport.deliveries == 2
    assert _count(tmp_path / "peer-inbox.sqlite3", "inbox") == 1
    assert _count(harness.database, "sender_copies") == 1
    assert _ids(_history(harness, limit=200)) == [message_id]

    harness.inbox.retry_due(now_ms=10**18)
    assert harness.inbox.outbox_count() == 0
    assert _ids(_history(harness, limit=200)) == [message_id]


def test_remote_sender_copies_do_not_consume_the_outbox_hold_cap(
    harness: _HistoryHarness, tmp_path: Path
) -> None:
    from hyprial.daemon.impl.inbox.links.pull import HoldPolicy

    transport = _remote_delivery(harness, tmp_path)
    harness.inbox.hold_policy = HoldPolicy(max_items=2)
    for index in range(4):
        _submit_remote(
            harness, f"m-delivered-{index}", created_at_ms=1000 + index
        )
    assert harness.inbox.outbox_count() == 0
    assert _count(harness.database, "dlq") == 0

    transport.offline.add(REMOTE_READER)
    for index in range(2):
        _submit_remote(
            harness,
            f"m-queued-{index}",
            created_at_ms=2000 + index,
            queued=True,
        )
    assert harness.inbox.outbox_count() == 2
    assert _count(harness.database, "dlq") == 0
    assert _count(harness.database, "sender_copies") == 6

    history_ids = _ids(_history(harness, limit=200))
    assert len(history_ids) == 6
    for index in range(4):
        assert f"m-delivered-{index}" in history_ids
    for index in range(2):
        assert f"m-queued-{index}" in history_ids
