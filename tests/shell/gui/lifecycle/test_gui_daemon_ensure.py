"""DL-1: ``hyprial gui`` starts an owned home's missing daemon before admission.

The launcher owns the rule (driver resolution, cheap validation, then the
ensure hook, then admission); the CLI only supplies the launch primitive.
These tests drive the real ``build_plan``/``admit_source`` with a fake
transport and a fake launch primitive.
"""

from __future__ import annotations

import json
from functools import partial
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from typer.testing import CliRunner

import hyprial.shell.impl.cli.runtime.entry  # noqa: F401
from hyprial import __version__
from hyprial.daemon import daemon_catalog
from hyprial.daemon.impl.application.onboarding.bootstrap.dispatcher import (
    WIRE_POLICY,
    BootstrapDispatcher,
)
from hyprial.kernel import ipc_errors
from hyprial.shell.impl.cli.commands.common.root import app as cli_root_app
from hyprial.shell.impl.cli.commands.common.services import get_services
from hyprial.shell.impl.cli.commands.common.support import (
    DAEMON_START_IDLE_BUDGET_SECONDS,
    CliError,
)
from hyprial.shell.impl.cli.commands.gui import commands
from hyprial.shell.impl.gui import launcher
from hyprial.shell.impl.gui.backend import admission, config
from hyprial.shell.impl.gui.errors import GuiError
from hyprial.shell.impl.gui.install import launch as target_launch
from hyprial.shell.impl.gui.install import resources

OWNER = "ensure-owner"
NODE = "ensure-node"


