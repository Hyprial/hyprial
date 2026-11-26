"""Reconcile effect vocabulary, local member launch resolution and digests (I6-2).

Decision notes (cell model slice I6-2, plan §H.1-H.3):

- Member launch identity is typed ``(agent_id, member)``; document fields
  (``kind``, ``model``) combine with local launch facts (``cwd``, ``command``,
  ``args``, ``model_vendor``, ``approval``, ``env_ref``) to compute a
  deterministic SHA-256 ``config_digest``.
- Reconcile effects use dedicated effect names (``cell.member.ensure``,
  ``cell.member.stop``, ``cell.member.policy.apply``) and never reuse
  ``harness.ensure``.
- Every intent and completion carries ``ReconcileFence(cell_id, epoch,
  revision, member)`` plus ``attempt_id`` so stale completions are dropped.
- I6-5a: the member's document ``allow``/``deny`` resolve to a
  ``MemberPolicy``; its digest is part of ``config_digest`` and is kept apart
  as ``policy_digest``, while ``base_digest`` is the digest without it, so the
  planner can tell a policy-only change (``cell.member.policy.apply``) from a
  launch-configuration change.  A declared policy on a harness without a
  policy mapping fails the member (``policy-unsupported``).
- The document member name is not always the harness launch member
  (I6-2 restore fix, 2026-11-23).  A daemon-managed single-harness agent
  cell names its one member by the agent URI, while the local launch keeps
  the typed I6-1B identity ``member_name(spec)`` (``spec.member`` or, for a
  legacy launch, ``spec.name``), which is also the harness actor's key.
  ``LocalMemberLaunchFacts.launch_member`` carries that canonical name from
  the local launch, ``DesiredMemberSpec.launch_member_name`` launches and
  stops by it, and the document name stays the reconcile key and digest
  input.  An agent URI is therefore never fed to a harness as a member.
- ``LocalMemberLaunchFacts.held`` (I6-2 ownership fix, 2026-11-23) says the
  local launch row's last known result is not ``running`` (daemon restore's
  own predicate), so the planner can leave a deliberately failed member
  down; it is not a digest input.
- ``MemberEffectCompletion.adopted`` (I6-2 spec fix, 2026-11-23) marks an
  ensure that found the member already live instead of starting it.  The
  reported digest is always the live one; an adopted completion whose
  digest differs from the desired one makes the actor drain-and-restart
  the member (the document is authoritative for declared members), while
  a freshly started member with a different digest still fails, so a
  difference that a restart cannot remove never loops.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Protocol

from hyprial.daemon.impl.cells.structure import StructureMember
from hyprial.daemon.impl.lifecycle_manager.steps import (
    CELL_MEMBER_EFFECT_NAMES,
    CELL_MEMBER_ENSURE,
    CELL_MEMBER_POLICY_APPLY,
    CELL_MEMBER_STOP,
)
from hyprial.kernel import HarnessLaunchSpec

from .policy import (
    REASON_POLICY_UNSUPPORTED,
    MemberPolicy,
    resolve_member_policy,
    supports_policy,
)

CUSTODY_DAEMON = "daemon"
CUSTODY_EXTERNAL = "external"


@dataclass(frozen=True, slots=True, order=True)
class ReconcileMemberKey:
    """Typed member identity for document-driven reconcile."""

    agent_id: str
    member: str

    def __post_init__(self) -> None:
        if not isinstance(self.agent_id, str) or not self.agent_id:
            raise TypeError("agent_id must be a non-empty string")
        if not isinstance(self.member, str) or not self.member:
            raise TypeError("member must be a non-empty string")


@dataclass(frozen=True, slots=True)
class LocalMemberLaunchFacts:
    """Local machine-specific launch facts for one ``(agent_id, member)``."""

    agent_id: str
    member: str
    harness: str | None = None
    cwd: str | None = None
    command: tuple[str, ...] = ()
    args: tuple[str, ...] = ()
    model_vendor: str | None = None
    model: str | None = None
    approval: str | None = None
    env_ref: str | None = None
    custody: str = CUSTODY_DAEMON
    launch_member: str | None = None
    held: bool = False
    model_binding: Mapping[str, object] | None = None
    binding_error: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "command", tuple(self.command))
        object.__setattr__(self, "args", tuple(self.args))
        if self.model_binding is not None:
            object.__setattr__(self, "model_binding", MappingProxyType(dict(self.model_binding)))


class LocalMemberFactsPort(Protocol):
    """Read-only lookup for local member launch facts."""

    def facts_for(self, agent_id: str, member: str) -> LocalMemberLaunchFacts | None: ...


class DictLocalMemberFacts:
    """In-memory ``LocalMemberFactsPort`` keyed by ``(agent_id, member)``."""

    def __init__(self, facts: Mapping[tuple[str, str], LocalMemberLaunchFacts]) -> None:
        self._facts = dict(facts)

    def facts_for(self, agent_id: str, member: str) -> LocalMemberLaunchFacts | None:
        return self._facts.get((agent_id, member))


@dataclass(frozen=True, slots=True)
class DesiredMemberSpec:
    """Fully resolved desired launch configuration for one cell harness member."""

    key: ReconcileMemberKey
    role: str
    harness: str
    model: str | None
    model_vendor: str | None
    cwd: str
    command: tuple[str, ...]
    args: tuple[str, ...]
    approval: str | None
    env_ref: str | None
    config_digest: str
    policy_digest: str | None = None
    policy: MemberPolicy | None = None
    base_digest: str = ""
    launch_member: str | None = None
    model_binding: Mapping[str, object] | None = None

    def __post_init__(self) -> None:
        if self.model_binding is not None:
            object.__setattr__(self, "model_binding", MappingProxyType(dict(self.model_binding)))

    @property
    def launch_member_name(self) -> str:
        """The harness launch member: the local canonical name, else the document name."""

        return self.launch_member or self.key.member

    def to_launch_spec(self, agent_actor: str) -> HarnessLaunchSpec:
        """Build the typed ``HarnessLaunchSpec`` for this member."""

        return HarnessLaunchSpec(
            self.harness,
            agent_actor,
            True,
            self.args,
            "managed",
            None,
            self.cwd,
            None,
            None,
            self.command,
            None,
            None,
            False,
            None,
            None,
            self.model_vendor,
            self.model,
            None,
            "running",
            self.launch_member_name,
            model_binding=self.model_binding,
        )


def compute_member_config_digest(
    *,
    agent_id: str,
    member: str,
    harness: str,
    model: str | None,
    model_vendor: str | None = None,
    cwd: str,
    command: tuple[str, ...],
    args: tuple[str, ...],
    approval: str | None,
    env_ref: str | None,
    policy_digest: str | None = None,
    model_binding: Mapping[str, object] | None = None,
) -> str:
    """Deterministic SHA-256 over resolved member configuration."""

    payload = {
        "agentId": agent_id,
        "member": member,
        "harness": harness,
        "model": model,
        "modelVendor": model_vendor,
        "cwd": cwd,
        "command": list(command),
        "args": list(args),
        "approval": approval,
        "envRef": env_ref,
        "policyDigest": policy_digest,
    }
    if model_binding is not None:
        payload["modelBinding"] = dict(model_binding)
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def resolve_member_desired_spec(
    agent_id: str,
    doc_member: StructureMember,
    facts: LocalMemberLaunchFacts | None,
) -> tuple[DesiredMemberSpec | None, str | None]:
    """Combine document member fields with local launch facts."""

    if facts is None:
        return None, f"missing-local-facts:{doc_member.name}:no-record"
    if facts.custody == CUSTODY_EXTERNAL:
        return None, "external-custody"
    if facts.binding_error is not None:
        return None, facts.binding_error
    if facts.model_binding is not None and (
        doc_member.kind != facts.harness or doc_member.model != facts.model
    ):
        return None, "model-binding-cell-intent-mismatch"
    harness = doc_member.kind or facts.harness
    if not harness:
        return None, f"missing-local-facts:{doc_member.name}:harness"
    if not facts.cwd:
        return None, f"missing-local-facts:{doc_member.name}:cwd"
    if not facts.command:
        return None, f"missing-local-facts:{doc_member.name}:command"
    model = doc_member.model if doc_member.model is not None else facts.model
    policy = resolve_member_policy(harness, doc_member.allow, doc_member.deny)
    if policy is not None and not supports_policy(harness):
        return None, f"{REASON_POLICY_UNSUPPORTED}:{doc_member.name}:{harness}"
    launch = {
        "agent_id": agent_id,
        "member": doc_member.name,
        "harness": harness,
        "model": model,
        "model_vendor": facts.model_vendor,
        "cwd": facts.cwd,
        "command": facts.command,
        "args": facts.args,
        "approval": facts.approval,
        "env_ref": facts.env_ref,
        "model_binding": facts.model_binding,
    }
    policy_digest = policy.digest if policy is not None else None
    digest = compute_member_config_digest(**launch, policy_digest=policy_digest)
    return (
        DesiredMemberSpec(
            key=ReconcileMemberKey(agent_id, doc_member.name),
            role=doc_member.role,
            harness=harness,
            model=model,
            model_vendor=facts.model_vendor,
            cwd=facts.cwd,
            command=facts.command,
            args=facts.args,
            approval=facts.approval,
            env_ref=facts.env_ref,
            config_digest=digest,
            policy_digest=policy_digest,
            policy=policy,
            base_digest=compute_member_config_digest(**launch),
            launch_member=facts.launch_member,
            model_binding=facts.model_binding,
        ),
        None,
    )


@dataclass(frozen=True, slots=True)
class ReconcileFence:
    """Completion fence ensuring old epochs, revisions or members cannot commit."""

    cell_id: str
    epoch: str
    revision: int
    member: str

    def __post_init__(self) -> None:
        if not self.cell_id or not self.epoch or not self.member:
            raise TypeError("fence cell_id, epoch and member must be non-empty")
        if isinstance(self.revision, bool) or self.revision < 0:
            raise TypeError("fence revision must be a non-negative integer")


class MemberEffectAction(StrEnum):
    """Durable member reconcile effect actions."""

    ENSURE = CELL_MEMBER_ENSURE
    STOP = CELL_MEMBER_STOP
    POLICY_APPLY = CELL_MEMBER_POLICY_APPLY


@dataclass(frozen=True, slots=True)
class MemberEffectIntent:
    """Non-blocking reconcile effect submitted outside the actor."""

    fence: ReconcileFence
    attempt_id: str
    agent_id: str
    action: MemberEffectAction
    spec: DesiredMemberSpec | None = None
    reason: str | None = None
    previous: DesiredMemberSpec | None = None


@dataclass(frozen=True, slots=True)
class MemberEffectCompletion:
    """Completion reported back to the reconcile actor with its fence."""

    fence: ReconcileFence
    attempt_id: str
    action: MemberEffectAction
    succeeded: bool
    reported_digest: str | None = None
    error_code: str | None = None
    error_detail: str | None = None
    adopted: bool = False


class MemberEffectPort(Protocol):
    """Non-blocking execution port for cell member reconcile effects."""

    def submit(self, intent: MemberEffectIntent) -> None: ...


__all__ = [
    "CELL_MEMBER_EFFECT_NAMES",
    "CELL_MEMBER_ENSURE",
    "CELL_MEMBER_POLICY_APPLY",
    "CELL_MEMBER_STOP",
    "CUSTODY_DAEMON",
    "CUSTODY_EXTERNAL",
    "DesiredMemberSpec",
    "DictLocalMemberFacts",
    "LocalMemberFactsPort",
    "LocalMemberLaunchFacts",
    "MemberEffectAction",
    "MemberEffectCompletion",
    "MemberEffectIntent",
    "MemberEffectPort",
    "ReconcileFence",
    "ReconcileMemberKey",
    "compute_member_config_digest",
    "resolve_member_desired_spec",
]
