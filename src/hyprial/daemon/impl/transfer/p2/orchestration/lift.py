"""AT07 I3-b: open one P2 chain and read its decision from its two ledgers.

``chain.evaluate_chain`` decides, ``target.land_and_activate`` lands; what is
missing between them and a caller is the *chain identity*: the two admissions
that have to join.  ``chain._same_plan`` compares every binding field except
the role, so the source and target sides must be composed from **one** fact
set.  Two callers composing their own bindings from the same facts look
equivalent and are not: a target actor spelled two ways (a canonical URI on one
side, the local actor name on the other) produces two ledgers that each look
healthy and a chain that holds forever at ``plan-mismatch`` with no error to
read.  This module composes them once, derives the target from the source, and
refuses facts that cannot describe one chain at all.

It owns no storage, no mailbox and no IPC: the admissions are prepared through
whatever ``AdmissionStore`` the caller passes, and who may call this at all stays
the composition layer's authority decision (an admission is not a grant).

This increment wires the source side and the decision.  It lifts no P0 refusal
by itself: what opens the operator path is the proven-chain table the
composition layer consults first (``chain_is_proven``), so a harness no
two-daemon run has proved is still refused, with the P0 code and mode string,
before any of this runs.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from hyprial.daemon.impl.transfer.p2.journal import StepState
from hyprial.daemon.impl.transfer.p2.model import Admission, Binding, Role
from hyprial.daemon.impl.transfer.p2.orchestration.chain import (
    ChainResume,
    SourceLiveness,
    decide_from_rows,
    evaluate_chain,
    read_admission,
    read_row,
)
from hyprial.daemon.impl.transfer.p2.store import AdmissionStore


class ChainPlanError(ValueError):
    """These facts cannot describe one chain; refused before any row exists."""


#: The card's mode string.  It is spelled exactly as the P0 refusal spells it so
#: "proven" and "refused" name the same thing on both sides of the wire.
P2_CHAIN_MODE = "agent-home-p2"


#: The entrypoint a proven row speaks for.  ``headless`` is the streaming
#: worker P0 already moves; ``tui`` is the CLI-hosted interactive session.  A
#: row that omits the field reads as ``headless`` -- never as "all
#: entrypoints" -- so a missing field can never widen what a run proved.
ENTRYPOINT_HEADLESS = "headless"
ENTRYPOINT_TUI = "tui"


@dataclass(frozen=True, slots=True)
class ProvenChain:
    """One (harness, mode, entrypoint) whose P2 chain a real run proved.

    ``evidence`` names that run: the exact tuple it proved (harness, version,
    OS, mode, entrypoint) and where the reading lives.  The runtime check
    cannot re-read a harness version or an OS, so it matches on (harness,
    mode, entrypoint) -- the evidence is what makes the row reviewable, not
    what the guard computes.  ``entrypoint`` defaults to ``headless`` so an
    older row without the field stays exactly as narrow as it was.
    """

    harness: str
    mode: str
    evidence: str
    entrypoint: str = ENTRYPOINT_HEADLESS


#: Data, never configuration: nothing on the wire, in the environment, or on a
#: command line can add a row, and every row here is a published support-matrix
#: change.  A row exists only because a real two-daemon run proved that harness,
#: and the evidence names the run (and what it did not prove).
PROVEN_CHAINS: tuple[ProvenChain, ...] = (
    ProvenChain(
        "pi",
        P2_CHAIN_MODE,
        "AT07 I4: two-daemon run, docs/notes/at07-i4-run.txt "
        "(real daemons/lifecycle/registry/ledger/landing; the pi process is the "
        "repository's protocol double, so the vendor binary and its credential "
        "remain NOT_RUN -- TC012 BLOCKED)",
        ENTRYPOINT_HEADLESS,
    ),
    ProvenChain(
        "codex",
        P2_CHAIN_MODE,
        "C3-S1-C: two-daemon run, docs/notes/c3-codex-run.txt "
        "(real daemons/lifecycle/registry/ledger/landing; the codex worker's "
        "model protocol is the repository's double, so the vendor binary and its "
        "credential remain NOT_RUN -- TC012 BLOCKED)",
    ),
    ProvenChain(
        "claude",
        P2_CHAIN_MODE,
        "C3-S2-C: two-daemon run, docs/notes/c3-claude-run.txt "
        "(real daemons/lifecycle/registry/ledger/landing; the claude process is "
        "the repository's protocol double (tests/fixtures/harnesses/"
        "fake_claude_sdk.py) and the credential is a placeholder, so the real "
        "vendor binary, the real SDK and the real credential remain NOT_RUN -- "
        "TC012 BLOCKED)",
    ),
)


def chain_is_proven(
    harness: str,
    mode: str = P2_CHAIN_MODE,
    entrypoint: str = ENTRYPOINT_HEADLESS,
) -> bool:
    """May this (harness, mode, entrypoint) P2 chain run at all?

    False until a run says otherwise.  The ``entrypoint`` default is
    ``headless`` on purpose: an old caller (and an old row) keeps the exact
    proof it had, and a TUI can never be admitted by omission.
    """

    return any(
        entry.harness == harness
        and entry.mode == mode
        and entry.entrypoint == entrypoint
        for entry in PROVEN_CHAINS
    )


@dataclass(frozen=True, slots=True)
class ChainPlan:
    """One chain: its single operation id and the two admissions that must join."""

    operation_id: str
    source: Binding
    target: Binding


def chain_bindings(
    *,
    operation_id: str,
    owner: str,
    source_node: str,
    source_actor: str,
    source_entity_token: str,
    target_node: str,
    target_actor: str,
    plan_digest: str,
) -> ChainPlan:
    """The two admissions of one chain, both derived from one fact set.

    The roles are the only difference between the results, which is exactly the
    join ``chain._same_plan`` requires.  Node ids must differ: one node id on
    both ends would mint identical actor URIs for the two incarnations, which is
    the dual-active shape the whole chain exists to refuse.
    """

    if source_node == target_node:
        raise ChainPlanError(
            f"source and target node ids are both {source_node!r}; one node id on "
            "both ends would mint identical actor URIs (dual-active)"
        )
    source = Binding(
        role=Role.SOURCE,
        owner=owner,
        source_node=source_node,
        source_agent_id=source_actor,
        source_entity_token=source_entity_token,
        target_node=target_node,
        target_actor=target_actor,
        plan_digest=plan_digest,
    )
    # Nothing about the two sides may drift, so the target is derived, not
    # composed a second time.  The operation id travels with the pair: it is
    # the chain's identity, so the caller cannot open the two halves under
    # different ids and leave a chain that never joins.
    return ChainPlan(operation_id, source, replace(source, role=Role.TARGET))


def begin_source(store: AdmissionStore, plan: ChainPlan) -> Admission:
    """Open the source's half of the chain, idempotently.

    Re-opening an already-open chain returns the existing admission; a different
    plan under the same operation id is a ``replay-mismatch`` from the store,
    which is the point -- the operation id names one chain.
    """

    if plan.source.role is not Role.SOURCE or plan.target.role is not Role.TARGET:
        raise ChainPlanError("a chain plan must carry one source and one target binding")
    return store.prepare(plan.operation_id, plan.source)


def admit_target(store: AdmissionStore, plan: ChainPlan) -> Admission:
    """Open the target's half of the chain on the *target* daemon."""

    if plan.source.role is not Role.SOURCE or plan.target.role is not Role.TARGET:
        raise ChainPlanError("a chain plan must carry one source and one target binding")
    return store.prepare(plan.operation_id, plan.target)


