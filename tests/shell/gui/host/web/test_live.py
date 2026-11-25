"""The live relay behind ``hyprial gui dev``: Vite assets and the HMR socket, nothing else."""

from __future__ import annotations

import asyncio
import http.client
import json
import subprocess
import sys
from http import HTTPStatus
from typing import Any

import httpx
import pytest
from websockets.asyncio.client import connect
from websockets.asyncio.server import serve
from websockets.exceptions import InvalidStatus

from hyprial.shell.impl.gui.host.app import build_app
from hyprial.shell.impl.gui.host.web.live import LiveOriginError, LiveUpstream, parse_live_origin
from tests.shell.gui.host.relay.test_http_limits import run_server
from tests.shell.gui.host.support import ORIGIN, Gateway, host_config, run_gateway
from tests.shell.gui.host.test_process import MODULE, child_env, ready_line, refused, stop

UPSTREAM = "http://127.0.0.1:5173"


class Direct(httpx.AsyncBaseTransport):
    """Hands the handler's response to the client unread, as a socket transport does (MockTransport reads it)."""

    def __init__(self, handler: Any) -> None:
        self.handler = handler

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        return self.handler(request)


class Chunks(httpx.AsyncByteStream):
    def __init__(self, *chunks: bytes) -> None:
        self.chunks = chunks

    async def __aiter__(self) -> Any:
        for chunk in self.chunks:
            yield chunk


def vite(handler: Any) -> LiveUpstream:
    return LiveUpstream(UPSTREAM, transport=Direct(handler))


def seen_nothing(request: httpx.Request) -> httpx.Response:
    raise AssertionError(f"the dev server must not be contacted: {request.url}")


def get(path: str, handler: Any, **options: Any) -> Any:
    async def scenario(gateway: Any) -> Any:
        return await gateway.call(path, **options)

    return run_gateway(scenario, live=vite(handler))


@pytest.mark.parametrize(
    "value",
    ["http://127.0.0.1:5173", "http://127.0.0.1:5173/", "http://[::1]:5173"],
)
def test_a_loopback_literal_origin_is_accepted(value: str) -> None:
    assert parse_live_origin(value) == value.rstrip("/")


@pytest.mark.parametrize(
    "value",
    [
        "http://localhost:5173",
        "http://0.0.0.0:5173",
        "http://192.168.1.2:5173",
        "https://127.0.0.1:5173",
        "http://127.0.0.1",
        "http://127.0.0.1:0",
        "http://user@127.0.0.1:5173",
        "http://127.0.0.1:5173/app",
        "http://127.0.0.1:5173/?x=1",
        "http://127.0.0.1:5173/#x",
        "http://127.0.0.1:70000",
        "127.0.0.1:5173",
        "",
    ],
)
def test_anything_but_a_loopback_literal_origin_is_refused(value: str) -> None:
    with pytest.raises(LiveOriginError):
        parse_live_origin(value)


def test_the_request_is_relayed_with_an_exact_header_allowlist_and_the_normalised_path() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, stream=Chunks(b"module"), headers={"content-type": "text/javascript"})

    reply = get(
        "/src/a/../main.tsx?t=17&import",
        handler,
        headers={
            "cookie": "hyprial_gui_session=secret",
            "authorization": "Bearer never",
            "x-hyprial-csrf": "never",
            "accept": "*/*",
            "if-none-match": 'W/"etag"',
            "user-agent": "browser",
        },
    )
    assert reply.status == 200 and reply.raw == "module"
    (request,) = captured
    assert str(request.url) == f"{UPSTREAM}/src/main.tsx?t=17&import"
    assert set(request.headers) <= {"host", "accept", "if-none-match", "user-agent", "accept-encoding", "connection"}
    assert request.headers["host"] == "127.0.0.1:5173"
    assert request.headers["accept"] == "*/*" and request.headers["if-none-match"] == 'W/"etag"'


def test_the_response_is_streamed_without_hop_by_hop_cookie_or_cors_headers() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            stream=Chunks(b"one-", b"two-", b"three"),
            headers={
                "content-type": "text/javascript",
                "etag": "abc",
                "set-cookie": "vite=1",
                "access-control-allow-origin": "*",
                "keep-alive": "timeout=5",
                "x-extra": "kept",
            },
        )

    reply = get("/src/main.tsx", handler)
    assert reply.raw == "one-two-three"
    assert reply.headers["etag"] == "abc" and reply.headers["x-extra"] == "kept"
    for dropped in ("set-cookie", "access-control-allow-origin", "keep-alive"):
        assert dropped not in reply.headers


