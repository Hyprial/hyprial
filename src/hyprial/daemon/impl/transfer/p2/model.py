"""Immutable preparation values, independent of IPC and identity storage.

These are internal contracts. A binding is not proof of operator authority;
the eventual IPC adapter must admit the target-local operator before submission.
"""

from dataclasses import asdict, dataclass
from enum import StrEnum
import json
import re


class Role(StrEnum):
    SOURCE = "source"
    TARGET = "target"


class Reason(StrEnum):
    BINDING_MISMATCH = "binding-mismatch"
    REPLAY_MISMATCH = "replay-mismatch"
    RESERVED = "reserved"
    UNKNOWN = "unknown-operation"
    EXPIRED = "expired"
    REVOKED = "revoked"
    CANCELLED = "cancelled"
    RENEWAL_CONFLICT = "renewal-conflict"
    PHASE_CONFLICT = "phase-conflict"
    CORRUPT_RECORD = "corrupt-record"


class AdmissionConflict(Exception):
    """Expected ledger rejection, not yet a published wire error."""

    def __init__(self, reason: Reason) -> None:
        self.reason = reason
        super().__init__(reason.value)


def identifier(value: str) -> None:
    if not isinstance(value, str) or not value.strip() or len(value) > 256:
        raise ValueError("identifier must contain 1..256 characters")
    if any(ord(char) < 32 for char in value):
        raise ValueError("identifier contains a control character")


@dataclass(frozen=True, slots=True)
class Binding:
    role: Role
    owner: str
    source_node: str
    source_agent_id: str
    source_entity_token: str
    target_node: str
    target_actor: str
    plan_digest: str

    def __post_init__(self) -> None:
        if not isinstance(self.role, Role):
            raise ValueError("role must be source or target")
        for key, value in asdict(self).items():
            if key != "role":
                identifier(value)
        if re.fullmatch(r"[0-9a-f]{64}", self.plan_digest) is None:
            raise ValueError("plan digest must be a SHA256 hex digest")

    def encode(self) -> str:
        return json.dumps({**asdict(self), "_schema_version": 1}, sort_keys=True, separators=(",", ":"))

    @classmethod
    def decode(cls, raw: str) -> "Binding":
        try:
            values = json.loads(raw)
            if not isinstance(values, dict):
                raise ValueError("binding must be an object")
            version = values.pop("_schema_version", 1)  # Original A rows are version 1.
            if type(version) is not int or version != 1:
                raise ValueError("unsupported binding schema")
            values["role"] = Role(values["role"])
            return cls(**values)
        except (TypeError, ValueError, KeyError) as error:
            raise AdmissionConflict(Reason.CORRUPT_RECORD) from error


@dataclass(frozen=True, slots=True)
class Admission:
    operation_id: str
    binding: Binding
    created_at_ms: int
    expires_at_ms: int
    revoked_at_ms: int | None
    cancelled_at_ms: int | None

    def require_live(self, now_ms: int) -> None:
        if self.cancelled_at_ms is not None:
            raise AdmissionConflict(Reason.CANCELLED)
        if self.revoked_at_ms is not None:
            raise AdmissionConflict(Reason.REVOKED)
        if now_ms >= self.expires_at_ms:
            raise AdmissionConflict(Reason.EXPIRED)
