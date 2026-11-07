"U3 acceptance: a routine task IS a PAC graph -- against real PAC.\n\n``tests/biz/routines/contracts/test_routine.py`` drives the registry through a fake dispatch seam,\nwhich proves the routine's duty cycle and nothing about PAC.  The mapping\nnote (``docs/notes/pac/u3-routine-to-pac-mapping-2026-09-17.md`` §4) requires the\nopposite: ⛔ no mocked PAC.  So every test here opens a real\n``PacGraphStore``, builds real nodes through the real write boundary, and\ncompletes a task through the real ``PacReactor``.  The only fake is the\nmessage plane (``Plane``), which is not PAC: it is the daemon's inbox, and\nwhat it must reproduce is that inbox's idempotency, not its transport.\n\nWhat each test pins, and what deleting the guarantee breaks:\n\n* the structure -- one graph per durable key, ``work`` owned by the target,\n  ``deadline`` a clock guarded by ``work`` (drop ``operation_key`` ⇒ the\n  replay test mints a second graph; drop ``guarded_by_node_id`` ⇒ the\n  flagged task still reports overdue);\n* completion is the OWNER FLAGGING the node (ruling 1 of 2026-09-13) -- no\n  text matcher exists to fool, and a non-owner cannot complete it;\n* the deadline is FIXED (ruling D): a replay never moves it, and a task\n  past it reports ``escalated`` exactly once;\n* a settled task is reported, never re-dispatched and never re-counted\n  (Allen, 2026-09-17: 「超时上报后，这个任务直接标记为失败，由被上报的\n  人/agent决定是否重排」);\n* the half-built graph a crash leaves behind converges instead of wedging.\n"

from __future__ import annotations

import ast
import inspect
import json
import sqlite3
import time
from pathlib import Path

import pytest

from hyprial.identity import PacError
from hyprial.daemon.impl.pac.graphs.edits import add_node, create_graph
from hyprial.daemon.impl.pac.graphs.reactor import PacReactor
from hyprial.daemon.impl.pac.graphs.reactor import NullSender
from hyprial.daemon.impl.pac.graphs.authority import PacGraphAuthority
from hyprial.daemon.impl.pac.storage.store import PacGraphStore, default_database_path
from hyprial.biz.impl.routine.execution.pac_dispatch import (
    STATE_RUNNING,
    PacRoutineDispatch,
    )
from hyprial.kernel import operation_key
from hyprial.kernel import WORK_NODE
from hyprial.kernel import STATE_ESCALATED
from hyprial.kernel import STATE_DONE
from hyprial.kernel import DEADLINE_NODE
from hyprial.biz.impl.routine.execution.registry import DEFAULT_TASK_TIMEOUT_SECONDS
from hyprial.biz.impl.routine.execution.service import RoutineService
from hyprial.biz.impl.routine.execution.source import SourceTask
from hyprial.biz.impl.routine.storage.store import RoutineStore

ROUTINE = "hq-selfdrive"
OWNER = "agent:o:test-node:duty"
TARGET = "agent:o:test-node:cc-1"
ESCALATE_TO = "user:allen"
TIMEOUT = DEFAULT_TASK_TIMEOUT_SECONDS  # the product default, so this mirror cannot drift from it
START_MS = 1_000_000


class Plane:
    """The message plane, with the inbox's idempotency and nothing else.

    ``DaemonApplication._deliver_routine_task`` keys the delivery on the
    conversation (``effect_id=f"routine-task:{conversation_id}"``), the io
    derives the message id from that effect id by uuid5, and the inbox's
    ``messages`` table holds ``message_id`` UNIQUE -- so a replayed dispatch
    cannot queue a second copy of the same task, across restarts included.
    ``calls`` counts attempts (what the dispatcher did); ``queued`` counts
    messages (what the target would see).
    """

    def __init__(self, *, refuse: bool = False) -> None:
        self.calls: list[dict[str, str]] = []
        self.queued: dict[str, dict[str, str]] = {}
        self.refuse = refuse

    def __call__(
        self, *, target: str, conversation_id: str, text: str, sender: str
    ) -> bool:
        record = {
            "target": target,
            "conversationId": conversation_id,
            "text": text,
            "sender": sender,
        }
        self.calls.append(record)
        if self.refuse:
            return False
        self.queued.setdefault(conversation_id, record)
        return True


class Clock:
    def __init__(self, now_ms: int = START_MS) -> None:
        self.now_ms = now_ms

    def __call__(self) -> int:
        return self.now_ms


