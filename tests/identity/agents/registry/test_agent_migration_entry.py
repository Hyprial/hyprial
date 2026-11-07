"""Production path for agent-home migration: CLI -> daemon IPC -> coordinator."""

from __future__ import annotations
from tests.support.runtime._owned_applications import close_application, owned_applications as owned_applications

import json
import shutil
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from hyprial.identity.impl.agents import SupportMatrix
from hyprial.identity.impl.agents.migration.entry import load_packaged_support_matrix
from hyprial.daemon.impl.application import DaemonApplication
from hyprial.shell.impl.cli.runtime.services import initialize_hyprial_home
from hyprial.daemon.impl.inbox import InboxService, MemoryDeliveryTransport
from hyprial.shell.impl.daemon_wiring import daemon_dependencies as _deep_module_daemon_dependencies
from hyprial.shell.impl.cli.commands.common.root import app as _cli_root_app
import hyprial.shell.impl.cli.runtime.entry  # noqa: F401  (installs the explicit CLI composition)


class _Harnesses:
    def status(self) -> tuple[object, ...]:
        return ()


class _Presence:
    def online_actors(self) -> tuple[str, ...]:
        return ()

    def actor_online(self, _actor: str) -> bool:
        return False

    def online_mailboxes(self) -> tuple[str, ...]:
        return ()


class _Runtime:
    def stop(self) -> None:
        pass


class _RoutineService:
    def list(self) -> dict[str, list[object]]:
        return {"routines": []}

    def close(self) -> None:
        pass


def _source(root: Path) -> Path:
    source = root / "legacy-config"
    native = source / "native" / "codex"
    skill = source / "shared" / "skills" / "fixture"
    native.mkdir(mode=0o700, parents=True)
    skill.mkdir(mode=0o700, parents=True)
    for directory in (
        source,
        source / "native",
        native,
        source / "shared",
        source / "shared" / "skills",
        skill,
    ):
        directory.chmod(0o700)
    (native / "settings.json").write_text(
        json.dumps({"fixture": "NON_PRODUCTION_SENTINEL"}) + "\n",
        encoding="utf-8",
    )
    (native / "settings.json").chmod(0o600)
    (skill / "SKILL.md").write_text("fixture skill\n", encoding="utf-8")
    (skill / "SKILL.md").chmod(0o600)
    return source


def _manifest(source: Path, *, expires_at_ms: int) -> dict[str, object]:
    return {
        "schemaVersion": 1,
        "subjectKind": "non-resident",
        "authorizationWindow": {
            "windowId": "isolated-entry-test",
            "responsibleOwner": "operator@example.test",
            "expiresAtMs": expires_at_ms,
        },
        "entries": [
            {
                "source": str(source),
                "destination": "config",
                "sensitive": False,
            }
        ],
        "requiredSupport": [
            {
                "harness": "codex",
                "version": "0.155.1",
                "operatingSystem": "Darwin 25.5.0 arm64",
                "entrypoint": "app-server",
                "authMode": "file",
            }
        ],
    }


def _payload(result: Any) -> dict[str, Any]:
    assert result.exit_code == 0, result.output
    value = json.loads(result.stdout)
    assert isinstance(value, dict)
    return value


def _start_isolated_daemon(
    monkeypatch: Any,
) -> tuple[Path, Path, DaemonApplication, threading.Thread, CliRunner]:
    # Short (AF_UNIX path limit) and free of symlinked ancestors, which the
    # migration refuses: /tmp is a symlink to /private/tmp on macOS only.
    root = Path(tempfile.mkdtemp(prefix="hyprial-agent-migrate-", dir=Path("/tmp").resolve()))
    user_home = root / "user-home"
    home = root / "hyprial-home"
    state = root / "state"
    user_home.mkdir(mode=0o700)
    state.mkdir(mode=0o700)
    monkeypatch.setenv("HOME", str(user_home))
    monkeypatch.setenv("HYPRIAL_HOME", str(home))
    monkeypatch.setenv("HARNESS_STATE_DIR", str(state))
    monkeypatch.setenv("HYPRIAL_OWNER", "owner@example.test")
    monkeypatch.setenv("HYPRIAL_NETWORK_ISOLATED", "1")
    monkeypatch.setenv("HYPRIAL_PEER_DISCOVERY", "0")
    initialize_hyprial_home()

    application = DaemonApplication(
        state_dir=state,
        socket_path=state / "daemon.sock",
        node_id="migration-entry-test",
        owner="owner@example.test",
        hyprial_home=home,
    **_deep_module_daemon_dependencies())
    application._inbox = InboxService(  # noqa: SLF001
        state / "inbox.sqlite3", MemoryDeliveryTransport()
    )
    application._harnesses = _Harnesses()  # type: ignore[assignment]  # noqa: SLF001
    application._presence = _Presence()  # type: ignore[assignment]  # noqa: SLF001
    application._runtime = _Runtime()  # type: ignore[assignment]  # noqa: SLF001
    application._routine_service = _RoutineService()  # type: ignore[assignment]  # noqa: SLF001
    application._start_maintenance_scheduler = lambda: None  # type: ignore[method-assign]  # noqa: SLF001
    application.agents.create("fixture-agent")
    application._start_server()  # noqa: SLF001
    dispatcher = threading.Thread(target=application._serve, daemon=True)  # noqa: SLF001
    dispatcher.start()
    return root, state, application, dispatcher, CliRunner()