def test_a_head_request_is_relayed_as_head_with_no_body() -> None:
    methods: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        methods.append(request.method)
        return httpx.Response(200, stream=Chunks(b""), headers={"content-length": "6", "etag": "x"})

    reply = get("/", handler, method="HEAD")
    assert methods == ["HEAD"] and reply.status == 200 and reply.raw == ""


def test_the_cookie_jar_is_closed_to_whatever_the_dev_server_sets() -> None:
    cookies: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        cookies.append(request.headers.get("cookie"))
        return httpx.Response(200, stream=Chunks(b"x"), headers={"set-cookie": "a=1; Path=/"})

    async def scenario(gateway: Any) -> None:
        await gateway.call("/one")
        await gateway.call("/two")

    run_gateway(scenario, live=vite(handler))
    assert cookies == [None, None]


def test_health_is_the_hosts_own_and_says_development_without_asking_the_dev_server() -> None:
    reply = get("/__gui/healthz", seen_nothing)
    assert reply.status == 200
    assert reply.json["mode"] == "development" and reply.json["dataMode"] == "hosted"
    assert reply.json["driver"] == "daemon" and "assets" not in reply.json


def test_daemon_paths_are_never_relayed() -> None:
    async def scenario(gateway: Any) -> list[tuple[int, str]]:
        outcomes = []
        for method, path in (
            ("POST", "/daemon/session"),
            ("GET", "/daemon/request"),
            ("GET", "/daemon/readyz"),
            ("POST", "/daemon/request/agent.list"),
            ("GET", "/daemon/anything"),
        ):
            reply = await gateway.call(path, method=method, body={} if method == "POST" else None)
            outcomes.append((reply.status, reply.json.get("code", "")))
        outcomes.append(((await gateway.raw(b"/daemon/../daemon/x"))[0], ""))
        return outcomes

    outcomes = run_gateway(scenario, live=vite(seen_nothing))
    assert all(status >= 400 for status, _ in outcomes)


def test_the_host_and_origin_rule_applies_before_anything_is_relayed() -> None:
    async def scenario(gateway: Any) -> list[tuple[int, str]]:
        denied = await gateway.call("/src/main.tsx", host="evil.example:4173")
        cross = await gateway.call("/src/main.tsx", headers={"sec-fetch-site": "cross-site"})
        posted = await gateway.call("/src/main.tsx", method="POST", body={})
        return [(r.status, r.json.get("code", "")) for r in (denied, cross, posted)]

    assert run_gateway(scenario, live=vite(seen_nothing)) == [
        (403, "HTTP_HOST_DENIED"),
        (403, "ORIGIN_REJECTED"),
        (405, "HTTP_METHOD_NOT_ALLOWED"),
    ]


def test_a_control_character_in_the_target_is_a_400_before_the_dev_server_is_contacted() -> None:
    async def scenario(gateway: Any) -> tuple[int, str]:
        status, _, body = await gateway.raw(b"/src/a\x01b.tsx")
        return status, body.decode()

    status, body = run_gateway(scenario, live=vite(seen_nothing))
    assert status == 400 and '"code":"INVALID_REQUEST"' in body


@pytest.mark.parametrize(
    "target",
    [b"*", b"*@example.com/", b"*@127.0.0.1:9/stolen", b"@example.com/x", b"example.com/x"],
    ids=["asterisk", "userinfo-foreign-host", "userinfo-other-port", "at-host", "no-leading-slash"],
)
def test_a_target_that_is_not_an_absolute_path_is_a_400_and_never_names_another_host(target: bytes) -> None:
    async def scenario(gateway: Any) -> tuple[int, str]:
        status, _, body = await gateway.raw(target)
        return status, body.decode()

    status, body = run_gateway(scenario, live=vite(seen_nothing))
    assert status == 400 and '"code":"INVALID_REQUEST"' in body


@pytest.mark.parametrize("path", ["/src/main.tsx", "//example.com/x", "/a@example.com/b", "/:9/x"])
def test_every_relayed_url_keeps_the_configured_origin(path: str) -> None:
    seen: list[httpx.URL] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url)
        return httpx.Response(200, stream=Chunks(b"ok"))

    get(path, handler)
    assert seen and all((url.scheme, url.host, url.port, url.userinfo) == ("http", "127.0.0.1", 5173, b"") for url in seen)


