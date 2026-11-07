"""The §C.1 periodic reconciliation: one test per judge, mutation-proven.

docs/design/design-assign.md §C.1: a periodic pass re-reads each live assign's
source and stamps ``released_at_ms`` on the dead ones.  §C.1.1 is the half
that keeps it from mis-killing: 「查不到」must be visible **unknown**, never
a quiet death sentence -- a missed release is reclaimed next pass, a mis-kill
is not recoverable, so every ambiguous branch must land on "not dead".

Every test here runs against the real ``AssignStore``/``RoutineStore``
(the same shapes production reads and writes); no test fabricates a row
shape the production code could not produce.  The one raw INSERT seeds a
routine-kind row, whose shape is the §B.1 schema itself -- no producer
exists yet (assign.py), which is exactly why the table must be written
directly to keep the routine branch of the judge in front of a real row.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from hyprial.daemon.impl.pac.actors.reconcile import (
    AssignReconcileReport,
    AssignSourceVerdict,
    run_reconciliation_pass,
)
from hyprial.biz.impl.routine.storage.store import RoutineStore
from enum import StrEnum
from hyprial.daemon.impl.pac.storage.assignments import AssignStore

class RunState(StrEnum):
    RUNNING = "running"
    COMPLETED = "completed"
    CANCELLED = "cancelled"

SRC = Path(__file__).resolve().parents[4] / 'src/hyprial'

DOC = """
version: 1
name: assign-reconcile
task: "PING {{nonce}}"
targets: [w1]
await: {kind: ack, timeout: 60s}
report_to: user:allen
"""

WORKER = "agent:bobadmin:node-a:w1"
RUN = "run-1"
NOW = 5_000


def _stores(tmp_path: Path) -> tuple[AssignStore, RoutineStore, Path]:
    db = tmp_path / "workflows.sqlite3"
    store = AssignStore(db)
    store._db.execute("CREATE TABLE legacy_states(run_id TEXT PRIMARY KEY,state TEXT)")
    store._db.commit()
    routines = RoutineStore(tmp_path / "routines.sqlite3")
    return store, routines, db


def _save_run(store: AssignStore, state: RunState) -> None:
    with store._db:
        store._db.execute("INSERT OR REPLACE INTO legacy_states VALUES (?,?)", (RUN, state.value))


def _live_workflow_row(store: AssignStore, run_id: str = RUN) -> None:
    store.record_workflow_assign(actor=WORKER, run_id=run_id, assigned_at_ms=2)


def _live_routine_row(db: Path, name: str) -> None:
    """Seed one routine-kind assign row -- the §B.1 schema, written directly.

    record_workflow_assign hardcodes the workflow kind on purpose (A1 pinned
    the registry as its only writer), and nothing writes routine rows yet;
    inserting through the table's real DDL is the only honest way to put the
    routine branch of the judge in front of a real row.
    """

    raw = sqlite3.connect(db)
    try:
        with raw:
            raw.execute(
                "INSERT INTO actor_assign (actor, assign_kind, assign_ref,"
                " assigned_at_ms) VALUES (?, 'routine', ?, 2)",
                (WORKER, name),
            )
    finally:
        raw.close()


def _released_at(store: AssignStore, kind: str = "workflow", ref: str = RUN) -> int | None:
    rows = store.assign_rows(actor=WORKER)
    matches = [row[2] for row in rows if row[0] == kind and row[1] == ref]
    assert len(matches) == 1, f"expected exactly one {kind}/{ref} row, got {rows}"
    return matches[0]


def _probe(routines: RoutineStore):
    return lambda name: routines.get_routine(name) is not None


def _run_state(store: AssignStore):
    def read(run_id):
        row = store._db.execute("SELECT state FROM legacy_states WHERE run_id=?", (run_id,)).fetchone()
        return row[0] if row else None
    return read


def _pass(store, routines, *, sink=None, now_ms=NOW):
    return run_reconciliation_pass(
        store=store,
        run_state=_run_state(store),
        routine_exists=None if routines is None else _probe(routines),
        sink=sink,
        now_ms=now_ms,
    )


# ── 判据一:run 处于终态 ⇒ 释放 ────────────────────────────────────────────


def test_a_run_in_a_terminal_state_releases_its_assign(tmp_path: Path) -> None:
    store, routines, _ = _stores(tmp_path)
    try:
        # The drift this pass exists for: the run reached its terminal state,
        # and the assign row arrived AFTER that (a late delivery-completion
        # re-live's the row via record's ON CONFLICT) -- no terminal
        # transition will ever fire again, so the pass is the only writer
        # left that can stamp it.
        _save_run(store, RunState.RUNNING)
        _save_run(store, RunState.COMPLETED)
        _live_workflow_row(store)
        assert _released_at(store) is None

        report = _pass(store, routines)

        assert report.dead == ((WORKER, "workflow", RUN),)
        assert _released_at(store) == NOW
        assert store.live_assigns(actor=WORKER) == ()
    finally:
        store.close()
        routines.close()


def test_a_cancelled_run_releases_its_assign_too(tmp_path: Path) -> None:
    """The 45-hour shape (§0.0①): cancel 也不执行收尾,核对必须替它收。"""

    store, routines, _ = _stores(tmp_path)
    try:
        _save_run(store, RunState.CANCELLED)
        _live_workflow_row(store)

        report = _pass(store, routines)

        assert report.dead == ((WORKER, "workflow", RUN),)
        assert _released_at(store) == NOW
    finally:
        store.close()
        routines.close()


# ── 判据二:run 还在跑 ⇒ 不动 ─────────────────────────────────────────────


def test_a_running_run_does_not_release_its_assign(tmp_path: Path) -> None:
    store, routines, _ = _stores(tmp_path)
    try:
        _save_run(store, RunState.RUNNING)
        _live_workflow_row(store)

        report = _pass(store, routines)

        assert report.dead == () and report.unknown == ()
        assert report.alive == 1
        assert _released_at(store) is None, (
            "a RUNNING run's assign was released; the run is still doing the "
            "work and this release is the mis-kill §C.1.1 exists to prevent"
        )
        assert store.live_assigns(actor=WORKER) == (("workflow", RUN),)
        # §C.1 step 2 stays gated (E5): a pass that only stamps rows must
        # carry the reason actor-release is still blocked, visibly.
        assert report.actor_release_blocked is not None
    finally:
        store.close()
        routines.close()


# ── 判据三:查不到 ⇒ unknown,可见,不释放(§C.1.1)─────────────────────────


def test_a_run_that_cannot_be_found_is_unknown_visible_and_kept(
    tmp_path: Path,
) -> None:
    store, routines, _ = _stores(tmp_path)
    try:
        # No run row at all: could be another node's run, could be retention.
        # None of that is a terminal state, so nothing may be released.
        _live_workflow_row(store, "run-from-elsewhere")
        seen: list[AssignReconcileReport] = []

        report = _pass(store, routines, sink=seen.append)

        assert report.dead == (), (
            "a not-found run was judged dead; 「查不到」≠「已死」(§C.1.1) -- "
            "absence is not a terminal state and must never release"
        )
        assert report.unknown == ((WORKER, "workflow", "run-from-elsewhere"),)
        assert _released_at(store, "workflow", "run-from-elsewhere") is None
        # 且可见: the sink received the same report, unknowns included --
        # a report nobody emits is not visibility.
        assert len(seen) == 1 and seen[0].unknown == report.unknown
    finally:
        store.close()
        routines.close()


def test_a_failed_lookup_is_unknown_not_dead(tmp_path: Path) -> None:
    """§C.1.1②: 查询没成功 ⛔ 不算死 -- storage trouble is exactly when a
    batch mis-kill would happen, so the pass declines to judge instead."""

    store, routines, db = _stores(tmp_path)
    try:
        _save_run(store, RunState.RUNNING)
        _live_workflow_row(store)
        _live_routine_row(db, "routine-x")

        def broken_probe(name: str) -> bool:
            raise sqlite3.OperationalError("database is locked")

        report = run_reconciliation_pass(
            store=store,
            run_state=_run_state(store),
            routine_exists=broken_probe,
            sink=None,
            now_ms=NOW,
        )

        assert report.dead == ()
        assert (WORKER, "routine", "routine-x") in report.unknown, (
            "a failed lookup must land in unknown (visible, not released), "
            "not in dead -- the two 「查不到」cases must not collapse"
        )
        assert _released_at(store) is None
        assert _released_at(store, "routine", "routine-x") is None
    finally:
        store.close()
        routines.close()


# ── 判据四:routine 明确停止 ⇒ 释放;暂停 ≠ 停止 ──────────────────────────


def test_a_removed_routine_releases_its_assign(tmp_path: Path) -> None:
    """§C.1.1①: a successful query finding nothing IS the deletion -- a
    removed routine leaves no terminal record, so absence here is the
    decision that killed it (明确停止)."""

    store, routines, db = _stores(tmp_path)
    try:
        routines.upsert_routine(
            name="daily",
            yaml_text="interval: 60s",
            owner="user:allen",
            enabled=True,
            next_due_ms=1,
            created_at_ms=1,
        )
        _live_routine_row(db, "daily")

        alive_report = _pass(store, routines)
        assert alive_report.dead == () and alive_report.alive == 1
        assert _released_at(store, "routine", "daily") is None

        assert routines.remove_routine("daily") is True
        removed_report = _pass(store, routines, now_ms=NOW + 1)

        assert removed_report.dead == ((WORKER, "routine", "daily"),)
        assert _released_at(store, "routine", "daily") == NOW + 1
    finally:
        store.close()
        routines.close()


def test_a_paused_routine_is_not_stopped(tmp_path: Path) -> None:
    """§G2: disable 是暂停,退役是另一个决定 -- enabled=0 绝不能当死."""

    store, routines, db = _stores(tmp_path)
    try:
        routines.upsert_routine(
            name="paused",
            yaml_text="interval: 60s",
            owner="user:allen",
            enabled=False,
            next_due_ms=1,
            created_at_ms=1,
        )
        _live_routine_row(db, "paused")

        report = _pass(store, routines)

        assert report.dead == ()
        assert report.alive == 1
        assert _released_at(store, "routine", "paused") is None
    finally:
        store.close()
        routines.close()


# ── 幂等与隔离 ────────────────────────────────────────────────────────────


def test_repeated_passes_do_not_move_the_release_timestamp(tmp_path: Path) -> None:
    store, routines, _ = _stores(tmp_path)
    try:
        _save_run(store, RunState.COMPLETED)
        _live_workflow_row(store)
        _pass(store, routines)
        first = _released_at(store)

        _pass(store, routines, now_ms=NOW + 9_999)

        assert _released_at(store) == first, (
            "a second pass moved an already-released row's timestamp; the "
            "release must be idempotent (an already-released row keeps its "
            "original stamp)"
        )
    finally:
        store.close()
        routines.close()


def test_a_failing_pass_becomes_an_error_report_and_releases_nothing(
    tmp_path: Path,
) -> None:
    store, routines, _ = _stores(tmp_path)
    try:
        _live_workflow_row(store)
        seen: list[AssignReconcileReport] = []
        store.close()  # the pass must survive its own store being gone

        report = run_reconciliation_pass(
            store=store,
            run_state=_run_state(store),
            routine_exists=_probe(routines),
            sink=seen.append,
            now_ms=NOW,
        )

        assert report.error is not None
        assert report.dead == () and report.unknown == ()
        assert seen and seen[0].error is not None, (
            "a failed pass must reach the sink as an error report -- silent "
            "skip and raised exception are both wrong (the first hides, the "
            "second kills the timer that active runs depend on)"
        )
    finally:
        routines.close()


def test_a_raising_sink_cannot_undo_or_block_a_release(tmp_path: Path) -> None:
    """§C.3: hook 失败不影响回收 -- the observer runs after the stamps."""

    store, routines, _ = _stores(tmp_path)
    try:
        _save_run(store, RunState.COMPLETED)
        _live_workflow_row(store)

        def exploding_sink(report: AssignReconcileReport) -> None:
            raise RuntimeError("observer is down")

        report = run_reconciliation_pass(
            store=store,
            run_state=_run_state(store),
            routine_exists=_probe(routines),
            sink=exploding_sink,
            now_ms=NOW,
        )

        assert report.dead == ((WORKER, "workflow", RUN),)
        assert _released_at(store) == NOW
    finally:
        store.close()
        routines.close()


# ── 可达性:生产组装点真的会跑这个 pass(E1 的教训)────────────────────────






# ── 判定函数本身:三态表,直接钉死,防「顺手简化」─────────────────────────


def test_the_verdict_table_has_no_ambiguous_branch_landing_on_dead() -> None:
    """Each §C.1.1 branch, judged directly -- the table the mutation targets."""

    verdict = AssignSourceVerdict

    def running_run(run_id: str) -> str:
        return "running"

    def completed_run(run_id: str) -> str:
        return "completed"

    def missing_run(run_id: str) -> None:
        return None

    def raising(run_id: str) -> str:
        raise RuntimeError("down")

    assert (
        judge("workflow", run_state=completed_run, routine_exists=None)
        is verdict.DEAD
    )
    assert (
        judge("workflow", run_state=running_run, routine_exists=None)
        is verdict.ALIVE
    )
    assert (
        judge("workflow", run_state=missing_run, routine_exists=None)
        is verdict.UNKNOWN
    )
    assert judge("workflow", run_state=raising, routine_exists=None) is verdict.UNKNOWN
    # 没有 probe 的 routine 边:查不成 ⇒ 活着,永远不因「看不见」而判死
    assert judge("routine", run_state=None, routine_exists=None) is verdict.UNKNOWN


def judge(kind, *, run_state, routine_exists):
    from hyprial.daemon.impl.pac.actors.reconcile import judge_assign_source

    return judge_assign_source(
        kind, "ref", run_state=run_state, routine_exists=routine_exists
    )
