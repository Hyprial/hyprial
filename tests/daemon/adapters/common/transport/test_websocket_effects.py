from __future__ import annotations

import asyncio

import pytest
import websockets
from websockets.exceptions import ConnectionClosed

from hyprial.daemon.impl.adapters.common.transport import (
    WebSocketCloseOrigin,
    WebSocketLocalClose,
    WebSocketOpenError,
    observe_connection_closed,
    open_websocket_once,
)

from .support import close_writer


def test_real_loopback_opening_failures_are_one_shot_and_typed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")

    async def scenario() -> None:
        attempts = 0

        async def reject(_reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            nonlocal attempts
            attempts += 1
            writer.write(
                b"HTTP/1.1 503 Service Unavailable\r\nContent-Length: 0\r\n\r\n"
            )
            await writer.drain()
            await close_writer(writer)

        server = await asyncio.start_server(reject, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        try:
            with pytest.raises(WebSocketOpenError) as captured:
                await open_websocket_once(
                    f"ws://127.0.0.1:{port}",
                    generation=11,
                    open_timeout=0.2,
                    close_timeout=0.1,
                )
            observation = captured.value.observation
            assert attempts == 1
            assert observation.generation == 11
            assert observation.origin is WebSocketCloseOrigin.OPENING
            assert observation.clean is False
            assert observation.opening_error == "InvalidStatus"
        finally:
            server.close()
            await server.wait_closed()

        async def hang(_reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            await asyncio.sleep(1)
            await close_writer(writer)

        server = await asyncio.start_server(hang, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        try:
            with pytest.raises(WebSocketOpenError) as captured:
                await open_websocket_once(
                    f"ws://127.0.0.1:{port}",
                    generation=12,
                    open_timeout=0.02,
                    close_timeout=0.02,
                )
            assert captured.value.observation.opening_error == "TimeoutError"
        finally:
            server.close()
            await server.wait_closed()

    asyncio.run(scenario())


def test_real_loopback_connection_closed_preserves_direction_and_generation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")

    async def scenario() -> None:
        async def remote_closer(socket) -> None:
            await socket.close(code=4001, reason="rotate")

        async with websockets.serve(remote_closer, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            socket = await open_websocket_once(
                f"ws://127.0.0.1:{port}", generation=21, open_timeout=0.2, close_timeout=0.1
            )
            with pytest.raises(ConnectionClosed) as captured:
                await socket.recv()
            observed = observe_connection_closed(captured.value, generation=21)
            assert observed.generation == 21
            assert observed.origin is WebSocketCloseOrigin.REMOTE
            assert observed.rcvd_code == 4001
            assert observed.rcvd_reason == "rotate"
            assert observed.sent_code == 4001
            assert observed.rcvd_then_sent is True

        peer_closed = asyncio.Event()

        async def await_local(socket) -> None:
            await socket.wait_closed()
            peer_closed.set()

        async with websockets.serve(await_local, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            socket = await open_websocket_once(
                f"ws://127.0.0.1:{port}", generation=22, open_timeout=0.2, close_timeout=0.1
            )
            local = WebSocketLocalClose(1000, "reconnect")
            await socket.close(code=local.code, reason=local.reason)
            await peer_closed.wait()
            with pytest.raises(ConnectionClosed) as captured:
                await socket.recv()
            observed = observe_connection_closed(
                captured.value, generation=22, local_intent=local
            )
            assert observed.origin is WebSocketCloseOrigin.LOCAL
            assert observed.sent_code == 1000
            assert observed.local_code == 1000
            assert observed.local_reason == "reconnect"

    asyncio.run(scenario())
