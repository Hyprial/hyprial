"""Pure adapter driver values and runtime/onboarding/media protocols."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, Protocol

from .content import AttachmentHandle, AuthorizedByteStream, ChatMessage, ContentBlock, ReplyReference
from .intents import IntentV1
from .values import (
    AdapterInstanceRef,
    AudienceSnapshot,
    ChatRef,
    FrozenJsonV1,
    NativeAlias,
    SourceProvenance,
    _secret_paths,
    freeze_json_v1,
)

if TYPE_CHECKING:
    from .receipts import OperationNode, PartSubmission


def _tuple_field(instance: object, name: str, *, limit: int = 1024) -> None:
    value = getattr(instance, name)
    if isinstance(value, (str, bytes, bytearray, memoryview)):
        raise TypeError(f"{name} must be a sequence")
    result = tuple(value)
    if len(result) > limit:
        raise ValueError(f"{name} exceeds {limit} entries")
    object.__setattr__(instance, name, result)


def _required(value: str, name: str) -> None:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must not be empty")
    if len(value.encode("utf-8")) > 4096:
        raise ValueError(f"{name} exceeds 4096 bytes")


EchoOperationKind = Literal[
    "text-send",
    "file-upload",
    "file-send",
    "card-create",
    "card-update",
    "reaction",
    "control-presentation",
    "mail-submit",
]
EchoStrategy = Literal[
    "echoed-operation-id", "authoritative-reconcile", "ambiguous-self-quarantine"
]
ECHO_OPERATION_KINDS = (
    "text-send",
    "file-upload",
    "file-send",
    "card-create",
    "card-update",
    "reaction",
    "control-presentation",
    "mail-submit",
)
ECHO_STRATEGIES = (
    "echoed-operation-id",
    "authoritative-reconcile",
    "ambiguous-self-quarantine",
)


@dataclass(frozen=True, slots=True)
class CapabilityFactV1:
    name: str
    support: Literal["native", "emulated", "degraded", "unsupported", "unknown"]
    permission: Literal["granted", "missing", "unknown", "not-applicable"]
    limits: FrozenJsonV1
    reason_code: str | None

    def __post_init__(self) -> None:
        _required(self.name, "name")
        if self.support not in ("native", "emulated", "degraded", "unsupported", "unknown"):
            raise ValueError("unknown capability support")
        if self.permission not in ("granted", "missing", "unknown", "not-applicable"):
            raise ValueError("unknown capability permission")
        object.__setattr__(self, "limits", freeze_json_v1(self.limits))
        if self.support in ("unsupported", "unknown") and self.permission == "granted":
            raise ValueError("unsupported/unknown capability cannot claim granted permission")


@dataclass(frozen=True, slots=True)
class CapabilitySnapshot:
    instance_id: str
    generation: int
    revision: str
    source: str
    observed_at_ms: int
    expires_at_ms: int
    facts: tuple[CapabilityFactV1, ...]

    def __post_init__(self) -> None:
        for name in ("instance_id", "revision", "source"):
            _required(getattr(self, name), name)
        if self.generation < 1 or self.expires_at_ms <= self.observed_at_ms:
            raise ValueError("invalid capability generation/time range")
        _tuple_field(self, "facts", limit=256)
        if not all(isinstance(fact, CapabilityFactV1) for fact in self.facts):
            raise TypeError("capability facts must be typed")
        names = tuple(fact.name for fact in self.facts)
        if names != tuple(sorted(names, key=lambda name: name.encode("utf-8"))):
            raise ValueError("capability facts must be sorted")
        if len(names) != len(set(names)):
            raise ValueError("capability fact names must be unique")


@dataclass(frozen=True, slots=True)
class EchoEvidenceV1:
    operation: EchoOperationKind
    strategy: EchoStrategy
    capability_revision: str
    evidence_revision: str
    source: str
    verified_at_ms: int
    expires_at_ms: int | None
    revoked: bool

    def __post_init__(self) -> None:
        for name in ("capability_revision", "evidence_revision", "source"):
            _required(getattr(self, name), name)
        if self.expires_at_ms is not None and self.expires_at_ms <= self.verified_at_ms:
            raise ValueError("echo evidence must expire after verification")
        if self.operation not in ECHO_OPERATION_KINDS:
            raise ValueError("unknown echo operation")
        if self.strategy not in ECHO_STRATEGIES:
            raise ValueError("unknown echo strategy")


@dataclass(frozen=True, slots=True)
class EchoPolicySnapshotV1:
    instance_id: str
    instance_generation: int
    capability_revision: str
    entries: tuple[EchoEvidenceV1, ...]
    digest: str

    def __post_init__(self) -> None:
        for name in ("instance_id", "capability_revision", "digest"):
            _required(getattr(self, name), name)
        if self.instance_generation < 1:
            raise ValueError("instance_generation must be positive")
        _tuple_field(self, "entries", limit=8)
        if not all(isinstance(entry, EchoEvidenceV1) for entry in self.entries):
            raise TypeError("echo policy entries must be typed")
        if any(entry.capability_revision != self.capability_revision for entry in self.entries):
            raise ValueError("echo evidence capability revision mismatch")
        operations = tuple(entry.operation for entry in self.entries)
        if len(operations) != len(set(operations)):
            raise ValueError("echo policy has duplicate operation entries")


@dataclass(frozen=True, slots=True)
class IngressObservation:
    observation_id: str
    aliases: tuple[NativeAlias, ...]
    source_kind: Literal["live", "recovery", "callback", "poll"]
    raw_safe_ref: str
    observation_digest: str
    received_at_ms: int

    def __post_init__(self) -> None:
        for name in ("observation_id", "raw_safe_ref", "observation_digest"):
            _required(getattr(self, name), name)
        _tuple_field(self, "aliases", limit=128)
        if self.source_kind not in ("live", "recovery", "callback", "poll"):
            raise ValueError("unknown ingress observation source kind")
        if not self.aliases:
            raise ValueError("ingress observation requires a stable alias")


@dataclass(frozen=True, slots=True)
class PlatformEventEnvelope:
    observation: IngressObservation
    payload: FrozenJsonV1

    def __post_init__(self) -> None:
        object.__setattr__(self, "payload", freeze_json_v1(self.payload))


@dataclass(frozen=True, slots=True)
class NativeCheckpoint:
    kind: Literal["callback-ack", "poll-cursor", "stream-sequence", "imap-cursor", "none"]
    token: str | None
    deadline_ms: int | None

    def __post_init__(self) -> None:
        if self.kind not in ("callback-ack", "poll-cursor", "stream-sequence", "imap-cursor", "none"):
            raise ValueError("unknown checkpoint kind")
        if self.kind == "none" and self.token is not None:
            raise ValueError("none checkpoint cannot carry a token")
        if self.kind != "none" and not self.token:
            raise ValueError("checkpoint token is required")


@dataclass(frozen=True, slots=True)
class IngressUnit:
    envelope: PlatformEventEnvelope
    checkpoint: NativeCheckpoint
    hydration_required: bool


@dataclass(frozen=True, slots=True)
class CustodyReceipt:
    journal_id: str
    envelope_digest: str
    committed_at_ms: int

    def __post_init__(self) -> None:
        _required(self.journal_id, "journal_id")
        _required(self.envelope_digest, "envelope_digest")


@dataclass(frozen=True, slots=True)
class HydratedEvent:
    envelope: PlatformEventEnvelope
    body: FrozenJsonV1

    def __post_init__(self) -> None:
        object.__setattr__(self, "body", freeze_json_v1(self.body))


@dataclass(frozen=True, slots=True)
class NormalizedIngress:
    context: object
    provenance: SourceProvenance
    content: tuple[ContentBlock, ...]
    attachments: tuple[AttachmentHandle, ...]
    reply_to: ReplyReference | None

    def __post_init__(self) -> None:
        _tuple_field(self, "content")
        _tuple_field(self, "attachments", limit=128)


@dataclass(frozen=True, slots=True)
class RuntimeSession:
    session_id: str
    instance_id: str
    generation: int
    ingress_mode: Literal["callback", "pull"]

    def __post_init__(self) -> None:
        _required(self.session_id, "session_id")
        _required(self.instance_id, "instance_id")
        if self.generation < 1:
            raise ValueError("generation must be positive")
        if self.ingress_mode not in ("callback", "pull"):
            raise ValueError("unknown ingress mode")


@dataclass(frozen=True, slots=True)
class VerifiedCallbackRequest:
    instance_id: str
    generation: int
    proof_ref: str
    headers: FrozenJsonV1
    body: FrozenJsonV1

    def __post_init__(self) -> None:
        _required(self.instance_id, "instance_id")
        _required(self.proof_ref, "proof_ref")
        if self.generation < 1:
            raise ValueError("generation must be positive")
        object.__setattr__(self, "headers", freeze_json_v1(self.headers))
        object.__setattr__(self, "body", freeze_json_v1(self.body))


@dataclass(frozen=True, slots=True)
class AuthorizedChatTarget:
    target_id: str
    chat_ref: ChatRef
    audience: AudienceSnapshot
    operation: Literal["send", "reply", "mirror", "control-presentation"]
    canonical_parent_id: str | None

    def __post_init__(self) -> None:
        _required(self.target_id, "target_id")
        if not isinstance(self.chat_ref, ChatRef) or not isinstance(self.audience, AudienceSnapshot):
            raise TypeError("authorized target refs must be typed")
        if self.operation not in ("send", "reply", "mirror", "control-presentation"):
            raise ValueError("unknown target operation")


@dataclass(frozen=True, slots=True)
class NativeTargetResolution:
    target: AuthorizedChatTarget
    status: Literal["resolved", "unresolved", "unauthorized", "unsupported"]
    native_chat_id: str | None
    native_thread_id: str | None
    reason_code: str | None

    def __post_init__(self) -> None:
        if self.status not in ("resolved", "unresolved", "unauthorized", "unsupported"):
            raise ValueError("unknown target resolution status")
        if self.status == "resolved" and not self.native_chat_id:
            raise ValueError("resolved target requires native_chat_id")
        if self.status != "resolved" and not self.reason_code:
            raise ValueError("unresolved target requires a reason_code")


@dataclass(frozen=True, slots=True)
class OperationDraftNode:
    local_name: str
    kind: EchoOperationKind
    depends_on: tuple[str, ...]
    template: FrozenJsonV1

    def __post_init__(self) -> None:
        _required(self.local_name, "local_name")
        if self.kind not in ECHO_OPERATION_KINDS:
            raise ValueError("unknown operation draft kind")
        _tuple_field(self, "depends_on", limit=256)
        object.__setattr__(self, "template", freeze_json_v1(self.template))
        if self.local_name in self.depends_on:
            raise ValueError("operation draft node cannot depend on itself")


@dataclass(frozen=True, slots=True)
class OperationDraft:
    renderer_revision: str
    capability_revision: str
    materialized_content_digest: str
    nodes: tuple[OperationDraftNode, ...]

    def __post_init__(self) -> None:
        for name in ("renderer_revision", "capability_revision", "materialized_content_digest"):
            _required(getattr(self, name), name)
        _tuple_field(self, "nodes", limit=256)
        names = tuple(node.local_name for node in self.nodes)
        if not names or len(names) != len(set(names)):
            raise ValueError("operation draft requires uniquely named nodes")
        seen: set[str] = set()
        for node in self.nodes:
            if not set(node.depends_on) <= seen:
                raise ValueError("operation draft nodes must be topologically ordered")
            seen.add(node.local_name)


@dataclass(frozen=True, slots=True)
class NativeSubmissionOutcome:
    submissions: tuple[PartSubmission, ...]
    retry_after_ms: int | None

    def __post_init__(self) -> None:
        _tuple_field(self, "submissions", limit=1024)
        if self.retry_after_ms is not None and self.retry_after_ms < 0:
            raise ValueError("retry_after_ms must not be negative")


@dataclass(frozen=True, slots=True)
class OnboardingPreflight:
    state: Literal["ready", "blocked", "unsupported"]
    reason_codes: tuple[str, ...]

    def __post_init__(self) -> None:
        _tuple_field(self, "reason_codes", limit=64)
        if self.state not in ("ready", "blocked", "unsupported"):
            raise ValueError("unknown onboarding preflight state")
        if self.state == "ready" and self.reason_codes:
            raise ValueError("ready onboarding cannot carry blocking reasons")
        if self.state != "ready" and not self.reason_codes:
            raise ValueError("blocked onboarding requires a reason")


@dataclass(frozen=True, slots=True)
class OnboardingAttempt:
    attempt_id: str
    config: FrozenJsonV1
    credential_ref: str

    def __post_init__(self) -> None:
        _required(self.attempt_id, "attempt_id")
        _required(self.credential_ref, "credential_ref")
        secrets = _secret_paths(self.config)
        if secrets:
            raise ValueError(f"onboarding config contains secret-shaped fields: {secrets}")
        object.__setattr__(self, "config", freeze_json_v1(self.config))


@dataclass(frozen=True, slots=True)
class VerifiedPlatformIdentity:
    account_scope: str
    stable_account_id: str
    proof_ref: str

    def __post_init__(self) -> None:
        for name in ("account_scope", "stable_account_id", "proof_ref"):
            _required(getattr(self, name), name)


@dataclass(frozen=True, slots=True)
class SealedProviderMediaRef:
    instance_id: str
    generation: int
    opaque_ref_digest: str
    authority_handle_id: str

    def __post_init__(self) -> None:
        for name in ("instance_id", "opaque_ref_digest", "authority_handle_id"):
            _required(getattr(self, name), name)
        if self.generation < 1:
            raise ValueError("generation must be positive")


@dataclass(frozen=True, slots=True)
class AdapterHealth:
    state: Literal[
        "configured", "starting", "online", "degraded", "reconnecting", "stopped", "blocked"
    ]
    capabilities_revision: str
    pending_count: int
    unknown_count: int
    reason_codes: tuple[str, ...]

    def __post_init__(self) -> None:
        _required(self.capabilities_revision, "capabilities_revision")
        if self.state not in (
            "configured", "starting", "online", "degraded", "reconnecting", "stopped", "blocked"
        ):
            raise ValueError("unknown adapter health state")
        _tuple_field(self, "reason_codes", limit=64)
        if self.pending_count < 0 or self.unknown_count < 0:
            raise ValueError("health counts must not be negative")


@dataclass(frozen=True, slots=True)
class AdapterCloseOutcome:
    state: Literal["closed", "retained-pending", "unknown"]
    retained_ingress: int
    retained_deliveries: int
    reason_code: str | None

    def __post_init__(self) -> None:
        if self.state not in ("closed", "retained-pending", "unknown"):
            raise ValueError("unknown adapter close state")
        if self.retained_ingress < 0 or self.retained_deliveries < 0:
            raise ValueError("retained counts must not be negative")
        if self.state == "closed" and (self.retained_ingress or self.retained_deliveries):
            raise ValueError("closed outcome cannot retain work")


class IngressSink(Protocol):
    async def stage(self, unit: IngressUnit) -> CustodyReceipt: ...


class AdapterCodec(Protocol):
    def normalize(self, event: HydratedEvent) -> NormalizedIngress: ...

    def render(
        self, target: NativeTargetResolution, message: ChatMessage | IntentV1
    ) -> OperationDraft: ...


class AdapterRuntime(Protocol):
    async def start(self, instance: AdapterInstanceRef, sink: IngressSink) -> RuntimeSession: ...

    async def next_ingress(self) -> IngressUnit: ...

    async def accept_callback(self, request: VerifiedCallbackRequest) -> IngressUnit: ...

    async def commit_checkpoint(
        self, checkpoint: NativeCheckpoint, custody: CustodyReceipt
    ) -> None: ...

    async def hydrate(self, envelope: PlatformEventEnvelope) -> HydratedEvent: ...

    async def history(
        self, after: NativeCheckpoint | None, limit: int
    ) -> tuple[IngressUnit, ...]: ...

    async def resolve(self, target: AuthorizedChatTarget) -> NativeTargetResolution: ...

    async def submit(self, operation: OperationNode) -> NativeSubmissionOutcome: ...

    async def reconcile(
        self, operation: OperationNode, prior: NativeSubmissionOutcome
    ) -> NativeSubmissionOutcome: ...

    async def reconnect(self, reason: str) -> None: ...

    async def health(self) -> AdapterHealth: ...

    async def close(self, deadline_ms: int) -> AdapterCloseOutcome: ...


class AdapterOnboarding(Protocol):
    async def inspect(self, config: FrozenJsonV1) -> OnboardingPreflight: ...

    async def authenticate(self, attempt: OnboardingAttempt) -> VerifiedPlatformIdentity: ...

    async def fingerprint(self, identity: VerifiedPlatformIdentity) -> AdapterInstanceRef: ...

    async def probe_capabilities(
        self, identity: VerifiedPlatformIdentity
    ) -> CapabilitySnapshot: ...


class AdapterMedia(Protocol):
    async def fetch(
        self, source: SealedProviderMediaRef, max_bytes: int
    ) -> AuthorizedByteStream: ...

    async def upload(
        self,
        operation: OperationNode,
        target: NativeTargetResolution,
        stream: AuthorizedByteStream,
    ) -> PartSubmission: ...


__all__ = [
    "AdapterCloseOutcome",
    "AdapterCodec",
    "AdapterHealth",
    "AdapterMedia",
    "AdapterOnboarding",
    "AdapterRuntime",
    "AuthorizedChatTarget",
    "CapabilityFactV1",
    "CapabilitySnapshot",
    "CustodyReceipt",
    "EchoEvidenceV1",
    "ECHO_OPERATION_KINDS",
    "ECHO_STRATEGIES",
    "EchoOperationKind",
    "EchoPolicySnapshotV1",
    "EchoStrategy",
    "HydratedEvent",
    "IngressObservation",
    "IngressSink",
    "IngressUnit",
    "NativeCheckpoint",
    "NativeSubmissionOutcome",
    "NativeTargetResolution",
    "NormalizedIngress",
    "OnboardingAttempt",
    "OnboardingPreflight",
    "OperationDraft",
    "OperationDraftNode",
    "PlatformEventEnvelope",
    "RuntimeSession",
    "SealedProviderMediaRef",
    "VerifiedCallbackRequest",
    "VerifiedPlatformIdentity",
]