def test_headers_the_dev_server_names_in_connection_are_hop_by_hop_and_dropped() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            stream=Chunks(b"ok"),
            headers={"connection": "X-Secret-Hop, close", "x-secret-hop": "1", "proxy-connection": "keep-alive", "x-extra": "kept"},
        )

    reply = get("/src/main.tsx", handler)
    assert reply.headers["x-extra"] == "kept"
    for dropped in ("connection", "x-secret-hop", "proxy-connection"):
        assert dropped not in reply.headers


def test_an_unreachable_dev_server_is_a_host_envelope_that_names_no_address() -> None:
    def refused(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused 127.0.0.1:5173", request=request)

    reply = get("/", refused)
    assert reply.status == 502
    assert reply.json["code"] == "UPSTREAM_UNAVAILABLE" and reply.json["dispatch"] == "not-dispatched"
    assert reply.json["ok"] is False and "5173" not in reply.raw


def test_a_status_the_http_stack_cannot_write_is_an_invalid_response() -> None:
    reply = get("/", lambda request: httpx.Response(700, stream=Chunks(b"x")))
    assert reply.status == 502 and reply.json["code"] == "UPSTREAM_INVALID_RESPONSE"


class DevServer:
    """A real loopback dev server: plain HTTP for the proxy-environment case, WebSocket for HMR."""

    def __init__(self) -> None:
        self.connections: list[dict[str, Any]] = []
        self.server: Any = None
        self.port = 0

    async def __aenter__(self) -> "DevServer":
        def answer(connection: Any, request: Any) -> Any:
            if request.headers.get("Upgrade", "").lower() == "websocket":
                return None
            return connection.respond(HTTPStatus.OK, "from the dev server\n")

        async def hmr(connection: Any) -> None:
            self.connections.append(
                {
                    "path": connection.request.path,
                    "origin": connection.request.headers.get("Origin"),
                    "subprotocol": connection.subprotocol,
                    "host": connection.request.headers.get("Host"),
                }
            )
            async for message in connection:
                if message == "close-me":
                    await connection.close(4001, "done")
                    return
                await connection.send(message)

        self.server = await serve(hmr, "127.0.0.1", 0, subprotocols=["vite-hmr"], process_request=answer)
        self.port = self.server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *exc: Any) -> None:
        self.server.close()
        await self.server.wait_closed()

    @property
    def origin(self) -> str:
        return f"http://127.0.0.1:{self.port}"


def test_the_proxy_environment_never_redirects_the_relay(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy", "HTTPS_PROXY", "https_proxy"):
        monkeypatch.setenv(name, "http://127.0.0.1:9")
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)

    async def main() -> str:
        async with DevServer() as dev:
            app = build_app(host_config(), None, live=LiveUpstream(dev.origin))
            await app.startup()
            try:
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=ORIGIN) as client:
                    return (await Gateway(client, app).call("/")).raw
            finally:
                await app.shutdown()

    assert asyncio.run(main()) == "from the dev server\n"


class Socket:
    """The ASGI side of one WebSocket: what the app sent and what the browser would send."""

    def __init__(self, *, host: str = "127.0.0.1:4173", origin: str | None = ORIGIN, path: str = "/", query: bytes = b"") -> None:
        headers = [(b"host", host.encode())]
        if origin is not None:
            headers.append((b"origin", origin.encode()))
        self.scope = {
            "type": "websocket",
            "path": path,
            "raw_path": path.encode(),
            "query_string": query,
            "headers": headers,
            "subprotocols": ["vite-hmr"],
            "server": ("127.0.0.1", 4173),
        }
        self.incoming: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.outgoing: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.incoming.put_nowait({"type": "websocket.connect"})

    async def receive(self) -> dict[str, Any]:
        return await self.incoming.get()

    async def send(self, message: dict[str, Any]) -> None:
        await self.outgoing.put(message)

    async def frame(self) -> dict[str, Any]:
        return await asyncio.wait_for(self.outgoing.get(), 5)


async def run_socket(dev: DevServer, socket: Socket) -> asyncio.Task[None]:
    app = build_app(host_config(), None, live=LiveUpstream(dev.origin))
    await app.startup()
    task = asyncio.create_task(app(socket.scope, socket.receive, socket.send))
    task.add_done_callback(lambda _: asyncio.ensure_future(app.shutdown()))
    return task


