"""Native httpx/websockets port of the official QQ bot wire.

Authority: tencent-connect/qqbot-nodejs @ ca55d9c395b582b7fcfad0ec27209c35dd04e0b3
  - src/protocol/api/token.ts      token endpoint, singleflight, background refresh
  - src/protocol/api/api-client.ts  Authorization: QQBot <access_token>
  - src/protocol/api/routes.ts      /v2/users|groups/{id}/messages|files|upload_*
  - src/protocol/api/messages.ts    passive body {content,msg_type,msg_seq,msg_id}
  - src/protocol/gateway/constants.ts  opcodes, intents, close codes
  - src/protocol/gateway/reconnect.ts  close-code action table
Selection note: the pinned SDK's /v2 upload_prepare/upload_part_finish wire
conflicts with older published REST documentation. This port follows the SDK
wire only; no branch-B documentation semantics are merged.
This module owns sockets and effects but no reconnect and no heartbeat loops.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import random
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any, Literal, TypeAlias
from urllib.parse import urlsplit

import httpx

from hyprial.daemon.impl.adapters.common.transport import (
    HttpAccepted,
    HttpEffectObservation,
    ResourceCloseOutcome,
    TransportResourceOwner,
    WebSocketLocalClose,
    execute_http_effect,
    open_websocket_once,
)

from .errors import QQProtocolError, QQTokenUnavailable

# ── Gateway constants (constants.ts) ─────────────────────────────────────────

OP_DISPATCH = 0
OP_HEARTBEAT = 1
OP_IDENTIFY = 2
OP_RESUME = 6
OP_RECONNECT = 7
OP_INVALID_SESSION = 9
OP_HELLO = 10
OP_HEARTBEAT_ACK = 11

INTENTS_GROUP_AND_C2C = 1 << 25
INTENTS_INTERACTION = 1 << 26
DEFAULT_INTENTS = INTENTS_GROUP_AND_C2C | INTENTS_INTERACTION

RECONNECT_DELAYS_MS = (1000, 2000, 5000, 10000, 30000, 60000)
MAX_RECONNECT_ATTEMPTS = 100
RATE_LIMIT_DELAY_MS = 60_000
MAX_MSG_SEQ = 65_535

CLOSE_AUTH_FAILED = 4004
CLOSE_RATE_LIMITED = 4008
CLOSE_INSUFFICIENT_INTENTS = 4914
CLOSE_DISALLOWED_INTENTS = 4915

FATAL_CLOSE_CODES = frozenset({CLOSE_INSUFFICIENT_INTENTS, CLOSE_DISALLOWED_INTENTS})
REFRESH_TOKEN_CLOSE_CODES = frozenset({4006, 4007, 4009}) | frozenset(range(4900, 4914))


@dataclass(frozen=True)
class QQCloseAction:
    reconnect: bool
    delay_ms: int | None
    clear_session: bool
    refresh: Literal["none", "token", "credentials"]
    fatal: bool
    reason: str


def close_action(code: int | None) -> QQCloseAction:
    """Official reconnect.ts handleClose table, ported exactly."""
    if code in FATAL_CLOSE_CODES:
        return QQCloseAction(False, None, False, "none", True, f"qq-fatal-{code}")
    if code == CLOSE_AUTH_FAILED:
        return QQCloseAction(True, None, False, "credentials", False, "qq-auth-failed")
    if code == CLOSE_RATE_LIMITED:
        return QQCloseAction(
            True, RATE_LIMIT_DELAY_MS, False, "none", False, "qq-rate-limited-close"
        )
    if code in REFRESH_TOKEN_CLOSE_CODES:
        return QQCloseAction(True, None, True, "token", False, f"qq-session-invalid-{code}")
    if code == 1000:
        return QQCloseAction(False, None, False, "none", False, "qq-clean-close")
    return QQCloseAction(True, None, False, "none", False, f"qq-close-{code}")


# ── Token manager (token.ts) ─────────────────────────────────────────────────

TOKEN_PATH = "/app/getAppAccessToken"
TOKEN_REFRESH_AHEAD_S = 300.0
TOKEN_MIN_REFRESH_S = 60.0
TOKEN_JITTER_S = 30.0
TOKEN_RETRY_S = 5.0


@dataclass(frozen=True)
class _CachedToken:
    value: str
    expires_at: float
    expires_in: float


class QQTokenManager:
    """Per-AppID token cache with singleflight fetch and forced background refresh."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        token_url: str,
        app_id: str,
        client_secret: Callable[[], str],
        now: Callable[[], float],
        sleep: Callable[[float], Awaitable[None]],
        jitter: Callable[[], float] | None = None,
        request_timeout: float = 10.0,
    ) -> None:
        self._client = client
        self._token_url = token_url
        self._app_id = app_id
        self._client_secret = client_secret
        self._now = now
        self._sleep = sleep
        self._jitter = jitter or (lambda: random.uniform(0.0, TOKEN_JITTER_S))
        self._request_timeout = request_timeout
        self._cached: _CachedToken | None = None
        self._pending: asyncio.Task[str] | None = None
        self._background: asyncio.Task[None] | None = None

    @property
    def background_running(self) -> bool:
        return self._background is not None and not self._background.done()

    async def access_token(self) -> str:
        cached = self._cached
        if cached is not None and self._now() < cached.expires_at:
            return cached.value
        try:
            return await self._singleflight_fetch()
        except QQProtocolError:
            raise QQTokenUnavailable("qq-token-expired-fetch-failed") from None

    def invalidate(self) -> None:
        self._cached = None

    async def _singleflight_fetch(self) -> str:
        if self._pending is None:
            self._pending = asyncio.create_task(
                self._fetch_and_cache(), name=f"qq-token-fetch-{self._app_id}"
            )
        pending = self._pending
        try:
            return await asyncio.shield(pending)
        finally:
            if self._pending is pending and pending.done():
                self._pending = None

    async def _fetch_and_cache(self) -> str:
        secret = self._client_secret()
        if not secret:
            raise QQProtocolError("qq-credentials-unavailable")
        try:
            response = await self._client.post(
                self._token_url,
                json={"appId": self._app_id, "clientSecret": secret},
                timeout=self._request_timeout,
            )
        except httpx.HTTPError:
            raise QQProtocolError("qq-token-fetch-failed") from None
        if response.status_code < 200 or response.status_code >= 300:
            raise QQProtocolError("qq-token-fetch-failed")
        try:
            payload = response.json()
            value = payload["access_token"]
            raw_expires = payload.get("expires_in", 7200)
            expires_in = raw_expires if isinstance(raw_expires, int) and raw_expires >= 1 else 7200
        except (KeyError, ValueError, TypeError):
            raise QQProtocolError("qq-token-response-malformed") from None
        if not isinstance(value, str) or not value:
            raise QQProtocolError("qq-token-response-malformed")
        self._cached = _CachedToken(value, self._now() + expires_in, float(expires_in))
        return value

    def start_background(self) -> None:
        if self._background is not None and not self._background.done():
            return
        self._background = asyncio.create_task(
            self._background_loop(), name=f"qq-token-refresh-{self._app_id}"
        )

    async def stop(self) -> None:
        task = self._background
        self._background = None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def _background_loop(self) -> None:
        fetch_now = self._cached is None
        while True:
            try:
                if fetch_now or self._cached is None:
                    await self._singleflight_fetch()
                fetch_now = False
                cached = self._cached
                if cached is None:
                    fetch_now = True
                    await self._sleep(TOKEN_RETRY_S)
                    continue
                delay = max(
                    cached.expires_in - TOKEN_REFRESH_AHEAD_S - self._jitter(),
                    TOKEN_MIN_REFRESH_S,
                )
                await self._sleep(delay)
                fetch_now = True
            except asyncio.CancelledError:
                raise
            except Exception:
                fetch_now = True
                await self._sleep(TOKEN_RETRY_S)


