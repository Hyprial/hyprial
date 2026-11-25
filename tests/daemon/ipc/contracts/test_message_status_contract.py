from __future__ import annotations

import threading

import pytest

from hyprial.daemon.impl.application import DaemonApplication
from hyprial.daemon.impl.inbox import InboxService, MemoryDeliveryTransport
from hyprial.daemon.impl.request_catalog import METHOD_REGISTRY
from hyprial.kernel import (
    DaemonRequestError,
    RequestPortError,
    daemon_request_once,
    ipc_errors,
)
from hyprial.shell.impl.daemon_wiring import daemon_dependencies


def test_message_status_is_declared_and_bound() -> None:
    binding = METHOD_REGISTRY.get("message.status")

    assert binding is not None
    assert binding.spec.name == "message.status"
    assert binding.spec.mutates is False
    assert binding.spec.retry == "safe"
    assert binding.spec.http_exposed is False


def test_message_status_rejects_unknown_parameters() -> None:
    with pytest.raises(DaemonRequestError) as raised:
        METHOD_REGISTRY.invoke(object(), "message.status", {"from": "sender", "extra": True})

    assert raised.value.code == ipc_errors.INVALID_ARGUMENT


@pytest.mark.parametrize(
    ("code", "data"),
    [
        (ipc_errors.INVALID_AGENT_NAME, None),
        (
            ipc_errors.AMBIGUOUS_TARGET,
            {"target": "dispatch-coordinator", "candidates": ["a", "b"]},
        ),
    ],
)
def test_message_status_preserves_reachable_error_code_and_data(code, data) -> None:
    class App:
        def _ipc_message_status(self, params, *, _trusted_message_origin=None):
            raise DaemonRequestError(code, "legacy status failure", data)

    with pytest.raises(DaemonRequestError) as raised:
        METHOD_REGISTRY.invoke(
            App(), "message.status", {"from": "sender", "messageId": None}
        )

    assert raised.value.code == code
    assert raised.value.data == data


def test_message_status_accepts_null_message_selector_without_proxy() -> None:
    binding = METHOD_REGISTRY.get("message.status")
    assert binding is not None

    class App:
        def _ipc_message_status(self, params, *, _trusted_message_origin=None):
            return {
                "ok": True,
                "from": params["from"],
                "records": [],
                "diagnostics": {
                    "localRecords": 0,
                    "meshReplies": 0,
                    "meshRecords": 0,
                    "meshHolders": [],
                    "meshResponders": [],
                    "undecodableReplies": 0,
                    "undecodableHolders": [],
                    "replyErrors": 0,
                    "meshError": None,
                    "holders": [],
                    "missingRecipientHolders": [],
                    "conflictingMessageIds": [],
                    "duplicateHolderRecords": [],
                    "noKnownLoss": True,
                },
            }

    result = METHOD_REGISTRY.invoke(App(), "message.status", {"from": "sender", "messageId": None})
    assert result["records"] == []


def test_message_status_rejects_null_on_behalf_of() -> None:
    with pytest.raises(DaemonRequestError, match="onBehalfOf") as raised:
        METHOD_REGISTRY.invoke(
            object(),
            "message.status",
            {"from": "sender", "onBehalfOf": None},
        )

    assert raised.value.code == ipc_errors.INVALID_ARGUMENT


def test_message_status_rejects_invalid_result_at_registry_boundary() -> None:
    class App:
        def _ipc_message_status(self, params, *, _trusted_message_origin=None):
            return {"ok": True, "records": []}

    with pytest.raises(DaemonRequestError) as raised:
        METHOD_REGISTRY.invoke(App(), "message.status", {"from": "sender"})

    assert raised.value.code == ipc_errors.DAEMON_ERROR


@pytest.mark.parametrize("timeout", [0.1, 10.0])
def test_message_status_accepts_bounded_timeout_and_forwards_origin(timeout) -> None:
    class App:
        seen = None

        def _ipc_message_status(self, params, *, _trusted_message_origin=None):
            self.seen = (params["timeoutSeconds"], _trusted_message_origin)
            return {
                "ok": True,
                "from": "sender",
                "records": [],
                "diagnostics": {
                    "localRecords": 0,
                    "meshReplies": 0,
                    "meshRecords": 0,
                    "meshHolders": [],
                    "meshResponders": [],
                    "undecodableReplies": 0,
                    "undecodableHolders": [],
                    "replyErrors": 0,
                    "meshError": None,
                    "holders": [],
                    "missingRecipientHolders": [],
                    "conflictingMessageIds": [],
                    "duplicateHolderRecords": [],
                    "noKnownLoss": True,
                },
            }

    app = App()
    result = METHOD_REGISTRY.invoke(
        app,
        "message.status",
        {"from": "sender", "timeoutSeconds": timeout},
        trusted_message_origin="ipc",
    )
    assert result["ok"] is True
    assert app.seen == (timeout, "ipc")


