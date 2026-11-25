"""Real 127.0.0.1 HTTP/WS harness, manual clocks, and QQ wire fixtures."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

from websockets.asyncio.server import serve

from hyprial.daemon.impl.adapters.common.transport import SealedNativeMediaAuthority
from hyprial.daemon.impl.adapters.contracts import AdapterInstanceRef
from hyprial.daemon.impl.adapters.qq.components import (
    QQLoopbackOverrides,
    QQWireComponents,
    build_default_components,
)
from hyprial.daemon.impl.adapters.qq.provider import (
    QQCredentials,
)

APP_ID = "app-1"
APP_SECRET = "s-1"
BOT_ID = "bot-1"


class ManualClock:
    """Monotonic clock the test advances by hand."""

    def __init__(self, start: float = 1000.0) -> None:
        self._now = start

    def __call__(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += seconds


class ManualSleep:
    """Sleep that records delays and wakes exactly one pending caller per advance()."""

    def __init__(self) -> None:
        self.delays: list[float] = []
        self._wake: asyncio.Queue[None] = asyncio.Queue()

    async def __call__(self, delay: float) -> None:
        if delay <= 0:
            await asyncio.sleep(0)
            return
        self.delays.append(delay)
        await self._wake.get()

    def advance(self) -> None:
        self._wake.put_nowait(None)


class WallClockMs:
    """Wall-clock milliseconds source with manual offset."""

    def __init__(self, start_ms: int = 1_000_000) -> None:
        self._now = start_ms

    def __call__(self) -> int:
        return self._now

    def advance_ms(self, milliseconds: int) -> None:
        self._now += milliseconds


@dataclass
class RecordedRequest:
    method: str
    target: str
    headers: dict[str, str]
    body: bytes

    @property
    def path(self) -> str:
        return self.target.split("?", 1)[0]

    def json(self) -> object:
        return json.loads(self.body)

    def header(self, name: str) -> str | None:
        return self.headers.get(name.lower())


Responder = Callable[[RecordedRequest], Awaitable[tuple[int, dict[str, str], bytes]]]


def json_response(status: int, body: object) -> tuple[int, dict[str, str], bytes]:
    return status, {"content-type": "application/json"}, json.dumps(body).encode()


async def hang_forever(_request: RecordedRequest) -> tuple[int, dict[str, str], bytes]:
    await asyncio.sleep(3600)
    raise AssertionError("unreachable")


class LocalHTTP:
    """Scriptable real HTTP server bound to 127.0.0.1."""

    def __init__(self) -> None:
        self.requests: list[RecordedRequest] = []
        self.responder: Responder = _not_configured
        self._server: asyncio.AbstractServer | None = None
        self._writers: set[object] = set()
        self.base_url = ""

    async def start(self) -> str:
        self._server = await asyncio.start_server(self._serve, "127.0.0.1", 0)
        port = self._server.sockets[0].getsockname()[1]
        self.base_url = f"http://127.0.0.1:{port}"
        return self.base_url

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            for writer in list(self._writers):
                try:
                    writer.close()  # type: ignore[attr-defined]
                except Exception:
                    pass
            await self._server.wait_closed()
            self._server = None

    async def _serve(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        self._writers.add(writer)
        try:
            while True:
                request_line = await reader.readline()
                if not request_line:
                    return
                parts = request_line.decode().split()
                if len(parts) < 3:
                    return
                method, target = parts[0], parts[1]
                headers: dict[str, str] = {}
                while True:
                    line = await reader.readline()
                    if line in (b"\r\n", b"\n", b""):
                        break
                    name, _, value = line.decode().partition(":")
                    headers[name.strip().lower()] = value.strip()
                length = int(headers.get("content-length", "0") or "0")
                body = await reader.readexactly(length) if length else b""
                recorded = RecordedRequest(method, target, headers, body)
                self.requests.append(recorded)
                status, response_headers, payload = await self.responder(recorded)
                reason = {200: "OK", 204: "No Content", 400: "Bad Request", 429: "Too Many Requests"}.get(status, "Status")
                head = f"HTTP/1.1 {status} {reason}\r\ncontent-length: {len(payload)}\r\nconnection: close\r\n"
                for key, value in response_headers.items():
                    head += f"{key}: {value}\r\n"
                writer.write(head.encode() + b"\r\n" + payload)
                await writer.drain()
                return
        except (ConnectionError, asyncio.IncompleteReadError):
            return
        finally:
            self._writers.discard(writer)
            writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionError, OSError):
                pass


async def _not_configured(_request: RecordedRequest) -> tuple[int, dict[str, str], bytes]:
    return json_response(500, {"error": "responder-not-configured"})


class GatewayConnection:
    def __init__(self, connection: object) -> None:
        self._connection = connection
        self.received: list[object] = []
        self.closed = asyncio.Event()

    async def send_json(self, payload: object) -> None:
        await self._connection.send(json.dumps(payload))  # type: ignore[attr-defined]

    async def recv_json(self) -> object | None:
        try:
            raw = await self._connection.recv()  # type: ignore[attr-defined]
        except Exception:
            self.closed.set()
            return None
        self.received.append(raw)
        return json.loads(raw)

    async def close(self, code: int = 1000, reason: str = "") -> None:
        await self._connection.close(code, reason)  # type: ignore[attr-defined]
        self.closed.set()


GatewayHandler = Callable[[GatewayConnection], Awaitable[None]]


class LocalGateway:
    """Scriptable real WebSocket server bound to 127.0.0.1."""

    def __init__(self) -> None:
        self.connections: list[GatewayConnection] = []
        self.handler: GatewayHandler = _ignore_gateway
        self.url = ""
        self._server = None

    async def start(self) -> str:
        self._server = await serve(self._serve, "127.0.0.1", 0)
        port = self._server.sockets[0].getsockname()[1]
        self.url = f"ws://127.0.0.1:{port}"
        return self.url

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    async def _serve(self, connection: object) -> None:
        wrapped = GatewayConnection(connection)
        self.connections.append(wrapped)
        try:
            await self.handler(wrapped)
        except Exception:
            pass


async def _ignore_gateway(connection: GatewayConnection) -> None:
    while await connection.recv_json() is not None:
        pass


def default_hello(sequence: int = 1, session: str = "qq-session") -> GatewayHandler:
    """Handshake handler: Hello -> await identify/resume -> READY."""

    async def handler(connection: GatewayConnection) -> None:
        await connection.send_json({"op": 10, "d": {"heartbeat_interval": 30000}})
        identify = await connection.recv_json()
        if identify is None:
            return
        await connection.send_json(
            {"op": 0, "s": sequence, "t": "READY", "d": {"session_id": session}}
        )
        while await connection.recv_json() is not None:
            pass

    return handler


def group_event(sequence: int, event_id: str, message_id: str) -> dict[str, object]:
    return {
        "op": 0,
        "s": sequence,
        "t": "GROUP_AT_MESSAGE_CREATE",
        "id": event_id,
        "d": {
            "id": message_id,
            "event_id": event_id,
            "group_openid": "group-1",
            "author": {"id": "user-1"},
            "content": f" <@!{BOT_ID}> hello {message_id} ",
        },
    }


def c2c_event(sequence: int, event_id: str, message_id: str) -> dict[str, object]:
    return {
        "op": 0,
        "s": sequence,
        "t": "C2C_MESSAGE_CREATE",
        "id": event_id,
        "d": {
            "id": message_id,
            "event_id": event_id,
            "author": {"user_openid": "user-openid-1"},
            "content": f"hello {message_id}",
        },
    }


def qq_instance(generation: int = 2) -> AdapterInstanceRef:
    fingerprint = hashlib.sha256(f"qq\0qq-official\0{APP_ID}\0{BOT_ID}".encode()).hexdigest()
    return AdapterInstanceRef(
        "qq-1", "qq", "official-bot", "main", "org-1", fingerprint, generation
    )


class StaticCredentials:
    def __init__(self, app_id: str = APP_ID, secret: str = APP_SECRET) -> None:
        self.app_id = app_id
        self.secret = secret
        self.calls = 0

    async def resolve(self, credential_ref: str) -> QQCredentials:
        assert credential_ref == "receiver:qq"
        self.calls += 1
        return QQCredentials(self.app_id, self.secret)


class MemoryLedger:
    def __init__(self, value: str | None = None) -> None:
        self.value = value

    async def load(self, instance_id: str, generation: int) -> str | None:
        return self.value

    async def commit(self, instance_id: str, generation: int, token: str) -> None:
        self.value = token


def sealed_authority(tmp_path: Path) -> SealedNativeMediaAuthority:
    return SealedNativeMediaAuthority(tmp_path / "media.sqlite", max_blob_bytes=1024 * 1024)


def build_components(
    *,
    http: LocalHTTP,
    gateway_url: str | None,
    store_path: Path,
    authority: SealedNativeMediaAuthority | None,
    clock: ManualClock | None = None,
    sleeper: ManualSleep | None = None,
    jitter: Callable[[], float] | None = None,
    now_ms: Callable[[], int] | None = None,
    sleep: Callable[[float], Awaitable[None]] | None = None,
    token_sleep: Callable[[float], Awaitable[None]] | None = None,
    secret: str = APP_SECRET,
    gateway_close_timeout: float = 10.0,
    rest_timeout: float | None = None,
) -> QQWireComponents:
    loopback = QQLoopbackOverrides(
        token_base_url=http.base_url,
        api_base_url=http.base_url,
        gateway_url=gateway_url,
    )
    extra: dict[str, object] = {}
    if rest_timeout is not None:
        extra["rest_timeout"] = rest_timeout
        extra["rest_upload_timeout"] = rest_timeout
    return build_default_components(
        app_id=APP_ID,
        client_secret=lambda: secret,
        store_path=store_path,
        media_authority=authority,
        loopback=loopback,
        now_ms=now_ms,
        sleep=sleep,
        token_clock=clock,
        token_sleep=token_sleep or sleeper,
        token_jitter=jitter,
        gateway_close_timeout=gateway_close_timeout,
        **extra,
    )


def text_operation(
    *,
    msg_id: str | None = "message-1",
    content: str = "hello",
    scene: str = "group",
    operation_id: str = "qq-op-1",
    **overrides: object,
) -> SimpleNamespace:
    request: dict[str, object] = {
        "provider": "qq",
        "organization_id": "org-1",
        "instance_id": "qq-1",
        "generation": 2,
        "receiver_ref": "receiver:qq",
        "action": "send_message",
        "scene": scene,
        "target_id": "group-1" if scene == "group" else "user-1",
        "content": content,
        "msg_id": msg_id,
        "part_index": 0,
    }
    request.update(overrides)
    return SimpleNamespace(
        node_id=operation_id,
        idempotency_key=operation_id,
        sealed_request=_freeze(request),
        template=_freeze(request),
    )


def media_operation(
    *,
    msg_id: str = "message-1",
    media_ref: dict[str, object] | None = None,
    scope: dict[str, object] | None = None,
    operation_id: str = "qq-media-op-1",
    **overrides: object,
) -> SimpleNamespace:
    request: dict[str, object] = {
        "provider": "qq",
        "organization_id": "org-1",
        "instance_id": "qq-1",
        "generation": 2,
        "receiver_ref": "receiver:qq",
        "action": "send_media",
        "scene": "group",
        "target_id": "group-1",
        "msg_id": msg_id,
        "media_ref": media_ref
        or {
            "instance_id": "qq-1",
            "generation": 2,
            "opaque_ref_digest": "0" * 64,
            "authority_handle_id": "native-media:v1:missing",
        },
        "media_scope": scope
        or {
            "owner_principal": "principal-1",
            "organization_id": "org-1",
            "provider": "qq",
            "instance_id": "qq-1",
            "generation": 2,
            "target_ref_id": "group-1",
            "content_digest": "1" * 64,
        },
        "part_index": 0,
    }
    request.update(overrides)
    return SimpleNamespace(
        node_id=operation_id,
        idempotency_key=operation_id,
        sealed_request=_freeze(request),
        template=_freeze(request),
    )


def _freeze(value: object) -> object:
    from hyprial.daemon.impl.adapters.contracts import freeze_json_v1

    return freeze_json_v1(value)


class FrameSecrets:
    """Fail when any recorded wire text contains the AppSecret."""

    def __init__(self) -> None:
        self.sources: list[tuple[str, list[object]]] = []

    def add(self, label: str, frames: list[object]) -> None:
        self.sources.append((label, frames))

    def check(self) -> None:
        for label, frames in self.sources:
            for frame in frames:
                assert APP_SECRET not in str(frame), f"AppSecret leaked via {label}"