def decide(
    plan: ChainPlan,
    *,
    source_store: AdmissionStore,
    target_store: AdmissionStore,
    source_live: SourceLiveness,
) -> ChainResume:
    """Read what the source side may do next.

    A named entry point so the caller that finishes a chain does not reach past
    the wiring to the decision; the rules live in ``chain`` and are not restated
    here.
    """

    return evaluate_chain(
        plan.operation_id,
        source_store=source_store,
        target_store=target_store,
        source_live=source_live,
    )


#: The version of the reported-half projection below.  A parser that does not
#: know the version refuses it rather than guessing at the shape.
REPORT_VERSION = 1

_BINDING_KEYS = (
    "role",
    "owner",
    "sourceNode",
    "sourceAgentId",
    "sourceEntityToken",
    "targetNode",
    "targetActor",
    "planDigest",
)
_ADMISSION_KEYS = (
    "operationId",
    "binding",
    "createdAtMs",
    "expiresAtMs",
    "revokedAtMs",
    "cancelledAtMs",
)
_ROW_KEYS = (
    "operationId",
    "actor",
    "phase",
    "step",
    "fingerprint",
    "generation",
    "entityToken",
    "receipt",
    "needsAttention",
)


@dataclass(frozen=True, slots=True)
class TargetHalf:
    """One side's **report** of the target half of a chain.

    This is a claim the caller carries, not remote proof: the channel that
    would make it proof (a verified identity between the two daemons) is not
    wired yet, so this sits at the same trust tier as the P0 flow, where the
    source completes on the orchestrator's own bookkeeping and the target's
    confirmation is just a receipt string.  It is a strict, checkable claim
    instead of a boolean, which is the whole point of shipping it.
    """

    operation_id: str
    admission: Admission
    row: StepState

    @property
    def binding(self) -> Binding:
        return self.admission.binding