@pytest.mark.parametrize(
    "timeout", [None, 0, -1, 11.0, True, "bad", float("nan"), float("inf")]
)
def test_message_status_rejects_invalid_timeout(timeout) -> None:
    with pytest.raises(DaemonRequestError, match=r"range \(0, 10\]") as raised:
        METHOD_REGISTRY.invoke(
            object(), "message.status", {"from": "sender", "timeoutSeconds": timeout}
        )

    assert raised.value.code == ipc_errors.INVALID_ARGUMENT


def test_message_status_cli_entries_round_trip_over_an_isolated_unix_socket(
    tmp_path,
) -> None:
    root = tmp_path / "c01"
    root.mkdir()
    app = DaemonApplication(
        state_dir=root / "state",
        socket_path=root / "daemon.sock",
        node_id="node-c01",
        owner="op-c01",
        **daemon_dependencies(),
    )
    app._inbox = InboxService(root / "inbox.sqlite3", MemoryDeliveryTransport())
    app._harnesses = object()
    app._presence = object()
    class IdleRuntime:
        def poll(self) -> None:
            return None

        def stop(self) -> None:
            return None

    app._runtime = IdleRuntime()
    app._start_server()
    dispatcher = threading.Thread(target=app._serve, daemon=True)
    dispatcher.start()
    try:
        response = daemon_request_once(
            "message.status",
            {
                "from": "user:op-c01",
                "messageId": None,
                "timeoutSeconds": 0.5,
            },
            socket_path=app.socket_path,
            timeout=5.0,
        )
        assert response["ok"] is True
        assert response["records"] == []
        for invalid_timeout in (None, True, "bad", 0, -1, 11, float("inf")):
            with pytest.raises(RequestPortError, match=r"range \(0, 10\]") as raised:
                daemon_request_once(
                    "message.status",
                    {"from": "user:op-c01", "timeoutSeconds": invalid_timeout},
                    socket_path=app.socket_path,
                    timeout=5.0,
                )
            assert raised.value.code == ipc_errors.INVALID_ARGUMENT
        with pytest.raises(RequestPortError, match="onBehalfOf") as raised:
            daemon_request_once(
                "message.status",
                {"from": "user:op-c01", "onBehalfOf": None},
                socket_path=app.socket_path,
                timeout=5.0,
            )
        assert raised.value.code == ipc_errors.INVALID_ARGUMENT
    finally:
        app.stop_event.set()
        if app._server is not None:
            app._server.close()
        dispatcher.join(timeout=2.0)
        app._close()


def test_message_status_schema_unchanged_and_documents_cell_refused_projection(
    tmp_path,
) -> None:
    from hyprial.daemon.impl.inbox import DeliveryLifecycle, InboxMessage
    from hyprial.daemon.impl.request_catalog.message import status as status_catalog

    spec = METHOD_REGISTRY.get("message.status").spec
    assert list(spec.result_schema["properties"]["state"]["enum"]) == [
        "fetched",
        "expired",
        "refused",
        "pending",
        "unknown",
    ]
    assert "CELL_WORK_REFUSED" in (status_catalog.__doc__ or "")
    assert "harness_failure_settlements" in (status_catalog.__doc__ or "")

    root = tmp_path / "cr"
    root.mkdir()
    app = DaemonApplication(
        state_dir=root / "state",
        socket_path=tmp_path / "d.sock",
        node_id="node-cr",
        owner="op-cr",
        **daemon_dependencies(),
    )
    inbox = InboxService(root / "inbox.sqlite3", MemoryDeliveryTransport(), node_id="node-cr")
    app._inbox = inbox
    app._harnesses = object()
    app._presence = object()

    class IdleRuntime:
        def poll(self) -> None:
            return None

        def stop(self) -> None:
            return None

    app._runtime = IdleRuntime()
    dispatcher: threading.Thread | None = None
    try:
        message = InboxMessage(
            message_id="msg-cell-refused-ipc",
            conversation_id="c-1",
            sender="user:op-cr",
            recipient="agent:op-cr:node-cr:worker",
            payload=b'{"message":"hi"}',
            intent="request",
            lifecycle=DeliveryLifecycle.DURABLE_SERVICE,
            created_at_ms=100,
        )
        assert inbox.receive(message, now_ms=100).accepted
        inbox.settle_harness_failure(
            message.recipient,
            message.message_id,
            "CELL_WORK_REFUSED",
            permanent=True,
            max_attempts=3,
            backoff_ms=(1_000,),
            now_ms=200,
        )
        app._start_server()
        dispatcher = threading.Thread(target=app._serve, daemon=True)
        dispatcher.start()
        response = daemon_request_once(
            "message.status",
            {"from": "user:op-cr", "messageId": "msg-cell-refused-ipc", "timeoutSeconds": 0.5},
            socket_path=app.socket_path,
            timeout=5.0,
        )
        assert response["ok"] is True
        assert response["state"] == "refused"
        assert response["records"][0]["reason"] == "CELL_WORK_REFUSED"
    finally:
        app.stop_event.set()
        if app._server is not None:
            app._server.close()
        if dispatcher is not None:
            dispatcher.join(timeout=2.0)
        inbox.close()
        app._close()