# ── REST wire (api-client.ts, routes.ts) ─────────────────────────────────────

ParseSuccess: TypeAlias = Callable[[httpx.Response], HttpAccepted]


def message_native_id(response: httpx.Response) -> HttpAccepted:
    payload = response.json()
    native_id = payload.get("id") if isinstance(payload, Mapping) else None
    if not isinstance(native_id, str) or not native_id:
        raise ValueError("native id missing")
    return HttpAccepted(native_id)


def message_path(scene: str, target_id: str) -> str:
    prefix = "/v2/users" if scene == "c2c" else "/v2/groups"
    return f"{prefix}/{target_id}/messages"


def upload_prepare_path(scene: str, target_id: str) -> str:
    prefix = "/v2/users" if scene == "c2c" else "/v2/groups"
    return f"{prefix}/{target_id}/upload_prepare"


def upload_part_finish_path(scene: str, target_id: str) -> str:
    prefix = "/v2/users" if scene == "c2c" else "/v2/groups"
    return f"{prefix}/{target_id}/upload_part_finish"


def upload_complete_path(scene: str, target_id: str) -> str:
    prefix = "/v2/users" if scene == "c2c" else "/v2/groups"
    return f"{prefix}/{target_id}/files"


@dataclass(frozen=True)
class QQRestResult:
    observation: HttpEffectObservation
    body: Mapping[str, object] | None


