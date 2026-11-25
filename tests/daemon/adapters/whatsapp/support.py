from __future__ import annotations

import hashlib
from collections import defaultdict, deque
from copy import deepcopy
from dataclasses import dataclass
from types import SimpleNamespace

from hyprial.daemon.impl.adapters.contracts import (
    AdapterInstanceRef,
    AuthorizedChatTarget,
    AudienceSnapshot,
    ChatRef,
    PlatformEventEnvelope,
    IngressObservation,
    NativeAlias,
    NativeCheckpoint,
    freeze_json_v1,
)


class FakeWorker:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []
        self.responses: dict[str, deque[object]] = defaultdict(deque)

    def add(self, operation: str, *responses: object) -> None:
        self.responses[operation].extend(responses)

    def invoke(self, payload: object, *, timeout: float = 15.0) -> object:
        assert isinstance(payload, dict)
        copied = deepcopy(payload)
        self.calls.append(copied)
        operation = copied["operation"]
        queue = self.responses[str(operation)]
        if not queue:
            raise AssertionError(f"unexpected native operation: {operation}")
        result = queue.popleft()
        if isinstance(result, BaseException):
            raise result
        if callable(result):
            return result(copied)
        return deepcopy(result)

    def operations(self) -> list[str]:
        return [str(call["operation"]) for call in self.calls]


def instance(generation: int = 7) -> AdapterInstanceRef:
    return AdapterInstanceRef(
        instance_id="wa-main",
        kind="whatsapp",
        variant="linked-device",
        local_name="whatsapp-main",
        organization_id="org-a",
        external_fingerprint="f" * 64,
        generation=generation,
    )


def native_event(
    *,
    message_id: str = "wamid-1",
    event_kind: str = "message",
    from_me: bool = False,
) -> dict[str, object]:
    return {
        "schema": "hyprial.whatsapp.native-event/v1",
        "accountJid": "15550001@s.whatsapp.net",
        "chatJid": "120363000000@g.us",
        "participantJid": "15550002@s.whatsapp.net",
        "senderJid": "15550002@s.whatsapp.net",
        "messageId": message_id,
        "deviceId": "device-0",
        "quotedMessageId": "wamid-parent",
        "wrappers": ["ephemeral", "view-once-v2"],
        "viewOnce": True,
        "isFromMe": from_me,
        "isGroup": True,
        "eventKind": event_kind,
        "text": "hello",
        "pushName": "Person",
        "providerCreatedAtMs": 1234,
        "receivedAtMs": 1250,
        "media": [
            {
                "ref": "native-media:wa-main:wamid-1:0",
                "kind": "image",
                "name": "photo.jpg",
                "mediaType": "image/jpeg",
                "size": 5,
                "sha256": hashlib.sha256(b"image").hexdigest(),
            }
        ],
        "unknown": {"contentType": "imageMessage"},
    }


def envelope(payload: dict[str, object] | None = None) -> PlatformEventEnvelope:
    data = payload or native_event()
    alias = NativeAlias(
        instance_id="wa-main",
        instance_generation=7,
        account_scope="15550001@s.whatsapp.net",
        provider_resource="120363000000@g.us",
        semantic_kind="create",
        alias_kind="message",
        normalized_id=str(data["messageId"]) or "invalid-message-id",
        normalized_revision="whatsapp-linked-device/v1",
    )
    return PlatformEventEnvelope(
        observation=IngressObservation(
            observation_id=f"obs:{data['messageId']}",
            aliases=(alias,),
            source_kind="live",
            raw_safe_ref=f"whatsapp-event:{data['messageId']}",
            observation_digest="d" * 64,
            received_at_ms=int(data["receivedAtMs"]),
        ),
        payload=data,
    )


def target() -> AuthorizedChatTarget:
    ref = ChatRef(
        ref_id="chat-ref:v1:wa-group",
        instance=instance(),
        account_scope="15550001@s.whatsapp.net",
        native_chat_id="120363000000@g.us",
        kind="group",
        native_thread_id=None,
        native_root_id=None,
    )
    return AuthorizedChatTarget(
        target_id="target-wa",
        chat_ref=ref,
        audience=AudienceSnapshot(
            kind="group",
            participants=(),
            completeness="unknown",
            correspondence_id=None,
            policy_version=1,
        ),
        operation="send",
        canonical_parent_id=None,
    )


def operation(node_id: str = "node-1", *, kind: str = "text-send") -> object:
    return SimpleNamespace(
        node_id=node_id,
        kind=kind,
        sealed_request=freeze_json_v1(
            {
                "chatJid": "120363000000@g.us",
                "text": "reply",
                "quotedMessageId": "wamid-parent",
                "partIndex": 0,
            }
        ),
        idempotency_key=f"idem:{node_id}",
    )


def checkpoint(token: str = "cursor-1") -> NativeCheckpoint:
    return NativeCheckpoint(kind="poll-cursor", token=token, deadline_ms=None)


@dataclass
class MemoryStream:
    chunks: deque[bytes]
    name: str | None = "file.bin"
    media_type: str | None = "application/octet-stream"

    def __post_init__(self) -> None:
        data = b"".join(self.chunks)
        self.size = len(data)
        self.sha256 = hashlib.sha256(data).hexdigest()
        self.closed = False

    async def read(self, max_bytes: int) -> bytes:
        if not self.chunks:
            return b""
        chunk = self.chunks.popleft()
        assert len(chunk) <= max_bytes
        return chunk

    async def aclose(self) -> None:
        self.closed = True
