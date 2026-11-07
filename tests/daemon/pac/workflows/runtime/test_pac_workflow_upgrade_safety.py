"""Behavioral coverage for the A1/A2/A3 upgrade-safety contract.

The fixture deliberately uses the complete legacy barrier schema retained from
the 7324b4f5 workflow store, rather than the small runs/effects fixture in the
seeded regression tests.  These tests exercise the transaction boundaries and
the daemon's real composition path without starting an external daemon.
"""

from __future__ import annotations
from tests.support.runtime._owned_applications import owned_applications as owned_applications

import json
import time
from pathlib import Path
import sqlite3
from types import SimpleNamespace
from typing import Any, Callable

import pytest

import hyprial.daemon.impl.application.wiring.services as daemon_application
import hyprial.daemon.impl.dispatch.remote as remote_workflow
import hyprial.daemon.impl.pac.graphs.authority as pac_authority
import hyprial.daemon.impl.pac.storage.legacy as legacy_workflows
from hyprial.kernel import ipc_errors
from hyprial.daemon.impl.application import DaemonApplication
from hyprial.identity import PacError
from hyprial.daemon.impl.pac.storage.legacy import cutover
from hyprial.daemon.impl.pac.workflows.runtime import GraphWorkflowService
from hyprial.shell.impl.daemon_wiring import daemon_dependencies as _deep_module_daemon_dependencies


ROOT = Path(__file__).resolve().parents[5]
# legacy_barrier.sql is the complete retained copy of the 7324b4f5 store DDL.
OLD_SCHEMA = (ROOT / 'src/hyprial/daemon/impl/pac/storage/legacy_barrier.sql').read_text()
CUTOVER_REASON = (
    "workflow execution retired; unfinished tracking terminated, no task replay"
)


def _quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _seed_old_home(
    state_dir: Path,
    *,
    with_effect: bool = True,
    with_agent_task: bool = True,
) -> dict[str, list[tuple[Any, ...]]]:
    state_dir.mkdir(parents=True, exist_ok=True)
    database = state_dir / "workflows.sqlite3"
    db = sqlite3.connect(database)
    try:
        db.executescript(OLD_SCHEMA)
        db.execute(
            "INSERT INTO runs VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                "run-live",
                "live",
                "legacy body",
                "agent:owner:node:submitter",
                "nonce-live",
                "running",
                None,
                "[]",
                10,
                None,
            ),
        )
        db.execute(
            "INSERT INTO runs VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                "run-done",
                "done",
                "finished body",
                "user:owner",
                "nonce-done",
                "completed",
                "done",
                "[]",
                11,
                12,
            ),
        )
        db.execute(
                "INSERT INTO targets VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                "run-live",
                "worker",
                "conversation:live",
                "awaiting",
                1,
                1000,
                None,
                None,
                "partial",
                0,
                0,
            ),
        )
        db.execute(
                "INSERT INTO targets VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                "run-done",
                "worker",
                "conversation:done",
                "done",
                1,
                1000,
                None,
                None,
                "complete",
                1,
                0,
            ),
        )
        if with_agent_task:
            db.execute(
                "INSERT INTO agent_task_runs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "task-live",
                    "agent:owner:node:worker",
                    "ns",
                    "external-live",
                    "digest",
                    "caller:owner",
                    "{}",
                    "task body must never enter the log",
                    "{}",
                    "running",
                    None,
                    None,
                    20,
                    None,
                ),
            )
            db.execute(
                "INSERT INTO agent_task_runs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "task-done",
                    "agent:owner:node:worker",
                    "ns",
                    "external-done",
                    "digest-done",
                    "caller:owner",
                    "{}",
                    "finished body",
                    "{}",
                    "completed",
                    None,
                    None,
                    21,
                    22,
                ),
            )
        db.execute(
            "INSERT INTO actor_assign VALUES (?,?,?,?,?)",
            ("worker", "workflow", "run-live", 1, None),
        )
        db.execute(
            "INSERT INTO actor_assign VALUES (?,?,?,?,?)",
            ("unrelated", "routine", "routine-live", 1, None),
        )
        if with_effect:
            db.execute(
                "INSERT INTO workflow_effect_batches VALUES (?,?,?)",
                ("corr-live", "WorkflowTimerCompleted", '{"runId":"run-live"}'),
            )
            db.execute(
                "INSERT INTO workflow_effect_batches VALUES (?,?,?)",
                ("corr-zero", "WorkflowFinished", '{"runId":"run-done"}'),
            )
            db.execute(
                "INSERT INTO workflow_effects VALUES (?,?,?,?)",
                (
                    "effect-live",
                    "corr-live",
                    7,
                    '{"effect_id":"effect-live","payload":"opaque"}',
                ),
            )
        db.commit()
        return {
            "batches": db.execute(
                "SELECT correlation_id, final_kind, final_payload_json "
                "FROM workflow_effect_batches ORDER BY rowid"
            ).fetchall(),
            "effects": db.execute(
                "SELECT effect_id, correlation_id, generation, payload_json "
                "FROM workflow_effects ORDER BY rowid"
            ).fetchall(),
        }
    finally:
        db.close()


