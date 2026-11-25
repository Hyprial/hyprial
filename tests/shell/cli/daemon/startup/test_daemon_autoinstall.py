"""Z-8c launch-primitive auto-install: opt-outs, gates, non-fatal fallbacks.

Decision (2026-11-15, workflow brief + audit must-fix L4): every manager
interaction below goes through a monkeypatched fake on
``daemon.service._service_manager`` — no test here reaches launchctl,
systemctl, or any real unit directory, and none relies on the backend's
HOME guard as its only protection. The suite pins both opt-out variables to
"0" (conftest + isolated_environment), so the primitive's auto-install path
is opt-in per test; the primitive additionally refuses fake HOMEs and
AppTranslocation paths on its own. ``DAEMON_AUTOSTART_DISABLED`` is reached
only by implicit callers; an explicit start is never a silent no-op — with
the opt-out it bypasses the manager and keeps today's detached spawn.
"""

from __future__ import annotations

import ast
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import hyprial
from hyprial.daemon import ServiceOwnershipError
from hyprial.daemon.impl.operations.service import (
    ServiceCommandError,
    ServiceStatus,
)
from hyprial.kernel import DaemonLaunchResult, ipc_errors
from hyprial.shell.impl.cli.commands.common import support as _cli_support
from hyprial.shell.impl.cli.commands.common.pending_restart import restart_daemon
from hyprial.shell.impl.cli.commands.common.services import get_services
from hyprial.shell.impl.cli.commands.daemon import service as service_module
import hyprial.shell.impl.cli.runtime.entry  # noqa: F401  (installs the explicit CLI composition)
from tests.support.runtime.environment import isolated_environment
from tests.support.runtime.environment import real_account_home

SERVICE_OPT_OUTS = ("HYPRIAL_SERVICE_AUTOINSTALL", "HYPRIAL_DAEMON_AUTOSTART")


class FakeRunner:
    def __init__(self) -> None:
        self.commands: list[tuple[str, ...]] = []

    def __call__(self, command: Any, *, timeout: float | None = None):  # type: ignore[no-untyped-def]
        command = tuple(command)
        self.commands.append(command)
        return subprocess.CompletedProcess(command, 0, "", "")


class FakeServiceManager:
    """The only manager this module's tests allow: records, never executes."""

    def __init__(
        self,
        root: Path,
        *,
        install_error: Exception | None = None,
        revert_error: Exception | None = None,
    ) -> None:
        self.platform = "launchd"
        self.uid = 501
        self.username = "unit-tester"
        self.runner = FakeRunner()
        self.install_error = install_error
        self.revert_error = revert_error
        self.install_calls = 0
        self.reverts: list[tuple[str | None, bool]] = []
        self.unit = "com.hyprial.hyprial.daemon"
        self.unit_path = (
            root / "Library" / "LaunchAgents" / "com.hyprial.hyprial.daemon.plist"
        )

    def _status(self, *, loaded: bool) -> ServiceStatus:
        installed = self.unit_path.is_file()
        return ServiceStatus(
            platform=self.platform,
            unit=self.unit,
            unit_path=self.unit_path,
            installed=installed,
            loaded=loaded,
            running=False,
            attributed=installed,
        )

    def install(self) -> ServiceStatus:
        self.install_calls += 1
        if self.install_error is not None:
            raise self.install_error
        self.unit_path.parent.mkdir(parents=True, exist_ok=True)
        self.unit_path.write_text("fake-unit-bytes", encoding="utf-8")
        return self._status(loaded=True)

    def capture_unit_state(self) -> tuple[str | None, bool]:
        installed = self.unit_path.is_file()
        return (self.unit_path.read_text(encoding="utf-8") if installed else None, False)

    def revert_install(self, state: tuple[str | None, bool]) -> str:
        self.reverts.append(state)
        if self.revert_error is not None:
            raise self.revert_error
        if state[0] is None:
            self.unit_path.unlink(missing_ok=True)
            return "removed"
        self.unit_path.write_text(state[0], encoding="utf-8")
        return "restored"

    def inspect(self) -> ServiceStatus:
        return self._status(loaded=False)


