"""AT07 I3: strict recovery for one P2 chain, read from its two ledgers.

A P2 chain has exactly two owners and therefore two ledgers:

* the **source** daemon records the ``quiesce`` step that holds the source
  incarnation in place (C2);
* the **target** daemon records ``land`` and ``activate`` for the same
  ``operation_id`` and the same plan digest.

Nothing in either daemon can see the other's disk, so "may the source complete
now?" is answered from durable rows plus one live observation.  This module is
that answer, and it is deliberately read-only: it never records a step, never
releases custody, and never completes anything.  Its whole job is to make the
resumption decision *and its refusals* explicit, so a crash window cannot be
closed by quietly assuming the other side finished.

Fail-closed rules, in order (first match wins):

0. a ledger row is corrupt                     -> raise (never "absent")
1. no source intent at all                      -> hold, ``no-quiesce-intent``
2. either ledger carries ``needs_attention``    -> hold, ``attention:<step>``
3. the source has not recorded ``quiesced``     -> hold, ``source-not-quiesced``
4. no target admission for this operation      -> hold, ``target-not-admitted``
5. the two admissions are not one plan          -> hold, ``plan-mismatch``
6. the report is older than the source's own
   attempt for this chain                       -> hold, ``stale-target-half``
7. no target ``land`` row                       -> hold, ``target-not-landed``
8. target ``land`` row still pending            -> hold, ``target-landing-unresolved``
9. target has not recorded ``active``           -> hold, ``target-not-active``
10. source liveness is unknown                  -> hold, ``liveness-unknown``
11. source incarnation is still running         -> hold, ``dual-active``
12. otherwise                                   -> complete

Rule 5 is the join: both sides must carry the same binding except for the
role, which is what makes "the target's proof" and "the source's hold"
statements about one chain instead of two similar ones.  Rule 0 is the other
half of that discipline: a *corrupt* row or binding is raised, never read as
"this side has nothing" -- a damaged target ledger must not invite a fresh
landing, and a damaged source ledger must not invite a fresh preparation.
Only ``binding-mismatch`` ("this chain is not mine to read") is folded into
"absent".  Rule 6 is the staleness guard on the reported half: a
report from an attempt older than the source's own current attempt is refused
(head of the rule), and so is a report whose own step and generation disagree
(an ``activate`` cannot be the target's first attempt, a ``land`` cannot be a
later one).  The rule is deliberately one-way -- it can only refuse -- because
an old "the target is active" must not come back and unlock a newer attempt.
Rule 11 is the card's
anti-dual-active invariant as a decision: the target
holding the agent while the source is still live is *reported*, never rounded
down to "complete".  Rule 9 is the same discipline for an unreadable probe:
"we could not tell" is not "it is stopped".
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from hyprial.daemon.impl.transfer.p2.model import AdmissionConflict, Reason, Role
from hyprial.daemon.impl.transfer.p2.store import AdmissionStore

HOLD = "hold"
COMPLETE = "complete"

#: The step names this decision reads, in the order a chain creates them.
SOURCE_STEP = "quiesce"
TARGET_STEPS = ("land", "activate")


class SourceLiveness(Protocol):
    """Observe whether one source incarnation is still running.

    ``True`` = running, ``False`` = observed stopped, ``None`` = unknown.
    Only a direct ``False`` may unlock the source's completion.
    """

    def __call__(self, actor: str, entity_token: str) -> bool | None: ...


@dataclass(frozen=True, slots=True)
class ChainResume:
    """The decision for one chain, with the rows it was made from."""

    operation_id: str
    action: str
    reason: str
    source_phase: str | None
    target_phase: str | None
    source_entity_token: str | None

    @property
    def may_complete(self) -> bool:
        return self.action == COMPLETE

    def to_json(self) -> dict[str, object]:
        return {
            "operationId": self.operation_id,
            "action": self.action,
            "reason": self.reason or None,
            "sourcePhase": self.source_phase,
            "targetPhase": self.target_phase,
        }


def _admission(store: AdmissionStore, operation_id: str):
    try:
        return store.get(operation_id)
    except AdmissionConflict as error:
        # "Not admitted here" is not this reader's chain; a corrupt row or
        # binding is a different fact and must stay loud.
        if error.reason is Reason.CORRUPT_RECORD:
            raise
        return None


def _row(store: AdmissionStore, operation_id: str):
    try:
        return store.journal.get(operation_id)
    except AdmissionConflict as error:
        if error.reason is Reason.CORRUPT_RECORD:
            raise
        return None


_CHAIN_FIELDS = (
    "owner",
    "source_node",
    "source_agent_id",
    "source_entity_token",
    "target_node",
    "target_actor",
    "plan_digest",
)


def read_admission(store: AdmissionStore, operation_id: str):
    """The admission this ledger holds, or ``None`` when it is not this chain.

    A corrupt row is raised, not folded into "absent" -- the same rule the
    decision itself uses, exposed so a caller that reads the rows itself cannot
    accidentally use a laxer one.
    """

    return _admission(store, operation_id)


def read_row(store: AdmissionStore, operation_id: str):
    """The journal row this ledger holds, or ``None`` when it is not this chain."""

    return _row(store, operation_id)


def _same_plan(source_binding: object, target_binding: object) -> bool:
    """Both sides describe one chain: same fields, opposite roles."""

    if source_binding.role is not Role.SOURCE or target_binding.role is not Role.TARGET:
        return False
    return all(
        getattr(source_binding, name) == getattr(target_binding, name)
        for name in _CHAIN_FIELDS
    )


def _is_stale(source, target) -> bool:
    """Is this report older than the attempt the source is on?

    ``generation`` is each side's own attempt counter, so the comparison at the
    head is one-way and can only refuse: the source's own step is a single
    attempt per operation today (``journal.begin`` returns the stored step
    instead of opening a new generation), so that arm is latent until a source
    retry path exists -- it is stated here so a future retry cannot silently
    unlock on an old report.

    The second arm is live today and needs no other ledger: the report carries
    its own counter, and the target's later step cannot have happened before its
    earlier one.  An ``activate`` row reported at the target's first attempt, or
    a ``land`` row reported at a later one, is a report about an attempt that is
    not this chain's.
    """

    if target.generation < source.generation:
        return True
    if target.step == "activate":
        return target.generation < 2
    return target.generation != 1


def evaluate_chain(
    operation_id: str,
    *,
    source_store: AdmissionStore,
    target_store: AdmissionStore,
    source_live: SourceLiveness,
) -> ChainResume:
    """Decide from two local ledgers.  Pure read: nothing is written.

    This is the reader, not the rules: it fetches the four rows and hands them
    to ``decide_from_rows``.  A caller that holds one ledger locally and the
    other as a reported half uses that function directly, so there is still one
    rule set and no second statement of it.
    """

    return decide_from_rows(
        operation_id,
        source_admission=_admission(source_store, operation_id),
        source_row=_row(source_store, operation_id),
        target_admission=_admission(target_store, operation_id),
        target_row=_row(target_store, operation_id),
        source_live=source_live,
    )


def decide_from_rows(
    operation_id: str,
    *,
    source_admission: object,
    source_row: object,
    target_admission: object,
    target_row: object,
    source_live: SourceLiveness,
) -> ChainResume:
    """The fail-closed rules, over four rows.  Pure: nothing is written."""

    source = source_row
    target = target_row

    def decision(action: str, reason: str) -> ChainResume:
        return ChainResume(
            operation_id=operation_id,
            action=action,
            reason=reason,
            source_phase=None if source is None else source.phase,
            target_phase=None if target is None else target.phase,
            source_entity_token=(
                None
                if source_admission is None
                else source_admission.binding.source_entity_token
            ),
        )

    if source_admission is None or source_admission.binding.role is not Role.SOURCE:
        return decision(HOLD, "no-quiesce-intent")
    if source is None:
        return decision(HOLD, "no-quiesce-intent")
    if source.needs_attention:
        return decision(HOLD, f"attention:{source.step}")
    if target is not None and target.needs_attention:
        return decision(HOLD, f"attention:{target.step}")
    if source.phase != "quiesced":
        return decision(HOLD, "source-not-quiesced")
    if target_admission is None or target_admission.binding.role is not Role.TARGET:
        return decision(HOLD, "target-not-admitted")
    if not _same_plan(source_admission.binding, target_admission.binding):
        return decision(HOLD, "plan-mismatch")
    if target is not None and target.step != SOURCE_STEP and _is_stale(source, target):
        # One-way on purpose: this rule can only refuse.  A half carried back
        # from an attempt older than the source's own current one may describe
        # a target that has since been re-landed or rolled back, and must not
        # unlock this attempt just because it once said "active".
        return decision(HOLD, "stale-target-half")
    if target is None or target.step == SOURCE_STEP:
        return decision(HOLD, "target-not-landed")
    if target.phase == "land-pending":
        return decision(HOLD, "target-landing-unresolved")
    if target.phase != "active":
        return decision(HOLD, "target-not-active")

    live = source_live(source.actor, source_admission.binding.source_entity_token)
    if live is None:
        return decision(HOLD, "liveness-unknown")
    if live:
        return decision(HOLD, "dual-active")
    return decision(COMPLETE, "")
