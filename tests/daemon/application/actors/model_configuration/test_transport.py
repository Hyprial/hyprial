"""Loopback HTTP and real AF_UNIX streams use the public method registry.

The startup catalog handshake is synthetic; model operations cross real sockets.
No production daemon, model endpoint, credential or runtime process is involved.
"""

from dataclasses import replace
import json
import socket
import threading
from types import SimpleNamespace

from hyprial.daemon.impl.ipc.server import _IpcServerMixin
from hyprial.daemon.impl.request_catalog import METHOD_REGISTRY
from hyprial.kernel import DaemonRequestError, LocalIpcStream, daemon_request_once
from hyprial.shell.impl.request_http.server import RequestHttpServer
from tests.shell.request_http.test_server import _config, _entries, _fake, _http
from .test_service import configured as configured, app as app, params
from .test_service import _isolated_environment as _isolated_environment


def test_model_methods_cross_http_ipc_with_exact_errors_and_no_runtime(configured, tmp_path, monkeypatch):
    from hyprial.kernel.impl.ipc import client
    from hyprial.daemon.impl.desired_state.bindings.store import ModelBindingStore

    app, _, _, _, cell = configured
    calls, threads = [], []
    owner = _IpcServerMixin()
    owner._ipc_peer = threading.local()

    def update(cell_id, **kwargs):
        nonlocal cell
        assert kwargs["expect_revision"] == cell.revision
        cell = replace(cell, params=kwargs["params"], revision=cell.revision + 1)
        return cell

    app._cell_runtime = SimpleNamespace(
        lookup=lambda _: cell, update=update, health=lambda: SimpleNamespace(source_stale=False),
        identity=SimpleNamespace(authority=cell.authority),
    )

    def dispatch(sock):
        stream = LocalIpcStream(sock)
        try:
            data = bytearray()
            while not data.endswith(b"\n"):
                chunk = stream.recv(65536)
                if not chunk:
                    return
                data.extend(chunk)
            request = json.loads(data)
            calls.append(request["method"])

            def handle():
                envelope = {"version": 1, "id": request["id"]}
                try:
                    envelope["result"] = METHOD_REGISTRY.invoke(
                        app, request["method"], request["params"], trusted_message_origin="ipc")
                except DaemonRequestError as error:
                    envelope["error"] = {"code": error.code, "message": str(error), "data": error.data}
                return envelope

            response = owner._dispatch_ipc_response(
                stream, bytes(data), request_id=request["id"], method=request["method"],
                peer_pid=None, handler=handle,
            )
            if response is not None:
                stream.sendall(json.dumps(response).encode() + b"\n")
        finally:
            stream.close()

    def connect(*_args, **_kwargs):
        server_side, client_side = socket.socketpair()
        thread = threading.Thread(target=dispatch, args=(server_side,))
        threads.append(thread)
        thread.start()
        return LocalIpcStream(client_side)

    monkeypatch.setattr(client, "_connect_daemon_socket", connect)
    handshake, _ = _fake(_entries())

    def request_once(method, params, **kwargs):
        if not method.startswith("agent.model."):
            return handshake(method, params, **kwargs)
        return daemon_request_once(method, params, **kwargs)

    server = RequestHttpServer(_config(tmp_path), request_once=request_once)
    server.start()

    def call(method, body, authenticated=True):
        status, _, raw = _http(server, "POST", "/daemon/request/agent.model." + method,
                               token=server.token if authenticated else None, body=body)
        return status, json.loads(raw)

    try:
        request = params(configured)
        assert call("configure", request, authenticated=False)[0] == 401
        assert calls == []
        status, result = call("configure", request)
        assert status == 200, result
        assert result["result"]["binding"]["configuration"]["model"] == "qwen3.7-flash"
        assert result["result"]["runtime"] == "not-verified"
        status, result = call("status", {"name": "worker"})
        assert status == 200 and result["result"]["cell"]["intentMatches"]
        status, result = call("configure", {**request, "operationId": "stale"})
        assert status == 409 and result["dispatch"] == "dispatched"
        assert result["data"] == {"phase": "bind", "reason": "revision-mismatch"}
        request = {**request, "operationId": "pending", "expectedBindingRevision": 2,
                   "expectedCellRevision": cell.revision}
        request["configuration"] = {**request["configuration"], "model": "hy4-flash"}
        monkeypatch.setattr(ModelBindingStore, "finalize", lambda *a: (_ for _ in ()).throw(OSError()))
        status, result = call("configure", request)
        assert status == 409 and result["code"] == "MODEL_CONFIGURATION_INCOMPLETE"
        status, result = call("status", {"name": "worker"})
        assert status == 200 and result["result"]["operation"]["phase"] == "prepared"
        app._agent_registry.revoke_secret_grant("worker", "model")
        status, result = call("cancel", {"name": "worker", "operationId": "pending",
                                          "expectedBindingRevision": 2})
        assert status == 200 and result["result"]["operation"]["phase"] == "cancelled"
        assert result["result"]["binding"]["bindingRevision"] == 2
        assert result["result"]["cell"]["intentMatches"] is True
        status, result = call("configure", {**request, "operationId": "revoked",
                                              "expectedCellRevision": cell.revision})
        assert status == 403 and result["code"] == "MODEL_AUTH_UNAVAILABLE"
        assert app.desired_state.load().harnesses == ()
        assert "synthetic-bound-key" not in json.dumps(result)
        assert calls.count("agent.model.configure") == 4
    finally:
        server.shutdown()
        for thread in threads:
            thread.join(2)
            assert not thread.is_alive()
