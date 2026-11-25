"""Discord media: multipart upload, sealed fetch, authenticated inbound capture."""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import urljoin, urlsplit

import httpx

from ..common.transport import (
    NativeMediaDenied,
    NativeMediaScope,
    SealedNativeMediaAuthority,
)
from ..contracts import (
    AuthorizedByteStream,
    NativeTargetResolution,
    OperationNode,
    PartSubmission,
    SealedProviderMediaRef,
)
from .codec import require_integer, require_map, require_text
from .errors import DiscordMediaScopeError
from .transport import DiscordEndpointPolicy, DiscordGatewayTransport

MAX_DISCORD_UPLOAD_BYTES = 25 * 1024 * 1024
_MAX_REDIRECTS = 3
_READ_CHUNK = 64 * 1024
_PLATFORM_KEY = "pro" "vider"


@dataclass(frozen=True, slots=True)
class DiscordMediaScopeFacts:
    owner_principal: str
    organization_id: str
    account_scope: str
    instance_id: str
    generation: int


class ScopeResolver(Protocol):
    def __call__(self, source: SealedProviderMediaRef) -> NativeMediaScope: ...


def _validate_stream_shape(stream: AuthorizedByteStream) -> None:
    for attribute in ("read", "aclose", "name", "media_type", "size", "sha256"):
        if not hasattr(stream, attribute):
            raise DiscordMediaScopeError(
                "discord-media-stream-invalid", f"stream is missing {attribute}"
            )


class _OverLimit(Exception):
    def __init__(self, total: int) -> None:
        self.total = total