def _install_v1_cutover(state_dir: Path, *, at: int) -> None:
    """Create the earlier sealed marker without the v2 archive/marker."""

    database = state_dir / "workflows.sqlite3"
    db = sqlite3.connect(database)
    try:
        prior_states: list[tuple[str, str, str]] = []
        terminated = 0
        for table in ("runs", "targets", "agent_task_runs", "agent_task_targets"):
            columns = [
                row[1] for row in db.execute(f"PRAGMA table_info({_quote(table)})")
            ]
            for row in db.execute(
                f"SELECT rowid AS archive_rowid,* FROM {_quote(table)}"
            ).fetchall():
                index = {"archive_rowid": row[0], **dict(zip(columns, row[1:]))}
                terminal = (
                    {"completed", "cancelled", "failed"}
                    if "runs" in table
                    else {
                        "completed",
                        "succeeded",
                        "rejected",
                        "done",
                        "timed_out",
                        "escalated",
                        "cancelled",
                        "failed",
                    }
                )
                if index["state"] in terminal:
                    continue
                rowid = index.pop("archive_rowid")
                prior_states.append(
                    (table, str(rowid), json.dumps(index, sort_keys=True))
                )
                update = f"UPDATE {_quote(table)} SET state='cancelled'"
                args: tuple[Any, ...] = ()
                if "finished_at_ms" in columns:
                    update += ", finished_at_ms=?"
                    args = (at,)
                db.execute(update + " WHERE rowid=?", (*args, rowid))
                if table in {"runs", "agent_task_runs"}:
                    terminated += 1
        db.execute(
            "CREATE TABLE pac_cutover(id INTEGER PRIMARY KEY CHECK(id=1),"
            "at INTEGER NOT NULL, terminated INTEGER NOT NULL, reason TEXT NOT NULL,"
            "source_present INTEGER NOT NULL DEFAULT 1)"
        )
        db.execute(
            "CREATE TABLE pac_prior_states(table_name TEXT NOT NULL,row_key TEXT NOT NULL,"
            "prior_json TEXT NOT NULL,PRIMARY KEY(table_name,row_key))"
        )
        db.executemany("INSERT INTO pac_prior_states VALUES (?,?,?)", prior_states)
        db.execute(
            "INSERT INTO pac_cutover VALUES (1,?,?,?,1)",
            (at, terminated, CUTOVER_REASON),
        )
        db.execute(
            "CREATE TRIGGER user_effect_guard BEFORE UPDATE ON workflow_effects "
            "BEGIN SELECT RAISE(ABORT,'user trigger retained'); END"
        )
        _install_seals(db)
        db.commit()
    finally:
        db.close()


def _install_seals(db: sqlite3.Connection) -> None:
    tables = {
        row[0]
        for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    }
    for table in tables:
        for operation in ("INSERT", "UPDATE", "DELETE"):
            db.execute(
                f"CREATE TRIGGER pac_sealed_{table}_{operation} "
                f"BEFORE {operation} ON {_quote(table)} "
                "BEGIN SELECT RAISE(ABORT,'WORKFLOW_RETIRED: legacy archive is read-only'); END"
            )


def _assert_live_effects_unchanged(state_dir: Path, before: dict[str, list[Any]]) -> None:
    db = sqlite3.connect(state_dir / "workflows.sqlite3")
    try:
        assert db.execute(
            "SELECT correlation_id, final_kind, final_payload_json "
            "FROM workflow_effect_batches ORDER BY rowid"
        ).fetchall() == before["batches"]
        assert db.execute(
            "SELECT effect_id, correlation_id, generation, payload_json "
            "FROM workflow_effects ORDER BY rowid"
        ).fetchall() == before["effects"]
        assert db.execute("SELECT state FROM runs WHERE run_id='run-live'").fetchone()[0] == "running"
        assert not db.execute(
            "SELECT 1 FROM sqlite_master WHERE name IN "
            "('pac_cutover','pac_cutover_safety',"
            "'pac_archived_workflow_effect_batches','pac_archived_workflow_effects')"
        ).fetchall()
    finally:
        db.close()


