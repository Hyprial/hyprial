"""The §C.1 periodic reconciliation: judge every live assign's source, release the dead.

docs/design-assign.md §C.1 is explicit that reclamation is a *check*, not a
finalization step: cancel / timeout / crash / kill none of them run cleanup
code, so the only trustworthy input is the source's **current observed
state**, re-read every tick -- never a memory of who promised to notify us.
The 45-hour idle burn (§0.0①) happened because nothing ever re-read; this
pass is the thing that re-reads.

Per pass (throttled to :data:`ASSIGN_RECONCILE_INTERVAL_MS`, driven by the
workflow timer the daemon maintenance tick already submits -- no new thread):

1. scan every ``actor_assign`` row with ``released_at_ms IS NULL``;
2. judge its source: a workflow assign looks up the run's current state, a
   routine assign asks whether the routine still exists;
3. stamp ``released_at_ms`` on the dead ones (idempotent), leave the rest.

§C.1.1 is the part that must never be "simplified" while implementing this:

* **失踪 ≠ 终止.** A run this node cannot find is *not* a dead run: the row
  may belong to a run that lived elsewhere (跨机器 assign, §I) or to history
  no store still holds.  ``查不到`` records **unknown**, and unknown releases
  nothing -- it is made visible on the report instead, so the gap is seen
  rather than silently converted into a release.
* 「查不到」has two sources that look identical in an empty result: the
  source is really gone, or *this lookup failed*.  The store layer separates
  them (a successful query returning nothing vs. a raised error), and where
  they cannot be separated the verdict is **alive**.  The costs are
  asymmetric (§C.1.1): a missed release is reclaimed next pass, a mis-kill
  is not recoverable, so the default must lean toward not releasing.

Why the two kinds disagree about absence -- and must:

* kind=routine: a successful query that finds no routine *is* the deletion
  (§C.1.1①: a removed routine leaves no terminal record; refusing to read
  absence would make deletion the one path that never reclaims).  That is
  「明确停止」: it took a decision to make the row disappear.  ``enabled=0``
  is *not* that decision -- disable is a pause, retire is unresolved (§G2) --
  so a paused routine stays alive here.
* kind=workflow: a run's absence carries no such decision.  Runs are the
  one-shot carrier, may belong to another node, and old rows are subject to
  retention; the brief for this mechanism pins it outright: run 查不到 ⇒
  unknown, never dead.  Only an **observed** terminal state
  (``WORKFLOW_TERMINAL_STATES``: completed / cancelled) kills a workflow assign.

The report surfaces :func:`hyprial.assign.reclamation_blocked_reason` so nobody
has to remember the other half of the boundary: this pass stamps **rows**
(§C.1 step 1).  It does not release *actors* (§C.1 step 2 / E5) -- that
verdict reasons from "no live rows", which is exactly the inference that
proves nothing while a kind without a producer exists, and it stays gated.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum

from hyprial.assign import WORKFLOW_TERMINAL_STATES, reclamation_blocked_reason
from hyprial.assign_store import AssignLedger

#: How often the reconciliation actually runs, in ms of source time.  The
#: workflow timer fires every maintenance tick (~1s); re-reading run states
#: that often buys nothing -- the 45-hour incident had 45 hours of margin.
ASSIGN_RECONCILE_INTERVAL_MS = 30_000


class AssignSourceVerdict(StrEnum):
    """What the current pass observed about one live assign's source."""

    DEAD = "dead"
    ALIVE = "alive"
    UNKNOWN = "unknown"


#: ``run_state(ref) -> str | None``: the run's current state, or ``None`` when
#: no such run row exists.  A failed lookup raises -- that is the third state.
RunStateReader = Callable[[str], str | None]

#: ``routine_exists(ref) -> bool``: whether the routine exists *right now*.
#: ``False`` must mean a successful query came back empty (determinate
#: absence); a lookup that could not complete raises (⇒ unknown, never dead).
RoutineExistsProbe = Callable[[str], bool]

#: (actor, assign_kind, assign_ref) -- one live edge, exactly as the store
#: returns it.
AssignRow = tuple[str, str, str]


def judge_assign_source(
    assign_kind: str,
    assign_ref: str,
    *,
    run_state: RunStateReader,
    routine_exists: RoutineExistsProbe | None,
) -> AssignSourceVerdict:
    """§C.1 step 1 for one assign: is its source still alive?

    Every fallback in this function lands on ALIVE/UNKNOWN, never DEAD --
    §C.1.1's asymmetry lives exactly here, so this is the one place a future
    editor must not "streamline".
    """

    if assign_kind == "workflow":
        try:
            state = run_state(assign_ref)
        except Exception:
            # 查询没成功 ⛔ 不算死: storage trouble is when a batch mis-kill
            # would happen, so this pass simply declines to judge.
            return AssignSourceVerdict.UNKNOWN
        if state is None:
            # run 查不到 ≠ 已死 (§C.1.1): absence is not a terminal state.
            return AssignSourceVerdict.UNKNOWN
        if str(state) not in WORKFLOW_TERMINAL_STATES:
            return AssignSourceVerdict.ALIVE
        return AssignSourceVerdict.DEAD
    if assign_kind == "routine":
        if routine_exists is None:
            # No probe wired: the query cannot be made to succeed, which is
            # the indistinguishable case, so the row stays alive.
            return AssignSourceVerdict.UNKNOWN
        try:
            exists = routine_exists(assign_ref)
        except Exception:
            return AssignSourceVerdict.UNKNOWN
        # False = a successful query found nothing = the routine was removed
        # (§C.1.1① 明确停止).  Only here, on the routine kind, does absence
        # carry a decision -- see the module docstring for why runs differ.
        return AssignSourceVerdict.ALIVE if exists else AssignSourceVerdict.DEAD
    # An unknown kind cannot be judged against any source -- the closed kind
    # set makes this unreachable today; if it ever stops being unreachable,
    # this is the fail-safe the new kind falls into until it declares how its
    # source is observed.
    return AssignSourceVerdict.UNKNOWN


