"""login_orchestrate_daemon: identity switch scenarios."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from hyprial.daemon.impl.configuration.identity import (
    write_settings_identity,
    write_settings_owner,
)
from hyprial.daemon.impl.network import tailcat as tailcat_module
from hyprial.kernel import ipc_errors
from tests.fixtures.network.fake_oidc import fake_oidc  # noqa: E402

from .support import (
    NEW,
    OLD,
    _environment,
    _invoke_login,
    _launch_result,
    _profile,
    _seed_agents,
    _seed_wal_state_after_clean_shutdown,
)
from hyprial.shell.impl.cli.commands.common import support as _cli_support
from hyprial.shell.impl.cli.commands.common.services import get_services as _cli_get_services
import hyprial.shell.impl.cli.runtime.entry  # noqa: F401  (installs the explicit CLI composition)


def test_first_login_commits_tuple_and_automatically_starts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home, _state = _environment(tmp_path, monkeypatch)
    monkeypatch.setattr(
        _cli_get_services(),
        "_daemon_probe",
        lambda **_kwargs: (_ for _ in ()).throw(
            ipc_errors.DaemonUnavailableError("stopped")
        ),
    )
    launches: list[object] = []

    with fake_oidc() as server:
        profile = _profile(server)

        def launch(**kwargs):
            launches.append(kwargs["expected_identity_generation"])
            return _launch_result(profile)

        result = _invoke_login(server, monkeypatch, launch=launch)

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["identity"] == {
        "status": "authenticated",
        "owner": NEW,
        "mode": "casdoor",
        "issuer": profile.issuer,
        "source": "test-profile",
        "committed": True,
    }
    assert payload["daemon"]["epoch"] == "new-epoch"
    assert payload["migration"]["status"] == "applied"
    assert launches == [1]
    settings = json.loads((home / "settings.json").read_text())
    assert settings["owner"] == NEW
    assert settings["identityMode"] == "casdoor"
    assert settings["identityIssuer"] == profile.issuer


def test_same_tuple_live_daemon_updates_credential_without_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home, _state = _environment(tmp_path, monkeypatch)
    with fake_oidc() as server:
        profile = _profile(server)
        write_settings_identity(
            NEW,
            mode="casdoor",
            issuer=profile.issuer,
            environ={"HYPRIAL_HOME": str(home)},
            hyprial_home=home,
        )
        monkeypatch.setattr(
            _cli_get_services(),
            "_daemon_probe",
            lambda **_kwargs: {
                "running": True,
                "pid": 123,
                "epoch": "same-epoch",
                "owner": NEW,
                "identityMode": "casdoor",
                "identityIssuer": profile.issuer,
            },
        )
        result = _invoke_login(
            server,
            monkeypatch,
            launch=lambda **_kwargs: pytest.fail("same identity restarted daemon"),
        )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["daemon"]["epoch"] == "same-epoch"
    assert payload["daemon"]["state"] == "running"


def test_switch_stops_strictly_replans_and_verifies_new_epoch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home, state = _environment(tmp_path, monkeypatch)
    _seed_agents(state)
    write_settings_identity(
        OLD,
        mode="casdoor",
        issuer="https://old.example",
        environ={"HYPRIAL_HOME": str(home)},
        hyprial_home=home,
    )
    monkeypatch.setattr(
        _cli_get_services(),
        "_daemon_probe",
        lambda **_kwargs: {
            "running": True,
            "pid": 111,
            "epoch": "old-epoch",
            "owner": OLD,
            "identityMode": "casdoor",
            "identityIssuer": "https://old.example",
        },
    )
    stops: list[dict[str, object]] = []
    monkeypatch.setattr(
        _cli_get_services(),
        "_stop_daemon_for_identity_switch",
        lambda: stops.append({"strict": True}) or {"ok": True, "pid": 111},
    )
    import hyprial.shell.impl.login.preview as preview_module

    original_preview = preview_module.preview_owner_migration
    preview_attempts: list[int] = []

    def preview_with_live_wal_retry(**kwargs):
        # Only the ONLINE preview may retry a changed source; the stopped
        # replay is covered by its own no-retry test below.
        preview_attempts.append(len(preview_attempts) + 1)
        if len(preview_attempts) == 1:
            raise preview_module.LoginPreviewError(
                "PREVIEW_SOURCE_CHANGED",
                "live WAL changed during snapshot",
                {"sourceInventoryChanged": True},
            )
        return original_preview(**kwargs)

    monkeypatch.setattr(
        preview_module, "preview_owner_migration", preview_with_live_wal_retry
    )
    with fake_oidc() as server:
        profile = _profile(server)
        result = _invoke_login(
            server,
            monkeypatch,
            "--switch-account",
            launch=lambda **_kwargs: _launch_result(profile),
        )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert stops == [{"strict": True}]
    assert preview_attempts == [1, 2, 3]
    assert payload["identity"]["previousOwner"] == OLD
    assert payload["daemon"]["epoch"] == "new-epoch"
    assert payload["migration"]["status"] == "applied"


def test_clean_shutdown_without_wal_still_replans_and_completes_switch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Post-stop replay preview must tolerate WAL databases with no -wal.

    This is the review-reproduced production shape: after a normal daemon
    shutdown the migration databases remain in WAL mode but carry no
    ``-wal``/``-shm`` files.  SQLite's read-only first reader materializes a
    bounded artifact pair (empty ``-wal``, 32 KiB-multiple ``-shm``); the S2
    guard recognizes exactly that shape and reports it as
    ``sqliteReadArtifacts`` instead of failing.  The stopped replan and the
    switch must complete, and anything left in the source directory must be
    exactly that bounded shape — never a non-empty journal.
    """

    home, state = _environment(tmp_path, monkeypatch)
    _seed_wal_state_after_clean_shutdown(state)
    write_settings_identity(
        OLD,
        mode="casdoor",
        issuer="https://old.example",
        environ={"HYPRIAL_HOME": str(home)},
        hyprial_home=home,
    )
    monkeypatch.setattr(
        _cli_get_services(),
        "_daemon_probe",
        lambda **_kwargs: {
            "running": True,
            "pid": 111,
            "epoch": "old-epoch",
            "owner": OLD,
            "identityMode": "casdoor",
            "identityIssuer": "https://old.example",
        },
    )
    stops: list[dict[str, object]] = []
    monkeypatch.setattr(
        _cli_get_services(),
        "_stop_daemon_for_identity_switch",
        lambda: stops.append({"strict": True}) or {"ok": True, "pid": 111},
    )
    import hyprial.shell.impl.login.preview as preview_module

    original_preview = preview_module.preview_owner_migration
    previews: list[object] = []

    def capture_preview(**kwargs):
        preview = original_preview(**kwargs)
        previews.append(preview)
        return preview

    monkeypatch.setattr(preview_module, "preview_owner_migration", capture_preview)
    with fake_oidc() as server:
        profile = _profile(server)
        result = _invoke_login(
            server,
            monkeypatch,
            "--switch-account",
            launch=lambda **_kwargs: _launch_result(profile),
        )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert stops == [{"strict": True}]
    assert payload["identity"]["previousOwner"] == OLD
    assert payload["identity"]["owner"] == NEW
    assert payload["daemon"]["epoch"] == "new-epoch"
    assert payload["migration"]["status"] == "applied"
    # The no-wal first-reader path must actually have been exercised: at
    # least one preview reported SQLite's bounded read artifacts.
    artifact_reports = [
        artifact
        for preview in previews
        for artifact in preview.sqlite_read_artifacts
    ]
    assert artifact_reports, "no preview observed the bounded read artifacts"
    journal_files = [
        path for path in state.iterdir() if path.name.endswith(("-wal", "-shm"))
    ]
    for path in journal_files:
        size = path.stat().st_size
        if path.name.endswith("-wal"):
            assert size == 0, f"non-empty journal left in source: {path.name}"
        else:
            assert size >= 32768 and size % 32768 == 0, (
                f"unbounded shm left in source: {path.name} ({size} bytes)"
            )


