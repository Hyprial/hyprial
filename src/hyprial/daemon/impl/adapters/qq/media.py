"""Source-A local-byte chunked media wire for the official QQ bot API.

Pinned phases (media-chunked.ts @ ca55d9c):
  upload_prepare -> presigned part PUT -> upload_part_finish -> completion
followed by the passive media message (msg_type 7, media.file_info). Every
mutating phase carries a stable effect identity (operation + phase); only
pre-admission rejections are retryable, anything post-admission is unknown.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from typing import Any

from hyprial.daemon.impl.adapters.common.transport import (
    HttpAccepted,
    HttpEffectState,
    NativeMediaDenied,
    NativeMediaScope,
    SealedNativeMediaAuthority,
)
from hyprial.daemon.impl.adapters.contracts import (
    AdapterInstanceRef,
    NativeTargetResolution,
    PartSubmission,
    SealedProviderMediaRef,
)

from .codec import thaw_json
from .components import QQOperationStore
from .errors import QQProtocolError
from .transport import (
    QQRestResult,
    QQRestWire,
    message_native_id,
    message_path,
    upload_complete_path,
    upload_part_finish_path,
    upload_prepare_path,
)

MD5_10M_SIZE = 10_002_432
MAX_MEDIA_BYTES = 100 * 1024 * 1024
RETRYABLE_BIZ_CODE = 22009

_FILE_TYPES = {"image": 1, "video": 2, "audio": 3, "file": 4}
_FORBIDDEN_SOURCE_KEYS = ("url", "path", "file_data", "local_path")


def _file_type(media_type: str | None) -> int:
    prefix = (media_type or "").split("/", 1)[0]
    return _FILE_TYPES.get(prefix, 4)


class _BoundMedia:
    __slots__ = ("scene", "target_id", "msg_id", "media_ref", "media_scope")

    def __init__(
        self, scene: str, target_id: str, msg_id: str, media_ref: dict, media_scope: dict
    ) -> None:
        self.scene = scene
        self.target_id = target_id
        self.msg_id = msg_id
        self.media_ref = media_ref
        self.media_scope = media_scope


def _final_part(part_index: int, reason: str) -> PartSubmission:
    return PartSubmission(part_index, "failed-final", None, True, reason)


def _failure(result: QQRestResult, part_index: int, phase: str) -> tuple[PartSubmission | None, int | None]:
    observation = result.observation
    if observation.state is HttpEffectState.ACCEPTED:
        return None, None
    if observation.state is HttpEffectState.UNKNOWN:
        return PartSubmission(part_index, "unknown", None, False, f"qq-{phase}-unknown"), None
    if observation.state is HttpEffectState.RETRYABLE:
        return PartSubmission(part_index, "failed-retryable", None, True, "qq-pre-send-failure"), None
    body = result.body or {}
    biz = body.get("code", body.get("err_code"))
    if biz == RETRYABLE_BIZ_CODE:
        return PartSubmission(part_index, "failed-retryable", None, True, "qq-biz-22009"), None
    status = observation.status_code
    if status == 429:
        delay = body.get("retry_after_ms")
        delay_ms = (
            delay if isinstance(delay, int) and not isinstance(delay, bool) and delay >= 0 else None
        )
        return PartSubmission(part_index, "failed-retryable", None, True, "qq-rate-limited"), delay_ms
    return PartSubmission(part_index, "failed-final", None, True, f"qq-{phase}-http-{status}"), None


def _prepare_parser(response: Any) -> HttpAccepted:
    payload = response.json()
    upload_id = payload.get("upload_id") if isinstance(payload, Mapping) else None
    if not isinstance(upload_id, str) or not upload_id:
        raise ValueError("upload_id missing")
    return HttpAccepted(upload_id)


def _no_native_id(_response: Any) -> HttpAccepted:
    return HttpAccepted(None)


def _file_info_parser(response: Any) -> HttpAccepted:
    payload = response.json()
    file_info = payload.get("file_info") if isinstance(payload, Mapping) else None
    if not isinstance(file_info, str) or not file_info:
        raise ValueError("file_info missing")
    return HttpAccepted(file_info)


class QQMedia:
    """Chunked uploader bound to one instance/org/account/native-msg scope."""

    def __init__(
        self,
        *,
        rest: QQRestWire,
        authority: SealedNativeMediaAuthority,
        store: QQOperationStore,
        instance: AdapterInstanceRef,
        app_id: str,
    ) -> None:
        self._rest = rest
        self._authority = authority
        self._store = store
        self._instance = instance
        self._app_id = app_id

    def _bind(self, request: Mapping[str, object]) -> _BoundMedia:
        expected = {
            "provider": "qq",
            "organization_id": self._instance.organization_id,
            "instance_id": self._instance.instance_id,
            "generation": self._instance.generation,
        }
        for field, value in expected.items():
            if request.get(field) != value:
                raise QQProtocolError("qq-media-scope-denied")
        scene = request.get("scene")
        if scene not in ("c2c", "group"):
            raise QQProtocolError("qq-media-scope-denied")
        target_id = request.get("target_id")
        if not isinstance(target_id, str) or not target_id:
            raise QQProtocolError("qq-media-scope-denied")
        msg_id = request.get("msg_id")
        if not isinstance(msg_id, str) or not msg_id:
            raise QQProtocolError("qq-msg-id-required")
        for key in _FORBIDDEN_SOURCE_KEYS:
            if request.get(key) is not None:
                raise QQProtocolError("qq-media-caller-url-rejected")
        media_ref = request.get("media_ref")
        media_scope = request.get("media_scope")
        if not isinstance(media_ref, Mapping) or not isinstance(media_scope, Mapping):
            raise QQProtocolError("qq-media-scope-denied")
        if (
            media_ref.get("instance_id") != self._instance.instance_id
            or media_ref.get("generation") != self._instance.generation
            or media_scope.get("organization_id") != self._instance.organization_id
            or media_scope.get("provider") != "qq"
            or media_scope.get("instance_id") != self._instance.instance_id
            or media_scope.get("generation") != self._instance.generation
            or media_scope.get("target_ref_id") != target_id
        ):
            raise QQProtocolError("qq-media-scope-denied")
        return _BoundMedia(scene, target_id, msg_id, dict(media_ref), dict(media_scope))

    def _state_key(self, operation_id: str) -> dict[str, Any]:
        return {
            "instance_id": self._instance.instance_id,
            "generation": self._instance.generation,
            "operation_id": operation_id,
        }

    def _open_stream(self, binding: _BoundMedia) -> Any:
        try:
            scope = NativeMediaScope(
                binding.media_scope["owner_principal"],
                binding.media_scope["organization_id"],
                "qq",
                binding.media_scope["instance_id"],
                binding.media_scope["generation"],
                binding.media_scope["target_ref_id"],
                binding.media_scope["content_digest"],
            )
            ref = SealedProviderMediaRef(
                instance_id=binding.media_ref["instance_id"],
                generation=binding.media_ref["generation"],
                opaque_ref_digest=binding.media_ref["opaque_ref_digest"],
                authority_handle_id=binding.media_ref["authority_handle_id"],
            )
        except (TypeError, ValueError, KeyError) as error:
            raise QQProtocolError("qq-media-scope-denied") from error
        try:
            return self._authority.open(ref, scope=scope, max_bytes=MAX_MEDIA_BYTES)
        except NativeMediaDenied as error:
            raise QQProtocolError("qq-media-scope-denied") from error

    async def _read_body(self, stream: Any) -> bytes:
        chunks: list[bytes] = []
        while True:
            chunk = await stream.read(1 << 20)
            if not chunk:
                break
            chunks.append(chunk)
        await stream.aclose()
        return b"".join(chunks)

    async def upload(
        self, operation: Any, target: NativeTargetResolution, stream: Any
    ) -> PartSubmission:
        """AdapterMedia seam: re-open sealed bytes and refuse misrouted handles."""
        sealed = getattr(operation, "sealed_request", None)
        request = thaw_json(sealed) if sealed is not None else None
        if not isinstance(request, Mapping):
            return _final_part(0, "qq-media-scope-denied")
        part_index = _part_index(request)
        try:
            binding = self._bind(request)
        except QQProtocolError as error:
            return _final_part(part_index, error.code)
        opened = self._open_stream(binding)
        if (
            getattr(stream, "sha256", None) != opened.sha256
            or getattr(stream, "size", None) != opened.size
        ):
            await opened.aclose()
            return _final_part(part_index, "qq-media-handle-mismatch")
        part, _delay = await self._deliver(
            binding, opened, _msg_seq(request), str(operation_id_of(operation)), _now_ms(request), part_index
        )
        return part

    async def send_for_runtime(
        self,
        request: Mapping[str, object],
        *,
        msg_seq: int,
        operation_id: str,
        now_ms: int,
    ) -> tuple[PartSubmission, int | None]:
        part_index = _part_index(request)
        try:
            binding = self._bind(request)
            opened = self._open_stream(binding)
        except QQProtocolError as error:
            return _final_part(part_index, error.code), None
        return await self._deliver(binding, opened, msg_seq, operation_id, now_ms, part_index)

    async def _deliver(
        self,
        binding: _BoundMedia,
        opened: Any,
        msg_seq: int,
        operation_id: str,
        now_ms: int,
        part_index: int,
    ) -> tuple[PartSubmission, int | None]:
        body = await self._read_body(opened)
        if len(body) > MAX_MEDIA_BYTES:
            return _final_part(part_index, "qq-media-too-large"), None
        md5 = hashlib.md5(body).hexdigest()
        sha1 = hashlib.sha1(body).hexdigest()
        md5_10m = hashlib.md5(body[:MD5_10M_SIZE]).hexdigest() if len(body) > MD5_10M_SIZE else md5
        state: dict[str, Any] = dict(
            self._store.load_media_state(**self._state_key(operation_id)) or {}
        )
        state.setdefault("done_parts", [])
        upload_id = state.get("upload_id")
        block_size = state.get("block_size")
        parts = state.get("parts")
        if not state.get("file_info"):
            if not upload_id or not block_size or not parts:
                prepared, part, delay = await self._prepare(
                    binding, opened, body, md5, sha1, md5_10m, operation_id, now_ms, part_index
                )
                if part is not None:
                    return part, delay
                upload_id, block_size, parts = prepared
                state.update({"upload_id": upload_id, "block_size": block_size, "parts": parts})
                self._store.save_media_state(
                    **self._state_key(operation_id), state=state, now_ms=now_ms
                )
            done: list[int] = list(state["done_parts"])
            for entry in parts:
                index = entry["index"]
                if index in done:
                    continue
                offset = (index - 1) * block_size
                chunk = body[offset : offset + block_size]
                part, delay = await self._put_and_finish(
                    binding, upload_id, index, chunk, entry["presigned_url"], part_index
                )
                if part is not None:
                    return part, delay
                done.append(index)
                state["done_parts"] = done
                self._store.save_media_state(
                    **self._state_key(operation_id), state=state, now_ms=now_ms
                )
            file_info, part, delay = await self._complete(binding, upload_id, part_index)
            if part is not None:
                return part, delay
            state["file_info"] = file_info
            self._store.save_media_state(**self._state_key(operation_id), state=state, now_ms=now_ms)
        return await self._send_message(
            binding, str(state["file_info"]), msg_seq, operation_id, now_ms, part_index
        )

    async def _prepare(
        self,
        binding: _BoundMedia,
        opened: Any,
        body: bytes,
        md5: str,
        sha1: str,
        md5_10m: str,
        operation_id: str,
        now_ms: int,
        part_index: int,
    ) -> tuple[tuple[str, int, list[dict]], PartSubmission | None, int | None]:
        result = await self._rest.post_json(
            upload_prepare_path(binding.scene, binding.target_id),
            {
                "file_type": _file_type(opened.media_type),
                "file_name": opened.name or "file",
                "file_size": len(body),
                "md5": md5,
                "sha1": sha1,
                "md5_10m": md5_10m,
            },
            effecting=True,
            upload=True,
            parse_success=_prepare_parser,
        )
        part, delay = _failure(result, part_index, "upload-prepare")
        if part is not None:
            return (("", 0, []), part, delay)
        payload = result.body or {}
        upload_id = payload.get("upload_id")
        block_size = payload.get("block_size")
        raw_parts = payload.get("parts")
        parts: list[dict] = []
        if isinstance(raw_parts, list) and raw_parts:
            for entry in raw_parts:
                if isinstance(entry, Mapping):
                    index = entry.get("index")
                    url = entry.get("presigned_url")
                    if (
                        isinstance(index, int)
                        and not isinstance(index, bool)
                        and index >= 1
                        and isinstance(url, str)
                        and url
                    ):
                        parts.append({"index": index, "presigned_url": url})
        if (
            not isinstance(upload_id, str)
            or not upload_id
            or isinstance(block_size, bool)
            or not isinstance(block_size, int)
            or block_size < 1
            or not parts
        ):
            return (
                ("", 0, []),
                PartSubmission(part_index, "unknown", None, False, "qq-upload-prepare-malformed"),
                None,
            )
        return ((upload_id, block_size, parts), None, None)

    async def _put_and_finish(
        self,
        binding: _BoundMedia,
        upload_id: str,
        index: int,
        chunk: bytes,
        presigned_url: str,
        part_index: int,
    ) -> tuple[PartSubmission | None, int | None]:
        try:
            put = await self._rest.put_bytes(presigned_url, chunk)
        except QQProtocolError as error:
            return _final_part(part_index, error.code), None
        part, delay = _failure(put, part_index, "cos-put")
        if part is not None:
            return part, delay
        finish = await self._rest.post_json(
            upload_part_finish_path(binding.scene, binding.target_id),
            {
                "upload_id": upload_id,
                "part_index": index,
                "block_size": len(chunk),
                "md5": hashlib.md5(chunk).hexdigest(),
            },
            effecting=True,
            upload=True,
            parse_success=_no_native_id,
        )
        return _failure(finish, part_index, "upload-part-finish")

    async def _complete(
        self, binding: _BoundMedia, upload_id: str, part_index: int
    ) -> tuple[str, PartSubmission | None, int | None]:
        result = await self._rest.post_json(
            upload_complete_path(binding.scene, binding.target_id),
            {"upload_id": upload_id},
            effecting=True,
            upload=True,
            parse_success=_file_info_parser,
        )
        part, delay = _failure(result, part_index, "upload-complete")
        if part is not None:
            return ("", part, delay)
        file_info = (result.body or {}).get("file_info")
        if not isinstance(file_info, str) or not file_info:
            return (
                "",
                PartSubmission(part_index, "unknown", None, False, "qq-upload-complete-malformed"),
                None,
            )
        return (file_info, None, None)

    async def _send_message(
        self,
        binding: _BoundMedia,
        file_info: str,
        msg_seq: int,
        operation_id: str,
        now_ms: int,
        part_index: int,
    ) -> tuple[PartSubmission, int | None]:
        result = await self._rest.post_json(
            message_path(binding.scene, binding.target_id),
            {
                "msg_type": 7,
                "media": {"file_info": file_info},
                "msg_seq": msg_seq,
                "msg_id": binding.msg_id,
            },
            effecting=True,
            parse_success=message_native_id,
        )
        observation = result.observation
        self._store.record_attempt(
            instance_id=self._instance.instance_id,
            generation=self._instance.generation,
            operation_id=operation_id,
            msg_id=binding.msg_id,
            msg_seq=msg_seq,
            attempted_at_ms=now_ms,
            result=(
                f"accepted:{observation.native_id}"
                if observation.state is HttpEffectState.ACCEPTED
                else "unknown"
            ),
        )
        if observation.state is HttpEffectState.ACCEPTED:
            return (
                PartSubmission(part_index, "accepted", observation.native_id, False, None),
                None,
            )
        part, delay = _failure(result, part_index, "media-send")
        assert part is not None
        return part, delay


def _part_index(request: Mapping) -> int:
    value = request.get("part_index", 0)
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _msg_seq(request: Mapping) -> int:
    value = request.get("msg_seq", 1)
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 1


def _now_ms(request: Mapping) -> int:
    value = request.get("now_ms")
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def operation_id_of(operation: Any) -> str:
    value = getattr(operation, "node_id", None) or getattr(operation, "idempotency_key", None)
    return value if isinstance(value, str) and value else "qq-media-operation"


__all__ = ["QQMedia"]
