"""MCP tool surface must stay aligned with the real daemon method registry.

The MCP unit tests exercise the server against a permissive fake connection,
which is how two shipped mismatches went unnoticed: ``harness_whoami`` invoked
``identity.whoami`` (METHOD_NOT_FOUND on the daemon) and ``harness_send``
forwarded ``to`` as a bare string (daemon rejects with INVALID_ARGUMENT: to
must be a non-empty array). These tests run every affected tool through the
real ``DaemonApplication.handle`` so any future drift fails here instead of in
production.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
from mcp.client._memory import InMemoryTransport
from mcp.client.session import ClientSession

from hyprial.kernel import ipc_errors
from hyprial.daemon.impl.application import DaemonApplication, DaemonRequestError
from hyprial.daemon.impl.inbox import AckResult, InboxMessage, SubmissionResult
from hyprial.daemon.impl.mcp import StatelessDaemonProxy, create_mcp_server
from hyprial.daemon.impl.mcp.api import DaemonRequestRejected
from hyprial.daemon.impl.inbox.authority.errors import InboxSubmissionOutcomeUnknown
from hyprial.shell.impl.daemon_wiring import daemon_dependencies as _deep_module_daemon_dependencies
from tests.daemon.path_authz.test_send_gate import (
    ACTOR_NAME,
    SESSION,
    gated as gated,
)
from tests.support.runtime._owned_applications import owned_applications as owned_applications


class FakeInbox:
    def __init__(self) -> None:
        self.submitted: list[InboxMessage] = []

    def pending_messages(self, actor: str):  # type: ignore[no-untyped-def]
        return ()

    def prune_inbox(self):  # type: ignore[no-untyped-def]
        return []

    def fetch_pending(self, actor: str):  # type: ignore[no-untyped-def]
        return self.pending_messages(actor)

    def submit(self, message: InboxMessage, **_link: object) -> SubmissionResult:
        self.submitted.append(message)
        return SubmissionResult(message.message_id, accepted=True)

    def ack(self, actor: str, message_id: str) -> AckResult:
        return AckResult(message_id, acknowledged=True)

    def outbox_count(self) -> int:
        return 0

    def custody_count(self) -> int:
        return 0


class UnknownOutcomeInbox(FakeInbox):
    def submit(self, message: InboxMessage, **_link: object) -> SubmissionResult:
        self.submitted.append(message)
        return SubmissionResult(
            message.message_id,
            accepted=True,
            queued=True,
            code=ipc_errors.SUBMIT_OUTCOME_UNKNOWN,
        )


class TypedUnknownOutcomeInbox(FakeInbox):
    def submit(self, message: InboxMessage, **_link: object) -> SubmissionResult:
        self.submitted.append(message)
        raise InboxSubmissionOutcomeUnknown(message, "inbox:submit:controlled")


class FakePresence:
    def online_actors(self):  # type: ignore[no-untyped-def]
        return ()


class DaemonBridgeConnection:
    """Route proxy requests into the real daemon dispatch, not a fake."""

    def __init__(self, app: DaemonApplication) -> None:
        self._app = app

    async def request(
        self, method: str, params: dict[str, Any], *, request_id: str | None = None
    ) -> dict[str, Any]:
        return self._app.handle(method, params)


class DaemonBridgeFactory:
    def __init__(self, app: DaemonApplication) -> None:
        self._app = app

    @asynccontextmanager
    async def connect(self) -> AsyncIterator[DaemonBridgeConnection]:
        yield DaemonBridgeConnection(self._app)


class DaemonWireBridgeConnection(DaemonBridgeConnection):
    async def request(self, method, params, *, request_id=None):
        frame = {"version": 1, "id": request_id, "method": method, "params": params}
        response = self._app._response(json.dumps(frame).encode())
        if "error" in response:
            error = response["error"]
            raise DaemonRequestRejected(error["code"], error["message"], error.get("data"))
        return response["result"]


class DaemonWireBridgeFactory(DaemonBridgeFactory):
    @asynccontextmanager
    async def connect(self) -> AsyncIterator[DaemonBridgeConnection]:
        yield DaemonWireBridgeConnection(self._app)


def daemon(tmp_path: Path, inbox: FakeInbox, own_daemon_application) -> DaemonApplication:
    # Register ownership before registration can raise and pin the app in a
    # traceback; successful-path GC is not failure-path cleanup.
    app = own_daemon_application(DaemonApplication(
        state_dir=tmp_path / "daemon-state",
        socket_path=tmp_path / "daemon.sock",
        node_id="node-1",
        owner="op",
    **_deep_module_daemon_dependencies()))
    app._inbox = inbox
    app._harnesses = object()  # type: ignore[assignment]
    app._presence = FakePresence()  # type: ignore[assignment]
    app.handle(
        "session.register",
        {
            "actor": "alice",
            "sessionRef": "session-1",
            "cwd": "/work/alice",
            "command": ["claude"],
            "source": "claude-channel",
            "channelConfirmed": True,
        },
    )
    return app


def structured(result) -> dict[str, Any]:  # type: ignore[no-untyped-def]
    if result.structured_content is not None:
        return result.structured_content
    return json.loads(result.content[0].text)


def test_factory_owns_app_before_initial_registration_can_fail(
    tmp_path: Path, monkeypatch, own_daemon_application,
) -> None:
    owned = []

    def own(app):
        owned.append(app)
        return own_daemon_application(app)

    def fail_registration(self, method, params):
        assert method == "session.register"
        raise RuntimeError("injected registration failure")

    monkeypatch.setattr(DaemonApplication, "handle", fail_registration)
    with pytest.raises(RuntimeError, match="injected registration failure") as caught:
        daemon(tmp_path, FakeInbox(), own)
    assert len(owned) == 1
    assert caught.value.__traceback__ is not None


@pytest.mark.anyio
async def test_every_mcp_tool_method_resolves_against_the_real_daemon(
    tmp_path: Path, own_daemon_application,
) -> None:
    inbox = FakeInbox()
    app_daemon = daemon(tmp_path, inbox, own_daemon_application)
    proxy = StatelessDaemonProxy(DaemonBridgeFactory(app_daemon))
    server = create_mcp_server(
        proxy,
        actor="alice",
        session_ref="session-1",
    )

    async with (
        InMemoryTransport(server) as (read_stream, write_stream),
        ClientSession(read_stream, write_stream) as client,
    ):
        await client.initialize()

        # Regression: identity.whoami was METHOD_NOT_FOUND on the daemon.
        whoami = await client.call_tool("harness_whoami", {})
        assert whoami.is_error is not True
        identity = structured(whoami)
        assert identity["actor"] == "agent:op:node-1:alice"
        assert identity["sessionRef"] == "session-1"
        assert identity["nodeId"] == "node-1"
        assert identity["sessionRegistered"] is True

        # Regression: the string `to` from the MCP schema reached the daemon
        # unwrapped and was rejected with "to must be a non-empty array".
        # (A canonical four-segment target: bare/two-segment forms are
        # node-shaped and the send-time admission gate rejects them.)
        send = await client.call_tool(
            "harness_send", {"to": "agent:op:node-2:bob", "summary": "ping", "message": "ping"}
        )
        assert send.is_error is not True
        assert structured(send)["ok"] is True
        # Canonical URIs pass through normalization untouched.
        assert [message.recipient for message in inbox.submitted] == [
            "agent:op:node-2:bob"
        ]

        # Same drift class: the server invoked "targets.list"; the daemon
        # registers "targets".
        targets = await client.call_tool("harness_targets", {"kind": "agent"})
        assert targets.is_error is not True
        # Registering a session now also registers an Agent, so the actor is a
        # known target with a real liveness verdict (it just registered, so it
        # is online). Before the agent entity this list only ever held actors
        # zenoh presence already reported, and every row was stamped with a
        # hard-coded "online" literal that could not be falsified.
        listed = structured(targets)["targets"]
        assert len(listed) == 1
        assert listed[0]["targetKind"] == "agent"
        assert listed[0]["targetUri"].endswith(":node-1:alice")
        assert listed[0]["status"] == "online"


@pytest.mark.anyio
async def test_harness_send_preserves_accepted_unknown_submit_outcome(
    tmp_path: Path, own_daemon_application,
) -> None:
    inbox = UnknownOutcomeInbox()
    app_daemon = daemon(tmp_path, inbox, own_daemon_application)
    server = create_mcp_server(
        StatelessDaemonProxy(DaemonBridgeFactory(app_daemon)),
        actor="alice",
        session_ref="session-1",
    )

    async with (
        InMemoryTransport(server) as (read_stream, write_stream),
        ClientSession(read_stream, write_stream) as client,
    ):
        await client.initialize()
        result = await client.call_tool(
            "harness_send",
            {"to": "agent:op:node-2:bob", "summary": "ping", "message": "ping"},
        )

    assert result.is_error is not True
    payload = structured(result)
    assert payload["ok"] is True
    assert payload["deliveries"][0]["accepted"] is True
    assert payload["deliveries"][0]["outcomeKnown"] is False
    assert payload["deliveries"][0]["code"] == ipc_errors.SUBMIT_OUTCOME_UNKNOWN
    assert "reason" not in payload["deliveries"][0]


@pytest.mark.anyio
async def test_harness_send_preserves_typed_unknown_identity_without_success(
    tmp_path: Path, own_daemon_application,
) -> None:
    inbox = TypedUnknownOutcomeInbox()
    app_daemon = daemon(tmp_path, inbox, own_daemon_application)
    server = create_mcp_server(
        StatelessDaemonProxy(DaemonWireBridgeFactory(app_daemon)),
        actor="alice", session_ref="session-1",
    )
    async with (
        InMemoryTransport(server) as (read_stream, write_stream),
        ClientSession(read_stream, write_stream) as client,
    ):
        await client.initialize()
        result = await client.call_tool(
            "harness_send", {"to": "agent:op:node-2:bob", "summary": "ping", "message": "ping"},
        )
    assert result.is_error is True
    # The SDK prefixes ToolError text; the JSON payload must retain identity.
    text = result.content[0].text
    payload = json.loads(text[text.index("{"):])
    assert payload["code"] == ipc_errors.SUBMIT_OUTCOME_UNKNOWN
    assert payload["data"]["outcomeKnown"] is False
    assert payload["data"]["commandAdmission"] == "accepted"
    assert payload["data"]["messageId"] == inbox.submitted[0].message_id
    assert payload["data"]["idempotencyKey"] == inbox.submitted[0].idempotency_key
    assert payload["data"]["submissionCorrelationId"] == "inbox:submit:controlled"
    assert len(inbox.submitted) == 1  # UNKNOWN is not a generic reconnect retry.


def test_identity_whoami_is_a_registered_daemon_method(tmp_path: Path, own_daemon_application) -> None:
    app_daemon = daemon(tmp_path, FakeInbox(), own_daemon_application)

    result = app_daemon.handle(
        "identity.whoami", {"actor": "alice", "sessionRef": "session-1"}
    )

    assert result == {
        "ok": True,
        "actor": "agent:op:node-1:alice",
        "sessionRef": "session-1",
        "nodeId": "node-1",
        "sessionRegistered": True,
        "channelCurrentThisGeneration": True,
        "channelCurrentEpoch": app_daemon.epoch,
        "daemonEpoch": app_daemon.epoch,
    }

    # A stale session still gets an answer instead of an error, with the
    # registration mismatch made visible.
    stale = app_daemon.handle(
        "identity.whoami", {"actor": "alice", "sessionRef": "session-0"}
    )
    assert stale["sessionRegistered"] is False


def test_daemon_send_still_rejects_a_bare_string_target(tmp_path: Path, own_daemon_application) -> None:
    """The daemon-side array contract is unchanged; only the server wraps."""

    app_daemon = daemon(tmp_path, FakeInbox(), own_daemon_application)

    with pytest.raises(DaemonRequestError) as error:
        app_daemon.handle(
            "message.send",
            {
                "actor": "alice",
                "sessionRef": "session-1",
                "to": "agent:bob",
                "summary": "ping",
                "message": "ping",
            },
        )
    assert error.value.code == "INVALID_ARGUMENT"


@pytest.mark.anyio
async def test_mcp_orgfs_export_refuses_existing_by_default_and_overwrites_explicitly(
    gated,
) -> None:
    """m4: the MCP default is non-destructive; only overwrite=true replaces."""

    app_daemon = gated["app"]
    space_id = app_daemon.handle(
        "orgfs.create", {"name": "mcp-export"}, _trusted_message_origin="ipc"
    )["space_id"]
    app_daemon.handle(
        "orgfs.write",
        {"spaceId": space_id, "node": "report.txt", "text": "from orgfs"},
        _trusted_message_origin="ipc",
    )
    destination = gated["repo"] / "report.txt"
    destination.write_text("keep existing", encoding="utf-8")
    server = create_mcp_server(
        StatelessDaemonProxy(DaemonWireBridgeFactory(app_daemon)),
        actor=ACTOR_NAME,
        session_ref=SESSION,
    )

    async with (
        InMemoryTransport(server) as (read_stream, write_stream),
        ClientSession(read_stream, write_stream) as client,
    ):
        await client.initialize()
        refused = await client.call_tool(
            "orgfs_export",
            {
                "spaceId": space_id,
                "node": "report.txt",
                "destination": str(destination),
            },
        )
        assert refused.is_error is True
        assert "exists" in refused.content[0].text
        assert destination.read_text(encoding="utf-8") == "keep existing"

        replaced = await client.call_tool(
            "orgfs_export",
            {
                "spaceId": space_id,
                "node": "report.txt",
                "destination": str(destination),
                "overwrite": True,
            },
        )

    assert replaced.is_error is not True
    assert destination.read_text(encoding="utf-8") == "from orgfs"
