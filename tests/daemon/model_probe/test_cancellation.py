"""Disconnect closes the real probe socket through HTTP and local IPC."""

import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import socket
import threading
from types import SimpleNamespace
from urllib.parse import urlsplit
import pytest

from hyprial.daemon.impl.ipc.server import _IpcServerMixin
from hyprial.daemon.impl.request_catalog import METHOD_REGISTRY
from hyprial.kernel import (
    LocalIpcStream, RequestCancellation, daemon_request_once, ipc_errors, request_cancellation,
)
from hyprial.shell.impl.request_http.server import RequestHttpServer
from tests.shell.request_http.test_server import _config, _entries, _fake


@pytest.mark.parametrize('cancel_by', ['disconnect', 'ipc-deadline'])
@pytest.mark.parametrize('source', ['temporary', 'saved'])
def test_http_disconnect_cancels_one_ipc_probe_and_closes_upstream(tmp_path, monkeypatch, cancel_by, source):
    entered, upstream_closed, worker_done = (threading.Event() for _ in range(3))
    count = []

    class Upstream(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            count.append((body['model'], self.headers.get('Authorization')))
            self.send_response(200)
            self.send_header('Content-Length', '100')
            self.end_headers()
            self.wfile.flush()
            self.connection.settimeout(5)
            entered.set()
            try:
                if self.rfile.read(1) == b'':
                    upstream_closed.set()
            except OSError:
                pass

        def log_message(self, *_args):
            pass

    upstream = ThreadingHTTPServer(('127.0.0.1', 0), Upstream)
    upstream.daemon_threads = True
    upstream_thread = threading.Thread(target=upstream.serve_forever, daemon=True)
    upstream_thread.start()
    home = tmp_path / 'home'
    home.mkdir(mode=0o700)
    app = SimpleNamespace(hyprial_home=home, _agent_registry=SimpleNamespace())
    endpoint = f'http://127.0.0.1:{upstream.server_port}/v1'
    params = {'provider': 'custom', 'secret': 'synthetic', 'model': 'hy4-flash',
              'endpoint': endpoint, 'protocol': 'openai-completions', 'timeoutSeconds': 60}
    if source == 'saved':
        result = METHOD_REGISTRY.invoke(app, 'agent.secret.provider-configure', {
            'entryId': 'custom', 'fieldName': 'key', 'value': 'synthetic', 'expectedRevision': 0,
            'connection': {'version': 1, 'protocol': 'openai-completions',
                           'profile': 'openai-chat-completions/v1', 'endpoint': endpoint},
        }, trusted_message_origin='ipc')
        metadata = result['connections']['key']
        params = {'connection': {'entryId': 'custom', 'fieldName': 'key',
                  'connectionRef': metadata['connectionRef'], 'configRevision': metadata['configRevision']},
                  'model': 'hy4-flash', 'timeoutSeconds': 60}
    server_socket, client_socket = socket.socketpair()
    owner = _IpcServerMixin()
    owner._ipc_peer = threading.local()
    if cancel_by == 'ipc-deadline':
        from hyprial.daemon.impl.ipc import server as ipc_server
        monkeypatch.setattr(ipc_server, '_IPC_REQUEST_TIMEOUT', 1.0)

    def dispatch():
        stream = LocalIpcStream(server_socket)
        try:
            data = bytearray()
            while not data.endswith(b'\n'):
                data.extend(stream.recv(65536))
            request = json.loads(data)

            def handle():
                try:
                    result = METHOD_REGISTRY.invoke(app, request['method'], request['params'],
                                                    trusted_message_origin='ipc')
                    return {'version': 1, 'id': request['id'], 'result': result}
                finally:
                    worker_done.set()

            response = owner._dispatch_ipc_response(stream, bytes(data), request_id=request['id'],
                method=request['method'], peer_pid=None, handler=handle)
            if response is not None:
                stream.sendall(json.dumps(response).encode() + b'\n')
        finally:
            stream.close()

    daemon_thread = threading.Thread(target=dispatch, daemon=True)
    daemon_thread.start()
    from hyprial.kernel.impl.ipc import client
    monkeypatch.setattr(client, '_connect_daemon_socket',
                        lambda *_args, **_kwargs: LocalIpcStream(client_socket))
    handshake, _ = _fake(_entries())

    def request_once(method, params, **kwargs):
        if method != 'agent.secret.provider-test':
            return handshake(method, params, **kwargs)
        return daemon_request_once(method, params, **kwargs)

    server = RequestHttpServer(_config(tmp_path), request_once=request_once)
    server.start()
    endpoint = urlsplit(server.endpoint)
    connection = http.client.HTTPConnection(endpoint.hostname, endpoint.port, timeout=5)
    try:
        connection.request('POST', '/daemon/request/agent.secret.provider-test',
            body=json.dumps(params),
            headers={'Authorization': 'Bearer ' + server.token, 'Content-Type': 'application/json'})
        assert entered.wait(3)
        if cancel_by == 'disconnect':
            connection.sock.shutdown(socket.SHUT_RDWR)
            connection.close()
        else:
            response = connection.getresponse()
            assert json.loads(response.read())['code'] == ipc_errors.IPC_TIMEOUT
        assert upstream_closed.wait(2), 'upstream must close before the 60-second probe deadline'
        assert worker_done.wait(2), 'probe capacity must be released after cancellation'
        assert count == [('hy4-flash', 'Bearer synthetic')]
    finally:
        connection.close()
        client_socket.close()
        server_socket.close()
        server.shutdown()
        upstream.shutdown()
        upstream.server_close()
        daemon_thread.join(2)
        upstream_thread.join(2)


def test_cancelled_probe_never_connects_to_daemon(tmp_path, monkeypatch):
    from hyprial.kernel.impl.ipc import client

    def forbidden(*args, **kwargs):
        pytest.fail('cancelled probe must not open an IPC connection')

    monkeypatch.setattr(client, '_connect_daemon_socket', forbidden)
    cancellation = RequestCancellation()
    cancellation.cancel()
    with request_cancellation(cancellation), pytest.raises(ipc_errors.DaemonDisconnectedError):
        daemon_request_once('agent.secret.provider-test', {}, socket_path=tmp_path / 'unused.sock')
