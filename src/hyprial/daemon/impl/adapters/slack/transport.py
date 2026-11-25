"""Production HTTPX and WebSocket transport for Slack Socket Mode."""

from __future__ import annotations

import inspect
import json
import math
from collections.abc import Callable, Mapping
from typing import Any
from urllib.parse import urlsplit

import httpx
from websockets.exceptions import ConnectionClosed

from ..common.transport import (
    HttpAccepted,
    HttpEffectAttempt,
    HttpEffectObservation,
    ResourceCloseOutcome,
    TransportResourceOwner,
    execute_http_effect,
    observe_connection_closed,
    open_websocket_once,
)
from .errors import SlackHttpResult, SlackProtocolError
from .types import SlackCredentials


OFFICIAL_API_BASE_URL = "https://slack.com/api"
_INTERNAL_FIELDS = {
    "provider",
    "organization_id",
    "instance_id",
    "generation",
    "receiver_ref",
    "method",
    "part_index",
    "handle_id",
}


def _safe_json(response: httpx.Response) -> Mapping[str, object] | None:
    try:
        value = response.json()
    except (ValueError, httpx.DecodingError):
        return None
    return value if isinstance(value, Mapping) else None


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def retry_after_ms(response: httpx.Response) -> int | None:
    """Convert a validated Retry-After header (seconds) to milliseconds."""
    raw = response.headers.get("Retry-After")
    if raw is None:
        return None
    try:
        seconds = float(raw)
    except ValueError:
        return None
    if not math.isfinite(seconds) or seconds < 0:
        return None
    return int(seconds * 1000)


def _socket_url(value: str, *, allow_insecure_loopback: bool) -> str:
    parsed = urlsplit(value)
    host = (parsed.hostname or "").lower()
    official = parsed.scheme == "wss" and (
        host == "slack.com" or host.endswith(".slack.com")
    )
    loopback = (
        allow_insecure_loopback
        and parsed.scheme == "ws"
        and host
        in {
            "127.0.0.1",
            "::1",
            "localhost",
        }
    )
    if (
        not (official or loopback)
        or parsed.username is not None
        or parsed.password is not None
        or (official and parsed.port not in (None, 443))
    ):
        raise SlackProtocolError("SLACK_SOCKET_URL_UNTRUSTED")
    return value


class _AdmissionStream(httpx.AsyncByteStream):
    def __init__(self, body: bytes, attempt: HttpEffectAttempt) -> None:
        self._body = body
        self._attempt = attempt

    async def __aiter__(self):
        self._attempt.mark_may_have_sent()
        if self._body:
            yield self._body


def request_with_admission_marker(
    request: httpx.Request, attempt: HttpEffectAttempt
) -> httpx.Request:
    """Mark custody at the first handoff of request-body bytes to HTTPX."""

    return httpx.Request(
        request.method,
        request.url,
        headers=request.headers,
        content=_AdmissionStream(request.content, attempt),
        extensions=request.extensions,
    )


