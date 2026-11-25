from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable


async def serve_raw_http(
    responder: Callable[[asyncio.StreamReader, asyncio.StreamWriter], Awaitable[None]],
) -> tuple[asyncio.AbstractServer, str]:
    server = await asyncio.start_server(responder, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    return server, f"http://127.0.0.1:{port}"


async def read_request(reader: asyncio.StreamReader) -> None:
    await reader.readuntil(b"\r\n\r\n")


async def close_writer(writer: asyncio.StreamWriter) -> None:
    writer.close()
    await writer.wait_closed()


class ClosingResource:
    def __init__(self, *, wait: asyncio.Event | None = None) -> None:
        self.close_calls = 0
        self.wait = wait

    async def aclose(self) -> None:
        self.close_calls += 1
        if self.wait is not None:
            await self.wait.wait()


class ClosingSocket:
    def __init__(self, *, wait: asyncio.Event | None = None) -> None:
        self.close_calls: list[tuple[int | None, str | None]] = []
        self.wait = wait

    async def close(self, code: int | None = None, reason: str | None = None) -> None:
        self.close_calls.append((code, reason))
        if self.wait is not None:
            await self.wait.wait()