def half_to_json(half: TargetHalf) -> dict[str, object]:
    """Project one reported half onto the wire, field for field."""

    binding = half.binding
    return {
        "version": REPORT_VERSION,
        "operationId": half.operation_id,
        "admission": {
            "operationId": half.admission.operation_id,
            "binding": {
                "role": binding.role.value,
                "owner": binding.owner,
                "sourceNode": binding.source_node,
                "sourceAgentId": binding.source_agent_id,
                "sourceEntityToken": binding.source_entity_token,
                "targetNode": binding.target_node,
                "targetActor": binding.target_actor,
                "planDigest": binding.plan_digest,
            },
            "createdAtMs": half.admission.created_at_ms,
            "expiresAtMs": half.admission.expires_at_ms,
            "revokedAtMs": half.admission.revoked_at_ms,
            "cancelledAtMs": half.admission.cancelled_at_ms,
        },
        "row": {
            "operationId": half.row.operation_id,
            "actor": half.row.actor,
            "phase": half.row.phase,
            "step": half.row.step,
            "fingerprint": half.row.fingerprint,
            "generation": half.row.generation,
            "entityToken": half.row.entity_token,
            "receipt": half.row.receipt,
            "needsAttention": half.row.needs_attention,
        },
    }


def _exact(payload: object, keys: tuple[str, ...], label: str) -> dict[str, object]:
    """A record with exactly these keys.  Anything else is refused, never filled."""

    if not isinstance(payload, dict):
        raise ChainPlanError(f"{label} must be an object")
    missing = sorted(set(keys) - set(payload))
    extra = sorted(set(payload) - set(keys))
    if missing or extra:
        raise ChainPlanError(
            f"{label} must carry exactly {sorted(keys)}; missing={missing} extra={extra}"
        )
    return payload


def _text(payload: dict[str, object], key: str, label: str) -> str:
    value = payload[key]
    if not isinstance(value, str):
        raise ChainPlanError(f"{label}.{key} must be a string")
    return value


def _optional_text(payload: dict[str, object], key: str, label: str) -> str | None:
    value = payload[key]
    if value is None:
        return None
    if not isinstance(value, str):
        raise ChainPlanError(f"{label}.{key} must be a string or null")
    return value


def _integer(payload: dict[str, object], key: str, label: str) -> int:
    value = payload[key]
    if isinstance(value, bool) or not isinstance(value, int):
        raise ChainPlanError(f"{label}.{key} must be an integer")
    return value


