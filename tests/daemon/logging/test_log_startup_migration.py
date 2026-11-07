from __future__ import annotations

import json
from pathlib import Path

import pytest

from hyprial.daemon.impl.application import DaemonApplication
from hyprial.kernel.impl.log import migrate_pre_trajectory_logs
from hyprial.shell.impl.daemon_wiring import daemon_dependencies as _deep_module_daemon_dependencies
from hyprial.shell.impl.cli.commands.common import support as _cli_support
import hyprial.shell.impl.cli.commands.message.observe.trajectory as _cli_owner_1
import hyprial.shell.impl.cli.runtime.entry  # noqa: F401  (installs the explicit CLI composition)


def _run_without_runtime(
    application: DaemonApplication, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(application, "_start_runtime", lambda: None)
    monkeypatch.setattr(application, "_start_server", lambda: None)
    monkeypatch.setattr(application, "_restore_adapters", lambda: None)
    monkeypatch.setattr(application, "_write_pid_file", lambda: None)
    monkeypatch.setattr(application, "_serve", lambda: None)
    application.run()
    # Restore owns `daemon.ready` and runs on its own thread; with `_serve`
    # stubbed out, run() returns before that thread does.  The assertions
    # below read the log the thread writes, so wait for the gate.
    assert application._restore_done.wait(2.0), "restore thread never finished"


def _records(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_daemon_startup_archives_existing_logs_once_and_trajectory_stays_clean(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    state_dir = home / "state"
    logs_dir = state_dir / "logs"
    daemon_log = logs_dir / "daemon.jsonl"
    worker_log = logs_dir / "workers" / "pi-old.jsonl"
    daemon_log.parent.mkdir(parents=True)
    worker_log.parent.mkdir(parents=True)
    legacy_line = {
        "ts": "2026-08-16T04:00:00.000Z",
        "level": "warn",
        "component": "daemon",
        "name": "old-node",
        "event": "alarm.raised",
        "correlationId": "legacy-message",
    }
    daemon_bytes = (json.dumps(legacy_line) + "\n").encode()
    worker_bytes = b'{"old":"worker-log"}\n'
    daemon_log.write_bytes(daemon_bytes)
    worker_log.write_bytes(worker_bytes)

    first = DaemonApplication(
        state_dir=state_dir,
        socket_path=state_dir / "daemon.sock",
        node_id="migration-test",
        hyprial_home=home,
        owner="tester",
    **_deep_module_daemon_dependencies())
    _run_without_runtime(first, monkeypatch)

    archive = logs_dir / "archive-pre-trajectory"
    marker = archive / ".migrated"
    assert (archive / "daemon.jsonl").read_bytes() == daemon_bytes
    assert (archive / "workers" / "pi-old.jsonl").read_bytes() == worker_bytes
    marker_payload = json.loads(marker.read_text(encoding="utf-8"))
    assert marker_payload["schemaVersion"] == 1
    assert marker_payload["files"] == ["daemon.jsonl", "workers/pi-old.jsonl"]
    assert marker_payload["migratedAt"].endswith("Z")
    first_marker = marker.read_bytes()
    migration_events = [
        record for record in _records(daemon_log) if record["event"] == "logs.migrated"
    ]
    assert len(migration_events) == 1
    assert migration_events[0]["fileCount"] == 2

    current = {
        "ts": "2026-08-17T01:00:00.000Z",
        "level": "info",
        "component": "daemon",
        "name": "migration-test",
        "event": "send.received",
        "messageId": "current-message",
        "correlationId": "current-message",
        "node": "daemon-send",
        "sender": "agent:alice:mac:sender",
        "target": "agent:bob:mac:worker",
        "conversationId": "conversation-current",
    }
    with daemon_log.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(current) + "\n")

    second = DaemonApplication(
        state_dir=state_dir,
        socket_path=state_dir / "daemon.sock",
        node_id="migration-test",
        hyprial_home=home,
        owner="tester",
    **_deep_module_daemon_dependencies())
    _run_without_runtime(second, monkeypatch)

    assert marker.read_bytes() == first_marker
    assert (archive / "daemon.jsonl").read_bytes() == daemon_bytes
    assert (archive / "workers" / "pi-old.jsonl").read_bytes() == worker_bytes
    active_records = _records(daemon_log)
    assert (
        len(
            [
                record
                for record in active_records
                if record["event"] == "logs.migrated"
            ]
        )
        == 1
    )
    assert current in active_records

    monkeypatch.setenv("HYPRIAL_HOME", str(home))
    monkeypatch.setenv("HARNESS_STATE_DIR", str(state_dir))
    monkeypatch.setattr(
        _cli_support,
        "_daemon_request",
        lambda *_args, **_kwargs: {"ok": True, "state": "unknown", "records": []},
    )
    legacy = _cli_owner_1._trajectory_result("legacy-message")
    active = _cli_owner_1._trajectory_result("current-message")
    assert legacy["found"] is False
    assert legacy["skippedLines"] == 0
    assert active["found"] is True
    assert active["skippedLines"] == 0


def test_empty_home_is_marked_before_new_contract_logs_are_created(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = tmp_path / "home"
    state_dir = home / "state"
    logs_dir = state_dir / "logs"

    first = DaemonApplication(
        state_dir=state_dir,
        socket_path=state_dir / "daemon.sock",
        node_id="empty-migration-test",
        hyprial_home=home,
        owner="tester",
    **_deep_module_daemon_dependencies())
    _run_without_runtime(first, monkeypatch)

    archive = logs_dir / "archive-pre-trajectory"
    marker = archive / ".migrated"
    marker_payload = json.loads(marker.read_text(encoding="utf-8"))
    assert marker_payload["files"] == []
    first_marker = marker.read_bytes()
    first_active_log = (logs_dir / "daemon.jsonl").read_bytes()
    assert any(
        record["event"] == "logs.migrated" and record["fileCount"] == 0
        for record in _records(logs_dir / "daemon.jsonl")
    )

    second = DaemonApplication(
        state_dir=state_dir,
        socket_path=state_dir / "daemon.sock",
        node_id="empty-migration-test",
        hyprial_home=home,
        owner="tester",
    **_deep_module_daemon_dependencies())
    _run_without_runtime(second, monkeypatch)

    assert marker.read_bytes() == first_marker
    assert not (archive / "daemon.jsonl").exists()
    assert (logs_dir / "daemon.jsonl").read_bytes().startswith(first_active_log)
    assert sum(
        record["event"] == "logs.migrated"
        for record in _records(logs_dir / "daemon.jsonl")
    ) == 1


def test_interrupted_migration_resumes_without_archiving_new_contract_log(
    tmp_path: Path,
) -> None:
    state_dir = tmp_path / "state"
    logs_dir = state_dir / "logs"
    archive = logs_dir / "archive-pre-trajectory"
    active_daemon = logs_dir / "daemon.jsonl"
    archived_daemon = archive / "daemon.jsonl"
    pending_worker = logs_dir / "workers" / "pi-old.jsonl"
    active_daemon.parent.mkdir(parents=True)
    archived_daemon.parent.mkdir(parents=True)
    pending_worker.parent.mkdir(parents=True)
    old_daemon_bytes = b'{"old":"daemon-log"}\n'
    new_daemon_bytes = b'{"new":"contract-log"}\n'
    old_worker_bytes = b'{"old":"worker-log"}\n'
    archived_daemon.write_bytes(old_daemon_bytes)
    active_daemon.write_bytes(new_daemon_bytes)
    pending_worker.write_bytes(old_worker_bytes)
    (archive / ".migration-in-progress").write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "startedAt": "2026-08-17T01:00:00.000Z",
                "files": ["daemon.jsonl", "workers/pi-old.jsonl"],
            }
        ),
        encoding="utf-8",
    )

    result = migrate_pre_trajectory_logs(state_dir)

    assert result is not None
    assert result.file_count == 2
    assert archived_daemon.read_bytes() == old_daemon_bytes
    assert active_daemon.read_bytes() == new_daemon_bytes
    assert (archive / "workers" / "pi-old.jsonl").read_bytes() == old_worker_bytes
    assert not pending_worker.exists()
    assert not (archive / ".migration-in-progress").exists()
    marker_payload = json.loads((archive / ".migrated").read_text(encoding="utf-8"))
    assert marker_payload["files"] == ["daemon.jsonl", "workers/pi-old.jsonl"]


