"""Authoritative platform provenance codec and separate sanitized-corpus validation."""

from __future__ import annotations

import types
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import fields, is_dataclass
from typing import Literal, Union, get_args, get_origin, get_type_hints

from .values import (
    ADAPTER_VARIANTS_BY_KIND,
    MAX_JSON_ARRAY_ITEMS,
    MAX_JSON_CANONICAL_BYTES,
    MAX_JSON_DEPTH,
    MAX_JSON_KEY_BYTES,
    MAX_JSON_MAP_ITEMS,
    MAX_JSON_STRING_BYTES,
    MAX_PLATFORM_ID_BYTES,
    MAX_PLATFORM_REFS,
    MAX_PLATFORM_UNKNOWN_BYTES,
    MAX_PLATFORM_UNKNOWN_ITEMS,
    PLATFORM_PROVENANCE_ARMS,
    DingTalkProvenanceV1,
    DiscordProvenanceV1,
    FrozenJsonArray,
    FrozenJsonMap,
    FrozenJsonV1,
    IMessageProvenanceV1,
    LarkProvenanceV1,
    MailProvenanceV1,
    MatrixProvenanceV1,
    PlatformProvenanceV1,
    PlatformUnknownV1,
    QQProvenanceV1,
    SlackProvenanceV1,
    TelegramProvenanceV1,
    WeComAppProvenanceV1,
    WeComBotProvenanceV1,
    WeixinProvenanceV1,
    WhatsAppProvenanceV1,
    _secret_paths,
    canonical_json_v1,
    freeze_json_v1,
    frozen_json_digest_v1,
)


_AUTHORITATIVE_KEYS = frozenset({"schemaVersion", "provenance"})
_SANITIZED_KEYS = frozenset(
    {"schemaVersion", "platform", "variant", "conversation", "sender", "native"}
)
_THREAD_KINDS = frozenset(
    {
        ("lark", "bot"),
        ("slack", "socket-mode"),
        ("telegram", "bot"),
        ("discord", "gateway"),
    }
)
_SANITIZED_NATIVE_KEYS = {
    ("lark", "bot"): frozenset({"eventId", "messageId"}),
    ("weixin", "personal"): frozenset({"messageId"}),
    ("dingtalk", "stream"): frozenset({"messageId"}),
    ("wecom", "bot"): frozenset({"messageId"}),
    ("wecom", "application"): frozenset({"messageId"}),
    ("qq", "official-bot"): frozenset({"messageId"}),
    ("slack", "socket-mode"): frozenset({"eventId", "messageId"}),
    ("telegram", "bot"): frozenset({"messageId"}),
    ("discord", "gateway"): frozenset({"messageId"}),
    ("whatsapp", "linked-device"): frozenset({"messageId"}),
    ("imessage", "macos-text-dm"): frozenset({"messageId"}),
    ("matrix", "client"): frozenset({"eventId"}),
    ("mail", "imap-smtp"): frozenset({"uidValidity", "uid", "messageId"}),
}
_ARM_BY_SCHEMA = {
    "hyprial.chat.platform.lark/v1": LarkProvenanceV1,
    "hyprial.chat.platform.weixin/v1": WeixinProvenanceV1,
    "hyprial.chat.platform.dingtalk/v1": DingTalkProvenanceV1,
    "hyprial.chat.platform.wecom-bot/v1": WeComBotProvenanceV1,
    "hyprial.chat.platform.wecom-app/v1": WeComAppProvenanceV1,
    "hyprial.chat.platform.qq/v1": QQProvenanceV1,
    "hyprial.chat.platform.slack/v1": SlackProvenanceV1,
    "hyprial.chat.platform.telegram/v1": TelegramProvenanceV1,
    "hyprial.chat.platform.discord/v1": DiscordProvenanceV1,
    "hyprial.chat.platform.whatsapp/v1": WhatsAppProvenanceV1,
    "hyprial.chat.platform.imessage/v1": IMessageProvenanceV1,
    "hyprial.chat.platform.matrix/v1": MatrixProvenanceV1,
    "hyprial.chat.platform.mail/v1": MailProvenanceV1,
}
_INT64_MIN = -(2**63)
_INT64_MAX = 2**63 - 1


def _error(code: str, secret_fields: Sequence[str] = ()) -> dict[str, object]:
    return {
        "ok": False,
        "code": code,
        "acceptedArms": len(PLATFORM_PROVENANCE_ARMS),
        "secretFields": list(secret_fields),
    }


