"""Authority-handle media streaming without large worker frames."""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass

from hyprial.daemon.impl.adapters.contracts import PartSubmission, canonical_json_v1

from .boundary import (
    ArtifactStatus,
    NativeWorker,
    WhatsAppEffectFence,
    WhatsAppProviderError,
    invoke_native,
    require_artifact,
)


MEDIA_CHUNK_BYTES = 64 * 1024


@dataclass(slots=True)
class _WorkerMediaStream:
    worker: NativeWorker
    stream_id: str
    name: str | None
    media_type: str | None
    size: int
    sha256: str
    remaining: int
    scope: dict[str, object]
    instance: object
    account_jid: str | None
    effect_fence: WhatsAppEffectFence | None
    closed: bool = False

    async def read(self, max_bytes: int) -> bytes:
        if self.closed or self.remaining == 0:
            return b""
        if max_bytes < 1:
            raise ValueError("max_bytes must be positive")
        if self.effect_fence is not None and self.account_jid is not None:
            self.effect_fence.assert_allows(self.instance, self.account_jid)
        requested = min(max_bytes, MEDIA_CHUNK_BYTES, self.remaining)
        response = await invoke_native(
            self.worker,
            "media.read",
            {"streamId": self.stream_id, "maxBytes": requested, **self.scope},
        )
        encoded = response.get("dataBase64")
        if not isinstance(encoded, str):
            raise WhatsAppProviderError("WHATSAPP_MEDIA_FRAME_INVALID", "media chunk missing")
        try:
            chunk = base64.b64decode(encoded, validate=True)
        except ValueError as error:
            raise WhatsAppProviderError("WHATSAPP_MEDIA_FRAME_INVALID", "invalid base64") from error
        if len(chunk) > requested:
            raise WhatsAppProviderError("WHATSAPP_MEDIA_FRAME_INVALID", "chunk exceeds authority")
        self.remaining -= len(chunk)
        if response.get("eof") is True:
            self.remaining = 0
        return bytes(chunk)

    async def aclose(self) -> None:
        if self.closed:
            return
        self.closed = True
        await invoke_native(
            self.worker, "media.close", {"streamId": self.stream_id, **self.scope}
        )


