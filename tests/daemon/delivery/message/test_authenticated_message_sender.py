"""Message IPC claims are bound to the daemon session that owns the actor."""

from __future__ import annotations

import json
import os
import socket
from pathlib import Path

import pytest
from mcp.client._memory import InMemoryTransport
from mcp.client.session import ClientSession

from hyprial.daemon.impl.application import DaemonApplication,DaemonRequestError
from hyprial.daemon.impl.ipc.params import _peer_process_id
from hyprial.daemon.impl.inbox import DeliveryLifecycle, InboxMessage, InboxService, MemoryDeliveryTransport
from hyprial.daemon.impl.mcp import StatelessDaemonProxy, create_mcp_server
from hyprial.kernel import canonical_agent_uri
from hyprial.shell.impl.cli.commands.workflow.control import _WorkflowNotificationSender
from hyprial.shell.impl.daemon_wiring import daemon_dependencies as _deep_module_daemon_dependencies
from tests.support.links.gate import PermissiveLinkGate, ScriptedLinkGate
from hyprial.shell.impl.cli.commands.common import support as _cli_support


OWNER = "op"
NODE = "node-1"
ACTOR_NAME = "worker-b"
ACTOR = f"agent:{OWNER}:{NODE}:{ACTOR_NAME}"
SESSION = "session-worker-b"


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
    def __init__(self) -> None:
        super().__init__()
        self.service: InboxService | None = None

    def is_online(self, _recipient: str) -> bool:
        return True

    def deliver(self, message: InboxMessage) -> bool:
        assert self.service is not None
        return self.service.receive(message).accepted


def _assemble_app(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, gate: object
) -> tuple[DaemonApplication, InboxService, Path]:
    """A disposable application/state pair with one bound link gate.

    No request reaches a live daemon; the caller chooses the gate so the same
    assembly can prove both the permissive premise and a deny-all control.
    """

    monkeypatch.setenv("HYPRIAL_SENDER_AUTH_MODE", "enforce")
    state = tmp_path / "state"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    app = DaemonApplication(
        state_dir=state,
        socket_path=state / "daemon.sock",
        node_id=NODE,
        owner=OWNER,
    **_deep_module_daemon_dependencies())
    transport = _LoopbackTransport()
    inbox = InboxService(
        tmp_path / "inbox.sqlite3", transport, link_admission=app.link_admission
    )
    app.link_admission.bind(gate)
    transport.service = inbox
    app._inbox = inbox
    app._presence = _Presence()  # type: ignore[assignment]
    app._harnesses = _Harnesses()  # type: ignore[assignment]
    app.handle("agent.create", {"name": ACTOR_NAME})
    app.handle("agent.create", {"name": "reader"})
    for actor, session_ref in ((ACTOR_NAME, SESSION), ("reader", "session-reader")):
        app.handle(
            "session.register",
            {
                "actor": actor,
                "sessionRef": session_ref,
                "cwd": str(workspace),
                "command": ["test-carrier"],
                "source": "claude-channel",
                "runtime": "claude_interactive",
            },
        )
    return app, inbox, workspace


def _close_app(app: DaemonApplication) -> None:
    app._harnesses = None
    try:
        app._close()
    finally:
        app._close_home_lease()


