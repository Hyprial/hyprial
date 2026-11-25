"""The protocol subclass in front of the host: exact limits, dispatch truth, deadlines, pipelining, shape."""

from __future__ import annotations

import asyncio
import collections
import inspect
import logging
from typing import Any

import pytest
import uvicorn
from uvicorn.server import ServerState

from hyprial.shell.impl.gui.host import __main__ as entry
from tests.shell.gui.host.relay.test_http_limits import exchange, run_server

PATH = "/probe"


class ProbeApp:
    """A raw ASGI app that records every dispatched request and answers after ``delay``."""

    def __init__(self, delay: float = 0.0) -> None:
        self.delay = delay
        self.calls: list[str] = []

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] == "lifespan":
            while True:
                message = await receive()
                if message["type"] == "lifespan.startup":
                    await send({"type": "lifespan.startup.complete"})
                else:
                    await send({"type": "lifespan.shutdown.complete"})
                    return
        self.calls.append(scope["path"])
        more = True
        while more:
            message = await receive()
            more = message["type"] == "http.request" and message.get("more_body", False)
        if self.delay:
            await asyncio.sleep(self.delay)
        await send({"type": "http.response.start", "status": 200, "headers": [(b"content-length", b"2")]})
        await send({"type": "http.response.body", "body": b"ok"})


def request(target: str = PATH, *headers: bytes, close: bool = True) -> bytes:
    lines = [f"GET {target} HTTP/1.1".encode(), b"Host: probe", *headers]
    if close:
        lines.append(b"Connection: close")
    return b"\r\n".join(lines) + b"\r\n\r\n"


def counted(target: str, headers: list[tuple[str, str]]) -> int:
    return len(target) + sum(len(name) + len(value) + 4 for name, value in headers)


def wire(target: str, headers: list[tuple[str, str]]) -> bytes:
    lines = [f"GET {target} HTTP/1.1".encode(), *(f"{name}: {value}".encode() for name, value in headers)]
    return b"\r\n".join(lines) + b"\r\n\r\n"


async def read_response(reader: asyncio.StreamReader) -> tuple[int, bytes]:
    head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 8)
    status = int(head.split(b" ", 2)[1])
    length = 0
    for line in head.split(b"\r\n")[1:]:
        if line.lower().startswith(b"content-length:"):
            length = int(line.split(b":", 1)[1])
    return status, await asyncio.wait_for(reader.readexactly(length), 8) if length else b""


def serve(scenario: Any, monkeypatch: pytest.MonkeyPatch, app: ProbeApp, **options: Any) -> Any:
    return run_server(scenario, monkeypatch, app=app, **options)


def test_the_limit_constants_and_the_server_config_are_pinned(monkeypatch) -> None:
    assert entry.READ_IDLE_SECONDS == 10
    assert entry.REQUEST_DEADLINE_SECONDS == 15
    assert entry.MAX_CONNECTIONS == 256
    assert entry.MAX_HEADER_BYTES == 16_384
    assert entry.MAX_HEADER_FIELDS == 100
    assert entry.KEEP_ALIVE_SECONDS == 5

    async def scenario(port: int, server: Any) -> None:
        assert server.config.limit_concurrency == 256
        assert server.config.timeout_keep_alive == 5
        assert server.config.http is entry.BoundedHttpProtocol

    serve(scenario, monkeypatch, ProbeApp())


def test_the_pinned_uvicorn_still_has_every_hook_the_subclass_overrides() -> None:
    entry._require_protocol_shape()
    config = uvicorn.Config(ProbeApp(), log_config=None)
    protocol = entry.BoundedHttpProtocol(config, ServerState(), {}, asyncio.new_event_loop())
    try:
        assert protocol.cycle is None
        assert isinstance(protocol.pipeline, collections.deque)
        for attribute in ("loop", "parser", "connections", "config"):
            assert hasattr(protocol, attribute), attribute
        assert hasattr(protocol, "transport")
    finally:
        protocol.loop.close()


def stub(parameters: tuple[str, ...]) -> Any:
    def hook(self: Any, *args: Any) -> None:
        return None

    names = ("self", *parameters)
    hook.__signature__ = inspect.Signature(  # type: ignore[attr-defined]
        [inspect.Parameter(name, inspect.Parameter.POSITIONAL_OR_KEYWORD) for name in names]
    )
    return hook


@pytest.mark.parametrize("missing", sorted(entry.PROTOCOL_HOOKS))
def test_a_renamed_or_removed_uvicorn_hook_fails_the_start_loudly(missing: str) -> None:
    namespace = {name: stub(parameters) for name, parameters in entry.PROTOCOL_HOOKS.items() if name != missing}
    stripped = type("Stripped", (), namespace)
    with pytest.raises(RuntimeError, match=missing):
        entry._require_protocol_shape(stripped)


