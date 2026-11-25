"""#1803: held-overdue graphs remind their creator, at most once per 24h.

The reminder is PAC-owned: detection reads the PAC store, the notice is a
recorded ``notifications`` row plus its ``notification_planned`` journal fact,
and delivery is PAC's existing drain.  These tests therefore assert on the
durable row and on what the delivery port was asked to send -- never on an
internal call order.
"""

from __future__ import annotations

import pytest

from hyprial.daemon.impl.pac.held_reminder import (
    HELD_REMINDER_KEY,
    HeldReminder,
    HeldReminderConfig,
)
from hyprial.kernel import GenerationScheduler

from .support import (
    AGENT,
    HOUR_MS,
    OPERATOR,
    Clock,
    Sender,
    held_graph,
    reminder_rows,
    service as service_fixture,
    start_graph,
)


@pytest.fixture
def harness(tmp_path):
    sender = Sender()
    clock = Clock()
    workflow = service_fixture(tmp_path, sender, clock)
    try:
        yield workflow, sender, clock, tmp_path
    finally:
        assert workflow.close()


def reminder(tmp_path, clock, workflow, **kwargs) -> HeldReminder:
    options = {
        "creator_exists": lambda _actor: True,
        "operator_route": OPERATOR,
        "wake_delivery": lambda: workflow._tick(),
    }
    options.update(kwargs)
    return HeldReminder(
        database=workflow.database,
        owner="test",
        config=options.pop("config", HeldReminderConfig()),
        clock_ms=clock,
        **options,
    )


def deliver(workflow, sender: Sender) -> list[dict]:
    workflow._tick()
    return sender.reminders()


def test_overdue_held_graph_notifies_once_with_blocking_facts_and_options(harness):
    workflow, sender, clock, _tmp = harness
    graph = held_graph(workflow)
    clock.advance(7 * HOUR_MS)

    notices = reminder(_tmp, clock, workflow).run_once()

    assert len(notices) == 1
    delivered = deliver(workflow, sender)
    assert len(delivered) == 1
    text = delivered[0]["text"]
    assert delivered[0]["recipient"] == "user:test"
    assert graph in text
    assert "review" in text and "output:review" in text
    assert "7h" in text
    assert "reset" in text and "cancel" in text and "threshold" in text
    # A reminder is a reminder: the graph is untouched.
    assert reminder_rows(workflow, graph)[0]["kind"] == "actor_alert"


def test_second_run_inside_the_window_adds_no_second_reminder(harness):
    workflow, sender, clock, _tmp = harness
    held_graph(workflow)
    clock.advance(7 * HOUR_MS)
    reminder(_tmp, clock, workflow).run_once()
    assert len(deliver(workflow, sender)) == 1

    clock.advance(HOUR_MS)
    assert reminder(_tmp, clock, workflow).run_once() == []
    assert len(deliver(workflow, sender)) == 1
    assert len(reminder_rows(workflow)) == 1


def test_restart_replays_the_same_notice_idempotently(harness):
    workflow, sender, clock, tmp_path = harness
    graph = held_graph(workflow)
    clock.advance(7 * HOUR_MS)

    first = HeldReminder(
        database=workflow.database,
        owner="test",
        config=HeldReminderConfig(),
        creator_exists=lambda _actor: True,
        operator_route=OPERATOR,
        clock_ms=clock,
        wake_delivery=lambda: None,
    )
    assert first.run_once()
    first.close()

    second = reminder(tmp_path, clock, workflow)
    assert second.run_once() == []
    assert len(reminder_rows(workflow, graph)) == 1
    assert len(deliver(workflow, sender)) == 1


def test_each_overdue_graph_gets_its_own_reminder(harness):
    workflow, sender, clock, tmp_path = harness
    graphs = [held_graph(workflow, key=f"held-{index}") for index in range(3)]
    clock.advance(8 * HOUR_MS)

    assert len(reminder(tmp_path, clock, workflow).run_once()) == 3

    delivered = deliver(workflow, sender)
    assert len(delivered) == 3
    for graph in graphs:
        assert sum(graph in item["text"] for item in delivered) == 1


def test_missing_creator_is_rerouted_to_the_operator_and_says_so(harness):
    workflow, sender, clock, tmp_path = harness
    graph = held_graph(workflow)
    clock.advance(7 * HOUR_MS)

    reminder(
        tmp_path, clock, workflow, creator_exists=lambda _actor: False
    ).run_once()

    delivered = deliver(workflow, sender)
    assert len(delivered) == 1
    assert delivered[0]["recipient"] == OPERATOR
    text = delivered[0]["text"]
    assert "user:test" in text and "missing" in text.lower()
    assert graph in text


