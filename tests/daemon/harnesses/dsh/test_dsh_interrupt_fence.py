from __future__ import annotations

import asyncio
import http.client
import json
import socket
import threading
import time
from types import SimpleNamespace

import pytest

from hyprial.daemon.impl.api import HarnessDelivery, HarnessResultStatus
from hyprial.kernel import HarnessLaunchSpec
import hyprial.daemon.impl.harnesses.dsh.api as dsh_module
from hyprial.daemon.impl.harnesses.dsh import DshApiClient, DshApiError, DshHarnessProcess, DshHttpApi


def _spec(name: str = "dsh-interrupt") -> HarnessLaunchSpec:
    return HarnessLaunchSpec(
        harness="dsh",
        name=name,
        headless=True,
    )


def _delivery(delivery_id: str, message: str) -> HarnessDelivery:
    return HarnessDelivery(
        delivery_id=delivery_id,
        conversation_id=delivery_id,
        sender="agent:test:sender",
        recipient="agent:test:dsh",
        message=message,
    )


def _wait_result(process: DshHarnessProcess, timeout: float = 2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        results = process.drain_results()
        if results:
            return results[0]
        time.sleep(0.005)
    raise AssertionError("timed out waiting for DSH result")


class _InterruptibleApi:
    def __init__(self) -> None:
        self.session_id = "session-fenced"
        self.prompt_count = 0
        self.cancelled = False
        self.prompt_started = threading.Event()

    async def call(self, method: str, payload: dict[str, object]) -> object:
        del payload
        if method == "host.describe":
            return {"version": "test"}
        if method == "session.create":
            return {"sessionId": self.session_id}
        if method == "session.prompt":
            self.prompt_count += 1
            self.prompt_started.set()
            return {"accepted": True}
        if method == "session.cancel":
            self.cancelled = True
            return {"accepted": True}
        if method == "session.history":
            if self.prompt_count == 1 and self.cancelled:
                events = [
                    {
                        "event": {
                            "type": "turn/end",
                            "seq": 1,
                            "data": {"reason": {"kind": "cancelled"}},
                        }
                    }
                ]
            elif self.prompt_count >= 2:
                events = [
                    {
                        "event": {
                            "type": "assistant/message",
                            "seq": 2,
                            "data": {
                                "message": {
                                    "content": [{"type": "text", "text": "second-ok"}]
                                }
                            },
                        }
                    },
                    {
                        "event": {
                            "type": "turn/end",
                            "seq": 3,
                            "data": {"reason": {"kind": "completed"}},
                        }
                    },
                ]
            else:
                events = []
            return {"events": events}
        raise AssertionError(f"unexpected method {method}")


def test_interrupt_is_fenced_and_same_session_accepts_next_turn() -> None:
    api = _InterruptibleApi()
    spec = _spec()
    process = DshHarnessProcess(
        spec,
        client_factory=lambda: DshApiClient(
            spec,
            api=api,
            poll_interval_seconds=0.001,
        ),
    )
    try:
        assert process.wait_ready(timeout=1)
        assert process.enqueue(_delivery("first", "cancel this"))
        assert api.prompt_started.wait(1)
        assert process.interrupt("first", timeout=1)
        interrupted = _wait_result(process)
        assert interrupted.status is HarnessResultStatus.INTERRUPTED

        assert process.enqueue(_delivery("second", "continue"))
        completed = _wait_result(process)
        assert completed.status is HarnessResultStatus.COMPLETED
        assert completed.output == "second-ok"
        assert api.prompt_count == 2
    finally:
        process.stop()
        process.stop()


class _BlockedClient:
    running = False
    exit_error = None

    def __init__(self) -> None:
        self.query_started = threading.Event()
        self.released = threading.Event()

    async def __aenter__(self):
        self.running = True
        return self

    async def __aexit__(self, *args: object) -> bool:
        del args
        self.running = False
        return False

    async def query(self, prompt: str) -> None:
        del prompt
        self.query_started.set()

    async def receive_response(self):
        await asyncio.to_thread(self.released.wait)
        yield SimpleNamespace(result="released", is_error=True)

    async def interrupt(self) -> None:
        return None

    def force_stop(self) -> None:
        self.released.set()

    def force_stopped(self) -> bool:
        return self.released.is_set()


def test_process_force_closes_blocked_local_io_with_bounded_double_stop() -> None:
    client = _BlockedClient()
    process = DshHarnessProcess(_spec(), client_factory=lambda: client)
    assert process.wait_ready(timeout=1)
    assert process.enqueue(_delivery("blocked", "block"))
    assert client.query_started.wait(1)

    started = time.monotonic()
    process.stop()
    assert time.monotonic() - started < 3.5
    assert client.released.is_set()
    process.stop()


class _BlockedResolverRequest:
    def __init__(self, started: threading.Event, released: threading.Event) -> None:
        self.started = started
        self.released = released

    def result(self, timeout: float):
        del timeout
        self.started.set()
        self.released.wait(10)
        raise DshApiError("resolver released after cancellation")

    def cancel(self) -> None:
        self.released.set()

    def close(self) -> None:
        self.released.set()


class _ConcurrentResolverRequest:
    def __init__(self) -> None:
        self.started = threading.Event()
        self.released = threading.Event()
        self.cancelled = False

    def result(self, timeout: float):
        del timeout
        self.started.set()
        self.released.wait(10)
        raise DshApiError("concurrent resolver released")

    def cancel(self) -> None:
        self.cancelled = True
        self.released.set()

    def close(self) -> None:
        self.released.set()


def test_cancel_active_cancels_every_concurrent_cache_miss_resolver(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolvers = [_ConcurrentResolverRequest(), _ConcurrentResolverRequest()]
    remaining = list(resolvers)
    factory_lock = threading.Lock()

    def start_resolver(_host: str, _port: int) -> _ConcurrentResolverRequest:
        with factory_lock:
            return remaining.pop(0)

    monkeypatch.setattr(dsh_module, "_start_resolver", start_resolver)
    api = DshHttpApi("http://concurrent-resolver.invalid:3080", timeout_seconds=30)
    errors: list[BaseException] = []

    def request() -> None:
        try:
            api._call_sync("session.history", {"sessionId": "s"})
        except BaseException as error:  # noqa: BLE001 - asserted below
            errors.append(error)

    threads = [threading.Thread(target=request) for _ in resolvers]
    for thread in threads:
        thread.start()
    assert all(resolver.started.wait(1) for resolver in resolvers)

    api.cancel_active()
    for thread in threads:
        thread.join(1)
    cancelled = [resolver.cancelled for resolver in resolvers]
    alive = [thread.is_alive() for thread in threads]
    stopped = api.stopped()

    for resolver in resolvers:
        resolver.released.set()
    for thread in threads:
        thread.join(1)
    api.close()

    assert cancelled == [True, True]
    assert alive == [False, False]
    assert stopped
    assert len(errors) == 2


def test_process_force_closes_pre_socket_resolver_on_first_stop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = threading.Event()
    released = threading.Event()
    original_getaddrinfo = socket.getaddrinfo

    def blocked_getaddrinfo(host: str, *args: object, **kwargs: object):
        if host != "blocked-resolver.invalid":
            return original_getaddrinfo(host, *args, **kwargs)
        started.set()
        released.wait(10)
        raise socket.gaierror("resolver released after cancellation")

    monkeypatch.setattr(socket, "getaddrinfo", blocked_getaddrinfo)
    monkeypatch.setattr(
        dsh_module,
        "_start_resolver",
        lambda _host, _port: _BlockedResolverRequest(started, released),
        raising=False,
    )
    spec = HarnessLaunchSpec(
        harness="dsh",
        name="dsh-resolver-stop",
        headless=True,
    )
    api = DshHttpApi("http://blocked-resolver.invalid:13080", timeout_seconds=30)
    process = DshHarnessProcess(
        spec,
        client_factory=lambda: DshApiClient(spec, api=api),
    )
    assert started.wait(1)

    failure: BaseException | None = None
    started_at = time.monotonic()
    try:
        process.stop()
    except BaseException as error:  # noqa: BLE001 - re-raised after cleanup
        failure = error
    elapsed = time.monotonic() - started_at
    released.set()
    if failure is not None:
        process.stop()
        raise failure
    assert elapsed < 3.5
    assert not process._thread.is_alive()
    assert process._force_stopped_client()


def test_process_force_closes_pre_connection_socket_on_first_stop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = threading.Event()
    released = threading.Event()
    original_connect = socket.socket.connect

    def blocked_connect(instance: socket.socket, address: object) -> None:
        if address != ("127.0.0.1", 13080):
            return original_connect(instance, address)  # type: ignore[arg-type]
        started.set()
        while instance.fileno() >= 0 and not released.wait(0.01):
            pass
        raise OSError("connect socket closed during cancellation")

    monkeypatch.setattr(socket.socket, "connect", blocked_connect)
    spec = HarnessLaunchSpec(
        harness="dsh",
        name="dsh-connect-stop",
        headless=True,
    )
    api = DshHttpApi("http://127.0.0.1:13080", timeout_seconds=30)
    process = DshHarnessProcess(
        spec,
        client_factory=lambda: DshApiClient(spec, api=api),
    )
    assert started.wait(1)

    failure: BaseException | None = None
    started_at = time.monotonic()
    try:
        process.stop()
    except BaseException as error:  # noqa: BLE001 - re-raised after cleanup
        failure = error
    elapsed = time.monotonic() - started_at
    released.set()
    if failure is not None:
        process.stop()
        raise failure
    assert elapsed < 3.5
    assert not process._thread.is_alive()
    assert process._force_stopped_client()


def test_real_http_fence_unblocks_a_stalled_call_and_rejects_stale_calls() -> None:
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)

    def serve_one() -> None:
        connection, _address = listener.accept()
        try:
            request = b""
            while b"\r\n\r\n" not in request:
                request += connection.recv(4096)
            headers, body = request.split(b"\r\n\r\n", 1)
            length_line = next(
                line
                for line in headers.split(b"\r\n")
                if line.lower().startswith(b"content-length:")
            )
            content_length = int(length_line.split(b":", 1)[1])
            while len(body) < content_length:
                body += connection.recv(4096)
            rpc_id = json.loads(body[:content_length])["rpcId"]
            started.set()
            release.wait(5)
            response_body = json.dumps(
                {
                    "rpcId": rpc_id,
                    "result": {"ok": True, "value": {"events": []}},
                }
            ).encode()
            response = (
                b"HTTP/1.1 200 OK\r\nContent-Length: "
                + str(len(response_body)).encode()
                + b"\r\nConnection: close\r\n\r\n"
                + response_body
            )
            try:
                connection.sendall(response)
            except (BrokenPipeError, ConnectionResetError):
                pass
        finally:
            connection.close()
            finished.set()

    server_thread = threading.Thread(target=serve_one)
    server_thread.start()
    api = DshHttpApi(
        f"http://localhost:{listener.getsockname()[1]}", timeout_seconds=30
    )

    errors: list[BaseException] = []

    def request() -> None:
        try:
            api._call_sync("session.history", {"sessionId": "s"})
        except BaseException as error:  # noqa: BLE001 - asserted below
            errors.append(error)

    request_thread = threading.Thread(target=request)

    try:
        request_thread.start()
        assert started.wait(2), errors
        api.close()
        request_thread.join(timeout=1)
        assert not request_thread.is_alive()
        assert len(errors) == 1 and isinstance(errors[0], DshApiError)
        assert api.stopped()
        with pytest.raises(DshApiError, match="transport is closed"):
            api._call_sync("session.history", {"sessionId": "s"})
    finally:
        release.set()
        assert finished.wait(1)
        listener.close()
        server_thread.join(timeout=1)
        assert not server_thread.is_alive()


def _accept_and_read_request(listener: socket.socket) -> tuple[socket.socket, str]:
    """Accept one HTTP request and return its connection and JSON-RPC id."""

    connection, _address = listener.accept()
    request = b""
    while b"\r\n\r\n" not in request:
        request += connection.recv(4096)
    headers, body = request.split(b"\r\n\r\n", 1)
    length_line = next(
        line
        for line in headers.split(b"\r\n")
        if line.lower().startswith(b"content-length:")
    )
    content_length = int(length_line.split(b":", 1)[1])
    while len(body) < content_length:
        body += connection.recv(4096)
    return connection, json.loads(body[:content_length])["rpcId"]


def test_close_racing_response_read_maps_to_dsh_api_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``close()`` landing between ``request()`` and ``getresponse()``.

    The deterministic sync point is the real ``getresponse``: the request
    thread parks there, ``close()`` resets the connection state to Idle, and
    the real ``getresponse()`` then raises ``ResponseNotReady``.  Before the
    fix that exception escaped ``_call_sync`` instead of a ``DshApiError``.
    """

    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    about_to_read = threading.Event()
    allow_read = threading.Event()
    real_getresponse = http.client.HTTPConnection.getresponse

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)

    def serve_one() -> None:
        connection, rpc_id = _accept_and_read_request(listener)
        try:
            started.set()
            release.wait(5)
            response_body = json.dumps(
                {"rpcId": rpc_id, "result": {"ok": True, "value": {"events": []}}}
            ).encode()
            response = (
                b"HTTP/1.1 200 OK\r\nContent-Length: "
                + str(len(response_body)).encode()
                + b"\r\nConnection: close\r\n\r\n"
                + response_body
            )
            try:
                connection.sendall(response)
            except (BrokenPipeError, ConnectionResetError):
                pass
        finally:
            connection.close()
            finished.set()

    def gated_getresponse(connection: http.client.HTTPConnection):
        about_to_read.set()
        assert allow_read.wait(5)
        return real_getresponse(connection)

    monkeypatch.setattr(http.client.HTTPConnection, "getresponse", gated_getresponse)
    server_thread = threading.Thread(target=serve_one)
    server_thread.start()
    api = DshHttpApi(
        f"http://127.0.0.1:{listener.getsockname()[1]}", timeout_seconds=30
    )
    errors: list[BaseException] = []

    def request() -> None:
        try:
            api._call_sync("session.history", {"sessionId": "s"})
        except BaseException as error:  # noqa: BLE001 - asserted below
            errors.append(error)

    request_thread = threading.Thread(target=request)
    try:
        request_thread.start()
        assert about_to_read.wait(2)
        assert started.wait(2)
        api.close()
        allow_read.set()
        request_thread.join(timeout=1)
        assert not request_thread.is_alive()
        assert [type(error).__name__ for error in errors] == ["DshApiError"]
        assert isinstance(errors[0].__cause__, http.client.ResponseNotReady)
        assert "aborted: HTTP transport was closed" in str(errors[0])
    finally:
        allow_read.set()
        release.set()
        assert finished.wait(2)
        listener.close()
        server_thread.join(timeout=1)
        assert not server_thread.is_alive()


def test_truncated_response_body_maps_to_dsh_api_error() -> None:
    """A genuine short body is still an ``HTTPException``, mapped and chained."""

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)

    def serve_one() -> None:
        connection, _rpc_id = _accept_and_read_request(listener)
        try:
            # Promise 1024 bytes, deliver two, close: real IncompleteRead.
            connection.sendall(
                b"HTTP/1.1 200 OK\r\nContent-Length: 1024\r\n"
                b"Connection: close\r\n\r\n{}"
            )
        finally:
            connection.close()

    server_thread = threading.Thread(target=serve_one)
    server_thread.start()
    api = DshHttpApi(
        f"http://127.0.0.1:{listener.getsockname()[1]}", timeout_seconds=30
    )
    try:
        with pytest.raises(DshApiError) as caught:
            api._call_sync("session.history", {"sessionId": "s"})
        assert isinstance(caught.value.__cause__, http.client.IncompleteRead)
        assert "failed:" in str(caught.value)
    finally:
        listener.close()
        server_thread.join(timeout=2)
        assert not server_thread.is_alive()


@pytest.mark.parametrize(
    "error",
    [
        http.client.CannotSendRequest("Request-sent"),
        http.client.ResponseNotReady("Idle"),
        http.client.BadStatusLine(""),
        http.client.IncompleteRead(b"", 10),
        http.client.RemoteDisconnected(
            "Remote end closed connection without response"
        ),
    ],
    ids=[
        "CannotSendRequest",
        "ResponseNotReady",
        "BadStatusLine",
        "IncompleteRead",
        "RemoteDisconnected",
    ],
)
def test_http_client_state_errors_map_to_dsh_api_error(
    monkeypatch: pytest.MonkeyPatch, error: http.client.HTTPException
) -> None:
    """The racing-close family all becomes one ``DshApiError``, cause intact."""

    def fail_request(*_args: object, **_kwargs: object) -> None:
        raise error

    monkeypatch.setattr(http.client.HTTPConnection, "request", fail_request)
    api = DshHttpApi("http://127.0.0.1:1", timeout_seconds=1)
    with pytest.raises(DshApiError) as caught:
        api._call_sync("session.history", {"sessionId": "s"})
    assert caught.value.__cause__ is error
    assert "failed:" in str(caught.value)
