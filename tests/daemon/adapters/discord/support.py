"""Real 127.0.0.1 HTTP and WebSocket fixtures for the Discord adapter tests."""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass
from typing import Awaitable, Callable

import websockets

from hyprial.daemon.impl.adapters.contracts import (
    AdapterInstanceRef,
    CustodyReceipt,
    frozen_json_digest_v1,
)
from hyprial.daemon.impl.adapters.discord import DiscordCredentials

REASONS = {
    200: "OK",
    201: "Created",
    204: "No Content",
    302: "Found",
    403: "Forbidden",
    404: "Not Found",
    429: "Too Many Requests",
    500: "Internal Server Error",
    503: "Service Unavailable",
}


class Ledger:
    def __init__(self, value: str | None = None) -> None:
        self.value = value

    async def load(self, instance_id: str, generation: int) -> str | None:
        return self.value

    async def commit(self, instance_id: str, generation: int, token: str) -> None:
        self.value = token


class Credentials:
    def __init__(self, token: str = "discord-bot-token") -> None:
        self.token = token

    async def resolve(self, credential_ref: str) -> DiscordCredentials:
        return DiscordCredentials(self.token, 513)


def instance_ref(
    application_id: str = "app-1", generation: int = 3
) -> AdapterInstanceRef:
    fingerprint = hashlib.sha256(
        f"discord\0discord-cloud\0{application_id}".encode()
    ).hexdigest()
    return AdapterInstanceRef(
        "discord-1", "discord", "gateway", "main", "org-1", fingerprint, generation
    )


def durable_value(
    *,
    sequence: int = 6,
    session_id: str | None = "sess-durable",
    resume_url: str | None = "wss://resume.gateway.discord.gg/?v=10&encoding=json",
) -> str:
    return json.dumps(
        {"resume_url": resume_url, "seq": sequence, "session_id": session_id, "v": 1},
        separators=(",", ":"),
        sort_keys=True,
    )


def custody_for(payload: object) -> CustodyReceipt:
    return CustodyReceipt("j-1", frozen_json_digest_v1(payload), 1)


@dataclass
class HttpExchange:
    method: str
    path: str
    headers: dict[str, str]
    body: bytes


HttpResponse = tuple[int, dict[str, str], bytes] | None
HttpHandler = Callable[[HttpExchange], "HttpResponse | Awaitable[HttpResponse]"]