def _stop_isolated_daemon(
    root: Path, application: DaemonApplication, dispatcher: threading.Thread
) -> None:
    application.stop_event.set()
    if application._server is not None:  # noqa: SLF001
        application._server.close()  # noqa: SLF001
    dispatcher.join(timeout=2.0)
    close_application(application)
    shutil.rmtree(root, ignore_errors=True)


def _preflight(runner: CliRunner, agent: str, manifest_path: Path) -> dict[str, Any]:
    return _payload(
        runner.invoke(
            _cli_root_app,
            [
                "agent",
                "home",
                "migrate",
                "preflight",
                agent,
                "--manifest",
                str(manifest_path),
                "--json",
            ],
        )
    )


def test_cli_ipc_preflight_execute_and_rollback_call_coordinator(
    monkeypatch: Any,
) -> None:
    """All three production commands cross the real Unix-socket boundary.

    Mutation witness: replacing the execute handler's coordinator call with a
    canned complete record leaves ``source`` in place, so the assertion after
    execute goes red even though the fake IPC response says success.
    """

    root, state, application, dispatcher, runner = _start_isolated_daemon(monkeypatch)
    source = _source(root)
    manifest_path = root / "migration-manifest.json"
    expires_at_ms = time.time_ns() // 1_000_000 + 60_000
    manifest_path.write_text(
        json.dumps(_manifest(source, expires_at_ms=expires_at_ms)),
        encoding="utf-8",
    )

    try:
        canonical_agent = application.agents.require("fixture-agent").uri
        preflight = _payload(
            runner.invoke(
                _cli_root_app,
                [
                    "agent",
                    "home",
                    "migrate",
                    "preflight",
                    canonical_agent,
                    "--manifest",
                    str(manifest_path),
                    "--json",
                ],
            )
        )
        migration_id = preflight["migrationId"]
        assert preflight["persisted"] is True
        plan_path = (
            state / "agent-migration-plans" / "fixture-agent" / f"{migration_id}.json"
        )
        assert plan_path.is_file()
        assert plan_path.stat().st_mode & 0o077 == 0

        executed = _payload(
            runner.invoke(
                _cli_root_app,
                [
                    "agent",
                    "home",
                    "migrate",
                    "execute",
                    "fixture-agent",
                    "--migration-id",
                    migration_id,
                    "--json",
                ],
            )
        )
        assert executed["migration"]["phase"] == "complete"
        assert not source.exists()
        target = (
            Path(
                application._agent_registry.home_receipt("fixture-agent").path  # noqa: SLF001
            )
            / "config"
        )
        assert (target / "native" / "codex" / "settings.json").is_file()

        rolled_back = _payload(
            runner.invoke(
                _cli_root_app,
                [
                    "agent",
                    "home",
                    "migrate",
                    "rollback",
                    "fixture-agent",
                    "--migration-id",
                    migration_id,
                    "--window-id",
                    "isolated-rollback-test",
                    "--responsible-owner",
                    "operator@example.test",
                    "--expires-at-ms",
                    str(expires_at_ms),
                    "--json",
                ],
            )
        )
        assert rolled_back["migration"]["phase"] == "rolled-back"
        assert source.is_dir()
        assert not (target / "native" / "codex" / "settings.json").exists()
        assert application.agents.require("fixture-agent").config is None
    finally:
        _stop_isolated_daemon(root, application, dispatcher)


