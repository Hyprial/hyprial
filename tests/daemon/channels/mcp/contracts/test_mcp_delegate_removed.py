"""Regression: harness_delegate is gone and no advertised MCP tool routes to a
METHOD_NOT_FOUND daemon method.

``harness_delegate`` shipped invoking daemon method ``message.delegate``, which
``DaemonApplication.handle`` never registered, so every call returned
METHOD_NOT_FOUND. Structured delegation (defer the parent turn, resume it with
the child result) is a stateful subsystem this runtime does not implement -- no
``message.delegate`` dispatch, no ``delegation.inspect/observe/complete``, no
provider-side turn suppression/resume -- and the tool could not have driven a
faithful implementation anyway (it forwarded only ``{to, message}`` with no
``parentMessageId`` to bind a child to the deferred parent). The tool was
therefore removed rather than shipped broken.

These tests pin the root cause (``message.delegate`` is unroutable), the fix
(the tool is not advertised), and the durable guard the alignment suite lacked:
drive every advertised tool through the real daemon dispatch and assert none of
them routes to an unknown method. The file is self-contained so it runs
standalone.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
from mcp.client._memory import InMemoryTransport
from mcp.client.session import ClientSession

from hyprial.daemon.impl.application import DaemonApplication, DaemonRequestError
from hyprial.daemon.impl.inbox import AckResult, InboxMessage, SubmissionResult
from hyprial.daemon.impl.inbox.batch import BatchBounds, BatchSelection
from hyprial.daemon.impl.mcp import StatelessDaemonProxy, create_mcp_server
from hyprial.shell.impl.daemon_wiring import daemon_dependencies as _deep_module_daemon_dependencies


class FakeInbox:
    def pending_messages(self, actor: str):  # type: ignore[no-untyped-def]
        return ()

    def batch_cursor_key(self) -> bytes:
        return b"mcp-delegate-removed-test-key"

    def batch_snapshot(self, recipient: str, **_kwargs: object) -> BatchSelection:
        return BatchSelection(
            actor=recipient,
            bounds=BatchBounds(inbox_high=0, notice_high=0, capture_ordinal=0),
            rows=(),
        )

    def system_notices(self, actor: str) -> tuple[InboxMessage, ...]:
        return ()

    def prune_inbox(self):  # type: ignore[no-untyped-def]
        return []

    def list_progress_events(
        self, recipient: str, *, delivery_id: str | None = None
    ) -> tuple[InboxMessage, ...]:
        return ()

    def submit(self, message: InboxMessage) -> SubmissionResult:
        return SubmissionResult(message.message_id, accepted=True)

    def ack(self, actor: str, message_id: str) -> AckResult:
        return AckResult(message_id, acknowledged=True)

    def outbox_count(self) -> int:
        return 0

    def custody_count(self) -> int:
        return 0


class FakePresence:
    def online_actors(self):  # type: ignore[no-untyped-def]
        return ()


class RecordingBridge:
    """Route proxy requests into the real daemon dispatch, recording the
    ``(method, error_code)`` of every call so routing drift is observable."""

    def __init__(self, app: DaemonApplication) -> None:
        self._app = app
        self.calls: list[tuple[str, str | None]] = []

    async def request(
        self, method: str, params: dict[str, Any], *, request_id: str | None = None
    ) -> dict[str, Any]:
        try:
            result = self._app.handle(method, params)
        except DaemonRequestError as error:
            self.calls.append((method, error.code))
            raise
        self.calls.append((method, None))
        return result


class RecordingBridgeFactory:
    def __init__(self, bridge: RecordingBridge) -> None:
        self._bridge = bridge

    @asynccontextmanager
    async def connect(self) -> AsyncIterator[RecordingBridge]:
        yield self._bridge


def make_daemon(tmp_path: Path) -> DaemonApplication:
    app = DaemonApplication(
        state_dir=tmp_path / "daemon-state",
        socket_path=tmp_path / "daemon.sock",
        node_id="node-1",
    **_deep_module_daemon_dependencies())
    app._inbox = FakeInbox()  # type: ignore[assignment]
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


# Best-effort arguments so each advertised tool actually reaches the daemon.
# Tools not listed are called with ``{}``. Arguments need not succeed -- they
# only need to route past METHOD_NOT_FOUND -- so a nonexistent messageId is
# fine; the daemon rejects it with a different code.
_TOOL_ARGS: dict[str, dict[str, Any]] = {
    "harness_send": {"to": "agent:bob", "summary": "ping", "message": "ping"},
    "harness_reply": {"messageId": "m1", "summary": "done", "message": "done"},
    "harness_read_body": {"messageId": "m3"},
    "harness_ack": {"messageId": "m2"},
    "harness_progress": {},
    "harness_targets": {"kind": "agent"},
    "orgfs_spaces": {},
    "orgfs_create": {"name": "surface-route-test"},
    "orgfs_resolve": {"spaceId": "missing-space", "path": "missing"},
    "orgfs_ls": {"spaceId": "missing-space", "path": "missing"},
    "orgfs_stat": {"spaceId": "missing-space", "node": "missing"},
    "orgfs_read": {"spaceId": "missing-space", "node": "missing"},
    "orgfs_export": {
        "spaceId": "missing-space",
        "node": "missing",
        "destination": "/missing/export",
    },
    "orgfs_import": {
        "spaceId": "missing-space",
        "node": "missing",
        "source": str(Path(__file__)),
    },
    "orgfs_write": {
        "spaceId": "missing-space",
        "node": "missing",
        "text": "content",
    },
    "orgfs_mkdir": {"spaceId": "missing-space", "path": "missing"},
    "orgfs_move": {
        "spaceId": "missing-space",
        "sourcePath": "missing",
        "destinationPath": "elsewhere",
    },
    "orgfs_purge_status": {
        "spaceId": "missing-space",
        "planId": "missing-plan",
    },
    "orgfs_remove": {"spaceId": "missing-space", "node": "missing"},
    "orgfs_history": {"spaceId": "missing-space", "node": "missing"},
    "orgfs_read_at": {
        "spaceId": "missing-space",
        "node": "missing",
        "version": "missing-version",
    },
    "orgfs_stat_at": {
        "spaceId": "missing-space",
        "node": "missing",
        "version": "missing-version",
    },
    "orgfs_trash": {"spaceId": "missing-space"},
    "orgfs_restore": {
        "spaceId": "missing-space",
        "node": "missing",
        "version": "missing-version",
    },
    "orgfs_watch": {"spaceId": "missing-space"},
    "orgfs_status": {"spaceId": "missing-space"},
    "orgfs_invite": {"spaceId": "missing-space", "user": "user:bob"},
    "orgfs_remove_member": {
        "spaceId": "missing-space",
        "user": "user:bob",
    },
    "orgfs_members": {"spaceId": "missing-space"},
    "orgfs_join": {"spaceId": "missing-space"},
}


def test_message_delegate_is_an_unregistered_daemon_method(tmp_path: Path) -> None:
    """Root cause: the daemon dispatch never registered ``message.delegate``."""

    app = make_daemon(tmp_path)
    try:
        with pytest.raises(DaemonRequestError) as error:
            app.handle(
                "message.delegate",
                {
                    "actor": "alice",
                    "sessionRef": "session-1",
                    "to": "agent:child",
                    "message": "work",
                },
            )

        assert error.value.code == "METHOD_NOT_FOUND"
    finally:
        app._close()


@pytest.mark.anyio
async def test_delegate_tool_is_not_advertised_and_surface_routes(
    tmp_path: Path,
) -> None:
    app = make_daemon(tmp_path)
    try:
        bridge = RecordingBridge(app)
        proxy = StatelessDaemonProxy(RecordingBridgeFactory(bridge))
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
            names = {tool.name for tool in (await client.list_tools()).tools}

            # The fix: the broken capability is no longer advertised.
            assert "harness_delegate" not in names
            # Sanity: the healthy surface is still present.
            assert {"harness_send", "harness_whoami", "harness_read"} <= names

            # Durable guard for the whole bug class: every advertised tool must
            # route to a registered daemon method (this is what the alignment
            # suite failed to enforce, letting message.delegate ship dead).
            for name in sorted(names):
                await client.call_tool(name, _TOOL_ARGS.get(name, {}))

        routed_methods = {method for method, _code in bridge.calls}
        # Every advertised tool must actually have reached the daemon; a tool
        # rejected at MCP schema validation would never hit RecordingBridge and
        # would make the METHOD_NOT_FOUND guard below pass vacuously. Pinning the
        # exact routed set proves the guard is meaningful.
        assert routed_methods == {
            "message.send",
            "message.reply",
            "message.pending.batch",
            "message.read_body",
            "message.ack",
            "progress.list",
            "identity.whoami",
            "targets",
            "orgfs.spaces",
            "orgfs.create",
            "orgfs.resolve",
            "orgfs.ls",
            "orgfs.stat",
            "orgfs.read",
            "orgfs.export",
            "orgfs.import",
            "orgfs.write",
            "orgfs.mkdir",
            "orgfs.move",
            "orgfs.purge_status",
            "orgfs.remove",
            "orgfs.history",
            "orgfs.read_at",
            "orgfs.stat_at",
            "orgfs.trash",
            "orgfs.restore",
            "orgfs.watch",
            "orgfs.status",
            "orgfs.invite",
            "orgfs.remove_member",
            "orgfs.members",
            "orgfs.join",
        }
        assert "message.delegate" not in routed_methods
        unrouted = [
            method for method, code in bridge.calls if code == "METHOD_NOT_FOUND"
        ]
        assert unrouted == [], (
            f"advertised MCP tools route to unregistered daemon methods: {unrouted}"
        )
    finally:
        app._close()
