"""S2: ``login --switch-account --dry-run`` snapshot/classification contract."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
import time
from pathlib import Path

import pytest
from typer.testing import CliRunner
from hyprial.shell.impl.cli.commands.common.root import app as _cli_root_app
from hyprial.shell.impl.cli.commands.common.services import get_services as _cli_get_services
import hyprial.shell.impl.cli.runtime.entry  # noqa: F401  (installs the explicit CLI composition)

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / 'tests/fixtures'))

from hyprial.daemon.impl.configuration.identity import write_settings_owner
from hyprial.daemon.impl.configuration.network_profile import NetworkProfile
from hyprial.shell.impl.login.flow import run_login
from hyprial.shell.impl.login.preview import LoginPreviewError, preview_owner_migration
from tests.fixtures.network.fake_oidc import fake_oidc  # noqa: E402

OLD = "old-owner"
NEW = "alicesmith"
EXPECTED_SOURCES = {
    "agents.sqlite3",
    "adapters.sqlite3",
    "inbox.sqlite3",
    "lifecycle-operations.sqlite3",
    "routines.sqlite3",
    "workflows.sqlite3",
    "pac-graph.sqlite3",
    "desired-state.json.v1",
    "users.json",
}


def _profile(server) -> NetworkProfile:
    return NetworkProfile(
        org="acme",
        issuer=server.issuer,
        client_id="fake-client-id",
    )


def _seed_database(path: Path, sql: str = "CREATE TABLE empty (value TEXT)") -> None:
    connection = sqlite3.connect(path)
    with connection:
        connection.execute(sql)
    connection.close()


def _seed_complete_state(state: Path) -> None:
    state.mkdir()
    connection = sqlite3.connect(state / "agents.sqlite3")
    with connection:
        connection.execute(
            "CREATE TABLE agents (owner TEXT, actor TEXT, uri TEXT)"
        )
        connection.execute(
            "INSERT INTO agents VALUES (?, ?, ?)",
            (OLD, "worker", f"agent:{OLD}:node:worker"),
        )
    connection.close()
    for name in (
        "adapters.sqlite3",
        "inbox.sqlite3",
        "lifecycle-operations.sqlite3",
        "routines.sqlite3",
        "workflows.sqlite3",
    ):
        _seed_database(state / name)
    # The real PAC schema (store.connect runs the real migrations) with one
    # owner-bearing row: pac-graph.sqlite3 joined the shared inventory with
    # P1b B0, and the complete-inventory contract covers it like the rest.
    from hyprial.daemon.impl.pac.storage.store import connect as _pac_connect

    pac = _pac_connect(state / "pac-graph.sqlite3")
    with pac:
        pac.execute(
            "INSERT INTO graphs (graph_id, name, version, created_by,"
            " created_at) VALUES ('g1', 'graph', 1, ?, 1)",
            (f"user:{OLD}",),
        )
    pac.close()
    (state / "desired-state.json.v1").write_text(
        json.dumps({"actor": f"agent:{OLD}:node:worker"}), encoding="utf-8"
    )
    (state / "users.json").write_text(
        json.dumps({"owner": OLD}), encoding="utf-8"
    )


def _snapshot(root: Path) -> dict[str, bytes | None]:
    return {
        str(path.relative_to(root)): path.read_bytes() if path.is_file() else None
        for path in sorted(root.rglob("*"))
    }


def _without_pac_reader_artifacts(
    snapshot: dict[str, bytes | None],
) -> dict[str, bytes | None]:
    """Drop pac-graph's WAL reader artifacts from a state snapshot.

    pac-graph.sqlite3 is the only WAL-mode source in these seeds (the real
    store schema); reading a WAL database can materialize its -wal/-shm —
    the same SQLite protocol artifacts the preview's source guard classifies
    as SQLiteReadArtifact rather than source changes.  Everything else,
    pac-graph.sqlite3 itself included, must stay byte-identical.
    """

    return {
        name: content
        for name, content in snapshot.items()
        if not name.startswith("pac-graph.sqlite3-")
    }


def test_preview_uses_the_complete_shared_inventory_and_never_applies(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    state = tmp_path / "state"
    _seed_complete_state(state)
    write_settings_owner(OLD, environ={"HYPRIAL_HOME": str(home)})
    before_home = _snapshot(home)
    before_state = _snapshot(state)

    result = preview_owner_migration(
        state_dir=state, hyprial_home=home, target_owner=NEW
    )

    assert {source.name for source in result.sources} == EXPECTED_SOURCES
    assert all(source.status == "snapshotted" for source in result.sources)
    assert result.ready is True
    assert result.settings_owner == OLD
    assert result.data_owner == OLD
    assert result.rewritten_cells >= 2
    assert result.changed_files == 2
    assert result.scanned_columns >= 7
    assert result.as_dict()["scan"]["sourceCount"] == 9
    assert _snapshot(home) == before_home
    assert _without_pac_reader_artifacts(_snapshot(state)) == (
        _without_pac_reader_artifacts(before_state)
    )


def test_concurrent_non_migration_write_does_not_invalidate_preview(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Live logs/receipts are not migration inputs or commit evidence."""

    home = tmp_path / "home"
    home.mkdir()
    state = tmp_path / "state"
    _seed_complete_state(state)
    write_settings_owner(OLD, environ={"HYPRIAL_HOME": str(home)})
    import hyprial.shell.impl.login.preview as preview_module

    original_backup = preview_module._backup_database
    writes: list[Path] = []

    def backup_while_log_writer_runs(source: Path, destination: Path) -> None:
        original_backup(source, destination)
        if not writes:
            unrelated = state / "logs" / "daemon.jsonl"
            unrelated.parent.mkdir()
            unrelated.write_text('{"event":"still-running"}\n', encoding="utf-8")
            writes.append(unrelated)

    monkeypatch.setattr(preview_module, "_backup_database", backup_while_log_writer_runs)

    result = preview_owner_migration(
        state_dir=state, hyprial_home=home, target_owner=NEW
    )

    assert result.ready is True
    assert writes == [state / "logs" / "daemon.jsonl"]
    assert result.rewritten_cells >= 2


