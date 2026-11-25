"""Concrete Discord REST/Gateway transport bundle.

One bundle owns exactly one ``httpx.AsyncClient`` (proxy-free, redirect-free)
and one WebSocket generation at a time through the accepted common owner.
Official endpoints are fixed; only the constructor accepts loopback overrides.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx
from websockets.exceptions import ConnectionClosed

from ..common.transport import (
    HttpAccepted,
    HttpEffectState,
    ResourceCloseOutcome,
    TransportResourceOwner,
    WebSocketCloseObservation,
    WebSocketLocalClose,
    execute_http_effect,
    observe_connection_closed,
    open_websocket_once,
)
from .components import DiscordRateLimits, default_now_ms, discord_nonce
from .errors import DiscordConnectionClosed, DiscordProtocolError

OFFICIAL_REST_BASE_URL = "https://discord.com/api/v10"
OFFICIAL_GATEWAY_HOSTS = ("gateway.discord.gg",)
OFFICIAL_CDN_HOSTS = ("cdn.discordapp.com",)


@dataclass(frozen=True, slots=True)
class DiscordEndpointPolicy:
    """Official fixed endpoints with constructor-only loopback overrides."""

    rest_base_url: str = OFFICIAL_REST_BASE_URL
    gateway_hosts: tuple[str, ...] = OFFICIAL_GATEWAY_HOSTS
    gateway_schemes: tuple[str, ...] = ("wss",)
    cdn_hosts: tuple[str, ...] = OFFICIAL_CDN_HOSTS
    cdn_schemes: tuple[str, ...] = ("https",)
    cdn_allow_private_addresses: bool = False


@dataclass(frozen=True, slots=True)
class DiscordRestOutcome:
    state: HttpEffectState
    status_code: int | None
    native_id: str | None
    reason_code: str
    retry_after_ms: int | None = None
    bucket: str | None = None
    global_limited: bool = False


def _require_text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise DiscordProtocolError("discord-invalid-request", f"{name} is required")
    return value


class _WebSocketHandle:
    """Adapt websockets close semantics for the common resource owner."""

    __slots__ = ("_socket",)

    def __init__(self, socket: Any) -> None:
        self._socket = socket

    async def recv(self) -> Any:
        return await self._socket.recv()

    async def send(self, payload: str) -> None:
        await self._socket.send(payload)

    async def close(self, code: int | None = None, reason: str | None = None) -> None:
        if code is None:
            await self._socket.close()
        else:
            await self._socket.close(code, reason or "")


class DiscordGatewayTransport:
    """Single-owner HTTP + WebSocket bundle implementing the runtime protocol."""

    def __init__(
        self,
        *,
        policy: DiscordEndpointPolicy | None = None,
        client: httpx.AsyncClient | None = None,
        owns_client: bool = True,
        request_timeout: float = 15.0,
        ws_open_timeout: float = 10.0,
        ws_close_timeout: float = 10.0,
        rate_limits: DiscordRateLimits | None = None,
        now_ms: Callable[[], int] | None = None,
    ) -> None:
        self._policy = policy if policy is not None else DiscordEndpointPolicy()
        if client is not None and not isinstance(client, httpx.AsyncClient):
            raise TypeError("client must be httpx.AsyncClient")
        self._client = client if client is not None else httpx.AsyncClient(
            trust_env=False,
            follow_redirects=False,
            timeout=httpx.Timeout(request_timeout),
        )
        self._owner = TransportResourceOwner(self._client, owns_client=owns_client)
        self._ws_open_timeout = ws_open_timeout
        self._ws_close_timeout = ws_close_timeout
        self._socket: Any = None
        self._socket_generation = 0
        self._intents: dict[int, WebSocketLocalClose] = {}
        self._finalizing = False
        self._bot_token: str | None = None
        self.rate_limits = rate_limits if rate_limits is not None else DiscordRateLimits()
        self._now_ms = now_ms or default_now_ms

    @property
    def policy(self) -> DiscordEndpointPolicy:
        return self._policy

    @property
    def http_client(self) -> httpx.AsyncClient:
        """Borrowed client handle; callers must never close it."""

        return self._client

    @property
    def socket_generation(self) -> int:
        return self._socket_generation

    def now_ms(self) -> int:
        """Return the bundle clock used by shared rate-limit consumers."""

        return self._now_ms()

    def validate_gateway_url(self, url: str) -> str:
        parsed_url = urlsplit(url)
        if parsed_url.scheme not in self._policy.gateway_schemes:
            raise DiscordProtocolError("discord-gateway-scheme", "gateway scheme not allowed")
        host = parsed_url.hostname or ""
        if not host or parsed_url.username or parsed_url.password:
            raise DiscordProtocolError("discord-gateway-authority", "gateway authority not allowed")
        if not any(host == entry or host.endswith(f".{entry}") for entry in self._policy.gateway_hosts):
            raise DiscordProtocolError("discord-gateway-host", "gateway host not allowlisted")
        if parsed_url.fragment:
            raise DiscordProtocolError("discord-gateway-fragment", "gateway fragment not allowed")
        return urlunsplit(
            (
                parsed_url.scheme,
                parsed_url.netloc,
                parsed_url.path or "/",
                "v=10&encoding=json",
                "",
            )
        )

    async def authenticate(self, credentials: object) -> dict[str, object]:
        self._bot_token = getattr(credentials, "bot_token", None)
        application = await self._get_json("/oauth2/applications/@me", credentials)
        application_id = _require_text(application.get("id"), "application id")
        user = await self._get_json("/users/@me", credentials)
        bot_user_id = _require_text(user.get("id"), "bot user id")
        return {"id": application_id, "bot_user_id": bot_user_id}

    async def gateway(self, credentials: object) -> dict[str, object]:
        self._bot_token = getattr(credentials, "bot_token", None)
        payload = await self._get_json("/gateway/bot", credentials)
        url = _require_text(payload.get("url"), "gateway URL")
        limit = payload.get("session_start_limit")
        if not isinstance(limit, Mapping):
            raise DiscordProtocolError("discord-gateway-limit", "session_start_limit missing")
        return {"url": url, "session_start_limit": dict(limit)}

    async def connect(self, url: str, *, max_frame_bytes: int) -> None:
        if self._finalizing:
            raise RuntimeError("discord transport is finalizing and cannot reconnect")
        normalized = self.validate_gateway_url(url)
        self._socket_generation += 1
        generation = self._socket_generation
        socket = await open_websocket_once(
            normalized,
            generation=generation,
            open_timeout=self._ws_open_timeout,
            close_timeout=self._ws_close_timeout,
            max_size=max_frame_bytes,
        )
        handle = _WebSocketHandle(socket)
        self._owner.bind_socket(handle, generation=generation, owns_socket=True)
        self._socket = handle

    async def receive(self, *, max_frame_bytes: int) -> Mapping[str, object]:
        socket, generation = self._socket, self._socket_generation
        if socket is None:
            raise DiscordProtocolError("discord-socket-closed", "no gateway socket is bound")
        try:
            raw = await socket.recv()
        except ConnectionClosed as error:
            observation = self._observe(error, generation)
            raise DiscordConnectionClosed(observation) from error
        frame = json.loads(raw if isinstance(raw, str) else raw.decode())
        if not isinstance(frame, Mapping):
            raise DiscordProtocolError("discord-frame-invalid", "gateway frame must be an object")
        return dict(frame)

    async def send(self, payload: object) -> None:
        socket, generation = self._socket, self._socket_generation
        if socket is None:
            raise DiscordProtocolError("discord-socket-closed", "no gateway socket is bound")
        try:
            await socket.send(json.dumps(dict(payload)) if isinstance(payload, Mapping) else json.dumps(payload))
        except ConnectionClosed as error:
            observation = self._observe(error, generation)
            raise DiscordConnectionClosed(observation) from error

    async def close_socket(self, code: int = 4000) -> ResourceCloseOutcome | None:
        """Reconnect-style close (4000); the socket generation is fenced."""

        if isinstance(code, bool) or not isinstance(code, int) or not 1000 <= code <= 4999:
            raise ValueError("close code must be within the WebSocket wire range")
        generation = self._socket_generation
        if self._socket is None:
            return None
        reason = "discord-reconnect" if code != 1000 else "discord-close"
        self._intents[generation] = WebSocketLocalClose(code, reason)
        deadline = asyncio.get_running_loop().time() + self._ws_close_timeout
        outcome = await self._owner.close_socket(
            generation=generation, code=code, reason=reason, timeout_at=deadline
        )
        self._forget_socket(generation)
        return outcome

    async def close(self, code: int) -> ResourceCloseOutcome | None:
        """Runtime protocol compatibility; reconnect ownership stays socket-only."""

        return await self.close_socket(code)

    async def aclose(self, deadline_at: float) -> ResourceCloseOutcome:
        """Deadline-aware idempotent final close: socket 1000, then owned client."""

        self._finalizing = True
        generation = self._socket_generation
        if self._socket is not None:
            self._intents[generation] = WebSocketLocalClose(1000, "discord-final-close")
        self._socket = None
        return await self._owner.aclose(deadline_at)

    async def request(
        self, action: str, payload: Mapping[str, object], idempotency_key: str
    ) -> DiscordRestOutcome:
        if action != "create_message":
            raise DiscordProtocolError("discord-action-unsupported", f"unsupported action {action}")
        channel = _require_text(payload.get("channel_id"), "channel id")
        body = self._message_body(payload, idempotency_key)
        return await self._post_message(channel, body=body)

    async def post_multipart_message(
        self,
        channel_id: str,
        payload: Mapping[str, object],
        *,
        filename: str,
        content_type: str | None,
        body: bytes,
        idempotency_key: str,
    ) -> DiscordRestOutcome:
        payload_json = dict(self._message_body(dict(payload), idempotency_key))
        payload_json.pop("content", None)
        parts = {
            "payload_json": (
                None,
                json.dumps(payload_json, separators=(",", ":")),
                "application/json",
            ),
            "files[0]": (filename, body, content_type or "application/octet-stream"),
        }
        return await self._post_message(channel_id, body=None, files=parts)

    async def reconcile(
        self, action: str, payload: Mapping[str, object], idempotency_key: str
    ) -> Mapping[str, object] | None:
        if action != "create_message":
            raise DiscordProtocolError("discord-action-unsupported", f"unsupported action {action}")
        channel = _require_text(payload.get("channel_id"), "channel id")
        nonce = discord_nonce(idempotency_key)
        url = f"{self._policy.rest_base_url}/channels/{channel}/messages"
        try:
            response = await self._client.get(
                url, params={"limit": "100"}, headers=self._auth_headers()
            )
        except httpx.HTTPError:
            return None
        if response.status_code != 200:
            return None
        try:
            payload_body = response.json()
        except ValueError:
            return None
        if not isinstance(payload_body, list):
            return None
        for message in payload_body:
            if not isinstance(message, Mapping) or message.get("nonce") != nonce:
                continue
            native_id = message.get("id")
            if isinstance(native_id, str) and native_id:
                return dict(message)
        return None

    def _auth_headers(self) -> dict[str, str]:
        if not self._bot_token:
            raise DiscordProtocolError("discord-rest-unauthenticated", "bot token unavailable")
        return {"Authorization": f"Bot {self._bot_token}"}

    async def _get_json(self, path: str, credentials: object) -> Mapping[str, object]:
        url = f"{self._policy.rest_base_url}{path}"
        try:
            response = await self._client.get(url, headers=self._auth_headers())
        except httpx.HTTPError as error:
            raise DiscordProtocolError("discord-rest-transport") from error
        if response.status_code != 200:
            raise DiscordProtocolError(f"discord-rest-{response.status_code}")
        try:
            payload = response.json()
        except ValueError as error:
            raise DiscordProtocolError("discord-rest-invalid-json") from error
        if not isinstance(payload, Mapping):
            raise DiscordProtocolError("discord-rest-invalid-body")
        return payload

    @staticmethod
    def _message_body(payload: Mapping[str, object], idempotency_key: str) -> dict[str, object]:
        body: dict[str, object] = {
            "content": str(payload.get("content") or ""),
            "nonce": discord_nonce(idempotency_key),
        }
        reference = payload.get("message_reference")
        if isinstance(reference, str) and reference:
            body["message_reference"] = {"message_id": reference}
        elif isinstance(reference, Mapping) and reference:
            body["message_reference"] = dict(reference)
        return body

    async def _post_message(
        self,
        channel_id: str,
        *,
        body: Mapping[str, object] | None,
        files: dict[str, object] | None = None,
    ) -> DiscordRestOutcome:
        url = f"{self._policy.rest_base_url}/channels/{channel_id}/messages"
        captured: dict[str, httpx.Response] = {}

        async def operation(attempt: object) -> httpx.Response:
            request = self._client.build_request(
                "POST",
                url,
                params={"enforce_nonce": "true"},
                json=dict(body) if files is None else None,
                files=files,
                headers=self._auth_headers(),
            )
            attempt.mark_may_have_sent()  # type: ignore[attr-defined]
            response = await self._client.send(request)
            captured["response"] = response
            return response

        def parse_success(response: httpx.Response) -> HttpAccepted:
            payload = response.json()
            if not isinstance(payload, Mapping):
                raise ValueError("message body must be an object")
            native_id = payload.get("id")
            if not isinstance(native_id, str) or not native_id:
                raise ValueError("Discord message id is required")
            return HttpAccepted(native_id)

        observation = await execute_http_effect(
            operation, effecting=True, parse_success=parse_success  # type: ignore[arg-type]
        )
        return self._rest_outcome(observation, captured.get("response"))

    def _rest_outcome(
        self, observation: object, response: httpx.Response | None
    ) -> DiscordRestOutcome:
        retry_after_ms: int | None = None
        bucket: str | None = None
        global_limited = False
        if response is not None and response.status_code == 429:
            try:
                limited_body = response.json()
            except ValueError:
                limited_body = None
            if isinstance(limited_body, Mapping):
                global_limited = limited_body.get("global") is True
                raw_retry = limited_body.get("retry_after")
                try:
                    retry_after_ms = max(0, round(float(str(raw_retry)) * 1000))
                except (TypeError, ValueError):
                    retry_after_ms = None
            if retry_after_ms is None:
                header_retry = response.headers.get("retry-after")
                try:
                    retry_after_ms = max(0, round(float(header_retry) * 1000)) if header_retry else None
                except ValueError:
                    retry_after_ms = None
            bucket = response.headers.get("x-ratelimit-bucket")
        state = observation.state  # type: ignore[attr-defined]
        return DiscordRestOutcome(
            state=state,
            status_code=observation.status_code,  # type: ignore[attr-defined]
            native_id=observation.native_id,  # type: ignore[attr-defined]
            reason_code=observation.reason_code,  # type: ignore[attr-defined]
            retry_after_ms=retry_after_ms,
            bucket=bucket,
            global_limited=global_limited,
        )

    def _observe(self, error: ConnectionClosed, generation: int) -> WebSocketCloseObservation:
        return observe_connection_closed(
            error, generation=generation, local_intent=self._intents.get(generation)
        )

    def _forget_socket(self, generation: int) -> None:
        if self._socket_generation == generation:
            self._socket = None
        stale = sorted(self._intents)
        for old in stale[:-4]:
            self._intents.pop(old, None)


__all__ = [
    "DiscordEndpointPolicy",
    "DiscordGatewayTransport",
    "DiscordRestOutcome",
    "OFFICIAL_CDN_HOSTS",
    "OFFICIAL_GATEWAY_HOSTS",
    "OFFICIAL_REST_BASE_URL",
]