def test_cutover_archives_exact_effects_and_empties_fk_live_tables(tmp_path: Path) -> None:
    before = _seed_old_home(tmp_path)

    result = cutover(tmp_path, at=100)

    assert result["terminated"] == len(result["cancelled"]) == 2
    with sqlite3.connect(tmp_path / "workflows.sqlite3") as db:
        db.execute("PRAGMA foreign_keys=ON")
        assert db.execute("SELECT * FROM workflow_effect_batches").fetchall() == []
        assert db.execute("SELECT * FROM workflow_effects").fetchall() == []
        assert db.execute(
            "SELECT correlation_id, final_kind, final_payload_json "
            "FROM pac_archived_workflow_effect_batches ORDER BY rowid"
        ).fetchall() == before["batches"]
        assert db.execute(
            "SELECT effect_id, correlation_id, generation, payload_json "
            "FROM pac_archived_workflow_effects ORDER BY rowid"
        ).fetchall() == before["effects"]
        assert db.execute(
            "PRAGMA foreign_key_list(pac_archived_workflow_effects)"
        ).fetchone()[2] == "pac_archived_workflow_effect_batches"
        assert db.execute(
            "SELECT COUNT(*) FROM pac_archived_workflow_effect_batches "
            "WHERE correlation_id='corr-zero'"
        ).fetchone()[0] == 1
        # Every old write surface and both new archive tables are sealed.
        with pytest.raises(sqlite3.IntegrityError, match="WORKFLOW_RETIRED"):
            db.execute("INSERT INTO workflow_effect_batches VALUES ('new','x','{}')")
        db.rollback()
        with pytest.raises(sqlite3.IntegrityError, match="WORKFLOW_RETIRED"):
            db.execute("UPDATE runs SET state='running' WHERE run_id='run-live'")
        db.rollback()
        with pytest.raises(sqlite3.IntegrityError, match="WORKFLOW_RETIRED"):
            db.execute("DELETE FROM targets WHERE run_id='run-live'")
        db.rollback()
        with pytest.raises(sqlite3.IntegrityError, match="WORKFLOW_RETIRED"):
            db.execute(
                "UPDATE pac_archived_workflow_effects SET payload_json='execute'"
            )
        db.rollback()
        with pytest.raises(sqlite3.IntegrityError, match="WORKFLOW_RETIRED"):
            db.execute("DELETE FROM pac_archived_workflow_effect_batches")
        db.rollback()


def test_cutover_empty_home_and_zero_effect_batch_have_stable_report(tmp_path: Path) -> None:
    result = cutover(tmp_path, at=101)
    assert result["cancelled"] == []

    state = tmp_path / "with-zero"
    before = _seed_old_home(state, with_effect=False, with_agent_task=False)
    result = cutover(state, at=102)
    assert result["cancelled"] == [{
        "table": "runs",
        "runId": "run-live",
        "sender": "agent:owner:node:submitter",
        "name": "live",
        "priorState": "running",
        "state": "cancelled",
        "finishedAtMs": 102,
        "reason": CUTOVER_REASON,
    }]
    assert before["batches"] == [] and before["effects"] == []


def test_repair_of_sealed_v1_archives_effects_preserves_report_and_unknown_trigger(
    tmp_path: Path,
) -> None:
    before = _seed_old_home(tmp_path)
    _install_v1_cutover(tmp_path, at=77)

    first = cutover(tmp_path, at=999)
    second = cutover(tmp_path, at=1000)

    assert first == second
    assert first["at"] == 77
    assert first["terminated"] == len(first["cancelled"]) == 2
    with sqlite3.connect(tmp_path / "workflows.sqlite3") as db:
        assert db.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' AND name='user_effect_guard'"
        ).fetchone()
        assert db.execute("SELECT COUNT(*) FROM workflow_effects").fetchone()[0] == 0
        assert db.execute(
            "SELECT COUNT(*) FROM pac_archived_workflow_effects"
        ).fetchone()[0] == len(before["effects"])
        assert db.execute(
            "SELECT finished_at_ms FROM runs WHERE run_id='run-live'"
        ).fetchone()[0] == 77


