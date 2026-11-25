"""Telegram native media capture, sealed fetch, and authorized upload."""

from __future__ import annotations

import hashlib
import inspect
import json
import re
from collections.abc import Mapping
from typing import Protocol

from hyprial.daemon.impl.adapters.common.transport import (
    HttpEffectState,
    NativeMediaScope,
    SealedNativeMediaAuthority,
)
from hyprial.daemon.impl.adapters.contracts import (
    AdapterInstanceRef,
    AuthorizedByteStream,
    NativeTargetResolution,
    OperationNode,
    PartSubmission,
    SealedProviderMediaRef,
)

from .contracts import TelegramCredentialResolver, _integer, _map, _text
from .errors import TelegramFilePathExpired, TelegramMediaDenied
from .transport import HttpxTelegramTransport, TelegramSendOutcome, validate_file_path


MAX_DOWNLOAD_BYTES = 20 * 1024 * 1024
_FILE_ID = re.compile(r"^[A-Za-z0-9_-]{1,512}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class TelegramMediaScopeResolver(Protocol):
    def for_file_id(self, file_id: str) -> NativeMediaScope: ...

    def for_sealed(self, source: SealedProviderMediaRef) -> NativeMediaScope: ...


async def _resolved(value: object) -> object:
    if inspect.isawaitable(value):
        return await value
    return value


def _scope(value: object) -> NativeMediaScope:
    # The frozen scope stores the owning chat platform under its sealed-media
    # wire key; read it by exact field name and keep the telegram fence literal.
    if not isinstance(value, NativeMediaScope) or getattr(value, "provider") != "telegram":
        raise TelegramMediaDenied("telegram-media-scope-invalid")
    return value


def _thaw(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _thaw(nested) for key, nested in value.items()}
    if isinstance(value, tuple):
        if value and all(
            isinstance(entry, tuple) and len(entry) == 2 and isinstance(entry[0], str)
            for entry in value
        ):
            return {entry[0]: _thaw(entry[1]) for entry in value}
        return [_thaw(nested) for nested in value]
    return value


def _part(outcome: TelegramSendOutcome, part_index: int) -> PartSubmission:
    effect = outcome.effect
    state = {
        HttpEffectState.ACCEPTED: "accepted",
        HttpEffectState.RETRYABLE: "failed-retryable",
        HttpEffectState.REJECTED: "failed-final",
        HttpEffectState.UNKNOWN: "unknown",
    }[effect.state]
    return PartSubmission(
        part_index,
        state,  # type: ignore[arg-type]
        effect.native_id,
        effect.definitely_not_sent,
        None if effect.state is HttpEffectState.ACCEPTED else effect.reason_code,
    )


class TelegramMedia:
    def __init__(
        self,
        transport: HttpxTelegramTransport,
        authority: SealedNativeMediaAuthority,
        scope_resolver: TelegramMediaScopeResolver,
        credentials: TelegramCredentialResolver,
        receiver_ref: str,
        instance: AdapterInstanceRef,
        account_scope: str,
    ) -> None:
        if not isinstance(transport, HttpxTelegramTransport):
            raise TypeError("transport must be HttpxTelegramTransport")
        if not isinstance(authority, SealedNativeMediaAuthority):
            raise TypeError("authority must be SealedNativeMediaAuthority")
        if not receiver_ref:
            raise ValueError("receiver_ref is required")
        if not isinstance(instance, AdapterInstanceRef):
            raise TypeError("instance must be AdapterInstanceRef")
        if instance.kind != "telegram" or instance.variant != "bot":
            raise ValueError("Telegram media requires telegram/bot")
        if not isinstance(account_scope, str) or not account_scope:
            raise ValueError("account_scope is required")
        self._transport = transport
        self._authority = authority
        self._scope_resolver = scope_resolver
        self._credentials = credentials
        self._receiver_ref = receiver_ref
        self._instance = instance
        self._account_scope = account_scope

    @property
    def transport(self) -> HttpxTelegramTransport:
        return self._transport

    async def _get_file(
        self, file_id: str
    ) -> tuple[object, Mapping[str, object]]:
        credentials = await self._credentials.resolve(self._receiver_ref)
        response = _map(
            await self._transport.get_file(credentials, file_id), "getFile response"
        )
        if response.get("ok") is not True:
            raise TelegramMediaDenied("telegram-get-file-rejected")
        result = _map(response.get("result"), "getFile result")
        if result.get("file_id") is not None and result.get("file_id") != file_id:
            raise TelegramMediaDenied("telegram-get-file-id-mismatch")
        return credentials, result

    async def capture(
        self,
        file_id: str,
        *,
        max_bytes: int,
        expected_sha256: str | None = None,
        name: str | None = None,
        media_type: str | None = None,
    ) -> SealedProviderMediaRef:
        if not isinstance(file_id, str) or not _FILE_ID.fullmatch(file_id):
            raise TelegramMediaDenied("telegram-native-file-id-invalid")
        if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 1:
            raise TelegramMediaDenied("telegram-file-limit-invalid")
        if expected_sha256 is not None and not _SHA256.fullmatch(expected_sha256):
            raise TelegramMediaDenied("telegram-file-hash-invalid")
        bound = min(max_bytes, MAX_DOWNLOAD_BYTES)
        credentials, result = await self._get_file(file_id)
        file_path = validate_file_path(result.get("file_path"))
        declared = result.get("file_size")
        if declared is not None:
            size = _integer(declared, "file size")
            if size > bound:
                raise TelegramMediaDenied("telegram-file-too-large")
        try:
            body = await self._transport.download_file(
                credentials, file_path, max_bytes=bound
            )
        except TelegramFilePathExpired:
            credentials, result = await self._get_file(file_id)
            file_path = validate_file_path(result.get("file_path"))
            declared = result.get("file_size")
            if declared is not None and _integer(declared, "file size") > bound:
                raise TelegramMediaDenied("telegram-file-too-large")
            body = await self._transport.download_file(
                credentials, file_path, max_bytes=bound
            )
        digest = hashlib.sha256(body).hexdigest()
        if expected_sha256 is not None and digest != expected_sha256:
            raise TelegramMediaDenied("telegram-file-hash-mismatch")
        scope = _scope(
            await _resolved(self._scope_resolver.for_file_id(file_id))
        )
        unique = result.get("file_unique_id")
        metadata = {"file_id": file_id}
        if isinstance(unique, str) and unique:
            metadata["file_unique_id"] = unique
        return self._authority.seal(
            scope=scope,
            body=body,
            name=name,
            media_type=media_type,
            native_metadata=metadata,
        )

    async def fetch(
        self, source: SealedProviderMediaRef, max_bytes: int
    ) -> AuthorizedByteStream:
        scope = _scope(
            await _resolved(self._scope_resolver.for_sealed(source))
        )
        return self._authority.open(source, scope=scope, max_bytes=max_bytes)

    async def _read_stream(self, stream: AuthorizedByteStream) -> bytes:
        if (
            isinstance(stream.size, bool)
            or not isinstance(stream.size, int)
            or stream.size < 0
            or stream.size > MAX_DOWNLOAD_BYTES
        ):
            raise TelegramMediaDenied("telegram-upload-too-large")
        if not _SHA256.fullmatch(stream.sha256):
            raise TelegramMediaDenied("telegram-upload-hash-invalid")
        chunks: list[bytes] = []
        size = 0
        while True:
            chunk = await stream.read(min(64 * 1024, MAX_DOWNLOAD_BYTES - size + 1))
            if not isinstance(chunk, bytes):
                raise TelegramMediaDenied("telegram-upload-stream-invalid")
            if not chunk:
                break
            size += len(chunk)
            if size > MAX_DOWNLOAD_BYTES:
                raise TelegramMediaDenied("telegram-upload-too-large")
            chunks.append(chunk)
        body = b"".join(chunks)
        if len(body) != stream.size or hashlib.sha256(body).hexdigest() != stream.sha256:
            raise TelegramMediaDenied("telegram-upload-integrity-drift")
        return body

    async def upload(
        self,
        operation: OperationNode,
        target: NativeTargetResolution,
        stream: AuthorizedByteStream,
    ) -> PartSubmission:
        if getattr(operation, "kind", None) != "file-send":
            raise TelegramMediaDenied("telegram-media-route-invalid")
        if target.status != "resolved" or target.native_chat_id is None:
            raise TelegramMediaDenied("telegram-media-target-unresolved")
        chat_ref = target.target.chat_ref
        if chat_ref.instance != self._instance or chat_ref.account_scope != self._account_scope:
            raise TelegramMediaDenied("telegram-media-target-scope-mismatch")
        raw = _thaw(operation.sealed_request)
        if not isinstance(raw, Mapping):
            raise TelegramMediaDenied("telegram-media-request-invalid")
        request = {str(key): value for key, value in raw.items()}
        method = _text(request.get("method"), "media method")
        if method not in {"sendPhoto", "sendDocument"}:
            raise TelegramMediaDenied("telegram-media-method-invalid")
        expected = (
            ("provider", "telegram", "telegram-media-provider-mismatch"),
            (
                "organization_id",
                self._instance.organization_id,
                "telegram-media-organization-mismatch",
            ),
            (
                "instance_id",
                self._instance.instance_id,
                "telegram-media-instance-mismatch",
            ),
            (
                "generation",
                self._instance.generation,
                "telegram-media-generation-mismatch",
            ),
            ("receiver_ref", self._receiver_ref, "telegram-media-receiver-mismatch"),
        )
        for field, value, reason in expected:
            if request.get(field) != value:
                raise TelegramMediaDenied(reason)
        if str(request.get("chat_id")) != target.native_chat_id:
            raise TelegramMediaDenied("telegram-media-target-mismatch")
        thread_id = request.get("message_thread_id")
        if thread_id is not None and str(thread_id) != target.native_thread_id:
            raise TelegramMediaDenied("telegram-media-thread-mismatch")
        fields = {"chat_id": target.native_chat_id}
        if target.native_thread_id is not None:
            fields["message_thread_id"] = target.native_thread_id
        reply = request.get("reply_parameters")
        if reply is not None:
            if not isinstance(reply, Mapping):
                raise TelegramMediaDenied("telegram-media-reply-invalid")
            fields["reply_parameters"] = json.dumps(
                dict(reply), separators=(",", ":"), sort_keys=True
            )
        body = await self._read_stream(stream)
        credentials = await self._credentials.resolve(self._receiver_ref)
        outcome = await self._transport.send_media(
            credentials,
            method,
            fields,
            filename=stream.name or "upload",
            body=body,
            media_type=stream.media_type or "application/octet-stream",
        )
        return _part(outcome, _integer(request.get("part_index", 0), "part index"))


__all__ = ["MAX_DOWNLOAD_BYTES", "TelegramMedia", "TelegramMediaScopeResolver"]