def test_graph_under_the_threshold_produces_nothing(harness):
    workflow, sender, clock, tmp_path = harness
    held_graph(workflow)
    clock.advance(HOUR_MS)

    assert reminder(tmp_path, clock, workflow).run_once() == []
    assert reminder_rows(workflow) == []
    assert deliver(workflow, sender) == []


def test_threshold_is_configurable(harness):
    workflow, sender, clock, tmp_path = harness
    held_graph(workflow)
    clock.advance(2 * HOUR_MS)

    assert reminder(
        tmp_path,
        clock,
        workflow,
        config=HeldReminderConfig(threshold_ms=3_600_000),
    ).run_once()
    assert len(deliver(workflow, sender)) == 1


def test_disabled_switch_records_and_sends_nothing(harness):
    workflow, sender, clock, tmp_path = harness
    held_graph(workflow)
    clock.advance(9 * HOUR_MS)

    reminder(
        tmp_path, clock, workflow, config=HeldReminderConfig(enabled=False)
    ).run_once()

    assert reminder_rows(workflow) == []
    assert deliver(workflow, sender) == []


def test_config_reads_the_switch_and_threshold_from_the_environment():
    assert HeldReminderConfig.from_environment({}).enabled is True
    assert HeldReminderConfig.from_environment({}).threshold_ms == 6 * HOUR_MS
    assert (
        HeldReminderConfig.from_environment({"HYPRIAL_PAC_HELD_REMINDER": "0"})
        .enabled
        is False
    )
    assert (
        HeldReminderConfig.from_environment(
            {"HYPRIAL_PAC_HELD_REMINDER_THRESHOLD_SECONDS": "900"}
        ).threshold_ms
        == 900_000
    )
    # An unreadable override falls back to the default rather than disabling.
    assert (
        HeldReminderConfig.from_environment(
            {"HYPRIAL_PAC_HELD_REMINDER_THRESHOLD_SECONDS": "soon"}
        ).threshold_ms
        == 6 * HOUR_MS
    )


class RecordingTimer:
    def __init__(self, delay, function, args=()) -> None:
        self.delay = delay
        self.function = function
        self.args = args
        self.daemon = False
        self.cancelled = False
        RecordingTimers.created.append(self)

    def start(self) -> None:
        return None

    def cancel(self) -> None:
        self.cancelled = True


class RecordingTimers:
    created: list[RecordingTimer] = []

    def __call__(self, delay, function, args=()):
        return RecordingTimer(delay, function, args)


def test_check_runs_right_after_restore_and_then_on_its_own_key(harness):
    workflow, sender, clock, tmp_path = harness
    held_graph(workflow)
    clock.advance(7 * HOUR_MS)
    RecordingTimers.created = []
    scheduler = GenerationScheduler(timer_factory=RecordingTimers())

    hour = reminder(
        tmp_path,
        clock,
        workflow,
        scheduler=scheduler,
        config=HeldReminderConfig(),
    )
    try:
        hour.start_after_restore()
        # The restore-time run is immediate; it does not wait for the timer.
        assert len(deliver(workflow, sender)) == 1
        assert [timer.delay for timer in RecordingTimers.created] == [3600.0]
        assert HELD_REMINDER_KEY in scheduler._entries
        assert hour.run_once() == []
        timer = RecordingTimers.created[-1]
        clock.advance(HOUR_MS)
        timer.function(*timer.args)
        assert [timer.delay for timer in RecordingTimers.created] == [3600.0, 3600.0]
        assert len(deliver(workflow, sender)) == 1
    finally:
        hour.close()
    assert HELD_REMINDER_KEY not in scheduler._entries


def test_detection_reads_held_graphs_only(harness):
    workflow, sender, clock, tmp_path = harness
    held_graph(workflow, key="held-a")
    held_graph(workflow, key="held-b")
    running = start_graph(
        workflow,
        [{"id": "work", "task": "do it", "owner": AGENT}],
        key="running",
    )
    workflow._tick()
    assert workflow.status(run_id=running)["state"] == "running"

    clock.advance(9 * HOUR_MS)
    notices = reminder(tmp_path, clock, workflow).run_once()

    # The two held graphs are reminded; the still-running graph is not.
    assert len(notices) == 2
    delivered = deliver(workflow, sender)
    assert len(delivered) == 2
    assert all(running not in item["text"] for item in delivered)


