"""The HTTP stack in front of the host: header caps, the read deadline and forwarded-header distrust."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from hyprial.shell.impl.gui.host import __main__ as entry
from hyprial.shell.impl.gui.host.app import build_app
from tests.shell.gui.host.support import host_config


async def exchange(request: bytes, *, port: int, read_for: float = 5.0) -> bytes:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(request)
    await writer.drain()
    try:
        return await asyncio.wait_for(reader.read(65536), read_for)
    except (ConnectionError, asyncio.TimeoutError):
        return b""
    finally:
        writer.close()


def run_server(
    scenario: Any,
    monkeypatch: pytest.MonkeyPatch,
    *,
    read_idle: float = 10.0,
    deadline: float = 15.0,
    app: Any = None,
) -> Any:
    monkeypatch.setattr(entry, "READ_IDLE_SECONDS", read_idle)
    monkeypatch.setattr(entry, "REQUEST_DEADLINE_SECONDS", deadline)

    async def main() -> Any:
        listener = entry._listen("127.0.0.1", 0)
        port = listener.getsockname()[1]
        ready = asyncio.Event()
        loop = asyncio.get_running_loop()
        server = entry._make_server(app or build_app(host_config()), listener, lambda: loop.call_soon(ready.set))
        task = asyncio.create_task(server.serve(sockets=[listener]))
        await asyncio.wait_for(ready.wait(), 10)
        try:
            return await scenario(port, server)
        finally:
            server.should_exit = True
            await asyncio.wait_for(task, 10)
            listener.close()

    return asyncio.run(main())


def health(port: int, *extra: bytes) -> bytes:
    lines = [b"GET /__gui/healthz HTTP/1.1", f"Host: 127.0.0.1:{port}".encode(), *extra, b"Connection: close"]
    return b"\r\n".join(lines) + b"\r\n\r\n"


def test_a_normal_request_still_works_and_forwarded_headers_are_not_trusted(monkeypatch) -> None:
    async def scenario(port: int, server: Any) -> bytes:
        assert server.config.proxy_headers is False and server.config.server_header is False
        return await exchange(health(port, b"X-Forwarded-Proto: https", b"X-Forwarded-For: 10.1.1.1"), port=port)

    raw = run_server(scenario, monkeypatch)
    assert raw.startswith(b"HTTP/1.1 200") and b"server:" not in raw.lower()


def test_an_oversized_header_section_or_too_many_fields_is_refused_with_431(monkeypatch) -> None:
    async def scenario(port: int, server: Any) -> tuple[bytes, bytes, bytes]:
        big = await exchange(health(port, b"X-Big: " + b"a" * 20_000), port=port)
        many = await exchange(
            health(port, *(f"X-F{n}: 1".encode() for n in range(entry.MAX_HEADER_FIELDS + 5))), port=port
        )
        after = await exchange(health(port), port=port)
        return big, many, after

    big, many, after = run_server(scenario, monkeypatch)
    assert big.startswith(b"HTTP/1.1 431") and many.startswith(b"HTTP/1.1 431")
    assert after.startswith(b"HTTP/1.1 200")


def test_a_header_section_just_under_the_caps_is_served(monkeypatch) -> None:
    async def scenario(port: int, server: Any) -> bytes:
        return await exchange(health(port, b"X-Big: " + b"a" * 8_000), port=port)

    assert run_server(scenario, monkeypatch).startswith(b"HTTP/1.1 200")


PARTIALS = {
    "silent": lambda port: b"",
    "headers": lambda port: f"GET /__gui/healthz HTTP/1.1\r\nHost: 127.0.0.1:{port}".encode(),
    "body": lambda port: (
        f"POST /daemon/session HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nOrigin: http://127.0.0.1:{port}\r\n"
        "content-type: application/json\r\ncontent-length: 50\r\n\r\n{"
    ).encode(),
}


@pytest.mark.parametrize("kind", sorted(PARTIALS))
def test_a_connection_that_never_completes_its_request_is_closed(monkeypatch, kind: str) -> None:
    async def scenario(port: int, server: Any) -> tuple[bytes, float]:
        started = asyncio.get_running_loop().time()
        raw = await exchange(PARTIALS[kind](port), port=port, read_for=8.0)
        return raw, asyncio.get_running_loop().time() - started

    raw, elapsed = run_server(scenario, monkeypatch, read_idle=0.4)
    assert raw == b"" and elapsed < 4.0


def test_a_slow_but_complete_request_is_not_cut_off_while_the_app_works(monkeypatch) -> None:
    async def scenario(port: int, server: Any) -> bytes:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        request = health(port)
        writer.write(request[:20])
        await writer.drain()
        await asyncio.sleep(0.2)
        writer.write(request[20:])
        await writer.drain()
        raw = await asyncio.wait_for(reader.read(65536), 5)
        writer.close()
        return raw

    assert run_server(scenario, monkeypatch, read_idle=0.4).startswith(b"HTTP/1.1 200")