def half_from_json(payload: object, *, operation_id: str) -> TargetHalf:
    """Parse a reported half, refusing anything that is not exactly the shape.

    No field has a default: a missing key, an extra key, a wrong type, the
    wrong role, a row that belongs to another operation, a row whose actor is
    not this binding's target, a row that is not one of the target's own steps,
    a generation that is not a real attempt, or an admission that is already
    cancelled or revoked are all refusals.  A damaged report is never read as
    "no report".

    The digest anchor is the reported *binding*, not the row: the row's
    fingerprint is whichever step ran last, so ``_same_plan`` joining the two
    bindings is what keeps a stale or foreign report out of the decision.
    """

    report = _exact(payload, ("version", "operationId", "admission", "row"), "report")
    if _integer(report, "version", "report") != REPORT_VERSION:
        raise ChainPlanError(
            f"report.version must be {REPORT_VERSION}; a newer shape is not readable here"
        )
    if _text(report, "operationId", "report") != operation_id:
        raise ChainPlanError(
            "the reported half belongs to another operation"
        )
    raw_admission = _exact(report["admission"], _ADMISSION_KEYS, "report.admission")
    if _text(raw_admission, "operationId", "report.admission") != operation_id:
        raise ChainPlanError("the reported admission belongs to another operation")
    raw_binding = _exact(
        raw_admission["binding"], _BINDING_KEYS, "report.admission.binding"
    )
    try:
        binding = Binding(
            role=Role(_text(raw_binding, "role", "report.admission.binding")),
            owner=_text(raw_binding, "owner", "report.admission.binding"),
            source_node=_text(raw_binding, "sourceNode", "report.admission.binding"),
            source_agent_id=_text(
                raw_binding, "sourceAgentId", "report.admission.binding"
            ),
            source_entity_token=_text(
                raw_binding, "sourceEntityToken", "report.admission.binding"
            ),
            target_node=_text(raw_binding, "targetNode", "report.admission.binding"),
            target_actor=_text(raw_binding, "targetActor", "report.admission.binding"),
            plan_digest=_text(raw_binding, "planDigest", "report.admission.binding"),
        )
    except ValueError as error:
        raise ChainPlanError(f"the reported binding is not a binding: {error}") from error
    if binding.role is not Role.TARGET:
        raise ChainPlanError("a reported half must be the target role")
    admission = Admission(
        operation_id=operation_id,
        binding=binding,
        created_at_ms=_integer(raw_admission, "createdAtMs", "report.admission"),
        expires_at_ms=_integer(raw_admission, "expiresAtMs", "report.admission"),
        revoked_at_ms=_optional_text(raw_admission, "revokedAtMs", "report.admission"),
        cancelled_at_ms=_optional_text(raw_admission, "cancelledAtMs", "report.admission"),
    )
    raw_row = _exact(report["row"], _ROW_KEYS, "report.row")
    if _text(raw_row, "operationId", "report.row") != operation_id:
        raise ChainPlanError("the reported row belongs to another operation")
    needs_attention = raw_row["needsAttention"]
    if not isinstance(needs_attention, bool):
        raise ChainPlanError("report.row.needsAttention must be a boolean")
    row = StepState(
        operation_id=operation_id,
        actor=_text(raw_row, "actor", "report.row"),
        phase=_text(raw_row, "phase", "report.row"),
        step=_text(raw_row, "step", "report.row"),
        fingerprint=_text(raw_row, "fingerprint", "report.row"),
        generation=_integer(raw_row, "generation", "report.row"),
        entity_token=_text(raw_row, "entityToken", "report.row"),
        receipt=_text(raw_row, "receipt", "report.row"),
        needs_attention=needs_attention,
    )
    if row.generation < 1:
        raise ChainPlanError("report.row.generation must be a real attempt")
    if row.actor != binding.target_actor:
        raise ChainPlanError("report.row.actor is not this binding's target actor")
    if row.step not in ("land", "activate"):
        # A row that is not one of the target's own steps is some other
        # operation's row wearing this one's id.
        raise ChainPlanError(
            f"report.row.step {row.step!r} is not a target step of this chain"
        )
    if admission.cancelled_at_ms is not None or admission.revoked_at_ms is not None:
        raise ChainPlanError("the reported target admission is no longer live")
    return TargetHalf(operation_id=operation_id, admission=admission, row=row)


def decide_with_shipped_target(
    operation_id: str,
    *,
    source_store: AdmissionStore,
    target_half: TargetHalf,
    source_live: SourceLiveness,
) -> ChainResume:
    """Decide from this daemon's own ledger plus a reported target half.

    The rules are ``chain``'s own: this only says where the four rows come
    from.  The seven-field join, and every source-side refusal, therefore stay
    in force -- a well-formed target half can satisfy the target-side rules and
    can never let the source skip a rule of its own.
    """

    return decide_from_rows(
        operation_id,
        source_admission=read_admission(source_store, operation_id),
        source_row=read_row(source_store, operation_id),
        target_admission=target_half.admission,
        target_row=target_half.row,
        source_live=source_live,
    )