@pytest.fixture
def services(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    services = get_services()
    monkeypatch.setattr(services, "_hyprial_home", lambda: tmp_path / "home")
    monkeypatch.setattr(services, "_state_dir", lambda: tmp_path / "state")
    return services


def _patch_notices(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    from hyprial.shell.impl.cli.commands.common import daemon_start as daemon_start_module

    notices: list[str] = []
    monkeypatch.setattr(daemon_start_module, "notice", notices.append)
    return notices


def _patch_supervised_start(
    monkeypatch: pytest.MonkeyPatch, services: Any
) -> list[dict[str, Any]]:
    waited: list[dict[str, Any]] = []

    def wait(**kwargs: Any) -> dict[str, Any]:
        waited.append(kwargs)
        return {"running": True, "pid": 77, "epoch": "e-1"}

    monkeypatch.setattr(
        services,
        "_daemon_probe",
        lambda **_k: (_ for _ in ()).throw(ipc_errors.DaemonUnavailableError("down")),
    )
    monkeypatch.setattr(services, "_wait_for_daemon", wait)
    return waited


def _patch_detached_spawn(
    monkeypatch: pytest.MonkeyPatch, services: Any
) -> list[dict[str, Any]]:
    spawned: list[dict[str, Any]] = []

    def spawn(**kwargs: Any) -> DaemonLaunchResult:
        spawned.append(kwargs)
        return DaemonLaunchResult.launched({"running": True, "pid": 5, "epoch": "cli"})

    monkeypatch.setattr(services, "_launch_daemon_process_locked", spawn)
    return spawned


def _forbid_detached_spawn(
    monkeypatch: pytest.MonkeyPatch, services: Any
) -> None:
    def forbidden(**_kwargs: Any) -> DaemonLaunchResult:
        raise AssertionError("the primitive must not fall back to a detached spawn")

    monkeypatch.setattr(services, "_launch_daemon_process_locked", forbidden)


def _opt_in(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HYPRIAL_SERVICE_AUTOINSTALL", "1")
    monkeypatch.setenv("HYPRIAL_DAEMON_AUTOSTART", "1")
    monkeypatch.setenv("HOME", str(real_account_home()))
    monkeypatch.setenv("PATH", "")


def test_in_process_fixture_pins_both_service_opt_outs() -> None:
    assert os.environ["HYPRIAL_SERVICE_AUTOINSTALL"] == "0"
    assert os.environ["HYPRIAL_DAEMON_AUTOSTART"] == "0"


def test_isolated_environment_pins_both_opt_outs_for_children(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HYPRIAL_SERVICE_AUTOINSTALL", "1")
    monkeypatch.setenv("HYPRIAL_DAEMON_AUTOSTART", "1")
    env = isolated_environment(tmp_path)
    assert env["HYPRIAL_SERVICE_AUTOINSTALL"] == "0"
    assert env["HYPRIAL_DAEMON_AUTOSTART"] == "0"
    probe = subprocess.run(
        [
            sys.executable,
            "-c",
            "import os; print(os.environ['HYPRIAL_SERVICE_AUTOINSTALL'], "
            "os.environ['HYPRIAL_DAEMON_AUTOSTART'])",
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    assert probe.stdout.split() == ["0", "0"]


def _env_write_offenders(path: Path, node: ast.AST) -> list[str]:
    offenders: list[str] = []
    targets: list[ast.expr] = []
    if isinstance(node, ast.Assign):
        targets = node.targets
    elif isinstance(node, (ast.AugAssign, ast.AnnAssign)):
        targets = [node.target]
    for target in targets:
        if isinstance(target, ast.Subscript) and isinstance(target.slice, ast.Constant):
            if target.slice.value in SERVICE_OPT_OUTS:
                offenders.append(f"{path}: assignment to environment key")
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
        if node.func.attr in {"putenv", "setenv"} and node.args:
            first = node.args[0]
            if isinstance(first, ast.Constant) and first.value in SERVICE_OPT_OUTS:
                offenders.append(f"{path}: {node.func.attr} call")
    if isinstance(node, ast.Dict):
        for key in node.keys:
            if isinstance(key, ast.Constant) and key.value in SERVICE_OPT_OUTS:
                offenders.append(f"{path}: dict literal environment key")
    return offenders


def test_product_code_never_sets_the_service_opt_outs() -> None:
    root = Path(hyprial.__file__).resolve().parent
    offenders: list[str] = []
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            offenders.extend(_env_write_offenders(path, node))
    assert offenders == []


def test_autoinstall_installs_once_and_starts_supervised(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, services  # type: ignore[no-untyped-def]
) -> None:
    manager = FakeServiceManager(tmp_path)
    monkeypatch.setattr(service_module, "_service_manager", lambda: manager)
    _opt_in(monkeypatch)
    _patch_supervised_start(monkeypatch, services)
    _forbid_detached_spawn(monkeypatch, services)
    notices = _patch_notices(monkeypatch)

    launched = services._launch_daemon_process(ready_timeout=3.0)

    assert manager.install_calls == 1
    assert manager.runner.commands == [
        ("launchctl", "bootstrap", "gui/501", str(manager.unit_path))
    ]
    assert launched.running and launched.pid == 77
    assert len(notices) == 1
    assert "uninstall" in notices[0]


def _failing_supervised_start(monkeypatch: pytest.MonkeyPatch, services: Any) -> None:
    def wait(**_kwargs: Any) -> dict[str, Any]:
        raise services.CliError(
            ipc_errors.DAEMON_START_TIMEOUT, "daemon did not become ready"
        )

    monkeypatch.setattr(
        services,
        "_daemon_probe",
        lambda **_k: (_ for _ in ()).throw(ipc_errors.DaemonUnavailableError("down")),
    )
    monkeypatch.setattr(services, "_wait_for_daemon", wait)


def test_autoinstall_that_never_starts_is_reverted_and_falls_back_to_detached(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, services  # type: ignore[no-untyped-def]
) -> None:
    manager = FakeServiceManager(tmp_path)
    monkeypatch.setattr(service_module, "_service_manager", lambda: manager)
    _opt_in(monkeypatch)
    _failing_supervised_start(monkeypatch, services)
    spawned = _patch_detached_spawn(monkeypatch, services)
    notices = _patch_notices(monkeypatch)

    launched = services._launch_daemon_process(ready_timeout=3.0)

    assert manager.install_calls == 1
    assert manager.reverts == [(None, False)]
    assert not manager.unit_path.exists()
    assert len(spawned) == 1 and launched.running and launched.pid == 5
    assert len(notices) == 2
    assert "did not start" in notices[1]
    assert "daemon did not become ready" in notices[1]
    assert "removed" in notices[1]
    assert "detached" in notices[1]


def test_autoinstall_that_cannot_be_reverted_fails_instead_of_racing_a_detached_spawn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, services  # type: ignore[no-untyped-def]
) -> None:
    manager = FakeServiceManager(tmp_path, revert_error=ServiceCommandError("bootout refused", reason="manager-command-failed"))
    monkeypatch.setattr(service_module, "_service_manager", lambda: manager)
    _opt_in(monkeypatch)
    _failing_supervised_start(monkeypatch, services)
    _forbid_detached_spawn(monkeypatch, services)
    _patch_notices(monkeypatch)

    with pytest.raises(services.CliError) as raised:
        services._launch_daemon_process(ready_timeout=3.0)

    assert raised.value.code == "SERVICE_SUPERVISION_LOST"
    assert "service uninstall" in str(raised.value)
    assert raised.value.data == {"install": {"supervision": "lost", "reverted": "failed"}}


def test_socket_path_over_the_af_unix_limit_is_named_before_any_install_or_spawn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, services  # type: ignore[no-untyped-def]
) -> None:
    manager = FakeServiceManager(tmp_path)
    monkeypatch.setattr(service_module, "_service_manager", lambda: manager)
    _opt_in(monkeypatch)
    _forbid_detached_spawn(monkeypatch, services)
    long_state = tmp_path / ("s" * 200)
    monkeypatch.setattr(services, "_state_dir", lambda: long_state)
    monkeypatch.setattr(services, "_socket_path", lambda: long_state / "daemon.sock")

    with pytest.raises(services.CliError) as raised:
        services._launch_daemon_process(ready_timeout=3.0)

    assert raised.value.code == ipc_errors.DAEMON_START_FAILED
    assert str(long_state / "daemon.sock") in str(raised.value)
    assert "AF_UNIX limit" in str(raised.value)
    data = raised.value.data
    assert data["socketPath"] == str(long_state / "daemon.sock")
    assert data["length"] == len(os.fsencode(str(long_state / "daemon.sock")))
    assert data["length"] > data["limit"] >= 100
    assert manager.install_calls == 0
    assert manager.runner.commands == []


def test_socket_path_exactly_at_the_limit_is_accepted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, services  # type: ignore[no-untyped-def]
) -> None:
    from hyprial.shell.impl.cli.commands.common import daemon_start as daemon_start_module

    limit = daemon_start_module._af_unix_path_limit()
    assert limit is not None
    name = "/" + "p" * (limit - 1)
    monkeypatch.setattr(services, "_socket_path", lambda: Path(name))
    daemon_start_module._require_socket_path_fits(services)

    monkeypatch.setattr(services, "_socket_path", lambda: Path(name + "p"))
    with pytest.raises(services.CliError) as raised:
        daemon_start_module._require_socket_path_fits(services)
    assert raised.value.data["limit"] == limit
    assert raised.value.data["length"] == limit + 1


def test_implicit_caller_also_autoinstalls_when_allowed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, services  # type: ignore[no-untyped-def]
) -> None:
    manager = FakeServiceManager(tmp_path)
    monkeypatch.setattr(service_module, "_service_manager", lambda: manager)
    _opt_in(monkeypatch)
    _patch_supervised_start(monkeypatch, services)
    _forbid_detached_spawn(monkeypatch, services)
    _patch_notices(monkeypatch)

    launched = services._launch_daemon_process(ready_timeout=3.0, implicit=True)

    assert manager.install_calls == 1
    assert launched.running


def test_installed_supervisor_starts_through_the_manager_without_installing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, services  # type: ignore[no-untyped-def]
) -> None:
    manager = FakeServiceManager(tmp_path)
    manager.unit_path.parent.mkdir(parents=True)
    manager.unit_path.write_text("fake-unit-bytes", encoding="utf-8")
    monkeypatch.setattr(service_module, "_service_manager", lambda: manager)
    _opt_in(monkeypatch)
    waited = _patch_supervised_start(monkeypatch, services)
    _forbid_detached_spawn(monkeypatch, services)
    notices = _patch_notices(monkeypatch)

    launched = services._launch_daemon_process(
        ready_timeout=3.0, expected_identity_generation=4
    )

    assert manager.install_calls == 0
    assert manager.runner.commands == [
        ("launchctl", "bootstrap", "gui/501", str(manager.unit_path))
    ]
    assert waited[0]["timeout"] == 3.0
    assert launched.running
    assert notices == []


def test_service_opt_outs_keep_the_detached_spawn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, services  # type: ignore[no-untyped-def]
) -> None:
    monkeypatch.setenv("HYPRIAL_SERVICE_AUTOINSTALL", "0")
    monkeypatch.setenv("HYPRIAL_DAEMON_AUTOSTART", "0")
    manager = FakeServiceManager(tmp_path)
    monkeypatch.setattr(service_module, "_service_manager", lambda: manager)
    spawned = _patch_detached_spawn(monkeypatch, services)
    notices = _patch_notices(monkeypatch)

    services._launch_daemon_process(ready_timeout=1.0, expected_identity_generation=2)

    assert manager.install_calls == 0
    assert manager.runner.commands == []
    assert spawned == [
        {
            "ready_timeout": 1.0,
            "listen": None,
            "connect": None,
            "expected_identity_generation": 2,
        }
    ]
    assert notices == []