class ScriptedHttpServer:
    """Raw loopback HTTP/1.1 server recording every exchange."""

    def __init__(self, handler: HttpHandler) -> None:
        self.exchanges: list[HttpExchange] = []
        self._handler = handler
        self._server: asyncio.AbstractServer | None = None
        self._tasks: set[asyncio.Task[None]] = set()

    async def start(self) -> str:
        self._server = await asyncio.start_server(self._serve, "127.0.0.1", 0)
        port = self._server.sockets[0].getsockname()[1]
        return f"http://127.0.0.1:{port}"

    @property
    def hits(self) -> int:
        return len(self.exchanges)

    async def _serve(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        task = asyncio.current_task()
        if task is not None:
            self._tasks.add(task)
        try:
            while True:
                request_line = await reader.readline()
                if not request_line:
                    break
                parts = request_line.decode().split()
                headers: dict[str, str] = {}
                while True:
                    line = await reader.readline()
                    if line in (b"\r\n", b"\n", b""):
                        break
                    name, _, value = line.decode("latin-1").partition(":")
                    headers[name.strip().lower()] = value.strip()
                length = int(headers.get("content-length", "0"))
                body = await reader.readexactly(length) if length else b""
                exchange = HttpExchange(parts[0], parts[1], headers, body)
                self.exchanges.append(exchange)
                response = self._handler(exchange)
                if asyncio.iscoroutine(response):
                    response = await response
                if response is None:
                    await asyncio.Event().wait()
                    continue
                status, extra, payload = response
                head = (
                    f"HTTP/1.1 {status} {REASONS.get(status, 'OK')}\r\n"
                    f"Content-Length: {len(payload)}\r\n"
                    "Connection: keep-alive\r\n"
                )
                for name, value in extra.items():
                    head += f"{name}: {value}\r\n"
                writer.write(head.encode() + b"\r\n" + payload)
                await writer.drain()
        except (
            asyncio.IncompleteReadError,
            ConnectionError,
            asyncio.CancelledError,
        ):
            pass
        finally:
            if task is not None:
                self._tasks.discard(task)
            try:
                writer.close()
            except (ConnectionError, RuntimeError):
                pass

    async def stop(self) -> None:
        assert self._server is not None
        self._server.close()
        for pending in tuple(self._tasks):
            pending.cancel()
        for pending in tuple(self._tasks):
            try:
                await pending
            except asyncio.CancelledError:
                pass
        await self._server.wait_closed()


def json_response(payload: object, status: int = 200) -> HttpResponse:
    return status, {}, json.dumps(payload).encode()


def raw_response(
    body: bytes, status: int = 200, headers: dict[str, str] | None = None
) -> HttpResponse:
    return status, headers or {}, body


def discord_api_handler(
    gateway_url: str,
    *,
    application_id: str = "app-1",
    bot_user_id: str = "bot-1",
    remaining_sessions: int = 5,
) -> HttpHandler:
    def handler(exchange: HttpExchange) -> HttpResponse:
        if exchange.path == "/oauth2/applications/@me":
            return json_response({"id": application_id, "name": "hyprial"})
        if exchange.path == "/users/@me":
            return json_response({"id": bot_user_id, "bot": True})
        if exchange.path == "/gateway/bot":
            return json_response(
                {
                    "url": gateway_url,
                    "shards": 1,
                    "session_start_limit": {
                        "total": 1000,
                        "remaining": remaining_sessions,
                        "reset_after": 60000,
                        "max_concurrency": 1,
                    },
                }
            )
        return json_response({"message": "not found"}, 404)

    return handler


class GatewayConnection:
    """One accepted loopback WebSocket connection with recorded traffic."""

    def __init__(self, socket: object) -> None:
        self.socket = socket
        self.received: list[dict] = []
        self.client_close: tuple[int, str] | None = None
        self.closed = asyncio.Event()
        self._reader = asyncio.create_task(self._drain())

    async def _drain(self) -> None:
        try:
            while True:
                raw = await self.socket.recv()  # type: ignore[attr-defined]
                frame = json.loads(raw if isinstance(raw, str) else raw.decode())
                if isinstance(frame, dict):
                    self.received.append(frame)
        except websockets.exceptions.ConnectionClosed as error:
            if error.rcvd is not None:
                self.client_close = (error.rcvd.code, error.rcvd.reason)
        finally:
            self.closed.set()

    async def send(self, frame: dict) -> None:
        await self.socket.send(json.dumps(frame))  # type: ignore[attr-defined]

    async def close(self, code: int = 1000, reason: str = "") -> None:
        await self.socket.close(code=code, reason=reason)  # type: ignore[attr-defined]

    def abort(self) -> None:
        self.socket.transport.abort()  # type: ignore[attr-defined]

    async def wait_for(self, predicate: Callable[[dict], bool], timeout: float) -> dict:
        async with asyncio.timeout(timeout):
            while True:
                for frame in self.received:
                    if predicate(frame):
                        return frame
                await asyncio.sleep(0.002)

    async def wait_close(self, timeout: float) -> tuple[int, str] | None:
        async with asyncio.timeout(timeout):
            await self.closed.wait()
        return self.client_close


class LoopbackGateway:
    """Real loopback WebSocket server tracking every accepted connection."""

    def __init__(self) -> None:
        self.connections: list[GatewayConnection] = []
        self._server: asyncio.AbstractServer | None = None

    async def start(self) -> str:
        self._server = await websockets.serve(
            self._serve, "127.0.0.1", 0, close_timeout=0.5, max_size=1 << 20
        )
        port = self._server.sockets[0].getsockname()[1]
        return f"ws://127.0.0.1:{port}/"

    async def _serve(self, socket: object) -> None:
        connection = GatewayConnection(socket)
        self.connections.append(connection)
        await connection.closed.wait()

    async def wait_connection(self, index: int, timeout: float) -> GatewayConnection:
        async with asyncio.timeout(timeout):
            while len(self.connections) <= index:
                await asyncio.sleep(0.002)
        return self.connections[index]

    @property
    def count(self) -> int:
        return len(self.connections)

    async def stop(self) -> None:
        assert self._server is not None
        self._server.close()
        await self._server.wait_closed()
        for connection in self.connections:
            connection._reader.cancel()
        for connection in self.connections:
            try:
                await connection._reader
            except asyncio.CancelledError:
                pass


class ManualClock:
    """Deterministic heartbeat clock: every sleep blocks until advanced."""

    def __init__(self) -> None:
        self._ticks: asyncio.Queue[float] = asyncio.Queue()
        self.slept: list[float] = []

    async def sleep(self, delay: float) -> None:
        self.slept.append(delay)
        await self._ticks.get()

    def advance(self) -> None:
        self._ticks.put_nowait(0.0)
