"""0.5.5.1: upgrade restart verdicts use daemon identity facts."""

from __future__ import annotations

import builtins
import importlib
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from typing import Any

import pytest
from typer.testing import CliRunner

from hyprial.daemon.impl.autoupdate.alert import AlertOutcome
from hyprial.kernel import DaemonLaunchResult, ipc_errors
from hyprial.shell.impl.cli.commands.common import support as _cli_support
from hyprial.shell.impl.cli.commands.common.services import get_services as _cli_get_services
from hyprial.shell.impl.cli.runtime import entry as cli

from .support import (
    FRESH_LAUNCH_PING,
    _fake_upgrade,
    _locked_constraints as _locked_constraints,
)


_REAL_PRIME_POST_INSTALL_IMPORTS = _cli_get_services()._prime_post_install_restart_imports


def _alert_recorder(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    alerts: list[str] = []

    def notify(**kwargs: Any) -> AlertOutcome:
        alerts.append(str(kwargs["detail"]))
        return AlertOutcome(attempted=True, delivered=True, message_id="om_verdict")

    monkeypatch.setattr(_cli_get_services(), "notify_upgrade_failure", notify)
    return alerts


def test_slow_live_child_gets_the_hard_cap_and_succeeds_without_failure_alert(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A1: the 30 second idle window is not an upgrade failure boundary."""

    observed: list[float] = []

    def slow_launch(*, ready_timeout: float) -> DaemonLaunchResult:
        observed.append(ready_timeout)
        return DaemonLaunchResult.launched(
            {
                "running": True,
                "pid": 4202,
                "epoch": "new",
                "version": "0.4.0.dev2",
                "phase": "serving",
            }
        )

    alerts = _alert_recorder(monkeypatch)
    _fake_upgrade(monkeypatch, tmp_path, launch=slow_launch)

    outcome = CliRunner().invoke(cli.app, ["daemon", "upgrade", "--json"])

    assert outcome.exit_code == 0, outcome.output
    payload = json.loads(outcome.stdout)
    assert observed == [_cli_support.DAEMON_START_HARD_CAP_SECONDS]
    assert payload["restart"]["before"] == {
        "pid": 4101,
        "epoch": "old",
        "version": "0.4.0.dev1",
    }
    assert payload["restart"]["after"] == {
        "pid": 4202,
        "epoch": "new",
        "version": "0.4.0.dev2",
    }
    assert alerts == []


def test_slow_supervised_restart_uses_the_upgrade_hard_cap(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A1-supervised: the real manager waits past 10s and 30s, then succeeds."""

    from hyprial.daemon import ServiceConfig, ServiceManager
    import hyprial.shell.impl.cli.commands.daemon.service as service_module

    events = _fake_upgrade(monkeypatch, tmp_path)
    original_request = _cli_support._daemon_request
    clock = [0.0]
    restarted = [False]
    ready_at = _cli_support.DAEMON_START_IDLE_BUDGET_SECONDS + 1.0
    polls = [0]

    config = ServiceConfig(
        executable=Path(sys.executable),
        home=tmp_path / "service-home",
        hyprial_home=tmp_path / "service-home" / ".hyprial",
        state_dir=tmp_path / "state",
    )

    def command_runner(
        command: tuple[str, ...], *, timeout: float | None = None
    ) -> subprocess.CompletedProcess[str]:
        del timeout
        if command[:3] == ("systemctl", "--user", "show"):
            polls[0] += 1
            pid = 4202 if restarted[0] else 4101
            return subprocess.CompletedProcess(
                command,
                0,
                "LoadState=loaded\nActiveState=active\nSubState=running\n"
                f"MainPID={pid}\nNRestarts=1\nExecMainStatus=0\n",
                "",
            )
        if command[:2] == ("loginctl", "show-user"):
            return subprocess.CompletedProcess(command, 0, "yes\n", "")
        if command[:4] == ("systemctl", "--user", "restart", "hyprial.service"):
            restarted[0] = True
            return subprocess.CompletedProcess(command, 0, "", "")
        raise AssertionError(command)

    def sleep(seconds: float) -> None:
        clock[0] += seconds

    manager = ServiceManager(
        config,
        platform="systemd",
        service_home=config.home,
        runner=command_runner,
        sleep=sleep,
        username="owner",
    )
    manager.unit_path.parent.mkdir(parents=True, mode=0o700)
    manager.unit_path.write_text(manager.templates.render_systemd(config), encoding="utf-8")
    manager.unit_path.chmod(0o600)
    monkeypatch.setattr(service_module, "_service_manager", lambda: manager)

    def daemon_probe(timeout: float = 0.5) -> dict[str, Any]:
        del timeout
        if not restarted[0]:
            return {
                "running": True,
                "pid": 4101,
                "epoch": "old",
                "version": "0.4.0.dev1",
            }
        if clock[0] <= ready_at:
            raise ipc_errors.DaemonUnavailableError("supervised daemon starting")
        return dict(FRESH_LAUNCH_PING)

    monkeypatch.setattr(_cli_get_services(), "_daemon_probe", daemon_probe)

    def daemon_request(method: str, *args: Any, **kwargs: Any) -> dict[str, Any]:
        if method == "ping" and restarted[0] and clock[0] > ready_at:
            events.append(f"ping:{kwargs.get('timeout', 15):g}")
            return dict(FRESH_LAUNCH_PING, phase="reconciled")
        return original_request(method, *args, **kwargs)

    monkeypatch.setattr(_cli_support, "_daemon_request", daemon_request)
    alerts = _alert_recorder(monkeypatch)

    outcome = CliRunner().invoke(cli.app, ["daemon", "upgrade", "--json"])

    assert outcome.exit_code == 0, outcome.output
    payload = json.loads(outcome.stdout)
    assert clock[0] > _cli_support.DAEMON_START_IDLE_BUDGET_SECONDS
    assert clock[0] < _cli_support.DAEMON_START_HARD_CAP_SECONDS
    assert polls[0] > 40, "the ordinary ServiceManager budget must have elapsed"
    assert payload["restart"]["supervision"] == "supervised"
    assert payload["restart"]["after"] == {
        "pid": 4202,
        "epoch": "new",
        "version": "0.4.0.dev2",
    }
    assert alerts == []
    assert not any(event.startswith("launch:") for event in events)


def _supervised_status(
    tmp_path: Path,
    *,
    pid: int,
    serving_pid: int | None,
    running: bool = True,
    supervision: str = "supervised",
    last_exit_code: int | None = 0,
) -> SimpleNamespace:
    return SimpleNamespace(
        platform="launchd",
        unit="com.hyprial.hyprial.daemon",
        unit_path=tmp_path / "service.plist",
        installed=True,
        loaded=True,
        running=running,
        pid=pid,
        boot_persistent=True,
        changed=None,
        attributed=True,
        template_matches=True,
        serving_pid=serving_pid,
        supervision=supervision,
        relaunch_count=1,
        last_exit_code=last_exit_code,
        ownership_busy=False,
        ownership_reason=None,
        backup_path=None,
    )


def _run_supervised_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    after: SimpleNamespace,
    probe_after: dict[str, Any] | BaseException,
) -> tuple[Any, list[float], list[str]]:
    _fake_upgrade(monkeypatch, tmp_path)
    import hyprial.shell.impl.cli.commands.daemon.service as service_module

    restarted = False
    observed: list[float] = []
    before = _supervised_status(tmp_path, pid=4101, serving_pid=4101)

    def restart(_serving_pid: Any, *, timeout_seconds: float) -> SimpleNamespace:
        nonlocal restarted
        observed.append(timeout_seconds)
        restarted = True
        return after

    manager = SimpleNamespace(inspect=lambda serving_pid=None: before, restart=restart)
    monkeypatch.setattr(service_module, "_service_manager", lambda: manager)

    def probe(timeout: float = 0.5) -> dict[str, Any]:
        if not restarted:
            return {
                "running": True,
                "pid": 4101,
                "epoch": "old",
                "version": "0.4.0.dev1",
            }
        if isinstance(probe_after, BaseException):
            raise probe_after
        return probe_after

    monkeypatch.setattr(_cli_get_services(), "_daemon_probe", probe)
    alerts = _alert_recorder(monkeypatch)
    outcome = CliRunner().invoke(cli.app, ["daemon", "upgrade", "--json"])
    return outcome, observed, alerts


def test_supervised_child_exit_is_a_real_failure_and_alerts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A2-supervised: an exited supervisor child is terminal."""

    after = _supervised_status(
        tmp_path,
        pid=4202,
        serving_pid=None,
        running=False,
        supervision="lost",
        last_exit_code=17,
    )
    outcome, observed, alerts = _run_supervised_failure(
        monkeypatch,
        tmp_path,
        after=after,
        probe_after=ipc_errors.DaemonUnavailableError("no daemon"),
    )

    assert outcome.exit_code == 1
    payload = json.loads(outcome.stdout)
    assert payload["code"] == "UPGRADE_RESTART_FAILED"
    assert "exited" in payload["data"]["restart"]["reason"]
    assert observed == [_cli_support.DAEMON_START_HARD_CAP_SECONDS]
    assert alerts and "exited" in alerts[0]


def test_supervised_live_child_times_out_at_the_named_hard_cap_and_alerts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A3-supervised: custody wait failure names the shared hard cap."""

    after = _supervised_status(
        tmp_path,
        pid=4202,
        serving_pid=None,
        supervision="lost",
        last_exit_code=None,
    )
    outcome, observed, alerts = _run_supervised_failure(
        monkeypatch,
        tmp_path,
        after=after,
        probe_after=ipc_errors.DaemonUnavailableError("not ready"),
    )

    assert outcome.exit_code == 1
    reason = json.loads(outcome.stdout)["data"]["restart"]["reason"]
    assert "hard cap" in reason
    assert observed == [_cli_support.DAEMON_START_HARD_CAP_SECONDS]
    assert alerts and "hard cap" in alerts[0]


def test_supervised_old_daemon_reaches_the_shared_identity_verdict(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A4-supervised: an old answer is not hidden as supervisor custody loss."""

    old_answer = {
        "running": True,
        "pid": 4101,
        "epoch": "old",
        "version": "0.4.0.dev1",
        "phase": "reconciled",
    }
    after = _supervised_status(
        tmp_path,
        pid=4101,
        serving_pid=4101,
        supervision="lost",
        last_exit_code=None,
    )
    outcome, observed, alerts = _run_supervised_failure(
        monkeypatch, tmp_path, after=after, probe_after=old_answer
    )

    assert outcome.exit_code == 1
    reason = json.loads(outcome.stdout)["data"]["restart"]["reason"]
    assert "old daemon is still answering" in reason
    assert observed == [_cli_support.DAEMON_START_HARD_CAP_SECONDS]
    assert alerts and "old daemon is still answering" in alerts[0]


def test_child_exit_is_a_real_failure_and_alerts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A2: an observed child exit is terminal and names that fact."""

    def exited(*, ready_timeout: float) -> None:
        assert ready_timeout == _cli_support.DAEMON_START_HARD_CAP_SECONDS
        raise _cli_support.CliError(
            ipc_errors.DAEMON_START_FAILED,
            "daemon exited during startup with status 17",
        )

    alerts = _alert_recorder(monkeypatch)
    _fake_upgrade(monkeypatch, tmp_path, launch=exited)

    outcome = CliRunner().invoke(cli.app, ["daemon", "upgrade", "--json"])

    assert outcome.exit_code == 1
    payload = json.loads(outcome.stdout)
    assert payload["code"] == "UPGRADE_RESTART_FAILED"
    assert "exited" in payload["data"]["restart"]["reason"]
    assert alerts and "exited" in alerts[0]


def test_live_child_that_never_answers_fails_at_the_named_hard_cap(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A3: a live child gets the whole bounded wait, then alerts."""

    def hard_cap(*, ready_timeout: float) -> None:
        assert ready_timeout == _cli_support.DAEMON_START_HARD_CAP_SECONDS
        raise _cli_support.CliError(
            ipc_errors.DAEMON_START_TIMEOUT,
            f"daemon did not become ready within the hard cap of "
            f"{_cli_support.DAEMON_START_HARD_CAP_SECONDS:g}s; child is still alive",
        )

    alerts = _alert_recorder(monkeypatch)
    _fake_upgrade(monkeypatch, tmp_path, launch=hard_cap)

    outcome = CliRunner().invoke(cli.app, ["daemon", "upgrade", "--json"])

    assert outcome.exit_code == 1
    payload = json.loads(outcome.stdout)
    reason = payload["data"]["restart"]["reason"]
    assert "hard cap" in reason
    assert alerts and "hard cap" in alerts[0]


def test_old_daemon_identity_cannot_be_reported_as_restart_success(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A4: an answering old pid/epoch is a stop failure, not success."""

    def old_generation(*, ready_timeout: float) -> DaemonLaunchResult:
        assert ready_timeout == _cli_support.DAEMON_START_HARD_CAP_SECONDS
        return DaemonLaunchResult.existing(
            {
                "running": True,
                "pid": 4101,
                "epoch": "old",
                "version": "0.4.0.dev1",
                "phase": "reconciled",
            }
        )

    alerts = _alert_recorder(monkeypatch)
    _fake_upgrade(monkeypatch, tmp_path, launch=old_generation)

    outcome = CliRunner().invoke(cli.app, ["daemon", "upgrade", "--json"])

    assert outcome.exit_code == 1
    payload = json.loads(outcome.stdout)
    reason = payload["data"]["restart"]["reason"]
    assert "old daemon is still answering" in reason
    assert alerts and "old daemon is still answering" in alerts[0]


def test_fresh_identity_with_old_version_is_a_real_failure_and_alerts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A4: a fresh process that serves old code cannot prove the upgrade."""

    def old_version(*, ready_timeout: float) -> DaemonLaunchResult:
        assert ready_timeout == _cli_support.DAEMON_START_HARD_CAP_SECONDS
        return DaemonLaunchResult.launched(
            {
                "running": True,
                "pid": 4202,
                "epoch": "new",
                "version": "0.4.0.dev1",
                "phase": "serving",
            }
        )

    alerts = _alert_recorder(monkeypatch)
    _fake_upgrade(monkeypatch, tmp_path, launch=old_version)

    outcome = CliRunner().invoke(cli.app, ["daemon", "upgrade", "--json"])

    assert outcome.exit_code == 1
    payload = json.loads(outcome.stdout)
    reason = payload["data"]["restart"]["reason"]
    assert "reports version 0.4.0.dev1" in reason
    assert "expected installed version 0.4.0.dev2" in reason
    assert alerts and "reports version 0.4.0.dev1" in alerts[0]


def test_post_install_path_cannot_import_outside_the_primed_module_map(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A5/A6: execute the old-process tail over a different install tree."""

    events = _fake_upgrade(monkeypatch, tmp_path)
    primed: frozenset[str] = frozenset()
    original_import = builtins.__import__
    old_package_paths: dict[str, tuple[str, ...]] = {}

    def prime() -> frozenset[str]:
        nonlocal primed
        events.append("prime")
        primed = _REAL_PRIME_POST_INSTALL_IMPORTS()
        return primed

    def old_process_import(
        name: str,
        globals: dict[str, Any] | None = None,
        locals: dict[str, Any] | None = None,
        fromlist: tuple[str, ...] = (),
        level: int = 0,
    ) -> Any:
        if name.startswith("hyprial.") and name not in primed:
            raise ImportError("old process refused lazy new-package module: " + name)
        return original_import(name, globals, locals, fromlist, level)

    class RejectUnprimedHyprialModules:
        def find_spec(
            self, fullname: str, path: object = None, target: object = None
        ) -> None:
            del path, target
            if fullname.startswith("hyprial.") and fullname not in primed:
                raise ImportError(
                    "old process refused lazy new-package module: " + fullname
                )
            return None

    def install(argv: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        events.append("install")
        new_package = tmp_path / "new-install" / "hyprial"
        for name in primed:
            module = sys.modules.get(name)
            package_path = getattr(module, "__path__", None)
            if module is None or package_path is None:
                continue
            old_package_paths[name] = tuple(str(item) for item in package_path)
            replacement = new_package.joinpath(*name.split(".")[1:])
            replacement.mkdir(parents=True, exist_ok=True)
            (replacement / "__init__.py").write_text("", encoding="utf-8")
            monkeypatch.setattr(module, "__path__", [str(replacement)])
        new_kernel = new_package / "kernel" / "__init__.py"
        new_kernel.write_text("ONLY_IN_NEW_KERNEL = object()\n", encoding="utf-8")
        old_kernel = sys.modules["hyprial.kernel"]
        assert hasattr(old_kernel, "DaemonLaunchResult")
        assert not hasattr(old_kernel, "ONLY_IN_NEW_KERNEL")
        assert "DaemonLaunchResult" not in new_kernel.read_text(encoding="utf-8")
        monkeypatch.setattr(builtins, "__import__", old_process_import)
        monkeypatch.setattr(
            sys,
            "meta_path",
            [RejectUnprimedHyprialModules(), *sys.meta_path],
        )
        return subprocess.CompletedProcess(argv, 0, "installed\n", "")

    monkeypatch.setattr(_cli_get_services(), "_prime_post_install_restart_imports", prime)
    monkeypatch.setattr(subprocess, "run", install)

    outcome = CliRunner().invoke(cli.app, ["daemon", "upgrade", "--json"])

    assert outcome.exit_code == 0, outcome.output
    assert events.index("prime") < events.index("install")
    assert "hyprial.kernel" in old_package_paths
    assert json.loads(outcome.stdout)["restart"]["restarted"] is True


@pytest.mark.parametrize("route", ["dunder-import", "importlib", "lazy-facade"])
def test_import_drift_guard_rejects_every_module_loading_route(
    monkeypatch: pytest.MonkeyPatch, route: str
) -> None:
    """B1 mutation controls: every Python module loader reaches the fence."""

    import hyprial.daemon as daemon_facade

    target = "hyprial.daemon.impl.configuration.home_guard"
    primed = _REAL_PRIME_POST_INSTALL_IMPORTS() - {target}
    monkeypatch.delitem(sys.modules, target, raising=False)
    monkeypatch.delitem(daemon_facade.__dict__, "live_daemon_pid", raising=False)

    class RejectUnprimedHyprialModules:
        def find_spec(
            self, fullname: str, path: object = None, target: object = None
        ) -> None:
            del path, target
            if fullname.startswith("hyprial.") and fullname not in primed:
                raise ImportError(
                    "old process refused lazy new-package module: " + fullname
                )
            return None

    monkeypatch.setattr(
        sys, "meta_path", [RejectUnprimedHyprialModules(), *sys.meta_path]
    )

    with pytest.raises(ImportError, match="old process refused"):
        if route == "dunder-import":
            builtins.__import__(target, fromlist=("live_daemon_pid",))
        elif route == "importlib":
            importlib.import_module(target)
        else:
            getattr(daemon_facade, "live_daemon_pid")


def test_old_process_primes_the_complete_post_install_module_map() -> None:
    """A5: post-install work cannot discover a module from new files.

    This subprocess starts with the old CLI modules in ``sys.modules``.  The
    primer must return the exact allowed map and include the lazy owner-alert
    SDK before an in-place install can replace the package on disk.  A future
    post-install lazy module therefore makes this guard fail before release.
    """

    script = """
import builtins
import json
import sys
import hyprial.cli
from hyprial.shell.impl.cli.commands.common import support as _cli_support
from hyprial.shell.impl.cli.commands.common.services import get_services as _cli_get_services
from hyprial.shell.impl.cli.runtime import entry as cli

sdk = 'hyprial.daemon.impl.adapters.lark.outbound.sdk'
assert sdk not in sys.modules
primed = _cli_get_services()._prime_post_install_restart_imports()
assert isinstance(primed, frozenset)
assert sdk in primed and sdk in sys.modules
original = builtins.__import__

def old_process_import(name, globals=None, locals=None, fromlist=(), level=0):
    if name.startswith('hyprial.') and name not in primed:
        raise ImportError('old process refused lazy new-package module: ' + name)
    return original(name, globals, locals, fromlist, level)

builtins.__import__ = old_process_import
from hyprial.daemon.impl.adapters.lark.outbound.sdk import LarkSdkGateway
from hyprial.shell.impl.cli.commands.daemon.service import restart_supervised_daemon
print(json.dumps({'sdk': LarkSdkGateway.__name__, 'restart': callable(restart_supervised_daemon)}))
"""
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).resolve().parents[5],
        env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[5] / "src")},
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout) == {"sdk": "LarkSdkGateway", "restart": True}