class WhatsAppMedia:
    def __init__(
        self,
        worker: NativeWorker,
        *,
        instance,
        artifact: ArtifactStatus,
        account_jid: str | None = None,
        effect_fence: WhatsAppEffectFence | None = None,
    ) -> None:
        self.worker = worker
        self.instance = instance
        self.artifact = artifact
        self.account_jid = account_jid
        self.effect_fence = effect_fence

    def _assert_active(self, account_jid: str | None = None) -> str | None:
        require_artifact(self.artifact)
        account = self.account_jid or account_jid
        if self.effect_fence is not None:
            if account is None:
                raise WhatsAppProviderError(
                    "WHATSAPP_EFFECT_SCOPE_MISMATCH", "media account scope missing"
                )
            self.effect_fence.assert_allows(self.instance, account)
        return account

    def _scope(self, account_jid: str | None) -> dict[str, object]:
        return {
            "instanceId": self.instance.instance_id,
            "generation": self.instance.generation,
            **({"accountJid": account_jid} if account_jid is not None else {}),
        }

    async def fetch(self, source, max_bytes: int) -> _WorkerMediaStream:
        account = self._assert_active()
        if source.instance_id != self.instance.instance_id or source.generation != self.instance.generation:
            raise WhatsAppProviderError("WHATSAPP_MEDIA_AUTHORITY_MISMATCH", "media scope mismatch")
        if max_bytes < 1:
            raise ValueError("max_bytes must be positive")
        response = await invoke_native(
            self.worker,
            "media.open",
            {
                "authorityHandleId": source.authority_handle_id,
                "maxBytes": max_bytes,
                **self._scope(account),
            },
        )
        size = response.get("size")
        if not isinstance(size, int) or not 0 <= size <= max_bytes:
            raise WhatsAppProviderError("WHATSAPP_MEDIA_AUTHORITY_EXCEEDED", "media size exceeds grant")
        scope = self._scope(account)
        return _WorkerMediaStream(
            self.worker,
            response["streamId"],
            response.get("name"),
            response.get("mediaType"),
            size,
            response["sha256"],
            size,
            scope,
            self.instance,
            account,
            self.effect_fence,
        )

    async def upload(self, operation, target, stream) -> PartSubmission:
        if target.status != "resolved" or not target.native_chat_id:
            raise WhatsAppProviderError("WHATSAPP_TARGET_UNRESOLVED", "media target unresolved")
        account = self._assert_active(target.target.chat_ref.account_scope)
        scope = self._scope(account)
        response = await invoke_native(
            self.worker,
            "media.upload-begin",
            {
                "nodeId": operation.node_id,
                "chatJid": target.native_chat_id,
                "name": stream.name,
                "mediaType": stream.media_type,
                "size": stream.size,
                "sha256": stream.sha256,
                **scope,
            },
        )
        upload_id = response.get("uploadId")
        if not isinstance(upload_id, str) or not upload_id:
            raise WhatsAppProviderError("WHATSAPP_MEDIA_FRAME_INVALID", "upload id missing")
        sent = 0
        try:
            while True:
                self._assert_active(account)
                chunk = bytes(await stream.read(MEDIA_CHUNK_BYTES))
                if not chunk:
                    break
                sent += len(chunk)
                if sent > stream.size:
                    raise WhatsAppProviderError(
                        "WHATSAPP_MEDIA_AUTHORITY_EXCEEDED", "upload stream exceeds declared size"
                    )
                accepted = await invoke_native(
                    self.worker,
                    "media.upload-chunk",
                    {
                        "uploadId": upload_id,
                        "dataBase64": base64.b64encode(chunk).decode(),
                        **scope,
                    },
                )
                if accepted.get("acceptedBytes") != len(chunk):
                    raise WhatsAppProviderError("WHATSAPP_MEDIA_FRAME_INVALID", "short native chunk")
            if sent != stream.size:
                raise WhatsAppProviderError("WHATSAPP_MEDIA_FRAME_INVALID", "upload size mismatch")
            finished = await invoke_native(
                self.worker,
                "media.upload-finish",
                {"uploadId": upload_id, "sha256": stream.sha256, **scope},
            )
            if finished.get("staged") is False:
                return PartSubmission(
                    0,
                    "failed-final",
                    None,
                    True,
                    str(finished.get("reasonCode") or "WHATSAPP_MEDIA_REJECTED"),
                )
            if finished.get("staged") is not True or finished.get("uploadId") != upload_id:
                raise WhatsAppProviderError(
                    "WHATSAPP_MEDIA_FRAME_INVALID", "native upload was not durably staged"
                )
            request = json.loads(canonical_json_v1(operation.sealed_request))
            sent_result = await invoke_native(
                self.worker,
                "media.upload-send",
                {
                    "uploadId": upload_id,
                    "nodeId": operation.node_id,
                    "kind": operation.kind,
                    "idempotencyKey": operation.idempotency_key,
                    "sealedRequest": request,
                    "chatJid": target.native_chat_id,
                    "quotedMessageId": request.get("quotedMessageId"),
                    **scope,
                },
            )
            if sent_result.get("state") == "accepted" and sent_result.get(
                "evidence"
            ) != "tryx-send-result":
                return PartSubmission(
                    sent_result.get("partIndex", 0),
                    "unknown",
                    None,
                    False,
                    "WHATSAPP_SEND_EVIDENCE_MISSING",
                )
            return PartSubmission(
                sent_result["partIndex"],
                sent_result["state"],
                sent_result.get("providerMessageId"),
                sent_result["definitelyNotSent"],
                sent_result.get("reasonCode"),
            )
        finally:
            await stream.aclose()


__all__ = ["MEDIA_CHUNK_BYTES", "WhatsAppMedia"]
