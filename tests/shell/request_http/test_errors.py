"""dispatch is a source/stage label; the same code can differ by origin."""

from __future__ import annotations

from pathlib import Path
import time

from hyprial import __version__
from hyprial.daemon import daemon_catalog as local_catalog
from hyprial.kernel import DaemonRequestError, RequestPortError, ipc_errors
from hyprial.shell.impl.request_http.catalog import build_view
from hyprial.shell.impl.request_http.config import Budgets, RequestHttpConfig
from hyprial.shell.impl.request_http.errors import HttpFailure, retryable
from hyprial.shell.impl.request_http.server import RequestHttpServer

PING = {
    "version": __version__,
    "nodeId": "h01-node",
    "owner": "h01-owner",
    "epoch": "epoch-1",
    "phase": "serving",
}


def _server() -> tuple[RequestHttpServer, object]:
    config = RequestHttpConfig(
        socket_path=Path("/tmp/daemon.sock"),
        owner="h01-owner",
        actor="agent:h01-owner:h01-node:operator",
        host="127.0.0.1",
        port=0,
        allowed_origins=(),
        allowed_hosts=("127.0.0.1",),
        budgets=Budgets(15.0, 5.0, 30.0, 262144, 8388608, 8, 5.0),
        runtime_dir=Path("/tmp/request-http-runtime"),
        source=Path("/tmp/config.json"),
    )
    entries = []
    for entry in local_catalog()["methods"]:
        item = dict(entry)
        item["declared"] = True
        item["state"] = "available" if entry["httpExposed"] else "disabled"
        item["callable"] = item["state"] == "available"
        item["reason"] = None
        entries.append(item)
    view = build_view(
        PING,
        {
            "schemaVersion": 1,
            "catalogVersion": "1",
            "epoch": "epoch-1",
            "packageVersion": __version__,
            "methods": entries,
        },
        socket_path="/tmp/daemon.sock",
        local_entries=local_catalog()["methods"],
    )
    server = RequestHttpServer(config, request_once=lambda *a, **k: None)
    server._view = view
    server._startup_declaration = view.declaration_digest
    server._startup_http = view.http_contract_digest
    server._snapshot_at = time.monotonic()
    return server, view.routes["link.list"]


def test_local_schema_refusal_is_not_dispatched() -> None:
    failure = HttpFailure("INVALID_ARGUMENT", "bad field")
    assert (failure.status, failure.dispatch, failure.outcome_unknown) == (400, "not-dispatched", False)


def test_retry_permit_is_a_closed_set() -> None:
    assert retryable("HTTP_BUSY") and retryable("IPC_TIMEOUT")
    assert not retryable("CATALOG_STALE")
    assert not retryable("INVALID_ARGUMENT")


def test_daemon_declared_error_is_dispatched() -> None:
    server, route = _server()
    envelope = server._daemon_failure(
        RequestPortError("INVALID_ARGUMENT", "declared"), route, "http-1", "link.list"
    )
    assert envelope.status == 400
    assert envelope.payload["dispatch"] == "dispatched"
    assert envelope.payload["code"] == "INVALID_ARGUMENT"


def test_daemon_not_ready_is_dispatched_but_restore_is_not() -> None:
    server, route = _server()
    ready = server._daemon_failure(
        RequestPortError(ipc_errors.DAEMON_NOT_READY, "not wired"), route, "http-1", "link.list"
    )
    assert (ready.status, ready.payload["dispatch"]) == (503, "dispatched")
    restoring = server._daemon_failure(
        ipc_errors.DaemonRestoringError("restoring"), route, "http-1", "link.list"
    )
    assert (restoring.status, restoring.payload["dispatch"]) == (503, "not-dispatched")


def test_timeout_and_unknown_daemon_error_are_unknown() -> None:
    server, route = _server()
    timeout = server._daemon_failure(
        ipc_errors.IpcTimeoutError("timeout"), route, "http-1", "link.list"
    )
    assert (timeout.status, timeout.payload["dispatch"]) == (504, "unknown")
    unknown = server._daemon_failure(
        RequestPortError("SOMETHING_NEW", "unknown"), route, "http-1", "link.list"
    )
    assert unknown.payload["code"] == "DAEMON_ERROR"
    assert unknown.payload["dispatch"] == "unknown"
    declared_error = server._daemon_failure(
        DaemonRequestError("DAEMON_ERROR", "x"), route, "http-1", "link.list"
    )
    assert (declared_error.status, declared_error.payload["dispatch"]) == (500, "unknown")


def test_declared_error_data_must_match_its_schema() -> None:
    server, route = _server()
    accepted = server._daemon_failure(
        RequestPortError("INVALID_ARGUMENT", "x", {"actor": "agent:a:b:c"}),
        route,
        "http-1",
        "link.list",
    )
    assert accepted.payload["data"] == {"actor": "agent:a:b:c"}
    dropped = server._daemon_failure(
        RequestPortError("INVALID_ARGUMENT", "x", {"claimedIdentity": "secret"}),
        route,
        "http-1",
        "link.list",
    )
    assert "data" not in dropped.payload