def resolve(principal: str) -> str:
    """The daemon's resolve-or-reject, reduced to its two answers.

    A PAC owner is a principal URI (schema 8, G1=A exact equality), and the
    daemon resolves a bare local name into one or refuses.  Refusal is the
    point: a target that cannot be spelled as a URI could never authorize a
    ``flag set`` against the node, so the dispatch must fail loudly rather
    than write an owner nobody can act as.
    """

    if ":" in principal:
        return principal
    if principal == "cc-1":
        return TARGET
    raise RuntimeError(f"sender {principal!r} is not a registered agent on this node")


def make_dispatch(tmp_path: Path, *, plane: Plane | None = None, clock: Clock | None = None):
    plane = plane if plane is not None else Plane()
    clock = clock if clock is not None else Clock()
    dispatch = PacRoutineDispatch(
        state_dir=tmp_path,
        deliver=plane,
        clock_ms=clock,
        resolve_principal=resolve,
    )
    return dispatch, plane, clock


def start(
    dispatch: PacRoutineDispatch,
    *,
    uuid: str = "u1",
    target: str = TARGET,
    occurrence_slot_ms: int | None = None,
    rearm_after_created_at_ms: int = 0,
):
    return dispatch.start_idempotent(
        routine_name=ROUTINE,
        task_uuid=uuid,
        task_text="do it",
        target=target,
        escalate_to=ESCALATE_TO,
        timeout_seconds=TIMEOUT,
        sender=OWNER,
        occurrence_slot_ms=occurrence_slot_ms,
        rearm_after_created_at_ms=rearm_after_created_at_ms,
    )


def graphs(tmp_path: Path) -> list[dict[str, object]]:
    """Every graph in the store; an un-created store holds none.

    A dispatch that fails before opening PAC leaves no database at all, and
    "no database" must read as "no graph" rather than as an error -- the
    assertion is about what PAC contains, not about which of two ways it
    manages to contain nothing.
    """

    path = default_database_path(tmp_path)
    if not path.exists():
        return []
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in db.execute("SELECT * FROM graphs ORDER BY created_at")]
    except sqlite3.OperationalError:
        return []
    finally:
        db.close()


def notification_count(tmp_path: Path) -> int:
    db = sqlite3.connect(default_database_path(tmp_path))
    try:
        return int(db.execute("SELECT count(*) FROM notifications").fetchone()[0])
    finally:
        db.close()


def nodes(tmp_path: Path, graph_id: str) -> dict[str, object]:
    store = PacGraphStore(default_database_path(tmp_path))
    try:
        return {node.node_id: node for node in store.nodes(graph_id)}
    finally:
        store.close()


def flag_work(tmp_path: Path, graph_id: str, *, actor: str, clock: Clock):
    reactor = PacReactor(PacGraphStore(default_database_path(tmp_path)), clock=clock)
    try:
        return reactor.set_flag(
            graph_id, WORK_NODE, actor=actor, reason_ref=f"routine:{ROUTINE}#u1:result"
        )
    finally:
        reactor.close()


def tick(tmp_path: Path, graph_id: str, *, clock: Clock):
    reactor = PacReactor(PacGraphStore(default_database_path(tmp_path)), clock=clock)
    try:
        return reactor.tick_clocks(graph_id)
    finally:
        reactor.close()


# ── structure ──────────────────────────────────────────────────────────────


def test_one_task_is_one_activated_graph_with_a_guarded_clock(tmp_path: Path) -> None:
    dispatch, plane, clock = make_dispatch(tmp_path)
    result = start(dispatch)
    graph_id = str(result["graphId"])

    [graph] = graphs(tmp_path)
    assert graph["graph_id"] == graph_id
    assert graph["operation_key"] == operation_key(ROUTINE, "u1")
    assert graph["created_by"] == OWNER
    assert graph["activated_at"] is not None  # activation is what makes the clock tick
    assert graph["closed_at"] is None

    node = nodes(tmp_path, graph_id)
    assert set(node) == {WORK_NODE, DEADLINE_NODE}
    assert node[WORK_NODE].kind == "task"
    assert node[WORK_NODE].owner == TARGET  # the routed target, as a full URI
    assert node[WORK_NODE].flag is False
    assert node[DEADLINE_NODE].kind == "clock"
    assert node[DEADLINE_NODE].owner == ESCALATE_TO  # clock owner = 上报接收人
    assert node[DEADLINE_NODE].guarded_by_node_id == WORK_NODE
    assert node[DEADLINE_NODE].deadline_ms == START_MS + int(TIMEOUT * 1000)

    # The task text travels on the message plane because PAC notifications
    # carry a brief_ref and activation dispatches no root task.
    [message] = plane.queued.values()
    assert message["target"] == TARGET
    assert message["sender"] == OWNER
    assert "do it" in message["text"]
    assert f"hyprial workflow complete {graph_id} {WORK_NODE}" in message["text"]
    assert str(node[DEADLINE_NODE].deadline_ms) in message["text"]
    assert result["state"] == STATE_RUNNING