def test_autostart_opt_out_bypasses_even_an_installed_supervisor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, services  # type: ignore[no-untyped-def]
) -> None:
    monkeypatch.setenv("HYPRIAL_DAEMON_AUTOSTART", "0")
    manager = FakeServiceManager(tmp_path)
    manager.unit_path.parent.mkdir(parents=True)
    manager.unit_path.write_text("fake-unit-bytes", encoding="utf-8")
    monkeypatch.setattr(service_module, "_service_manager", lambda: manager)
    spawned = _patch_detached_spawn(monkeypatch, services)
    notices = _patch_notices(monkeypatch)

    launched = services._launch_daemon_process(ready_timeout=1.0)

    assert manager.install_calls == 0
    assert manager.runner.commands == []
    assert spawned and launched.running
    assert notices == []


def test_autostart_opt_out_refuses_only_implicit_callers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, services  # type: ignore[no-untyped-def]
) -> None:
    monkeypatch.setenv("HYPRIAL_DAEMON_AUTOSTART", "0")
    manager = FakeServiceManager(tmp_path)
    monkeypatch.setattr(service_module, "_service_manager", lambda: manager)
    _forbid_detached_spawn(monkeypatch, services)
    _patch_notices(monkeypatch)

    with pytest.raises(services.CliError) as raised:
        services._launch_daemon_process(ready_timeout=1.0, implicit=True)

    assert raised.value.code == "DAEMON_AUTOSTART_DISABLED"
    assert manager.install_calls == 0
    assert manager.runner.commands == []


