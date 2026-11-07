"""Bounded PAC admission must eventually visit every open actor node."""

import time
from types import SimpleNamespace
from queue import Empty
from pathlib import Path
from typing import Any

import pytest

from hyprial.daemon.impl.pac.actors import daemon as actor_daemon
from hyprial.daemon.impl.pac.actors.daemon import PacActorService, DaemonActorRuntime
from hyprial.daemon.impl.pac.graphs.edits import create_graph, add_node, activate_graph, close_graph
from hyprial.daemon.impl.pac.graphs.reactor import NullSender
from hyprial.daemon.impl.pac.storage.store import PacGraphStore, default_database_path
from tests.support.scaling import CountingCursor, assert_flat, at_scales


@pytest.mark.parametrize(
    ("closed_count", "partial_drain"),
    [(0, False), (1, False), (129, True)],
    ids=["open-only", "with-cleanup", "partial-drain"],
)
def test_repeated_cadences_eventually_admit_every_open_actor(
    tmp_path, monkeypatch, closed_count, partial_drain
):
    home = tmp_path / "home"
    home.mkdir()
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setenv("HYPRIAL_HOME", str(home))
    monkeypatch.setenv("HARNESS_STATE_DIR", str(state))
    monkeypatch.setenv("HYPRIAL_OWNER", "hyprial-unit-owner")
    monkeypatch.setenv("HARNESS_SOCKET_PATH", str(tmp_path / "daemon.sock"))
    store = PacGraphStore(default_database_path(state))
    try:
        graph = create_graph(
            store, name="fair-open-actors", created_by="user:hyprial-unit-owner"
        )["graphId"]
        version = 1
        expected = set()
        for index in range(130):
            node = f"actor-{index:03d}"
            expected.add((graph, node))
            version = add_node(
                store,
                state,
                graph_id=graph,
                node_id=node,
                owner="user:hyprial-unit-owner",
                brief_ref=f"brief#{node}",
                kind="actor",
                actor_name=f"fairness-{index:03d}",
                launch_ref="launch/worker.yaml",
                expect_version=version,
            )["version"]
            # The fallback sweep gates on a committed activation in
            # transition; an event supplies a never-activated node.
            db = store.write()
            db.execute(
                "INSERT INTO actor_activations "
                "(graph_id,node_id,desired,op,effect_id,operation_id,updated_at) "
                "VALUES (?,?,'up','pending',?,?,0)",
                (graph, node, f"effect:{node}", f"operation:{node}"),
            )
            db.commit()
        activate_graph(store, graph, actor="user:hyprial-unit-owner")
        if closed_count:
            closed_graph = create_graph(
                store, name="closed-needs-stop", created_by="user:hyprial-unit-owner"
            )["graphId"]
            closed_version = 1
            for index in range(closed_count):
                node_id = f"cleanup-{index:03d}"
                closed_version = add_node(
                    store,
                    state,
                    graph_id=closed_graph,
                    node_id=node_id,
                    owner="user:hyprial-unit-owner",
                    brief_ref=f"brief#{node_id}",
                    kind="actor",
                    actor_name=node_id,
                    launch_ref="launch/worker.yaml",
                    expect_version=closed_version,
                )["version"]
                db = store.write()
                db.execute(
                    "INSERT INTO actor_activations "
                    "(graph_id,node_id,desired,op,effect_id,operation_id,updated_at) "
                    "VALUES (?,?,'up','done',?,?,0)",
                    (
                        closed_graph,
                        node_id,
                        f"effect:{node_id}",
                        f"operation:{node_id}",
                    ),
                )
                db.commit()
                expected.add((closed_graph, node_id))
            activate_graph(store, closed_graph, actor="user:hyprial-unit-owner")
            close_graph(store, closed_graph, actor="user:hyprial-unit-owner")
        service = PacActorService(
            state_dir=state,
            reference_root=tmp_path,
            runtime=DaemonActorRuntime(SimpleNamespace()),
            sender=NullSender(),
            daemon_epoch="fairness-test",
            logger=lambda *args, **kwargs: None,
            workers=0,
        )
        admitted = set()
        try:
            if partial_drain:
                service._scan_and_admit(force_actors=True)
                served_classes = set()
                for _ in range(8):
                    retired = service._actor_queue.get_nowait()
                    with service._lock:
                        service._active_actors.discard(retired)
                    with service._actor_queue.mutex:
                        before = set(service._actor_queue.queue)
                    service._scan_and_admit(force_actors=True)
                    with service._actor_queue.mutex:
                        added = set(service._actor_queue.queue) - before
                    assert len(added) == 1
                    served_classes.update(
                        "open" if job[0] == graph else "closed" for job in added
                    )
                assert served_classes == {"open", "closed"}
                return
            # Deterministic drain models completed actor jobs between cadences.
            # Real store, scan ordering and bounded actor queue are unchanged.
            for _ in range(3):
                service._scan_and_admit(force_actors=True)
                while True:
                    try:
                        job = service._actor_queue.get_nowait()
                    except Empty:
                        break
                    admitted.add(job)
                    with service._lock:
                        service._active_actors.discard(job)
            assert admitted == expected, (
                f"Never admitted across 3 drained cadences: {sorted(expected - admitted)}"
            )
        finally:
            assert service.close()
    finally:
        store.close()


