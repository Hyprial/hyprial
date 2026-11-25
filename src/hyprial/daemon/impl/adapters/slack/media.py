"""Slack native media authority and external-upload implementation."""

from __future__ import annotations

import hashlib
import inspect
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from typing import Protocol
from urllib.parse import urlsplit

import httpx

from hyprial.daemon.impl.adapters.contracts import (
    AuthorizedByteStream,
    NativeTargetResolution,
    OperationNode,
    PartSubmission,
    SealedProviderMediaRef,
)

from ..common.transport import (
    HttpAccepted,
    HttpEffectObservation,
    HttpEffectState,
    NativeMediaScope,
    SealedNativeMediaAuthority,
    execute_http_effect,
)
from .errors import SlackProtocolError
from .types import NATIVE_SCOPE_PROVIDER_FIELD, SlackCredentialResolver
from .transport import OFFICIAL_API_BASE_URL, request_with_admission_marker, retry_after_ms


MEDIA_CHUNK_BYTES = 64 * 1024
_NATIVE_FILE_ID = re.compile(r"^[A-Za-z0-9]{2,128}$")


@dataclass(frozen=True, slots=True)
class SlackUploadSubmission(PartSubmission):
    """An upload part plus its validated retry delay in milliseconds."""

    retry_after_ms: int | None = None


class SlackMediaScopeResolver(Protocol):
    def resolve(self, source: SealedProviderMediaRef) -> NativeMediaScope: ...


def _json(response: httpx.Response) -> Mapping[str, object] | None:
    try:
        value = response.json()
    except (ValueError, httpx.DecodingError):
        return None
    return value if isinstance(value, Mapping) else None


def _origin(url: str) -> str:
    parsed = urlsplit(url)
    port = f":{parsed.port}" if parsed.port is not None else ""
    return f"{parsed.scheme}://{parsed.hostname or ''}{port}"


def _trusted_slack_url(url: str, explicit: frozenset[str]) -> str:
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower()
    official = parsed.scheme == "https" and (
        host == "slack.com"
        or host.endswith(".slack.com")
        or host == "slack-files.com"
        or host.endswith(".slack-files.com")
    )
    override = _origin(url) in explicit
    if (
        not (official or override)
        or parsed.username is not None
        or parsed.password is not None
        or (official and parsed.port not in (None, 443))
    ):
        raise SlackProtocolError("SLACK_MEDIA_URL_UNTRUSTED")
    return url


