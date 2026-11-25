"""login_orchestrate_daemon: custody scenarios."""

from __future__ import annotations
import json
import os
import subprocess
import sys
from pathlib import Path
import pytest
from typer.testing import CliRunner
from hyprial.kernel import DaemonLaunchResult
from hyprial.daemon.impl.configuration.identity import write_settings_owner
from hyprial.identity.impl.identity_transaction import (
    EXPECTED_IDENTITY_GENERATION_ENV,
    IdentityTransactionBusy,
    IdentityTransactionLock,
)
from hyprial.shell.impl.login.generation import IDENTITY_GENERATION_STALE_EXIT_CODE

from .support import (
    NEW,
    _environment,
)
from hyprial.shell.impl.cli.commands.common.root import app as _cli_root_app
from hyprial.shell.impl.cli.commands.common.services import get_services as _cli_get_services
import hyprial.shell.impl.cli.runtime.entry  # noqa: F401  (installs the explicit CLI composition)


def test_identity_transaction_is_one_os_lock_and_never_handed_over(
    tmp_path: Path,
) -> None:
    """One OS lock per home; another process can only wait for the release."""

    home = tmp_path / "home"
    home.mkdir()
    child = (
        "from pathlib import Path; import os, sys; "
        "from hyprial.identity.impl.identity_transaction import "
        "IdentityTransactionBusy, IdentityTransactionLock\n"
        "try:\n"
        "    IdentityTransactionLock.acquire(Path(os.environ['HYPRIAL_HOME']), timeout=0).close()\n"
        "except IdentityTransactionBusy:\n"
        "    print('busy', flush=True)\n"
        "else:\n"
        "    print('acquired', flush=True)\n"
    )
    environment = {**os.environ, "HYPRIAL_HOME": str(home)}

    def other_process() -> str:
        probe = subprocess.run(
            [sys.executable, "-c", child],
            env=environment,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert probe.returncode == 0, probe.stderr
        return probe.stdout.strip()

    with IdentityTransactionLock.acquire(home):
        with pytest.raises(IdentityTransactionBusy):
            IdentityTransactionLock.acquire(home, timeout=0)
        assert other_process() == "busy"
    assert other_process() == "acquired"
    assert not hasattr(IdentityTransactionLock, "acquire_or_adopt")


def test_launcher_holds_no_identity_lock_across_spawn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The daemon acquires the lock itself; the launcher only passes a check."""

    home, _state = _environment(tmp_path, monkeypatch)
    observed: list[object] = []

    def locked(**kwargs):
        assert "identity_transaction" not in kwargs
        IdentityTransactionLock.acquire(home, timeout=0).close()
        observed.append(kwargs.get("expected_identity_generation"))
        return DaemonLaunchResult.existing({"running": True, "pid": 1})

    monkeypatch.setattr(_cli_get_services(), "_launch_daemon_process_locked", locked)
    _cli_get_services()._launch_daemon_process(
        ready_timeout=0.1, expected_identity_generation=7
    )
    assert observed == [7]


def test_direct_daemon_run_holds_identity_lock_until_runtime_ready(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home, state = _environment(tmp_path, monkeypatch)
    write_settings_owner(NEW, environ={"HYPRIAL_HOME": str(home)}, hyprial_home=home)
    observations: list[str] = []

    class Runtime:
        # daemon run stamps stdio with the runtime's epoch (#2187).
        epoch = "test-epoch"

        stop_reason = "shutdown"

        def exit_unless_logout(self) -> None:
            observations.append("owns-exit")

        def run(self, *, ownership_stream, identity_transaction_stream, launch_id=None) -> None:
            with pytest.raises(IdentityTransactionBusy):
                IdentityTransactionLock.acquire(home, timeout=0)
            observations.append("locked-through-runtime")
            identity_transaction_stream.close()
            ownership_stream.close()

    from hyprial.daemon.impl.application import DaemonApplication

    monkeypatch.setattr(
        DaemonApplication,
        "from_environment",
        classmethod(lambda _cls, **_kwargs: Runtime()),
    )
    result = CliRunner().invoke(
        _cli_root_app,
        ["daemon", "run", "--json"],
        env={
            "HYPRIAL_HOME": str(home),
            "HARNESS_STATE_DIR": str(state),
            "HYPRIAL_PEER_DISCOVERY": "0",
            "HYPRIAL_OWNER": "",
        },
    )
    assert result.exit_code == 0, result.output
    assert observations == ["owns-exit", "locked-through-runtime"]
    with IdentityTransactionLock.acquire(home, timeout=0):
        pass


# -- S3 review e2: inherited-fd adoption (MEDIUM) --------------------------------


def _daemon_run_with_generation(
    home: Path,
    state: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    on_disk: int,
    expected: str,
    extra_env: dict[str, str] | None = None,
):
    """``daemon run`` launched with an expectation against ``on_disk``."""

    write_settings_owner(NEW, environ={"HYPRIAL_HOME": str(home)}, hyprial_home=home)
    secrets = home / "secrets"
    secrets.mkdir(exist_ok=True)
    (secrets / "login.json").write_text(
        json.dumps(
            {
                "version": 1,
                "issuer": "https://issuer.example",
                "refreshToken": "r",
                "obtainedAt": "2026-11-06T00:00:00+00:00",
                "identityGeneration": on_disk,
            }
        ),
        encoding="utf-8",
    )
    observations: list[object] = []

    class Runtime:
        epoch = "test-epoch"

        stop_reason = "shutdown"

        def exit_unless_logout(self) -> None:
            pass

        def run(self, *, ownership_stream, identity_transaction_stream, launch_id=None) -> None:
            observations.append(os.environ.get(EXPECTED_IDENTITY_GENERATION_ENV))
            identity_transaction_stream.close()
            ownership_stream.close()

    from hyprial.daemon.impl.application import DaemonApplication

    def construct(_cls, **_kwargs):
        observations.append("constructed")
        return Runtime()

    monkeypatch.setattr(DaemonApplication, "from_environment", classmethod(construct))
    result = CliRunner().invoke(
        _cli_root_app,
        ["daemon", "run", "--json"],
        env={
            "HYPRIAL_HOME": str(home),
            "HARNESS_STATE_DIR": str(state),
            "HYPRIAL_PEER_DISCOVERY": "0",
            "HYPRIAL_OWNER": "",
            EXPECTED_IDENTITY_GENERATION_ENV: expected,
            **(extra_env or {}),
        },
    )
    return result, observations


def test_daemon_run_with_a_superseded_generation_exits_stale_before_reading_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home, state = _environment(tmp_path, monkeypatch)
    result, observations = _daemon_run_with_generation(
        home, state, monkeypatch, on_disk=2, expected="1"
    )
    assert result.exit_code == IDENTITY_GENERATION_STALE_EXIT_CODE, result.output
    assert result.exit_code not in {0, 1}
    payload = json.loads(result.stdout)
    assert payload["code"] == "IDENTITY_TRANSACTION_STALE"
    assert payload["data"] == {"expectedIdentityGeneration": 1, "identityGeneration": 2}
    assert observations == [], "a stale daemon must not construct the runtime"
    # The stale exit released the lock: the winner's daemon can take it.
    IdentityTransactionLock.acquire(home, timeout=0).close()


def test_supervised_daemon_ignores_a_leaked_expectation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """KeepAlive/Restart=always must never see the stale exit (no restart storm)."""

    home, state = _environment(tmp_path, monkeypatch)
    result, observations = _daemon_run_with_generation(
        home,
        state,
        monkeypatch,
        on_disk=2,
        expected="1",
        extra_env={"HYPRIAL_SERVICE_MANAGED": "1"},
    )
    assert result.exit_code == 0, result.output
    assert observations == ["constructed", None]


def test_daemon_run_pops_the_expectation_before_any_descendant_can_inherit_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home, state = _environment(tmp_path, monkeypatch)
    result, observations = _daemon_run_with_generation(
        home, state, monkeypatch, on_disk=3, expected="3"
    )
    assert result.exit_code == 0, result.output
    assert observations == ["constructed", None]
