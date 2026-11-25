"""Closed chat/mail intent union and frozen synchronization policy values."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal, TypeAlias

from .content import CONTENT_BLOCK_TYPES, ContentBlock


IntentName = Literal[
    "chat.reply.source",
    "chat.reply.private",
    "chat.send.explicit",
    "mail.reply.sender",
    "mail.reply.reply-to",
    "mail.reply.all",
    "mail.send.explicit",
]
SyncMode = Literal["inherit", "corresponding", "exact"]
EffectiveSyncMode = Literal["exact", "corresponding"]
SyncEventClass = Literal[
    "human-chat-input",
    "human-mail-input",
    "chat-reply-source",
    "chat-reply-private",
    "chat-send-explicit",
    "mail-reply",
    "mail-send-explicit",
]
INTENT_NAMES = (
    "chat.reply.source",
    "chat.reply.private",
    "chat.send.explicit",
    "mail.reply.sender",
    "mail.reply.reply-to",
    "mail.reply.all",
    "mail.send.explicit",
)
SYNC_MODES = ("inherit", "corresponding", "exact")
EFFECTIVE_SYNC_MODES = ("exact", "corresponding")
SYNC_EVENT_CLASSES = (
    "human-chat-input",
    "human-mail-input",
    "chat-reply-source",
    "chat-reply-private",
    "chat-send-explicit",
    "mail-reply",
    "mail-send-explicit",
)


def _tuple(value: Sequence[object], *, name: str, limit: int = 1024) -> tuple[object, ...]:
    if isinstance(value, (str, bytes, bytearray, memoryview)):
        raise TypeError(f"{name} must be a sequence")
    result = tuple(value)
    if len(result) > limit:
        raise ValueError(f"{name} exceeds {limit} entries")
    return result


def _tuple_field(instance: object, name: str, *, limit: int = 1024) -> None:
    object.__setattr__(instance, name, _tuple(getattr(instance, name), name=name, limit=limit))


def _required(value: str, name: str) -> None:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must not be empty")
    if len(value.encode("utf-8")) > 4096:
        raise ValueError(f"{name} exceeds 4096 bytes")


def _opaque_ref(value: object, *, prefix: str, name: str) -> str:
    _required(value, name)  # type: ignore[arg-type]
    if not value.startswith(prefix):  # type: ignore[union-attr]
        raise ValueError(f"{name} must be an opaque {prefix[:-1]} value")
    if "@" in value:  # type: ignore[operator]
        raise ValueError(f"{name} must not contain an email address")
    return value  # type: ignore[return-value]


def _version_pairs(value: object, *, name: str) -> tuple[tuple[str, int], ...]:
    result = _tuple(value, name=name, limit=256)  # type: ignore[arg-type]
    if not all(
        isinstance(item, tuple)
        and len(item) == 2
        and isinstance(item[0], str)
        and bool(item[0])
        and isinstance(item[1], int)
        and not isinstance(item[1], bool)
        and item[1] > 0
        for item in result
    ):
        raise ValueError(f"{name} must contain positive generation pairs")
    typed = tuple(result)  # type: ignore[assignment]
    keys = tuple(item[0] for item in typed)
    if len(keys) != len(set(keys)):
        raise ValueError(f"{name} keys must be unique")
    if typed != tuple(sorted(typed, key=lambda item: item[0].encode("utf-8"))):
        raise ValueError(f"{name} must be sorted by UTF-8 key")
    return typed


@dataclass(frozen=True, slots=True)
class IntentCapability:
    name: IntentName
    allowed_sync: tuple[SyncMode, ...]
    parent_required: bool

    def __post_init__(self) -> None:
        _tuple_field(self, "allowed_sync", limit=3)
        if self.name not in INTENT_NAMES:
            raise ValueError("unknown intent capability")
        if not isinstance(self.parent_required, bool):
            raise TypeError("intent capability parent_required must be bool")
        if any(mode not in SYNC_MODES for mode in self.allowed_sync):
            raise ValueError("unknown intent capability sync mode")
        if not self.allowed_sync or len(set(self.allowed_sync)) != len(self.allowed_sync):
            raise ValueError("intent capability sync modes must be non-empty and unique")


@dataclass(frozen=True, slots=True)
class IntentPayload:
    intent_id: str
    content: tuple[ContentBlock, ...]
    attachments: tuple[str, ...]
    idempotency_key: str

    def __post_init__(self) -> None:
        _required(self.intent_id, "intent_id")
        _required(self.idempotency_key, "idempotency_key")
        _tuple_field(self, "content")
        _tuple_field(self, "attachments", limit=128)
        if not all(isinstance(block, CONTENT_BLOCK_TYPES) for block in self.content):
            raise TypeError("intent content must use the closed ContentBlock union")
        if not all(isinstance(handle, str) and handle for handle in self.attachments):
            raise ValueError("attachment refs must be non-empty strings")
        if len(self.attachments) != len(set(self.attachments)):
            raise ValueError("attachment refs must be unique")


@dataclass(frozen=True, slots=True)
class ChatReplyIntent:
    schema: Literal["hyprial.chat.intent.chat-reply/v1"]
    payload: IntentPayload
    parent_event_id: str
    mode: Literal["source-audience", "sender-private"]
    sync: SyncMode

    def __post_init__(self) -> None:
        if self.schema != "hyprial.chat.intent.chat-reply/v1":
            raise ValueError("unknown chat reply schema")
        _required(self.parent_event_id, "parent_event_id")
        if not isinstance(self.payload, IntentPayload):
            raise TypeError("chat reply payload must be IntentPayload")
        if self.mode not in ("source-audience", "sender-private") or self.sync not in SYNC_MODES:
            raise ValueError("invalid chat reply mode or sync")


@dataclass(frozen=True, slots=True)
class ChatSendIntent:
    schema: Literal["hyprial.chat.intent.chat-send/v1"]
    payload: IntentPayload
    parent_event_id: None
    mode: Literal["explicit-chats"]
    chat_refs: tuple[str, ...]
    sync: Literal["corresponding", "exact"]

    def __post_init__(self) -> None:
        if self.schema != "hyprial.chat.intent.chat-send/v1":
            raise ValueError("unknown chat send schema")
        if self.parent_event_id is not None or self.mode != "explicit-chats":
            raise ValueError("chat send must be explicit and parentless")
        _tuple_field(self, "chat_refs", limit=256)
        if not isinstance(self.payload, IntentPayload):
            raise TypeError("chat send payload must be IntentPayload")
        if self.sync not in ("corresponding", "exact"):
            raise ValueError("chat send sync must be corresponding or exact")
        if not self.chat_refs:
            raise ValueError("chat send requires opaque chat refs")
        for ref in self.chat_refs:
            _opaque_ref(ref, prefix="chat-ref:v1:", name="chat_ref")
        if len(self.chat_refs) != len(set(self.chat_refs)):
            raise ValueError("chat refs must be unique")


@dataclass(frozen=True, slots=True)
class MailReplyIntent:
    schema: Literal["hyprial.chat.intent.mail-reply/v1"]
    payload: IntentPayload
    parent_event_id: str
    mode: Literal["reply-sender", "reply-reply-to", "reply-all"]
    sync: SyncMode

    def __post_init__(self) -> None:
        if self.schema != "hyprial.chat.intent.mail-reply/v1":
            raise ValueError("unknown mail reply schema")
        _required(self.parent_event_id, "parent_event_id")
        if not isinstance(self.payload, IntentPayload):
            raise TypeError("mail reply payload must be IntentPayload")
        if self.mode not in ("reply-sender", "reply-reply-to", "reply-all"):
            raise ValueError("invalid mail reply mode")
        if self.sync not in SYNC_MODES:
            raise ValueError("invalid mail reply sync")


@dataclass(frozen=True, slots=True)
class MailSendIntent:
    schema: Literal["hyprial.chat.intent.mail-send/v1"]
    payload: IntentPayload
    parent_event_id: None
    mode: Literal["explicit-recipients"]
    recipient_refs: tuple[str, ...]
    sync: Literal["exact"]

    def __post_init__(self) -> None:
        if self.schema != "hyprial.chat.intent.mail-send/v1":
            raise ValueError("unknown mail send schema")
        if self.parent_event_id is not None or self.mode != "explicit-recipients" or self.sync != "exact":
            raise ValueError("mail send must be explicit, exact, and parentless")
        _tuple_field(self, "recipient_refs", limit=256)
        if not isinstance(self.payload, IntentPayload):
            raise TypeError("mail send payload must be IntentPayload")
        if not self.recipient_refs:
            raise ValueError("mail send requires verified opaque recipient refs")
        for ref in self.recipient_refs:
            _opaque_ref(ref, prefix="recipient-ref:v1:", name="recipient_ref")
        if len(self.recipient_refs) != len(set(self.recipient_refs)):
            raise ValueError("recipient refs must be unique")


IntentV1: TypeAlias = ChatReplyIntent | ChatSendIntent | MailReplyIntent | MailSendIntent
INTENT_TYPES = (ChatReplyIntent, ChatSendIntent, MailReplyIntent, MailSendIntent)


@dataclass(frozen=True, slots=True)
class SyncPolicySnapshotV1:
    schema: Literal["hyprial.chat.sync-policy/v1"]
    policy_id: str
    organization_id: str
    channel_id: str
    revision: int
    human_chat_input: EffectiveSyncMode
    human_mail_input: EffectiveSyncMode
    chat_reply_source: EffectiveSyncMode
    chat_reply_private: EffectiveSyncMode
    mail_reply: EffectiveSyncMode
    correspondence_versions: tuple[tuple[str, int], ...]
    binding_generations: tuple[tuple[str, int], ...]
    created_at_ms: int
    digest: str

    def __post_init__(self) -> None:
        if self.schema != "hyprial.chat.sync-policy/v1":
            raise ValueError("unknown sync policy schema")
        for name in ("policy_id", "organization_id", "channel_id", "digest"):
            _required(getattr(self, name), name)
        if isinstance(self.revision, bool) or not isinstance(self.revision, int):
            raise TypeError("sync policy revision must be an integer")
        if self.revision < 1:
            raise ValueError("sync policy revision must be positive")
        if isinstance(self.created_at_ms, bool) or not isinstance(self.created_at_ms, int):
            raise TypeError("sync policy creation time must be an integer")
        if self.created_at_ms < 0:
            raise ValueError("sync policy creation time must not be negative")
        for field_name in (
            "human_chat_input",
            "human_mail_input",
            "chat_reply_source",
            "chat_reply_private",
            "mail_reply",
        ):
            if getattr(self, field_name) not in EFFECTIVE_SYNC_MODES:
                raise ValueError(f"{field_name} must be an effective sync mode")
        object.__setattr__(
            self,
            "correspondence_versions",
            _version_pairs(self.correspondence_versions, name="correspondence_versions"),
        )
        object.__setattr__(
            self,
            "binding_generations",
            _version_pairs(self.binding_generations, name="binding_generations"),
        )


@dataclass(frozen=True, slots=True)
class FrozenSyncTargetSetV1:
    policy: SyncPolicySnapshotV1
    requested: SyncMode
    effective: EffectiveSyncMode
    event_class: SyncEventClass
    base_refs: tuple[str, ...]
    target_refs: tuple[str, ...]
    already_present_refs: tuple[str, ...]
    unresolved: tuple[tuple[str, str], ...]
    audience_digest: str

    def __post_init__(self) -> None:
        for name in ("base_refs", "target_refs", "already_present_refs", "unresolved"):
            _tuple_field(self, name, limit=256)
        _required(self.audience_digest, "audience_digest")
        if not isinstance(self.policy, SyncPolicySnapshotV1):
            raise TypeError("frozen target set requires a sync policy snapshot")
        if self.requested not in SYNC_MODES or self.effective not in EFFECTIVE_SYNC_MODES:
            raise ValueError("unknown requested or effective sync mode")
        if self.event_class not in SYNC_EVENT_CLASSES:
            raise ValueError("unknown sync event class")
        if self.requested != "inherit" and self.requested != self.effective:
            raise ValueError("explicit requested sync must equal effective sync")
        inherited_fields = {
            "human-chat-input": "human_chat_input",
            "human-mail-input": "human_mail_input",
            "chat-reply-source": "chat_reply_source",
            "chat-reply-private": "chat_reply_private",
            "mail-reply": "mail_reply",
        }
        if self.requested == "inherit":
            policy_field = inherited_fields.get(self.event_class)
            if policy_field is None:
                raise ValueError("explicit send event classes cannot inherit sync")
            if self.effective != getattr(self.policy, policy_field):
                raise ValueError("inherited effective sync must match the frozen event policy")
        if self.event_class == "chat-send-explicit" and self.requested == "inherit":
            raise ValueError("chat explicit send cannot inherit sync")
        if self.event_class == "mail-send-explicit" and (
            self.requested != "exact" or self.effective != "exact"
        ):
            raise ValueError("mail explicit send must use exact sync")
        for name in ("base_refs", "target_refs", "already_present_refs", "unresolved"):
            value = getattr(self, name)
            if value != tuple(sorted(value)):
                raise ValueError(f"{name} must be sorted for a frozen target set")
        if set(self.target_refs) & set(self.already_present_refs):
            raise ValueError("already-present refs cannot be send targets")
        for name in ("base_refs", "target_refs", "already_present_refs"):
            refs = getattr(self, name)
            if len(refs) != len(set(refs)):
                raise ValueError(f"{name} must be unique")
        if self.effective == "exact" and not set(self.target_refs) <= set(self.base_refs):
            raise ValueError("exact sync cannot expand beyond base refs")


_RAW_TARGET_KEYS = frozenset(
    {
        "nativeTarget",
        "nativeChatId",
        "nativeThreadId",
        "emailAddress",
        "recipientAddress",
        "providerTarget",
    }
)


def _has_raw_target(value: object) -> bool:
    if isinstance(value, Mapping):
        return any(key in _RAW_TARGET_KEYS or _has_raw_target(nested) for key, nested in value.items())
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray, memoryview)):
        return any(_has_raw_target(nested) for nested in value)
    return False


def validate_intent_v1(value: object) -> IntentV1 | dict[str, object]:
    """Validate the closed intent discriminator and reject provider-native targets."""

    if isinstance(value, INTENT_TYPES):
        return value
    if not isinstance(value, Mapping):
        return {"ok": False, "code": "INTENT_INVALID", "providerCalls": 0}
    if _has_raw_target(value):
        return {"ok": False, "code": "INTENT_NATIVE_TARGET_FORBIDDEN", "providerCalls": 0}
    schema = value.get("schema")
    if schema not in {
        "hyprial.chat.intent.chat-reply/v1",
        "hyprial.chat.intent.chat-send/v1",
        "hyprial.chat.intent.mail-reply/v1",
        "hyprial.chat.intent.mail-send/v1",
    }:
        return {"ok": False, "code": "INTENT_UNKNOWN", "providerCalls": 0}
    # Construction is intentionally typed: untrusted JSON must be decoded by a
    # boundary codec that builds IntentPayload/ContentBlock values explicitly.
    return {"ok": False, "code": "INTENT_TYPED_VALUE_REQUIRED", "providerCalls": 0}


__all__ = [
    "EffectiveSyncMode",
    "FrozenSyncTargetSetV1",
    "INTENT_TYPES",
    "IntentCapability",
    "IntentName",
    "IntentPayload",
    "IntentV1",
    "MailReplyIntent",
    "MailSendIntent",
    "ChatReplyIntent",
    "ChatSendIntent",
    "SyncEventClass",
    "SyncMode",
    "SyncPolicySnapshotV1",
    "validate_intent_v1",
]