@dataclass(frozen=True, slots=True)
class AssignReconcileReport:
    """One pass, in full: what died, what could not be judged, what is alive.

    ``unknown`` is the load-bearing field: §C.1.1 demands 查不到 be *visible*,
    and a report nobody emits is not visible -- the registry hands every
    report to the injected sink, and unknowns must reach it even when the
    pass released nothing (silence is how a persistent lookup failure
    graduates from gap to mis-kill).
    """

    judged_at_ms: int
    #: Rows judged dead this pass -- these were (or are being) stamped.
    dead: tuple[AssignRow, ...] = ()
    #: 查不到: lookup failed or source row absent; released nothing, shown here.
    unknown: tuple[AssignRow, ...] = ()
    #: Rows judged still alive (count only: nothing to act on, nothing to show).
    alive: int = 0
    #: The pass itself failed before judging anything; ``dead``/``unknown``
    #: are then empty *because nothing was judged*, which the reader must be
    #: able to tell apart from "a quiet, clean pass".
    error: str | None = None
    #: Why §C.1 step 2 (releasing actors) remains gated -- from
    #: ``reclamation_blocked_reason`` so the boundary travels with the report.
    actor_release_blocked: str | None = field(
        default_factory=reclamation_blocked_reason
    )


def reconcile_assign_ledger(
    *,
    store: AssignLedger,
    run_state: RunStateReader,
    routine_exists: RoutineExistsProbe | None,
    now_ms: int,
) -> AssignReconcileReport:
    """One §C.1 pass against a store: judge every live row, stamp the dead.

    Judging happens for every row *before* anything is stamped, so the report
    describes one consistent observation rather than a mix of before/after
    states.  Stamping is idempotent (already-released rows keep their
    timestamp), and a crash between judging and stamping costs one pass of
    lag -- the next pass re-reads and re-stamps.
    """

    live = store.live_assign_rows()
    dead: list[AssignRow] = []
    unknown: list[AssignRow] = []
    alive = 0
    for row in live:
        actor, kind, ref = row
        verdict = judge_assign_source(
            kind,
            ref,
            run_state=run_state,
            routine_exists=routine_exists,
        )
        if verdict is AssignSourceVerdict.DEAD:
            dead.append(row)
        elif verdict is AssignSourceVerdict.UNKNOWN:
            unknown.append(row)
        else:
            alive += 1

    # One stamp per source, not per row: a run that delivered to three actors
    # releases three rows through a single key, exactly like the inline
    # release in ``save_run`` does.
    for kind, ref in dict.fromkeys((kind, ref) for _, kind, ref in dead):
        store.release_assigns(
            assign_kind=kind, assign_ref=ref, released_at_ms=now_ms
        )
    return AssignReconcileReport(
        judged_at_ms=now_ms,
        dead=tuple(dead),
        unknown=tuple(unknown),
        alive=alive,
    )


def run_reconciliation_pass(
    *,
    store: AssignLedger,
    run_state: RunStateReader,
    routine_exists: RoutineExistsProbe | None,
    sink: Callable[[AssignReconcileReport], None] | None,
    now_ms: int,
) -> AssignReconcileReport:
    """The pass, isolated: it cannot raise, and its observer cannot kill it.

    Two boundaries, both deliberate:

    * a failing pass (store unreachable, disk unhappy) becomes an ``error``
      report -- §C.1.1's worst case is storage trouble plus a release, so a
      broken pass must end in "nothing was judged", not in an exception
      racing through the timer that active runs depend on;
    * the sink runs *after* the stamps are committed (§C.3: hook 失败不影响
      回收 -- an observer that raises can never un-release or block a
      release; it can only lose its own line).
    """

    try:
        report = reconcile_assign_ledger(
            store=store,
            run_state=run_state,
            routine_exists=routine_exists,
            now_ms=now_ms,
        )
    except Exception as error:  # noqa: BLE001 - the pass is an isolation boundary
        report = AssignReconcileReport(
            judged_at_ms=now_ms,
            error=f"{type(error).__name__}: {error}",
        )
    if sink is not None:
        try:
            sink(report)
        except Exception:  # noqa: BLE001 - observers are best-effort by contract
            pass
    return report


def due_for_reconciliation(now_ms: int, last_due_ms: int) -> bool:
    """Throttle predicate for the per-tick hook (§C.1: 每 N 秒,不是每 tick)."""

    return now_ms >= last_due_ms