def _valid_exact_string_map(value: object, keys: frozenset[str]) -> bool:
    if not isinstance(value, Mapping) or set(value) != keys:
        return False
    return all(
        isinstance(item, str)
        and bool(item)
        and len(item.encode("utf-8")) <= MAX_PLATFORM_ID_BYTES
        and unicodedata.normalize("NFC", item) == item
        for item in value.values()
    )


def _validate_canonical_json(value: object) -> None:
    if isinstance(value, str):
        if unicodedata.normalize("NFC", value) != value:
            raise ValueError("wire strings must already be NFC")
        return
    if isinstance(value, Mapping):
        for key, nested in value.items():
            if not isinstance(key, str) or unicodedata.normalize("NFC", key) != key:
                raise ValueError("wire map keys must be canonical strings")
            _validate_canonical_json(nested)
        return
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray, memoryview)):
        for nested in value:
            _validate_canonical_json(nested)


def _thaw(value: FrozenJsonV1) -> object:
    if isinstance(value, FrozenJsonMap):
        return {key: _thaw(nested) for key, nested in value}
    if isinstance(value, FrozenJsonArray):
        return [_thaw(nested) for nested in value]
    return value


def _authoritative_secret_paths(payload: object) -> list[str]:
    if not isinstance(payload, Mapping):
        return []
    unknown = payload.get("unknown")
    if not isinstance(unknown, Mapping):
        return []
    return _secret_paths(unknown.get("fields"))


def validate_sanitized_platform_provenance_v1(value: object) -> dict[str, object]:
    """Validate one non-authoritative corpus row without constructing authority."""

    if not isinstance(value, Mapping):
        return _error("PLATFORM_PROVENANCE_INVALID")
    key = (value.get("platform"), value.get("variant"))
    variants = ADAPTER_VARIANTS_BY_KIND.get(key[0])
    if variants is None or key[1] not in variants:
        return _error("PLATFORM_PROVENANCE_UNKNOWN")
    secret_fields = _secret_paths(value)
    if secret_fields:
        return _error("PLATFORM_PROVENANCE_SECRET_FORBIDDEN", secret_fields)
    if (
        set(value) != _SANITIZED_KEYS
        or type(value.get("schemaVersion")) is not int
        or value.get("schemaVersion") != 1
    ):
        return _error("PLATFORM_PROVENANCE_INVALID")
    conversation = value.get("conversation")
    conversation_keys = (
        frozenset({"kind", "id", "threadId"})
        if key in _THREAD_KINDS and isinstance(conversation, Mapping) and "threadId" in conversation
        else frozenset({"kind", "id"})
    )
    if not _valid_exact_string_map(conversation, conversation_keys):
        return _error("PLATFORM_PROVENANCE_INVALID")
    if not _valid_exact_string_map(value.get("sender"), frozenset({"id"})):
        return _error("PLATFORM_PROVENANCE_INVALID")
    if not _valid_exact_string_map(value.get("native"), _SANITIZED_NATIVE_KEYS[key]):
        return _error("PLATFORM_PROVENANCE_INVALID")
    try:
        _validate_canonical_json(value)
        frozen = freeze_json_v1(value)
        if len(canonical_json_v1(frozen)) > MAX_JSON_CANONICAL_BYTES:
            raise ValueError("sanitized provenance exceeds canonical byte limit")
        result = _thaw(frozen)
        if not isinstance(result, dict):  # pragma: no cover - mapping was checked above
            raise TypeError("sanitized provenance must remain a map")
        return result
    except (TypeError, ValueError):
        return _error("PLATFORM_PROVENANCE_INVALID")


def _encode_value(value: object) -> object:
    if isinstance(value, FrozenJsonMap):
        return {key: _encode_value(nested) for key, nested in value}
    if isinstance(value, FrozenJsonArray):
        return [_encode_value(nested) for nested in value]
    if is_dataclass(value) and not isinstance(value, type):
        return {item.name: _encode_value(getattr(value, item.name)) for item in fields(value)}
    if isinstance(value, tuple):
        return [_encode_value(nested) for nested in value]
    if value is None or type(value) in (bool, int, str):
        return value
    raise TypeError(f"unsupported authoritative provenance field: {type(value).__name__}")


def _exact_text(value: object, *, limit: int = MAX_PLATFORM_ID_BYTES) -> str:
    if type(value) is not str or unicodedata.normalize("NFC", value) != value:
        raise ValueError("provenance text must be an exact NFC string")
    if len(value.encode("utf-8")) > limit:
        raise ValueError("provenance text exceeds field limit")
    return value