def test_a_hook_whose_arguments_changed_fails_the_start_loudly() -> None:
    class Changed(entry.HttpToolsProtocol):
        def on_header(self, key: bytes) -> None:
            return None

    with pytest.raises(RuntimeError, match="on_header"):
        entry._require_protocol_shape(Changed)


@pytest.mark.parametrize("where", ["header", "target"])
def test_the_header_byte_limit_is_exact(monkeypatch, where: str) -> None:
    def build(total: int) -> bytes:
        base_headers = [("Host", "probe"), ("Connection", "close")]
        target = PATH
        padding = total - counted(target, base_headers) - len("X-Pad") - 4
        if where == "header":
            return wire(target, [*base_headers, ("X-Pad", "a" * padding)])
        target = PATH + "?" + "a" * (total - counted(PATH + "?", base_headers))
        return wire(target, base_headers)

    async def scenario(port: int, server: Any) -> tuple[bytes, bytes]:
        return (
            await exchange(build(entry.MAX_HEADER_BYTES), port=port),
            await exchange(build(entry.MAX_HEADER_BYTES + 1), port=port),
        )

    app = ProbeApp()
    exact, over = serve(scenario, monkeypatch, app)
    assert exact.startswith(b"HTTP/1.1 200")
    assert over.startswith(b"HTTP/1.1 431")
    assert app.calls == [PATH]


def test_the_header_field_limit_is_exact(monkeypatch) -> None:
    def build(fields: int) -> bytes:
        extra = [(f"X-F{n}", "1") for n in range(fields - 2)]
        return wire(PATH, [("Host", "probe"), ("Connection", "close"), *extra])

    async def scenario(port: int, server: Any) -> tuple[bytes, bytes]:
        return (
            await exchange(build(entry.MAX_HEADER_FIELDS), port=port),
            await exchange(build(entry.MAX_HEADER_FIELDS + 1), port=port),
        )

    app = ProbeApp()
    exact, over = serve(scenario, monkeypatch, app)
    assert exact.startswith(b"HTTP/1.1 200")
    assert over.startswith(b"HTTP/1.1 431")
    assert app.calls == [PATH]


def test_a_very_long_request_target_is_431(monkeypatch) -> None:
    async def scenario(port: int, server: Any) -> bytes:
        return await exchange(request(PATH + "?" + "a" * 20_000), port=port)

    app = ProbeApp()
    assert serve(scenario, monkeypatch, app).startswith(b"HTTP/1.1 431")
    assert app.calls == []


@pytest.mark.parametrize("kind", ["bytes", "fields", "target", "bytes-with-body", "fields-with-body", "target-with-body"])
def test_a_request_refused_with_431_never_reaches_the_app(monkeypatch, caplog, kind: str) -> None:
    with_body = kind.endswith("with-body")
    prefix = kind.split("-")[0]
    extra = {
        "bytes": [b"X-Big: " + b"a" * 20_000],
        "fields": [f"X-F{n}: 1".encode() for n in range(150)],
        "target": [],
    }[prefix]
    target = PATH + ("?" + "a" * 20_000 if prefix == "target" else "")
    raw = request(target, *extra, *([b"Content-Length: 2"] if with_body else []))
    if with_body:
        raw = raw.replace(b"GET ", b"POST ", 1) + b"{}"

    async def scenario(port: int, server: Any) -> bytes:
        got = await exchange(raw, port=port)
        await asyncio.sleep(0.3)
        return got

    app = ProbeApp()
    with caplog.at_level(logging.WARNING, logger="uvicorn.error"):
        reply = serve(scenario, monkeypatch, app)
    assert reply.startswith(b"HTTP/1.1 431")
    assert app.calls == []
    assert [r.getMessage() for r in caplog.records if r.name == "uvicorn.error"] == []


def test_a_complete_request_on_a_slow_app_is_not_cut_off_by_the_read_deadline(monkeypatch) -> None:
    app = ProbeApp(delay=1.4)
    observed: dict[str, Any] = {}

    async def scenario(port: int, server: Any) -> bytes:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(request(close=False))
        await writer.drain()
        await asyncio.sleep(0.2)
        (protocol,) = list(server.server_state.connections)
        observed["timers"] = (protocol._idle_timer, protocol._request_timer)
        status, _ = await read_response(reader)
        writer.close()
        return str(status).encode()

    assert serve(scenario, monkeypatch, app, read_idle=0.3, deadline=0.6) == b"200"
    assert observed["timers"] == (None, None)