def test_stopped_replan_blocks_late_unclassified_before_identity_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home, state = _environment(tmp_path, monkeypatch)
    _seed_agents(state)
    write_settings_identity(
        OLD,
        mode="casdoor",
        issuer="https://old.example",
        environ={"HYPRIAL_HOME": str(home)},
        hyprial_home=home,
    )
    before = (home / "settings.json").read_bytes()
    monkeypatch.setattr(
        _cli_get_services(),
        "_daemon_probe",
        lambda **_kwargs: {
            "running": True,
            "pid": 111,
            "epoch": "old-epoch",
            "owner": OLD,
            "identityMode": "casdoor",
            "identityIssuer": "https://old.example",
        },
    )

    def stop_and_insert(**_kwargs):
        database = sqlite3.connect(state / "workflows.sqlite3")
        with database:
            database.execute("CREATE TABLE live (value TEXT)")
            database.execute("INSERT INTO live VALUES (?)", (f"legacy={OLD}",))
        database.close()
        return {"ok": True, "pid": 111}

    monkeypatch.setattr(_cli_get_services(), "_stop_daemon_for_identity_switch", stop_and_insert)
    with fake_oidc() as server:
        result = _invoke_login(
            server,
            monkeypatch,
            "--switch-account",
            launch=lambda **_kwargs: pytest.fail("blocked replan started daemon"),
        )

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["data"]["failedPhase"] == "plan-stopped"
    assert payload["data"]["identityCommitted"] is False
    assert payload["data"]["migration"]["status"] == "blocked"
    assert (home / "settings.json").read_bytes() == before
    assert not (home / "secrets" / "login.json").exists()


def test_unknown_old_pid_never_passes_strict_stop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        _cli_support,
        "_daemon_request",
        lambda *_args, **_kwargs: {"ok": True, "teardownCompleted": True},
    )
    with pytest.raises(_cli_get_services().CliError) as caught:
        _cli_get_services()._stop_daemon_for_identity_switch()
    assert caught.value.code == "DAEMON_STOP_IDENTITY_UNKNOWN"


