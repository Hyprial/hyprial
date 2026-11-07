"""login_orchestrate_daemon: recovery scenarios."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hyprial.daemon.impl.configuration.identity import (
    write_settings_identity,
    write_settings_owner,
)
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
)
from hyprial.shell.impl.cli.commands.common.services import get_services as _cli_get_services
import hyprial.shell.impl.cli.runtime.entry  # noqa: F401  (installs the explicit CLI composition)

# -- S3 review e2: stopped replay must not retry a moving source (LOW) -----------


def test_stopped_replay_source_change_refuses_instead_of_retrying(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After the stop is proven, a changing source means another writer.

    Retrying the plan-stopped classification would retry the evidence away:
    the old generation is proven gone, so nothing legitimate can still be
    changing the source.  The refusal must carry plan-stopped, leave the
    owner untouched, and attempt the preview exactly once (no retry loop).
    """

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
    stops: list[dict[str, object]] = []
    monkeypatch.setattr(
        _cli_get_services(),
        "_stop_daemon_for_identity_switch",
        lambda: stops.append({"strict": True}) or {"ok": True, "pid": 111},
    )
    import hyprial.shell.impl.login.preview as preview_module

    original_preview = preview_module.preview_owner_migration
    attempts: list[int] = []

    def source_moves_only_after_stop(**kwargs):
        attempts.append(len(attempts) + 1)
        if stops:
            raise preview_module.LoginPreviewError(
                "PREVIEW_SOURCE_CHANGED",
                "source changed after the proven stop",
                {"sourceInventoryChanged": True},
            )
        return original_preview(**kwargs)

    monkeypatch.setattr(preview_module, "preview_owner_migration", source_moves_only_after_stop)
    with fake_oidc() as server:
        result = _invoke_login(
            server,
            monkeypatch,
            "--switch-account",
            launch=lambda **_kwargs: pytest.fail("moving source started daemon"),
        )

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["code"] == "PREVIEW_SOURCE_CHANGED"
    assert payload["data"]["failedPhase"] == "plan-stopped"
    assert payload["data"]["identityCommitted"] is False
    assert attempts == [1, 2]
    assert (home / "settings.json").read_bytes() == before
    assert not (home / "secrets" / "login.json").exists()


# -- S3 review e2: failure reports (LOW) -----------------------------------------


def test_owner_write_failed_next_step_states_credential_and_stop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """OWNER_WRITE_FAILED must not claim a clean retry-from-zero.

    The credential is already durable and the old generation is already
    stopped; nextStep has to say both, and identityCommitted stays false
    because the owner write is what failed.
    """

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
    monkeypatch.setattr(
        _cli_get_services(),
        "_stop_daemon_for_identity_switch",
        lambda: {"ok": True, "pid": 111},
    )
    import hyprial.shell.impl.login.flow as login_module

    def failing_write(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(login_module, "write_settings_identity", failing_write)
    with fake_oidc() as server:
        result = _invoke_login(
            server,
            monkeypatch,
            "--switch-account",
            launch=lambda **_kwargs: pytest.fail("failed write started daemon"),
        )

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["code"] == "OWNER_WRITE_FAILED"
    data = payload["data"]
    assert data["failedPhase"] == "commit"
    assert data["identityCommitted"] is False
    assert "already written" in data["nextStep"]
    assert "previous daemon generation is stopped" in data["nextStep"]
    # One-way commit: the credential stays durable for the re-run.
    assert (home / "secrets" / "login.json").exists()
    assert json.loads((home / "settings.json").read_text())["owner"] == OLD


@pytest.mark.parametrize("interrupt_phase", ["device", "start"])
def test_post_commit_interrupt_leaves_a_failure_report(
    interrupt_phase: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A KeyboardInterrupt after the commit must not vanish into an empty code.

    The operator still needs the phase that was interrupted and the fact
    that the identity stays committed — flattening to a bare INTERRUPTED
    via the finally boundary hid both.
    """

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

    def launch_interrupting(**_kwargs):
        raise KeyboardInterrupt()

    with fake_oidc() as server:
        profile = _profile(server)
        launch = (
            launch_interrupting
            if interrupt_phase == "start"
            else (lambda **_kwargs: _launch_result(profile))
        )
        result = _invoke_login(
            server,
            monkeypatch,
            "--switch-account",
            device_error=(
                KeyboardInterrupt() if interrupt_phase == "device" else None
            ),
            launch=launch,
        )

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["code"] == "INTERRUPTED"
    data = payload["data"]
    assert data["failedPhase"] == interrupt_phase
    assert data["identityCommitted"] is True
    assert "stays committed" in data["nextStep"]
    assert data["migration"]["status"] == "unknown"
    assert json.loads((home / "settings.json").read_text())["owner"] == NEW