def test_concurrent_migration_database_write_uses_backup_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A different writer changing DB bytes does not invalidate backup()."""

    home = tmp_path / "home"
    home.mkdir()
    state = tmp_path / "state"
    _seed_complete_state(state)
    write_settings_owner(OLD, environ={"HYPRIAL_HOME": str(home)})
    import hyprial.shell.impl.login.preview as preview_module

    original_backup = preview_module._backup_database
    writes: list[str] = []

    def backup_then_inbox_writer(source: Path, destination: Path) -> None:
        original_backup(source, destination)
        if source.name == "inbox.sqlite3":
            writer = sqlite3.connect(source)
            with writer:
                writer.execute("INSERT INTO empty VALUES (?)", ("live-message",))
            writer.close()
            writes.append(source.name)

    monkeypatch.setattr(preview_module, "_backup_database", backup_then_inbox_writer)

    result = preview_owner_migration(
        state_dir=state, hyprial_home=home, target_owner=NEW
    )

    assert result.ready is True
    assert writes == ["inbox.sqlite3"]
    assert result.rewritten_cells >= 2


def test_closed_wal_database_without_auxiliary_files_is_ready_and_reported(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    state = tmp_path / "state"
    _seed_complete_state(state)
    write_settings_owner(OLD, environ={"HYPRIAL_HOME": str(home)})
    database_name = "adapters.sqlite3"
    database = sqlite3.connect(state / database_name)
    assert database.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)
    database.close()
    wal = state / f"{database_name}-wal"
    shm = state / f"{database_name}-shm"
    assert not wal.exists()
    assert not shm.exists()

    result = preview_owner_migration(
        state_dir=state, hyprial_home=home, target_owner=NEW
    )

    assert result.ready is True
    artifacts = result.as_dict()["sqliteReadArtifacts"]
    observed = {(item["path"], item["effect"]) for item in artifacts}
    # pac-graph.sqlite3 joined the inventory (P1b B0) and is WAL-mode in the
    # real schema, so its own reader artifacts may appear here too — every
    # artifact must still be protocol-classified, and the adapters pair from
    # THIS test's setup must be among them.
    assert {
        (
            f"{database_name}-wal",
            "created-by-sqlite-read-protocol",
        ),
        (
            f"{database_name}-shm",
            "created-by-sqlite-read-protocol",
        ),
    } <= observed
    assert observed <= {
        (f"{name}-{suffix}", "created-by-sqlite-read-protocol")
        for name in ("adapters.sqlite3", "pac-graph.sqlite3")
        for suffix in ("wal", "shm")
    }
    assert wal.stat().st_size == 0
    assert shm.stat().st_size >= 32768


def test_new_nonempty_wal_is_rejected_as_a_real_source_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    state = tmp_path / "state"
    _seed_complete_state(state)
    write_settings_owner(OLD, environ={"HYPRIAL_HOME": str(home)})
    import hyprial.shell.impl.login.preview as preview_module

    original_backup = preview_module._backup_database

    def backup_then_tamper(source: Path, destination: Path) -> None:
        original_backup(source, destination)
        if source.name == "agents.sqlite3":
            (state / "agents.sqlite3-wal").write_bytes(b"not-an-empty-wal")

    monkeypatch.setattr(preview_module, "_backup_database", backup_then_tamper)

    with pytest.raises(LoginPreviewError) as caught:
        preview_owner_migration(
            state_dir=state, hyprial_home=home, target_owner=NEW
        )

    assert caught.value.code == "PREVIEW_SOURCE_CHANGED"
    assert caught.value.data["changedSource"] == "agents.sqlite3-wal"


def test_preview_sqlite_backup_includes_uncheckpointed_wal_rows(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    state = tmp_path / "state"
    state.mkdir()
    write_settings_owner(OLD, environ={"HYPRIAL_HOME": str(home)})
    writer = sqlite3.connect(state / "agents.sqlite3")
    writer.execute("PRAGMA journal_mode=WAL")
    writer.execute("PRAGMA wal_autocheckpoint=0")
    writer.execute("CREATE TABLE agents (owner TEXT, actor TEXT, uri TEXT)")
    writer.commit()
    writer.execute(
        "INSERT INTO agents VALUES (?, ?, ?)",
        (OLD, "wal-worker", f"agent:{OLD}:node:wal-worker"),
    )
    writer.commit()
    assert (state / "agents.sqlite3-wal").exists()
    before = {
        name: content
        for name, content in _snapshot(state).items()
        if not name.endswith("-shm")
    }
    try:
        result = preview_owner_migration(
            state_dir=state, hyprial_home=home, target_owner=NEW
        )
        assert result.rewritten_cells == 2
        assert {
            name: content
            for name, content in _snapshot(state).items()
            if not name.endswith("-shm")
        } == before
        assert (state / "agents.sqlite3-shm").exists()
    finally:
        writer.close()


def test_preview_reports_missing_and_corrupt_sources_differently(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    state = tmp_path / "state"
    state.mkdir()
    write_settings_owner(OLD, environ={"HYPRIAL_HOME": str(home)})
    (state / "agents.sqlite3").write_bytes(b"not sqlite")

    with pytest.raises(LoginPreviewError) as caught:
        preview_owner_migration(
            state_dir=state, hyprial_home=home, target_owner=NEW
        )

    assert caught.value.code == "PREVIEW_SOURCE_UNREADABLE"
    assert caught.value.data == {"source": "agents.sqlite3", "kind": "sqlite"}
    assert "absent" not in str(caught.value)


def test_preview_reports_registered_archival_columns(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    state = tmp_path / "state"
    state.mkdir()
    write_settings_owner(OLD, environ={"HYPRIAL_HOME": str(home)})
    agents = sqlite3.connect(state / "agents.sqlite3")
    with agents:
        agents.execute("CREATE TABLE agents (owner TEXT, actor TEXT, uri TEXT)")
        agents.execute("INSERT INTO agents VALUES (?, ?, ?)", (OLD, "worker", f"agent:{OLD}:node:worker"))
    agents.close()
    workflows = sqlite3.connect(state / "workflows.sqlite3")
    with workflows:
        workflows.execute("CREATE TABLE runs (yaml_text TEXT, report_text TEXT)")
        workflows.execute(
            "INSERT INTO runs VALUES (?, ?)",
            (f"history {OLD}", f"agent:{OLD}:node:a"),
        )
        workflows.execute("CREATE TABLE targets (reply_excerpt TEXT)")
        workflows.execute("INSERT INTO targets VALUES (?)", (f"reply {OLD}",))
    workflows.close()

    result = preview_owner_migration(
        state_dir=state, hyprial_home=home, target_owner=NEW
    )

    assert set(result.archival_columns) == {
        # Journal identities describe the decision-time incarnation, not
        # current routing; account switches must preserve the original event.
        "agent_grant_journal.actor",
        "agent_grant_journal.entity_token",
        "agent_grant_journal.action",
        "agent_grant_journal.grant_id",
        "agent_grant_journal.capability",
        "agent_grant_journal.scope",
        "agent_grant_journal.by",
        "agent_grant_journal.note",
        "runs.yaml_text",
        "runs.report_text",
        "targets.reply_excerpt",
        "schema_era.report_json",
        "notifications.text",
        "notifications.plan_json",
        "journal.data_json",
        "workflow_graphs.specification_ref", "workflow_graphs.specification_digest",
        "workflow_nodes.input_token", "workflow_nodes.request_id",
        "workflow_deliveries.message_id", "workflow_deliveries.request_id",
        "workflow_graphs.parent_graph_id", "workflow_graphs.parent_node_id",
        "workflow_nodes.node_kind",
        "workflow_expansions.parent_graph_id", "workflow_expansions.parent_node_id",
        "workflow_expansions.child_graph_id", "workflow_expansions.planner_node_id",
        "workflow_expansions.planner_request_id", "workflow_expansions.expansion_digest",
        "workflow_expansions.limits_json",
        "workflow_outcome_receipts.graph_id", "workflow_outcome_receipts.node_id",
        "workflow_outcome_receipts.expansion_digest",
        "remote_workflow_outbox.expansion_text", "remote_workflow_outbox.expansion_digest",
        "workflow_worktrees.graph_id", "workflow_worktrees.node_id",
        "workflow_worktrees.actor_node", "workflow_worktrees.plan_json",
        "workflow_worktrees.state", "workflow_worktrees.reason",
    }
    assert set(result.skipped_archival_columns) == {
        "workflows.sqlite3:runs.yaml_text",
        "workflows.sqlite3:runs.report_text",
        "workflows.sqlite3:targets.reply_excerpt",
    }  # the PAC archival columns exist only where pac-graph.sqlite3 does;
    # this state has none, so they appear in the registry but not as skips


def test_unclassified_context_is_bounded_and_does_not_leak_inbox_text(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    state = tmp_path / "state"
    state.mkdir()
    write_settings_owner(OLD, environ={"HYPRIAL_HOME": str(home)})
    agents = sqlite3.connect(state / "agents.sqlite3")
    with agents:
        agents.execute("CREATE TABLE agents (owner TEXT, actor TEXT, uri TEXT)")
        agents.execute("INSERT INTO agents VALUES (?, ?, ?)", (OLD, "worker", f"agent:{OLD}:node:worker"))
    agents.close()
    secret = "refresh_token=do-not-print-this-inbox-body"
    inbox = sqlite3.connect(state / "inbox.sqlite3")
    with inbox:
        inbox.execute("CREATE TABLE inbox (body TEXT)")
        inbox.execute("INSERT INTO inbox VALUES (?)", (f"{secret}; legacy={OLD}",))
    inbox.close()

    result = preview_owner_migration(
        state_dir=state, hyprial_home=home, target_owner=NEW
    )
    payload = json.dumps(result.as_dict())

    assert result.ready is False
    assert result.unclassified[0]["source"] == "inbox.sqlite3"
    assert result.unclassified[0]["column"] == "inbox.body"
    assert secret not in payload
    assert OLD in result.unclassified[0]["context"]


def test_cleanup_runs_on_interrupt_and_cleanup_failure_is_loud(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    state = tmp_path / "state"
    state.mkdir()
    write_settings_owner(OLD, environ={"HYPRIAL_HOME": str(home)})
    _seed_database(state / "agents.sqlite3")
    import hyprial.shell.impl.login.preview as preview_module

    created = tmp_path / "owned-preview"

    def make_preview(path: Path) -> str:
        path.mkdir()
        return str(path)

    monkeypatch.setattr(
        preview_module.tempfile,
        "mkdtemp",
        lambda **_kwargs: make_preview(created),
    )
    monkeypatch.setattr(
        preview_module,
        "_backup_database",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(KeyboardInterrupt()),
    )
    with pytest.raises(KeyboardInterrupt):
        preview_owner_migration(
            state_dir=state, hyprial_home=home, target_owner=NEW
        )
    assert not created.exists()

    created_again = tmp_path / "owned-preview-residual"
    monkeypatch.setattr(
        preview_module.tempfile,
        "mkdtemp",
        lambda **_kwargs: make_preview(created_again),
    )
    monkeypatch.setattr(preview_module, "_backup_database", lambda *_args: None)
    def fail_cleanup(_path: Path) -> None:
        raise OSError("unlink denied secret body")

    with pytest.raises(LoginPreviewError) as cleanup:
        preview_owner_migration(
            state_dir=state,
            hyprial_home=home,
            target_owner=NEW,
            cleanup=fail_cleanup,
        )
    assert cleanup.value.code == "PREVIEW_CLEANUP_FAILED"
    assert cleanup.value.data["residualDirectory"] == str(created_again)
    assert "secret body" not in str(cleanup.value)
    shutil.rmtree(created_again)


def test_run_login_dry_run_authenticates_but_writes_nothing(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    state = tmp_path / "state"
    _seed_complete_state(state)
    write_settings_owner(OLD, environ={"HYPRIAL_HOME": str(home)})
    before_home = _snapshot(home)
    before_state = _snapshot(state)

    with fake_oidc() as server:
        result = run_login(
            _profile(server),
            profile_source="profile.json",
            environ={
                "HYPRIAL_HOME": str(home),
                "HARNESS_STATE_DIR": str(state),
                "HYPRIAL_OWNER": "",
            },
            hyprial_home=home,
            state_dir=state,
            switch_account=True,
            dry_run=True,
            assume_yes=True,
            open_browser=False,
        )

    assert result.dry_run is True
    assert result.owner == NEW
    assert result.migration_preview["status"] == "ready"
    assert _snapshot(home) == before_home
    assert _without_pac_reader_artifacts(_snapshot(state)) == (
        _without_pac_reader_artifacts(before_state)
    )
    assert not (home / "secrets" / "login.json").exists()


def test_cli_dry_run_allows_live_daemon_and_never_provisions_a_device(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    state = tmp_path / "state"
    _seed_complete_state(state)
    write_settings_owner(OLD, environ={"HYPRIAL_HOME": str(home)})
    from hyprial.daemon.impl.mcp.channel.ownership import _read_process_identity

    heartbeat = {
        "schemaVersion": 1,
        "pid": os.getpid(),
        "processIdentity": _read_process_identity(os.getpid()),
        "generation": "preview-live",
        "heartbeatMonotonic": time.monotonic(),
        "keepaliveDurationSeconds": 5.0,
    }
    (home / ".active_daemon").write_text(json.dumps(heartbeat), encoding="utf-8")
    monkeypatch.setenv("HYPRIAL_HOME", str(home))
    monkeypatch.setenv("HARNESS_STATE_DIR", str(state))
    monkeypatch.setenv("HYPRIAL_PEER_DISCOVERY", "0")
    monkeypatch.delenv("HYPRIAL_OWNER", raising=False)
    with fake_oidc() as server:
        monkeypatch.setattr(_cli_get_services(), "resolve_profile", lambda: (_profile(server), "test"))
        import hyprial.daemon.impl.network.tailcat as tailcat_module

        monkeypatch.setattr(
            tailcat_module,
            "ensure_device_key",
            lambda *_args, **_kwargs: pytest.fail("dry-run attempted device provisioning"),
        )
        before_home = _snapshot(home)
        before_state = _snapshot(state)
        outcome = CliRunner().invoke(
            _cli_root_app,
            ["login", "--switch-account", "--dry-run", "--json", "--no-open"],
        )

    assert outcome.exit_code == 0, outcome.output
    payload = json.loads(outcome.stdout)
    assert payload["identity"]["status"] == "planned"
    assert payload["identity"]["committed"] is False
    assert payload["daemon"]["state"] == "not-attempted"
    assert payload["device"]["status"] == "not-attempted"
    assert _snapshot(home) == before_home
    assert _without_pac_reader_artifacts(_snapshot(state)) == (
        _without_pac_reader_artifacts(before_state)
    )


def test_source_changed_under_live_daemon_reports_daemon_running_and_next_step(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    state = tmp_path / "state"
    _seed_complete_state(state)
    write_settings_owner(OLD, environ={"HYPRIAL_HOME": str(home)})
    from hyprial.daemon.impl.mcp.channel.ownership import _read_process_identity

    heartbeat = {
        "schemaVersion": 1,
        "pid": os.getpid(),
        "processIdentity": _read_process_identity(os.getpid()),
        "generation": "preview-source-change",
        "heartbeatMonotonic": time.monotonic(),
        "keepaliveDurationSeconds": 5.0,
    }
    (home / ".active_daemon").write_text(json.dumps(heartbeat), encoding="utf-8")
    import hyprial.shell.impl.login.preview as preview_module

    original_backup = preview_module._backup_database

    def backup_then_tamper(source: Path, destination: Path) -> None:
        original_backup(source, destination)
        if source.name == "agents.sqlite3":
            (state / "agents.sqlite3-wal").write_bytes(b"not-an-empty-wal")

    monkeypatch.setattr(preview_module, "_backup_database", backup_then_tamper)
    monkeypatch.setenv("HYPRIAL_HOME", str(home))
    monkeypatch.setenv("HARNESS_STATE_DIR", str(state))
    monkeypatch.setenv("HYPRIAL_PEER_DISCOVERY", "0")
    monkeypatch.delenv("HYPRIAL_OWNER", raising=False)
    with fake_oidc() as server:
        monkeypatch.setattr(_cli_get_services(), "resolve_profile", lambda: (_profile(server), "test"))
        before_home = _snapshot(home)
        outcome = CliRunner().invoke(
            _cli_root_app,
            ["login", "--switch-account", "--dry-run", "--json", "--no-open"],
        )

    assert outcome.exit_code == 1, outcome.output
    payload = json.loads(outcome.stdout)
    assert payload["code"] == "PREVIEW_SOURCE_CHANGED"
    assert payload["data"]["changedSource"] == "agents.sqlite3-wal"
    assert payload["data"]["daemonRunning"] is True
    assert payload["data"]["nextStep"] == (
        "the daemon is running and the migration sources keep changing: "
        "retry later, stop it first (`hyprial daemon stop`) and re-run "
        "the dry-run, or run `hyprial login --switch-account` to stop the "
        "daemon, prove its exit, and complete the verified switch"
    )
    # The S3 surface now provides the orchestrated switch, so the remedy
    # names it — while keeping both S2 remedies verbatim.
    assert "--switch-account" in payload["data"]["nextStep"]
    assert "retry later" in payload["data"]["nextStep"]
    assert "re-run the dry-run" in payload["data"]["nextStep"]
    # Nothing was written and the (faked) daemon was never stopped.
    assert _snapshot(home) == before_home
    assert (home / ".active_daemon").exists()


def test_source_changed_without_live_daemon_keeps_the_plain_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    state = tmp_path / "state"
    _seed_complete_state(state)
    write_settings_owner(OLD, environ={"HYPRIAL_HOME": str(home)})
    assert not (home / ".active_daemon").exists()
    import hyprial.shell.impl.login.preview as preview_module

    original_backup = preview_module._backup_database

    def backup_then_tamper(source: Path, destination: Path) -> None:
        original_backup(source, destination)
        if source.name == "agents.sqlite3":
            (state / "agents.sqlite3-wal").write_bytes(b"not-an-empty-wal")

    monkeypatch.setattr(preview_module, "_backup_database", backup_then_tamper)
    monkeypatch.setenv("HYPRIAL_HOME", str(home))
    monkeypatch.setenv("HARNESS_STATE_DIR", str(state))
    monkeypatch.setenv("HYPRIAL_PEER_DISCOVERY", "0")
    monkeypatch.delenv("HYPRIAL_OWNER", raising=False)
    with fake_oidc() as server:
        monkeypatch.setattr(_cli_get_services(), "resolve_profile", lambda: (_profile(server), "test"))
        outcome = CliRunner().invoke(
            _cli_root_app,
            ["login", "--switch-account", "--dry-run", "--json", "--no-open"],
        )

    assert outcome.exit_code == 1, outcome.output
    payload = json.loads(outcome.stdout)
    assert payload["code"] == "PREVIEW_SOURCE_CHANGED"
    assert payload["data"] == {
        "sourceInventoryChanged": True,
        "changedSource": "agents.sqlite3-wal",
    }


def test_switch_help_describes_s3_default_and_no_daemon_contract() -> None:
    from typer.main import get_command

    login_command = get_command(_cli_root_app).commands["login"]
    option = next(
        parameter
        for parameter in login_command.params
        if "--switch-account" in parameter.opts
    )
    assert "By default login stops the old daemon" in option.help
    assert "proves exit" in option.help
    assert "With --no-daemon the daemon must already be stopped" in option.help


def test_dry_run_parameter_conflicts_fail_before_oidc(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HYPRIAL_HOME", str(home))
    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("HYPRIAL_PEER_DISCOVERY", "0")

    missing_switch = CliRunner().invoke(_cli_root_app, ["login", "--dry-run", "--json"])
    assert missing_switch.exit_code == 1
    assert json.loads(missing_switch.stdout)["code"] == "INVALID_ARGUMENT"

    # The retired sidecar flags are gone from the parameter face entirely:
    # combining --dry-run with one is a usage error (exit 2), not a
    # recognized-flag conflict.
    retired_flag = CliRunner().invoke(
        _cli_root_app,
        [
            "login",
            "--switch-account",
            "--dry-run",
            "--install-sidecar",
            "--json",
        ],
    )
    assert retired_flag.exit_code == 2