def test_archive_collision_never_marks_active_log_as_migrated(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    logs_dir = state_dir / "logs"
    archive = logs_dir / "archive-pre-trajectory"
    active = logs_dir / "daemon.jsonl"
    archived = archive / "daemon.jsonl"
    active.parent.mkdir(parents=True)
    archived.parent.mkdir(parents=True)
    active.write_bytes(b'{"active":"old-log"}\n')
    archived.write_bytes(b'{"archive":"operator-owned"}\n')

    for _attempt in range(2):
        with pytest.raises(FileExistsError, match="daemon.jsonl"):
            migrate_pre_trajectory_logs(state_dir)

        assert active.read_bytes() == b'{"active":"old-log"}\n'
        assert archived.read_bytes() == b'{"archive":"operator-owned"}\n'
        assert not (archive / ".migration-in-progress").exists()
        assert not (archive / ".migrated").exists()


def test_daemon_startup_migration_failure_warns_and_does_not_block(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    state_dir = home / "state"
    application = DaemonApplication(
        state_dir=state_dir,
        socket_path=state_dir / "daemon.sock",
        node_id="migration-failure-test",
        owner="tester",
    **_deep_module_daemon_dependencies())

    def fail_migration() -> None:
        raise PermissionError("migration denied")

    monkeypatch.setattr(application, "_migrate_logs_at_startup", fail_migration)
    _run_without_runtime(application, monkeypatch)

    assert (
        "WARNING: pre-trajectory log migration failed: migration denied"
        in capsys.readouterr().err
    )
    records = _records(state_dir / "logs" / "daemon.jsonl")
    assert any(record["event"] == "logs.migration_failed" for record in records)
    assert any(record["event"] == "daemon.ready" for record in records)


def test_owner_migration_count_is_logged_after_archival_every_start(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The count is emitted on every boot, and never before archival.

    Two things at once, because they failed together (2026-09-04):

    * **It fires at all, including zero.** The count exists so that "migrated
      nothing" stops being indistinguishable from "never ran"; gating the emit
      on ``rewrites > 0`` puts that ambiguity straight back.
    * **It fires after ``_migrate_logs_at_startup``.** The first version sat
      beside the logger's construction, above the boundary this module's
      comment draws, and wrote a pre-archival line -- which the two tests
      above caught only because the emit was later made unconditional.  While
      it was conditional the defect was invisible here and live in production,
      since no test migrates anything.  ⇒ A branch that only runs in
      production is only tested in production.
    """

    home = tmp_path / "home"
    state_dir = home / "state"

    application = DaemonApplication(
        state_dir=state_dir,
        socket_path=state_dir / "daemon.sock",
        node_id="owner-count-test",
        hyprial_home=home,
        owner="tester",
    **_deep_module_daemon_dependencies())
    _run_without_runtime(application, monkeypatch)

    events = [
        record["event"] for record in _records(state_dir / "logs" / "daemon.jsonl")
    ]
    assert "identity.owner_migration.applied" in events, (
        "the migration count was never logged; a start that reports nothing is "
        f"exactly the ambiguity it exists to remove. events={events}"
    )
    assert events.index("logs.migrated") < events.index(
        "identity.owner_migration.applied"
    ), f"owner-migration line written before archival decided what it inherited: {events}"

    applied = [
        record
        for record in _records(state_dir / "logs" / "daemon.jsonl")
        if record["event"] == "identity.owner_migration.applied"
    ]
    assert len(applied) == 1
    assert applied[0]["rewrittenCells"] == 0
    assert applied[0]["owner"] == "tester"