def _insert_actor_case(
    store: PacGraphStore,
    case: str,
    *,
    graph_state: str,
    desired: str | None = None,
    op: str | None = None,
    receipt: bool = False,
    cleanup: str | None = None,
) -> tuple[str, str]:
    graph_id = f"graph-{case}"
    node_id = f"actor-{case}"
    activated_at = 10 if graph_state in {"open", "closed"} else None
    closed_at = 20 if graph_state == "closed" else None
    with store._db:
        store._db.execute(
            "INSERT INTO graphs"
            "(graph_id,name,version,created_by,created_at,activated_at,closed_at) "
            "VALUES(?,?,1,'user:test',0,?,?)",
            (graph_id, case, activated_at, closed_at),
        )
        store._db.execute(
            "INSERT INTO nodes"
            "(graph_id,node_id,owner,brief_ref,kind,actor_name,launch_ref) "
            "VALUES(?,?, 'user:test','brief','actor',?, 'launch.yaml')",
            (graph_id, node_id, node_id),
        )
        if desired is not None and op is not None:
            store._db.execute(
                "INSERT INTO actor_activations"
                "(graph_id,node_id,desired,op,effect_id,operation_id,updated_at) "
                "VALUES(?,?,?,?,?,?,0)",
                (
                    graph_id,
                    node_id,
                    desired,
                    op,
                    f"effect:{case}",
                    f"activation:{case}",
                ),
            )
        if receipt:
            store._db.execute(
                "INSERT INTO workflow_worker_receipts"
                "(graph_id,actor_node,actor_name,actor_uri,ownership,launch_digest,"
                "roster_digest,state,created_at) "
                "VALUES(?,?,?,?,'workflow','launch','roster','planned',0)",
                (graph_id, node_id, node_id, f"agent:test:node:{node_id}"),
            )
        if cleanup is not None:
            store._db.execute(
                "INSERT INTO workflow_worker_cleanup_intents"
                "(graph_id,actor_node,actor_name,actor_uri,operation_id,state,"
                "created_at,updated_at,attention_reason) "
                "VALUES(?,?,?,?,?,?,0,0,?)",
                (
                    graph_id,
                    node_id,
                    node_id,
                    f"agent:test:node:{node_id}",
                    f"cleanup:{case}",
                    cleanup,
                    "operator" if cleanup == "attention" else None,
                ),
            )
    return graph_id, node_id


def _old_actor_jobs(store: PacGraphStore) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    rows = list(
        store._db.execute(
            "SELECT n.graph_id,n.node_id,g.activated_at,g.closed_at,"
            "a.desired,a.op,c.state AS cleanup_state,"
            "r.graph_id AS receipt_graph_id "
            "FROM nodes n JOIN graphs g ON g.graph_id=n.graph_id "
            "LEFT JOIN actor_activations a "
            "ON a.graph_id=n.graph_id AND a.node_id=n.node_id "
            "LEFT JOIN workflow_worker_cleanup_intents c "
            "ON c.graph_id=n.graph_id AND c.actor_node=n.node_id "
            "LEFT JOIN workflow_worker_receipts r "
            "ON r.graph_id=n.graph_id AND r.actor_node=n.node_id "
            "WHERE n.kind='actor' ORDER BY n.graph_id,n.node_id"
        )
    )
    open_jobs = [
        (str(row["graph_id"]), str(row["node_id"]))
        for row in rows
        if row["activated_at"] is not None
        and row["closed_at"] is None
        and (
            row["cleanup_state"] in ("pending", "attention")
            or (row["desired"] is not None and row["op"] != "done")
        )
    ]
    closed_jobs = [
        (str(row["graph_id"]), str(row["node_id"]))
        for row in rows
        if row["closed_at"] is not None
        and (
            row["cleanup_state"] in ("pending", "attention")
            or (
                row["cleanup_state"] is None
                and row["receipt_graph_id"] is not None
            )
            or (
                row["cleanup_state"] is None
                and row["receipt_graph_id"] is None
                and row["desired"] is not None
                and (row["desired"] != "down" or row["op"] != "done")
            )
        )
    ]
    return open_jobs, closed_jobs