def test_v19_database_gains_a_dated_hold_backfilled_from_the_journal(tmp_path):
    """The new column is the only store of a hold's age; old rows date from
    the recorded failure that held them."""

    import sqlite3

    from hyprial.daemon.impl.pac.storage.store import PacGraphStore

    sender = Sender()
    clock = Clock()
    workflow = service_fixture(tmp_path, sender, clock)
    try:
        graph = held_graph(workflow)
    finally:
        workflow.close()

    from tests.support.pac.migrations import strip_held_v20

    db = sqlite3.connect(workflow.database)
    try:
        strip_held_v20(db)
        db.commit()
    finally:
        db.close()

    store = PacGraphStore(workflow.database)
    try:
        version = store._db.execute("PRAGMA user_version").fetchone()[0]
        held_at = store._db.execute(
            "SELECT held_at_ms FROM workflow_graphs WHERE graph_id=?", (graph,)
        ).fetchone()[0]
    finally:
        store.close()
    assert version == 20
    # Clock starts at 1000 and the failing transition is recorded there.
    assert held_at == 1000


def test_local_user_creator_is_resolvable(tmp_path):
    """A terminal-created graph's creator is ``user:<owner>``, never "missing".

    Only a *local agent* identity can be proven absent here; a user identity is
    a delivery target of its own (owner DM path) and a remote principal cannot
    be judged from this node at all.
    """

    from hyprial.daemon.impl.application import DaemonApplication
    from hyprial.shell.impl.daemon_wiring import daemon_dependencies

    state = tmp_path / "state"
    state.mkdir(parents=True, exist_ok=True)
    app = DaemonApplication(
        state_dir=state,
        socket_path=tmp_path / "daemon.sock",
        node_id="machine",
        owner="test",
        zenoh_listen=("tcp/127.0.0.1:0",),
        zenoh_connect=(),
        **daemon_dependencies(),
    )
    app.network_isolated = True
    try:
        assert app._held_reminder_creator_exists("user:test") is True
        assert app._held_reminder_creator_exists("agent:test:machine:ghost") is False
        app.agents.create("pm")
        assert app._held_reminder_creator_exists("agent:test:machine:pm") is True
        # Another node's principal: this node cannot prove absence.
        assert app._held_reminder_creator_exists("agent:other:node:pm") is True
        # Legacy short names fall back to the local registry.
        assert app._held_reminder_creator_exists("pm") is True
        assert app._held_reminder_creator_exists("ghost") is False
    finally:
        app._close()
        app._close_home_lease()


def test_worker_state_is_reported_as_activation_not_observed_liveness(harness):
    """A worker whose up activation is merely settled is not proven live."""

    from hyprial.daemon.impl.pac.storage.store import PacGraphStore

    workflow, sender, clock, tmp_path = harness
    graph = start_graph(workflow, [{"id": "worker", "task": "do it"}], key="up-worker")
    workflow._tick()
    actor = None
    store = PacGraphStore(workflow.database)
    try:
        with store.write():
            store._db.execute(
                "UPDATE workflow_graphs SET state='held',held_at_ms=? WHERE graph_id=?",
                (clock(), graph),
            )
            store._db.execute(
                "INSERT INTO actor_activations "
                "(graph_id,node_id,incarnation,desired,op,effect_id,operation_id,updated_at) "
                "SELECT graph_id,node_id,1,'up','done','effect','operation',? "
                "FROM nodes WHERE graph_id=? AND kind='actor'",
                (clock(), graph),
            )
            store._db.commit()
        actor = store._db.execute(
            "SELECT actor_name FROM nodes WHERE graph_id=? AND kind='actor'",
            (graph,),
        ).fetchone()[0]
    finally:
        store.close()
    assert actor is not None
    clock.advance(7 * HOUR_MS)

    reminder(tmp_path, clock, workflow).run_once()
    delivered = deliver(workflow, sender)
    assert len(delivered) == 1
    text = delivered[0]["text"]
    assert "Live workers" not in text
    assert "desired" in text and "not observed liveness" in text
    plan = reminder_rows(workflow, graph)[0]["plan_json"]
    assert f'"workersDesiredUp": ["{actor}"]' in plan
    assert '"liveWorkers"' not in plan
