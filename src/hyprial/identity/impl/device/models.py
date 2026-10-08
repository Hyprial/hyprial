"""The local device's public record (tailnet cutover §4.1, W-B).

The sidecar owns the key file; this module owns the *public* projection of
that key material.  ``state/tailcat/device.json`` is the part a daemon may
publish into an organization's directory, and it never contains a private
half or the PSK.  Validation is strict on purpose — the record crosses
machine boundaries (it becomes a ``DirectoryDevice`` document in OrgFS), so
a corrupt or hand-edited file must fail loudly instead of half-loading.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from uuid import UUID

from hyprial.kernel import is_device_id_segment, is_identity_id_segment

__all__ = ["DeviceRecord", "NODEKEY_PREFIX"]

#: Sidecar ``genkey`` prints public keys as ``nodekey:<base32>``; the prefix
#: is the cheapest cross-check that a record carries public halves written
#: from sidecar stdout — not a private key, and not a Tailcat address.
NODEKEY_PREFIX = "nodekey:"

#: The exact camelCase keys of the frozen public wire shape (§4.1).
_RECORD_KEYS = frozenset(
    {"deviceId", "owner", "serverPublic", "clientPublic", "keyGeneration"}
)
_IDENTITY_KEYS = frozenset({"deviceUid", "name"})


@dataclass(frozen=True, slots=True)
class DeviceRecord:
    """This machine's device identity, publishable to an org directory.

    ``device_id`` is the machine's existing node id and ``owner`` is the
    settings owner (the profile issuer's ``preferred_username``); neither value
    is invented here.  ``key_generation`` counts intentional key rotations
    so directory readers can reject stale addresses.
    """

    device_id: str
    owner: str
    server_public: str
    client_public: str
    key_generation: int = 1
    # Added by immutable-id addressing P1. ``None`` is accepted in memory only
    # for a pre-P1 record; the next login's device stage upgrades it in place.
    device_uid: str | None = None
    name: str | None = None

    def __post_init__(self) -> None:
        if (self.device_uid is None) != (self.name is None):
            raise ValueError("deviceUid and name must either both be present or both be absent")
        if self.device_uid is not None:
            _device_uid(self.device_uid)
            _device_name(self.name)

    def as_record(self) -> dict[str, object]:
        """The camelCase JSON projection — the frozen wire shape (§4.1)."""
        return {
            "deviceId": self.device_id,
            "owner": self.owner,
            "serverPublic": self.server_public,
            "clientPublic": self.client_public,
            "keyGeneration": self.key_generation,
        }

    def as_identity_record(self) -> dict[str, object]:
        """The P1-only sibling record, kept out of the rollback-safe wire file."""

        if self.device_uid is None or self.name is None:
            raise ValueError("device identity is not provisioned")
        return {"deviceUid": self.device_uid, "name": self.name}

    def with_identity_record(self, value: Mapping[str, object]) -> "DeviceRecord":
        """Attach the strict sibling identity record to a public record."""

        if not isinstance(value, Mapping):
            raise ValueError(
                f"device identity must be a JSON object, got {type(value).__name__}"
            )
        unknown = sorted(set(value) - _IDENTITY_KEYS)
        if unknown:
            raise ValueError(f"device identity has unknown keys: {unknown}")
        missing = sorted(_IDENTITY_KEYS - set(value))
        if missing:
            raise ValueError(f"device identity is missing keys: {missing}")
        return DeviceRecord(
            device_id=self.device_id,
            owner=self.owner,
            server_public=self.server_public,
            client_public=self.client_public,
            key_generation=self.key_generation,
            device_uid=_device_uid(value["deviceUid"]),
            name=_device_name(value["name"]),
        )

    @classmethod
    def from_record(cls, value: Mapping[str, object]) -> "DeviceRecord":
        """Strict inverse of :meth:`as_record`; bad data raises ``ValueError``.

        Unknown keys are rejected: the record's schema is frozen by the
        cutover contract, and silently ignoring an unknown key would let
        two implementations disagree about what a device is.
        """
        if not isinstance(value, Mapping):
            raise ValueError(
                f"device record must be a JSON object, got {type(value).__name__}"
            )
        unknown = sorted(set(value) - _RECORD_KEYS)
        if unknown:
            raise ValueError(f"device record has unknown keys: {unknown}")
        missing = sorted(_RECORD_KEYS - set(value))
        if missing:
            raise ValueError(f"device record is missing keys: {missing}")
        return cls(
            device_id=_non_empty_string(value["deviceId"], "deviceId"),
            owner=_non_empty_string(value["owner"], "owner"),
            server_public=_nodekey(value["serverPublic"], "serverPublic"),
            client_public=_nodekey(value["clientPublic"], "clientPublic"),
            key_generation=_generation(value["keyGeneration"]),
        )


def _non_empty_string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} must be a non-empty string")
    return value


def _nodekey(value: object, label: str) -> str:
    text = _non_empty_string(value, label)
    if not text.startswith(NODEKEY_PREFIX) or len(text) <= len(NODEKEY_PREFIX):
        raise ValueError(f"{label} must look like '{NODEKEY_PREFIX}…'")
    return text


def _generation(value: object) -> int:
    # ``bool`` is an ``int`` subclass; a JSON ``true`` must not pass as 1.
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError("keyGeneration must be an integer >= 1")
    return value


def _device_uid(value: object) -> str:
    text = _non_empty_string(value, "deviceUid")
    if not is_device_id_segment(f"d.{text}"):
        raise ValueError("deviceUid must be 32 lowercase hexadecimal characters")
    try:
        parsed = UUID(hex=text)
    except ValueError as error:
        raise ValueError("deviceUid must be a UUID4 hexadecimal value") from error
    if parsed.version != 4 or parsed.hex != text:
        raise ValueError("deviceUid must be a UUID4 hexadecimal value")
    return text


def _device_name(value: object) -> str:
    text = _non_empty_string(value, "name")
    if not text.strip() or text != text.strip():
        raise ValueError("name must be non-empty without surrounding whitespace")
    if is_identity_id_segment(text.lower()):
        raise ValueError("name must not have an exact identity id shape")
    return text
