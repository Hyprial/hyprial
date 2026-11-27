# Decision notes (service grant S3, 2026-11-26):
# - Document schema is closed at every object level (top-level, service,
#   structured recipient) per plan-v2 section 3.1.
# - Owners inside GrantRecipient and StoredServiceGrant are always bare values
#   (never "user:<owner>") so they match the OrgFS path segment and S2's
#   service_grant_header contract directly. Boundary helpers normalize incoming
#   "user:<owner>" values at entry.
# - Grant ids use "sg." + 26 lowercase RFC 4648 base32 characters (130 random
#   bits), matching S2's sealed-envelope grantId validator.
# - S3 fix (2026-11-26, critic B1, S1, S6, N4, N5): owner and device identifiers
#   follow S2's identifier rule (at most 256 characters, no leading "@", no
#   surrogates) on top of the path-segment checks, because identity does not
#   export that rule. service.name reuses service-connect's service-name pattern
#   by import, not by copy. service.note is at most 512 characters with no
#   control or surrogate characters. The reader applies the same rules.
# - credential is validated against the S2 envelope's fixed key sets and scalar
#   types and copied by that shape, never with deepcopy, so a hostile nesting
#   depth cannot raise RecursionError. Values (base64, key ids) are the
#   opener's job.
# - A revoked tombstone may carry null service, recipient and grantedAtMs when
#   the original document could not be parsed; such a document is never
#   effective, never carries a credential, and is listed as revoked.
# - updatedAtMs (integration fix, 2026-11-27) is null until the grantor
#   rewrites an effective grant in place (a renewal or a new Key), and then
#   the time of the latest rewrite; grantedAtMs keeps its first value. It is
#   always written, and a document without it reads as null, so documents
#   written before the field existed still parse. Every other key stays
#   closed. The schema version stays 1 because the field is optional.
"""Service-grant domain models, id generation, and boundary normalization."""

from __future__ import annotations

import base64
import re
import secrets
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from hyprial.daemon.impl.service_connect.models import SERVICE_NAME_PATTERN
from hyprial.identity import directory_owner_principal, org_space_name
from hyprial.kernel import parse_user_uri

SCHEMA_VERSION = 1
ALL_MEMBERS = "@members"
GRANT_PROTOCOLS = frozenset({"tcp", "http"})
GRANT_ID_PATTERN = re.compile(r"sg\.[a-z2-7]{26}\Z")

_TOP_LEVEL_KEYS = frozenset(
    {
        "schemaVersion",
        "grantId",
        "service",
        "recipient",
        "grantedAtMs",
        "expiresAtMs",
        "revokedAtMs",
        "credential",
    }
)
_OPTIONAL_KEYS = frozenset({"updatedAtMs"})
_SERVICE_KEYS = frozenset({"deviceId", "port", "name", "protocol", "note"})
_RECIPIENT_KEYS = frozenset({"owner", "deviceId"})
NOTE_MAX_CHARS = 512
IDENTIFIER_MAX_CHARS = 256
_ENVELOPE_HEADER_TYPES: dict[str, type] = {
    "v": int,
    "alg": str,
    "wrap": str,
    "org": str,
    "grantId": str,
    "service": dict,
    "recipient": dict,
    "encryptionKeyId": str,
}
_ENVELOPE_SERVICE_TYPES: dict[str, type] = {"owner": str, "deviceId": str, "port": int}
_ENVELOPE_RECIPIENT_TYPES: dict[str, type] = {"owner": str, "deviceId": str}
_ENVELOPE_WRAP_TYPES: dict[str, type] = {
    "recipientKeyId": str,
    "epk": str,
    "nonce": str,
    "wrappedDek": str,
}
_ENVELOPE_PAYLOAD_TYPES: dict[str, type] = {"nonce": str, "ciphertext": str}


def _safe_segment(value: object, label: str) -> str:
    if type(value) is not str or not value or value.strip() != value:
        raise ValueError(f"{label} must be a non-empty trimmed string")
    if (
        len(value) > IDENTIFIER_MAX_CHARS
        or value in {".", ".."}
        or value.startswith("@")
        or any(ch in value for ch in ("/", "\\", ":"))
    ):
        raise ValueError(f"{label} is not a safe identifier")
    if any(ord(ch) < 32 or ord(ch) == 127 or 0xD800 <= ord(ch) <= 0xDFFF for ch in value):
        raise ValueError(f"{label} contains control or surrogate characters")
    return value