class _ActorScanConnection:
    def __init__(self, connection: Any) -> None:
        self._connection = connection
        self.actor_rows = 0
        self.plans: list[str] = []

    def execute(self, sql: str, *args: Any, **kwargs: Any) -> Any:
        cursor = self._connection.execute(sql, *args, **kwargs)
        if (
            "FROM nodes n JOIN graphs g" not in sql
            and "CROSS JOIN nodes n" not in sql
        ):
            return cursor
        self.plans.extend(
            str(row[3])
            for row in self._connection.execute(
                "EXPLAIN QUERY PLAN " + sql, *args, **kwargs
            )
        )
        return CountingCursor(cursor, self._count_actor_rows)

    def _count_actor_rows(self, amount: int) -> None:
        self.actor_rows += amount

    def __getattr__(self, name: str) -> Any:
        return getattr(self._connection, name)


def _scan_fixture(
    root: Path, settled_history: int, monkeypatch: pytest.MonkeyPatch
) -> tuple[int, list[str]]:
    state = root / "state"
    state.mkdir(parents=True)
    store = PacGraphStore(default_database_path(state))
    try:
        _insert_actor_case(
            store, "open-a", graph_state="open", desired="up", op="pending"
        )
        _insert_actor_case(
            store, "open-b", graph_state="open", desired="up", op="pending"
        )
        for index in range(settled_history):
            _insert_actor_case(
                store,
                f"settled-{index:03d}",
                graph_state="closed",
                desired="down",
                op="done",
            )
    finally:
        store.close()

    observed: list[_ActorScanConnection] = []
    real_store = actor_daemon.PacGraphStore

    class _CountingStore(real_store):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            proxy = _ActorScanConnection(self._db)
            self._db = proxy
            observed.append(proxy)

    with monkeypatch.context() as patch:
        patch.setattr(actor_daemon, "PacGraphStore", _CountingStore)
        service = PacActorService(
            state_dir=state,
            reference_root=root,
            runtime=DaemonActorRuntime(SimpleNamespace()),
            sender=NullSender(),
            daemon_epoch="actor-scan-test",
            logger=lambda *args, **kwargs: None,
            workers=0,
        )
        try:
            service._scan_and_admit(force_actors=True)
            assert list(service._actor_queue.queue) == [
                ("graph-open-a", "actor-open-a"),
                ("graph-open-b", "actor-open-b"),
            ]
        finally:
            assert service.close()
    return sum(item.actor_rows for item in observed), [
        step for item in observed for step in item.plans
    ]


def test_actor_scan_sql_matches_old_filter_for_every_actionable_shape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = tmp_path / "state"
    state.mkdir()
    store = PacGraphStore(default_database_path(state))
    cases = (
        ("inactive", "inactive", None, None, False, None),
        ("open-no-state", "open", None, None, False, None),
        ("open-settled", "open", "down", "done", True, "complete"),
        ("closed-up", "closed", "up", "done", False, None),
        ("closed-down-pending", "closed", "down", "pending", False, None),
        ("closed-down-done", "closed", "down", "done", False, None),
        ("closed-receipt-only", "closed", "up", "done", True, None),
        ("closed-cleanup-pending", "closed", "down", "done", True, "pending"),
        ("closed-cleanup-complete", "closed", "up", "pending", True, "complete"),
        ("closed-cleanup-attention", "closed", "up", "pending", True, "attention"),
    )
    try:
        for case, graph_state, desired, op, receipt, cleanup in cases:
            _insert_actor_case(
                store,
                case,
                graph_state=graph_state,
                desired=desired,
                op=op,
                receipt=receipt,
                cleanup=cleanup,
            )
        expected = _old_actor_jobs(store)
    finally:
        store.close()

    observed: list[_ActorScanConnection] = []
    real_store = actor_daemon.PacGraphStore

    class _CountingStore(real_store):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            proxy = _ActorScanConnection(self._db)
            self._db = proxy
            observed.append(proxy)

    with monkeypatch.context() as patch:
        patch.setattr(actor_daemon, "PacGraphStore", _CountingStore)
        service = PacActorService(
            state_dir=state,
            reference_root=tmp_path,
            runtime=DaemonActorRuntime(SimpleNamespace()),
            sender=NullSender(),
            daemon_epoch="actor-equivalence-test",
            logger=lambda *args, **kwargs: None,
            workers=0,
        )
        try:
            service._scan_and_admit(force_actors=True)
            actual = list(service._actor_queue.queue)
            assert actual == [*expected[0], *expected[1]]
            assert sum(item.actor_rows for item in observed) == len(actual)
        finally:
            assert service.close()