class HttpxSlackTransport:
    """One HTTP client plus at most one owned Socket Mode generation."""

    def __init__(
        self,
        *,
        client: httpx.AsyncClient | None = None,
        owns_client: bool | None = None,
        api_base_url: str = OFFICIAL_API_BASE_URL,
        settle: Callable[[HttpEffectObservation], object] | None = None,
        open_timeout: float = 10.0,
        close_timeout: float = 5.0,
        allow_insecure_loopback: bool = False,
        socket_url_override: str | None = None,
    ) -> None:
        if client is None:
            if owns_client is False:
                raise ValueError("a created Slack client must be owned")
            client = httpx.AsyncClient(trust_env=False, follow_redirects=False)
            owns = True
        else:
            owns = bool(owns_client) if owns_client is not None else False
        if open_timeout <= 0 or close_timeout <= 0:
            raise ValueError("Slack socket timeouts must be positive")
        self.client = client
        self._resources = TransportResourceOwner(client, owns_client=owns)
        self._api_base_url = api_base_url.rstrip("/")
        self._settlement = settle
        self._open_timeout = open_timeout
        self._close_timeout = close_timeout
        self._allow_insecure_loopback = allow_insecure_loopback
        self._socket_url_override = socket_url_override
        if socket_url_override is not None:
            _socket_url(
                socket_url_override, allow_insecure_loopback=allow_insecure_loopback
            )
        self._credentials: SlackCredentials | None = None
        self._issued_socket_url: str | None = None
        self._socket: Any = None
        self._socket_generation = 0

    def bind_credentials(self, credentials: SlackCredentials) -> None:
        if not isinstance(credentials, SlackCredentials):
            raise TypeError("credentials must be SlackCredentials")
        self._credentials = credentials

    def _credentials_required(self) -> SlackCredentials:
        if self._credentials is None:
            raise SlackProtocolError("SLACK_CREDENTIALS_UNAVAILABLE")
        return self._credentials

    def _url(self, method: str) -> str:
        if not method or "/" in method or "?" in method:
            raise SlackProtocolError("SLACK_METHOD_INVALID")
        return f"{self._api_base_url}/{method}"

    async def _post_control(
        self, method: str, *, token: str, payload: Mapping[str, object] | None = None
    ) -> Mapping[str, object]:
        try:
            response = await self.client.post(
                self._url(method), headers=_bearer(token), json=dict(payload or {})
            )
        except httpx.HTTPError as error:
            raise SlackProtocolError(
                "SLACK_CONTROL_REQUEST_FAILED", type(error).__name__
            ) from error
        data = _safe_json(response)
        if response.status_code < 200 or response.status_code >= 300 or not data:
            raise SlackProtocolError("SLACK_CONTROL_RESPONSE_INVALID")
        return data

    async def authenticate(self, credentials: SlackCredentials) -> object:
        self.bind_credentials(credentials)
        data = await self._post_control("auth.test", token=credentials.bot_token)
        return dict(data)

    async def open_socket(self, credentials: SlackCredentials) -> str:
        self.bind_credentials(credentials)
        if self._socket_url_override is not None:
            value = self._socket_url_override
        else:
            data = await self._post_control(
                "apps.connections.open", token=credentials.app_token
            )
            if data.get("ok") is not True or not isinstance(data.get("url"), str):
                raise SlackProtocolError(
                    "SLACK_SOCKET_OPEN_REJECTED", str(data.get("error") or "rejected")
                )
            value = data["url"]
        value = _socket_url(
            value, allow_insecure_loopback=self._allow_insecure_loopback
        )
        self._issued_socket_url = value
        return value

    async def connect(self, url: str, *, max_frame_bytes: int) -> None:
        if url != self._issued_socket_url or self._issued_socket_url is None:
            raise SlackProtocolError("SLACK_SOCKET_URL_STALE")
        if self._socket is not None:
            raise SlackProtocolError("SLACK_SOCKET_ALREADY_CONNECTED")
        _socket_url(url, allow_insecure_loopback=self._allow_insecure_loopback)
        self._issued_socket_url = None
        generation = self._socket_generation + 1
        socket = await open_websocket_once(
            url,
            generation=generation,
            open_timeout=self._open_timeout,
            close_timeout=self._close_timeout,
            max_size=max_frame_bytes,
        )
        self._resources.bind_socket(socket, generation=generation, owns_socket=True)
        self._socket_generation = generation
        self._socket = socket

    async def receive(self, *, max_frame_bytes: int) -> object:
        socket = self._socket
        if socket is None:
            raise SlackProtocolError("SLACK_SOCKET_NOT_CONNECTED")
        try:
            frame = await socket.recv()
        except ConnectionClosed as error:
            observation = observe_connection_closed(
                error, generation=self._socket_generation
            )
            raise SlackProtocolError(
                "SLACK_SOCKET_CLOSED", close_observation=observation
            ) from error
        if isinstance(frame, bytes):
            if len(frame) > max_frame_bytes:
                raise SlackProtocolError("SLACK_SOCKET_FRAME_TOO_LARGE")
            raw = frame
        elif isinstance(frame, str):
            raw = frame.encode()
            if len(raw) > max_frame_bytes:
                raise SlackProtocolError("SLACK_SOCKET_FRAME_TOO_LARGE")
        else:
            raise SlackProtocolError("SLACK_SOCKET_FRAME_INVALID")
        try:
            value = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise SlackProtocolError("SLACK_SOCKET_FRAME_INVALID") from error
        if not isinstance(value, Mapping):
            raise SlackProtocolError("SLACK_SOCKET_FRAME_INVALID")
        return dict(value)

    async def acknowledge(self, envelope_id: str) -> None:
        if not envelope_id:
            raise SlackProtocolError("SLACK_ENVELOPE_ID_REQUIRED")
        if self._socket is None:
            raise SlackProtocolError("SLACK_SOCKET_NOT_CONNECTED")
        await self._socket.send(
            json.dumps({"envelope_id": envelope_id}, separators=(",", ":"))
        )

    async def _settle(self, observation: HttpEffectObservation) -> None:
        if self._settlement is None:
            return
        result = self._settlement(observation)
        if inspect.isawaitable(result):
            await result

    @staticmethod
    def _retry_after(response: httpx.Response) -> int | None:
        return retry_after_ms(response)

    async def call(
        self, method: str, payload: object, idempotency_key: str
    ) -> SlackHttpResult:
        api_method = method
        if api_method in {"files.upload", "files.uploadV2"}:
            raise SlackProtocolError("SLACK_MEDIA_ROUTE_REQUIRED")
        if api_method != "chat.postMessage":
            raise SlackProtocolError("SLACK_METHOD_UNSUPPORTED")
        if not isinstance(payload, Mapping):
            raise SlackProtocolError("SLACK_PAYLOAD_INVALID")
        credentials = self._credentials_required()
        body = {
            str(key): value
            for key, value in payload.items()
            if key not in _INTERNAL_FIELDS
        }
        response_holder: list[httpx.Response] = []
        data_holder: list[Mapping[str, object] | None] = []

        async def send(attempt) -> httpx.Response:
            request = self.client.build_request(
                "POST",
                self._url(api_method),
                headers=_bearer(credentials.bot_token),
                json=body,
            )
            response = await self.client.send(
                request_with_admission_marker(request, attempt)
            )
            attempt.mark_response_received()
            data = _safe_json(response)
            response_holder.append(response)
            data_holder.append(data)
            if 200 <= response.status_code < 300 and data and data.get("ok") is False:
                return httpx.Response(
                    400,
                    headers=response.headers,
                    content=response.content,
                    request=response.request,
                )
            return response

        def parse(response: httpx.Response) -> HttpAccepted:
            data = _safe_json(response)
            if not data or data.get("ok") is not True:
                raise ValueError("Slack success response missing ok")
            native_id = data.get("ts")
            if not isinstance(native_id, str) or not native_id:
                raise ValueError("Slack chat success response missing ts")
            return HttpAccepted(native_id)

        observation = await execute_http_effect(
            send, effecting=True, parse_success=parse, settle=self._settle
        )
        response = response_holder[0] if response_holder else None
        data = data_holder[0] if data_holder else None
        error = (
            data.get("error") if data and isinstance(data.get("error"), str) else None
        )
        retry_after = self._retry_after(response) if response is not None else None
        return SlackHttpResult(observation, retry_after, error)

    async def reconcile(
        self, method: str, payload: object, idempotency_key: str
    ) -> None:
        return None

    async def close_socket(
        self, reason: str, timeout_at: float
    ) -> ResourceCloseOutcome:
        generation = self._socket_generation
        self._socket = None
        if generation < 1:
            return await self._resources.close_socket(
                generation=1,
                code=1001,
                reason=reason,
                timeout_at=timeout_at,
            )
        return await self._resources.close_socket(
            generation=generation,
            code=1001,
            reason=reason[:123],
            timeout_at=timeout_at,
        )

    async def aclose(self, timeout_at: float) -> ResourceCloseOutcome:
        self._socket = None
        return await self._resources.aclose(timeout_at)


__all__ = [
    "HttpxSlackTransport",
    "OFFICIAL_API_BASE_URL",
    "request_with_admission_marker",
    "retry_after_ms",
]