def test_legacy_routine_graph_writes_use_pac_authority_with_fixed_deadline(
    tmp_path: Path,
) -> None:
    clock = Clock()
    plane = Plane()
    authority = PacGraphAuthority(default_database_path(tmp_path), NullSender())
    dispatch = PacRoutineDispatch(
        state_dir=tmp_path, deliver=plane, clock_ms=clock,
        resolve_principal=resolve, graph_authority=authority,
    )
    try:
        first = start(dispatch)
        graph_id = str(first["graphId"])
        initial_deadline = nodes(tmp_path, graph_id)[DEADLINE_NODE].deadline_ms
        clock.now_ms += 100_000
        replay = start(dispatch)
        assert replay["graphId"] == graph_id
        assert nodes(tmp_path, graph_id)[DEADLINE_NODE].deadline_ms == initial_deadline
        assert len(graphs(tmp_path)) == 1
        authority.set_flag(graph_id, WORK_NODE, actor=TARGET)
        assert start(dispatch)["state"] == STATE_DONE
        dispatch.close(graph_id=graph_id, actor=OWNER)
        assert graphs(tmp_path)[0]["closed_at"] is not None
    finally:
        assert authority.close()


def test_occurrence_rearm_query_runs_through_pac_authority(tmp_path: Path) -> None:
    clock = Clock()
    plane = Plane()
    authority = PacGraphAuthority(default_database_path(tmp_path), NullSender())
    dispatch = PacRoutineDispatch(
        state_dir=tmp_path,
        deliver=plane,
        clock_ms=clock,
        resolve_principal=resolve,
        graph_authority=authority,
    )
    try:
        first = start(dispatch, occurrence_slot_ms=900_000)
        clock.now_ms += int(TIMEOUT * 1000) + 1
        blocked = start(dispatch, occurrence_slot_ms=1_800_000)
        assert blocked == {
            "graphId": first["graphId"],
            "state": STATE_ESCALATED,
        }
        boundary = dispatch.rearm_boundary(routine_name=ROUTINE)
        rearmed = start(
            dispatch,
            occurrence_slot_ms=1_800_000,
            rearm_after_created_at_ms=boundary,
        )
        assert rearmed["graphId"] != first["graphId"]
        assert len(plane.queued) == 2
    finally:
        assert authority.close()


def test_an_unresolvable_target_fails_the_dispatch_before_any_graph_exists(
    tmp_path: Path,
) -> None:
    dispatch, plane, _ = make_dispatch(tmp_path)
    with pytest.raises(RuntimeError, match="not a registered agent"):
        start(dispatch, target="ghost-worker")
    assert graphs(tmp_path) == []
    assert plane.calls == []


# ── one graph per task, across replays and restarts ────────────────────────


def test_a_replay_reuses_the_graph_and_queues_no_second_message(tmp_path: Path) -> None:
    dispatch, plane, clock = make_dispatch(tmp_path)
    first = start(dispatch)
    before = notification_count(tmp_path)

    clock.now_ms += 60_000
    second = start(dispatch)

    assert second["graphId"] == first["graphId"]
    assert len(graphs(tmp_path)) == 1
    assert len(plane.queued) == 1  # same conversation ⇒ the inbox dedupes it
    assert notification_count(tmp_path) == before


def test_done_task_gets_a_new_graph_in_the_next_occurrence(tmp_path: Path) -> None:
    dispatch, plane, clock = make_dispatch(tmp_path)
    first = start(dispatch, occurrence_slot_ms=900_000)
    flag_work(tmp_path, str(first["graphId"]), actor=TARGET, clock=clock)

    second = start(dispatch, occurrence_slot_ms=1_800_000)

    assert second["graphId"] != first["graphId"]
    assert second["state"] == STATE_RUNNING
    assert len(graphs(tmp_path)) == 2
    assert len(plane.queued) == 2


