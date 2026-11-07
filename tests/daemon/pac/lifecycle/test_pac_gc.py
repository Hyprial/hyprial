"""PAC GC removes only reclaimed workflow-owned worker agents."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from typer.main import get_command
from typer.testing import CliRunner

from hyprial.identity.impl.agents import AgentRegistry
from hyprial.kernel import PAC_GC_INTERVAL_SECONDS
from hyprial.daemon.impl.pac.actors.gc import PacGc
from hyprial.daemon.impl.pac.graphs.edits import close_graph
from hyprial.daemon.impl.pac.actors.coordinator import RuntimeObservation
from hyprial.daemon.impl.pac.graphs.reactor import NullSender
from hyprial.daemon.impl.pac.storage.store import PacGraphStore
from hyprial.daemon.impl.pac.workflows.runtime import GraphWorkflowService
from hyprial.shell.impl.cli.commands.common import support as _cli_support
from hyprial.shell.impl.cli.commands.common.root import app as _cli_root_app
from hyprial.shell.impl.cli.commands.common.services import get_services as _cli_get_services
import hyprial.shell.impl.cli.commands.doctor.commands as _cli_owner_1
import hyprial.shell.impl.cli.runtime.entry  # noqa: F401  (installs the explicit CLI composition)


USER = "user:test"
BORROWED = "agent:test:machine:wf-borrowed-adversary"


class Runtime:
    def __init__(self) -> None:
        self.present: set[str] = set()

    def observe(self, actor_name: str) -> RuntimeObservation:
        return RuntimeObservation(actor_name in self.present)


class Application:
    def __init__(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        home.mkdir(mode=0o700)
        self.agents = AgentRegistry(
            tmp_path / "state" / "agents.sqlite3",
            owner="test",
            machine="machine",
            hyprial_home=home,
        )
        self.destroyed: list[str] = []

    def _destroy_agent(
        self, name: str, *, expected_entity_token: str | None = None,
        require_unpinned: bool = False, require_offline: bool = False,
    ) -> dict[str, object]:
        self.destroyed.append(name)
        current = self.agents.get(name)
        if current is not None and expected_entity_token is not None and current.entity_token != expected_entity_token:
            return {"destroyed": False, "agent": name}
        return {"destroyed": self.agents.destroy(name), "agent": name}


def _service(tmp_path: Path) -> GraphWorkflowService:
    return GraphWorkflowService(
        state_dir=tmp_path / "state",
        owner="test",
        machine="machine",
        sender=NullSender(),
        start_thread=False,
    )


def _start_owned(service: GraphWorkflowService, key: str) -> str:
    return service.start(
        yaml_text=f"""
version: 2
name: {key}
nodes:
  - id: work
    task: work
""",
        sender=USER,
        operation_key=key,
    )["graphId"]


def _start_borrowed(service: GraphWorkflowService, key: str) -> str:
    return service.start(
        yaml_text=f"""
version: 2
name: {key}
nodes:
  - id: work
    owner: {BORROWED}
    task: work
