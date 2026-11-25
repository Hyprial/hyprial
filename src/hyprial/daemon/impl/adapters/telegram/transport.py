"""Hardened HTTPX Telegram Bot API transport."""

from __future__ import annotations

import asyncio
import ipaddress
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING
from urllib.parse import quote, urlsplit

import httpx

from ..common.transport import (
    HttpAccepted,
    HttpEffectAttempt,
    HttpEffectObservation,
    HttpEffectState,
    HttpRequestPhase,
    ResourceCloseOutcome,
    ResourceCloseState,
    execute_http_effect,
)
from .errors import (
    TelegramFilePathExpired,
    TelegramMediaDenied,
    TelegramPollRetry,
    TelegramProtocolError,
)

if TYPE_CHECKING:
    from .contracts import TelegramCredentials


OFFICIAL_API_BASE_URL = "https://api.telegram.org"
ALLOWED_UPDATES = (
    "message",
    "edited_message",
    "channel_post",
    "edited_channel_post",
    "callback_query",
)
_API_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9]{0,63}$")
_FILE_SEGMENT = re.compile(r"^[A-Za-z0-9._-]+$")


@dataclass(frozen=True, slots=True)
class TelegramSendOutcome:
    effect: HttpEffectObservation
    retry_after_ms: int | None


def _validated_base_url(value: str) -> str:
    if not isinstance(value, str) or not value:
        raise TelegramProtocolError("telegram-endpoint-invalid")
    value = value.rstrip("/")
    if value == OFFICIAL_API_BASE_URL:
        return value
    try:
        parsed = urlsplit(value)
        address = ipaddress.ip_address(parsed.hostname or "")
    except (ValueError, TypeError) as error:
        raise TelegramProtocolError("telegram-endpoint-not-loopback") from error
    if (
        parsed.scheme not in {"http", "https"}
        or not address.is_loopback
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in ("", "/")
    ):
        raise TelegramProtocolError("telegram-endpoint-not-loopback")
    return value


def validate_file_path(value: object) -> str:
    if not isinstance(value, str) or not value or len(value.encode()) > 4096:
        raise TelegramMediaDenied("telegram-file-path-invalid")
    if "\\" in value or "\x00" in value or value.startswith("/"):
        raise TelegramMediaDenied("telegram-file-path-invalid")
    parts = value.split("/")
    if any(part in ("", ".", "..") or not _FILE_SEGMENT.fullmatch(part) for part in parts):
        raise TelegramMediaDenied("telegram-file-path-invalid")
    return value