def _service_name(value: object) -> str | None:
    if value is not None and (type(value) is not str or SERVICE_NAME_PATTERN.fullmatch(value) is None):
        raise ValueError("service.name must be null or match the service-connect service name rule")
    return value


def _note(value: object) -> str | None:
    if value is None:
        return None
    if type(value) is not str or len(value) > NOTE_MAX_CHARS:
        raise ValueError(f"service.note must be null or a string of at most {NOTE_MAX_CHARS} characters")
    if any(unicodedata.category(ch) in {"Cc", "Cs"} for ch in value):
        raise ValueError("service.note contains control or surrogate characters")
    return value


def _typed_object(value: object, types: Mapping[str, type], label: str) -> dict[str, Any]:
    if type(value) is not dict or value.keys() != types.keys():
        raise ValueError(f"{label} keys do not match the sealed envelope")
    for key, expected in types.items():
        if type(value[key]) is not expected:
            raise ValueError(f"{label}.{key} has the wrong type")
    return dict(value)


def sealed_credential(value: object) -> dict[str, Any]:
    """Validate the S2 envelope shape and return a fresh copy built from that shape."""

    if type(value) is not dict or value.keys() != {"header", "recipients", "payload"}:
        raise ValueError("credential keys do not match the sealed envelope")
    header = _typed_object(value["header"], _ENVELOPE_HEADER_TYPES, "credential.header")
    header["service"] = _typed_object(header["service"], _ENVELOPE_SERVICE_TYPES, "credential.header.service")
    header["recipient"] = _typed_object(
        header["recipient"], _ENVELOPE_RECIPIENT_TYPES, "credential.header.recipient"
    )
    wraps = value["recipients"]
    if type(wraps) is not list or len(wraps) != 1:
        raise ValueError("credential.recipients must hold exactly one entry")
    return {
        "header": header,
        "recipients": [_typed_object(wraps[0], _ENVELOPE_WRAP_TYPES, "credential.recipients[0]")],
        "payload": _typed_object(value["payload"], _ENVELOPE_PAYLOAD_TYPES, "credential.payload"),
    }


def normalize_bare_owner(owner: str) -> str:
    """Normalize an owner string (bare or ``user:<owner>``) to a safe bare value."""

    if not isinstance(owner, str) or not owner:
        raise ValueError("owner must be a non-empty string")
    principal = directory_owner_principal(owner)
    bare = parse_user_uri(principal)
    if bare is None:
        raise ValueError("owner is not a valid user principal")
    return _safe_segment(bare, "owner")


def require_bare_owner(owner: object, label: str = "owner") -> str:
    """Validate an already-bare owner string (rejecting ``user:`` prefixes)."""

    return _safe_segment(owner, label)


def validate_grant_id(grant_id: object) -> str:
    """Validate one ``sg.<26 base32>`` grant identifier."""

    if not isinstance(grant_id, str) or GRANT_ID_PATTERN.fullmatch(grant_id) is None:
        raise ValueError("grantId must match sg.<26 lowercase base32 chars>")
    return grant_id


def generate_grant_id() -> str:
    """Mint one random ``sg.<26 base32>`` grant identifier."""

    token = base64.b32encode(secrets.token_bytes(17)).decode("ascii").lower()[:26]
    return f"sg.{token}"


def _non_negative_int(value: object, label: str) -> int:
    if type(value) is not int or value < 0:
        raise ValueError(f"{label} must be a non-negative integer")
    return value


def _optional_non_negative_int(value: object, label: str) -> int | None:
    if value is None:
        return None
    return _non_negative_int(value, label)


def _port(value: object) -> int:
    if type(value) is not int or not (1 <= value <= 65535):
        raise ValueError("service port must be an integer in 1..65535")
    return value


@dataclass(frozen=True, slots=True)
class ServiceEndpoint:
    """The grantor-side service endpoint targeted by a service grant."""

    device_id: str
    port: int
    name: str | None = None
    protocol: str = "tcp"
    note: str | None = None

    def __post_init__(self) -> None:
        _safe_segment(self.device_id, "service.deviceId")
        _port(self.port)
        _service_name(self.name)
        if not isinstance(self.protocol, str) or self.protocol not in GRANT_PROTOCOLS:
            raise ValueError("service.protocol must be 'tcp' or 'http'")
        _note(self.note)

    def as_record(self) -> dict[str, Any]:
        return {
            "deviceId": self.device_id,
            "port": self.port,
            "name": self.name,
            "protocol": self.protocol,
            "note": self.note,
        }

    @classmethod
    def from_record(cls, value: object) -> "ServiceEndpoint":
        if not isinstance(value, Mapping):
            raise ValueError("service must be a JSON object")
        keys = set(value)
        if keys != _SERVICE_KEYS:
            raise ValueError("service keys do not match schema")
        return cls(
            device_id=value["deviceId"],
            port=value["port"],
            name=value["name"],
            protocol=value["protocol"],
            note=value["note"],
        )