@pytest.fixture
def isolated_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A disposable application/state pair; no request reaches a live daemon."""

    # Explicit permissive premise: agent→agent rows flow through a slot with
    # no real gate yet (review item 3).
    app, inbox, _workspace = _assemble_app(
        tmp_path, monkeypatch, PermissiveLinkGate()
    )
    try:
        pending = InboxMessage(
            message_id="pending-for-worker-b",
            conversation_id="conversation-auth",
            sender="channel:lark:test",
            recipient=ACTOR,
            payload=b'{"message":"please respond"}',
            intent="request",
            lifecycle=DeliveryLifecycle.DURABLE_SERVICE,
            created_at_ms=1,
        )
        assert inbox.receive(pending).accepted
        yield app
    finally:
        _close_app(app)


@pytest.fixture
def deny_gate_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """The same assembly with a gate that knows no sender (review item 1)."""

    app, _inbox, _workspace = _assemble_app(
        tmp_path, monkeypatch, ScriptedLinkGate(known=set(), default_admit=False)
    )
    try:
        yield app
    finally:
        _close_app(app)


@pytest.mark.parametrize(
    ("method", "params"),
    [
        (
            "message.send",
            {"from": ACTOR, "to": [f"agent:{OWNER}:{NODE}:reader"], "message": "hi"},
        ),
        (
            "message.reply",
            {"from": ACTOR, "messageId": "pending-for-worker-b", "message": "done"},
        ),
        ("message.ack", {"from": ACTOR, "messageId": "pending-for-worker-b"}),
        ("message.pending.list", {"from": ACTOR}),
        ("progress.list", {"from": ACTOR}),
        ("message.status", {"from": ACTOR}),
    ],
)
def test_registered_agent_claim_without_session_is_rejected(
    isolated_app: DaemonApplication,
    method: str,
    params: dict[str, object],
) -> None:
    with pytest.raises(DaemonRequestError) as refused:
        isolated_app.handle(method, params, _trusted_message_origin="ipc")

    assert refused.value.code == "SENDER_NOT_AUTHENTICATED"
    assert ACTOR in str(refused.value)
    assert "no matching session was presented" in str(refused.value)
    assert "--on-behalf-of" in str(refused.value)


def test_every_message_path_refreshes_activity_as_on_dev(
    isolated_app: DaemonApplication, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sender auth changes who is authenticated, not who counts as active.

    dev queued an activity refresh for the resolved sender on every send; an
    operator send on behalf of an actor refreshes that actor, and so does an
    observed (unauthenticated, observe-mode) claim.
    """

    refreshed: list[str] = []
    monkeypatch.setattr(isolated_app, "_queue_agent_activity", refreshed.append)
    isolated_app.handle(
        "message.send",
        {
            "from": f"user:{OWNER}",
            "onBehalfOf": ACTOR,
            "to": [f"agent:{OWNER}:{NODE}:reader"],
            "summary": "operator relay",
            "message": "operator relay",
        },
        _trusted_message_origin="ipc",
    )
    isolated_app.handle(
        "message.send",
        {
            "from": ACTOR,
            "sessionRef": SESSION,
            "to": [f"agent:{OWNER}:{NODE}:reader"],
            "summary": "session send",
            "message": "session send",
        },
        _trusted_message_origin="ipc",
    )

    assert refreshed == [ACTOR, ACTOR]


def test_operator_send_is_never_delivered_as_the_claimed_actor(
    isolated_app: DaemonApplication,
) -> None:
    result = isolated_app.handle(
        "message.send",
        {
            "from": f"user:{OWNER}",
            "onBehalfOf": ACTOR,
            "to": [f"agent:{OWNER}:{NODE}:reader"],
            "summary": "operator relay",
            "message": "operator relay",
        },
        _trusted_message_origin="ipc",
    )
    message_id = result["messageId"]

    pending = isolated_app.handle(
        "message.pending.list",
        {
            "from": f"agent:{OWNER}:{NODE}:reader",
            "sessionRef": "session-reader",
        },
    )
    row = next(item for item in pending["messages"] if item["messageId"] == message_id)
    assert row["from"] == f"user:{OWNER}"
    assert row["origin"] == {"via": "operator", "onBehalfOf": ACTOR}

    query = isolated_app.handle(
        "message.query", {"from": f"agent:{OWNER}:{NODE}:reader", "view": "inbox"}
    )
    queried = next(item for item in query["entries"] if item["messageId"] == message_id)
    assert queried["origin"] == row["origin"]