def test_a_client_that_keeps_sending_within_the_interval_is_not_cut(monkeypatch) -> None:
    raw = request()

    async def scenario(port: int, server: Any) -> bytes:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        step = max(1, len(raw) // 6)
        for start in range(0, len(raw), step):
            writer.write(raw[start : start + step])
            await writer.drain()
            await asyncio.sleep(0.2)
        status, _ = await read_response(reader)
        writer.close()
        return str(status).encode()

    assert serve(scenario, monkeypatch, ProbeApp(), read_idle=0.45, deadline=6.0) == b"200"


def test_header_counters_reset_per_message_on_a_keep_alive_connection(monkeypatch) -> None:
    big = request(PATH, b"X-Big: " + b"a" * 9_000, close=False)

    async def scenario(port: int, server: Any) -> list[int]:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        statuses = []
        for _ in range(3):
            writer.write(big)
            await writer.drain()
            statuses.append((await read_response(reader))[0])
        writer.close()
        return statuses

    app = ProbeApp()
    assert serve(scenario, monkeypatch, app) == [200, 200, 200]
    assert app.calls == [PATH] * 3


@pytest.mark.parametrize("phase", ["headers", "body"])
def test_a_client_dripping_under_the_idle_interval_is_cut_at_the_request_deadline(monkeypatch, phase: str) -> None:
    head = b"POST /probe HTTP/1.1\r\nHost: probe\r\nContent-Length: 400\r\n\r\n"
    prefix, dripped = (b"", head[:-2] if phase == "headers" else b"x" * 400)
    if phase == "body":
        prefix = head

    async def scenario(port: int, server: Any) -> tuple[bool, float]:
        loop = asyncio.get_running_loop()
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        started = loop.time()
        writer.write(prefix)
        cut = False
        sent = 0
        while loop.time() - started < 4.0:
            writer.write(dripped[sent : sent + 1])
            sent += 1
            try:
                await writer.drain()
                data = await asyncio.wait_for(reader.read(1), 0.2)
            except asyncio.TimeoutError:
                continue
            except OSError:
                cut = True
                break
            if data == b"":
                cut = True
                break
        elapsed = loop.time() - started
        writer.close()
        return cut, elapsed

    app = ProbeApp()
    cut, elapsed = serve(scenario, monkeypatch, app, read_idle=1.0, deadline=1.2)
    assert cut and 1.0 <= elapsed < 3.0
    assert app.calls == ([] if phase == "headers" else ["/probe"])


def test_bytes_of_a_next_request_do_not_cost_a_dispatched_response(monkeypatch) -> None:
    app = ProbeApp(delay=1.2)

    async def scenario(port: int, server: Any) -> tuple[int, bytes]:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(request(close=False) + b"G")
        await writer.drain()
        reply = await read_response(reader)
        writer.close()
        return reply

    assert serve(scenario, monkeypatch, app, read_idle=0.3, deadline=0.5) == (200, b"ok")


def test_a_pipelined_over_limit_request_is_refused_after_the_dispatched_response(monkeypatch) -> None:
    app = ProbeApp(delay=0.6)
    follower = request(PATH, b"X-Big: " + b"a" * 20_000)

    async def scenario(port: int, server: Any) -> bytes:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(request(close=False) + follower)
        await writer.drain()
        raw = await asyncio.wait_for(reader.read(), 8)
        writer.close()
        return raw

    raw = serve(scenario, monkeypatch, app)
    assert raw.startswith(b"HTTP/1.1 200") and raw.count(b"HTTP/1.1 ") == 2
    assert raw.index(b"ok") < raw.index(b"HTTP/1.1 431")
    assert app.calls == [PATH]


def test_a_pipelined_request_that_was_not_dispatched_is_dropped_with_the_refusal(monkeypatch) -> None:
    app = ProbeApp(delay=0.5)
    follower = request(PATH, b"X-Big: " + b"a" * 20_000)

    async def scenario(port: int, server: Any) -> bytes:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(request("/first", close=False) + request("/second", close=False) + follower)
        await writer.drain()
        raw = await asyncio.wait_for(reader.read(), 8)
        await asyncio.sleep(0.8)
        writer.close()
        return raw

    raw = serve(scenario, monkeypatch, app)
    assert raw.startswith(b"HTTP/1.1 200") and raw.count(b"HTTP/1.1 200") == 1 and raw.endswith(b"connection: close\r\n\r\n") and b"HTTP/1.1 431" in raw
    assert app.calls == ["/first"]


def test_the_server_build_refuses_to_start_when_the_protocol_shape_check_fails(monkeypatch) -> None:
    def broken(*args: Any) -> None:
        raise RuntimeError("shape")

    monkeypatch.setattr(entry, "_require_protocol_shape", broken)
    with pytest.raises(RuntimeError, match="shape"):
        entry._make_server(ProbeApp(), None, lambda: None)