@dataclass(frozen=True, slots=True)
class GrantRecipient:
    """One specific member device authorized by a service grant."""

    owner: str
    device_id: str

    def __post_init__(self) -> None:
        require_bare_owner(self.owner, "recipient.owner")
        _safe_segment(self.device_id, "recipient.deviceId")

    def as_record(self) -> dict[str, str]:
        return {"owner": self.owner, "deviceId": self.device_id}

    @classmethod
    def from_record(cls, value: object) -> "GrantRecipient":
        if not isinstance(value, Mapping):
            raise ValueError("recipient must be '@members' or a JSON object")
        if set(value) != _RECIPIENT_KEYS:
            raise ValueError("recipient keys do not match schema")
        return cls(owner=value["owner"], device_id=value["deviceId"])


def parse_device_target(device: object) -> GrantRecipient | str:
    """Parse ``@members`` or ``<owner>/<deviceId>`` into a canonical target."""

    if not isinstance(device, str) or not device:
        raise ValueError("device must be '@members' or '<owner>/<deviceId>'")
    if device == ALL_MEMBERS:
        return ALL_MEMBERS
    owner_part, sep, device_id = device.partition("/")
    if not sep or "/" in device_id:
        raise ValueError("device must be '@members' or '<owner>/<deviceId>'")
    return GrantRecipient(
        owner=normalize_bare_owner(owner_part),
        device_id=_safe_segment(device_id, "recipient.deviceId"),
    )


@dataclass(frozen=True, slots=True)
class ServiceGrant:
    """One owner-authored service grant document (schemaVersion 1).

    ``service``, ``recipient`` and ``granted_at_ms`` are ``None`` only in a
    revoked tombstone written for a document that could no longer be parsed.
    """

    schema_version: int
    grant_id: str
    service: ServiceEndpoint | None
    recipient: GrantRecipient | str | None
    granted_at_ms: int | None
    expires_at_ms: int | None = None
    revoked_at_ms: int | None = None
    credential: Mapping[str, Any] | None = None
    updated_at_ms: int | None = None

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != SCHEMA_VERSION:
            raise ValueError("unsupported service grant schemaVersion")
        validate_grant_id(self.grant_id)
        if None in (self.service, self.recipient, self.granted_at_ms) and (
            self.revoked_at_ms is None or self.credential is not None
        ):
            raise ValueError("only a revoked grant without credential may omit service, recipient or grantedAtMs")
        if self.service is not None and not isinstance(self.service, ServiceEndpoint):
            raise ValueError("service must be a ServiceEndpoint")
        if not (
            self.recipient is None
            or isinstance(self.recipient, GrantRecipient)
            or self.recipient == ALL_MEMBERS
        ):
            raise ValueError("recipient must be GrantRecipient or '@members'")
        _optional_non_negative_int(self.granted_at_ms, "grantedAtMs")
        _optional_non_negative_int(self.expires_at_ms, "expiresAtMs")
        _optional_non_negative_int(self.revoked_at_ms, "revokedAtMs")
        _optional_non_negative_int(self.updated_at_ms, "updatedAtMs")
        if self.credential is not None:
            if not isinstance(self.credential, Mapping):
                raise ValueError("credential must be null or a JSON object")
            if self.recipient == ALL_MEMBERS:
                raise ValueError("credential is forbidden when recipient is '@members'")

    def as_record(self) -> dict[str, Any]:
        return {
            "schemaVersion": self.schema_version,
            "grantId": self.grant_id,
            "service": None if self.service is None else self.service.as_record(),
            "recipient": _recipient_record(self.recipient),
            "grantedAtMs": self.granted_at_ms,
            "expiresAtMs": self.expires_at_ms,
            "revokedAtMs": self.revoked_at_ms,
            "updatedAtMs": self.updated_at_ms,
            "credential": (
                None if self.credential is None else sealed_credential(self.credential)
            ),
        }

    @classmethod
    def from_record(cls, value: object) -> "ServiceGrant":
        if not isinstance(value, Mapping):
            raise ValueError("service grant document must be a JSON object")
        if not _TOP_LEVEL_KEYS <= set(value) <= _TOP_LEVEL_KEYS | _OPTIONAL_KEYS:
            raise ValueError("service grant document keys do not match schema")
        raw_cred = value["credential"]
        return cls(
            schema_version=value["schemaVersion"],
            grant_id=value["grantId"],
            service=None if value["service"] is None else ServiceEndpoint.from_record(value["service"]),
            recipient=_recipient_from_record(value["recipient"]),
            granted_at_ms=value["grantedAtMs"],
            expires_at_ms=value["expiresAtMs"],
            revoked_at_ms=value["revokedAtMs"],
            credential=None if raw_cred is None else sealed_credential(raw_cred),
            updated_at_ms=value.get("updatedAtMs"),
        )

    @classmethod
    def tombstone(cls, grant_id: str, raw: object, *, revoked_at_ms: int) -> "ServiceGrant":
        """Build a minimal revoked document, keeping each field of ``raw`` that still validates."""

        fields = raw if isinstance(raw, Mapping) else {}
        return cls(
            schema_version=SCHEMA_VERSION,
            grant_id=grant_id,
            service=_salvage(ServiceEndpoint.from_record, fields.get("service")),
            recipient=_salvage(_recipient_from_record, fields.get("recipient")),
            granted_at_ms=_salvage(_non_negative_int, fields.get("grantedAtMs"), "grantedAtMs"),
            expires_at_ms=_salvage(_non_negative_int, fields.get("expiresAtMs"), "expiresAtMs"),
            revoked_at_ms=revoked_at_ms,
            credential=None,
            updated_at_ms=_salvage(_non_negative_int, fields.get("updatedAtMs"), "updatedAtMs"),
        )