def test_actor_scan_rows_are_flat_and_nodes_use_an_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    at_n, at_4n = at_scales(
        lambda size: _scan_fixture(
            tmp_path / f"history-{size}", size, monkeypatch
        ),
        12,
    )
    assert_flat(at_n[0], at_4n[0])
    assert at_n[0] == 2
    assert at_n[1]
    assert not any(step.startswith("SCAN n") for step in at_n[1]), at_n[1]
    assert any("SEARCH n USING INDEX" in step for step in at_n[1]), at_n[1]


def _drain_actor_queue(service: PacActorService) -> set[tuple[str, str]]:
    """Force one fallback sweep, then model completed jobs by draining it."""

    service._scan_and_admit(force_actors=True)
    admitted: set[tuple[str, str]] = set()
    while True:
        try:
            job = service._actor_queue.get_nowait()
        except Empty:
            break
        admitted.add(job)
        with service._lock:
            service._active_actors.discard(job)
    return admitted


def test_fallback_sweep_excludes_settled_and_keeps_actionable(
    tmp_path: Path,
) -> None:
    """The one fallback sweep must be cheap and must not drop real work.

    Before the fix every open-graph actor node was re-supplied on every
    maintenance tick, so ~119 settled actors drove one observe/reconcile
    round-trip each per second and saturated the authority mailbox (the
    ``pac.actor.queue_full`` storm of 2026-10-13).  The fallback sweep now
    excludes every never-activated node and every settled actor that is not
    running -- the 55 ``desired='down', op='done'`` rows and the 60
    no-activation rows the production histogram measured.  It must keep
    supplying genuinely unfinished work (an open actor still coming up, an
    open actor with a pending cleanup, a closed graph's actor that still
    needs its stop) *and* the running subset (``desired='up', op='done'``),
    which has no event for a silent harness death and is therefore
    re-observed on the cheaper running tier.
    """

    state = tmp_path / "state"
    state.mkdir()
    store = PacGraphStore(default_database_path(state))
    try:
        _insert_actor_case(
            store, "open-settled", graph_state="open", desired="up", op="done"
        )
        _insert_actor_case(
            store, "open-settled-down", graph_state="open", desired="down", op="done"
        )
        _insert_actor_case(
            store, "open-no-state", graph_state="open"
        )
        _insert_actor_case(
            store, "open-pending", graph_state="open", desired="up", op="pending"
        )
        _insert_actor_case(
            store,
            "open-cleanup-pending",
            graph_state="open",
            desired="up",
            op="done",
            receipt=True,
            cleanup="pending",
        )
        _insert_actor_case(
            store, "closed-needs-stop", graph_state="closed", desired="up", op="done"
        )
    finally:
        store.close()

    service = PacActorService(
        state_dir=state,
        reference_root=tmp_path,
        runtime=DaemonActorRuntime(SimpleNamespace()),
        sender=NullSender(),
        daemon_epoch="idle-backoff-test",
        logger=lambda *args, **kwargs: None,
        workers=0,
    )
    settled = ("graph-open-settled", "actor-open-settled")
    settled_down = ("graph-open-settled-down", "actor-open-settled-down")
    absent = ("graph-open-no-state", "actor-open-no-state")
    pending = ("graph-open-pending", "actor-open-pending")
    cleanup = ("graph-open-cleanup-pending", "actor-open-cleanup-pending")
    closed = ("graph-closed-needs-stop", "actor-closed-needs-stop")
    try:
        first = _drain_actor_queue(service)
        # ``force_actors`` runs both tiers: the actionable dirty set *and* the
        # running subset (``settled`` is ``desired='up', op='done'``).
        assert {pending, cleanup, closed, settled} <= first
        assert settled_down not in first
        assert absent not in first

        # A second sweep repeats the same cheap set: the excluded classes
        # never re-enter the supply just because time passed.
        second = _drain_actor_queue(service)
        assert {pending, cleanup, closed, settled} <= second
        assert not ({settled_down, absent} & second)

        # With no event and neither tier due, nothing is supplied at all.
        service._actor_fallback_due = time.monotonic() + 10_000.0
        service._actor_running_due = time.monotonic() + 10_000.0
        service._scan_and_admit()
        assert list(service._actor_queue.queue) == []
    finally:
        assert service.close()