def test_actual_legacy_effect_store_replay_is_empty_after_cutover(tmp_path: Path) -> None:
    """Exercise the full retained 7324b4f5 schema, including a zero-effect batch."""

    before = _seed_old_home(tmp_path)
    cutover(tmp_path, at=103)
    db = sqlite3.connect(tmp_path / "workflows.sqlite3")
    try:
        # Exact old Store.save_effect_batch behavior: no payload decoding or
        # actor execution is possible because the old live tables are empty.
        for correlation_id, final_kind, final_payload in db.execute(
            "SELECT correlation_id, final_kind, final_payload_json "
            "FROM workflow_effect_batches"
        ).fetchall():
            db.execute(
                "INSERT OR REPLACE INTO workflow_effect_batches "
                "VALUES (?,?,?)",
                (correlation_id, final_kind, final_payload),
            )
        db.commit()
    finally:
        db.close()
    assert before["batches"]


def _copy_only_then_fail(db: sqlite3.Connection) -> None:
    legacy_workflows._ensure_effect_archive_tables(db)
    for source, archive in (
        (
            legacy_workflows._EFFECT_BATCHES,
            legacy_workflows._ARCHIVED_EFFECT_BATCHES,
        ),
        (legacy_workflows._EFFECTS, legacy_workflows._ARCHIVED_EFFECTS),
    ):
        columns = legacy_workflows._effect_columns(db, source)
        quoted = ", ".join(_quote(column) for column in columns)
        db.execute(
            f"INSERT INTO {_quote(archive)} ({quoted}) "
            f"SELECT {quoted} FROM {_quote(source)}"
        )
    raise RuntimeError("injected after copy")


@pytest.mark.parametrize(
    ("phase", "inject"),
    [
        ("copy", _copy_only_then_fail),
        (
            "delete",
            lambda db: (_REAL_COPY(db), (_ for _ in ()).throw(RuntimeError("injected after delete")))[1],
        ),
        (
            "trigger",
            lambda db: (_REAL_SEAL(db), (_ for _ in ()).throw(RuntimeError("injected after trigger replacement")))[1],
        ),
    ],
)
def test_cutover_failure_after_each_commit_phase_rolls_back(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
    inject: Callable[[sqlite3.Connection], None],
) -> None:
    before = _seed_old_home(tmp_path)
    if phase == "delete":
        monkeypatch.setattr(legacy_workflows, "_copy_and_empty_effect_tables", inject)
    elif phase == "trigger":
        monkeypatch.setattr(legacy_workflows, "_seal_tables", inject)
    else:
        monkeypatch.setattr(legacy_workflows, "_copy_and_empty_effect_tables", inject)

    with pytest.raises(RuntimeError, match="injected"):
        cutover(tmp_path, at=104)
    _assert_live_effects_unchanged(tmp_path, before)


def test_daemon_logs_one_cutover_cancellation_per_run(tmp_path: Path) -> None:
    app = _new_application(tmp_path)
    _seed_old_home(app.state_dir)
    try:
        app._start_runtime()
        events = _daemon_events(app.state_dir)
        cancelled = [
            event
            for event in events
            if event.get("event") == "workflow.cutover_cancelled"
        ]
        assert [(event["table"], event["runId"]) for event in cancelled] == [
            ("agent_task_runs", "task-live"),
            ("runs", "run-live"),
        ]
        assert all(event["sender"] for event in cancelled)
        assert all(event["priorState"] == "running" for event in cancelled)
        assert all(event["reason"] == CUTOVER_REASON for event in cancelled)
        assert all(event["at"] == 0 or isinstance(event["at"], int) for event in cancelled)
        assert all("task body" not in json.dumps(event) for event in cancelled)
    finally:
        app._close()


def _new_application(tmp_path: Path) -> DaemonApplication:
    home = tmp_path / "home"
    state_dir = home / "state"
    state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    app = DaemonApplication(
        state_dir=state_dir,
        socket_path=state_dir / "daemon.sock",
        node_id="upgrade-safety-node",
        hyprial_home=home,
        owner="tester",
        zenoh_listen=("tcp/127.0.0.1:0",),
        zenoh_connect=(),
    **_deep_module_daemon_dependencies())
    # Direct construction deliberately does not consult the environment.
    # Make this complete-runtime fixture isolated by construction, then assert
    # the decisions startup actually made rather than trusting only env flags.
    app.network_isolated = True
    return app


def _daemon_events(state_dir: Path) -> list[dict[str, Any]]:
    path = state_dir / "logs" / "daemon.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines() if line]