def _exact_frozen_json(value: object, *, wire: bool, depth: int = 0) -> object:
    if depth > MAX_JSON_DEPTH:
        raise ValueError("FrozenJsonV1 exceeds depth limit")
    if value is None or type(value) in (bool, int, str):
        if type(value) is int and not _INT64_MIN <= value <= _INT64_MAX:
            raise ValueError("FrozenJsonV1 integer must be signed 64-bit")
        if type(value) is str:
            _exact_text(value, limit=MAX_JSON_STRING_BYTES)
        return value
    if wire and isinstance(value, Mapping):
        if len(value) > MAX_JSON_MAP_ITEMS:
            raise ValueError("FrozenJsonV1 map exceeds entry limit")
        result: dict[str, object] = {}
        for key, nested in value.items():
            key = _exact_text(key, limit=MAX_JSON_KEY_BYTES)
            result[key] = _exact_frozen_json(nested, wire=True, depth=depth + 1)
        return result
    if not wire and type(value) is FrozenJsonMap:
        if len(value) > MAX_JSON_MAP_ITEMS:
            raise ValueError("FrozenJsonV1 map exceeds entry limit")
        result = {}
        previous_key: bytes | None = None
        for pair in value:
            if type(pair) is not tuple or len(pair) != 2:
                raise TypeError("FrozenJsonMap entries must be exact pairs")
            key = _exact_text(pair[0], limit=MAX_JSON_KEY_BYTES)
            encoded_key = key.encode("utf-8")
            if previous_key is not None and encoded_key <= previous_key:
                raise ValueError("FrozenJsonMap keys must be unique and sorted")
            previous_key = encoded_key
            result[key] = _exact_frozen_json(pair[1], wire=False, depth=depth + 1)
        return result
    sequence_type = list if wire else FrozenJsonArray
    if type(value) is sequence_type:
        if len(value) > MAX_JSON_ARRAY_ITEMS:
            raise ValueError("FrozenJsonV1 array exceeds entry limit")
        return [
            _exact_frozen_json(nested, wire=wire, depth=depth + 1) for nested in value
        ]
    raise TypeError("value is not an exact FrozenJsonV1 representation")


def _validate_annotation(annotation: object, value: object, *, wire: bool) -> object:
    origin = get_origin(annotation)
    if origin is Literal:
        if not any(type(value) is type(item) and value == item for item in get_args(annotation)):
            raise ValueError("literal discriminator mismatch")
        return value
    if origin is tuple:
        expected = list if wire else tuple
        if type(value) is not expected:
            raise TypeError("tuple fields must use their exact boundary representation")
        if len(value) > MAX_PLATFORM_REFS:
            raise ValueError("platform tuple field exceeds entry limit")
        arguments = get_args(annotation)
        if len(arguments) != 2 or arguments[1] is not Ellipsis:
            raise TypeError("only homogeneous tuple fields are supported")
        return tuple(
            _validate_annotation(arguments[0], nested, wire=wire) for nested in value
        )
    if origin in (types.UnionType, Union):
        for candidate in get_args(annotation):
            try:
                return _validate_annotation(candidate, value, wire=wire)
            except (TypeError, ValueError):
                continue
        raise TypeError("value does not match its closed union")
    if annotation is type(None):
        if value is not None:
            raise TypeError("value must be null")
        return None
    if annotation is str:
        return _exact_text(value)
    if annotation is int:
        if type(value) is not int or not _INT64_MIN <= value <= _INT64_MAX:
            raise TypeError("integer must be signed 64-bit and not bool")
        return value
    if annotation is bool:
        if type(value) is not bool:
            raise TypeError("bool field must be an exact bool")
        return value
    if isinstance(annotation, type) and is_dataclass(annotation):
        return _validate_dataclass(annotation, value, wire=wire)
    raise TypeError(f"unsupported authoritative provenance annotation: {annotation!r}")


