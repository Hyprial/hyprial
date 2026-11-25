"""Pure admission and frozen audience planning."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Mapping

from hyprial.identity import AgentIdentitySnapshot, OrganizationAuthorizationLeaseV1


class AdmissionDenied(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class FrozenTargetSet:
    source_ref: str
    requested: str
    effective: str
    event_class: str
    target_refs: tuple[str, ...]
    already_present_refs: tuple[str, ...]
    unresolved: tuple[tuple[str, str], ...]
    policy_revision: int
    policy_reads: int


@dataclass(frozen=True, slots=True)
class AdmissionDecision:
    organization_id: str
    actor: str
    entity_token: str
    association_generation: int
    lease_generation: int
    admitted: bool


def admit_with_authorization(
    *,
    identity: AgentIdentitySnapshot,
    lease: OrganizationAuthorizationLeaseV1,
    required_scope: tuple[str, ...],
    now_ms: int,
) -> AdmissionDecision:
    """Validate the reviewed identity value without importing identity internals."""

    organization = identity.organization
    if organization.organization_id is None or organization.status != "active":
        raise AdmissionDenied("AGENT_ORG_SCOPE_DENIED")
    if (
        lease.organization_id != organization.organization_id
        or lease.actor != identity.actor
        or lease.entity_token != identity.entity_token
        or lease.association_generation != organization.generation
    ):
        raise AdmissionDenied("AUTHORIZATION_SUBJECT_MISMATCH")
    try:
        lease.verify(now_ms=now_ms, required_scope=required_scope)
    except ValueError as error:
        code = str(error)
        if code == "AUTHORIZATION_LEASE_EXPIRED":
            raise AdmissionDenied("AGENT_ORG_AUTHORIZATION_STALE") from error
        raise AdmissionDenied(code) from error
    return AdmissionDecision(
        organization_id=lease.organization_id,
        actor=identity.actor,
        entity_token=identity.entity_token,
        association_generation=lease.association_generation,
        lease_generation=lease.generation,
        admitted=True,
    )


def freeze_sync_targets(
    *,
    source_ref: str,
    requested: Literal["inherit", "exact", "corresponding"],
    event_class: str,
    policy_revision: int,
    policy_modes: Mapping[str, str],
    correspondences: tuple[tuple[str, str, int], ...],
    authorized_refs: frozenset[str],
    source_already_present: bool,
) -> FrozenTargetSet:
    if source_ref not in authorized_refs:
        raise AdmissionDenied("CHAT_TARGET_UNAUTHORIZED")
    effective = policy_modes.get(event_class) if requested == "inherit" else requested
    if effective not in ("exact", "corresponding"):
        raise AdmissionDenied("CHAT_SYNC_POLICY_INVALID")
    targets: set[str] = set()
    unresolved: set[tuple[str, str]] = set()
    if not source_already_present:
        targets.add(source_ref)
    if effective == "corresponding":
        for left, right, _version in correspondences:
            if left != source_ref:
                continue
            if right in authorized_refs:
                targets.add(right)
            else:
                unresolved.add((right, "unauthorized-correspondence"))
    already = (source_ref,) if source_already_present else ()
    targets.difference_update(already)
    return FrozenTargetSet(
        source_ref=source_ref,
        requested=requested,
        effective=effective,
        event_class=event_class,
        target_refs=tuple(sorted(targets)),
        already_present_refs=already,
        unresolved=tuple(sorted(unresolved)),
        policy_revision=policy_revision,
        policy_reads=1,
    )


def resume_sync_targets(
    frozen: FrozenTargetSet, *, current_policy_revision: int
) -> FrozenTargetSet:
    del current_policy_revision
    return frozen


def resolve_sync_targets(value: dict[str, object]) -> dict[str, object]:
    source = str(value["source"])
    edges = tuple((str(edge[0]), str(edge[1]), 1) for edge in value["edges"])  # type: ignore[index]
    refs = frozenset({source, *(right for _left, right, _version in edges)})
    frozen = freeze_sync_targets(
        source_ref=source,
        requested=str(value["audience"]),  # type: ignore[arg-type]
        event_class="human-chat-input",
        policy_revision=1,
        policy_modes={"human-chat-input": "corresponding"},
        correspondences=edges,
        authorized_refs=refs,
        source_already_present=True,
    )
    return {
        "targetIds": list(frozen.target_refs),
        "sourceIncluded": source in frozen.target_refs,
        "hops": 1,
        "policyReads": frozen.policy_reads,
    }


def plan_proactive_mail(value: dict[str, object]) -> dict[str, object]:
    requested = tuple(str(item) for item in value.get("to", ()))
    verified = frozenset(str(item) for item in value.get("verifiedRecipients", ()))
    if value.get("parent") is not None:
        return {"ok": False, "code": "MAIL_SEND_PARENT_FORBIDDEN", "targets": [], "providerCalls": 0}
    if not requested or any(item not in verified for item in requested):
        return {
            "ok": False,
            "code": "MAIL_RECIPIENT_NOT_VERIFIED",
            "targets": [],
            "providerCalls": 0,
        }
    return {"ok": True, "code": None, "targets": sorted(set(requested)), "providerCalls": 0}


__all__ = [
    "AdmissionDecision",
    "AdmissionDenied",
    "FrozenTargetSet",
    "admit_with_authorization",
    "freeze_sync_targets",
    "plan_proactive_mail",
    "resolve_sync_targets",
    "resume_sync_targets",
]