def _assert_workflow_degraded(app: DaemonApplication) -> None:
    _assert_isolated_startup(app)
    assert app._workflow_service is None
    assert app._remote_workflow is None
    assert app._routine_service is None
    assert app._pac_actor_service is None
    assert app._runtime is not None
    app._run_scheduled_domains(0.0)
    assert app._record_workflow_outcome(SimpleNamespace(delivery_id="workflow-late")) is False
    assert app._response(
        b'{"version":1,"id":"w","method":"workflow.start",'
        b'"params":{"actor":"user:tester","yaml":""}}'
    )["error"]["code"] == ipc_errors.WORKFLOW_UNAVAILABLE
    assert app._response(
        b'{"version":1,"id":"wl","method":"workflow.list",'
        b'"params":{"actor":"user:tester"}}'
    )["error"]["code"] == ipc_errors.WORKFLOW_UNAVAILABLE
    assert app._response(
        b'{"version":1,"id":"wc","method":"workflow.complete",'
        b'"params":{"actor":"user:tester"}}'
    )["error"]["code"] == ipc_errors.WORKFLOW_UNAVAILABLE
    assert app._response(
        b'{"version":1,"id":"r","method":"routine.list",'
        b'"params":{"actor":"user:tester"}}'
    )["error"]["code"] == ipc_errors.ROUTINE_UNAVAILABLE
    assert app._response(
        b'{"version":1,"id":"rs","method":"routine.status",'
        b'"params":{"actor":"user:tester"}}'
    )["error"]["code"] == ipc_errors.ROUTINE_UNAVAILABLE


def _assert_isolated_startup(app: DaemonApplication) -> None:
    status = app._network_isolation_status()
    assert status["effective"] is True
    assert status["checks"]["endpointsLoopbackOnly"] is True
    assert status["checks"]["listenNotDerived"] is True
    assert status["checks"]["discoveryNotConsulted"] is True
    assert status["checks"]["gossipOff"] is True


def test_startup_cutover_failure_degrades_and_keeps_core_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        legacy_workflows,
        "cutover",
        lambda _state: (_ for _ in ()).throw(sqlite3.OperationalError("locked")),
    )
    app = _new_application(tmp_path)
    try:
        app._start_runtime()
        _assert_workflow_degraded(app)
        event = next(
            event
            for event in _daemon_events(app.state_dir)
            if event.get("event") == "workflow.cutover_failed"
        )
        assert event["level"] == "error"
        assert event["component"] == "workflow"
        assert event["exceptionClass"] == "OperationalError"
        assert event["sealed"] is False
        assert "workflow.dispatch" in event["disabledCapabilities"]
    finally:
        app._close()