def test_verified_lark_adapter_origin_keeps_the_adapter_path(
    isolated_app: DaemonApplication,
) -> None:
    result = isolated_app.handle(
        "message.send",
        {
            "from": "channel:lark:test",
            "to": [f"agent:{OWNER}:{NODE}:reader"],
            "summary": "adapter inbound",
            "message": "adapter inbound",
            "providerMetadata": {
                "provider": "lark",
                "chatType": "p2p",
                "sender": {
                    "standing": "verified",
                    "displayName": "Operator",
                    "owner": OWNER,
                },
            },
        },
        _trusted_message_origin="ipc",
    )
    pending = isolated_app.handle(
        "message.pending.list",
        {
            "from": f"agent:{OWNER}:{NODE}:reader",
            "sessionRef": "session-reader",
        },
    )
    row = next(
        item for item in pending["messages"] if item["messageId"] == result["messageId"]
    )
    assert row["origin"]["via"] == "adapter"
    assert row["origin"]["sender"]["owner"] == OWNER


class _Connection:
    def __init__(self, app: DaemonApplication) -> None:
        self.app = app

    async def request(self, method, params, *, request_id=None):  # type: ignore[no-untyped-def]
        del request_id
        return self.app.handle(method, params, _trusted_message_origin="ipc")


class _ConnectionContext:
    def __init__(self, app: DaemonApplication) -> None:
        self.connection = _Connection(app)

    async def __aenter__(self):  # type: ignore[no-untyped-def]
        return self.connection

    async def __aexit__(self, *_args):  # type: ignore[no-untyped-def]
        return False


class _ConnectionFactory:
    def __init__(self, app: DaemonApplication) -> None:
        self.app = app

    def connect(self) -> _ConnectionContext:
        return _ConnectionContext(self.app)


@pytest.mark.anyio
async def test_mcp_harness_send_carries_the_registered_session(
    isolated_app: DaemonApplication,
) -> None:
    server = create_mcp_server(
        StatelessDaemonProxy(_ConnectionFactory(isolated_app)),
        actor=ACTOR,
        session_ref=SESSION,
    )
    async with (
        InMemoryTransport(server) as (read_stream, write_stream),
        ClientSession(read_stream, write_stream) as client,
    ):
        await client.initialize()
        result = await client.call_tool(
            "harness_send",
            {"to": f"agent:{OWNER}:{NODE}:reader", "summary": "from MCP", "message": "from MCP"},
        )

    assert result.is_error is not True


