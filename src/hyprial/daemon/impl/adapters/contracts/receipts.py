"""Frozen operation DAGs and the exact target/delivery receipt algebra."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Literal

from .driver import ECHO_OPERATION_KINDS, ECHO_STRATEGIES, EchoOperationKind, EchoStrategy
from .values import FrozenJsonV1, _secret_paths, freeze_json_v1, frozen_json_digest_v1


MAX_SERVICE_AUTHORIZATION_LEASE_BYTES = 64 * 1024


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


OperationNodeState = Literal[
    "frozen",
    "held-authorization",
    "submitting",
    "accepted",
    "failed-final",
    "failed-retryable",
    "unknown",
    "unsupported",
    "cancelled",
]
OPERATION_NODE_STATES = (
    "frozen",
    "held-authorization",
    "submitting",
    "accepted",
    "failed-final",
    "failed-retryable",
    "unknown",
    "unsupported",
    "cancelled",
)
AuthorizationKind = Literal[
    "agent-operational",
    "organization-ticket",
    "adapter-management",
    "chat-management",
    "service",
    "control-responder",
]
AUTHORIZATION_KINDS = (
    "agent-operational",
    "organization-ticket",
    "adapter-management",
    "chat-management",
    "service",
    "control-responder",
)
AuthorizationAction = Literal[
    "agent.organization.assign",
    "agent.organization.reassign",
    "agent.organization.retire",
    "agent.incarnation.rotate",
    "adapter.instance.advance",
    "adapter.claim.grant",
    "adapter.claim.transfer",
    "adapter.claim.release",
    "adapter.claim.cancel",
    "adapter.binding.bind",
    "adapter.binding.move",
    "adapter.binding.unbind.prepare",
    "adapter.binding.unbind.settle",
    "correspondence.verify",
    "correspondence.revoke",
    "chat.admit",
    "chat.plan",
    "chat.submit.text",
    "chat.submit.file",
    "chat.submit.card",
    "chat.submit.reaction",
    "chat.submit.control",
    "chat.reconcile",
    "control.respond",
    "control.settle",
    "control.cancel",
]
AUTHORIZATION_ACTIONS = (
    "agent.organization.assign",
    "agent.organization.reassign",
    "agent.organization.retire",
    "agent.incarnation.rotate",
    "adapter.instance.advance",
    "adapter.claim.grant",
    "adapter.claim.transfer",
    "adapter.claim.release",
    "adapter.claim.cancel",
    "adapter.binding.bind",
    "adapter.binding.move",
    "adapter.binding.unbind.prepare",
    "adapter.binding.unbind.settle",
    "correspondence.verify",
    "correspondence.revoke",
    "chat.admit",
    "chat.plan",
    "chat.submit.text",
    "chat.submit.file",
    "chat.submit.card",
    "chat.submit.reaction",
    "chat.submit.control",
    "chat.reconcile",
    "control.respond",
    "control.settle",
    "control.cancel",
)


@dataclass(frozen=True, slots=True)
class AgentAuthorizationResourceV1:
    type: Literal["agent"]
    organization_id: str
    actor: str
    entity_token: str
    association_generation: int

    def __post_init__(self) -> None:
        _validate_resource(self, "agent", "actor", "entity_token")
        _positive(self.association_generation, "association_generation")


@dataclass(frozen=True, slots=True)
class InstanceAuthorizationResourceV1:
    type: Literal["instance"]
    organization_id: str
    instance_id: str
    instance_generation: int
    fingerprint: str

    def __post_init__(self) -> None:
        _validate_resource(self, "instance", "instance_id", "fingerprint")
        _positive(self.instance_generation, "instance_generation")


@dataclass(frozen=True, slots=True)
class ChatAuthorizationResourceV1:
    type: Literal["chat"]
    organization_id: str
    channel_id: str
    chat_ref_id: str
    chat_ref_generation: int
    audience_digest: str

    def __post_init__(self) -> None:
        _validate_resource(self, "chat", "channel_id", "chat_ref_id", "audience_digest")
        _positive(self.chat_ref_generation, "chat_ref_generation")


@dataclass(frozen=True, slots=True)
class CorrespondenceAuthorizationResourceV1:
    type: Literal["correspondence"]
    organization_id: str
    correspondence_id: str
    correspondence_version: int

    def __post_init__(self) -> None:
        _validate_resource(self, "correspondence", "correspondence_id")
        _positive(self.correspondence_version, "correspondence_version")


@dataclass(frozen=True, slots=True)
class ControlAuthorizationResourceV1:
    type: Literal["control"]
    organization_id: str
    control_id: str
    presentation_id: str | None

    def __post_init__(self) -> None:
        _validate_resource(self, "control", "control_id")
        if self.presentation_id is not None:
            _required(self.presentation_id, "presentation_id")


AuthorizationResourceV1 = (
    AgentAuthorizationResourceV1
    | InstanceAuthorizationResourceV1
    | ChatAuthorizationResourceV1
    | CorrespondenceAuthorizationResourceV1
    | ControlAuthorizationResourceV1
)
AUTHORIZATION_RESOURCE_TYPES = (
    AgentAuthorizationResourceV1,
    InstanceAuthorizationResourceV1,
    ChatAuthorizationResourceV1,
    CorrespondenceAuthorizationResourceV1,
    ControlAuthorizationResourceV1,
)


def _positive(value: int, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{name} must be a positive integer")


def _validate_resource(value: object, expected_type: str, *names: str) -> None:
    if getattr(value, "type") != expected_type:
        raise ValueError("authorization resource discriminator mismatch")
    _required(getattr(value, "organization_id"), "organization_id")
    for name in names:
        _required(getattr(value, name), name)


def _resource_json(value: AuthorizationResourceV1) -> dict[str, object]:
    if isinstance(value, AgentAuthorizationResourceV1):
        return {"type": value.type, "organizationId": value.organization_id, "actor": value.actor, "entityToken": value.entity_token, "associationGeneration": value.association_generation}
    if isinstance(value, InstanceAuthorizationResourceV1):
        return {"type": value.type, "organizationId": value.organization_id, "instanceId": value.instance_id, "instanceGeneration": value.instance_generation, "fingerprint": value.fingerprint}
    if isinstance(value, ChatAuthorizationResourceV1):
        return {"type": value.type, "organizationId": value.organization_id, "channelId": value.channel_id, "chatRefId": value.chat_ref_id, "chatRefGeneration": value.chat_ref_generation, "audienceDigest": value.audience_digest}
    if isinstance(value, CorrespondenceAuthorizationResourceV1):
        return {"type": value.type, "organizationId": value.organization_id, "correspondenceId": value.correspondence_id, "correspondenceVersion": value.correspondence_version}
    if isinstance(value, ControlAuthorizationResourceV1):
        return {"type": value.type, "organizationId": value.organization_id, "controlId": value.control_id, "presentationId": value.presentation_id}
    raise TypeError("authorization resource must be typed")


@dataclass(frozen=True, slots=True)
class AgentOperationalAuthorizationRefV1:
    schema: Literal["hyprial.identity.agent-operational-authorization-ref/v1"]
    lease_id: str
    lease_generation: int
    organization_id: str
    actor: str
    entity_token: str
    association_generation: int
    use_nonce: str

    def __post_init__(self) -> None:
        if self.schema != "hyprial.identity.agent-operational-authorization-ref/v1":
            raise ValueError("unknown agent operational authorization ref schema")
        for name in ("lease_id", "organization_id", "actor", "entity_token", "use_nonce"):
            _required(getattr(self, name), name)
        _positive(self.lease_generation, "lease_generation")
        _positive(self.association_generation, "association_generation")


@dataclass(frozen=True, slots=True)
class ServiceAuthorizationRefV1:
    schema: Literal["hyprial.identity.service-authorization-ref/v1"]
    lease_id: str
    lease_generation: int
    operation_id: str
    use_owner: Literal[
        "identity-agent-actor", "daemon-chat-outbox", "organization-claim-registrar"
    ]
    use_nonce: str
    lease_digest: str

    def __post_init__(self) -> None:
        if self.schema != "hyprial.identity.service-authorization-ref/v1":
            raise ValueError("unknown service authorization ref schema")
        for name in ("lease_id", "operation_id", "use_nonce", "lease_digest"):
            _required(getattr(self, name), name)
        _positive(self.lease_generation, "lease_generation")
        if self.use_owner not in (
            "identity-agent-actor", "daemon-chat-outbox", "organization-claim-registrar"
        ):
            raise ValueError("unknown service authorization use owner")


@dataclass(frozen=True, slots=True)
class RequiredAuthorizationV1:
    authorization_kind: AuthorizationKind
    expected_subject_principal: str
    action: AuthorizationAction
    resource: AuthorizationResourceV1
    source_record_ref: str
    source_generation: int
    use_nonce: str
    required_bytes_digest: str

    def __post_init__(self) -> None:
        if self.authorization_kind not in AUTHORIZATION_KINDS:
            raise ValueError("unknown required authorization kind")
        for name in (
            "expected_subject_principal",
            "source_record_ref",
            "use_nonce",
            "required_bytes_digest",
        ):
            _required(getattr(self, name), name)
        _positive(self.source_generation, "source_generation")
        if self.action not in AUTHORIZATION_ACTIONS:
            raise ValueError("unknown required authorization action")
        if not isinstance(self.resource, AUTHORIZATION_RESOURCE_TYPES):
            raise TypeError("required authorization resource must be typed")
        expected = authorization_required_digest_v1(
            self.authorization_kind,
            self.expected_subject_principal,
            self.action,
            self.resource,
            self.source_record_ref,
            self.source_generation,
            self.use_nonce,
        )
        if self.required_bytes_digest != expected:
            raise ValueError("required authorization canonical digest mismatch")


def authorization_required_digest_v1(
    authorization_kind: AuthorizationKind,
    expected_subject_principal: str,
    action: AuthorizationAction,
    resource: AuthorizationResourceV1,
    source_record_ref: str,
    source_generation: int,
    use_nonce: str,
) -> str:
    return frozen_json_digest_v1(
        {
            "authorizationKind": authorization_kind,
            "expectedSubjectPrincipal": expected_subject_principal,
            "action": action,
            "resource": _resource_json(resource),
            "sourceRecordRef": source_record_ref,
            "sourceGeneration": source_generation,
            "useNonce": use_nonce,
        }
    )


@dataclass(frozen=True, slots=True)
class OperationNode:
    node_id: str
    kind: EchoOperationKind
    depends_on: tuple[str, ...]
    template: FrozenJsonV1
    template_digest: str
    sealed_request: FrozenJsonV1 | None
    sealed_digest: str | None
    echo_strategy: EchoStrategy
    echo_capability_revision: str
    echo_evidence_revision: str
    agent_authorization_ref: AgentOperationalAuthorizationRefV1
    service_authorization_ref: ServiceAuthorizationRefV1
    required_authorizations: tuple[RequiredAuthorizationV1, ...]
    authorization_use_nonce: str
    idempotency_key: str
    state: OperationNodeState

    def __post_init__(self) -> None:
        for name in (
            "node_id",
            "template_digest",
            "echo_capability_revision",
            "echo_evidence_revision",
            "authorization_use_nonce",
            "idempotency_key",
        ):
            _required(getattr(self, name), name)
        _tuple_field(self, "depends_on", limit=256)
        _tuple_field(self, "required_authorizations", limit=32)
        if self.kind not in ECHO_OPERATION_KINDS:
            raise ValueError("unknown operation kind")
        if self.state not in OPERATION_NODE_STATES:
            raise ValueError("unknown operation node state")
        if self.echo_strategy not in ECHO_STRATEGIES:
            raise ValueError("unknown echo strategy")
        if not isinstance(self.agent_authorization_ref, AgentOperationalAuthorizationRefV1):
            raise TypeError("agent authorization ref must be typed and immutable")
        if not isinstance(self.service_authorization_ref, ServiceAuthorizationRefV1):
            raise TypeError("service authorization ref must be typed and immutable")
        if not all(isinstance(ref, RequiredAuthorizationV1) for ref in self.required_authorizations):
            raise TypeError("required authorization refs must be typed and immutable")
        if _secret_paths(self.template):
            raise ValueError("operation template must not contain secret-shaped fields")
        object.__setattr__(self, "template", freeze_json_v1(self.template))
        if frozen_json_digest_v1(self.template) != self.template_digest:
            raise ValueError("operation template digest mismatch")
        if self.node_id in self.depends_on:
            raise ValueError("operation node cannot depend on itself")
        if (self.sealed_request is None) != (self.sealed_digest is None):
            raise ValueError("sealed request and digest must appear together")
        if self.sealed_request is not None:
            if _secret_paths(self.sealed_request):
                raise ValueError("sealed request must not contain secret-shaped fields")
            object.__setattr__(self, "sealed_request", freeze_json_v1(self.sealed_request))
            if frozen_json_digest_v1(self.sealed_request) != self.sealed_digest:
                raise ValueError("sealed request digest mismatch")
        if self.state in ("submitting", "accepted", "failed-final", "failed-retryable", "unknown") and self.sealed_request is None:
            raise ValueError("effect-bearing operation state requires a sealed request")
        if self.echo_strategy == "ambiguous-self-quarantine":
            if self.echo_evidence_revision != "none:v1":
                raise ValueError("ambiguous echo strategy requires none:v1 evidence")
        elif self.echo_evidence_revision == "none:v1":
            raise ValueError("verified echo strategy requires evidence")
        if self.agent_authorization_ref.use_nonce != self.authorization_use_nonce:
            raise ValueError("agent authorization use nonce mismatch")
        if self.service_authorization_ref.use_nonce != self.authorization_use_nonce:
            raise ValueError("service authorization use nonce mismatch")
        if self.service_authorization_ref.use_owner != "daemon-chat-outbox":
            raise ValueError("native operation requires daemon-chat-outbox service use owner")
        expected_action = {
            "text-send": "chat.submit.text",
            "file-upload": "chat.submit.file",
            "file-send": "chat.submit.file",
            "card-create": "chat.submit.card",
            "card-update": "chat.submit.card",
            "reaction": "chat.submit.reaction",
            "control-presentation": "chat.submit.control",
            "mail-submit": "chat.submit.text",
        }[self.kind]
        required_by_key = {
            (ref.authorization_kind, ref.resource.type): ref
            for ref in self.required_authorizations
        }
        expected_keys = {
            ("agent-operational", "instance"),
            ("agent-operational", "chat"),
            ("service", "instance"),
            ("service", "chat"),
        }
        if len(required_by_key) != len(self.required_authorizations) or set(required_by_key) != expected_keys:
            raise ValueError("native operation requires exact agent/service Instance+Chat rows")
        for ref in self.required_authorizations:
            if (
                ref.action != expected_action
                or ref.use_nonce != self.authorization_use_nonce
                or ref.resource.organization_id != self.agent_authorization_ref.organization_id
            ):
                raise ValueError("required authorization action, nonce, or organization mismatch")
        agent_rows = tuple(
            ref
            for ref in self.required_authorizations
            if ref.authorization_kind == "agent-operational"
        )
        service_rows = tuple(
            ref for ref in self.required_authorizations if ref.authorization_kind == "service"
        )
        if any(
            ref.expected_subject_principal != self.agent_authorization_ref.actor
            or ref.source_record_ref != self.agent_authorization_ref.lease_id
            or ref.source_generation != self.agent_authorization_ref.lease_generation
            for ref in agent_rows
        ):
            raise ValueError("agent authorization rows do not match the operational lease")
        service_subjects = {ref.expected_subject_principal for ref in service_rows}
        if len(service_subjects) != 1 or not next(iter(service_subjects)).startswith(
            "service:daemon:"
        ):
            raise ValueError("service authorization rows require one daemon service subject")
        if any(
            ref.source_record_ref != self.service_authorization_ref.lease_id
            or ref.source_generation != self.service_authorization_ref.lease_generation
            for ref in service_rows
        ):
            raise ValueError("service authorization rows do not match the signed lease ref")
        for resource_type in ("instance", "chat"):
            if required_by_key[("agent-operational", resource_type)].resource != required_by_key[
                ("service", resource_type)
            ].resource:
                raise ValueError("agent and service authorizations must cover identical resources")


@dataclass(frozen=True, slots=True)
class NativeOperationDag:
    plan_id: str
    target_id: str
    renderer_revision: str
    capability_revision: str
    credential_ref: str
    materialized_content_digest: str
    service_authorization_lease_bytes: bytes
    service_authorization_lease_digest: str
    nodes: tuple[OperationNode, ...]

    def __post_init__(self) -> None:
        for name in (
            "plan_id",
            "target_id",
            "renderer_revision",
            "capability_revision",
            "credential_ref",
            "materialized_content_digest",
            "service_authorization_lease_digest",
        ):
            _required(getattr(self, name), name)
        if not isinstance(self.service_authorization_lease_bytes, bytes):
            raise TypeError("service authorization lease must be bytes")
        if not self.service_authorization_lease_bytes:
            raise ValueError("service authorization lease must not be empty")
        if len(self.service_authorization_lease_bytes) > MAX_SERVICE_AUTHORIZATION_LEASE_BYTES:
            raise ValueError("service authorization lease exceeds byte bound")
        lease_digest = hashlib.sha256(self.service_authorization_lease_bytes).hexdigest()
        if lease_digest != self.service_authorization_lease_digest:
            raise ValueError("service authorization lease digest mismatch")
        _tuple_field(self, "nodes", limit=256)
        node_ids = tuple(node.node_id for node in self.nodes)
        if not node_ids or len(node_ids) != len(set(node_ids)):
            raise ValueError("operation DAG requires unique nodes")
        seen: set[str] = set()
        for node in self.nodes:
            if not set(node.depends_on) <= seen:
                raise ValueError("operation DAG must be acyclic and topologically ordered")
            seen.add(node.node_id)
            if node.echo_capability_revision != self.capability_revision:
                raise ValueError("operation DAG capability revision mismatch")
            if node.service_authorization_ref.lease_digest != self.service_authorization_lease_digest:
                raise ValueError("operation service authorization lease digest mismatch")
            if node.service_authorization_ref.operation_id != self.plan_id:
                raise ValueError("operation service authorization ref must match the plan")


PartState = Literal[
    "accepted", "failed-final", "failed-retryable", "unknown", "unsupported", "cancelled"
]
PART_STATES = (
    "accepted",
    "failed-final",
    "failed-retryable",
    "unknown",
    "unsupported",
    "cancelled",
)


@dataclass(frozen=True, slots=True)
class PartSubmission:
    part_index: int
    state: PartState
    provider_message_id: str | None
    definitely_not_sent: bool
    reason_code: str | None

    @property
    def native_message_id(self) -> str | None:
        """Typed native id over the frozen legacy wire field."""

        return self.provider_message_id  # frozen wire field "provider_message_id"

    def __post_init__(self) -> None:
        if isinstance(self.part_index, bool) or not isinstance(self.part_index, int):
            raise TypeError("part_index must be an integer")
        if not isinstance(self.definitely_not_sent, bool):
            raise TypeError("definitely_not_sent must be bool")
        if self.part_index < 0:
            raise ValueError("part_index must not be negative")
        if self.state not in PART_STATES:
            raise ValueError("unknown part state")
        if self.state in ("accepted", "unknown") and self.definitely_not_sent:
            raise ValueError("accepted/unknown parts cannot be definitely_not_sent")
        if self.state == "accepted" and not self.provider_message_id:
            raise ValueError("accepted part requires provider_message_id")
        if self.state != "accepted" and self.provider_message_id is not None:
            raise ValueError("only accepted parts may carry provider_message_id")


TargetState = Literal[
    "already-present",
    "accepted",
    "partial",
    "partial-pending",
    "partial-unknown",
    "failed",
    "pending",
    "unknown",
    "unresolved",
    "unauthorized",
    "unsupported",
    "cancelled",
]
TARGET_STATES = (
    "already-present",
    "accepted",
    "partial",
    "partial-pending",
    "partial-unknown",
    "failed",
    "pending",
    "unknown",
    "unresolved",
    "unauthorized",
    "unsupported",
    "cancelled",
)
PREFLIGHT_TARGET_STATES = ("already-present", "unresolved", "unauthorized", "unsupported")


@dataclass(frozen=True, slots=True)
class TargetReceipt:
    target_id: str
    parts: tuple[PartSubmission, ...]
    state: TargetState

    def __post_init__(self) -> None:
        _required(self.target_id, "target_id")
        _tuple_field(self, "parts")
        if self.state not in TARGET_STATES:
            raise ValueError("unknown target state")
        if not all(isinstance(part, PartSubmission) for part in self.parts):
            raise TypeError("target receipt parts must be typed")
        indexes = tuple(part.part_index for part in self.parts)
        if indexes != tuple(sorted(indexes)) or len(indexes) != len(set(indexes)):
            raise ValueError("target receipt part indexes must be sorted and unique")
        if not self.parts:
            if self.state not in PREFLIGHT_TARGET_STATES:
                raise ValueError("only preflight target receipts may omit parts")
        elif self.state in PREFLIGHT_TARGET_STATES:
            raise ValueError("preflight target receipts must not contain parts")
        elif derive_target_state(self.parts) != self.state:
            raise ValueError("target state must be derived from parts")


DeliveryOutcome = Literal[
    "complete", "partial", "partial-pending", "partial-unknown", "failed", "pending", "unknown"
]


@dataclass(frozen=True, slots=True)
class DeliveryReceipt:
    plan_id: str
    outcome: DeliveryOutcome
    targets: tuple[TargetReceipt, ...]

    def __post_init__(self) -> None:
        _required(self.plan_id, "plan_id")
        _tuple_field(self, "targets")
        if self.outcome not in (
            "complete", "partial", "partial-pending", "partial-unknown", "failed", "pending", "unknown"
        ):
            raise ValueError("unknown delivery outcome")
        if not all(isinstance(target, TargetReceipt) for target in self.targets):
            raise TypeError("delivery targets must be typed")
        target_ids = tuple(target.target_id for target in self.targets)
        if not target_ids or len(target_ids) != len(set(target_ids)):
            raise ValueError("delivery receipt requires unique targets")
        if derive_delivery_outcome(self.targets) != self.outcome:
            raise ValueError("delivery outcome does not match target receipts")


def derive_target_state(parts: tuple[PartSubmission, ...]) -> TargetState:
    """Derive the exact mutually-exclusive target state from a complete partition."""

    states = {part.state for part in parts}
    accepted = any(part.state == "accepted" for part in parts)
    if "unknown" in states:
        return "partial-unknown" if accepted else "unknown"
    if "failed-retryable" in states:
        return "partial-pending" if accepted else "pending"
    if parts and all(part.state == "accepted" for part in parts):
        return "accepted"
    if accepted:
        return "partial"
    if "failed-final" in states:
        return "failed"
    if "unsupported" in states:
        return "unsupported"
    return "cancelled"


def derive_target_receipt(
    target_id: str,
    required_part_indexes: tuple[int, ...],
    submissions: tuple[PartSubmission, ...],
) -> TargetReceipt:
    """Normalize provider parts and make missing/duplicate indexes unknown."""

    required = tuple(required_part_indexes)
    if not required or required != tuple(sorted(set(required))) or required[0] < 0:
        raise ValueError("required part indexes must be a non-empty sorted set")
    by_index: dict[int, list[PartSubmission]] = {index: [] for index in required}
    invalid = False
    for submission in submissions:
        if submission.part_index not in by_index:
            invalid = True
            continue
        by_index[submission.part_index].append(submission)
    normalized: list[PartSubmission] = []
    for index in required:
        candidates = by_index[index]
        if len(candidates) != 1:
            invalid = True
            normalized.append(
                PartSubmission(index, "unknown", None, False, "invalid-provider-partition")
            )
        else:
            normalized.append(candidates[0])
    if invalid:
        normalized = [
            PartSubmission(index, "unknown", None, False, "invalid-provider-partition")
            for index in required
        ]
    parts = tuple(normalized)
    state = derive_target_state(parts)
    return TargetReceipt(target_id, parts, state)


def preflight_target_receipt(
    target_id: str,
    state: Literal["already-present", "unresolved", "unauthorized", "unsupported"],
) -> TargetReceipt:
    return TargetReceipt(target_id, (), state)


def retry_part_indexes(receipt: TargetReceipt) -> tuple[int, ...]:
    return tuple(part.part_index for part in receipt.parts if part.state == "failed-retryable")


def reconcile_part_indexes(receipt: TargetReceipt) -> tuple[int, ...]:
    return tuple(part.part_index for part in receipt.parts if part.state == "unknown")


def derive_delivery_outcome(targets: tuple[TargetReceipt, ...]) -> DeliveryOutcome:
    if not targets:
        raise ValueError("delivery requires at least one target")
    states = tuple(target.state for target in targets)
    if all(state in ("accepted", "already-present") for state in states):
        return "complete"
    success = any(state in ("accepted", "already-present", "partial", "partial-pending", "partial-unknown") for state in states)
    if any(state in ("unknown", "partial-unknown") for state in states):
        return "partial-unknown" if success else "unknown"
    if any(state in ("pending", "partial-pending") for state in states):
        return "partial-pending" if success else "pending"
    if success:
        return "partial"
    return "failed"


def derive_delivery_receipt(plan_id: str, targets: tuple[TargetReceipt, ...]) -> DeliveryReceipt:
    frozen = tuple(targets)
    return DeliveryReceipt(plan_id, derive_delivery_outcome(frozen), frozen)


@dataclass(frozen=True, slots=True)
class MailboxEpochV1:
    schema: Literal["hyprial.mail.mailbox-epoch/v1"]
    epoch_id: str
    mailbox_scope: str
    folder: str
    uidvalidity: int
    previous_epoch_id: str | None
    state: Literal["active", "rollover-detected", "reconciling", "held", "retired"]
    detected_at_ms: int
    snapshot_highest_uid: int | None
    scan_next_uid: int
    scan_end_uid: int | None
    tail_next_uid: int
    old_pending_count: int
    quarantine_count: int
    operation_id: str

    def __post_init__(self) -> None:
        if self.schema != "hyprial.mail.mailbox-epoch/v1":
            raise ValueError("unknown mailbox epoch schema")
        for name in ("epoch_id", "mailbox_scope", "folder", "operation_id"):
            _required(getattr(self, name), name)
        for name in (
            "uidvalidity",
            "detected_at_ms",
            "scan_next_uid",
            "tail_next_uid",
            "old_pending_count",
            "quarantine_count",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError(f"{name} must be an integer")
            if value < 0:
                raise ValueError(f"{name} must not be negative")
        if self.state not in ("active", "rollover-detected", "reconciling", "held", "retired"):
            raise ValueError("unknown mailbox epoch state")
        if self.uidvalidity < 1 or self.scan_next_uid < 1 or self.tail_next_uid < 1:
            raise ValueError("mailbox epoch and cursors must be positive")
        if self.snapshot_highest_uid is not None:
            if isinstance(self.snapshot_highest_uid, bool) or not isinstance(
                self.snapshot_highest_uid, int
            ):
                raise TypeError("snapshot_highest_uid must be an integer")
            if self.snapshot_highest_uid < 0:
                raise ValueError("snapshot_highest_uid must not be negative")
        if self.scan_end_uid is not None:
            if isinstance(self.scan_end_uid, bool) or not isinstance(self.scan_end_uid, int):
                raise TypeError("scan_end_uid must be an integer")
            if self.scan_end_uid < 1:
                raise ValueError("scan_end_uid must be positive")
            if self.scan_next_uid > self.scan_end_uid:
                raise ValueError("scan_next_uid must not pass scan_end_uid")
            if self.tail_next_uid <= self.scan_end_uid:
                raise ValueError("tail_next_uid must follow scan_end_uid")
            if (
                self.snapshot_highest_uid is not None
                and self.scan_end_uid > self.snapshot_highest_uid
            ):
                raise ValueError("scan_end_uid exceeds snapshot boundary")


@dataclass(frozen=True, slots=True)
class SmtpRecipientSubmission:
    recipient_id: str
    attempt: int
    rcpt_state: Literal["accepted", "refused-retryable", "refused-final"]
    data_state: Literal[
        "not-attempted", "accepted", "rejected-retryable", "rejected-final", "unknown"
    ]
    definitely_not_sent: bool
    smtp_code: int | None
    enhanced_status: str | None
    reason_code: str | None

    def __post_init__(self) -> None:
        _required(self.recipient_id, "recipient_id")
        if isinstance(self.attempt, bool) or not isinstance(self.attempt, int):
            raise TypeError("SMTP attempt must be an integer")
        if self.attempt < 1:
            raise ValueError("SMTP attempt must be positive")
        if not isinstance(self.definitely_not_sent, bool):
            raise TypeError("SMTP definitely_not_sent must be bool")
        if self.rcpt_state not in ("accepted", "refused-retryable", "refused-final"):
            raise ValueError("unknown SMTP RCPT state")
        if self.data_state not in (
            "not-attempted", "accepted", "rejected-retryable", "rejected-final", "unknown"
        ):
            raise ValueError("unknown SMTP DATA state")
        if self.rcpt_state != "accepted" and self.data_state != "not-attempted":
            raise ValueError("refused RCPT cannot have DATA state")
        if self.rcpt_state != "accepted" and not self.definitely_not_sent:
            raise ValueError("refused RCPT must be definitely_not_sent")
        if self.rcpt_state == "accepted" and self.data_state == "not-attempted":
            raise ValueError("accepted RCPT requires the transaction DATA result")
        if self.data_state in ("accepted", "unknown") and self.definitely_not_sent:
            raise ValueError("accepted/unknown SMTP DATA cannot be definitely_not_sent")
        if self.data_state in ("rejected-retryable", "rejected-final") and not self.definitely_not_sent:
            raise ValueError("explicitly rejected SMTP DATA must be definitely_not_sent")
        if self.smtp_code is not None and (
            isinstance(self.smtp_code, bool) or not isinstance(self.smtp_code, int)
        ):
            raise TypeError("SMTP status code must be an integer")
        expected_code_class = {
            ("accepted", "accepted"): 2,
            ("accepted", "rejected-retryable"): 4,
            ("accepted", "rejected-final"): 5,
            ("refused-retryable", "not-attempted"): 4,
            ("refused-final", "not-attempted"): 5,
        }.get((self.rcpt_state, self.data_state))
        if self.data_state == "unknown":
            if self.smtp_code is not None:
                raise ValueError("unknown SMTP DATA outcome cannot have a completed status code")
        elif expected_code_class is not None and (
            self.smtp_code is None or self.smtp_code // 100 != expected_code_class
        ):
            raise ValueError("SMTP status code contradicts RCPT/DATA state")


@dataclass(frozen=True, slots=True)
class SmtpTransactionReceipt:
    transaction_id: str
    outbound_message_id: str
    payload_digest: str
    recipients: tuple[SmtpRecipientSubmission, ...]

    def __post_init__(self) -> None:
        for name in ("transaction_id", "outbound_message_id", "payload_digest"):
            _required(getattr(self, name), name)
        _tuple_field(self, "recipients", limit=1024)
        if not all(isinstance(row, SmtpRecipientSubmission) for row in self.recipients):
            raise TypeError("SMTP recipients must be typed")
        recipient_ids = tuple(row.recipient_id for row in self.recipients)
        if not recipient_ids or len(recipient_ids) != len(set(recipient_ids)):
            raise ValueError("SMTP receipt requires unique recipients")
        attempts = {row.attempt for row in self.recipients}
        if len(attempts) != 1:
            raise ValueError("SMTP transaction recipients must share one attempt")
        data_results = {row.data_state for row in self.recipients if row.rcpt_state == "accepted"}
        if len(data_results) > 1:
            raise ValueError("SMTP transaction must apply one DATA result to all accepted RCPTs")


@dataclass(frozen=True, slots=True)
class MailDeliveryObservation:
    observation_id: str
    transaction_id: str
    recipient_id: str
    kind: Literal["dsn-delivered", "dsn-delayed", "dsn-failed", "mdn-displayed"]
    authenticated: bool
    source_message_id: str
    observed_at_ms: int
    evidence_digest: str

    def __post_init__(self) -> None:
        for name in (
            "observation_id",
            "transaction_id",
            "recipient_id",
            "source_message_id",
            "evidence_digest",
        ):
            _required(getattr(self, name), name)
        if self.kind not in ("dsn-delivered", "dsn-delayed", "dsn-failed", "mdn-displayed"):
            raise ValueError("unknown mail delivery observation kind")
        if not isinstance(self.authenticated, bool):
            raise TypeError("mail observation authenticated must be bool")
        if isinstance(self.observed_at_ms, bool) or not isinstance(self.observed_at_ms, int):
            raise TypeError("mail observation time must be an integer")
        if self.observed_at_ms < 0:
            raise ValueError("mail observation time must not be negative")


def smtp_submission_to_part(
    part_index: int, submission: SmtpRecipientSubmission
) -> PartSubmission:
    if submission.rcpt_state == "refused-retryable" or submission.data_state == "rejected-retryable":
        state: PartState = "failed-retryable"
    elif submission.rcpt_state == "refused-final" or submission.data_state == "rejected-final":
        state = "failed-final"
    elif submission.data_state == "unknown":
        state = "unknown"
    elif submission.rcpt_state == "accepted" and submission.data_state == "accepted":
        state = "accepted"
    else:
        raise ValueError("incomplete SMTP recipient submission")
    provider_id = submission.recipient_id if state == "accepted" else None
    return PartSubmission(
        part_index,
        state,
        provider_id,
        submission.definitely_not_sent,
        submission.reason_code,
    )


__all__ = [
    "AgentAuthorizationResourceV1",
    "AgentOperationalAuthorizationRefV1",
    "AuthorizationAction",
    "AuthorizationKind",
    "AuthorizationResourceV1",
    "ChatAuthorizationResourceV1",
    "ControlAuthorizationResourceV1",
    "CorrespondenceAuthorizationResourceV1",
    "DeliveryOutcome",
    "DeliveryReceipt",
    "MailDeliveryObservation",
    "MailboxEpochV1",
    "InstanceAuthorizationResourceV1",
    "NativeOperationDag",
    "OperationNode",
    "OperationNodeState",
    "PartState",
    "PartSubmission",
    "RequiredAuthorizationV1",
    "ServiceAuthorizationRefV1",
    "SmtpRecipientSubmission",
    "SmtpTransactionReceipt",
    "TargetReceipt",
    "TargetState",
    "authorization_required_digest_v1",
    "derive_delivery_outcome",
    "derive_delivery_receipt",
    "derive_target_receipt",
    "derive_target_state",
    "preflight_target_receipt",
    "reconcile_part_indexes",
    "retry_part_indexes",
    "smtp_submission_to_part",
]