def test_startup_workflow_construction_failure_degrades(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(**_kwargs: Any) -> None:
        raise OSError("read-only workflow database")

    monkeypatch.setattr(daemon_application, "GraphWorkflowService", fail)
    app = _new_application(tmp_path)
    try:
        app._start_runtime()
        _assert_workflow_degraded(app)
        event = next(
            event
            for event in _daemon_events(app.state_dir)
            if event.get("event") == "workflow.recovery_unavailable"
        )
        assert event["phase"] == "construct"
        assert event["exceptionClass"] == "OSError"
        assert event["disabledCapabilities"]
    finally:
        app._close()


def test_startup_graph_authority_failure_never_enables_direct_writer_fallbacks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(*_args: Any, **_kwargs: Any) -> None:
        raise sqlite3.OperationalError("read-only PAC database")

    monkeypatch.setattr(pac_authority, "PacGraphAuthority", fail)
    app = _new_application(tmp_path)
    try:
        app._start_runtime()
        _assert_workflow_degraded(app)
        assert app._pac_graph_authority is None
        event = next(
            event
            for event in _daemon_events(app.state_dir)
            if event.get("event") == "pac.graph_authority_unavailable"
        )
        assert event["errorType"] == "OperationalError"
        assert event["disabledCapabilities"] == [
            "workflow.dispatch",
            "workflow.mutation",
            "workflow.recovery",
            "workflow.remote",
            "routine.dispatch",
            "pac.actor.cadence",
            "pac.gc",
        ]
    finally:
        app._close()


def test_startup_remote_failure_keeps_local_workflow_and_routine_available(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(*_args: Any, **_kwargs: Any) -> None:
        raise PacError("REMOTE_TEST_FAILURE", "remote wire unavailable")

    monkeypatch.setattr(remote_workflow, "RemoteWorkflow", fail)
    app = _new_application(tmp_path)
    try:
        app._start_runtime()
        _assert_isolated_startup(app)
        assert app._workflow_service is not None
        assert app._routine_service is not None
        assert app._remote_workflow is None
        app._run_scheduled_domains(0.0)
        assert app._response(
            b'{"version":1,"id":"remote","method":"workflow.remote.current",'
            b'"params":{"messageId":"m"}}'
        )["error"]["code"] == "WORKFLOW_REMOTE_UNAVAILABLE"
        event = next(
            event
            for event in _daemon_events(app.state_dir)
            if event.get("event") == "workflow.remote_unavailable"
        )
        assert event["exceptionClass"] == "PacError"
        assert "workflow.remote.admission" in event["disabledCapabilities"]
    finally:
        app._close()


def test_startup_recovery_failure_degrades_and_does_not_report_zero_adopted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(_self: GraphWorkflowService) -> int:
        raise sqlite3.OperationalError("COUNT failed")

    monkeypatch.setattr(GraphWorkflowService, "recover", fail)
    app = _new_application(tmp_path)
    try:
        app._start_runtime()
        _assert_workflow_degraded(app)
        events = _daemon_events(app.state_dir)
        recovery = [
            event
            for event in events
            if event.get("event") == "workflow.recovery_unavailable"
        ]
        assert any(event["phase"] == "recover" for event in recovery)
        assert not any(event.get("event") == "workflow.recovered" for event in events)
    finally:
        app._close()


def test_recovery_cleanup_failure_is_logged_and_handle_retained(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        GraphWorkflowService,
        "recover",
        lambda _self: (_ for _ in ()).throw(PacError("RECOVER", "broken")),
    )
    monkeypatch.setattr(
        GraphWorkflowService,
        "close",
        lambda _self, _timeout=5.0: (_ for _ in ()).throw(RuntimeError("close failed")),
    )
    app = _new_application(tmp_path)
    try:
        app._start_runtime()
        assert app._workflow_service is None
        assert app._degraded_workflow_handles
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            if any(
                event.get("event") == "workflow.degrade_cleanup_failed"
                for event in _daemon_events(app.state_dir)
            ):
                break
            time.sleep(0.005)
        assert any(
            event.get("event") == "workflow.degrade_cleanup_failed"
            for event in _daemon_events(app.state_dir)
        )
    finally:
        monkeypatch.undo()
        app._close()


def test_remote_constructor_partial_failure_cleans_wire(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "pac-graph.sqlite3"
    from hyprial.daemon.impl.pac.storage.store import PacGraphStore

    PacGraphStore(database).close()
    wire_events: list[str] = []

    class Wire:
        def __init__(self, *_args: Any) -> None:
            pass

        def stop(self) -> None:
            wire_events.append("stop")

        def drain(self, _timeout: float) -> bool:
            wire_events.append("drain")
            return True

    class Thread:
        def __init__(self, **_kwargs: Any) -> None:
            pass

        def start(self) -> None:
            raise RuntimeError("thread start failed")

        def join(self, _timeout: float) -> None:
            wire_events.append("join")

    real_threading = remote_workflow.threading

    class _ThreadingProxy:
        def __getattr__(self, name: str) -> Any:
            return getattr(real_threading, name)

        def Thread(self, *args: Any, **kwargs: Any) -> Any:
            if kwargs.get("name") == "pac-remote-outcomes":
                return Thread(**kwargs)
            return real_threading.Thread(*args, **kwargs)

    application = SimpleNamespace(
        state_dir=tmp_path,
        _dispatch_service_actor="agent:owner:node:daemon",
        _workflow_service=object(),
    )
    monkeypatch.setattr(remote_workflow, "RemoteWire", Wire)
    # Limit the late-acquisition fault to this module's outcome pump. Replacing
    # threading.Thread globally would fail ActorRuntime before Wire exists.
    monkeypatch.setattr(remote_workflow, "threading", _ThreadingProxy())
    with pytest.raises(RuntimeError, match="thread start failed"):
        remote_workflow.RemoteWorkflow(application, object())
    assert wire_events == ["stop", "drain", "join"]


def test_healthy_startup_keeps_all_workflow_services(
    tmp_path: Path,
) -> None:
    app = _new_application(tmp_path)
    try:
        app._start_runtime()
        assert app._workflow_service is not None
        assert app._remote_workflow is not None
        assert app._routine_service is not None
        assert app._pac_actor_service is not None
    finally:
        app._close()


_REAL_COPY = legacy_workflows._copy_and_empty_effect_tables
_REAL_SEAL = legacy_workflows._seal_tables