def test_disposable_home_never_installs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, services  # type: ignore[no-untyped-def]
) -> None:
    _opt_in(monkeypatch)
    monkeypatch.setenv("HOME", str(tmp_path / "operating-home"))
    manager = FakeServiceManager(tmp_path)
    monkeypatch.setattr(service_module, "_service_manager", lambda: manager)
    spawned = _patch_detached_spawn(monkeypatch, services)
    notices = _patch_notices(monkeypatch)

    launched = services._launch_daemon_process(ready_timeout=1.0)

    assert manager.install_calls == 0
    assert manager.runner.commands == []
    assert spawned and launched.running
    assert notices == []


def test_translocated_program_path_skips_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, services  # type: ignore[no-untyped-def]
) -> None:
    _opt_in(monkeypatch)
    translocated = tmp_path / "AppTranslocation" / "app" / "hyprial"
    monkeypatch.setattr(sys, "argv", [str(translocated)])
    manager = FakeServiceManager(tmp_path)
    monkeypatch.setattr(service_module, "_service_manager", lambda: manager)
    spawned = _patch_detached_spawn(monkeypatch, services)
    notices = _patch_notices(monkeypatch)

    launched = services._launch_daemon_process(ready_timeout=1.0)

    assert manager.install_calls == 0
    assert manager.runner.commands == []
    assert spawned and launched.running
    assert notices == []


