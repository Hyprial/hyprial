# Decision notes (service grant S3, 2026-11-26):
# - Document schema is closed at every object level (top-level, service,
#   structured recipient) per plan-v2 section 3.1.
# - Owners inside GrantRecipient and StoredServiceGrant are always bare values
#   (never "user:<owner>") so they match the OrgFS path segment and S2's
#   service_grant_header contract directly. Boundary helpers normalize incoming
#   "user:<owner>" values at entry.
# - Grant ids use "sg." + 26 lowercase RFC 4648 base32 characters (130 random
#   bits), matching S2's sealed-envelope grantId validator.
"""Service-grant domain models, id generation, and boundary normalization."""

from __future__ import annotations

import base64
import copy
import re
import secrets
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

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
_SERVICE_KEYS = frozenset({"deviceId", "port", "name", "protocol", "note"})
_RECIPIENT_KEYS = frozenset({"owner", "deviceId"})


def _safe_segment(value: object, label: str) -> str:
    if not isinstance(value, str) or not value or value.strip() != value:
        raise ValueError(f"{label} must be a non-empty trimmed string")
    if value in {".", ".."} or any(ch in value for ch in ("/", "\\", ":", "\x00")):
        raise ValueError(f"{label} is not a safe path segment")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
        raise ValueError(f"{label} contains control characters")
    return value


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
        if self.name is not None:
            if not isinstance(self.name, str) or not self.name or self.name.strip() != self.name:
                raise ValueError("service.name must be null or a non-empty trimmed string")
        if not isinstance(self.protocol, str) or self.protocol not in GRANT_PROTOCOLS:
            raise ValueError("service.protocol must be 'tcp' or 'http'")
        if self.note is not None and not isinstance(self.note, str):
            raise ValueError("service.note must be null or a string")

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
    """One owner-authored service grant document (schemaVersion 1)."""

    schema_version: int
    grant_id: str
    service: ServiceEndpoint
    recipient: GrantRecipient | str
    granted_at_ms: int
    expires_at_ms: int | None = None
    revoked_at_ms: int | None = None
    credential: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != SCHEMA_VERSION:
            raise ValueError("unsupported service grant schemaVersion")
        validate_grant_id(self.grant_id)
        if not isinstance(self.service, ServiceEndpoint):
            raise ValueError("service must be a ServiceEndpoint")
        if not (
            isinstance(self.recipient, GrantRecipient)
            or self.recipient == ALL_MEMBERS
        ):
            raise ValueError("recipient must be GrantRecipient or '@members'")
        _non_negative_int(self.granted_at_ms, "grantedAtMs")
        _optional_non_negative_int(self.expires_at_ms, "expiresAtMs")
        _optional_non_negative_int(self.revoked_at_ms, "revokedAtMs")
        if self.credential is not None:
            if not isinstance(self.credential, Mapping):
                raise ValueError("credential must be null or a JSON object")
            if self.recipient == ALL_MEMBERS:
                raise ValueError("credential is forbidden when recipient is '@members'")

    def as_record(self) -> dict[str, Any]:
        return {
            "schemaVersion": self.schema_version,
            "grantId": self.grant_id,
            "service": self.service.as_record(),
            "recipient": (
                ALL_MEMBERS
                if isinstance(self.recipient, str)
                else self.recipient.as_record()
            ),
            "grantedAtMs": self.granted_at_ms,
            "expiresAtMs": self.expires_at_ms,
            "revokedAtMs": self.revoked_at_ms,
            "credential": (
                None if self.credential is None else copy.deepcopy(dict(self.credential))
            ),
        }

    @classmethod
    def from_record(cls, value: object) -> "ServiceGrant":
        if not isinstance(value, Mapping):
            raise ValueError("service grant document must be a JSON object")
        if set(value) != _TOP_LEVEL_KEYS:
            raise ValueError("service grant document keys do not match schema")
        raw_recipient = value["recipient"]
        recipient: GrantRecipient | str = (
            ALL_MEMBERS
            if raw_recipient == ALL_MEMBERS
            else GrantRecipient.from_record(raw_recipient)
        )
        raw_cred = value["credential"]
        credential = (
            None
            if raw_cred is None
            else (
                copy.deepcopy(dict(raw_cred))
                if isinstance(raw_cred, Mapping)
                else _raise_invalid_credential()
            )
        )
        return cls(
            schema_version=value["schemaVersion"],
            grant_id=value["grantId"],
            service=ServiceEndpoint.from_record(value["service"]),
            recipient=recipient,
            granted_at_ms=value["grantedAtMs"],
            expires_at_ms=value["expiresAtMs"],
            revoked_at_ms=value["revokedAtMs"],
            credential=credential,
        )


def _raise_invalid_credential() -> Any:
    raise ValueError("credential must be null or a JSON object")


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
        "service": grant.service.as_record(),
        "recipient": (
            ALL_MEMBERS
            if isinstance(grant.recipient, str)
            else grant.recipient.as_record()
        ),
        "grantedAtMs": grant.granted_at_ms,
        "expiresAtMs": grant.expires_at_ms,
        "revokedAtMs": grant.revoked_at_ms,
        "hasCredential": grant.credential is not None,
        "effective": effective,
    }


__all__ = [
    "ALL_MEMBERS",
    "GRANT_ID_PATTERN",
    "GRANT_PROTOCOLS",
    "GrantRecipient",
    "SCHEMA_VERSION",
    "ServiceEndpoint",
    "ServiceGrant",
    "StoredServiceGrant",
    "generate_grant_id",
    "grant_view",
    "normalize_bare_owner",
    "parse_device_target",
    "require_bare_owner",
    "validate_grant_id",
]