def _request_map(operation: OperationNode) -> Mapping[str, object]:
    value = operation.sealed_request
    if value is None:
        raise SlackProtocolError("SLACK_MEDIA_REQUEST_REQUIRED")
    try:
        mapped = dict(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as error:
        raise SlackProtocolError("SLACK_MEDIA_REQUEST_INVALID") from error
    return mapped


class SlackMedia:
    """Borrow the bundle HTTP client; never own or close it."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        credentials: SlackCredentialResolver,
        receiver_ref: str,
        *,
        authority: SealedNativeMediaAuthority | None,
        scope_resolver: SlackMediaScopeResolver | None,
        api_base_url: str = OFFICIAL_API_BASE_URL,
        allowed_upload_origins: set[str] | frozenset[str] = frozenset(),
        allowed_download_origins: set[str] | frozenset[str] = frozenset(),
        settle: Callable[[HttpEffectObservation], object] | None = None,
    ) -> None:
        if not isinstance(client, httpx.AsyncClient):
            raise TypeError("client must be httpx.AsyncClient")
        if not receiver_ref:
            raise ValueError("Slack receiver ref is required")
        self.client = client
        self._credentials = credentials
        self._receiver_ref = receiver_ref
        self._authority = authority
        self._scope_resolver = scope_resolver
        self._api_base_url = api_base_url.rstrip("/")
        self._upload_origins = frozenset(allowed_upload_origins)
        self._download_origins = frozenset(allowed_download_origins)
        self._settlement = settle

    async def _settle(self, observation: HttpEffectObservation) -> None:
        if self._settlement is None:
            return
        result = self._settlement(observation)
        if inspect.isawaitable(result):
            await result

    async def _scope(self, source: SealedProviderMediaRef) -> NativeMediaScope:
        if self._scope_resolver is None:
            raise SlackProtocolError("SLACK_MEDIA_SCOPE_RESOLVER_UNAVAILABLE")
        value = self._scope_resolver.resolve(source)
        if inspect.isawaitable(value):
            value = await value
        if not isinstance(value, NativeMediaScope) or getattr(
            value, NATIVE_SCOPE_PROVIDER_FIELD
        ) != "slack":
            raise SlackProtocolError("SLACK_MEDIA_SCOPE_INVALID")
        return value

    async def fetch(
        self, source: SealedProviderMediaRef, max_bytes: int
    ) -> AuthorizedByteStream:
        if self._authority is None:
            raise SlackProtocolError("SLACK_MEDIA_AUTHORITY_UNAVAILABLE")
        if (
            isinstance(max_bytes, bool)
            or not isinstance(max_bytes, int)
            or max_bytes < 1
        ):
            raise SlackProtocolError("SLACK_MEDIA_LIMIT_INVALID")
        return self._authority.open(
            source, scope=await self._scope(source), max_bytes=max_bytes
        )

    async def _effect(
        self,
        request: httpx.Request,
        *,
        native_id: str,
        parse: Callable[[httpx.Response], HttpAccepted],
    ) -> tuple[HttpEffectObservation, httpx.Response | None]:
        response_holder: list[httpx.Response] = []
        settled: list[HttpEffectObservation] = []

        async def send(attempt) -> httpx.Response:
            response = await self.client.send(
                request_with_admission_marker(request, attempt)
            )
            attempt.mark_response_received()
            response_holder.append(response)
            data = _json(response)
            if 200 <= response.status_code < 300 and data and data.get("ok") is False:
                return httpx.Response(
                    400,
                    headers=response.headers,
                    content=response.content,
                    request=response.request,
                )
            return response

        async def settle(observation: HttpEffectObservation) -> None:
            if (
                observation.state is HttpEffectState.UNKNOWN
                and native_id != "pending"
                and observation.native_id is None
            ):
                observation = replace(observation, native_id=native_id)
            settled.append(observation)
            await self._settle(observation)

        observation = await execute_http_effect(
            send, effecting=True, parse_success=parse, settle=settle
        )
        if settled:
            observation = settled[0]
        if (
            observation.state is HttpEffectState.ACCEPTED
            and observation.native_id is None
        ):
            raise SlackProtocolError("SLACK_MEDIA_NATIVE_ID_MISSING")
        return observation, response_holder[0] if response_holder else None

    @staticmethod
    def _part(
        part_index: int,
        observation: HttpEffectObservation,
        phase: str,
        retry_after_ms: int | None = None,
    ) -> SlackUploadSubmission:
        if (
            observation.status_code == 429
            and observation.state is HttpEffectState.REJECTED
        ):
            # A 429 response proves Slack refused the effect: it is
            # safely retryable after the validated delay, never final.
            return SlackUploadSubmission(
                part_index,
                "failed-retryable",
                None,
                True,
                f"slack-media-{phase}-rate-limited",
                retry_after_ms,
            )
        if observation.state is HttpEffectState.ACCEPTED:
            return SlackUploadSubmission(
                part_index, "accepted", observation.native_id, False, None
            )
        if observation.state is HttpEffectState.UNKNOWN:
            return SlackUploadSubmission(
                part_index, "unknown", None, False, f"slack-media-{phase}-unknown"
            )
        if observation.state is HttpEffectState.RETRYABLE:
            return SlackUploadSubmission(
                part_index,
                "failed-retryable",
                None,
                observation.definitely_not_sent,
                f"slack-media-{phase}-retryable",
            )
        return SlackUploadSubmission(
            part_index,
            "failed-final",
            None,
            observation.definitely_not_sent,
            f"slack-media-{phase}-rejected",
        )

    async def _read_stream(self, stream: AuthorizedByteStream) -> bytes:
        if (
            isinstance(stream.size, bool)
            or not isinstance(stream.size, int)
            or stream.size < 0
        ):
            raise SlackProtocolError("SLACK_MEDIA_SIZE_INVALID")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = await stream.read(min(MEDIA_CHUNK_BYTES, stream.size - total + 1))
            if not isinstance(chunk, bytes):
                raise SlackProtocolError("SLACK_MEDIA_STREAM_INVALID")
            if not chunk:
                break
            total += len(chunk)
            if total > stream.size:
                raise SlackProtocolError("SLACK_MEDIA_SIZE_DRIFT")
            chunks.append(chunk)
        body = b"".join(chunks)
        if len(body) != stream.size:
            raise SlackProtocolError("SLACK_MEDIA_SIZE_DRIFT")
        if hashlib.sha256(body).hexdigest() != stream.sha256:
            raise SlackProtocolError("SLACK_MEDIA_HASH_DRIFT")
        return body

    async def upload(
        self,
        operation: OperationNode,
        target: NativeTargetResolution,
        stream: AuthorizedByteStream,
    ) -> PartSubmission:
        request = _request_map(operation)
        part_index = request.get("part_index", 0)
        if (
            isinstance(part_index, bool)
            or not isinstance(part_index, int)
            or part_index < 0
        ):
            raise SlackProtocolError("SLACK_MEDIA_PART_INVALID")
        if request.get("method") == "files.upload":
            raise SlackProtocolError("SLACK_DEPRECATED_UPLOAD_FORBIDDEN")
        if operation.kind != "file-upload" or request.get("method") != "files.uploadV2":
            raise SlackProtocolError("SLACK_MEDIA_ROUTE_REQUIRED")
        if target.status != "resolved" or not target.native_chat_id:
            raise SlackProtocolError("SLACK_MEDIA_TARGET_UNRESOLVED")
        if (
            request.get("channel") != target.native_chat_id
            or request.get("thread_ts") != target.native_thread_id
        ):
            raise SlackProtocolError("SLACK_MEDIA_TARGET_MISMATCH")
        instance = target.target.chat_ref.instance
        expected = {
            "provider": "slack",
            "organization_id": instance.organization_id,
            "instance_id": instance.instance_id,
            "generation": instance.generation,
            "receiver_ref": self._receiver_ref,
        }
        if any(request.get(key) != value for key, value in expected.items()):
            raise SlackProtocolError("SLACK_MEDIA_SCOPE_MISMATCH")
        credentials = await self._credentials.resolve(self._receiver_ref)
        body: bytes
        try:
            body = await self._read_stream(stream)
            first_data: dict[str, str] = {}
            first_request = self.client.build_request(
                "POST",
                f"{self._api_base_url}/files.getUploadURLExternal",
                headers={"Authorization": f"Bearer {credentials.bot_token}"},
                json={"filename": stream.name or "upload.bin", "length": stream.size},
            )

            def parse_first(response: httpx.Response) -> HttpAccepted:
                data = _json(response)
                file_id = data.get("file_id") if data else None
                upload_url = data.get("upload_url") if data else None
                if data is None or data.get("ok") is not True:
                    raise ValueError("Slack upload URL response rejected")
                if not isinstance(file_id, str) or not _NATIVE_FILE_ID.fullmatch(
                    file_id
                ):
                    raise ValueError("Slack upload URL response missing file id")
                if not isinstance(upload_url, str):
                    raise ValueError("Slack upload URL response missing URL")
                first_data.update(file_id=file_id, upload_url=upload_url)
                return HttpAccepted(file_id)

            first, first_response = await self._effect(
                first_request, native_id="pending", parse=parse_first
            )
            if first.state is not HttpEffectState.ACCEPTED:
                return self._part(
                    part_index,
                    first,
                    "get-upload-url",
                    retry_after_ms(first_response) if first_response else None,
                )
            file_id = first_data["file_id"]
            upload_url = _trusted_slack_url(
                first_data["upload_url"], self._upload_origins
            )
            byte_request = self.client.build_request(
                "POST",
                upload_url,
                headers={
                    "Content-Type": stream.media_type or "application/octet-stream"
                },
                content=body,
            )

            def parse_bytes(response: httpx.Response) -> HttpAccepted:
                if not 200 <= response.status_code < 300:
                    raise ValueError("Slack external upload rejected")
                return HttpAccepted(file_id)

            uploaded, upload_response = await self._effect(
                byte_request, native_id=file_id, parse=parse_bytes
            )
            if uploaded.state is not HttpEffectState.ACCEPTED:
                return self._part(
                    part_index,
                    uploaded,
                    "upload-bytes",
                    retry_after_ms(upload_response) if upload_response else None,
                )
            complete_payload: dict[str, object] = {
                "files": [{"id": file_id, "title": stream.name or "upload.bin"}],
                "channel_id": target.native_chat_id,
            }
            if target.native_thread_id:
                complete_payload["thread_ts"] = target.native_thread_id
            complete_request = self.client.build_request(
                "POST",
                f"{self._api_base_url}/files.completeUploadExternal",
                headers={"Authorization": f"Bearer {credentials.bot_token}"},
                json=complete_payload,
            )

            def parse_complete(response: httpx.Response) -> HttpAccepted:
                data = _json(response)
                files = data.get("files") if data else None
                if (
                    data is None
                    or data.get("ok") is not True
                    or not isinstance(files, list)
                ):
                    raise ValueError("Slack completion response malformed")
                if not any(
                    isinstance(item, Mapping) and item.get("id") == file_id
                    for item in files
                ):
                    raise ValueError("Slack completion response changed file id")
                return HttpAccepted(file_id)

            completed, complete_response = await self._effect(
                complete_request, native_id=file_id, parse=parse_complete
            )
            return self._part(
                part_index,
                completed,
                "complete",
                retry_after_ms(complete_response) if complete_response else None,
            )
        finally:
            await stream.aclose()

    async def capture(
        self, file_id: str, *, scope: NativeMediaScope, max_bytes: int
    ) -> SealedProviderMediaRef:
        if not isinstance(file_id, str) or not _NATIVE_FILE_ID.fullmatch(file_id):
            raise SlackProtocolError("SLACK_MEDIA_NATIVE_ID_INVALID")
        if not isinstance(scope, NativeMediaScope) or getattr(
            scope, NATIVE_SCOPE_PROVIDER_FIELD
        ) != "slack":
            raise SlackProtocolError("SLACK_MEDIA_SCOPE_INVALID")
        if (
            isinstance(max_bytes, bool)
            or not isinstance(max_bytes, int)
            or max_bytes < 1
        ):
            raise SlackProtocolError("SLACK_MEDIA_LIMIT_INVALID")
        if self._authority is None:
            raise SlackProtocolError("SLACK_MEDIA_AUTHORITY_UNAVAILABLE")
        credentials = await self._credentials.resolve(self._receiver_ref)
        headers = {"Authorization": f"Bearer {credentials.bot_token}"}
        try:
            info = await self.client.get(
                f"{self._api_base_url}/files.info",
                params={"file": file_id},
                headers=headers,
            )
        except httpx.HTTPError as error:
            raise SlackProtocolError(
                "SLACK_MEDIA_INFO_FAILED", type(error).__name__
            ) from error
        data = _json(info)
        native = data.get("file") if data else None
        if (
            info.status_code != 200
            or data is None
            or data.get("ok") is not True
            or not isinstance(native, Mapping)
        ):
            raise SlackProtocolError("SLACK_MEDIA_INFO_INVALID")
        if native.get("id") != file_id:
            raise SlackProtocolError("SLACK_MEDIA_NATIVE_ID_DRIFT")
        size = native.get("size")
        if (
            isinstance(size, bool)
            or not isinstance(size, int)
            or size < 0
            or size > max_bytes
        ):
            raise SlackProtocolError("SLACK_MEDIA_SIZE_INVALID")
        download_url = native.get("url_private_download") or native.get("url_private")
        if not isinstance(download_url, str):
            raise SlackProtocolError("SLACK_MEDIA_DOWNLOAD_URL_MISSING")
        download_url = _trusted_slack_url(download_url, self._download_origins)
        try:
            async with self.client.stream(
                "GET", download_url, headers=headers
            ) as response:
                if response.is_redirect:
                    raise SlackProtocolError("SLACK_MEDIA_REDIRECT_FORBIDDEN")
                if response.status_code != 200:
                    raise SlackProtocolError("SLACK_MEDIA_DOWNLOAD_REJECTED")
                advertised = response.headers.get("Content-Length")
                if advertised is not None:
                    try:
                        advertised_size = int(advertised)
                    except ValueError as error:
                        raise SlackProtocolError(
                            "SLACK_MEDIA_DOWNLOAD_REJECTED"
                        ) from error
                    if advertised_size < 0 or advertised_size > max_bytes:
                        raise SlackProtocolError("SLACK_MEDIA_DOWNLOAD_REJECTED")
                chunks: list[bytes] = []
                captured_size = 0
                async for chunk in response.aiter_bytes():
                    captured_size += len(chunk)
                    if captured_size > max_bytes:
                        raise SlackProtocolError("SLACK_MEDIA_DOWNLOAD_REJECTED")
                    chunks.append(chunk)
        except httpx.HTTPError as error:
            raise SlackProtocolError(
                "SLACK_MEDIA_DOWNLOAD_FAILED", type(error).__name__
            ) from error
        body = b"".join(chunks)
        if len(body) != size:
            raise SlackProtocolError("SLACK_MEDIA_SIZE_DRIFT")
        digest = hashlib.sha256(body).hexdigest()
        expected_digest = native.get("sha256")
        if expected_digest is not None and expected_digest != digest:
            raise SlackProtocolError("SLACK_MEDIA_HASH_DRIFT")
        name = native.get("name") if isinstance(native.get("name"), str) else None
        media_type = (
            native.get("mimetype") if isinstance(native.get("mimetype"), str) else None
        )
        return self._authority.seal(
            scope=scope,
            body=body,
            name=name,
            media_type=media_type,
            native_metadata={"fileId": file_id},
        )


__all__ = [
    "MEDIA_CHUNK_BYTES",
    "SlackMedia",
    "SlackMediaScopeResolver",
    "SlackUploadSubmission",
]
