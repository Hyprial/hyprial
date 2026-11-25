"""Immutable mechanism inputs; daemon producers establish their trust.

Trust provenance is supplied by daemon producers; constructing these Python
values does not establish proof or a production trust chain. Isolated fixtures
can supply synthetic provenance, but freshness requires explicit observations.
Production producers must report unavailable when any required proof is missing.
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields
from enum import StrEnum
from typing import Literal, Protocol

from hyprial.kernel import (
    ipc_errors, is_agent_id_segment, is_device_id_segment, is_user_id_segment,
)


class DecodeCode(StrEnum):
    SEGMENT_COUNT = "SEGMENT_COUNT"
    OWNER_ID_INVALID = "OWNER_ID_INVALID"
    HOST_ID_INVALID = "HOST_ID_INVALID"
    AGENT_ID_INVALID = "AGENT_ID_INVALID"
    DUPLICATE_KEY = "DUPLICATE_KEY"
    SCHEMA_VERSION_INVALID = "SCHEMA_VERSION_INVALID"
    PAYLOAD_INVALID = ipc_errors.PAYLOAD_INVALID


class DecodeError(ValueError):
    def __init__(self, code: DecodeCode):
        self.code = code
        super().__init__("Invalid authorization input")


class Reason(StrEnum):
    REQUEST_INVALID = "REQUEST_INVALID"
    SUBJECT_REJECTED = "SUBJECT_REJECTED"
    RESOURCE_REJECTED = "RESOURCE_REJECTED"
    SUBJECT_UNAVAILABLE = "SUBJECT_UNAVAILABLE"
    RESOURCE_UNAVAILABLE = "RESOURCE_UNAVAILABLE"
    REF_MISMATCH = "REF_MISMATCH"
    RULESET_INVALID = "RULESET_INVALID"
    RULESET_UNAVAILABLE = "RULESET_UNAVAILABLE"
    RULESET_STALE = "RULESET_STALE"
    NO_AUTHORIZATION = "NO_AUTHORIZATION"
    MATCH = "MATCH"
    NO_MATCH = "NO_MATCH"


def _require_immutable(value: object) -> None:
    if value is None or type(value) in (str, int, float, bool) or isinstance(value, StrEnum):
        return
    if type(value) is tuple:
        for item in value:
            _require_immutable(item)
        return
    if isinstance(value, _FrozenValue):
        for item in fields(value):
            _require_immutable(getattr(value, item.name))
        return
    raise TypeError("Authorization values must have immutable leaves")


class _FrozenValue:
    __slots__ = ()

    def __post_init__(self) -> None:
        for item in fields(self):
            _require_immutable(getattr(self, item.name))


def _validate_segments(owner: str, host: str, agent: str, *, pattern: bool) -> None:
    for value, predicate, code in (
        (owner, is_user_id_segment, DecodeCode.OWNER_ID_INVALID),
        (host, is_device_id_segment, DecodeCode.HOST_ID_INVALID),
        (agent, is_agent_id_segment, DecodeCode.AGENT_ID_INVALID),
    ):
        if type(value) is not str or not ((pattern and value == "*") or predicate(value)):
            raise DecodeError(code)


@dataclass(frozen=True, slots=True)
class SubjectPattern(_FrozenValue):
    owner: str
    host: str
    agent: str

    def __post_init__(self) -> None:
        _validate_segments(self.owner, self.host, self.agent, pattern=True)

    @property
    def canonical(self) -> str:
        return f"{self.owner}:{self.host}:{self.agent}"


@dataclass(frozen=True, slots=True)
class Subject(_FrozenValue):
    owner: str
    host: str
    agent: str

    def __post_init__(self) -> None:
        _validate_segments(self.owner, self.host, self.agent, pattern=False)

    @property
    def canonical(self) -> str:
        return f"{self.owner}:{self.host}:{self.agent}"


@dataclass(frozen=True, slots=True)
class Rule(_FrozenValue):
    resource: str
    action: str
    allow: tuple[SubjectPattern, ...]

    def __post_init__(self) -> None:
        _FrozenValue.__post_init__(self)
        if type(self.resource) is not str or type(self.action) is not str:
            raise TypeError("Rule resource and action must be strings")
        if type(self.allow) is not tuple or any(type(item) is not SubjectPattern for item in self.allow):
            raise TypeError("Rule allow must be a tuple of patterns")


@dataclass(frozen=True, slots=True)
class RuleSetRef(_FrozenValue):
    scope: str
    declaration_id: str
    version: str
    digest: str
    schema_version: int
    subject_profile: str

    def __post_init__(self) -> None:
        _FrozenValue.__post_init__(self)
        for name in ("scope", "declaration_id", "version", "digest", "subject_profile"):
            if type(getattr(self, name)) is not str:
                raise TypeError("Rule reference fields must be strings")
        if type(self.schema_version) is not int:
            raise TypeError("Rule reference schema version must be an integer")


@dataclass(frozen=True, slots=True)
class OwnerAuthorization(_FrozenValue):
    """Verified owner proof, separate from the host's residency assertion."""

    declaration_ref: str
    trusted_key_source: str
    verification_provenance: str
    owner: str
    host: str
    agent: str
    valid_from: float
    valid_until: float


@dataclass(frozen=True, slots=True)
class SubjectObservation(_FrozenValue):
    state: Literal["verified", "rejected", "unavailable"]
    subject: Subject | None
    epoch: int
    connection_host: str | None = None
    attribution_host: str | None = None
    tailnet_scope: str | None = None
    connection_epoch: int | None = None
    attribution_epoch: int | None = None
    owner_authorization: OwnerAuthorization | None = None
    host_residency_ref: str | None = None
    verification_provenance: str | None = None
    observed_at: float | None = None
    now: float | None = None
    max_observation_age: float | None = None
    valid_until: float | None = None


@dataclass(frozen=True, slots=True)
class ResourceObservation(_FrozenValue):
    state: Literal["verified", "rejected", "unavailable"]
    resource: str | None
    mapping_revision: str | None
    scope: str | None
    authority_ref: RuleSetRef | None
    verification_provenance: str | None = None
    observed_at: float | None = None
    now: float | None = None
    max_observation_age: float | None = None
    valid_until: float | None = None


@dataclass(frozen=True, slots=True)
class RuleSetObservation(_FrozenValue):
    state: Literal["active", "absent", "invalid", "unavailable", "stale"]
    ref: RuleSetRef | None
    rules: tuple[Rule, ...]
    epoch: int
    observed_at: float
    now: float
    max_observation_age: float
    watch_continuous: bool
    epoch_current: bool
    verification_provenance: str | None = None
    valid_until: float | None = None


@dataclass(frozen=True, slots=True)
class Decision(_FrozenValue):
    outcome: Literal["allow", "deny", "unknown"]
    reason: Reason = field(repr=False)
    ruleset_ref: RuleSetRef | None
    epoch: int

    def __bool__(self) -> bool:
        raise TypeError("Compare the authorization outcome explicitly")


class EvaluatorPort(Protocol):
    def evaluate(
        self, *, subject: SubjectObservation, resource: ResourceObservation,
        action: str, ruleset_ref: RuleSetRef | None, rules: RuleSetObservation,
    ) -> Decision: ...