@pytest.mark.parametrize("device_failure_kind", ["typed", "unexpected"])
def test_device_failure_still_starts_new_daemon_and_is_machine_visible(
    device_failure_kind: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """D12: the device stage replaces join — its failure never fails the
    login, never rolls the identity back, and never skips the daemon start."""
    home, state = _environment(tmp_path, monkeypatch)
    _seed_agents(state)
    write_settings_owner(OLD, environ={"HYPRIAL_HOME": str(home)}, hyprial_home=home)
    monkeypatch.setattr(
        _cli_get_services(),
        "_daemon_probe",
        lambda **_kwargs: (_ for _ in ()).throw(
            ipc_errors.DaemonUnavailableError("stopped")
        ),
    )
    launched: list[bool] = []
    with fake_oidc() as server:
        profile = _profile(server)

        def launch(**_kwargs):
            launched.append(True)
            return _launch_result(profile)

        device_error: BaseException = {
            "typed": tailcat_module.TailcatSidecarError(
                "KEYGEN_FAILED", "genkey exited with status 1"
            ),
            "unexpected": RuntimeError("device provisioning crashed"),
        }[device_failure_kind]
        result = _invoke_login(
            server,
            monkeypatch,
            "--switch-account",
            device_error=device_error,
            launch=launch,
        )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert launched == [True]
    assert payload["ok"] is True
    assert payload["identity"]["committed"] is True
    device = payload["device"]
    assert device["ready"] is False
    assert device["error"]["code"] == (
        "KEYGEN_FAILED" if device_failure_kind == "typed" else "DEVICE_KEY_FAILED"
    )
    assert "hyprial-tailcat" in device["nextStep"]
    assert payload["daemon"]["state"] == "running"


def test_no_daemon_keeps_live_switch_refusal_and_never_stops_or_starts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home, state = _environment(tmp_path, monkeypatch)
    _seed_agents(state)
    write_settings_owner(OLD, environ={"HYPRIAL_HOME": str(home)}, hyprial_home=home)
    import hyprial.daemon.impl.configuration.home_guard as guard_module

    monkeypatch.setattr(guard_module, "live_daemon_pid", lambda _home: 991)
    monkeypatch.setattr(
        _cli_get_services(),
        "_stop_daemon_for_identity_switch",
        lambda: pytest.fail("--no-daemon stopped a daemon"),
    )
    with fake_oidc() as server:
        result = _invoke_login(
            server,
            monkeypatch,
            "--switch-account",
            "--no-daemon",
            launch=lambda **_kwargs: pytest.fail("--no-daemon started a daemon"),
        )
    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["code"] == "DAEMON_RUNNING"
    assert payload["data"]["identityCommitted"] is False
    assert json.loads((home / "settings.json").read_text())["owner"] == OLD


def test_a_failed_switch_after_a_supervisor_stop_asks_the_supervisor_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The stop went through launchd/systemd, so KeepAlive no longer brings the
    old generation back; an uncommitted failure must ask for it explicitly and
    name ``hyprial daemon start`` in nextStep."""

    from hyprial.shell.impl.cli.commands.daemon import service as service_module

    home, state = _environment(tmp_path, monkeypatch)
    _seed_agents(state)
    write_settings_identity(
        OLD,
        mode="casdoor",
        issuer="https://old.example",
        environ={"HYPRIAL_HOME": str(home)},
        hyprial_home=home,
    )
    monkeypatch.setattr(
        _cli_get_services(),
        "_daemon_probe",
        lambda **_kwargs: {
            "running": True,
            "pid": 111,
            "epoch": "old-epoch",
            "owner": OLD,
            "identityMode": "casdoor",
            "identityIssuer": "https://old.example",
        },
    )

    def stop_and_insert(**_kwargs):
        database = sqlite3.connect(state / "workflows.sqlite3")
        with database:
            database.execute("CREATE TABLE live (value TEXT)")
            database.execute("INSERT INTO live VALUES (?)", (f"legacy={OLD}",))
        database.close()
        return {"ok": True, "pid": 111, "supervisor": {"platform": "launchd"}}

    requested: list[object] = []
    supervisor = (object(), object())
    monkeypatch.setattr(_cli_get_services(), "_stop_daemon_for_identity_switch", stop_and_insert)
    monkeypatch.setattr(service_module, "installed_supervisor", lambda: supervisor)
    monkeypatch.setattr(service_module, "request_supervised_start", requested.append)
    with fake_oidc() as server:
        result = _invoke_login(
            server,
            monkeypatch,
            "--switch-account",
            launch=lambda **_kwargs: pytest.fail("blocked replan started daemon"),
        )

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["data"]["failedPhase"] == "plan-stopped"
    assert payload["data"]["identityCommitted"] is False
    assert requested == [supervisor]
    assert payload["data"]["daemon"]["stoppedVia"] == "supervisor"
    assert payload["data"]["daemon"]["restart"] == {"status": "requested"}
    assert "hyprial daemon start" in payload["data"]["nextStep"]