@pytest.mark.parametrize("operation", ["preflight", "execute", "rollback"])
def test_cli_ipc_migration_rejects_foreign_canonical_actor(
    monkeypatch: Any, operation: str
) -> None:
    """A foreign URI must never alias a same-named local migration target."""

    root, state, application, dispatcher, runner = _start_isolated_daemon(monkeypatch)
    source = _source(root)
    manifest_path = root / "migration-manifest.json"
    expires_at_ms = time.time_ns() // 1_000_000 + 60_000
    manifest_path.write_text(
        json.dumps(_manifest(source, expires_at_ms=expires_at_ms)),
        encoding="utf-8",
    )
    foreign = "agent:foreign@example.test:other-node:fixture-agent"
    target = (
        Path(
            application._agent_registry.home_receipt(  # noqa: SLF001
                "fixture-agent"
            ).path
        )
        / "config"
    )

    try:
        arguments = ["agent", "home", "migrate", operation, foreign]
        if operation == "preflight":
            arguments.extend(["--manifest", str(manifest_path)])
        else:
            migration_id = _preflight(runner, "fixture-agent", manifest_path)[
                "migrationId"
            ]
            if operation == "rollback":
                _payload(
                    runner.invoke(
                        _cli_root_app,
                        [
                            "agent",
                            "home",
                            "migrate",
                            "execute",
                            "fixture-agent",
                            "--migration-id",
                            migration_id,
                            "--json",
                        ],
                    )
                )
            arguments.extend(["--migration-id", migration_id])
        if operation == "rollback":
            arguments.extend(
                [
                    "--window-id",
                    "isolated-foreign-rollback-test",
                    "--responsible-owner",
                    "operator@example.test",
                    "--expires-at-ms",
                    str(expires_at_ms),
                ]
            )
        arguments.append("--json")

        agent_before = application.agents.require("fixture-agent")
        plans_root = state / "agent-migration-plans"
        plans_before = (
            {
                path.relative_to(plans_root): path.read_bytes()
                for path in plans_root.rglob("*.json")
            }
            if plans_root.exists()
            else {}
        )
        source_existed = source.exists()
        target_existed = target.exists()

        result = runner.invoke(_cli_root_app, arguments)

        assert result.exit_code == 1, result.output
        failure = json.loads(result.stdout)
        assert failure == {
            "ok": False,
            "code": "AGENT_NOT_FOUND",
            "error": f"no agent named {foreign!r} on this machine",
        }
        assert application.agents.require("fixture-agent") == agent_before
        assert source.exists() is source_existed
        assert target.exists() is target_existed
        plans_after = (
            {
                path.relative_to(plans_root): path.read_bytes()
                for path in plans_root.rglob("*.json")
            }
            if plans_root.exists()
            else {}
        )
        assert plans_after == plans_before
    finally:
        _stop_isolated_daemon(root, application, dispatcher)


def test_preflight_accepts_resident_scope(
    tmp_path: Path,
) -> None:
    source = _source(tmp_path)
    manifest = _manifest(source, expires_at_ms=1)
    manifest["subjectKind"] = "resident"
    from hyprial.identity.impl.agents.migration.entry import MigrationPreflightManifest

    parsed = MigrationPreflightManifest.from_json(manifest)
    assert parsed.subject_kind == "resident"


def test_preflight_rejects_unknown_subject_kind_with_allowed_values(
    tmp_path: Path,
) -> None:
    source = _source(tmp_path)
    manifest = _manifest(source, expires_at_ms=1)
    manifest["subjectKind"] = "daemon"
    from hyprial.identity.impl.agents.migration.entry import MigrationPreflightManifest

    with pytest.raises(ValueError, match="'non-resident'.*'resident'"):
        MigrationPreflightManifest.from_json(manifest)


def _review_snapshot(name: str) -> SupportMatrix:
    path = (
        Path(__file__).resolve().parents[4]
        / 'docs/notes/evidence'
        / name
        / 'support-matrix.json'
    )
    return SupportMatrix.from_json(json.loads(path.read_text(encoding="utf-8")))


def test_packaged_support_matrix_matches_reviewed_evidence() -> None:
    """The packaged table is exactly what the latest review signed off.

    A reviewed row is never edited in place: a promotion is recorded by a
    *later* snapshot that re-states the row, and the packaged table then has to
    equal that snapshot.  The previous review keeps its own veto -- every key it
    signed off must still exist -- so a row cannot disappear quietly either.
    """

    previous = _review_snapshot('agent-home-p2-c3-codex-20261015')
    latest = _review_snapshot('agent-home-p2-c3-pi130-20261018')
    packaged = load_packaged_support_matrix()

    assert packaged.rows == latest.rows
    packaged_keys = {row.key for row in packaged.rows}
    assert {row.key for row in previous.rows} <= packaged_keys