@pytest.mark.parametrize(
    "options",
    [
        {"origin": None},
        {"origin": "http://127.0.0.1:9999"},
        {"origin": "http://evil.example:4173"},
        {"host": "evil.example:4173"},
        {"host": "127.0.0.1:9999"},
        {"path": "/daemon/session"},
        {"path": "/daemon/request/agent.list"},
        {"path": "/x/../daemon/session"},
        {"path": "*"},
        {"path": "*@example.com/ws"},
        {"path": "*@127.0.0.1:9/ws"},
    ],
    ids=[
        "no-origin", "wrong-port-origin", "foreign-origin", "foreign-host", "wrong-port-host", "daemon", "daemon-method",
        "daemon-dot-segment", "asterisk", "userinfo-foreign-host", "userinfo-other-port",
    ],
)
def test_a_refused_upgrade_is_closed_before_the_dev_server_is_contacted(options: dict[str, Any]) -> None:
    async def main() -> tuple[dict[str, Any], int]:
        async with DevServer() as dev:
            socket = Socket(**options)
            task = await run_socket(dev, socket)
            closing = await socket.frame()
            await task
            return closing, len(dev.connections)

    closing, contacted = asyncio.run(main())
    assert closing == {"type": "websocket.close", "code": 1008} and contacted == 0


def test_an_upgrade_without_a_live_upstream_is_closed() -> None:
    async def main() -> dict[str, Any]:
        socket = Socket()
        app = build_app(host_config(), None)
        await app.startup()
        task = asyncio.create_task(app(socket.scope, socket.receive, socket.send))
        closing = await socket.frame()
        await task
        await app.shutdown()
        return closing

    assert asyncio.run(main()) == {"type": "websocket.close", "code": 1008}


def test_frames_flow_both_ways_with_the_subprotocol_the_query_and_the_upstream_origin() -> None:
    async def main() -> tuple[Any, ...]:
        async with DevServer() as dev:
            socket = Socket(query=b"token=abc123")
            task = await run_socket(dev, socket)
            accepted = await socket.frame()
            await socket.incoming.put({"type": "websocket.receive", "text": "hello"})
            echoed_text = await socket.frame()
            await socket.incoming.put({"type": "websocket.receive", "bytes": b"\x00\x01"})
            echoed_bytes = await socket.frame()
            await socket.incoming.put({"type": "websocket.receive", "text": "close-me"})
            closing = await socket.frame()
            await task
            return accepted, echoed_text, echoed_bytes, closing, dev.connections, dev.origin

    accepted, echoed_text, echoed_bytes, closing, connections, origin = asyncio.run(main())
    assert accepted == {"type": "websocket.accept", "subprotocol": "vite-hmr"}
    assert echoed_text == {"type": "websocket.send", "text": "hello"}
    assert echoed_bytes == {"type": "websocket.send", "bytes": b"\x00\x01"}
    assert closing == {"type": "websocket.close", "code": 4001}
    assert connections == [
        {"path": "/?token=abc123", "origin": origin, "subprotocol": "vite-hmr", "host": origin.removeprefix("http://")}
    ]


def test_a_browser_disconnect_closes_the_upstream_socket() -> None:
    async def main() -> bool:
        async with DevServer() as dev:
            socket = Socket()
            task = await run_socket(dev, socket)
            await socket.frame()
            await socket.incoming.put({"type": "websocket.disconnect", "code": 1001})
            await asyncio.wait_for(task, 5)
            return task.done() and task.exception() is None

    assert asyncio.run(main())


def test_an_unreachable_dev_server_closes_the_upgrade_before_accept(monkeypatch: pytest.MonkeyPatch) -> None:
    async def main() -> dict[str, Any]:
        async with DevServer() as dev:
            dead = DevServer()
            dead.port = dev.port
        socket = Socket()
        app = build_app(host_config(), None, live=LiveUpstream(dead.origin))
        await app.startup()
        task = asyncio.create_task(app(socket.scope, socket.receive, socket.send))
        closing = await socket.frame()
        await task
        await app.shutdown()
        return closing

    assert asyncio.run(main()) == {"type": "websocket.close", "code": 1011}


def test_the_socket_survives_the_read_deadlines_that_bound_an_ordinary_request(monkeypatch: pytest.MonkeyPatch) -> None:
    async def main() -> tuple[str, str, int]:
        async with DevServer() as dev:
            app = build_app(host_config(), None, live=LiveUpstream(dev.origin))

            async def scenario(port: int, server: Any) -> tuple[str, str, int]:
                address = f"ws://127.0.0.1:{port}/?token=t"
                async with connect(address, origin=f"http://127.0.0.1:{port}", subprotocols=["vite-hmr"], proxy=None) as client:
                    await client.send("before")
                    first = await asyncio.wait_for(client.recv(), 5)
                    await asyncio.sleep(1.3)
                    await client.send("after")
                    second = await asyncio.wait_for(client.recv(), 5)
                with pytest.raises(InvalidStatus) as refused:
                    async with connect(address, proxy=None):
                        pass
                return str(first), str(second), refused.value.response.status_code

            return await _serve(scenario, monkeypatch, app)

    assert asyncio.run(main()) == ("before", "after", 403)