def test_worker_workflow_notification_stays_from_the_worker_session(
    isolated_app: DaemonApplication,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HYPRIAL_WORKER_ACTOR", ACTOR)
    monkeypatch.setenv("HYPRIAL_WORKER_SESSION_REF", SESSION)
    monkeypatch.setattr(
        _cli_support,
        "_daemon_request",
        lambda method, params: isolated_app.handle(
            method, params, _trusted_message_origin="ipc"
        ),
    )

    message_id = _WorkflowNotificationSender().send(
        recipient=f"agent:{OWNER}:{NODE}:reader",
        text="workflow complete notification",
        sender=ACTOR,
        conversation_id="workflow-test",
        idempotency_key="workflow-notification-test",
        summary="workflow complete notification",
    )

    pending = isolated_app.handle(
        "message.pending.list",
        {
            "from": f"agent:{OWNER}:{NODE}:reader",
            "sessionRef": "session-reader",
        },
    )
    row = next(item for item in pending["messages"] if item["messageId"] == message_id)
    assert row["from"] == ACTOR
    assert row["origin"] == {"via": "session"}


def test_real_ipc_dispatch_rejects_and_counts_the_unauthenticated_claim(
    isolated_app: DaemonApplication,
) -> None:
    records: list[tuple[str, dict[str, object]]] = []
    isolated_app._log = (  # type: ignore[method-assign]
        lambda _level, _component, event, **fields: records.append((event, fields))
    )
    frame = json.dumps(
        {
            "version": 1,
            "id": "ipc-auth-red",
            "method": "message.send",
            "params": {
                "from": ACTOR,
                "to": [f"agent:{OWNER}:{NODE}:reader"],
                "message": "forged",
            },
        }
    ).encode()

    response = isolated_app._response(frame)

    assert response["error"]["code"] == "SENDER_NOT_AUTHENTICATED"
    event, fields = records[-1]
    assert event == "send.sender_unauthenticated"
    assert fields["method"] == "message.send"
    assert fields["claimedActor"] == ACTOR
    assert fields["count"] == 1


def test_local_peer_pid_is_recordable_when_the_platform_exposes_it() -> None:
    local, peer = socket.socketpair()
    try:
        observed = _peer_process_id(local)
    finally:
        local.close()
        peer.close()

    if observed is not None:
        assert observed == os.getpid()


def test_observe_rollout_mode_logs_but_does_not_reject(
    isolated_app: DaemonApplication,
) -> None:
    records: list[tuple[str, dict[str, object]]] = []
    isolated_app._sender_auth_enforced = False
    isolated_app._log = (  # type: ignore[method-assign]
        lambda _level, _component, event, **fields: records.append((event, fields))
    )

    response = isolated_app._response(
        json.dumps(
            {
                "version": 1,
                "id": "ipc-auth-observe",
                "method": "message.send",
                "params": {
                    "from": ACTOR,
                    "to": [f"agent:{OWNER}:{NODE}:reader"],
                    "summary": "observed",
                    "message": "observed",
                },
            }
        ).encode()
    )

    assert response["result"]["ok"] is True
    assert any(event == "send.sender_unauthenticated" for event, _fields in records)


def test_observe_preserves_sessionless_dev_semantics_with_one_log_per_call(
    isolated_app: DaemonApplication,
) -> None:
    isolated_app._sender_auth_enforced = False
    records: list[tuple[str, dict[str, object]]] = []
    isolated_app._log = (  # type: ignore[method-assign]
        lambda _level, _component, event, **fields: records.append((event, fields))
    )
    assert isinstance(isolated_app._inbox, InboxService)
    for message_id in ("observe-reply", "observe-ack"):
        assert isolated_app._inbox.receive(
            InboxMessage(
                message_id=message_id,
                conversation_id="conversation-observe",
                sender=f"agent:{OWNER}:{NODE}:reader",
                recipient=ACTOR,
                payload=b'{"message":"observe compatibility"}',
                intent="request",
                lifecycle=DeliveryLifecycle.DURABLE_SERVICE,
                created_at_ms=2,
            )
        ).accepted

    def ipc(method: str, params: dict[str, object], request_id: str) -> dict[str, object]:
        before = sum(
            event == "send.sender_unauthenticated" for event, _fields in records
        )
        response = isolated_app._response(
            json.dumps(
                {
                    "version": 1,
                    "id": request_id,
                    "method": method,
                    "params": params,
                }
            ).encode()
        )
        assert "error" not in response, response
        after = sum(
            event == "send.sender_unauthenticated" for event, _fields in records
        )
        assert after == before + 1
        result = response.get("result")
        assert isinstance(result, dict)
        return result

    sent = ipc(
        "message.send",
        {
            "from": ACTOR,
            "to": [f"agent:{OWNER}:{NODE}:reader"],
            "summary": "observe send",
            "message": "observe send",
        },
        "observe-send",
    )
    assert sent["ok"] is True

    pending = ipc("message.pending.list", {"from": ACTOR}, "observe-pending")
    assert {item["messageId"] for item in pending["messages"]} >= {
        "pending-for-worker-b",
        "observe-reply",
        "observe-ack",
    }

    replied = ipc(
        "message.reply",
        {
            "from": ACTOR,
            "messageId": "observe-reply",
            "summary": "observe reply",
            "message": "observe reply",
        },
        "observe-reply-request",
    )
    assert replied["replied"] is True
    assert replied["acknowledged"] is True

    acknowledged = ipc(
        "message.ack",
        {"from": ACTOR, "messageId": "observe-ack"},
        "observe-ack-request",
    )
    assert acknowledged["acknowledged"] is True

    unauthenticated = [
        fields
        for event, fields in records
        if event == "send.sender_unauthenticated"
    ]
    assert [fields["method"] for fields in unauthenticated] == [
        "message.send",
        "message.pending.list",
        "message.reply",
        "message.ack",
    ]


def test_rollout_defaults_to_observe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("HYPRIAL_SENDER_AUTH_MODE", raising=False)
    state = tmp_path / "default-observe"
    app = DaemonApplication(
        state_dir=state,
        socket_path=state / "daemon.sock",
        node_id=NODE,
        owner=OWNER,
    **_deep_module_daemon_dependencies())
    try:
        assert app._sender_auth_enforced is False
    finally:
        app.agents.close()


def test_registered_agent_claim_with_its_session_can_send(
    isolated_app: DaemonApplication,
) -> None:
    result = isolated_app.handle(
        "message.send",
        {
            "from": ACTOR_NAME,
            "sessionRef": SESSION,
            "to": [f"agent:{OWNER}:{NODE}:reader"],
            "summary": "authenticated",
            "message": "authenticated",
        },
        _trusted_message_origin="ipc",
    )

    assert result["ok"] is True
    assert result["actingPrincipal"] == ACTOR


def test_message_rejects_registered_session_after_liveness_release(
    isolated_app: DaemonApplication,
) -> None:
    isolated_app._agent_liveness.release(ACTOR)

    with pytest.raises(DaemonRequestError) as refused:
        isolated_app.handle(
            "message.send",
            {
                "from": ACTOR,
                "sessionRef": SESSION,
                "to": [f"agent:{OWNER}:{NODE}:reader"],
                "summary": "dead carrier",
                "message": "must not send",
            },
            _trusted_message_origin="ipc",
        )

    assert refused.value.code == "STALE_SESSION"


def test_message_keeps_managed_worker_session_authority(
    isolated_app: DaemonApplication, monkeypatch: pytest.MonkeyPatch
) -> None:
    managed = f"agent:{OWNER}:{NODE}:managed"
    isolated_app.handle("agent.create", {"name": "managed"})
    monkeypatch.setattr(
        isolated_app,
        "_worker_session_ref",
        lambda actor: "managed-session" if actor == managed else None,
    )

    result = isolated_app.handle(
        "message.send",
        {
            "from": managed,
            "sessionRef": "managed-session",
            "to": [f"agent:{OWNER}:{NODE}:reader"],
            "summary": "managed worker",
            "message": "managed worker",
        },
        _trusted_message_origin="ipc",
    )

    assert result["actingPrincipal"] == managed


def test_another_actors_session_cannot_authenticate_the_claim(
    isolated_app: DaemonApplication,
) -> None:
    isolated_app.handle("agent.create", {"name": "worker-a"})
    isolated_app.handle(
        "session.register",
        {
            "actor": "worker-a",
            "sessionRef": "session-worker-a",
            "cwd": str(isolated_app.state_dir.parent / "workspace"),
            "command": ["test-carrier"],
            "source": "claude-channel",
            "runtime": "claude_interactive",
        },
    )

    with pytest.raises(DaemonRequestError) as refused:
        isolated_app.handle(
            "message.send",
            {
                "from": ACTOR,
                "sessionRef": "session-worker-a",
                "to": [f"agent:{OWNER}:{NODE}:reader"],
                "message": "forged",
            },
            _trusted_message_origin="ipc",
        )

    # The fence's own verdict is kept (a Channel child reads it to retire),
    # and the refusal must not hand the forger the real owner's credential.
    assert refused.value.code == "SESSION_SUPERSEDED"
    assert SESSION not in str(refused.value)


# README ruling 63 / rework 3: the reserved mechanism actor names must not be
# usable as a session identity either.  ``link_governed`` exempts them as
# sender or recipient on ANY node, so a session registered under one would
# send with the exemption and bypass every link constraint.
RESERVED_SESSION_ACTOR_INPUTS = (
    "_turn-hooks",
    f"agent:{OWNER}:{NODE}:_turn-hooks",
    "agent:x:y:dispatch-coordinator",
    "agent:x:y:_turn-hooks",
)
NEAR_RESERVED_SESSION_ACTOR_INPUTS = (
    "dispatch-coordinator2",
    "turn-hooks",
    "_turn-hooks2",
)


def _canonical_session_actor(actor: str) -> str:
    return actor if ":" in actor else canonical_agent_uri(OWNER, NODE, actor)


def _registered_session_actors(app: DaemonApplication) -> set[str]:
    return {
        session.actor
        for session in app._agent_session_domains.session.read_sessions()
    }


@pytest.mark.parametrize("actor", RESERVED_SESSION_ACTOR_INPUTS)
def test_reserved_actor_names_cannot_register_a_session(
    isolated_app: DaemonApplication, actor: str
) -> None:
    """The registration guard must cover the session entry point too.

    ``session.register`` needs no registry row, so the previous AgentRegistry
    guard alone let ``_turn-hooks`` and any node's ``dispatch-coordinator``
    establish a session and send with the link exemption.
    """

    workspace = isolated_app.state_dir.parent / "workspace"
    with pytest.raises(DaemonRequestError) as refused:
        isolated_app.handle(
            "session.register",
            {
                "actor": actor,
                "sessionRef": f"session-{actor}",
                "cwd": str(workspace),
                "command": ["test-carrier"],
                "source": "claude-channel",
                "runtime": "claude_interactive",
            },
        )

    assert refused.value.code == "INVALID_AGENT_NAME"
    assert "reserved" in str(refused.value)
    assert _canonical_session_actor(actor) not in _registered_session_actors(
        isolated_app
    )


@pytest.mark.parametrize("actor", NEAR_RESERVED_SESSION_ACTOR_INPUTS)
def test_near_reserved_actor_names_still_register_a_session(
    isolated_app: DaemonApplication, actor: str
) -> None:
    """The guard is exact equality on the actor segment, never a prefix."""

    workspace = isolated_app.state_dir.parent / "workspace"
    result = isolated_app.handle(
        "session.register",
        {
            "actor": actor,
            "sessionRef": f"session-{actor}",
            "cwd": str(workspace),
            "command": ["test-carrier"],
            "source": "claude-channel",
            "runtime": "claude_interactive",
        },
    )

    assert result["actor"] == _canonical_session_actor(actor)
    assert _canonical_session_actor(actor) in _registered_session_actors(
        isolated_app
    )


@pytest.mark.parametrize(
    "actor",
    [
        "_turn-hooks",
        "agent:x:y:_turn-hooks",
        "agent:x:y:dispatch-coordinator",
    ],
)
def test_reserved_actor_names_cannot_send_with_a_session(
    deny_gate_app: DaemonApplication, actor: str
) -> None:
    """A pre-existing session must not let the reserved name past the guard.

    The guard runs before the session fence, so a stale or replayed
    ``sessionRef`` cannot resurrect the identity.  The deny-all gate proves the
    message never even reached an admission decision.
    """

    with pytest.raises(DaemonRequestError) as refused:
        deny_gate_app.handle(
            "message.send",
            {
                "from": actor,
                "sessionRef": "legacy-reserved-session",
                "to": [f"agent:{OWNER}:{NODE}:reader"],
                "message": "bypass",
            },
            _trusted_message_origin="ipc",
        )

    assert refused.value.code == "INVALID_AGENT_NAME"
    gate = deny_gate_app.link_admission.gate
    assert isinstance(gate, ScriptedLinkGate)
    assert gate.sender_known_calls() == []
    assert deny_gate_app._inbox.pending_messages(
        f"agent:{OWNER}:{NODE}:reader"
    ) == ()


def test_deny_gate_still_governs_an_ordinary_agent_session(
    deny_gate_app: DaemonApplication,
) -> None:
    """Control: an ordinary agent still reaches ``sender_known`` and is refused."""

    deny_gate_app.handle(
        "message.send",
        {
            "from": ACTOR,
            "sessionRef": SESSION,
            "to": [f"agent:{OWNER}:{NODE}:reader"],
            "summary": "ordinary",
            "message": "ordinary",
        },
        _trusted_message_origin="ipc",
    )

    gate = deny_gate_app.link_admission.gate
    assert isinstance(gate, ScriptedLinkGate)
    assert gate.sender_known_calls() == [(f"agent:{OWNER}:{NODE}:reader", ACTOR)]