class Daemon:
    """A fake daemon behind the admission transport seam."""

    def __init__(
        self,
        home: Path,
        *,
        running: bool = False,
        restoring_polls: int = 0,
        bootstrap: bool = False,
    ) -> None:
        self.home = home
        self.running = running
        self.restoring_polls = restoring_polls
        self.bootstrap = bootstrap
        self.pings = 0

    def start(self) -> None:
        self.running = True

    def request(self, method: str, params: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        if not self.running:
            raise ipc_errors.DaemonUnavailableError("not running")
        if self.bootstrap:
            return self._bootstrap_answer(method)
        if method == "ping":
            self.pings += 1
            restoring = self.pings <= self.restoring_polls
            return {
                "running": True,
                "pid": 321,
                "version": __version__,
                "nodeId": NODE,
                "owner": OWNER,
                "phase": "restoring" if restoring else "serving",
                "restorePending": restoring,
                "epoch": "epoch-1",
                "socket": str(self.home / "state" / "daemon.sock"),
            }
        entries = daemon_catalog()["methods"]
        for entry in entries:
            entry.update(declared=True, state="available", callable=True, reason=None)
        return {
            "schemaVersion": 1,
            "catalogVersion": "1",
            "epoch": "epoch-1",
            "packageVersion": __version__,
            "methods": entries,
        }


    def _bootstrap_answer(self, method: str) -> dict[str, Any]:
        if method == "ping":
            self.pings += 1
        runtime = SimpleNamespace(
            socket_path=self.home / "state" / "daemon.sock",
            view=SimpleNamespace(record=SimpleNamespace(epoch="epoch-1")),
        )
        dispatcher = BootstrapDispatcher(app=None, runtime=runtime, context=None, policy=WIRE_POLICY)
        dispatcher._promotion_enabled = True
        if method == "ping":
            return {**dispatcher.ping(), "nodeId": NODE, "pid": 321}
        return dispatcher.catalog()


class Clock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class Launches:
    def __init__(self, daemon: Daemon | None = None) -> None:
        self.daemon = daemon
        self.calls: list[dict[str, Any]] = []

    def __call__(self, **kwargs: Any) -> object:
        self.calls.append(kwargs)
        if self.daemon is not None:
            self.daemon.start()
        return {"status": "launched"}


def _forbidden(**kwargs: Any) -> object:
    pytest.fail("a refused or ownerless launch reached the daemon launch primitive")


def _home(tmp_path: Path, *, owner: str | None = OWNER) -> Path:
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    settings: dict[str, Any] = {"version": 1}
    if owner is not None:
        settings["owner"] = owner
    (home / "settings.json").write_text(json.dumps(settings), encoding="utf-8")
    return home


def _environ(home: Path, **extra: str) -> dict[str, str]:
    return {
        "HOME": str(home.parent / "user"),
        "HYPRIAL_HOME": str(home),
        "HARNESS_STATE_DIR": str(home / "state"),
        "HYPRIAL_NODE_ID": NODE,
        **extra,
    }


def _wheel(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "wheel-gui"
    root.mkdir()
    (root / "index.html").write_text("<h1>installed</h1>\n", encoding="utf-8")
    resources.write_manifest(
        root,
        resources.build_manifest(
            root,
            package_version="0.0.0",
            source_commit="a" * 40,
            source_remote="https://example.invalid/hyprial",
            gui_version="0.0.0",
        ),
    )
    monkeypatch.setattr(target_launch, "installed_asset_root", lambda: root.resolve())
    monkeypatch.setattr(launcher, "_assert_port_free", lambda host, port: None)
    return root


def _admit(daemon: Daemon, clock: Clock | None = None) -> Any:
    options: dict[str, Any] = {"request_once": daemon.request}
    if clock is not None:
        options.update(sleep=clock.sleep, monotonic=clock.monotonic)
    return partial(admission.admit_source, **options)


def _project(tmp_path: Path, *, dependencies: bool) -> launcher.GuiProject:
    project_home = tmp_path / "checkout" / "gui"
    project_home.mkdir(parents=True)
    if dependencies:
        (project_home / "node_modules").mkdir()
    return launcher.GuiProject(
        home=project_home,
        worktree=project_home.parent,
        branch="work/x",
        node="/fake/node",
        npm="/fake/npm",
        node_version="v24.0.0",
        npm_version="11.0.0",
        scripts={"dev": "vite", "start": "node server.mjs"},
    )


def test_installed_unset_driver_starts_the_missing_daemon_before_admission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _wheel(tmp_path, monkeypatch)
    home = _home(tmp_path)
    daemon = Daemon(home)
    launches = Launches(daemon)

    plan = launcher.build_plan(
        "",
        environ=_environ(home),
        cwd=tmp_path,
        admit=_admit(daemon),
        ensure_daemon=lambda: launches(ready_timeout=DAEMON_START_IDLE_BUDGET_SECONDS),
    )

    assert plan.driver == "daemon"
    assert plan.admission is not None and plan.admission.principal == f"user:{OWNER}"
    assert launches.calls == [{"ready_timeout": DAEMON_START_IDLE_BUDGET_SECONDS}]


def test_installed_explicit_daemon_driver_starts_the_missing_daemon(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _wheel(tmp_path, monkeypatch)
    home = _home(tmp_path)
    daemon = Daemon(home)
    launches = Launches(daemon)

    launcher.build_plan(
        "",
        environ=_environ(home, HYPRIAL_GUI_BACKEND_DRIVER="daemon"),
        cwd=tmp_path,
        admit=_admit(daemon),
        ensure_daemon=lambda: launches(),
    )

    assert len(launches.calls) == 1


def test_a_running_daemon_is_handed_to_the_primitive_once_per_invocation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _wheel(tmp_path, monkeypatch)
    home = _home(tmp_path)
    daemon = Daemon(home, running=True)
    launches = Launches(daemon)

    for _ in range(2):
        launcher.build_plan(
            "",
            environ=_environ(home),
            cwd=tmp_path,
            admit=_admit(daemon),
            ensure_daemon=lambda: launches(),
        )

    assert len(launches.calls) == 2
    assert daemon.pings >= 2


def test_ownerless_home_reaches_the_launch_primitive_once_and_is_admitted_as_bootstrap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _wheel(tmp_path, monkeypatch)
    home = _home(tmp_path, owner=None)
    daemon = Daemon(home, bootstrap=True)
    launches = Launches(daemon)

    plan = launcher.build_plan(
        "",
        environ=_environ(home),
        cwd=tmp_path,
        admit=_admit(daemon),
        ensure_daemon=lambda: launches(),
    )

    assert plan.admission is not None and plan.admission.phase == "bootstrap"
    assert plan.admission.owner is None and plan.admission.principal is None
    assert len(launches.calls) == 1
    assert daemon.pings >= 1


def test_contract_mock_source_admission_never_ensures() -> None:
    result = admission.admit_source(
        {"HYPRIAL_GUI_BACKEND_DRIVER": "contract-mock"},
        ensure_daemon=_forbidden,
    )

    assert result.driver == "contract-mock"


REFUSALS = {
    "unknown-action": (("start",), {}, {}, "INVALID_ARGUMENT"),
    "non-loopback-ip": (("",), {"ip": "0.0.0.0"}, {}, "INVALID_ARGUMENT"),
    "bad-ip": (("",), {"ip": "not-an-ip"}, {}, "INVALID_ARGUMENT"),
    "port-negative": (("",), {"port": -1}, {}, "INVALID_ARGUMENT"),
    "port-too-large": (("",), {"port": 70000}, {}, "INVALID_ARGUMENT"),
    "blank-driver": (("",), {}, {"HYPRIAL_GUI_BACKEND_DRIVER": " "}, "GUI_BACKEND_DRIVER"),
    "unknown-driver": (("",), {}, {"HYPRIAL_GUI_BACKEND_DRIVER": "other"}, "GUI_BACKEND_DRIVER"),
    "contract-mock-from-wheel": (
        ("",),
        {},
        {"HYPRIAL_GUI_BACKEND_DRIVER": "contract-mock"},
        "GUI_MOCK_UNAVAILABLE",
    ),
    "gui-home-on-wheel": (("",), {"gui_home": Path("/nonexistent")}, {}, "INVALID_ARGUMENT"),
    "gui-home-env-on-wheel": (("",), {}, {"HYPRIAL_GUI_HOME": "/nonexistent"}, "GUI_CONFIG_CONFLICT"),
    "owner-override": (("",), {}, {"HYPRIAL_OWNER": OWNER}, "GUI_OWNER_OVERRIDE"),
    "legacy-config": (("",), {}, {"HYPRIAL_GUI_AUTH": "x"}, "GUI_CONFIG_CONFLICT"),
    "principal-mismatch": (
        ("",),
        {},
        {"HYPRIAL_GUI_PRINCIPAL": "user:someone-else"},
        "GUI_PRINCIPAL_MISMATCH",
    ),
    "socket-mismatch": (
        ("",),
        {},
        {"HARNESS_SOCKET_PATH": "/tmp/elsewhere.sock"},
        "GUI_SOCKET_MISMATCH",
    ),
}


@pytest.mark.parametrize("name", sorted(REFUSALS))
def test_refusals_never_start_a_daemon(
    name: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    args, options, extra, code = REFUSALS[name]
    _wheel(tmp_path, monkeypatch)
    home = _home(tmp_path)
    daemon = Daemon(home)

    with pytest.raises(GuiError) as caught:
        launcher.build_plan(
            *args,
            environ=_environ(home, **extra),
            cwd=tmp_path,
            admit=_admit(daemon),
            ensure_daemon=_forbidden,
            **options,
        )

    assert caught.value.code == code
    assert daemon.pings == 0


def test_missing_home_never_starts_a_daemon(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _wheel(tmp_path, monkeypatch)

    with pytest.raises(GuiError) as caught:
        launcher.build_plan(
            "",
            environ={"HYPRIAL_HOME": str(tmp_path / "absent")},
            cwd=tmp_path,
            ensure_daemon=_forbidden,
        )

    assert caught.value.code == "GUI_HOME_NOT_INITIALIZED"


def test_source_dev_with_a_valid_project_ensures_then_admits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _home(tmp_path)
    project = _project(tmp_path, dependencies=True)
    monkeypatch.setattr(launcher, "_assert_port_free", lambda host, port: None)
    monkeypatch.setattr(target_launch, "_dev_prompt", lambda *args, **kwargs: "")
    daemon = Daemon(home)
    launches = Launches(daemon)

    plan = launcher.build_plan(
        "dev",
        gui_home=project.home,
        environ=_environ(home, HYPRIAL_GUI_BACKEND_DRIVER="daemon"),
        cwd=tmp_path,
        inspect=lambda *args, **kwargs: project,
        admit=_admit(daemon),
        ensure_daemon=lambda: launches(),
    )

    assert plan.driver == "daemon"
    assert plan.backend_owned is False
    assert len(launches.calls) == 1


def test_source_contract_mock_with_a_valid_project_never_ensures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _home(tmp_path)
    project = _project(tmp_path, dependencies=True)
    monkeypatch.setattr(launcher, "_assert_port_free", lambda host, port: None)
    monkeypatch.setattr(target_launch, "_dev_prompt", lambda *args, **kwargs: "")

    plan = launcher.build_plan(
        "dev",
        gui_home=project.home,
        environ=_environ(home, HYPRIAL_GUI_BACKEND_DRIVER="contract-mock"),
        cwd=tmp_path,
        inspect=lambda *args, **kwargs: project,
        ensure_daemon=_forbidden,
    )

    assert plan.driver == "contract-mock"


def test_admission_waits_for_a_restoring_daemon_within_the_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _wheel(tmp_path, monkeypatch)
    home = _home(tmp_path)
    daemon = Daemon(home, restoring_polls=5)
    clock = Clock()
    launches = Launches(daemon)

    plan = launcher.build_plan(
        "",
        environ=_environ(home),
        cwd=tmp_path,
        admit=_admit(daemon, clock),
        ensure_daemon=lambda: launches(),
    )

    assert plan.admission is not None and plan.admission.phase == "serving"
    assert len(launches.calls) == 1
    assert len(clock.sleeps) == 5
    assert sum(clock.sleeps) < admission.RESTORE_WAIT_SECONDS


def test_admission_without_the_ensure_hook_still_fails_fast_when_restoring(
    tmp_path: Path,
) -> None:
    home = _home(tmp_path)
    daemon = Daemon(home, running=True, restoring_polls=1)
    clock = Clock()

    with pytest.raises(GuiError) as caught:
        admission.admit_source(
            _environ(home, HYPRIAL_GUI_BACKEND_DRIVER="daemon"),
            request_once=daemon.request,
            sleep=clock.sleep,
            monotonic=clock.monotonic,
        )

    assert caught.value.code == "GUI_DAEMON_NOT_READY"
    assert clock.sleeps == []


def test_a_daemon_that_never_finishes_restoring_fails_inside_the_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _wheel(tmp_path, monkeypatch)
    home = _home(tmp_path)
    daemon = Daemon(home, restoring_polls=10**9)
    clock = Clock()
    launches = Launches(daemon)

    with pytest.raises(GuiError) as caught:
        launcher.build_plan(
            "",
            environ=_environ(home),
            cwd=tmp_path,
            admit=_admit(daemon, clock),
            ensure_daemon=lambda: launches(),
        )

    assert caught.value.code == "GUI_DAEMON_NOT_READY"
    assert "restoring" in str(caught.value)
    assert len(launches.calls) == 1
    assert admission.RESTORE_WAIT_SECONDS <= clock.now < admission.RESTORE_WAIT_SECONDS + 1


def test_unavailable_daemon_after_ensure_is_not_waited_for(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _wheel(tmp_path, monkeypatch)
    home = _home(tmp_path)
    clock = Clock()

    with pytest.raises(GuiError) as caught:
        launcher.build_plan(
            "",
            environ=_environ(home),
            cwd=tmp_path,
            admit=_admit(Daemon(home), clock),
            ensure_daemon=lambda: None,
        )

    assert caught.value.code == "GUI_DAEMON_UNAVAILABLE"
    assert clock.sleeps == []


def _run_gui(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    daemon: Daemon,
    launch: Any,
    clock: Clock | None = None,
    extra: list[str] | None = None,
    slow_backend: bool = False,
    json_output: bool = True,
    host_exit: tuple[int, str] | None = None,
) -> tuple[Any, list[Any], list[str]]:
    _wheel(tmp_path, monkeypatch)
    for key, value in _environ(daemon.home).items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("HYPRIAL_GUI_BACKEND_DRIVER", raising=False)
    monkeypatch.delenv("HYPRIAL_OWNER", raising=False)
    started: list[Any] = []
    events: list[str] = []
    fake = SimpleNamespace(
        runtime=get_services().runtime,
        _launch_daemon_process=lambda **kwargs: (events.append("launch"), launch(**kwargs))[1],
    )
    monkeypatch.setattr(commands, "get_services", lambda: fake)
    monkeypatch.setattr(config, "admit_source", _admit(daemon, clock))

    def start_session(plan: Any, **kwargs: Any) -> Any:
        started.append(plan)
        if slow_backend:
            kwargs["on_backend_wait"]()
        if host_exit is not None:
            kwargs["on_host_exit"](*host_exit)
        code = 0 if host_exit is None else host_exit[0]
        return SimpleNamespace(plan=plan, wait=lambda: code, stop=lambda: None)

    monkeypatch.setattr(launcher, "start_session", start_session)
    monkeypatch.setattr(
        launcher,
        "startup_payload",
        lambda session: {"ok": True, "url": session.plan.url, "driver": session.plan.driver},
    )
    monkeypatch.setattr(launcher, "render_startup", lambda payload: "started")
    flags = ["--json"] if json_output else []
    result = CliRunner().invoke(
        cli_root_app, ["gui", "--no-open", *flags, "--port", "4999", *(extra or [])]
    )
    return result, started, events


def test_gui_command_starts_the_daemon_then_opens_normally(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _home(tmp_path)
    daemon = Daemon(home)
    launches = Launches(daemon)

    result, started, events = _run_gui(monkeypatch, tmp_path, daemon=daemon, launch=launches)

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {
        "ok": True,
        "url": "http://127.0.0.1:4999/",
        "driver": "daemon",
    }
    assert events == ["launch"]
    assert launches.calls == [{"ready_timeout": DAEMON_START_IDLE_BUDGET_SECONDS, "autoinstall": False}]
    assert len(started) == 1


def test_gui_command_announces_a_slow_backend_start_on_stderr_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    daemon = Daemon(_home(tmp_path))

    result, _started, _events = _run_gui(
        monkeypatch, tmp_path, daemon=daemon, launch=Launches(daemon), slow_backend=True
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["ok"] is True
    announced = [json.loads(line) for line in result.stderr.splitlines() if line.startswith("{")]
    assert {"event": "gui-backend-wait"} in announced


def test_gui_command_explains_a_host_exit_on_stderr_and_keeps_its_status_and_one_stdout_object(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    daemon = Daemon(_home(tmp_path))

    result, _started, _events = _run_gui(
        monkeypatch,
        tmp_path,
        daemon=daemon,
        launch=Launches(daemon),
        host_exit=(3, "watcher_failed"),
    )

    assert result.exit_code == 3, result.output
    assert len(result.stdout.strip().splitlines()) == 1
    assert json.loads(result.stdout)["ok"] is True
    explained = [json.loads(line) for line in result.stderr.splitlines() if line.startswith("{")]
    exits = [line for line in explained if line.get("event") == "gui-host-exited"]
    assert len(exits) == 1 and exits[0]["exit_code"] == 3
    assert "watcher_failed" in exits[0]["log_tail"]


def test_gui_command_explains_a_host_exit_in_words_without_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    daemon = Daemon(_home(tmp_path))

    result, _started, _events = _run_gui(
        monkeypatch,
        tmp_path,
        daemon=daemon,
        launch=Launches(daemon),
        json_output=False,
        host_exit=(3, "watcher_failed"),
    )

    assert result.exit_code == 3, result.output
    assert result.stderr.count("The GUI host exited unexpectedly with exit code 3.") == 1
    assert "watcher_failed" in result.stderr
    assert "exited unexpectedly" not in result.stdout


def test_gui_command_announces_a_slow_backend_start_in_words_without_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    daemon = Daemon(_home(tmp_path))

    result, _started, _events = _run_gui(
        monkeypatch,
        tmp_path,
        daemon=daemon,
        launch=Launches(daemon),
        slow_backend=True,
        json_output=False,
    )

    assert result.exit_code == 0, result.output
    assert "Still starting the GUI;" in result.stderr


def test_gui_command_waits_out_a_restoring_daemon_then_opens(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _home(tmp_path)
    daemon = Daemon(home, restoring_polls=4)
    clock = Clock()

    result, started, _events = _run_gui(
        monkeypatch, tmp_path, daemon=daemon, launch=Launches(daemon), clock=clock
    )

    assert result.exit_code == 0, result.output
    assert len(started) == 1
    assert len(clock.sleeps) == 4


def test_gui_command_reports_a_daemon_that_never_restores_and_leaves_no_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _home(tmp_path)
    daemon = Daemon(home, restoring_polls=10**9)
    clock = Clock()

    result, started, events = _run_gui(
        monkeypatch, tmp_path, daemon=daemon, launch=Launches(daemon), clock=clock
    )

    assert result.exit_code == 1, result.output
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert payload["code"] == "GUI_DAEMON_NOT_READY"
    assert started == []
    assert events == ["launch"]


def test_launch_primitive_error_reaches_the_startup_record_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _home(tmp_path)

    def fail(**kwargs: Any) -> object:
        raise CliError("DAEMON_START_FAILED", "daemon exited during startup with status 3")

    result, started, _events = _run_gui(
        monkeypatch, tmp_path, daemon=Daemon(home), launch=fail
    )

    assert result.exit_code == 1, result.output
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert payload["code"] == "DAEMON_START_FAILED"
    assert payload["error"] == "daemon exited during startup with status 3"
    assert started == []


def test_gui_command_starts_the_daemon_for_an_ownerless_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _home(tmp_path, owner=None)
    daemon = Daemon(home, bootstrap=True)
    launches = Launches(daemon)

    result, started, events = _run_gui(monkeypatch, tmp_path, daemon=daemon, launch=launches)

    assert result.exit_code == 0, result.output
    assert events == ["launch"]
    assert launches.calls == [{"ready_timeout": DAEMON_START_IDLE_BUDGET_SECONDS, "autoinstall": False}]
    assert len(started) == 1


def test_gui_command_refuses_a_bad_ip_without_starting_a_daemon(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _home(tmp_path)

    result, started, events = _run_gui(
        monkeypatch,
        tmp_path,
        daemon=Daemon(home),
        launch=_forbidden,
        extra=["--ip", "0.0.0.0"],
    )

    assert result.exit_code == 1, result.output
    assert json.loads(result.stdout)["code"] == "INVALID_ARGUMENT"
    assert events == [] and started == []