def _salvage(parse: Any, value: object, *args: object) -> Any:
    if value is None:
        return None
    try:
        return parse(value, *args)
    except (ValueError, TypeError, KeyError, RecursionError):
        return None


def _recipient_from_record(value: object) -> GrantRecipient | str | None:
    if value is None:
        return None
    if value == ALL_MEMBERS:
        return ALL_MEMBERS
    return GrantRecipient.from_record(value)


def _recipient_record(recipient: GrantRecipient | str | None) -> dict[str, str] | str | None:
    if recipient is None or isinstance(recipient, str):
        return recipient
    return recipient.as_record()


@dataclass(frozen=True, slots=True)
class StoredServiceGrant:
    """One verified grant bound to its org and OrgFS path-derived grantor."""

    org: str
    grantor: str
    grant: ServiceGrant

    def __post_init__(self) -> None:
        org_space_name(self.org)
        require_bare_owner(self.grantor, "grantor")
        if not isinstance(self.grant, ServiceGrant):
            raise ValueError("grant must be a ServiceGrant")


def grant_view(stored: StoredServiceGrant, *, now_ms: int, effective: bool) -> dict[str, Any]:
    """Serialize one stored grant into its safe non-secret IPC/CLI view."""

    grant = stored.grant
    return {
        "org": stored.org,
        "grantor": stored.grantor,
        "schemaVersion": grant.schema_version,
        "grantId": grant.grant_id,
        "service": None if grant.service is None else grant.service.as_record(),
        "recipient": _recipient_record(grant.recipient),
        "grantedAtMs": grant.granted_at_ms,
        "expiresAtMs": grant.expires_at_ms,
        "revokedAtMs": grant.revoked_at_ms,
        "updatedAtMs": grant.updated_at_ms,
        "hasCredential": grant.credential is not None,
        "effective": effective,
    }


__all__ = [
    "ALL_MEMBERS",
    "GRANT_ID_PATTERN",
    "GRANT_PROTOCOLS",
    "GrantRecipient",
    "IDENTIFIER_MAX_CHARS",
    "NOTE_MAX_CHARS",
    "SCHEMA_VERSION",
    "SERVICE_NAME_PATTERN",
    "ServiceEndpoint",
    "ServiceGrant",
    "StoredServiceGrant",
    "generate_grant_id",
    "grant_view",
    "normalize_bare_owner",
    "parse_device_target",
    "require_bare_owner",
    "sealed_credential",
    "validate_grant_id",
]