class HttpxTelegramTransport:
    """One Bot API HTTPS client, without proxy inheritance or redirect following."""

    def __init__(
        self,
        client: httpx.AsyncClient | None = None,
        *,
        owns_client: bool | None = None,
        api_base_url: str = OFFICIAL_API_BASE_URL,
        read_timeout_seconds: float = 35.0,
    ) -> None:
        self._api_base_url = _validated_base_url(api_base_url)
        if not isinstance(read_timeout_seconds, (int, float)) or isinstance(
            read_timeout_seconds, bool
        ) or read_timeout_seconds <= 0:
            raise ValueError("read_timeout_seconds must be positive")
        self._read_timeout = float(read_timeout_seconds)
        if client is None:
            client = httpx.AsyncClient(trust_env=False, follow_redirects=False)
            actual_ownership = True
        else:
            if not isinstance(client, httpx.AsyncClient):
                raise TypeError("client must be httpx.AsyncClient")
            actual_ownership = False
        if owns_client is not None:
            if not isinstance(owns_client, bool):
                raise TypeError("owns_client must be bool or None")
            actual_ownership = owns_client
        self._client = client
        self._owns_client = actual_ownership
        self._poll_tasks: set[asyncio.Task[object]] = set()
        self._close_task: asyncio.Task[ResourceCloseOutcome] | None = None

    @property
    def client(self) -> httpx.AsyncClient:
        return self._client

    @staticmethod
    def _token(credentials: TelegramCredentials) -> str:
        token = getattr(credentials, "bot_token", None)
        if not isinstance(token, str) or not token:
            raise TelegramProtocolError("telegram-credentials-invalid")
        return token

    def _url(
        self, credentials: TelegramCredentials, value: str, *, file: bool = False
    ) -> str:
        token = quote(self._token(credentials), safe=":")
        if file:
            path = validate_file_path(value)
            return f"{self._api_base_url}/file/bot{token}/{path}"
        if not _API_PATTERN.fullmatch(value):
            raise TelegramProtocolError("telegram-method-invalid")
        return f"{self._api_base_url}/bot{token}/{value}"

    @staticmethod
    def _json(response: httpx.Response, reason: str) -> dict[str, object]:
        try:
            value = response.json()
        except (json.JSONDecodeError, UnicodeDecodeError, httpx.DecodingError) as error:
            raise TelegramProtocolError(reason) from error
        if not isinstance(value, dict):
            raise TelegramProtocolError(reason)
        return value

    def _timeout(self) -> httpx.Timeout:
        return httpx.Timeout(self._read_timeout, connect=10.0, write=10.0, pool=10.0)

    async def _post_json(
        self,
        credentials: TelegramCredentials,
        method: str,
        body: Mapping[str, object],
        *,
        long_poll: bool = False,
    ) -> dict[str, object]:
        if self._close_task is not None:
            raise TelegramProtocolError("telegram-transport-closed")
        try:
            response = await self._client.post(
                self._url(credentials, method),
                json=dict(body),
                timeout=self._timeout(),
            )
        except httpx.ReadTimeout as error:
            del error
            if long_poll:
                raise TelegramPollRetry("telegram-long-poll-timeout") from None
            raise TelegramProtocolError(f"telegram-{method.lower()}-transport") from None
        except httpx.HTTPError as error:
            del error
            raise TelegramProtocolError(f"telegram-{method.lower()}-transport") from None
        return self._json(response, f"telegram-{method.lower()}-response-invalid")

    async def get_me(self, credentials: TelegramCredentials) -> object:
        return await self._post_json(credentials, "getMe", {})

    async def get_webhook_info(self, credentials: TelegramCredentials) -> object:
        return await self._post_json(credentials, "getWebhookInfo", {})

    async def get_updates(
        self,
        credentials: TelegramCredentials,
        *,
        offset: int,
        limit: int,
    ) -> object:
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise ValueError("offset must be a non-negative integer")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValueError("limit must be 1..100")
        task = asyncio.current_task()
        if task is None:
            raise RuntimeError("Telegram long poll requires an asyncio task")
        self._poll_tasks.add(task)
        try:
            return await self._post_json(
                credentials,
                "getUpdates",
                {
                    "offset": offset,
                    "limit": limit,
                    "timeout": 30,
                    "allowed_updates": list(ALLOWED_UPDATES),
                },
                long_poll=True,
            )
        except asyncio.CancelledError:
            raise TelegramPollRetry("telegram-long-poll-cancelled") from None
        finally:
            self._poll_tasks.discard(task)

    async def get_file(
        self, credentials: TelegramCredentials, file_id: str
    ) -> dict[str, object]:
        return await self._post_json(credentials, "getFile", {"file_id": file_id})

    @staticmethod
    def _success(response: httpx.Response) -> HttpAccepted:
        value = HttpxTelegramTransport._json(response, "telegram-send-response-invalid")
        if value.get("ok") is not True or not isinstance(value.get("result"), dict):
            raise ValueError("Telegram send was not accepted")
        message_id = value["result"].get("message_id")  # type: ignore[index]
        if isinstance(message_id, bool) or not isinstance(message_id, (str, int)):
            raise ValueError("Telegram send response omitted message_id")
        native_id = str(message_id)
        if not native_id:
            raise ValueError("Telegram send response omitted message_id")
        return HttpAccepted(native_id)

    async def _effect(
        self,
        credentials: TelegramCredentials,
        method: str,
        *,
        json_body: Mapping[str, object] | None = None,
        data: Mapping[str, str] | None = None,
        files: Mapping[str, tuple[str, bytes, str]] | None = None,
    ) -> TelegramSendOutcome:
        if self._close_task is not None:
            effect = HttpEffectObservation(
                HttpRequestPhase.PRE_SEND,
                HttpEffectState.REJECTED,
                True,
                None,
                None,
                None,
                "telegram-transport-closed",
            )
            return TelegramSendOutcome(effect, None)
        response_box: list[httpx.Response] = []

        async def operation(attempt: HttpEffectAttempt) -> httpx.Response:
            async def trace(name: str, _info: Mapping[str, object]) -> None:
                if (
                    name.endswith("send_request_headers.started")
                    and attempt.phase is HttpRequestPhase.PRE_SEND
                ):
                    attempt.mark_may_have_sent()

            request = self._client.build_request(
                "POST",
                self._url(credentials, method),
                json=dict(json_body) if json_body is not None else None,
                data=dict(data) if data is not None else None,
                files=dict(files) if files is not None else None,
                timeout=self._timeout(),
                extensions={"trace": trace},
            )
            response = await self._client.send(request)
            if attempt.phase is HttpRequestPhase.PRE_SEND:
                attempt.mark_may_have_sent()
            attempt.mark_response_received()
            response_box.append(response)
            return response

        effect = await execute_http_effect(
            operation, effecting=True, parse_success=self._success
        )
        retry_after_ms = None
        if response_box and response_box[0].status_code == 429:
            try:
                body = self._json(response_box[0], "telegram-rate-limit-invalid")
                parameters = body.get("parameters")
                retry_after = (
                    parameters.get("retry_after")
                    if isinstance(parameters, Mapping)
                    else None
                )
            except TelegramProtocolError:
                retry_after = None
            if isinstance(retry_after, int) and not isinstance(retry_after, bool) and retry_after >= 0:
                retry_after_ms = retry_after * 1000
                effect = replace(
                    effect,
                    state=HttpEffectState.RETRYABLE,
                    definitely_not_sent=True,
                    reason_code="telegram-rate-limited",
                )
        return TelegramSendOutcome(effect, retry_after_ms)

    async def send_message(
        self, credentials: TelegramCredentials, payload: object
    ) -> TelegramSendOutcome:
        if not isinstance(payload, Mapping):
            raise TelegramProtocolError("telegram-send-request-invalid")
        allowed = {"chat_id", "text", "message_thread_id", "reply_parameters"}
        body = {str(key): value for key, value in payload.items() if key in allowed}
        if not isinstance(body.get("chat_id"), (str, int)) or not isinstance(
            body.get("text"), str
        ):
            raise TelegramProtocolError("telegram-send-request-invalid")
        return await self._effect(credentials, "sendMessage", json_body=body)

    async def send_media(
        self,
        credentials: TelegramCredentials,
        method: str,
        fields: Mapping[str, str],
        *,
        filename: str,
        body: bytes,
        media_type: str,
    ) -> TelegramSendOutcome:
        if method not in {"sendPhoto", "sendDocument"}:
            raise TelegramProtocolError("telegram-media-method-invalid")
        file_field = "photo" if method == "sendPhoto" else "document"
        return await self._effect(
            credentials,
            method,
            data=fields,
            files={file_field: (filename, body, media_type)},
        )

    async def download_file(
        self,
        credentials: TelegramCredentials,
        file_path: str,
        *,
        max_bytes: int,
    ) -> bytes:
        if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 1:
            raise ValueError("max_bytes must be positive")
        try:
            async with self._client.stream(
                "GET",
                self._url(credentials, file_path, file=True),
                timeout=self._timeout(),
            ) as response:
                if response.status_code == 404:
                    raise TelegramFilePathExpired("telegram-file-path-expired")
                if not 200 <= response.status_code < 300:
                    raise TelegramMediaDenied("telegram-file-download-rejected")
                length = response.headers.get("content-length")
                if length is not None:
                    try:
                        declared = int(length)
                    except ValueError as error:
                        raise TelegramMediaDenied("telegram-file-size-invalid") from error
                    if declared < 0 or declared > max_bytes:
                        raise TelegramMediaDenied("telegram-file-too-large")
                chunks: list[bytes] = []
                size = 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > max_bytes:
                        raise TelegramMediaDenied("telegram-file-too-large")
                    chunks.append(chunk)
                return b"".join(chunks)
        except TelegramMediaDenied:
            raise
        except httpx.HTTPError as error:
            del error
            raise TelegramMediaDenied("telegram-file-download-transport") from None

    async def _finalize(self) -> tuple[bool, bool]:
        polls = tuple(self._poll_tasks)
        for task in polls:
            task.cancel()
        if polls:
            await asyncio.gather(*polls, return_exceptions=True)
        if self._owns_client:
            await self._client.aclose()
            return True, True
        return True, False

    async def _close(self, deadline_ms: int) -> ResourceCloseOutcome:
        if isinstance(deadline_ms, bool) or not isinstance(deadline_ms, int) or deadline_ms < 0:
            raise ValueError("deadline_ms must be a non-negative integer")
        finalizer = asyncio.create_task(self._finalize())
        try:
            async with asyncio.timeout(deadline_ms / 1000):
                _polls_done, client_closed = await asyncio.shield(finalizer)
        except TimeoutError:
            return ResourceCloseOutcome(
                ResourceCloseState.UNKNOWN,
                None,
                False,
                self._client.is_closed,
                "telegram-close-deadline",
            )
        except Exception:
            return ResourceCloseOutcome(
                ResourceCloseState.UNKNOWN,
                None,
                False,
                self._client.is_closed,
                "telegram-close-error",
            )
        return ResourceCloseOutcome(
            ResourceCloseState.CLOSED, None, False, client_closed, None
        )

    async def aclose(self, deadline_ms: int) -> ResourceCloseOutcome:
        if self._close_task is None:
            self._close_task = asyncio.create_task(self._close(deadline_ms))
        return await asyncio.shield(self._close_task)


__all__ = [
    "ALLOWED_UPDATES",
    "OFFICIAL_API_BASE_URL",
    "HttpxTelegramTransport",
    "TelegramSendOutcome",
    "validate_file_path",
]