@pytest.mark.parametrize(
    "install_error",
    [
        ServiceOwnershipError(
            "refusing to replace unsafe service file /x",
            reason="supervisor-file-foreign",
        ),
        ServiceOwnershipError(
            "user-service management requires HYPRIAL_HOME=$HOME/.hyprial",
            reason="service-home-nondefault",
        ),
        ServiceCommandError(
            "service command failed (1): systemctl --user show: boom",
            reason="manager-command-failed",
        ),
        ServiceCommandError(
            "service command is unavailable: launchctl: boom",
            reason="manager-command-unavailable",
        ),
    ],
)
def test_install_failure_is_nonfatal_with_one_notice(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    services,  # type: ignore[no-untyped-def]
    install_error: Exception,
) -> None:
    manager = FakeServiceManager(tmp_path, install_error=install_error)
    monkeypatch.setattr(service_module, "_service_manager", lambda: manager)
    _opt_in(monkeypatch)
    spawned = _patch_detached_spawn(monkeypatch, services)
    notices = _patch_notices(monkeypatch)

    launched = services._launch_daemon_process(ready_timeout=2.0)

    assert manager.install_calls == 1
    assert manager.runner.commands == []
    assert spawned and launched.running
    assert len(notices) == 1
    assert install_error.reason in notices[0]
    assert "detached" in notices[0]
    assert "daemon service install" in notices[0]


