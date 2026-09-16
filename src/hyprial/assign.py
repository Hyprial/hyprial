"""What an assign row means, and which kinds actually get written today.

``actor_assign`` answers "why does this actor still exist?" -- the edge a
future reclamation pass (docs/design-assign.md §C) reads before releasing
anything.  This module holds the two things that are *not* rows: the closed
set of assign kinds, and, for each kind, whether any code path writes it.

That second part exists because the table has a third state the schema cannot
express.  ``released_at_ms`` distinguishes "never assigned" (no rows -- an
actor standing by) from "assigned and finished" (rows, all released -- a
reclaim candidate).  But a kind that *nobody writes* produces no rows either,
and in the table that is indistinguishable from "never assigned" while
demanding the opposite treatment: standing by is a verdict, whereas an
unwritten kind means **we do not know**.

Recording it here rather than in a comment is deliberate: reclamation must be
able to *read* the fact, not depend on someone remembering it.
"""

from __future__ import annotations

from dataclasses import dataclass

#: Every value ``actor_assign.assign_kind`` may hold.  Closed on purpose: a new
#: kind must be declared below, which forces its producer question to be
#: answered rather than defaulted.
ASSIGN_KINDS: tuple[str, ...] = ("workflow", "routine")

#: Persisted states that prove a legacy workflow assign source is finished.
#: This belongs to the shared ledger contract because both the inline writer
#: and the periodic reconciler must use exactly the same closed set.
WORKFLOW_TERMINAL_STATES = frozenset({"completed", "cancelled"})


@dataclass(frozen=True, slots=True)
class AssignProducer:
    """Who writes this kind of assign row, if anyone does."""

    kind: str
    #: Dotted module that writes rows of this kind; ``None`` when nothing does.
    writer: str | None
    note: str

    @property
    def exists(self) -> bool:
        return self.writer is not None


ASSIGN_PRODUCERS: dict[str, AssignProducer] = {
    "workflow": AssignProducer(
        kind="workflow",
        writer="hyprial.workflow.registry",
        note=(
            "Written when a deliver:target effect reports success, from the "
            "recipient the delivery actually resolved to. The row therefore "
            "appears when the work reached the actor, not when the run "
            "intended to send it -- a target that was never successfully "
            "delivered to is not doing the work and has no assign."
        ),
    ),
    "routine": AssignProducer(
        kind="routine",
        writer=None,
        note=(
            "Nothing writes routine assigns. A routine picks a target per "
            "task and dispatches a run, so the actor gets a *workflow* "
            "assign; the only standing actor<->routine link is "
            "routines.owner, which design-assign.md §B.4 keeps as a separate "
            "concept (who is responsible) from assign (why this actor "
            "exists). Until a producer exists, an actor holding only "
            "workflow assigns cannot be judged: it may be a worker whose run "
            "ended, or an agent whose routine edge was never recorded."
        ),
    ),
}


def kinds_without_producer() -> tuple[str, ...]:
    """Assign kinds no code path writes -- reclamation must respect these."""

    return tuple(
        kind for kind in ASSIGN_KINDS if not ASSIGN_PRODUCERS[kind].exists
    )


def reclamation_blocked_reason() -> str | None:
    """Why a reclamation pass must not release actors yet, or ``None``.

    The future E4/E5 passes call this *before* judging anything.  While any
    kind lacks a producer, "this actor has no live assign" cannot be
    distinguished from "this actor's kind of assign is never recorded", and
    releasing on that basis is the mis-kill the whole mechanism exists to
    prevent (design-assign.md §0.0).

    ⚠️ A consequence worth stating loudly where someone will read it: while
    this returns a reason, reclamation correctly does **nothing at all**. That
    is the safe default, not a broken pass -- and a mechanism that correctly
    does nothing is the easiest thing in the world for a well-meaning person
    to "fix".
    """

    missing = kinds_without_producer()
    if not missing:
        return None
    details = "; ".join(f"{kind}: {ASSIGN_PRODUCERS[kind].note}" for kind in missing)
    return (
        "assign kinds with no producer, so absence of rows proves nothing: "
        f"{', '.join(missing)} -- {details}"
    )