def test_escalated_task_waits_for_an_explicit_rearm(tmp_path: Path) -> None:
    dispatch, plane, clock = make_dispatch(tmp_path)
    first = start(dispatch, occurrence_slot_ms=900_000)
    clock.now_ms += int(TIMEOUT * 1000) + 1

    blocked = start(dispatch, occurrence_slot_ms=1_800_000)
    assert blocked == {"graphId": first["graphId"], "state": STATE_ESCALATED}
    assert len(graphs(tmp_path)) == 1
    assert len(plane.queued) == 1

    boundary = dispatch.rearm_boundary(routine_name=ROUTINE)
    rearmed = start(
        dispatch,
        occurrence_slot_ms=1_800_000,
        rearm_after_created_at_ms=boundary,
    )
    assert rearmed["graphId"] != first["graphId"]
    assert len(graphs(tmp_path)) == 2
    assert len(plane.queued) == 2


def test_routine_resume_rearms_an_escalated_source_task(tmp_path: Path) -> None:
    tasks = [SourceTask(uuid="u1", description="do it", tags=("route:self",))]
    service, plane, clock, _ = make_service(tmp_path, tasks)
    try:
        service.submit_timer()
        _wait(lambda: len(plane.queued) == 1)
        clock.now_ms += int(TIMEOUT * 1000) + 1
        service.submit_timer()
        _wait(
            lambda: service.status(name=ROUTINE)["outcomes"][-1:]
            == ["escalated"]
        )
        assert len(graphs(tmp_path)) == 1
        assert len(plane.queued) == 1

        service.resume(name=ROUTINE)
        clock.now_ms += 1_001
        service.submit_timer()
        _wait(lambda: len(plane.queued) == 2)
        assert len(graphs(tmp_path)) == 2
    finally:
        service.close()


def test_a_restart_reuses_the_graph_the_previous_process_created(tmp_path: Path) -> None:
    """The durable key lives in sqlite, not in the dispatcher's memory."""

    first = start(make_dispatch(tmp_path)[0])
    revived, plane, _ = make_dispatch(tmp_path)  # a brand-new dispatcher object
    second = start(revived)
    assert second["graphId"] == first["graphId"]
    assert len(graphs(tmp_path)) == 1


def test_a_half_built_graph_converges_instead_of_wedging(tmp_path: Path) -> None:
    """A crash between create and activate must not bind the key forever.

    ``create_graph`` is the first of four writes (create, ``work``,
    ``deadline``, activate).  If the dispatcher re-ran all four it would
    fail on whichever one already landed -- and the durable key returns that
    same graph every time, so the task could never be dispatched again.

    The seed stops AFTER ``work``, on purpose: a graph with no nodes at all
    is the easy half of this, and re-running every write survives it.  The
    state that wedges is the one where a write already landed, so that is
    the state the fixture builds.
    """

    store = PacGraphStore(default_database_path(tmp_path))
    try:
        head = create_graph(
            store,
            name=f"routine-{ROUTINE}",
            created_by=OWNER,
            operation_key=operation_key(ROUTINE, "u1"),
        )
        add_node(
            store,
            tmp_path,
            graph_id=str(head["graphId"]),
            node_id=WORK_NODE,
            owner=TARGET,
            brief_ref=f"routine:{ROUTINE}#u1",
            expect_version=int(head["version"]),
        )
    finally:
        store.close()

    dispatch, plane, _ = make_dispatch(tmp_path)
    result = start(dispatch)

    assert result["graphId"] == head["graphId"]
    assert len(graphs(tmp_path)) == 1
    assert set(nodes(tmp_path, str(result["graphId"]))) == {WORK_NODE, DEADLINE_NODE}
    assert graphs(tmp_path)[0]["activated_at"] is not None
    assert len(plane.queued) == 1


def test_the_deadline_never_moves_on_a_replay(tmp_path: Path) -> None:
    """Ruling D: a fixed deadline, ⛔ no activity extension."""

    dispatch, _, clock = make_dispatch(tmp_path)
    graph_id = str(start(dispatch)["graphId"])
    first = nodes(tmp_path, graph_id)[DEADLINE_NODE].deadline_ms

    clock.now_ms += 30 * 60_000
    start(dispatch)

    assert nodes(tmp_path, graph_id)[DEADLINE_NODE].deadline_ms == first


# ── completion is the owner flagging the node ──────────────────────────────