def test_unsupported_platform_falls_back_with_one_notice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, services  # type: ignore[no-untyped-def]
) -> None:
    def unsupported_manager() -> FakeServiceManager:
        raise service_module.ServicePlatformUnsupported("win32")

    monkeypatch.setattr(service_module, "_service_manager", unsupported_manager)
    _opt_in(monkeypatch)
    spawned = _patch_detached_spawn(monkeypatch, services)
    notices = _patch_notices(monkeypatch)

    launched = services._launch_daemon_process(ready_timeout=2.0)

    assert spawned and launched.running
    assert len(notices) == 1
    assert "win32" in notices[0]
    assert "detached" in notices[0]


def test_endpoint_overrides_skip_autoinstall(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, services  # type: ignore[no-untyped-def]
) -> None:
    manager = FakeServiceManager(tmp_path)
    monkeypatch.setattr(service_module, "_service_manager", lambda: manager)
    _opt_in(monkeypatch)
    spawned = _patch_detached_spawn(monkeypatch, services)
    notices = _patch_notices(monkeypatch)

    services._launch_daemon_process(ready_timeout=1.0, listen="tcp/0.0.0.0:1")

    assert manager.install_calls == 0
    assert manager.runner.commands == []
    assert spawned
    assert notices == []


def test_pending_restart_launch_passes_autoinstall_false(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, services  # type: ignore[no-untyped-def]
) -> None:
    missing_unit = tmp_path / "unconfigured-service.plist"
    manager = SimpleNamespace(
        unit_path=missing_unit,
        inspect=lambda serving_pid=None: SimpleNamespace(
            platform="launchd",
            unit="com.hyprial.hyprial.daemon",
            unit_path=missing_unit,
            installed=False,
            loaded=False,
            running=False,
            pid=None,
            boot_persistent=None,
            changed=None,
            attributed=False,
            template_matches=False,
            serving_pid=None,
            supervision="unsupervised",
            relaunch_count=None,
            last_exit_code=None,
            ownership_busy=False,
            ownership_reason=None,
            backup_path=None,
        ),
    )
    monkeypatch.setattr(service_module, "_service_manager", lambda: manager)
    captured: list[dict[str, Any]] = []

    def capture(**kwargs: Any) -> DaemonLaunchResult:
        captured.append(kwargs)
        return DaemonLaunchResult.launched(
            {"running": True, "pid": 4242, "epoch": "new-gen", "version": "0.5.0"}
        )

    monkeypatch.setattr(services, "_launch_daemon_process", capture)

    result = restart_daemon(
        {},
        before_pid=None,
        before_epoch=None,
        installed_version=None,
        stop=False,
    )

    assert captured == [
        {
            "ready_timeout": _cli_support.DAEMON_START_HARD_CAP_SECONDS,
            "autoinstall": False,
        }
    ]
    assert result.running and result.pid == 4242


def test_account_home_match_fails_closed_without_pwd(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import builtins

    from hyprial.shell.impl.cli.commands.common import daemon_start as daemon_start_module

    real_import = builtins.__import__

    def refuse_pwd(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "pwd":
            raise ImportError("No module named 'pwd'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", refuse_pwd)
    monkeypatch.setattr(os, "getuid", lambda: 1000, raising=False)
    assert daemon_start_module._home_matches_account_home() is False

    monkeypatch.undo()
    monkeypatch.delattr(os, "getuid", raising=False)
    assert daemon_start_module._home_matches_account_home() is False
