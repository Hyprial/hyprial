"""Immutable message content, context, attachment, and control contracts."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, Protocol, TypeAlias

from .values import (
    AudienceSnapshot,
    FrozenJsonV1,
    MirrorLineage,
    PlatformProvenanceV1,
    PrincipalRef,
    SourceProvenance,
    ChatRef,
    PLATFORM_PROVENANCE_ARMS,
    freeze_json_v1,
)

if TYPE_CHECKING:
    from .intents import IntentCapability, SyncPolicySnapshotV1


def _as_tuple(value: object, *, name: str, limit: int | None = None) -> tuple[object, ...]:
    if isinstance(value, (str, bytes, bytearray, memoryview)):
        raise TypeError(f"{name} must be a sequence")
    result = tuple(value)  # type: ignore[arg-type]
    if limit is not None and len(result) > limit:
        raise ValueError(f"{name} exceeds {limit} entries")
    return result


def _tuple_field(instance: object, name: str, *, limit: int | None = None) -> None:
    object.__setattr__(instance, name, _as_tuple(getattr(instance, name), name=name, limit=limit))


def _required(value: str, name: str, *, max_bytes: int = 32 * 1024) -> None:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must not be empty")
    if len(value.encode("utf-8")) > max_bytes:
        raise ValueError(f"{name} exceeds {max_bytes} bytes")


AttachmentKind = Literal["image", "file", "audio", "video", "other"]


@dataclass(frozen=True, slots=True)
class AttachmentHandle:
    handle_id: str
    kind: AttachmentKind
    name: str | None
    media_type: str | None
    size: int | None
    sha256: str | None
    source_message_id: str
    expires_at_ms: int | None

    def __post_init__(self) -> None:
        _required(self.handle_id, "handle_id", max_bytes=4096)
        _required(self.source_message_id, "source_message_id", max_bytes=4096)
        if self.size is not None and self.size < 0:
            raise ValueError("attachment size must not be negative")
        if self.kind not in ("image", "file", "audio", "video", "other"):
            raise ValueError("unknown attachment kind")
        if self.sha256 is not None and (
            len(self.sha256) != 64 or any(ch not in "0123456789abcdef" for ch in self.sha256)
        ):
            raise ValueError("attachment sha256 must be lowercase hexadecimal")


@dataclass(frozen=True, slots=True)
class TextBlock:
    type: Literal["text"]
    text: str

    def __post_init__(self) -> None:
        if self.type != "text":
            raise ValueError("TextBlock discriminator mismatch")
        _required(self.text, "text")


@dataclass(frozen=True, slots=True)
class RichTextBlock:
    type: Literal["rich-text"]
    safe_ast: tuple[FrozenJsonV1, ...]
    plain_fallback: str

    def __post_init__(self) -> None:
        if self.type != "rich-text":
            raise ValueError("RichTextBlock discriminator mismatch")
        ast = _as_tuple(self.safe_ast, name="safe_ast", limit=1024)
        object.__setattr__(self, "safe_ast", tuple(freeze_json_v1(node) for node in ast))
        _required(self.plain_fallback, "plain_fallback")


@dataclass(frozen=True, slots=True)
class MentionBlock:
    type: Literal["mention"]
    principal: PrincipalRef
    label: str

    def __post_init__(self) -> None:
        if self.type != "mention":
            raise ValueError("MentionBlock discriminator mismatch")
        if not isinstance(self.principal, PrincipalRef):
            raise TypeError("mention principal must be typed")
        _required(self.label, "label", max_bytes=4096)


@dataclass(frozen=True, slots=True)
class AttachmentBlock:
    type: Literal["attachment"]
    handle: AttachmentHandle

    def __post_init__(self) -> None:
        if self.type != "attachment":
            raise ValueError("AttachmentBlock discriminator mismatch")
        if not isinstance(self.handle, AttachmentHandle):
            raise TypeError("attachment block handle must be typed")


ControlKind = Literal["command", "approval", "question"]


@dataclass(frozen=True, slots=True)
class ControlBlock:
    type: Literal["control"]
    control_id: str
    control_type: ControlKind
    presentation_only: bool
    callback_ref: str | None

    def __post_init__(self) -> None:
        if self.type != "control":
            raise ValueError("ControlBlock discriminator mismatch")
        _required(self.control_id, "control_id", max_bytes=4096)
        if self.control_type not in ("command", "approval", "question"):
            raise ValueError("unknown control block kind")
        if not isinstance(self.presentation_only, bool):
            raise TypeError("presentation_only must be bool")
        if self.callback_ref is not None:
            _required(self.callback_ref, "callback_ref", max_bytes=4096)
        if self.presentation_only != (self.callback_ref is None):
            raise ValueError("control callback presence must match presentation_only")


@dataclass(frozen=True, slots=True)
class UnsupportedNativeBlock:
    type: Literal["unsupported-native"]
    native_type: str
    safe_summary: str
    audit_ref: str

    def __post_init__(self) -> None:
        if self.type != "unsupported-native":
            raise ValueError("UnsupportedNativeBlock discriminator mismatch")
        _required(self.native_type, "native_type", max_bytes=4096)
        _required(self.safe_summary, "safe_summary")
        _required(self.audit_ref, "audit_ref", max_bytes=4096)


ContentBlock: TypeAlias = (
    TextBlock | RichTextBlock | MentionBlock | AttachmentBlock | ControlBlock | UnsupportedNativeBlock
)
CONTENT_BLOCK_TYPES = (
    TextBlock,
    RichTextBlock,
    MentionBlock,
    AttachmentBlock,
    ControlBlock,
    UnsupportedNativeBlock,
)


@dataclass(frozen=True, slots=True)
class ReplyReference:
    canonical_event_id: str | None
    source_native_message_id: str | None
    author: PrincipalRef | None
    excerpt: tuple[ContentBlock, ...]
    attachment_summaries: tuple[AttachmentHandle, ...]
    unavailable_reason: Literal[
        "not-delivered", "not-found", "deleted", "permission-denied", "unsupported"
    ] | None

    def __post_init__(self) -> None:
        _tuple_field(self, "excerpt", limit=1024)
        _tuple_field(self, "attachment_summaries", limit=128)
        if not all(isinstance(block, CONTENT_BLOCK_TYPES) for block in self.excerpt):
            raise TypeError("reply excerpt must use the closed ContentBlock union")
        if not all(isinstance(handle, AttachmentHandle) for handle in self.attachment_summaries):
            raise TypeError("reply attachment summaries must be typed")
        if self.author is not None and not isinstance(self.author, PrincipalRef):
            raise TypeError("reply author must be typed")
        if self.unavailable_reason not in (
            None,
            "not-delivered",
            "not-found",
            "deleted",
            "permission-denied",
            "unsupported",
        ):
            raise ValueError("unknown reply unavailable reason")
        if self.unavailable_reason is None and not (
            self.canonical_event_id or self.source_native_message_id
        ):
            raise ValueError("available reply reference requires an event or native message id")


@dataclass(frozen=True, slots=True)
class ChatContext:
    channel_id: str
    source_chat: ChatRef
    title: str | None
    sender: PrincipalRef
    audience: AudienceSnapshot
    mentions: tuple[PrincipalRef, ...]
    available_intents: tuple[IntentCapability, ...]
    capabilities_revision: str

    def __post_init__(self) -> None:
        from .intents import IntentCapability

        _required(self.channel_id, "channel_id", max_bytes=4096)
        _required(self.capabilities_revision, "capabilities_revision", max_bytes=4096)
        _tuple_field(self, "mentions", limit=128)
        _tuple_field(self, "available_intents", limit=16)
        if not isinstance(self.source_chat, ChatRef):
            raise TypeError("chat context source must be ChatRef")
        if not isinstance(self.sender, PrincipalRef) or not isinstance(self.audience, AudienceSnapshot):
            raise TypeError("chat context sender and audience must be typed")
        if not all(isinstance(item, PrincipalRef) for item in self.mentions):
            raise TypeError("chat context mentions must be typed")
        if not all(isinstance(item, IntentCapability) for item in self.available_intents):
            raise TypeError("chat context available intents must be typed")
        intent_names = tuple(item.name for item in self.available_intents)
        if len(intent_names) != len(set(intent_names)):
            raise ValueError("chat context available intents must be unique")


@dataclass(frozen=True, slots=True)
class ChatMessage:
    canonical_event_id: str
    channel_sequence: int
    context: ChatContext
    provenance: SourceProvenance
    platform: PlatformProvenanceV1
    lineage: MirrorLineage
    content: tuple[ContentBlock, ...]
    attachments: tuple[AttachmentHandle, ...]
    reply_to: ReplyReference | None
    sync_policy_snapshot: SyncPolicySnapshotV1
    admission_policy_revision: str

    def __post_init__(self) -> None:
        from .intents import SyncPolicySnapshotV1

        _required(self.canonical_event_id, "canonical_event_id", max_bytes=4096)
        _required(self.admission_policy_revision, "admission_policy_revision", max_bytes=4096)
        if self.channel_sequence < 1:
            raise ValueError("channel_sequence must be positive")
        _tuple_field(self, "content", limit=1024)
        _tuple_field(self, "attachments", limit=128)
        if not isinstance(self.context, ChatContext):
            raise TypeError("chat message context must be typed")
        if not isinstance(self.provenance, SourceProvenance):
            raise TypeError("chat message provenance must be typed")
        if not isinstance(self.platform, PLATFORM_PROVENANCE_ARMS):
            raise TypeError("chat message platform provenance must be a typed arm")
        if not isinstance(self.lineage, MirrorLineage):
            raise TypeError("chat message lineage must be typed")
        if not all(isinstance(block, CONTENT_BLOCK_TYPES) for block in self.content):
            raise TypeError("chat message content must use the closed ContentBlock union")
        if not all(isinstance(handle, AttachmentHandle) for handle in self.attachments):
            raise TypeError("chat message attachments must be typed")
        if self.reply_to is not None and not isinstance(self.reply_to, ReplyReference):
            raise TypeError("chat message reply_to must be typed")
        if not isinstance(self.sync_policy_snapshot, SyncPolicySnapshotV1):
            raise TypeError("chat message sync policy snapshot must be typed")


@dataclass(frozen=True, slots=True)
class ControlIntentV1:
    schema: Literal["hyprial.chat.control-intent/v1"]
    control_id: str
    kind: ControlKind
    owner_authority_ref: str
    owner_operation_id: str
    source_canonical_event_id: str
    channel_id: str
    agent_entity_token: str
    issuer_principal: str
    allowed_principals: tuple[str, ...]
    allowed_choices: tuple[str, ...]
    single_use: bool
    expires_at_ms: int
    prompt: str
    effect_command_ref: str
    effect_idempotency_key: str

    def __post_init__(self) -> None:
        if self.schema != "hyprial.chat.control-intent/v1":
            raise ValueError("unknown control intent schema")
        for name in (
            "control_id",
            "owner_authority_ref",
            "owner_operation_id",
            "source_canonical_event_id",
            "channel_id",
            "agent_entity_token",
            "issuer_principal",
            "effect_command_ref",
            "effect_idempotency_key",
        ):
            _required(getattr(self, name), name, max_bytes=4096)
        _tuple_field(self, "allowed_principals", limit=256)
        _tuple_field(self, "allowed_choices", limit=32)
        if self.kind not in ("command", "approval", "question"):
            raise ValueError("unknown control intent kind")
        if not isinstance(self.single_use, bool):
            raise TypeError("single_use must be bool")
        if not self.allowed_principals or not self.allowed_choices:
            raise ValueError("control intent requires principals and choices")
        if any(len(choice.encode("utf-8")) > 64 for choice in self.allowed_choices):
            raise ValueError("control choice exceeds 64 bytes")
        if len(self.prompt.encode("utf-8")) > 4096:
            raise ValueError("control prompt exceeds 4096 bytes")
        if not self.prompt:
            raise ValueError("control prompt must not be empty")
        for name in ("allowed_principals", "allowed_choices"):
            values = getattr(self, name)
            if not all(isinstance(value, str) and value for value in values):
                raise ValueError(f"{name} must contain non-empty strings")
            if len(values) != len(set(values)):
                raise ValueError(f"{name} must be unique")


@dataclass(frozen=True, slots=True)
class ControlPresentationPolicyV1:
    schema: Literal["hyprial.chat.control-presentation-policy/v1"]
    policy_id: str
    revision: int
    issuer_authority_ref: str
    control_id: str
    allowed_kinds: tuple[ControlKind, ...]
    endpoints: tuple[tuple[str, int], ...]
    allowed_principals: tuple[str, ...]
    audience_digest: str
    max_ref_lifetime_ms: int
    expires_at_ms: int
    state: Literal["active", "revoked"]
    digest: str

    def __post_init__(self) -> None:
        if self.schema != "hyprial.chat.control-presentation-policy/v1":
            raise ValueError("unknown presentation policy schema")
        _tuple_field(self, "allowed_kinds", limit=3)
        _tuple_field(self, "endpoints", limit=256)
        _tuple_field(self, "allowed_principals", limit=256)
        if not self.allowed_kinds or any(
            kind not in ("command", "approval", "question") for kind in self.allowed_kinds
        ):
            raise ValueError("presentation policy has unknown control kinds")
        if self.state not in ("active", "revoked"):
            raise ValueError("unknown presentation policy state")
        if len(self.allowed_kinds) != len(set(self.allowed_kinds)):
            raise ValueError("presentation policy kinds must be unique")
        if len(self.allowed_principals) != len(set(self.allowed_principals)):
            raise ValueError("presentation policy principals must be unique")
        if self.revision < 1 or self.max_ref_lifetime_ms <= 0:
            raise ValueError("invalid presentation policy revision/lifetime")


@dataclass(frozen=True, slots=True)
class ControlResponderAuthorizationBindingV1:
    principal: str
    authorization_id: str
    authorization_generation: int
    authorization_nonce: str
    required_scope_digest: str
    resource_digest: str
    proof_ref: str
    expires_at_ms: int

    def __post_init__(self) -> None:
        for name in (
            "principal",
            "authorization_id",
            "authorization_nonce",
            "required_scope_digest",
            "resource_digest",
            "proof_ref",
        ):
            _required(getattr(self, name), name, max_bytes=4096)
        if self.authorization_generation < 1:
            raise ValueError("authorization_generation must be positive")


@dataclass(frozen=True, slots=True)
class ControlResponderAuthorizationSetV1:
    revision: int
    bindings: tuple[ControlResponderAuthorizationBindingV1, ...]
    digest: str

    def __post_init__(self) -> None:
        _tuple_field(self, "bindings", limit=256)
        if self.revision < 1 or not self.bindings:
            raise ValueError("responder set requires a revision and bindings")
        principals = tuple(binding.principal for binding in self.bindings)
        if principals != tuple(sorted(principals, key=lambda value: value.encode("utf-8"))):
            raise ValueError("responder bindings must be sorted by principal UTF-8 bytes")
        if len(principals) != len(set(principals)):
            raise ValueError("responder principals must be unique")


@dataclass(frozen=True, slots=True)
class ControlCallbackRefV1:
    schema: Literal["hyprial.chat.control-callback-ref/v1"]
    token_id: str
    control_id: str
    presentation_id: str
    instance_id: str
    instance_generation: int
    chat_ref_id: str
    chat_ref_generation: int
    principal_set_digest: str
    choices_digest: str
    presentation_policy_id: str
    presentation_policy_revision: int
    presentation_policy_digest: str
    responder_authorizations: ControlResponderAuthorizationSetV1
    issuer_key_id: str
    issuer_key_generation: int
    issued_at_ms: int
    expires_at_ms: int
    nonce: str
    ref_digest: str
    signature_b64: str

    def __post_init__(self) -> None:
        if self.schema != "hyprial.chat.control-callback-ref/v1":
            raise ValueError("unknown callback ref schema")
        for name in (
            "token_id",
            "control_id",
            "presentation_id",
            "instance_id",
            "chat_ref_id",
            "principal_set_digest",
            "choices_digest",
            "presentation_policy_id",
            "presentation_policy_digest",
            "issuer_key_id",
            "nonce",
            "ref_digest",
            "signature_b64",
        ):
            _required(getattr(self, name), name, max_bytes=4096)
        if min(
            self.instance_generation,
            self.chat_ref_generation,
            self.presentation_policy_revision,
            self.issuer_key_generation,
        ) < 1:
            raise ValueError("callback ref generations must be positive")
        if self.expires_at_ms <= self.issued_at_ms:
            raise ValueError("callback ref must expire after issuance")


@dataclass(frozen=True, slots=True)
class ControlResponseV1:
    schema: Literal["hyprial.chat.control-response/v1"]
    response_id: str
    callback_ref: str
    native_callback_delivery_id: str
    choice: str
    responder_platform_subject: str
    responder_proof_ref: str
    observed_at_ms: int

    def __post_init__(self) -> None:
        if self.schema != "hyprial.chat.control-response/v1":
            raise ValueError("unknown control response schema")
        for name in (
            "response_id",
            "callback_ref",
            "native_callback_delivery_id",
            "choice",
            "responder_platform_subject",
            "responder_proof_ref",
        ):
            _required(getattr(self, name), name, max_bytes=4096)


@dataclass(frozen=True, slots=True)
class ControlSettlementReceiptV1:
    schema: Literal["hyprial.chat.control-settlement/v1"]
    control_id: str
    response_id: str
    decision_generation: int
    choice: str
    responder_principal: str
    owner_operation_id: str
    effect_idempotency_key: str
    effect_state: Literal["accepted", "unknown", "failed-final"]
    settled_at_ms: int | None
    replay: bool
    reason_code: str | None

    def __post_init__(self) -> None:
        if self.schema != "hyprial.chat.control-settlement/v1":
            raise ValueError("unknown control settlement schema")
        if self.decision_generation < 1:
            raise ValueError("decision_generation must be positive")


class AuthorizedByteStream(Protocol):
    name: str | None
    media_type: str | None
    size: int
    sha256: str

    async def read(self, max_bytes: int) -> bytes: ...

    async def aclose(self) -> None: ...


class AttachmentAuthority(Protocol):
    async def open(
        self,
        handle_id: str,
        principal: str,
        purpose: Literal["agent-read", "mirror"],
        target_ref_id: str | None,
        max_bytes: int,
    ) -> AuthorizedByteStream: ...


__all__ = [
    "AttachmentAuthority",
    "AttachmentBlock",
    "AttachmentHandle",
    "AttachmentKind",
    "AuthorizedByteStream",
    "CONTENT_BLOCK_TYPES",
    "ChatContext",
    "ChatMessage",
    "ContentBlock",
    "ControlBlock",
    "ControlCallbackRefV1",
    "ControlIntentV1",
    "ControlKind",
    "ControlPresentationPolicyV1",
    "ControlResponderAuthorizationBindingV1",
    "ControlResponderAuthorizationSetV1",
    "ControlResponseV1",
    "ControlSettlementReceiptV1",
    "MentionBlock",
    "ReplyReference",
    "RichTextBlock",
    "TextBlock",
    "UnsupportedNativeBlock",
]