def test_the_owner_flagging_work_is_what_completes_the_task(tmp_path: Path) -> None:
    dispatch, _, clock = make_dispatch(tmp_path)
    graph_id = str(start(dispatch)["graphId"])
    assert dispatch.status(graph_id=graph_id)["state"] == STATE_RUNNING

    flag_work(tmp_path, graph_id, actor=TARGET, clock=clock)

    assert dispatch.status(graph_id=graph_id)["state"] == STATE_DONE
    assert nodes(tmp_path, graph_id)[WORK_NODE].flag is True


def test_only_the_owner_can_complete_it_and_no_text_can(tmp_path: Path) -> None:
    """The retired dispatcher completed a task when a reply CONTAINED
    ``DONE <nonce>``; a substring is not an authorization.  Here the only
    way through is the node's owner, and the dispatched text is data."""

    dispatch, plane, clock = make_dispatch(tmp_path)
    graph_id = str(start(dispatch)["graphId"])

    with pytest.raises(PacError):
        flag_work(tmp_path, graph_id, actor=OWNER, clock=clock)  # creator ≠ owner
    assert dispatch.status(graph_id=graph_id)["state"] == STATE_RUNNING

    assert "DONE" not in list(plane.queued.values())[0]["text"]


# ── the deadline reports, and a guarded clock stays quiet ──────────────────


def test_an_unflagged_task_reports_overdue_to_the_escalation_owner(
    tmp_path: Path,
) -> None:
    dispatch, _, clock = make_dispatch(tmp_path)
    graph_id = str(start(dispatch)["graphId"])

    # Exactly ON the deadline is not past it -- the reactor's rule -- and the
    # dispatcher's state must not run ahead of the notification.
    clock.now_ms = START_MS + int(TIMEOUT * 1000)
    assert tick(tmp_path, graph_id, clock=clock) == []
    assert dispatch.status(graph_id=graph_id)["state"] == STATE_RUNNING

    clock.now_ms += 1
    planned = tick(tmp_path, graph_id, clock=clock)

    assert [item.recipient for item in planned] == [ESCALATE_TO]
    assert dispatch.status(graph_id=graph_id)["state"] == STATE_ESCALATED
    # Re-ticking the same deadline notifies nobody twice (idempotent per
    # deadline), and the state stays escalated rather than escalating again.
    assert tick(tmp_path, graph_id, clock=clock) == []
    assert dispatch.status(graph_id=graph_id)["state"] == STATE_ESCALATED


def test_a_flagged_task_never_reports_overdue(tmp_path: Path) -> None:
    """The guarded clock (#500) is the whole reason the deadline can be
    fixed: a completed task past its deadline must stay silent."""

    dispatch, _, clock = make_dispatch(tmp_path)
    graph_id = str(start(dispatch)["graphId"])
    flag_work(tmp_path, graph_id, actor=TARGET, clock=clock)

    clock.now_ms = START_MS + int(TIMEOUT * 1000) + 60_000

    assert tick(tmp_path, graph_id, clock=clock) == []
    assert dispatch.status(graph_id=graph_id)["state"] == STATE_DONE


def test_a_settled_task_is_reported_and_never_dispatched_again(tmp_path: Path) -> None:
    dispatch, plane, clock = make_dispatch(tmp_path)
    graph_id = str(start(dispatch)["graphId"])

    clock.now_ms = START_MS + int(TIMEOUT * 1000) + 1
    assert start(dispatch) == {"graphId": graph_id, "state": STATE_ESCALATED}
    assert len(plane.calls) == 1  # ⛔ not even attempted a second time

    dispatch.close(graph_id=graph_id, actor=OWNER)
    assert start(dispatch)["state"] == STATE_ESCALATED
    assert len(plane.calls) == 1


def test_status_and_the_dispatcher_agree_on_a_graph_closed_early(
    tmp_path: Path,
) -> None:
    """One definition of settlement, read from two places.

    A graph closed before its deadline with ``work`` unflagged is the input
    on which two separate rules would disagree: "past deadline?" says
    running, "closed?" says over.  Both readers must give the same answer,
    and it must not be the one that reads "not completed" as "completed".
    """

    dispatch, _, clock = make_dispatch(tmp_path)
    graph_id = str(start(dispatch)["graphId"])
    dispatch.close(graph_id=graph_id, actor=OWNER)

    assert dispatch.status(graph_id=graph_id)["state"] == STATE_ESCALATED
    assert start(dispatch)["state"] == STATE_ESCALATED


