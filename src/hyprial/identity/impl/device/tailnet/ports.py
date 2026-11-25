"""Trusted adapter ports; values are projections, never authentication proofs."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from ..client import DeviceAuthorityConfig


@dataclass(frozen=True, slots=True)
class TailnetAuthorityPin:
    tailnet_id: str
    owner: str
    device_id: str
    master_public: str
    server_public: str
    address: str = field(repr=False)
    control_port: int


@dataclass(frozen=True, slots=True)
class TailnetMember:
    owner: str
    device_id: str
    state: str


@dataclass(frozen=True, slots=True)
class TailnetMembership:
    tailnet_id: str
    role: str
    revision: int
    authority_device_id: str
    member_ready: bool
    lease_remaining_seconds: int | None
    code: str | None
    local_member: TailnetMember | None
    authority_pin: TailnetAuthorityPin


@dataclass(frozen=True, slots=True)
class TailnetAuthorityBinding:
    authority_pin: TailnetAuthorityPin
    config: DeviceAuthorityConfig = field(repr=False)
    policy_issuer: str
    trust_bundle_id: str


@dataclass(frozen=True, slots=True)
class TailnetCredentialAssociation:
    authority_pin: TailnetAuthorityPin
    tailnet_id: str
    device_id: str
    owner: str
    device_uid: str
    binding_id: str
    credential_generation: int
    spki_sha256: str
    owner_user_id: str
    policy_issuer: str
    trust_bundle_id: str
    state: str


class TailnetCredentialContext(Protocol):
    """Producer must authenticate provenance and distinguish absent from failure.

    membership consumes sidecar lease projection without persisting a new lease.
    authority supplies explicitly provisioned HTTPS config bound to the full pin.
    association returns None ONLY for authoritative absence, raises on failure.
    """

    def membership(self, tailnet_id: str) -> TailnetMembership: ...
    def authority(
        self, tailnet_id: str, pin: TailnetAuthorityPin
    ) -> TailnetAuthorityBinding: ...
    def association(
        self, tailnet_id: str, device_id: str, pin: TailnetAuthorityPin
    ) -> TailnetCredentialAssociation | None: ...


class TailnetCredentialError(RuntimeError):
    """Non-secret gate failure; remote_outcome is cached observation only."""

    def __init__(
        self,
        code: str,
        *,
        phase: str,
        remote_outcome: str = "NOT_SENT",
        attempt_outcome: str | None = None,
        transport_code: str | None = None,
        retryable: bool = False,
        http_status: int | None = None,
        operation_id: str | None = None,
        reason_code: str | None = None,
        exception_class: str | None = None,
    ):
        self.code = code
        self.phase = phase
        self.remote_outcome = remote_outcome
        self.attempt_outcome = attempt_outcome
        self.transport_code = transport_code
        self.retryable = retryable
        self.http_status = http_status
        self.operation_id = operation_id
        self.reason_code = reason_code
        self.exception_class = exception_class
        super().__init__(f"{code} ({phase}; {remote_outcome})")