def _validate_dataclass(cls: type[object], value: object, *, wire: bool) -> object:
    definitions = fields(cls)
    if wire:
        if not isinstance(value, Mapping):
            raise TypeError(f"{cls.__name__} must be a map")
        if set(value) != {item.name for item in definitions}:
            raise ValueError(f"{cls.__name__} has missing or extra fields")
    elif type(value) is not cls:
        raise TypeError(f"{cls.__name__} must use its exact typed value")
    hints = get_type_hints(cls)
    validated: dict[str, object] = {}
    for item in definitions:
        raw = value[item.name] if wire else getattr(value, item.name)
        if cls is PlatformUnknownV1 and item.name == "fields":
            thawed = _exact_frozen_json(raw, wire=wire)
            if not isinstance(thawed, dict) or len(thawed) > MAX_PLATFORM_UNKNOWN_ITEMS:
                raise ValueError("platform unknown fields must be a bounded map")
            frozen = freeze_json_v1(thawed)
            if len(canonical_json_v1(frozen)) > MAX_PLATFORM_UNKNOWN_BYTES:
                raise ValueError("platform unknown fields exceed byte limit")
            validated[item.name] = _thaw(frozen)
        else:
            validated[item.name] = _validate_annotation(hints[item.name], raw, wire=wire)
    rebuilt = cls(**validated)
    if not wire and rebuilt != value:
        raise ValueError(f"{cls.__name__} is not an exact canonical typed value")
    return rebuilt if wire else value


def encode_platform_provenance_v1(value: PlatformProvenanceV1) -> dict[str, object]:
    """Encode every field of an existing authoritative provenance arm."""

    if type(value) not in PLATFORM_PROVENANCE_ARMS:
        raise TypeError("platform provenance encoder requires an authoritative typed arm")
    validated = _validate_dataclass(type(value), value, wire=False)
    encoded = {"schemaVersion": 1, "provenance": _encode_value(validated)}
    if _authoritative_secret_paths(encoded["provenance"]):
        raise ValueError("authoritative platform provenance contains secret-shaped fields")
    if len(canonical_json_v1(encoded)) > MAX_JSON_CANONICAL_BYTES:
        raise ValueError("authoritative platform provenance exceeds canonical byte limit")
    return encoded


def decode_platform_provenance_v1(value: object) -> PlatformProvenanceV1 | dict[str, object]:
    """Decode an exact authoritative wire value; never invent authority from corpus rows."""

    if type(value) in PLATFORM_PROVENANCE_ARMS:
        try:
            return _validate_dataclass(type(value), value, wire=False)
        except (TypeError, ValueError):
            return _error("PLATFORM_PROVENANCE_INVALID")
    if not isinstance(value, Mapping):
        return _error("PLATFORM_PROVENANCE_INVALID")
    if "platform" in value:
        key = (value.get("platform"), value.get("variant"))
        variants = ADAPTER_VARIANTS_BY_KIND.get(key[0])
        if variants is None or key[1] not in variants:
            return _error("PLATFORM_PROVENANCE_UNKNOWN")
        return _error("PLATFORM_PROVENANCE_INVALID")
    if (
        set(value) != _AUTHORITATIVE_KEYS
        or type(value.get("schemaVersion")) is not int
        or value.get("schemaVersion") != 1
    ):
        return _error("PLATFORM_PROVENANCE_INVALID")
    payload = value.get("provenance")
    if not isinstance(payload, Mapping):
        return _error("PLATFORM_PROVENANCE_INVALID")
    arm = _ARM_BY_SCHEMA.get(payload.get("schema"))
    if arm is None:
        return _error("PLATFORM_PROVENANCE_UNKNOWN")
    secret_fields = _authoritative_secret_paths(payload)
    if secret_fields:
        return _error("PLATFORM_PROVENANCE_SECRET_FORBIDDEN", secret_fields)
    try:
        _validate_canonical_json(value)
        if len(canonical_json_v1(value)) > MAX_JSON_CANONICAL_BYTES:
            raise ValueError("authoritative provenance exceeds canonical byte limit")
        decoded = _validate_dataclass(arm, payload, wire=True)
        if not isinstance(decoded, PLATFORM_PROVENANCE_ARMS):
            raise TypeError("decoded provenance is outside the closed union")
        if encode_platform_provenance_v1(decoded) != value:
            raise ValueError("authoritative provenance is not canonical")
        return decoded
    except (TypeError, ValueError):
        return _error("PLATFORM_PROVENANCE_INVALID")


def platform_provenance_digest_v1(value: PlatformProvenanceV1) -> str:
    """Digest the complete canonical authoritative wire value."""

    return frozen_json_digest_v1(encode_platform_provenance_v1(value))


__all__ = [
    "decode_platform_provenance_v1",
    "encode_platform_provenance_v1",
    "platform_provenance_digest_v1",
    "validate_sanitized_platform_provenance_v1",
]