def test_status_is_loud_when_the_graph_is_gone(tmp_path: Path) -> None:
    dispatch, _, _ = make_dispatch(tmp_path)
    start(dispatch)
    with pytest.raises(RuntimeError, match="missing"):
        dispatch.status(graph_id="routine-hq-selfdrive-deadbeef")


def test_a_refused_delivery_fails_loudly_and_a_later_attempt_delivers(
    tmp_path: Path,
) -> None:
    """The graph is created before the message is sent, on purpose: a
    delivery that never happens leaves a task node nobody flagged, which the
    deadline reports.  The reverse order would deliver a task no node
    records."""

    refusing = Plane(refuse=True)
    dispatch, _, clock = make_dispatch(tmp_path, plane=refusing)
    with pytest.raises(RuntimeError, match="refused by the message plane"):
        start(dispatch)
    graph_id = str(graphs(tmp_path)[0]["graph_id"])
    assert nodes(tmp_path, graph_id)[WORK_NODE].flag is False

    accepting = Plane()
    revived, plane, _ = make_dispatch(tmp_path, plane=accepting, clock=clock)
    assert str(start(revived)["graphId"]) == graph_id
    assert len(plane.queued) == 1
    assert len(graphs(tmp_path)) == 1


# ── the routine's duty cycle, on real PAC ──────────────────────────────────


#: How long one routine cycle may take to produce an observable effect.  It
#: bounds a HANG, ⛔ not the machine: every predicate here waits for something
#: to APPEAR, so a generous budget only makes a real failure slower to report.
#: Same constant and same reason as ``CYCLE_WAIT_SECONDS`` in test_routine.py.
CYCLE_WAIT_SECONDS = 30.0


def _wait(predicate, *, timeout: float = CYCLE_WAIT_SECONDS) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    assert predicate()


ROUTINE_YAML = f"""
version: 1
name: {ROUTINE}
schedule: {{interval: 15m}}
source: {{kind: taskwarrior, filter: "+selfdrive"}}
policy:
  routes:
    - {{tag: "route:self", target: "self"}}
  default: escalate
  task_template: "【自驱】{{{{task.description}}}}"
limits:
  max_in_flight: 3
  circuit_breaker: {{window_runs: 5, escalate_ratio: 1.0, action: "pause+alarm"}}
on_task_timeout: {{action: escalate, escalate_to: "{ESCALATE_TO}"}}
"""


class Alarm:
    def __init__(self) -> None:
        self.calls: list[dict[str, str]] = []

    def escalate(self, *, to: str, text: str) -> None:
        self.calls.append({"to": to, "text": text})


def make_service(tmp_path: Path, tasks: list[SourceTask]):
    plane = Plane()
    clock = Clock()
    alarm = Alarm()
    service = RoutineService(
        pac=PacRoutineDispatch(
            state_dir=tmp_path,
            deliver=plane,
            clock_ms=clock,
            resolve_principal=resolve,
        ),
        alarm=alarm,
        state_dir=tmp_path,
        clock_ms=clock,
        source_query=lambda _filter: tasks,
    )
    service.add(yaml_text=ROUTINE_YAML, owner=OWNER)
    # ``add`` sets next_due one interval out, so a tick at the same instant
    # is correctly a no-op; every test here wants the FIRST cycle to run.
    clock.now_ms += 16 * 60_000
    return service, plane, clock, alarm


