"""End-to-end host tests for the directory and session-create operations.

Split from ``test_app_adapter_host`` only to keep each test module inside the
1000-code-line structural limit.  These drive a real AF_UNIX host socket with
the fake daemon port defined by the parent module.
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

import pytest

from tests.shell.gui.adapter.test_app_adapter_host import (
    SENDER,
    frame,
    host_socket_sessions,
    start_host,
)

ECHO_TARGET = "agent:chloe.martin@example.com:dev000000001.local:echo"


@pytest.fixture()
def host_socket():
    """A short AF_UNIX socket path (macOS sun_path is finite) and its server."""

    directory = tempfile.mkdtemp(prefix="apc-dir-", dir="/tmp")
    socket_path = Path(directory) / "s"
    yield directory, socket_path
    shutil.rmtree(directory, ignore_errors=True)


@pytest.fixture(autouse=True)
def stop_hosts():
    """Join every host thread so no socket fd survives into the fd guard."""

    yield
    for stop, thread in host_socket_sessions:
        stop.set()
    for _, thread in host_socket_sessions:
        thread.join(timeout=3.0)
    host_socket_sessions.clear()


def stable_binding(target_uri: str) -> tuple[str, str]:
    session_id = str(uuid5(NAMESPACE_URL, f"hyprial:gui:session:{SENDER}:{target_uri}"))
    conversation_id = str(
        uuid5(NAMESPACE_URL, f"hyprial:gui:conversation:{SENDER}:{target_uri}")
    )
    return session_id, conversation_id


def test_host_serves_the_directory_then_ensures_and_uses_a_session(host_socket) -> None:
    directory, socket_path = host_socket
    session_id, conversation_id = stable_binding(ECHO_TARGET)
    daemon = start_host(
        host_socket,
        {"hosts": [{"nodeId": "node-a", "status": "online"}]},
        {
            "targets": [
                {
                    "targetKind": "agent",
                    "targetUri": ECHO_TARGET,
                    "actor": "agent:echo",
                    "status": "online",
                    "deliverable": True,
                    "hosted": True,
                    "hostedBy": "node-a",
                }
            ]
        },
        {
            "targets": [
                {
                    "targetKind": "agent",
                    "targetUri": ECHO_TARGET,
                    "actor": "agent:echo",
                    "status": "online",
                    "deliverable": True,
                    "hosted": True,
                    "hostedBy": "node-a",
                }
            ]
        },
        {
            "ok": True,
            "operationId": "op-1",
            "conversationId": conversation_id,
            "deliveries": [
                {
                    "target": ECHO_TARGET,
                    "messageId": "m-1",
                    "accepted": True,
                    "queued": False,
                }
            ],
            "messageId": "m-1",
        },
        {
            "ok": True,
            "conversationId": conversation_id,
            "entries": [
                {
                    "kind": "message",
                    "messageId": "m-1",
                    "conversationId": conversation_id,
                    "from": SENDER,
                    "to": ECHO_TARGET,
                    "intent": "request",
                    "createdAtMs": 1000,
                    "message": "hello",
                }
            ],
            "nextCursor": None,
        },
    )

    assert frame(socket_path, {"operation": "targets.list", "params": {}}) == {
        "ok": True,
        "result": {
            "nodes": [{"nodeId": "node-a", "status": "online"}],
            "targets": [
                {
                    "targetUri": ECHO_TARGET,
                    "targetKind": "agent",
                    "status": "online",
                    "hosted": True,
                    "hostedBy": "node-a",
                }
            ],
            "counts": {"nodes": 1, "targets": 1},
            "truncated": {"nodes": False, "targets": False},
        },
    }
    created = frame(
        socket_path,
        {"operation": "sessions.create", "params": {"targetUri": ECHO_TARGET}},
    )
    assert created == {
        "ok": True,
        "result": {
            "sessionId": session_id,
            "targetUri": ECHO_TARGET,
            "status": "online",
            "created": True,
        },
    }
    assert frame(socket_path, {"operation": "sessions.list", "params": {}}) == {
        "ok": True,
        "result": [
            {"sessionId": "s-1"},
            {"sessionId": session_id, "title": ECHO_TARGET, "targetUri": ECHO_TARGET},
        ],
    }
    sent = frame(
        socket_path,
        {
            "operation": "sessions.send",
            "params": {
                "sessionId": session_id,
                "request": {"text": "hello", "idempotencyKey": "k-1"},
            },
        },
    )
    assert sent["ok"] is True
    assert sent["result"]["status"] == "accepted"
    history = frame(
        socket_path,
        {
            "operation": "sessions.history",
            "params": {"sessionId": session_id, "args": {"limit": 50}},
        },
    )
    assert history["ok"] is True
    assert [event["text"] for event in history["result"]["events"]] == ["hello"]

    assert daemon.calls == [
        ("hosts", {}),
        ("targets", {"kind": "agent"}),
        ("targets", {"kind": "agent"}),
        (
            "message.send",
            {
                "from": SENDER,
                "to": [ECHO_TARGET],
                "message": "hello",
                "summary": "hello",
                "conversationId": conversation_id,
                "idempotencyKey": "k-1",
            },
        ),
        (
            "message.history",
            {"from": SENDER, "conversationId": conversation_id, "limit": 50},
        ),
    ]


def test_host_refuses_directory_parameters_and_create_identity_before_the_daemon(
    host_socket,
) -> None:
    directory, socket_path = host_socket
    daemon = start_host(host_socket)

    assert frame(
        socket_path,
        {"operation": "targets.list", "params": {"kind": "agent"}},
    )["error"]["code"] == "invalid-argument"
    assert frame(
        socket_path,
        {"operation": "sessions.create", "params": {"from": SENDER}},
    )["error"]["code"] == "identity-rejected"
    assert frame(
        socket_path,
        {
            "operation": "sessions.create",
            "params": {"targetUri": ECHO_TARGET, "actor": "someone-else"},
        },
    )["error"]["code"] == "identity-rejected"
    assert frame(
        socket_path,
        {
            "operation": "sessions.create",
            "params": {"targetUri": ECHO_TARGET, "extra": 1},
        },
    )["error"]["code"] == "invalid-argument"
    assert frame(
        socket_path,
        {"operation": "sessions.create", "params": {}},
    )["error"]["code"] == "invalid-argument"
    assert daemon.calls == []