async def _serve(scenario: Any, monkeypatch: pytest.MonkeyPatch, app: Any) -> Any:
    from hyprial.shell.impl.gui.host import __main__ as entry

    monkeypatch.setattr(entry, "READ_IDLE_SECONDS", 0.4)
    monkeypatch.setattr(entry, "REQUEST_DEADLINE_SECONDS", 0.4)
    listener = entry._listen("127.0.0.1", 0)
    port = listener.getsockname()[1]
    ready = asyncio.Event()
    loop = asyncio.get_running_loop()
    server = entry._make_server(app, listener, lambda: loop.call_soon(ready.set), websockets=True)
    task = asyncio.create_task(server.serve(sockets=[listener]))
    await asyncio.wait_for(ready.wait(), 10)
    try:
        return await scenario(port, server)
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, 10)
        listener.close()


def test_run_server_still_serves_http_without_websockets(monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario(port: int, server: Any) -> str:
        assert server.config.ws == "none"
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"GET /__gui/healthz HTTP/1.1\r\nHost: 127.0.0.1:%d\r\nConnection: close\r\n\r\n" % port)
        await writer.drain()
        raw = await reader.read(65536)
        writer.close()
        return raw.decode()

    raw = run_server(scenario, monkeypatch)
    assert raw.startswith("HTTP/1.1 200") and json.loads(raw.split("\r\n\r\n", 1)[1])["mode"] == "production"


def test_the_proxy_environment_never_redirects_the_socket(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy", "HTTPS_PROXY", "https_proxy", "WS_PROXY", "ws_proxy"):
        monkeypatch.setenv(name, "http://127.0.0.1:9")
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)

    async def main() -> dict[str, Any]:
        async with DevServer() as dev:
            socket = Socket()
            task = await run_socket(dev, socket)
            accepted = await socket.frame()
            await socket.incoming.put({"type": "websocket.disconnect", "code": 1001})
            await asyncio.wait_for(task, 5)
            return accepted

    assert asyncio.run(main()) == {"type": "websocket.accept", "subprotocol": "vite-hmr"}


def test_the_child_in_live_mode_says_development_serves_no_assets_and_stops_cleanly() -> None:

    argv = [sys.executable, "-m", MODULE, "serve", "--port", "0", "--live-upstream", UPSTREAM]
    child = subprocess.Popen(
        argv, env=child_env(), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )
    try:
        payload = ready_line(child)
        connection = http.client.HTTPConnection("127.0.0.1", payload["port"], timeout=10)
        connection.request("GET", "/__gui/healthz")
        health = json.loads(connection.getresponse().read())
        connection.request("GET", "/src/main.tsx")
        unreachable = connection.getresponse()
        unreachable.read()
        connection.close()
    finally:
        code, out, _ = stop(child)
    assert payload["mode"] == "development" and payload["dataMode"] == "hosted" and payload["driver"] == "daemon"
    assert health["mode"] == "development" and "assets" not in health
    assert unreachable.status == 502
    assert code == 0 and out.strip().splitlines()[-1] == "HYPRIAL_GUI_STOPPED {}"


@pytest.mark.parametrize(
    "argv",
    [
        ["--live-upstream", "http://localhost:5173"],
        ["--live-upstream", "http://0.0.0.0:5173"],
        ["--live-upstream", "https://127.0.0.1:5173"],
        ["--live-upstream", "http://127.0.0.1:5173/app"],
    ],
)
def test_a_live_upstream_that_is_not_a_loopback_literal_is_refused_with_exit_two(argv: list[str]) -> None:

    child = subprocess.Popen(
        [sys.executable, "-m", MODULE, "serve", "--port", "0", *argv],
        env=child_env(),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    code, report = refused(child)
    assert code == 2 and report["ok"] is False and report["code"] == "INVALID_ARGUMENT"


@pytest.mark.parametrize("argv", [[], ["--root", "x", "--live-upstream", UPSTREAM]])
def test_exactly_one_of_root_and_live_upstream_is_required(argv: list[str]) -> None:

    done = subprocess.run(
        [sys.executable, "-m", MODULE, "serve", "--port", "0", *argv],
        env=child_env(),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert done.returncode == 2 and "HYPRIAL_GUI_READY" not in done.stdout
