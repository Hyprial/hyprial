"""Immutable value contracts shared by chat adapter implementations.

This module deliberately contains no SDK, storage, credential, or network code.
Provider payloads cross the daemon boundary only after they have been converted
to the bounded :class:`FrozenJsonV1` representation defined here.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, fields
from typing import Literal, TypeAlias, get_args


MAX_JSON_DEPTH = 16
MAX_JSON_MAP_ITEMS = 256
MAX_JSON_ARRAY_ITEMS = 1024
MAX_JSON_KEY_BYTES = 256
MAX_JSON_STRING_BYTES = 32 * 1024
MAX_JSON_CANONICAL_BYTES = 256 * 1024
MAX_PLATFORM_ID_BYTES = 4 * 1024
MAX_PLATFORM_REFS = 128
MAX_PLATFORM_UNKNOWN_ITEMS = 64
MAX_PLATFORM_UNKNOWN_BYTES = 32 * 1024
_INT64_MIN = -(2**63)
_INT64_MAX = 2**63 - 1
_SECRET_KEY = re.compile(
    r"(?:secret|token|password|private.?key|credential|cookie|authorization|webhook.?url)",
    re.IGNORECASE,
)


class FrozenJsonArray(tuple):
    """Tuple marker retaining JSON array identity for canonical encoding."""


class FrozenJsonMap(tuple):
    """Sorted tuple marker retaining JSON object identity for encoding."""


FrozenJsonScalar: TypeAlias = None | bool | int | str
FrozenJsonV1: TypeAlias = (
    FrozenJsonScalar
    | tuple["FrozenJsonV1", ...]
    | tuple[tuple[str, "FrozenJsonV1"], ...]
)


def _utf8_size(value: str) -> int:
    return len(value.encode("utf-8"))


def _nfc(value: str, *, label: str, allow_empty: bool = True, limit: int = MAX_JSON_STRING_BYTES) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{label} must be a string")
    normalized = unicodedata.normalize("NFC", value)
    if not allow_empty and not normalized:
        raise ValueError(f"{label} must not be empty")
    if _utf8_size(normalized) > limit:
        raise ValueError(f"{label} exceeds {limit} UTF-8 bytes")
    return normalized


def _freeze_json(value: object, *, depth: int, active: set[int]) -> FrozenJsonV1:
    if depth > MAX_JSON_DEPTH:
        raise ValueError(f"FrozenJsonV1 depth exceeds {MAX_JSON_DEPTH}")
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        if not _INT64_MIN <= value <= _INT64_MAX:
            raise ValueError("FrozenJsonV1 integer must be signed 64-bit")
        return value
    if isinstance(value, str):
        return _nfc(value, label="FrozenJsonV1 string")
    if isinstance(value, (float, complex)):
        raise TypeError("FrozenJsonV1 rejects floating-point values")
    if isinstance(value, (FrozenJsonMap, FrozenJsonArray)):
        return _freeze_json(_json_ready(value), depth=depth, active=active)

    identity = id(value)
    if identity in active:
        raise ValueError("FrozenJsonV1 rejects cycles")
    active.add(identity)
    try:
        if isinstance(value, Mapping):
            if len(value) > MAX_JSON_MAP_ITEMS:
                raise ValueError(f"FrozenJsonV1 map exceeds {MAX_JSON_MAP_ITEMS} entries")
            pairs: list[tuple[str, FrozenJsonV1]] = []
            normalized_keys: set[str] = set()
            for key, nested in value.items():
                if not isinstance(key, str):
                    raise TypeError("FrozenJsonV1 map keys must be strings")
                normalized = _nfc(value=key, label="FrozenJsonV1 key", limit=MAX_JSON_KEY_BYTES)
                if normalized in normalized_keys:
                    raise ValueError("FrozenJsonV1 map has duplicate NFC keys")
                normalized_keys.add(normalized)
                pairs.append((normalized, _freeze_json(nested, depth=depth + 1, active=active)))
            pairs.sort(key=lambda item: item[0].encode("utf-8"))
            return FrozenJsonMap(pairs)
        if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray, memoryview)):
            if len(value) > MAX_JSON_ARRAY_ITEMS:
                raise ValueError(f"FrozenJsonV1 array exceeds {MAX_JSON_ARRAY_ITEMS} entries")
            return FrozenJsonArray(
                _freeze_json(nested, depth=depth + 1, active=active) for nested in value
            )
    finally:
        active.remove(identity)
    raise TypeError(f"unsupported FrozenJsonV1 value: {type(value).__name__}")


def _json_ready(value: FrozenJsonV1) -> object:
    if isinstance(value, FrozenJsonMap):
        return {key: _json_ready(nested) for key, nested in value}
    if isinstance(value, FrozenJsonArray):
        return [_json_ready(nested) for nested in value]
    if isinstance(value, tuple):
        # Public callers may supply an already-frozen plain tuple. Preserve the
        # documented map representation when it is unambiguous.
        if all(
            isinstance(item, tuple) and len(item) == 2 and isinstance(item[0], str)
            for item in value
        ):
            return {item[0]: _json_ready(item[1]) for item in value}
        return [_json_ready(item) for item in value]
    return value


def canonical_json_v1(value: FrozenJsonV1 | object) -> bytes:
    """Return deterministic UTF-8 JSON bytes for a bounded frozen value."""

    frozen = _freeze_json(value, depth=0, active=set())
    encoded = json.dumps(
        _json_ready(frozen),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=False,
        allow_nan=False,
    ).encode("utf-8")
    if len(encoded) > MAX_JSON_CANONICAL_BYTES:
        raise ValueError(f"FrozenJsonV1 projection exceeds {MAX_JSON_CANONICAL_BYTES} bytes")
    return encoded

def freeze_json_v1(value: object) -> FrozenJsonV1:
    """Deep-copy and freeze JSON-compatible input under the v1 bounds."""

    frozen = _freeze_json(value, depth=0, active=set())
    canonical_json_v1(frozen)
    return frozen


def frozen_json_digest_v1(value: FrozenJsonV1 | object) -> str:
    return hashlib.sha256(b"hyprial-json-v1\0" + canonical_json_v1(value)).hexdigest()


def _tuple(value: Sequence[object], *, label: str, limit: int | None = None) -> tuple[object, ...]:
    if isinstance(value, (str, bytes, bytearray, memoryview)):
        raise TypeError(f"{label} must be a sequence")
    result = tuple(value)
    if limit is not None and len(result) > limit:
        raise ValueError(f"{label} exceeds {limit} entries")
    return result


def _freeze_sequence_field(instance: object, name: str, *, limit: int | None = None) -> None:
    object.__setattr__(instance, name, _tuple(getattr(instance, name), label=name, limit=limit))


def _validate_ids(instance: object, *names: str, optional: tuple[str, ...] = ()) -> None:
    for name in names:
        value = getattr(instance, name)
        if value is None and name in optional:
            continue
        object.__setattr__(
            instance,
            name,
            _nfc(value, label=name, allow_empty=False, limit=MAX_PLATFORM_ID_BYTES),
        )


def _strict_int(value: object, name: str, *, minimum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an integer")
    if minimum is not None and value < minimum:
        raise ValueError(f"{name} must be at least {minimum}")
    return value


def _strict_bool(value: object, name: str) -> bool:
    if not isinstance(value, bool):
        raise TypeError(f"{name} must be bool")
    return value


AdapterKind = Literal[
    "lark", "weixin", "dingtalk", "wecom", "qq", "slack",
    "telegram", "discord", "whatsapp", "imessage", "matrix", "mail",
]
AdapterVariant = Literal[
    "bot", "personal", "stream", "application", "official-bot", "socket-mode",
    "gateway", "linked-device", "macos-text-dm", "client", "imap-smtp",
]
ADAPTER_KINDS: tuple[str, ...] = get_args(AdapterKind)
ADAPTER_VARIANTS_BY_KIND: Mapping[str, tuple[str, ...]] = {
    "lark": ("bot",),
    "weixin": ("personal",),
    "dingtalk": ("stream",),
    "wecom": ("bot", "application"),
    "qq": ("official-bot",),
    "slack": ("socket-mode",),
    "telegram": ("bot",),
    "discord": ("gateway",),
    "whatsapp": ("linked-device",),
    "imessage": ("macos-text-dm",),
    "matrix": ("client",),
    "mail": ("imap-smtp",),
}
ChatKind = Literal["dm", "group", "thread", "mail"]
ResolutionStatus = Literal["verified", "unresolved", "conflict", "unavailable"]
MirrorOrigin = Literal["native-human", "native-control", "bridge-mirror", "agent-output"]
CHAT_KINDS = ("dm", "group", "thread", "mail")
RESOLUTION_STATUSES = ("verified", "unresolved", "conflict", "unavailable")
MIRROR_ORIGINS = ("native-human", "native-control", "bridge-mirror", "agent-output")


@dataclass(frozen=True, slots=True)
class AdapterInstanceRef:
    instance_id: str
    kind: AdapterKind
    variant: AdapterVariant
    local_name: str
    organization_id: str
    external_fingerprint: str
    generation: int

    def __post_init__(self) -> None:
        _validate_ids(
            self,
            "instance_id",
            "local_name",
            "organization_id",
            "external_fingerprint",
        )
        if self.kind not in ADAPTER_KINDS:
            raise ValueError("unknown adapter kind")
        if self.variant not in ADAPTER_VARIANTS_BY_KIND[self.kind]:
            raise ValueError("adapter variant does not belong to kind")
        if self.generation < 1:
            raise ValueError("generation must be positive")


@dataclass(frozen=True, slots=True)
class ChatRef:
    ref_id: str
    instance: AdapterInstanceRef
    account_scope: str
    native_chat_id: str
    kind: ChatKind
    native_thread_id: str | None
    native_root_id: str | None

    def __post_init__(self) -> None:
        _validate_ids(
            self,
            "ref_id",
            "account_scope",
            "native_chat_id",
            "native_thread_id",
            "native_root_id",
            optional=("native_thread_id", "native_root_id"),
        )
        if self.kind not in CHAT_KINDS:
            raise ValueError("unknown chat kind")


@dataclass(frozen=True, slots=True)
class PrincipalRef:
    provider_subject_id: str
    open_id: str | None
    union_id: str | None
    display_name: str | None
    hyprial_principal: str | None
    resolution: ResolutionStatus
    proof_ref: str | None
    is_self: bool | None
    standing: str | None
    source: str | None

    def __post_init__(self) -> None:
        _validate_ids(self, "provider_subject_id")
        for name in (
            "open_id",
            "union_id",
            "display_name",
            "hyprial_principal",
            "proof_ref",
            "standing",
            "source",
        ):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, _nfc(value, label=name, limit=MAX_PLATFORM_ID_BYTES))
        if self.resolution not in RESOLUTION_STATUSES:
            raise ValueError("unknown principal resolution")
        if self.resolution == "verified" and not self.proof_ref:
            raise ValueError("verified principal requires proof_ref")


@dataclass(frozen=True, slots=True)
class AudienceSnapshot:
    kind: Literal["private", "group", "mail-envelope"]
    participants: tuple[PrincipalRef, ...]
    completeness: Literal["complete", "partial", "unknown"]
    correspondence_id: str | None
    policy_version: int

    def __post_init__(self) -> None:
        _freeze_sequence_field(self, "participants")
        if self.kind not in ("private", "group", "mail-envelope"):
            raise ValueError("unknown audience kind")
        if self.completeness not in ("complete", "partial", "unknown"):
            raise ValueError("unknown audience completeness")
        if not all(isinstance(item, PrincipalRef) for item in self.participants):
            raise TypeError("audience participants must be PrincipalRef values")
        subjects = tuple(item.provider_subject_id for item in self.participants)
        if len(subjects) != len(set(subjects)):
            raise ValueError("audience participants must be unique")
        if self.policy_version < 1:
            raise ValueError("policy_version must be positive")


@dataclass(frozen=True, slots=True)
class NativeAlias:
    instance_id: str
    instance_generation: int
    account_scope: str
    provider_resource: str
    semantic_kind: Literal["create", "edit", "reaction", "control"]
    alias_kind: Literal["event", "message", "cursor-item", "manual-release"]
    normalized_id: str
    normalized_revision: str
    platform: str | None = None
    chat_scope: str | None = None

    def __post_init__(self) -> None:
        _validate_ids(
            self,
            "instance_id",
            "account_scope",
            "provider_resource",
            "normalized_id",
            "normalized_revision",
            "platform",
            "chat_scope",
            optional=("platform", "chat_scope"),
        )
        if self.instance_generation < 1:
            raise ValueError("instance_generation must be positive")
        if self.semantic_kind not in ("create", "edit", "reaction", "control"):
            raise ValueError("unknown alias semantic kind")
        if self.alias_kind not in ("event", "message", "cursor-item", "manual-release"):
            raise ValueError("unknown alias kind")


@dataclass(frozen=True, slots=True)
class SourceProvenance:
    ingress_id: str
    observation_ids: tuple[str, ...]
    aliases: tuple[NativeAlias, ...]
    received_at_ms: int
    provider_created_at_ms: int | None
    origin: MirrorOrigin
    native_sender_id: str

    def __post_init__(self) -> None:
        _validate_ids(self, "ingress_id", "native_sender_id")
        _freeze_sequence_field(self, "observation_ids")
        _freeze_sequence_field(self, "aliases", limit=MAX_PLATFORM_REFS)
        if not self.observation_ids or not self.aliases:
            raise ValueError("provenance requires observations and aliases")
        if len(self.observation_ids) != len(set(self.observation_ids)):
            raise ValueError("observation ids must be unique")
        if self.origin not in MIRROR_ORIGINS:
            raise ValueError("unknown mirror origin")
        if self.received_at_ms < 0 or (
            self.provider_created_at_ms is not None and self.provider_created_at_ms < 0
        ):
            raise ValueError("provenance timestamps must not be negative")


@dataclass(frozen=True, slots=True)
class MirrorLineage:
    source_canonical_event_id: str | None
    source_plan_id: str | None
    source_target_id: str | None
    hop: Literal[0, 1]

    def __post_init__(self) -> None:
        if self.hop not in (0, 1):
            raise ValueError("mirror lineage hop must be zero or one")
        if self.hop == 0 and any(
            value is not None
            for value in (self.source_canonical_event_id, self.source_plan_id, self.source_target_id)
        ):
            raise ValueError("hop zero cannot carry mirror lineage")
        if self.hop == 1 and not self.source_canonical_event_id:
            raise ValueError("mirrored lineage requires source event")


@dataclass(frozen=True, slots=True)
class PlatformScopeV1:
    organization_id: str
    instance_id: str
    generation: int
    account_scope: str
    environment: str

    def __post_init__(self) -> None:
        _validate_ids(self, "organization_id", "instance_id", "account_scope", "environment")
        if self.generation < 1:
            raise ValueError("generation must be positive")


@dataclass(frozen=True, slots=True)
class PlatformRefsV1:
    event_id: str | None = None
    message_id: str | None = None
    reply_id: str | None = None
    thread_id: str | None = None
    control_id: str | None = None
    attachment_refs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _freeze_sequence_field(self, "attachment_refs", limit=MAX_PLATFORM_REFS)
        for name in ("event_id", "message_id", "reply_id", "thread_id", "control_id"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(
                    self,
                    name,
                    _nfc(value, label=name, allow_empty=False, limit=MAX_PLATFORM_ID_BYTES),
                )


def _secret_paths(value: object, path: str = "$") -> list[str]:
    found: list[str] = []
    if isinstance(value, Mapping):
        for key, nested in value.items():
            child = f"{path}.{key}"
            if isinstance(key, str) and _SECRET_KEY.search(key):
                found.append(child)
            else:
                found.extend(_secret_paths(nested, child))
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray, memoryview)):
        for index, nested in enumerate(value):
            found.extend(_secret_paths(nested, f"{path}[{index}]"))
    return found


@dataclass(frozen=True, slots=True)
class PlatformUnknownV1:
    fields: FrozenJsonV1
    raw_source_ref: str

    def __post_init__(self) -> None:
        frozen = freeze_json_v1(self.fields)
        if not isinstance(frozen, FrozenJsonMap):
            raise TypeError("platform unknown fields must be a map")
        if len(frozen) > MAX_PLATFORM_UNKNOWN_ITEMS:
            raise ValueError("platform unknown fields exceed entry limit")
        if len(canonical_json_v1(frozen)) > MAX_PLATFORM_UNKNOWN_BYTES:
            raise ValueError("platform unknown fields exceed byte limit")
        secret_paths = _secret_paths(_json_ready(frozen))
        if secret_paths:
            raise ValueError(f"secret-shaped platform fields forbidden: {secret_paths}")
        object.__setattr__(self, "fields", frozen)
        _validate_ids(self, "raw_source_ref")


@dataclass(frozen=True, slots=True)
class MailAddressV1:
    address: str
    display_name: str | None
    principal: PrincipalRef | None

    def __post_init__(self) -> None:
        _validate_ids(self, "address")
        if self.display_name is not None:
            object.__setattr__(self, "display_name", _nfc(self.display_name, label="display_name"))


@dataclass(frozen=True, slots=True)
class MailRecipientV1:
    recipient_id: str
    address: MailAddressV1
    role: Literal["to", "cc", "bcc"]
    address_key: str
    visible_to_agent: bool
    authorization_ref: str | None

    def __post_init__(self) -> None:
        _validate_ids(self, "recipient_id", "address_key")
        if not isinstance(self.address, MailAddressV1):
            raise TypeError("mail recipient address must be typed")
        if self.role not in ("to", "cc", "bcc"):
            raise ValueError("unknown mail recipient role")
        if not isinstance(self.visible_to_agent, bool):
            raise TypeError("visible_to_agent must be bool")


@dataclass(frozen=True, slots=True)
class MailEnvelopeV1:
    schema: Literal["hyprial.chat.platform.mail/v1"]
    mailbox_scope: str
    authenticated_mailbox_id: str
    folder: str
    uidvalidity: int
    uid: int
    message_id: str | None
    in_reply_to: tuple[str, ...]
    references: tuple[str, ...]
    header_from: MailAddressV1
    envelope_sender: MailAddressV1 | None
    reply_to: MailAddressV1 | None
    recipients: tuple[MailRecipientV1, ...]
    bcc_visibility: Literal["none", "self-only", "unknown"]

    def __post_init__(self) -> None:
        if self.schema != "hyprial.chat.platform.mail/v1":
            raise ValueError("unknown mail envelope schema")
        _validate_ids(self, "mailbox_scope", "authenticated_mailbox_id", "folder")
        _strict_int(self.uidvalidity, "uidvalidity", minimum=1)
        _strict_int(self.uid, "uid", minimum=1)
        for name in ("in_reply_to", "references", "recipients"):
            _freeze_sequence_field(self, name, limit=MAX_PLATFORM_REFS)
        if any(not recipient.visible_to_agent for recipient in self.recipients):
            raise ValueError("private Bcc recipients must not enter agent-visible envelopes")
        if self.bcc_visibility not in ("none", "self-only", "unknown"):
            raise ValueError("unknown Bcc visibility")
        recipient_ids = tuple(recipient.recipient_id for recipient in self.recipients)
        address_keys = tuple(recipient.address_key for recipient in self.recipients)
        if len(recipient_ids) != len(set(recipient_ids)) or len(address_keys) != len(set(address_keys)):
            raise ValueError("mail envelope recipients must be unique")


@dataclass(frozen=True, slots=True)
class MailAudienceResolution:
    mode: Literal["reply-sender", "reply-reply-to", "reply-all", "explicit-recipients"]
    source_message_id: str | None
    authorization_revision: str
    recipients: tuple[MailRecipientV1, ...]
    digest: str

    def __post_init__(self) -> None:
        _validate_ids(self, "authorization_revision", "digest")
        _freeze_sequence_field(self, "recipients", limit=MAX_PLATFORM_REFS)
        if not self.recipients:
            raise ValueError("mail audience must not be empty")
        if self.mode not in ("reply-sender", "reply-reply-to", "reply-all", "explicit-recipients"):
            raise ValueError("unknown mail audience mode")
        if not all(isinstance(item, MailRecipientV1) for item in self.recipients):
            raise TypeError("mail audience recipients must be typed")


@dataclass(frozen=True, slots=True)
class LarkMentionV1:
    display_name: str
    open_id: str

    def __post_init__(self) -> None:
        _validate_ids(self, "display_name", "open_id")


class _PlatformArm:
    """Validation shared by all frozen provenance dataclasses."""

    def _validate_common(self, expected_schema: str) -> None:
        if getattr(self, "schema") != expected_schema:
            raise ValueError("platform provenance schema mismatch")
        if not isinstance(getattr(self, "scope"), PlatformScopeV1):
            raise TypeError("platform provenance scope must be typed")
        if not isinstance(getattr(self, "refs"), PlatformRefsV1):
            raise TypeError("platform provenance refs must be typed")
        if not isinstance(getattr(self, "unknown"), PlatformUnknownV1):
            raise TypeError("platform provenance unknown fields must be typed")
        for item in fields(self):
            value = getattr(self, item.name)
            if isinstance(value, list):
                object.__setattr__(self, item.name, tuple(value))

    @property
    def digest(self) -> str:
        from .provenance import platform_provenance_digest_v1

        return platform_provenance_digest_v1(self)  # type: ignore[arg-type]


@dataclass(frozen=True, slots=True)
class LarkProvenanceV1(_PlatformArm):
    schema: Literal["hyprial.chat.platform.lark/v1"]
    provider: Literal["lark"]
    scope: PlatformScopeV1
    refs: PlatformRefsV1
    unknown: PlatformUnknownV1
    event_id: str
    message_id: str
    chat_id: str
    chat_type: str
    root_id: str | None
    thread_id: str | None
    reply_to: str | None
    sender_id: str
    sender_type: str
    sender_open_id: str | None
    sender_union_id: str | None
    resolved_sender: PrincipalRef
    mentions: tuple[LarkMentionV1, ...]
    create_time: str | None
    receive_time_ms: int
    message_type: str
    content_status: str

    def __post_init__(self) -> None:
        self._validate_common("hyprial.chat.platform.lark/v1")
        if self.provider != "lark":
            raise ValueError("Lark provider discriminator mismatch")
        _validate_ids(self, "event_id", "message_id", "chat_id", "sender_id")
        _strict_int(self.receive_time_ms, "receive_time_ms", minimum=0)
        _freeze_sequence_field(self, "mentions", limit=MAX_PLATFORM_REFS)
        if self.refs.event_id not in (None, self.event_id) or self.refs.message_id not in (
            None,
            self.message_id,
        ):
            raise ValueError("Lark shared refs mismatch")


@dataclass(frozen=True, slots=True)
class WeixinProvenanceV1(_PlatformArm):
    schema: Literal["hyprial.chat.platform.weixin/v1"]
    scope: PlatformScopeV1
    refs: PlatformRefsV1
    unknown: PlatformUnknownV1
    ilink_user_id: str
    chat_id: str
    item_type: str
    context_token_ref: str | None
    reply_message_id: str | None
    media_refs: tuple[str, ...]

    def __post_init__(self) -> None:
        self._validate_common("hyprial.chat.platform.weixin/v1")
        _validate_ids(self, "ilink_user_id", "chat_id")
        _freeze_sequence_field(self, "media_refs", limit=MAX_PLATFORM_REFS)
        if not self.refs.message_id:
            raise ValueError("Weixin provenance requires a stable message id")


@dataclass(frozen=True, slots=True)
class DingTalkProvenanceV1(_PlatformArm):
    schema: Literal["hyprial.chat.platform.dingtalk/v1"]
    scope: PlatformScopeV1
    refs: PlatformRefsV1
    unknown: PlatformUnknownV1
    conversation_id: str
    conversation_type: str
    sender_id: str
    session_webhook_ref: str | None
    quote_message_id: str | None
    out_track_id: str | None
    media_refs: tuple[str, ...]

    def __post_init__(self) -> None:
        self._validate_common("hyprial.chat.platform.dingtalk/v1")
        _validate_ids(self, "conversation_id", "sender_id")
        _freeze_sequence_field(self, "media_refs", limit=MAX_PLATFORM_REFS)
        if not (self.refs.event_id or self.refs.message_id):
            raise ValueError("DingTalk provenance requires event or message id")


@dataclass(frozen=True, slots=True)
class WeComBotProvenanceV1(_PlatformArm):
    schema: Literal["hyprial.chat.platform.wecom-bot/v1"]
    scope: PlatformScopeV1
    refs: PlatformRefsV1
    unknown: PlatformUnknownV1
    bot_id: str
    req_id: str
    chat_id: str
    chat_type: str
    sender_id: str
    frame_type: str
    quote_id: str | None
    media_refs: tuple[str, ...]

    def __post_init__(self) -> None:
        self._validate_common("hyprial.chat.platform.wecom-bot/v1")
        _validate_ids(self, "bot_id", "req_id", "chat_id", "sender_id")
        _freeze_sequence_field(self, "media_refs", limit=MAX_PLATFORM_REFS)


@dataclass(frozen=True, slots=True)
class WeComAppProvenanceV1(_PlatformArm):
    schema: Literal["hyprial.chat.platform.wecom-app/v1"]
    scope: PlatformScopeV1
    refs: PlatformRefsV1
    unknown: PlatformUnknownV1
    corp_id: str
    agent_id: str
    from_user: str
    msg_id: str
    msg_type: str
    callback_proof_ref: str
    media_refs: tuple[str, ...]

    def __post_init__(self) -> None:
        self._validate_common("hyprial.chat.platform.wecom-app/v1")
        _validate_ids(self, "corp_id", "agent_id", "from_user", "msg_id", "callback_proof_ref")
        _freeze_sequence_field(self, "media_refs", limit=MAX_PLATFORM_REFS)


@dataclass(frozen=True, slots=True)
class QQProvenanceV1(_PlatformArm):
    schema: Literal["hyprial.chat.platform.qq/v1"]
    scope: PlatformScopeV1
    refs: PlatformRefsV1
    unknown: PlatformUnknownV1
    app_id: str
    scene: Literal["c2c", "group"]
    author_id: str
    group_open_id: str | None
    event_id: str
    message_id: str
    reply_id: str | None
    msg_seq: int | None
    media_refs: tuple[str, ...]

    def __post_init__(self) -> None:
        self._validate_common("hyprial.chat.platform.qq/v1")
        _validate_ids(self, "app_id", "author_id", "event_id", "message_id")
        _freeze_sequence_field(self, "media_refs", limit=MAX_PLATFORM_REFS)
        if self.scene not in ("c2c", "group"):
            raise ValueError("unknown QQ scene")
        if self.scene == "group" and not self.group_open_id:
            raise ValueError("QQ group provenance requires group_open_id")
        if self.msg_seq is not None:
            _strict_int(self.msg_seq, "msg_seq", minimum=0)
        if self.refs.event_id not in (None, self.event_id):
            raise ValueError("QQ shared event ref mismatch")
        if self.refs.message_id not in (None, self.message_id):
            raise ValueError("QQ shared message ref mismatch")


@dataclass(frozen=True, slots=True)
class SlackProvenanceV1(_PlatformArm):
    schema: Literal["hyprial.chat.platform.slack/v1"]
    scope: PlatformScopeV1
    refs: PlatformRefsV1
    unknown: PlatformUnknownV1
    team_id: str
    enterprise_id: str | None
    channel_id: str
    event_id: str
    event_ts: str
    message_ts: str
    thread_ts: str | None
    subtype: str | None
    user_id: str
    file_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        self._validate_common("hyprial.chat.platform.slack/v1")
        _validate_ids(self, "team_id", "channel_id", "event_id", "message_ts", "user_id")
        _freeze_sequence_field(self, "file_ids", limit=MAX_PLATFORM_REFS)


@dataclass(frozen=True, slots=True)
class TelegramProvenanceV1(_PlatformArm):
    schema: Literal["hyprial.chat.platform.telegram/v1"]
    scope: PlatformScopeV1
    refs: PlatformRefsV1
    unknown: PlatformUnknownV1
    bot_id: str
    update_id: int
    chat_id: str
    chat_type: str
    message_id: int | None
    topic_id: int | None
    callback_query_id: str | None
    from_user_id: str
    file_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        self._validate_common("hyprial.chat.platform.telegram/v1")
        _validate_ids(self, "bot_id", "chat_id", "from_user_id")
        _strict_int(self.update_id, "update_id", minimum=0)
        if self.message_id is not None:
            _strict_int(self.message_id, "message_id", minimum=0)
        if self.topic_id is not None:
            _strict_int(self.topic_id, "topic_id", minimum=0)
        _freeze_sequence_field(self, "file_ids", limit=MAX_PLATFORM_REFS)
        if self.message_id is None and self.callback_query_id is None:
            raise ValueError("Telegram provenance requires message or callback")


@dataclass(frozen=True, slots=True)
class DiscordProvenanceV1(_PlatformArm):
    schema: Literal["hyprial.chat.platform.discord/v1"]
    scope: PlatformScopeV1
    refs: PlatformRefsV1
    unknown: PlatformUnknownV1
    application_id: str
    guild_id: str | None
    channel_id: str
    thread_id: str | None
    message_id: str | None
    interaction_id: str | None
    gateway_seq: int | None
    author_id: str
    attachment_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        self._validate_common("hyprial.chat.platform.discord/v1")
        _validate_ids(self, "application_id", "channel_id", "author_id")
        if self.gateway_seq is not None:
            _strict_int(self.gateway_seq, "gateway_seq", minimum=0)
        _freeze_sequence_field(self, "attachment_ids", limit=MAX_PLATFORM_REFS)
        if self.gateway_seq is None or (self.message_id is None and self.interaction_id is None):
            raise ValueError("Discord provenance requires gateway and message/interaction evidence")


@dataclass(frozen=True, slots=True)
class WhatsAppProvenanceV1(_PlatformArm):
    schema: Literal["hyprial.chat.platform.whatsapp/v1"]
    scope: PlatformScopeV1
    refs: PlatformRefsV1
    unknown: PlatformUnknownV1
    account_jid: str
    chat_jid: str
    participant_jid: str | None
    message_id: str
    device_id: str | None
    quoted_message_id: str | None
    wrappers: tuple[str, ...]
    view_once: bool
    is_from_me: bool
    media_refs: tuple[str, ...]

    def __post_init__(self) -> None:
        self._validate_common("hyprial.chat.platform.whatsapp/v1")
        _validate_ids(self, "account_jid", "chat_jid", "message_id")
        _strict_bool(self.view_once, "view_once")
        _strict_bool(self.is_from_me, "is_from_me")
        _freeze_sequence_field(self, "wrappers", limit=MAX_PLATFORM_REFS)
        _freeze_sequence_field(self, "media_refs", limit=MAX_PLATFORM_REFS)


@dataclass(frozen=True, slots=True)
class IMessageProvenanceV1(_PlatformArm):
    schema: Literal["hyprial.chat.platform.imessage/v1"]
    scope: PlatformScopeV1
    refs: PlatformRefsV1
    unknown: PlatformUnknownV1
    account_ref: str
    chat_guid: str
    message_guid: str
    rowid: int
    handle: str
    is_from_me: bool

    def __post_init__(self) -> None:
        self._validate_common("hyprial.chat.platform.imessage/v1")
        _validate_ids(self, "account_ref", "chat_guid", "message_guid", "handle")
        _strict_int(self.rowid, "rowid", minimum=0)
        _strict_bool(self.is_from_me, "is_from_me")
        if self.refs.thread_id or self.refs.control_id or self.refs.attachment_refs:
            raise ValueError("iMessage v1 does not support thread/control/attachments")


@dataclass(frozen=True, slots=True)
class MatrixProvenanceV1(_PlatformArm):
    schema: Literal["hyprial.chat.platform.matrix/v1"]
    scope: PlatformScopeV1
    refs: PlatformRefsV1
    unknown: PlatformUnknownV1
    homeserver: str
    user_id: str
    device_id: str | None
    room_id: str
    event_id: str
    txn_id: str | None
    relation_type: str | None
    relates_to: str | None
    thread_root: str | None
    encrypted: bool
    media_mxc: tuple[str, ...]

    def __post_init__(self) -> None:
        self._validate_common("hyprial.chat.platform.matrix/v1")
        _validate_ids(self, "homeserver", "user_id", "room_id", "event_id")
        _strict_bool(self.encrypted, "encrypted")
        _freeze_sequence_field(self, "media_mxc", limit=MAX_PLATFORM_REFS)


@dataclass(frozen=True, slots=True)
class MailProvenanceV1(_PlatformArm):
    schema: Literal["hyprial.chat.platform.mail/v1"]
    scope: PlatformScopeV1
    refs: PlatformRefsV1
    unknown: PlatformUnknownV1
    envelope: MailEnvelopeV1

    def __post_init__(self) -> None:
        self._validate_common("hyprial.chat.platform.mail/v1")
        if self.envelope.mailbox_scope != self.scope.account_scope:
            raise ValueError("mail envelope and platform scope mismatch")


PlatformProvenanceV1: TypeAlias = (
    LarkProvenanceV1
    | WeixinProvenanceV1
    | DingTalkProvenanceV1
    | WeComBotProvenanceV1
    | WeComAppProvenanceV1
    | QQProvenanceV1
    | SlackProvenanceV1
    | TelegramProvenanceV1
    | DiscordProvenanceV1
    | WhatsAppProvenanceV1
    | IMessageProvenanceV1
    | MatrixProvenanceV1
    | MailProvenanceV1
)
PLATFORM_PROVENANCE_ARMS: tuple[type[object], ...] = get_args(PlatformProvenanceV1)
PLATFORM_PROVENANCE_SCHEMAS: Mapping[str, type[object]] = {
    "lark": LarkProvenanceV1,
    "weixin": WeixinProvenanceV1,
    "dingtalk": DingTalkProvenanceV1,
    "wecom-bot": WeComBotProvenanceV1,
    "wecom-app": WeComAppProvenanceV1,
    "qq": QQProvenanceV1,
    "slack": SlackProvenanceV1,
    "telegram": TelegramProvenanceV1,
    "discord": DiscordProvenanceV1,
    "whatsapp": WhatsAppProvenanceV1,
    "imessage": IMessageProvenanceV1,
    "matrix": MatrixProvenanceV1,
    "mail": MailProvenanceV1,
}


__all__ = [
    "ADAPTER_KINDS",
    "ADAPTER_VARIANTS_BY_KIND",
    "AdapterInstanceRef",
    "AdapterKind",
    "AdapterVariant",
    "AudienceSnapshot",
    "ChatKind",
    "ChatRef",
    "DingTalkProvenanceV1",
    "DiscordProvenanceV1",
    "FrozenJsonArray",
    "FrozenJsonMap",
    "FrozenJsonV1",
    "IMessageProvenanceV1",
    "LarkMentionV1",
    "LarkProvenanceV1",
    "MailAddressV1",
    "MailAudienceResolution",
    "MailEnvelopeV1",
    "MailProvenanceV1",
    "MailRecipientV1",
    "MatrixProvenanceV1",
    "MirrorLineage",
    "NativeAlias",
    "PLATFORM_PROVENANCE_ARMS",
    "PLATFORM_PROVENANCE_SCHEMAS",
    "PlatformProvenanceV1",
    "PlatformRefsV1",
    "PlatformScopeV1",
    "PlatformUnknownV1",
    "PrincipalRef",
    "QQProvenanceV1",
    "ResolutionStatus",
    "SlackProvenanceV1",
    "SourceProvenance",
    "TelegramProvenanceV1",
    "WeComAppProvenanceV1",
    "WeComBotProvenanceV1",
    "WeixinProvenanceV1",
    "WhatsAppProvenanceV1",
    "canonical_json_v1",
    "freeze_json_v1",
    "frozen_json_digest_v1",
]