""",
        sender=USER,
        operation_key=key,
    )["graphId"]


def _register_owned_worker(
    application: Application,
    store: PacGraphStore,
    graph_id: str,
    *,
    cleanup: str | None = "complete",
) -> tuple[str, Path]:
    [receipt] = store.workflow_worker_receipts(graph_id)
    actor_name = str(receipt["actor_name"])
    agent = application.agents.create(actor_name)
    home = Path(application.agents.home_receipt(actor_name).path)
    store._db.execute(
        "UPDATE workflow_worker_receipts SET agent_entity_token=? WHERE graph_id=?",
        (agent.entity_token, graph_id),
    )
    if cleanup is not None:
        store._db.execute(
            "UPDATE workflow_worker_cleanup_intents SET state=? WHERE graph_id=?",
            (cleanup, graph_id),
        )
        if cleanup == "complete":
            store._db.execute(
                "UPDATE workflow_worker_receipts SET state='down' WHERE graph_id=?",
                (graph_id,),
            )
    store._db.commit()
    return actor_name, home


def _close(store: PacGraphStore, graph_id: str) -> None:
    close_graph(store, graph_id, actor=USER)


def _gc(
    tmp_path: Path,
    application: Application,
    runtime: Runtime,
    logs: list[tuple[str, str, str, dict[str, Any]]],
    *,
    max_removals: int = 50,
    start_thread: bool = False,
) -> PacGc:
    return PacGc(
        state_dir=tmp_path / "state",
        application=application,
        runtime=runtime,
        logger=lambda level, component, event, **fields: logs.append(
            (level, component, event, fields)
        ),
        max_removals=max_removals,
        start_thread=start_thread,
        jitter=lambda: 0.5,
    )


def test_completed_cleanup_owned_worker_is_removed_with_its_home(tmp_path: Path) -> None:
    application = Application(tmp_path)
    runtime = Runtime()
    logs: list[tuple[str, str, str, dict[str, Any]]] = []
    service = _service(tmp_path)
    try:
        graph_id = _start_owned(service, "completed-cleanup")
        store = PacGraphStore(service.database)
        try:
            _close(store, graph_id)
            actor_name, home = _register_owned_worker(application, store, graph_id)
        finally:
            store.close()

        gc = _gc(tmp_path, application, runtime, logs)
        report = gc.run_pass()

        assert report["selected"] == 1
        assert report["removed"] == 1
        assert application.agents.get(actor_name) is None
        assert not home.exists()
        assert gc.status() == {
            "lastPassAtMs": report["atMs"],
            "lastRemovedCount": 1,
            "removableBacklog": 0,
        }
        [(level, component, event, fields)] = logs
        assert (level, component, event) == ("info", "pac", "pac.gc.pass")
        assert fields["selected"] == 1
        assert fields["removed"] == 1
        assert fields["errors"] == 0
        assert isinstance(fields["durationMs"], int)
    finally:
        service.close()
        application.agents.close()


def test_open_graph_worker_is_never_removed(tmp_path: Path) -> None:
    application = Application(tmp_path)
    service = _service(tmp_path)
    try:
        graph_id = _start_owned(service, "open-graph")
        store = PacGraphStore(service.database)
        try:
            [receipt] = store.workflow_worker_receipts(graph_id)
            actor_name = str(receipt["actor_name"])
            application.agents.create(actor_name)
        finally:
            store.close()
        report = _gc(tmp_path, application, Runtime(), []).run_pass()
        assert report["removed"] == 0
        assert report["skipped"] == {"graph-open": 1}
        assert application.agents.get(actor_name) is not None
    finally:
        service.close()
        application.agents.close()


def test_borrowed_wf_named_actor_is_never_removed(tmp_path: Path) -> None:
    application = Application(tmp_path)
    service = _service(tmp_path)
    try:
        graph_id = _start_borrowed(service, "borrowed-prefix")
        store = PacGraphStore(service.database)
        try:
            _close(store, graph_id)
        finally:
            store.close()
        application.agents.create("wf-borrowed-adversary")

        report = _gc(tmp_path, application, Runtime(), []).run_pass()

        assert report["removed"] == 0
        assert report["skipped"] == {"not-workflow-owned": 1}
        assert application.agents.get(BORROWED) is not None
    finally:
        service.close()
        application.agents.close()


def test_ownership_guard_rejects_a_borrowed_roster_row_without_a_receipt() -> None:
    assert not PacGc._workflow_owned(
        {"ownership": "borrowed", "owner": BORROWED},
        None,
        actor_uri=BORROWED,
        actor_name="wf-borrowed-adversary",
    )


def test_cleanup_pending_blocks_removal_until_complete(tmp_path: Path) -> None:
    application = Application(tmp_path)
    service = _service(tmp_path)
    try:
        graph_id = _start_owned(service, "pending-cleanup")
        store = PacGraphStore(service.database)
        try:
            _close(store, graph_id)
            actor_name, _home = _register_owned_worker(
                application, store, graph_id, cleanup="pending"
            )
        finally:
            store.close()
        gc = _gc(tmp_path, application, Runtime(), [])

        pending = gc.run_pass()
        assert pending["removed"] == 0
        assert pending["skipped"] == {"cleanup-pending": 1}
        assert application.agents.get(actor_name) is not None

        store = PacGraphStore(service.database)
        try:
            store._db.execute(
                "UPDATE workflow_worker_cleanup_intents SET state='complete' "
                "WHERE graph_id=?",
                (graph_id,),
            )
            store._db.execute(
                "UPDATE workflow_worker_receipts SET state='down' WHERE graph_id=?",
                (graph_id,),
            )
            store._db.commit()
        finally:
            store.close()
        assert gc.run_pass()["removed"] == 1
    finally:
        service.close()
        application.agents.close()


def test_never_activated_worker_without_incarnation_receipt_is_preserved(tmp_path: Path) -> None:
    application = Application(tmp_path)
    service = _service(tmp_path)
    try:
        graph_id = _start_owned(service, "never-activated")
        store = PacGraphStore(service.database)
        try:
            _close(store, graph_id)
            [receipt] = store.workflow_worker_receipts(graph_id)
            actor_name = str(receipt["actor_name"])
            application.agents.create(actor_name)
            store._db.execute(
                "DELETE FROM workflow_worker_cleanup_intents WHERE graph_id=?",
                (graph_id,),
            )
            store._db.commit()
        finally:
            store.close()

        report = _gc(tmp_path, application, Runtime(), []).run_pass()

        assert report["removed"] == 0
        assert report["skipped"]["ownership-incarnation-unknown"] == 1
        assert application.agents.get(actor_name) is not None
    finally:
        service.close()
        application.agents.close()


def test_pinned_and_online_workers_are_skipped_with_reasons(tmp_path: Path) -> None:
    application = Application(tmp_path)
    runtime = Runtime()
    service = _service(tmp_path)
    try:
        pinned_graph = _start_owned(service, "pinned-worker")
        online_graph = _start_owned(service, "online-worker")
        store = PacGraphStore(service.database)
        try:
            for graph_id in (pinned_graph, online_graph):
                _close(store, graph_id)
            pinned, _ = _register_owned_worker(application, store, pinned_graph)
            online, _ = _register_owned_worker(application, store, online_graph)
        finally:
            store.close()
        application.agents.pin("test-adapter", pinned)
        runtime.present.add(online)

        report = _gc(tmp_path, application, runtime, []).run_pass()

        assert report["removed"] == 0
        assert report["skipped"] == {"online": 1, "pinned": 1}
        assert application.agents.get(pinned) is not None
        assert application.agents.get(online) is not None
    finally:
        service.close()
        application.agents.close()


def test_dry_run_uses_same_selection_without_writes(tmp_path: Path) -> None:
    application = Application(tmp_path)
    service = _service(tmp_path)
    try:
        graph_id = _start_owned(service, "dry-run")
        store = PacGraphStore(service.database)
        try:
            _close(store, graph_id)
            actor_name, home = _register_owned_worker(application, store, graph_id)
        finally:
            store.close()
        gc = _gc(tmp_path, application, Runtime(), [])

        preview = gc.preview()

        assert preview["dryRun"] is True
        assert preview["selectedCount"] == 1
        assert preview["removableBacklog"] == 1
        assert preview["selected"] == [
            {
                "graphId": graph_id,
                "actor": f"agent:test:machine:{actor_name}",
                "agent": actor_name,
            }
        ]
        assert application.destroyed == []
        assert application.agents.get(actor_name) is not None
        assert home.exists()
    finally:
        service.close()
        application.agents.close()


def test_pass_is_bounded(tmp_path: Path) -> None:
    application = Application(tmp_path)
    service = _service(tmp_path)
    try:
        store = PacGraphStore(service.database)
        try:
            for index in range(3):
                graph_id = _start_owned(service, f"bounded-{index}")
                _close(store, graph_id)
                _register_owned_worker(application, store, graph_id)
        finally:
            store.close()

        report = _gc(
            tmp_path, application, Runtime(), [], max_removals=2
        ).run_pass()

        assert report["selected"] == 2
        assert report["removableBacklog"] == 3
        assert report["removed"] == 2
        assert report["skipped"] == {"batch-limit": 1}
        assert len(application.agents.list()) == 1
    finally:
        service.close()
        application.agents.close()


def test_status_reports_the_last_pass_without_scanning(tmp_path: Path) -> None:
    application = Application(tmp_path)
    gc = _gc(tmp_path, application, Runtime(), [])

    def forbidden() -> None:
        raise AssertionError("daemon status must not run the GC selection")

    gc._select = forbidden  # type: ignore[method-assign]
    assert gc.status() == {
        "lastPassAtMs": None,
        "lastRemovedCount": 0,
        "removableBacklog": None,
    }


def test_close_stops_sleeping_gc_thread_promptly(tmp_path: Path) -> None:
    application = Application(tmp_path)
    gc = _gc(tmp_path, application, Runtime(), [], start_thread=True)
    started = time.monotonic()

    assert gc.close(timeout=0.5)
    assert time.monotonic() - started < 0.5
    assert not gc.thread.is_alive()
    application.agents.close()


def test_gc_cadence_is_five_minutes_with_bounded_jitter(tmp_path: Path) -> None:
    application = Application(tmp_path)
    gc = PacGc(
        state_dir=tmp_path / "state",
        application=application,
        runtime=Runtime(),
        logger=lambda *_args, **_kwargs: None,
        start_thread=False,
        jitter=lambda: 0.0,
    )
    try:
        assert PAC_GC_INTERVAL_SECONDS == 300.0
        assert gc._delay() == 270.0
        gc.jitter = lambda: 1.0
        assert gc._delay() == 330.0
        assert gc.thread.name == "hyprial-pac-gc"
    finally:
        gc.close()
        application.agents.close()


def test_cli_preview_and_help_inventory(monkeypatch: Any) -> None:
    calls: list[tuple[str, dict[str, object]]] = []
    monkeypatch.setattr(_cli_get_services(), "require_initialized_hyprial_home", lambda: None)
    monkeypatch.setattr(
        _cli_support,
        "_daemon_request",
        lambda method, params=None: calls.append((method, params or {}))
        or {"ok": True, "dryRun": True, "selected": [], "selectedCount": 0},
    )
    # The read-only worktree advice has its own suite; keep this test on the
    # daemon preview it exists to pin.
    monkeypatch.setattr(
        "hyprial.shell.impl.cli.commands.workflow.misc.collect_worktree_reports",
        lambda repos, base=None: [],
    )

    result = CliRunner().invoke(_cli_root_app, ["workflow", "gc", "--dry-run", "--json"])

    assert result.exit_code == 0, result.output
    body = json.loads(result.stdout)
    assert body["dryRun"] is True
    assert body["worktrees"] == []
    assert calls == [("pac.gc.preview", {})]
    root = get_command(_cli_root_app)
    assert "pac" not in root.commands
    assert "gc" in root.commands["workflow"].commands


def test_cli_refuses_mutating_gc_mode(monkeypatch: Any) -> None:
    monkeypatch.setattr(_cli_get_services(), "require_initialized_hyprial_home", lambda: None)
    result = CliRunner().invoke(_cli_root_app, ["workflow", "gc", "--json"])

    assert result.exit_code == 1
    assert json.loads(result.stdout)["code"] == "INVALID_ARGUMENT"


def test_workflow_gc_help_and_retired_pac_have_no_ipc(monkeypatch: Any) -> None:
    def unexpected_request(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("help and rejected modes must not issue IPC")

    monkeypatch.setattr(_cli_support, "_daemon_request", unexpected_request)
    monkeypatch.setattr(_cli_get_services(), "require_initialized_hyprial_home", lambda: None)
    runner = CliRunner()
    help_result = runner.invoke(_cli_root_app, ["workflow", "gc", "--help"])
    assert help_result.exit_code == 0, help_result.output
    assert "--dry-run" in help_result.output
    assert "--json" in help_result.output
    workflow_help = runner.invoke(_cli_root_app, ["workflow", "--help"])
    assert workflow_help.exit_code == 0
    assert "gc" in workflow_help.output
    retired = runner.invoke(_cli_root_app, ["pac", "gc", "--dry-run", "--json"])
    assert retired.exit_code == 2
    assert "No such command" in retired.output
    rejected = runner.invoke(_cli_root_app, ["workflow", "gc", "--json"])
    assert rejected.exit_code == 1
    assert json.loads(rejected.stdout)["code"] == "INVALID_ARGUMENT"


def test_gc_doctor_first_pass_hint_uses_workflow_namespace() -> None:
    check = _cli_owner_1._pac_gc_doctor_check(
        {"pacGc": {"lastPassAtMs": None, "lastRemovedCount": 0, "removableBacklog": None}}
    )
    assert check is not None
    assert "hyprial workflow gc --dry-run" in check["detail"]
    assert "hyprial pac gc" not in check["detail"]


def test_doctor_projects_last_pass_and_its_backlog() -> None:
    check = _cli_owner_1._pac_gc_doctor_check(
        {
            "pacGc": {
                "lastPassAtMs": 1234,
                "lastRemovedCount": 2,
                "removableBacklog": 3,
            }
        }
    )

    assert check == {
        "name": "pac-gc",
        "status": "ok",
        "detail": "last pass at 1234 removed 2; 3 worker agent(s) still removable after it",
        "metrics": {
            "lastPassAtMs": 1234,
            "lastRemovedCount": 2,
            "removableBacklog": 3,
        },
    }


def test_gc_does_not_destroy_replacement_after_selection(tmp_path: Path) -> None:
    application = Application(tmp_path)
    runtime = Runtime()
    service = _service(tmp_path)
    try:
        graph_id = _start_owned(service, "replacement-after-selection")
        store = PacGraphStore(service.database)
        try:
            _close(store, graph_id)
            actor_name, _ = _register_owned_worker(application, store, graph_id)
        finally:
            store.close()
        collector = _gc(tmp_path, application, runtime, [])
        select = collector._select
        replacement_tokens: list[str] = []

        def select_then_replace():
            selected = select()
            assert len(selected.candidates) == 1
            _original = application.agents.require(actor_name)
            application.agents.destroy(actor_name)
            replacement = application.agents.create(actor_name)
            replacement_tokens.append(replacement.entity_token)
            return selected

        collector._select = select_then_replace
        report = collector.run_pass()
        current = application.agents.get(actor_name)
        assert current is not None, "GC deleted a replacement it did not select"
        assert current.entity_token == replacement_tokens[0]
        assert report["removed"] == 0
    finally:
        service.close()
        application.agents.close()