def test_a_cycle_builds_a_real_graph_and_closes_it_when_the_owner_is_done(
    tmp_path: Path,
) -> None:
    """The §4 acceptance, end to end and with no mocked PAC: cycle → graph →
    task message → owner flags ``work`` → next cycle reads done and closes
    the graph.  Nothing constructs a workflow run."""

    tasks = [SourceTask(uuid="u1", description="do it", tags=("selfdrive", "route:self"))]
    service, plane, clock, _ = make_service(tmp_path, tasks)
    try:
        service.submit_timer()
        _wait(lambda: len(plane.queued) == 1)
        [graph] = graphs(tmp_path)
        graph_id = str(graph["graph_id"])
        assert graph["operation_key"] == operation_key(
            ROUTINE, "u1", 1_900_000
        )
        # route:self ⇒ the routine's own owner holds the work node.
        assert nodes(tmp_path, graph_id)[WORK_NODE].owner == OWNER
        assert (
            nodes(tmp_path, graph_id)[DEADLINE_NODE].deadline_ms
            == clock.now_ms + int(DEFAULT_TASK_TIMEOUT_SECONDS * 1000)
        )
        # Again the later event, ⛔ not the earlier one: the message is queued
        # DURING the dispatch effect, while the in-flight row is written when
        # that effect completes.  `plane.queued` is therefore observable
        # first, and asserting the row right after it is the same race.
        _wait(lambda: len(service.status(name=ROUTINE)["inFlight"]) == 1)

        flag_work(tmp_path, graph_id, actor=OWNER, clock=clock)
        clock.now_ms += 16 * 60_000
        service.submit_timer()
        # Wait for the OUTCOME, ⛔ not for the in-flight row to clear: the
        # registry drops the in-flight row when it plans the next cycle and
        # writes the routine's `outcomes` only at `_finish_cycle`, a full
        # effect round trip later.  Observing the earlier event and asserting
        # the later one is the same race this suite already fixed in
        # test_routine.py -- it just had a second home here (CI task 16669,
        # under 81s of throttling).
        _wait(lambda: service.status(name=ROUTINE)["outcomes"][-1:] == ["done"])
        _wait(lambda: len(plane.queued) == 2)
        status = service.status(name=ROUTINE)
        assert len(status["inFlight"]) == 1
        assert status["inFlight"][0]["runId"] != graph_id
        current_graphs = graphs(tmp_path)
        assert current_graphs[0]["closed_at"] is not None
        assert current_graphs[0]["closed_by"] == OWNER
        assert current_graphs[1]["closed_at"] is None
        assert not (tmp_path / "workflows.sqlite3").exists()
    finally:
        service.close()


def test_a_settled_task_the_source_keeps_listing_does_not_trip_the_breaker(
    tmp_path: Path,
) -> None:
    """The failure this pins: a finished task that stays in the source is
    re-read every cycle, and counting that settlement again would fill the
    breaker's window with escalations and pause a healthy routine."""

    tasks = [SourceTask(uuid="u1", description="do it", tags=("selfdrive", "route:self"))]
    service, plane, clock, alarm = make_service(tmp_path, tasks)
    try:
        service.submit_timer()
        _wait(lambda: len(plane.queued) == 1)
        graph_id = str(graphs(tmp_path)[0]["graph_id"])
        flag_work(tmp_path, graph_id, actor=OWNER, clock=clock)
        clock.now_ms += 16 * 60_000
        service.submit_timer()
        _wait(lambda: service.status(name=ROUTINE)["outcomes"][-1:] == ["done"])

        settled = list(service.status(name=ROUTINE)["outcomes"])
        for _ in range(2 * 5):  # twice the breaker window
            clock.now_ms += 16 * 60_000
            service.submit_timer()
            time.sleep(0.02)

        status = service.status(name=ROUTINE)
        assert status["enabled"] is True
        assert list(status["outcomes"])[:1] == settled
        assert len(plane.queued) == 2
        assert alarm.calls == []
        assert len(graphs(tmp_path)) == 2
    finally:
        service.close()


# ── migration of the retired dispatcher's rows ─────────────────────────────


def test_occurrence_and_alarm_retry_columns_migrate_transactionally(
    tmp_path: Path,
) -> None:
    database = tmp_path / "routines.sqlite3"
    db = sqlite3.connect(database)
    try:
        db.executescript(
            """
            CREATE TABLE routines (
                name TEXT PRIMARY KEY, yaml_text TEXT NOT NULL, owner TEXT NOT NULL,
                enabled INTEGER NOT NULL, next_due_ms INTEGER NOT NULL,
                source_error_streak INTEGER NOT NULL, outcomes TEXT NOT NULL,
                created_at_ms INTEGER NOT NULL, version INTEGER NOT NULL,
                quarantine_reason TEXT, registration_id TEXT
            );
            CREATE TABLE routine_cycles (
                correlation_id TEXT PRIMARY KEY, routine_name TEXT NOT NULL UNIQUE,
                generation INTEGER NOT NULL, version INTEGER NOT NULL,
                tasks_json TEXT NOT NULL, outcomes_json TEXT NOT NULL,
                pending_status INTEGER NOT NULL, pending_start INTEGER NOT NULL
            );
            CREATE TABLE routine_effects (
                effect_id TEXT PRIMARY KEY, parent_correlation_id TEXT NOT NULL,
                routine_name TEXT NOT NULL, generation INTEGER NOT NULL,
                version INTEGER NOT NULL, payload_json TEXT NOT NULL
            );
            """
        )
    finally:
        db.close()
    store = RoutineStore(database)
    try:
        assert "rearm_after_created_at_ms" in {
            row[1] for row in store._db.execute("PRAGMA table_info(routines)")
        }
        assert "occurrence_slot_ms" in {
            row[1]
            for row in store._db.execute("PRAGMA table_info(routine_cycles)")
        }
        effect_columns = {
            row[1] for row in store._db.execute("PRAGMA table_info(routine_effects)")
        }
        assert {"attempts", "available_at_ms"} <= effect_columns
    finally:
        store.close()