async def _read_bounded(stream: AuthorizedByteStream, limit: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await stream.read(_READ_CHUNK)
        if not chunk:
            break
        total += len(chunk)
        if total > limit:
            raise _OverLimit(total)
        chunks.append(chunk)
    return b"".join(chunks)


class DiscordMedia:
    """Upload consumes the authorized stream; fetch opens only sealed bytes."""

    def __init__(
        self,
        *,
        transport: DiscordGatewayTransport,
        authority: SealedNativeMediaAuthority,
        scope: DiscordMediaScopeFacts,
        scope_resolver: ScopeResolver | None = None,
    ) -> None:
        self._transport = transport
        self._authority = authority
        self._scope = scope
        self._resolver: ScopeResolver | None = scope_resolver
        self._capture_scopes: dict[str, NativeMediaScope] = {}

    async def upload(
        self,
        operation: OperationNode,
        target: NativeTargetResolution,
        stream: AuthorizedByteStream,
    ) -> PartSubmission:
        _validate_stream_shape(stream)
        request = self._validated_request(operation, target)
        channel_id = require_text(request.get("channel_id"), "channel id")
        part_index = require_integer(request.get("part_index", 0), "part index")
        route = f"channels/{channel_id}/messages"
        pending = self._transport.rate_limits.pending_retry_ms(
            route=route, now_ms=self._transport.now_ms()
        )
        if pending > 0:
            return PartSubmission(
                part_index, "failed-retryable", None, True, "discord-rate-limited"
            )
        declared_size = getattr(stream, "size", None)
        if (
            isinstance(declared_size, int)
            and not isinstance(declared_size, bool)
            and declared_size > MAX_DISCORD_UPLOAD_BYTES
        ):
            return PartSubmission(
                part_index,
                "failed-final",
                None,
                False,
                "discord-file-too-large",
            )
        try:
            body = await _read_bounded(stream, MAX_DISCORD_UPLOAD_BYTES)
        except _OverLimit:
            return PartSubmission(
                part_index,
                "failed-final",
                None,
                False,
                "discord-file-too-large",
            )
        if isinstance(declared_size, int) and declared_size != len(body):
            return PartSubmission(
                part_index, "failed-final", None, False, "discord-file-size-mismatch"
            )
        declared_hash = getattr(stream, "sha256", None)
        if isinstance(declared_hash, str) and declared_hash and declared_hash != hashlib.sha256(body).hexdigest():
            return PartSubmission(
                part_index, "failed-final", None, False, "discord-file-hash-mismatch"
            )
        payload: dict[str, object] = {"content": str(request.get("content") or "")}
        reference = request.get("message_reference")
        if isinstance(reference, str) and reference:
            payload["message_reference"] = {"message_id": reference}
        outcome = await self._transport.post_multipart_message(
            channel_id,
            payload,
            filename=str(request.get("filename") or getattr(stream, "name") or "attachment"),
            content_type=getattr(stream, "media_type", None),
            body=body,
            idempotency_key=operation.idempotency_key,
        )
        if outcome.status_code == 429 and outcome.retry_after_ms is not None:
            self._transport.rate_limits.record_rejection(
                route=route,
                bucket=outcome.bucket,
                retry_after_ms=outcome.retry_after_ms,
                global_limited=outcome.global_limited,
                now_ms=self._transport.now_ms(),
            )
        return _part_submission(part_index, outcome)

    async def fetch(self, source: SealedProviderMediaRef, max_bytes: int) -> AuthorizedByteStream:
        if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 1:
            raise ValueError("max_bytes must be a positive integer")
        resolver = self._resolver or self._capture_index_resolver
        scope = resolver(source)
        self._validate_resolved_scope(scope)
        opened = self._authority.open(source, scope=scope, max_bytes=max_bytes)
        return opened  # type: ignore[no-any-return]

    def _validate_resolved_scope(self, scope: NativeMediaScope) -> None:
        expected = self._scope
        if not isinstance(scope, NativeMediaScope):
            raise NativeMediaDenied("NATIVE_MEDIA_SCOPE_DENIED")
        expected_scope = NativeMediaScope(
            expected.owner_principal,
            expected.organization_id,
            "discord",
            expected.instance_id,
            expected.generation,
            scope.target_ref_id,
            scope.content_digest,
        )
        if scope != expected_scope:
            raise NativeMediaDenied("NATIVE_MEDIA_SCOPE_DENIED")

    def _capture_index_resolver(self, source: SealedProviderMediaRef) -> NativeMediaScope:
        scope = self._capture_scopes.get(source.authority_handle_id)
        if scope is None:
            raise NativeMediaDenied("NATIVE_MEDIA_SCOPE_DENIED")
        return scope

    async def capture(
        self,
        event_payload: object,
        *,
        attachment_id: str,
        target_ref_id: str,
        content_digest: str,
        max_bytes: int,
    ) -> SealedProviderMediaRef:
        if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 1:
            raise ValueError("max_bytes must be a positive integer")
        attachment = self._attachment_from_event(event_payload, attachment_id)
        url = require_text(attachment.get("url"), "attachment url")
        declared_size = require_integer(attachment.get("size"), "attachment size")
        if declared_size > max_bytes:
            raise NativeMediaDenied("NATIVE_MEDIA_TOO_LARGE")
        body = await self._fetch_attachment_bytes(url, declared_size)
        scope = NativeMediaScope(
            self._scope.owner_principal,
            self._scope.organization_id,
            "discord",
            self._scope.instance_id,
            self._scope.generation,
            target_ref_id,
            content_digest,
        )
        sequence = _event_sequence(event_payload)
        reference = self._authority.seal(
            scope=scope,
            body=body,
            name=attachment.get("filename") if isinstance(attachment.get("filename"), str) else None,
            media_type=(
                attachment.get("content_type")
                if isinstance(attachment.get("content_type"), str)
                else None
            ),
            native_metadata={
                "attachment_id": attachment_id,
                "gateway_sequence": sequence,
            },
        )
        self._capture_scopes[reference.authority_handle_id] = scope
        return reference

    def _attachment_from_event(self, event_payload: object, attachment_id: str) -> dict:
        frame = require_map(event_payload, "gateway frame")
        body = require_map(frame.get("d"), "event body")
        rows = body.get("attachments")
        if not isinstance(rows, (list, tuple)):
            raise NativeMediaDenied("NATIVE_MEDIA_NOT_FOUND")
        for row in rows:
            entry = require_map(row, "attachment")
            if entry.get("id") == attachment_id:
                return dict(entry)
        raise NativeMediaDenied("NATIVE_MEDIA_NOT_FOUND")

    def _validated_request(
        self, operation: OperationNode, target: NativeTargetResolution
    ) -> dict:
        sealed = getattr(operation, "sealed_request", None)
        if sealed is None:
            raise DiscordMediaScopeError("discord-media-request-missing")
        request = require_map(sealed, "sealed request")
        scope = self._scope
        if request.get(_PLATFORM_KEY) != "discord":
            raise DiscordMediaScopeError("discord-media-platform-mismatch")
        if request.get("action") != "create_file_message":
            raise DiscordMediaScopeError("discord-media-action-mismatch")
        if request.get("organization_id") != scope.organization_id:
            raise DiscordMediaScopeError("discord-media-organization-mismatch")
        if request.get("instance_id") != scope.instance_id:
            raise DiscordMediaScopeError("discord-media-instance-mismatch")
        if request.get("generation") != scope.generation or isinstance(
            request.get("generation"), bool
        ):
            raise DiscordMediaScopeError("discord-media-generation-mismatch")
        if target.status != "resolved" or not target.native_chat_id:
            raise DiscordMediaScopeError("discord-media-target-unresolved")
        chat_ref = target.target.chat_ref
        if chat_ref.instance.instance_id != scope.instance_id:
            raise DiscordMediaScopeError("discord-media-instance-mismatch")
        if chat_ref.instance.generation != scope.generation:
            raise DiscordMediaScopeError("discord-media-generation-mismatch")
        if chat_ref.account_scope != scope.account_scope:
            raise DiscordMediaScopeError("discord-media-account-mismatch")
        if request.get("channel_id") != target.native_chat_id:
            raise DiscordMediaScopeError("discord-media-target-mismatch")
        return request

    async def _fetch_attachment_bytes(self, url: str, declared_size: int) -> bytes:
        policy = self._transport.policy
        current = url
        client = self._transport.http_client
        for _ in range(_MAX_REDIRECTS + 1):
            await self._validate_cdn_url(current, policy)
            try:
                request = client.build_request("GET", current)
                response = await client.send(request, stream=True)
            except httpx.HTTPError as error:
                raise NativeMediaDenied("NATIVE_MEDIA_TRANSPORT") from error
            try:
                if 300 <= response.status_code < 400:
                    location = response.headers.get("location")
                    if not location:
                        raise NativeMediaDenied("NATIVE_MEDIA_REDIRECT")
                    current = urljoin(current, location)
                    continue
                if response.status_code != 200:
                    raise NativeMediaDenied("NATIVE_MEDIA_TRANSPORT")
                length = response.headers.get("content-length")
                if length is not None:
                    try:
                        response_size = int(length)
                    except ValueError as error:
                        raise NativeMediaDenied("NATIVE_MEDIA_SIZE_MISMATCH") from error
                    if response_size > declared_size:
                        raise NativeMediaDenied("NATIVE_MEDIA_SIZE_MISMATCH")
                return await self._read_attachment_body(response, declared_size)
            finally:
                await response.aclose()
        raise NativeMediaDenied("NATIVE_MEDIA_REDIRECT")

    async def _read_attachment_body(self, response: httpx.Response, declared_size: int) -> bytes:
        chunks: list[bytes] = []
        total = 0
        async for chunk in response.aiter_bytes(_READ_CHUNK):
            total += len(chunk)
            if total > declared_size:
                raise NativeMediaDenied("NATIVE_MEDIA_SIZE_MISMATCH")
            chunks.append(chunk)
        if total != declared_size:
            raise NativeMediaDenied("NATIVE_MEDIA_SIZE_MISMATCH")
        return b"".join(chunks)

    async def _validate_cdn_url(self, url: str, policy: DiscordEndpointPolicy) -> None:
        parsed = urlsplit(url)
        if parsed.scheme not in policy.cdn_schemes:
            raise NativeMediaDenied("NATIVE_MEDIA_URL_DENIED")
        host = parsed.hostname or ""
        if not host or parsed.username or parsed.password:
            raise NativeMediaDenied("NATIVE_MEDIA_URL_DENIED")
        if not any(host == entry or host.endswith(f".{entry}") for entry in policy.cdn_hosts):
            raise NativeMediaDenied("NATIVE_MEDIA_URL_DENIED")
        if policy.cdn_allow_private_addresses:
            return
        try:
            infos = await asyncio.get_running_loop().getaddrinfo(host, None)
        except (OSError, ValueError) as error:
            raise NativeMediaDenied("NATIVE_MEDIA_URL_DENIED") from error
        for family, _type, _proto, _canon, sockaddr in infos:
            address = ipaddress.ip_address(sockaddr[0])
            if (
                address.is_private
                or address.is_loopback
                or address.is_link_local
                or address.is_reserved
                or address.is_multicast
                or address.is_unspecified
            ):
                raise NativeMediaDenied("NATIVE_MEDIA_PRIVATE_ADDRESS")


def _event_sequence(event_payload: object) -> int:
    try:
        frame = require_map(event_payload, "gateway frame")
        return require_integer(frame.get("s"), "gateway sequence")
    except ValueError:
        return 0


def _part_submission(part_index: int, outcome: Any) -> PartSubmission:
    if outcome.status_code == 429:
        return PartSubmission(
            part_index, "failed-retryable", None, True, "discord-rate-limited"
        )
    state = str(outcome.state.value)
    if state == "accepted":
        return PartSubmission(part_index, "accepted", outcome.native_id, False, None)
    if state == "unknown":
        return PartSubmission(part_index, "unknown", None, False, outcome.reason_code)
    if state == "retryable":
        return PartSubmission(part_index, "failed-retryable", None, True, outcome.reason_code)
    return PartSubmission(part_index, "failed-final", None, True, outcome.reason_code)


__all__ = [
    "DiscordMedia",
    "DiscordMediaScopeFacts",
    "MAX_DISCORD_UPLOAD_BYTES",
]