class _EffectBodyStream(httpx.AsyncByteStream):
    """Move effect admission to the first body chunk handed to HTTPX."""

    def __init__(
        self,
        stream: httpx.AsyncByteStream,
        mark_may_have_sent: Callable[[], None],
    ) -> None:
        self._stream = stream
        self._mark_may_have_sent = mark_may_have_sent
        self._marked = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        async for chunk in self._stream:
            if chunk and not self._marked:
                self._mark_may_have_sent()
                self._marked = True
            yield chunk

    async def aclose(self) -> None:
        await self._stream.aclose()


def _private_host(host: str) -> bool:
    lowered = host.lower()
    if lowered in ("localhost",) or lowered.endswith(".localhost") or lowered.endswith(".local"):
        return True
    try:
        address = ipaddress.ip_address(lowered)
    except ValueError:
        return False
    return not address.is_global


class QQRestWire:
    """One authenticated REST wire over the single shared client."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        api_base: str,
        token: QQTokenManager,
        allowed_presigned_hosts: frozenset[str] | None = None,
        default_timeout: float = 30.0,
        upload_timeout: float = 120.0,
    ) -> None:
        self._client = client
        self._api_base = api_base.rstrip("/")
        self._token = token
        self._allowed_presigned_hosts = allowed_presigned_hosts
        self._default_timeout = default_timeout
        self._upload_timeout = upload_timeout

    async def _authorization(self) -> str:
        return f"QQBot {await self._token.access_token()}"

    def _presigned_target(self, url: str) -> str:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").lower()
        if parsed.username or parsed.password or not host:
            raise QQProtocolError("qq-media-presigned-url-denied")
        allowed = self._allowed_presigned_hosts
        if allowed is not None:
            if host not in allowed or parsed.scheme not in ("http", "https"):
                raise QQProtocolError("qq-media-presigned-url-denied")
            return url
        if parsed.scheme != "https" or _private_host(host):
            raise QQProtocolError("qq-media-presigned-url-denied")
        return url

    async def _execute(
        self,
        build: Callable[[httpx.Timeout], httpx.Request],
        *,
        effecting: bool,
        parse_success: ParseSuccess,
        timeout: float,
    ) -> QQRestResult:
        holder: dict[str, httpx.Response] = {}

        async def operation(attempt: Any) -> httpx.Response:
            request = build(httpx.Timeout(timeout))
            if effecting:
                request.stream = _EffectBodyStream(
                    request.stream, attempt.mark_may_have_sent
                )
            else:
                attempt.mark_may_have_sent()
            response = await self._client.send(request)
            holder["response"] = response
            attempt.mark_response_received()
            return response

        observation = await execute_http_effect(
            operation, effecting=effecting, parse_success=parse_success
        )
        response = holder.get("response")
        body: Mapping[str, object] | None = None
        if response is not None:
            try:
                parsed = response.json()
                body = parsed if isinstance(parsed, Mapping) else None
            except (ValueError, TypeError):
                body = None
        return QQRestResult(observation, body)

    async def post_json(
        self,
        path: str,
        body: Mapping[str, object],
        *,
        effecting: bool,
        parse_success: ParseSuccess | None = None,
        upload: bool = False,
        timeout: float | None = None,
    ) -> QQRestResult:
        authorization = await self._authorization()
        effective_timeout = timeout or (self._upload_timeout if upload else self._default_timeout)

        def build(request_timeout: httpx.Timeout) -> httpx.Request:
            return self._client.build_request(
                "POST",
                f"{self._api_base}{path}",
                json=dict(body),
                timeout=request_timeout,
                headers={
                    "Authorization": authorization,
                    "Content-Type": "application/json",
                    "User-Agent": "hyprial-qq-wire/1",
                },
            )

        return await self._execute(
            build,
            effecting=effecting,
            parse_success=parse_success or message_native_id,
            timeout=effective_timeout,
        )

    async def get_json(
        self, path: str, *, timeout: float | None = None
    ) -> QQRestResult:
        authorization = await self._authorization()

        def build(request_timeout: httpx.Timeout) -> httpx.Request:
            return self._client.build_request(
                "GET",
                f"{self._api_base}{path}",
                timeout=request_timeout,
                headers={
                    "Authorization": authorization,
                    "User-Agent": "hyprial-qq-wire/1",
                },
            )

        return await self._execute(
            build,
            effecting=False,
            parse_success=lambda _response: HttpAccepted(None),
            timeout=timeout or self._default_timeout,
        )

    async def put_bytes(
        self, url: str, data: bytes, *, timeout: float | None = None
    ) -> QQRestResult:
        target = self._presigned_target(url)

        def build(request_timeout: httpx.Timeout) -> httpx.Request:
            return self._client.build_request(
                "PUT",
                target,
                content=data,
                timeout=request_timeout,
                headers={"User-Agent": "hyprial-qq-wire/1"},
            )

        return await self._execute(
            build,
            effecting=True,
            parse_success=lambda _response: HttpAccepted(None),
            timeout=timeout or self._upload_timeout,
        )


# ── Gateway socket (one generation at a time) ────────────────────────────────


class QQGatewaySocket:
    """Single owned WebSocket generation; reconnect/heartbeat live in the runtime."""

    def __init__(
        self,
        owner: TransportResourceOwner,
        *,
        open_timeout: float = 15.0,
        close_timeout: float = 10.0,
        max_frame_bytes: int = 1_048_576,
    ) -> None:
        self._owner = owner
        self._open_timeout = open_timeout
        self._close_timeout = close_timeout
        self._max_frame_bytes = max_frame_bytes
        self._generation = 0
        self._socket: Any = None

    @property
    def generation(self) -> int:
        return self._generation

    async def connect(self, url: str) -> None:
        self._generation += 1
        socket = await open_websocket_once(
            url,
            generation=self._generation,
            open_timeout=self._open_timeout,
            close_timeout=self._close_timeout,
            max_size=self._max_frame_bytes,
        )
        self._owner.bind_socket(socket, generation=self._generation, owns_socket=True)
        self._socket = socket

    def _require_socket(self) -> Any:
        if self._socket is None:
            raise QQProtocolError("qq-gateway-socket-closed")
        return self._socket

    async def send_json(self, payload: Mapping[str, object]) -> None:
        await self._require_socket().send(json.dumps(dict(payload)))

    async def recv_json(self) -> Mapping[str, object]:
        raw = await self._require_socket().recv()
        try:
            frame = json.loads(raw)
        except (TypeError, ValueError):
            raise QQProtocolError("qq-gateway-frame-invalid") from None
        if not isinstance(frame, Mapping) or isinstance(frame.get("op"), bool) or not isinstance(
            frame.get("op"), int
        ):
            raise QQProtocolError("qq-gateway-frame-invalid")
        return frame

    async def close_socket(
        self, *, code: int, reason: str, timeout_at: float
    ) -> ResourceCloseOutcome:
        local = WebSocketLocalClose(code, reason)
        self._socket = None
        return await self._owner.close_socket(
            generation=self._generation,
            code=local.code,
            reason=local.reason,
            timeout_at=timeout_at,
        )

    def forget_socket(self) -> None:
        self._socket = None


__all__ = [
    "CLOSE_AUTH_FAILED",
    "CLOSE_RATE_LIMITED",
    "DEFAULT_INTENTS",
    "FATAL_CLOSE_CODES",
    "MAX_MSG_SEQ",
    "MAX_RECONNECT_ATTEMPTS",
    "OP_DISPATCH",
    "OP_HEARTBEAT",
    "OP_HEARTBEAT_ACK",
    "OP_HELLO",
    "OP_IDENTIFY",
    "OP_INVALID_SESSION",
    "OP_RECONNECT",
    "OP_RESUME",
    "QQCloseAction",
    "QQGatewaySocket",
    "QQRestResult",
    "QQRestWire",
    "QQTokenManager",
    "RATE_LIMIT_DELAY_MS",
    "RECONNECT_DELAYS_MS",
    "REFRESH_TOKEN_CLOSE_CODES",
    "close_action",
    "message_native_id",
    "message_path",
    "upload_complete_path",
    "upload_part_finish_path",
    "upload_prepare_path",
]