def test_legacy_rows_of_the_retired_dispatcher_are_deleted_on_open(
    tmp_path: Path,
) -> None:
    """Allen, 2026-09-17, on the in-flight rows left by workflow v1:
    「迁移时直接删除」.  A ``run-*`` id is not a graph id (PAC mints
    ``routine-<name>-<hex>``), and an effect whose kind this build no longer
    knows can never be executed -- both would be re-driven forever."""

    database = tmp_path / "routines.sqlite3"
    store = RoutineStore(database)
    store.close()
    db = sqlite3.connect(database)
    try:
        db.execute(
            "INSERT INTO in_flight (routine, task_uuid, run_id, target, started_at_ms)"
            " VALUES ('hq-selfdrive', 'legacy', 'run-abc123', 'cc-1', 1)"
        )
        db.execute(
            "INSERT INTO in_flight (routine, task_uuid, run_id, target, started_at_ms)"
            " VALUES ('hq-selfdrive', 'kept', 'routine-hq-selfdrive-0f0f0f0f', 'cc-1', 2)"
        )
        db.execute(
            "INSERT INTO routine_effects (effect_id, parent_correlation_id,"
            " routine_name, generation, version, payload_json)"
            " VALUES ('e-1', 'corr-1', 'hq-selfdrive', 1, 1, ?)",
            (json.dumps({"kind": "RoutineWorkflowEffect", "operation": "workflow.start"}),),
        )
        db.execute(
            "INSERT INTO routine_effects (effect_id, parent_correlation_id,"
            " routine_name, generation, version, payload_json)"
            " VALUES ('e-2', 'corr-1', 'hq-selfdrive', 1, 1, ?)",
            (json.dumps({"kind": "RoutinePacEffect", "operation": "pac.status"}),),
        )
        db.commit()
    finally:
        db.close()

    reopened = RoutineStore(database)
    try:
        assert reopened.migrated_u3 == {
            "effects": ("e-1",),
            "inFlight": ("hq-selfdrive:legacy:run-abc123",),
        }
        assert [item.task_uuid for item in reopened.in_flight(routine="hq-selfdrive")] == [
            "kept"
        ]
        assert reopened.effect("e-1") is None
        assert reopened.effect("e-2") is not None  # a kind this build knows stays
    finally:
        reopened.close()


def test_what_the_migration_dropped_reaches_a_log_line(tmp_path: Path) -> None:
    """A deletion whose only trace is an absence cannot be audited later.

    Two halves, both asserted: the service exposes the dropped ids, and the
    composition actually logs them.  The AST half exists because the log
    call has no other observer -- a refactor that dropped it would leave
    every routine test green while the migration went silent.
    """

    database = tmp_path / "routines.sqlite3"
    RoutineStore(database).close()
    db = sqlite3.connect(database)
    try:
        db.execute(
            "INSERT INTO in_flight (routine, task_uuid, run_id, target, started_at_ms)"
            " VALUES ('hq-selfdrive', 'legacy', 'run-abc123', 'cc-1', 1)"
        )
        db.commit()
    finally:
        db.close()

    service = RoutineService(pac=object(), alarm=object(), state_dir=tmp_path)
    try:
        assert service.migrated_u3["inFlight"] == ("hq-selfdrive:legacy:run-abc123",)
    finally:
        service.close()

    from hyprial.daemon.impl.application.wiring import services as wiring_services_module

    source = inspect.getsource(wiring_services_module._WiringServicesMixin)
    logged = [
        node
        for node in ast.walk(ast.parse(source.lstrip()))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "_log"
        and any(
            isinstance(arg, ast.Constant) and arg.value == "routine.migrated_u3"
            for arg in node.args
        )
    ]
    assert len(logged) == 1
    [call] = logged
    assert {keyword.arg for keyword in call.keywords} == {"effects", "inFlight"}
