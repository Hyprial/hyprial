"""Unit coverage for the source-mode ``hyprial gui`` launcher.

These tests deliberately use fake npm/node subprocesses so the Python test job
does not need Node installed.  The real npm/Vite check is a separate UI
dependency and is recorded as NOT_RUN until the Windows owner's candidate is
available.
"""

from __future__ import annotations

from dataclasses import replace
import json
import os
from pathlib import Path
import queue
import socket
import subprocess
import sys
import textwrap
import threading

import pytest
from typer.testing import CliRunner

from hyprial.shell.impl.gui import launcher
from hyprial.shell.impl.gui.backend.admission import SourceAdmission
from hyprial.shell.impl.gui.errors import GuiError
from hyprial.shell.impl.cli.commands.common.root import app as _cli_root_app
import hyprial.shell.impl.cli.runtime.entry  # noqa: F401  (installs the explicit CLI composition)


class FakeRunner:
    def __init__(self, *, worktree: Path | None, node: str = "v24.21.0", npm: str = "11.19.0"):
        self.worktree = worktree
        self.node = node
        self.npm = npm
        self.calls: list[list[str]] = []

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        executable = argv[0]
        if executable == "/fake/git":
            if argv[-2:] == ["rev-parse", "--show-toplevel"]:
                if self.worktree is None:
                    return subprocess.CompletedProcess(argv, 1, "", "not a git repository")
                return subprocess.CompletedProcess(argv, 0, f"{self.worktree}\n", "")
            if "rev-parse" in argv and "--abbrev-ref" in argv:
                return subprocess.CompletedProcess(argv, 0, "work/gui-test\n", "")
            return subprocess.CompletedProcess(argv, 1, "", "unexpected git command")
        if executable == "/fake/node":
            return subprocess.CompletedProcess(argv, 0, f"{self.node}\n", "")
        if executable == "/fake/npm":
            return subprocess.CompletedProcess(argv, 0, f"{self.npm}\n", "")
        return subprocess.CompletedProcess(argv, 127, "", "unexpected executable")


def _which(paths: dict[str, str]):
    return lambda name: paths.get(name)


def _project(tmp_path: Path, *, scripts: dict[str, str] | None = None) -> launcher.GuiProject:
    home = tmp_path / "gui"
    home.mkdir(exist_ok=True)
    (home / "node_modules").mkdir(exist_ok=True)
    (home / "package.json").write_text(
        json.dumps({"name": "@hyprial/gui-app", "scripts": scripts or {}}),
        encoding="utf-8",
    )
    return launcher.GuiProject(
        home=home,
        worktree=tmp_path,
        branch="work/gui-test",
        node="/fake/node",
        npm="/fake/npm",
        node_version="v24.21.0",
        npm_version="11.19.0",
        scripts=scripts
        or {
            "build": "vite build",
            "dev": "vite",
        },
    )


def _fake_inspect(project: launcher.GuiProject):
    def inspect(home, **kwargs):
        assert home == project.home
        return project

    return inspect


def _fake_plan(project: launcher.GuiProject, *, mode: str = "normal") -> launcher.GuiLaunchPlan:
    return launcher.GuiLaunchPlan(
        mode=mode,
        project=project,
        host="127.0.0.1",
        port=4173,
        url="http://127.0.0.1:4173/",
        backend_endpoint=None,
        backend_owned=False,
        frontend_argv=("/fake/npm", "start"),
        env={"HYPRIAL_GUI_HOME": str(project.home)},
        open_browser=False,
    )

def _mock_admission(tmp_path: Path) -> SourceAdmission:
    state = (tmp_path / "state").resolve()
    return SourceAdmission(
        driver="contract-mock",
        owner="contract-mock",
        node="local",
        principal="user:contract-mock",
        home=None,
        state_dir=state,
        socket_path=None,
    )


class FakeProcess:
    def __init__(self, argv, *, pid: int, returncode: int | None = None, stdout=None):
        self.argv = list(argv)
        self.pid = pid
        self.returncode = returncode
        self.stdout = stdout

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        return self.returncode

    def terminate(self):
        self.returncode = -15

    def kill(self):
        self.returncode = -9


def test_build_plan_normal_selects_the_explicit_source_driver_and_the_python_host(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(launcher, "_assert_port_free", lambda host, port: None)
    project = _project(tmp_path)
    source = {
        "PATH": "/usr/bin",
        "HYPRIAL_GUI_BACKEND_DRIVER": "contract-mock",
        "HARNESS_STATE_DIR": str(tmp_path / "state"),
    }

    plan = launcher.build_plan(
        "",
        port=5173,
        environ=source,
        cwd=project.home,
        inspect=_fake_inspect(project),
    )

    assert plan.mode == "normal"
    assert plan.backend_owned is True
    assert plan.backend_endpoint is None
    assert plan.driver == "contract-mock"
    assert plan.runtime_dir == (tmp_path / "state" / "gui" / "contract-mock").resolve()
    assert plan.frontend_argv == (
        sys.executable,
        "-P",
        "-m",
        "hyprial.shell.impl.gui.host",
        "serve",
        "--root",
        str(project.home / "dist"),
        "--host",
        "127.0.0.1",
        "--port",
        "5173",
    )
    assert plan.assets_argv is None
    assert plan.build_argv == ("/fake/npm", "run", "build")
    assert plan.env["HYPRIAL_GUI_HOME"] == str(project.home)
    assert "HYPRIAL_BACKEND_ENDPOINT" not in plan.env
    assert source == {
        "PATH": "/usr/bin",
        "HYPRIAL_GUI_BACKEND_DRIVER": "contract-mock",
        "HARNESS_STATE_DIR": str(tmp_path / "state"),
    }
    assert plan.url == "http://127.0.0.1:5173/"


def test_build_plan_dev_runs_the_python_host_in_front_of_a_vite_child_without_a_build(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(launcher, "_assert_port_free", lambda host, port: None)
    project = _project(tmp_path)

    plan = launcher.build_plan(
        "dev",
        gui_home=project.home,
        environ={
            "HYPRIAL_GUI_BACKEND_DRIVER": "contract-mock",
            "HARNESS_STATE_DIR": str(tmp_path / "state"),
        },
        cwd=project.home,
        inspect=_fake_inspect(project),
    )

    assert plan.mode == "dev"
    assert plan.auto_worktree is None
    assert plan.backend_owned is True
    assert plan.driver == "contract-mock"
    assert plan.backend_endpoint is None
    assert plan.build_argv is None
    assert "HYPRIAL_BACKEND_ENDPOINT" not in plan.env
    assert plan.frontend_argv == (
        sys.executable,
        "-P",
        "-m",
        "hyprial.shell.impl.gui.host",
        "serve",
        "--host",
        "127.0.0.1",
        "--port",
        "4173",
        "--dev-tools",
    )
    assert plan.assets_argv == (
        "/fake/npm",
        "run",
        "dev",
        "--",
        "--host",
        "127.0.0.1",
        "--port",
        "0",
        "--strict-port",
    )


@pytest.mark.parametrize("action", ["start", "status", "stop", "upgrade", "dashboard"])
def test_retired_cli_verbs_are_rejected(action: str) -> None:
    with pytest.raises(GuiError) as caught:
        launcher.parse_action(action)
    assert caught.value.code == "INVALID_ARGUMENT"


@pytest.mark.parametrize("value", ["example.com", "0.0.0.0/path", "not-an-ip"])
def test_ip_validation_rejects_hostnames(value: str) -> None:
    with pytest.raises(GuiError) as caught:
        launcher.parse_host(value)
    assert caught.value.code == "INVALID_ARGUMENT"


def test_host_and_port_validation() -> None:
    assert launcher.parse_host("localhost") == "127.0.0.1"
    assert launcher.parse_host("::1") == "::1"
    assert launcher.parse_port(1) == 1
    assert launcher.parse_port(65535) == 65535
    assert launcher.parse_port(0) == 0
    for value in (65536, -1, True):
        with pytest.raises(GuiError) as caught:
            launcher.parse_port(value)
        assert caught.value.code == "INVALID_ARGUMENT"


def test_build_plan_with_port_zero_probes_nothing_and_passes_port_zero_to_the_host(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        launcher,
        "_assert_port_free",
        lambda host, port: pytest.fail("port 0 must not be probed"),
    )
    project = _project(tmp_path)
    plan = launcher.build_plan(
        "",
        port=0,
        environ={
            "PATH": "/usr/bin",
            "HYPRIAL_GUI_BACKEND_DRIVER": "contract-mock",
            "HARNESS_STATE_DIR": str(tmp_path / "state"),
        },
        cwd=project.home,
        inspect=_fake_inspect(project),
    )
    assert plan.port == 0
    assert plan.frontend_argv[-2:] == ("--port", "0")


def test_gui_home_detection_ignores_hyprial_gui_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    detected = tmp_path / "checkout" / "gui"
    detected.mkdir(parents=True)
    (detected / "package.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("HYPRIAL_GUI_HOME", str(tmp_path / "from-env"))

    assert launcher.resolve_gui_home(cwd=detected.parent) == detected.resolve()


def test_build_plan_normal_source_rejects_gui_home_option_before_inspection(
    tmp_path: Path,
) -> None:
    project = _project(tmp_path)

    def unreachable(*args, **kwargs):
        raise AssertionError("rejected option reached inspection or admission")

    with pytest.raises(launcher.GuiError) as caught:
        launcher.build_plan(
            "",
            gui_home=project.home,
            environ={
                "PATH": "/usr/bin",
                "HYPRIAL_GUI_BACKEND_DRIVER": "contract-mock",
                "HARNESS_STATE_DIR": str(tmp_path / "state"),
            },
            cwd=project.home,
            inspect=unreachable,
            admit=unreachable,
        )

    assert caught.value.code == "INVALID_ARGUMENT"


def test_inspect_project_reads_git_metadata_and_never_writes(
    tmp_path: Path,
) -> None:
    home = tmp_path / "gui"
    home.mkdir()
    package = {
        "name": "@hyprial/gui-app",
        "engines": {"node": ">=24"},
        "scripts": {"start": "node server.mjs", "dev": "vite", "stub": "node stub.mjs"},
    }
    (home / "package.json").write_text(json.dumps(package), encoding="utf-8")
    dirty = home / "src" / "App.tsx"
    dirty.parent.mkdir()
    dirty.write_text("// uncommitted work\n", encoding="utf-8")
    before = dirty.read_text(encoding="utf-8")
    runner = FakeRunner(worktree=tmp_path)

    project = launcher.inspect_project(
        home,
        which=_which({"git": "/fake/git", "node": "/fake/node", "npm": "/fake/npm"}),
        run=runner,
    )

    assert project.worktree == tmp_path
    assert project.branch == "work/gui-test"
    assert project.scripts["stub"] == "node stub.mjs"
    assert dirty.read_text(encoding="utf-8") == before
    assert all(call[1:3] != ["-C", str(home)] or "rev-parse" in call for call in runner.calls)
    flattened = " ".join(" ".join(call) for call in runner.calls)
    assert " checkout " not in f" {flattened} "
    assert " reset " not in f" {flattened} "
    assert " clean " not in f" {flattened} "


def test_inspect_project_requires_git_worktree(tmp_path: Path) -> None:
    home = tmp_path / "gui"
    home.mkdir()
    (home / "package.json").write_text('{"scripts": {}}', encoding="utf-8")
    with pytest.raises(GuiError) as caught:
        launcher.inspect_project(
            home,
            which=_which({"git": "/fake/git", "node": "/fake/node", "npm": "/fake/npm"}),
            run=FakeRunner(worktree=None),
        )
    assert caught.value.code == "GUI_NOT_GIT_WORKTREE"


def test_inspect_project_requires_node_and_npm(tmp_path: Path) -> None:
    home = tmp_path / "gui"
    home.mkdir()
    (home / "package.json").write_text('{"scripts": {}}', encoding="utf-8")
    runner = FakeRunner(worktree=tmp_path)
    for missing in ("node", "npm"):
        paths = {"git": "/fake/git", "node": "/fake/node", "npm": "/fake/npm"}
        paths.pop(missing)
        with pytest.raises(GuiError) as caught:
            launcher.inspect_project(home, which=_which(paths), run=runner)
        assert caught.value.code == f"GUI_{missing.upper()}_MISSING"


def test_inspect_project_enforces_node_engine(tmp_path: Path) -> None:
    home = tmp_path / "gui"
    home.mkdir()
    (home / "package.json").write_text(
        json.dumps({"engines": {"node": ">=99"}, "scripts": {}}), encoding="utf-8"
    )
    with pytest.raises(GuiError) as caught:
        launcher.inspect_project(
            home,
            which=_which({"git": "/fake/git", "node": "/fake/node", "npm": "/fake/npm"}),
            run=FakeRunner(worktree=tmp_path),
        )
    assert caught.value.code == "GUI_NODE_VERSION"


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://example.com:4321",
        "http://user:pass@127.0.0.1:4321",
        "http://127.0.0.1:4321/api",
        "http://127.0.0.1:4321/?x=1",
    ],
)
def test_legacy_backend_endpoint_is_rejected(endpoint: str, monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(launcher, "_assert_port_free", lambda host, port: None)
    project = _project(tmp_path)
    with pytest.raises(GuiError) as caught:
        launcher.build_plan(
            "",
            environ={"HYPRIAL_BACKEND_ENDPOINT": endpoint},
            cwd=project.home,
            inspect=_fake_inspect(project),
        )
    assert caught.value.code == "GUI_CONFIG_CONFLICT"


def test_frontend_port_can_be_reused_after_connection_close() -> None:
    if launcher.os.name == "nt":
        pytest.skip("POSIX socket reuse semantics")
    with socket.socket() as server:
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        port = server.getsockname()[1]
        with socket.create_connection(("127.0.0.1", port), timeout=2) as client:
            connection, _ = server.accept()
            connection.close()
            assert client.recv(1) == b""
    launcher._assert_port_free("127.0.0.1", port)


def test_frontend_port_conflict_is_explicit(tmp_path: Path) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        if os.name != "nt":
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        port = sock.getsockname()[1]
        with pytest.raises(GuiError) as caught:
            launcher._assert_port_free("127.0.0.1", port)
    assert caught.value.code == "GUI_PORT_IN_USE"
    assert str(port) in str(caught.value)


def test_the_normal_gui_needs_a_build_script_and_dev_needs_a_dev_script_but_no_start_script(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setattr(launcher, "_assert_port_free", lambda host, port: None)
    project = _project(tmp_path, scripts={"dev": "vite"})
    with pytest.raises(GuiError) as caught:
        launcher.build_plan(
            "",
            environ={
                "HYPRIAL_GUI_BACKEND_DRIVER": "contract-mock",
                "HARNESS_STATE_DIR": str(tmp_path / "state"),
            },
            cwd=project.home,
            inspect=_fake_inspect(project),
        )
    assert caught.value.code == "GUI_SCRIPT_MISSING"
    assert caught.value.data["script"] == "build"

    dev_project = _project(
        tmp_path, scripts={"build": "vite build"}
    )
    with pytest.raises(GuiError) as caught:
        launcher.build_plan(
            "dev",
            gui_home=dev_project.home,
            environ={
                "HYPRIAL_GUI_BACKEND_DRIVER": "contract-mock",
                "HARNESS_STATE_DIR": str(tmp_path / "state"),
            },
            cwd=dev_project.home,
            inspect=_fake_inspect(dev_project),
        )
    assert caught.value.code == "GUI_SCRIPT_MISSING"
    assert caught.value.data["script"] == "dev"

    accepted = _project(tmp_path, scripts={"build": "vite build", "dev": "vite"})
    plan = launcher.build_plan(
        "",
        environ={
            "HYPRIAL_GUI_BACKEND_DRIVER": "contract-mock",
            "HARNESS_STATE_DIR": str(tmp_path / "state"),
        },
        cwd=accepted.home,
        inspect=_fake_inspect(accepted),
    )
    assert "start" not in accepted.scripts and plan.build_argv == ("/fake/npm", "run", "build")


def test_run_build_routes_output_away_from_json_stdout() -> None:
    project = Path("/tmp/does-not-matter")
    plan = launcher.GuiLaunchPlan(
        mode="normal",
        project=launcher.GuiProject(
            home=project,
            worktree=project,
            branch="x",
            node="/fake/node",
            npm="/fake/npm",
            node_version="v24",
            npm_version="11",
            scripts={"build": "vite build"},
        ),
        host="127.0.0.1",
        port=4173,
        url="http://127.0.0.1:4173/",
        backend_endpoint=None,
        backend_owned=False,
        frontend_argv=("/fake/npm", "start"),
        env={},
        open_browser=False,
        build_argv=("/fake/npm", "run", "build"),
    )
    seen: dict = {}

    def build(argv, **kwargs):
        seen.update(kwargs)
        return subprocess.CompletedProcess(argv, 0)

    launcher._run_build(plan, json_output=True, runner=build)

    assert seen["stdout"] is sys.stderr
    assert seen["stderr"] is subprocess.STDOUT
    assert seen["stdin"] is subprocess.DEVNULL


def test_session_wait_reports_backend_exit_and_frontend_exit() -> None:
    project = Path("/tmp/does-not-matter")
    plan = launcher.GuiLaunchPlan(
        mode="normal",
        project=launcher.GuiProject(
            home=project,
            worktree=project,
            branch="x",
            node="/fake/node",
            npm="/fake/npm",
            node_version="v24",
            npm_version="11",
            scripts={},
        ),
        host="127.0.0.1",
        port=4173,
        url="http://127.0.0.1:4173/",
        backend_endpoint=None,
        backend_owned=True,
        frontend_argv=("npm", "start"),
        env={},
        open_browser=False,
    )
    frontend = FakeProcess(("npm", "start"), pid=301, returncode=0)
    session = launcher.GuiSession(
        plan=plan,
        frontend=launcher.GuiChild("frontend", frontend, None),
        backend=None,
    )
    assert session.wait(sleep=lambda seconds: None) == 0

    backend = FakeProcess(("npm", "run", "stub"), pid=302, returncode=2)
    session = launcher.GuiSession(
        plan=plan,
        frontend=launcher.GuiChild("frontend", FakeProcess(("npm", "start"), pid=303), None),
        backend=launcher.GuiChild("backend", backend, None),
    )
    with pytest.raises(GuiError) as caught:
        session.wait(sleep=lambda seconds: None)
    assert caught.value.code == "GUI_BACKEND_EXITED"


@pytest.mark.skipif(os.name == "nt", reason="POSIX process-group cleanup assertion")
def test_terminate_child_kills_only_its_process_group() -> None:
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        start_new_session=True,
    )
    unrelated = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        start_new_session=True,
    )
    try:
        group = os.getpgid(child.pid)
        launcher._terminate_child(
            launcher.GuiChild("frontend", child, group)
        )
        assert child.poll() is not None
        assert unrelated.poll() is None
    finally:
        for process in (child, unrelated):
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)


def test_cli_help_exposes_only_the_minimal_entry() -> None:
    result = CliRunner().invoke(_cli_root_app, ["gui", "--help"])
    assert result.exit_code == 0, result.output
    assert "dev" in result.output
    assert "--ip" in result.output
    assert "--port" in result.output
    assert "--gui-home" in result.output
    assert "--no-open" in result.output
    assert "status" not in result.output
    assert "upgrade" not in result.output


def test_cli_rejects_retired_verb_with_a_stable_error(tmp_path: Path) -> None:
    result = CliRunner().invoke(
        _cli_root_app,
        ["gui", "start", "--json", "--gui-home", str(tmp_path)],
    )
    assert result.exit_code == 1, result.output
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert payload["code"] == "INVALID_ARGUMENT"


@pytest.mark.parametrize("argv", [["gui"], ["gui", "dev"]])
def test_cli_startup_emits_one_json_record_and_exits_with_the_frontend(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, argv: list[str]
) -> None:
    project = _project(tmp_path)
    plan = replace(
        _fake_plan(project),
        driver="contract-mock",
        admission=_mock_admission(tmp_path),
    )
    session = launcher.GuiSession(
        plan=plan,
        frontend=launcher.GuiChild(
            "frontend", FakeProcess(("npm", "start"), pid=401, returncode=0), None
        ),
        backend=None,
    )
    calls: list[tuple[str, dict]] = []

    def fake_build(action, **kwargs):
        calls.append((action, kwargs))
        if action == "dev":
            return replace(plan, mode="dev", frontend_argv=("npm", "run", "dev"))
        return plan

    monkeypatch.setattr(launcher, "build_plan", fake_build)
    monkeypatch.setattr(launcher, "start_session", lambda plan, **kwargs: session)
    monkeypatch.setattr(launcher, "_terminate_child", lambda child: None)

    result = CliRunner().invoke(_cli_root_app, [*argv, "--json", "--port", "5173"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["url"] == "http://127.0.0.1:4173/"
    assert payload["business"].startswith("Source GUI: contract-mock")
    assert payload["backend_principal"] == "user:contract-mock"
    assert calls[0][1]["port"] == 5173
    assert calls[0][1]["no_open"] is False
    assert calls[0][0] == ("" if argv == ["gui"] else "dev")


def test_gui_json_startup_is_flushed_before_the_foreground_wait() -> None:
    """The record must cross a real pipe before the session ends.

    A redirected stdout is block-buffered.  The child process keeps waiting
    after printing, so this regresses the missing flush rather than trusting
    an in-process capture that never buffers.
    """
    worktree = Path(__file__).resolve().parents[4]
    script = textwrap.dedent(
        """
        import time
        from types import SimpleNamespace
        from hyprial.shell.impl.cli.commands.gui import commands
        from hyprial.shell.impl.gui import launcher

        class Session:
            def wait(self):
                time.sleep(60)
                return 0

            def stop(self):
                pass

        commands.get_services = lambda: SimpleNamespace(
            runtime=SimpleNamespace(
                execute=lambda operation, **kwargs: operation()
            )
        )
        launcher.build_plan = lambda *args, **kwargs: object()
        launcher.start_session = lambda *args, **kwargs: Session()
        launcher.startup_payload = lambda session: {
            "ok": True,
            "mode": "normal",
            "business": "not implemented: hyprial backend not wired",
        }
        commands.gui_command(
            action="",
            ip="127.0.0.1",
            port=4173,
            no_open=True,
            gui_home=None,
            stdin_stop=False,
            json_output=True,
        )
        """
    )
    process = subprocess.Popen(
        [sys.executable, "-c", script],
        cwd=str(worktree),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    first_line: queue.Queue[str] = queue.Queue()

    def read_first_line() -> None:
        first_line.put(process.stdout.readline() if process.stdout else "")

    threading.Thread(target=read_first_line, daemon=True).start()
    try:
        try:
            line = first_line.get(timeout=5)
        except queue.Empty:
            line = ""
    finally:
        process.kill()
        process.wait(timeout=5)
        stderr = process.stderr.read() if process.stderr else ""
    assert line, f"no flushed startup record; stderr={stderr}"
    assert json.loads(line)["ok"] is True


def _daemon_admission(tmp_path: Path) -> SourceAdmission:
    return SourceAdmission(
        driver="daemon",
        owner="source-owner",
        node="source-node",
        principal="user:source-owner",
        home=tmp_path / "home",
        state_dir=(tmp_path / "state").resolve(),
        socket_path=(tmp_path / "state" / "daemon.sock").resolve(),
    )


@pytest.mark.parametrize("mode", ["", "dev"])
def test_an_unset_driver_means_the_daemon_for_a_source_checkout_and_the_caller_environment_is_untouched(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, mode: str
) -> None:
    monkeypatch.setattr(launcher, "_assert_port_free", lambda host, port: None)
    project = _project(tmp_path)
    admitted: list[dict[str, str]] = []

    def admit(*, environ, **options):
        admitted.append(dict(environ))
        return _daemon_admission(tmp_path)

    caller = {"PATH": "/usr/bin", "HARNESS_STATE_DIR": str(tmp_path / "state")}
    plan = launcher.build_plan(
        mode,
        gui_home=project.home if mode == "dev" else None,
        environ=caller,
        cwd=project.home,
        inspect=_fake_inspect(project),
        admit=admit,
    )

    assert plan.driver == "daemon"
    assert admitted[0]["HYPRIAL_GUI_BACKEND_DRIVER"] == "daemon"
    assert caller == {"PATH": "/usr/bin", "HARNESS_STATE_DIR": str(tmp_path / "state")}
    assert "HYPRIAL_GUI_BACKEND_DRIVER" not in plan.env


def test_an_explicit_driver_is_never_replaced_by_the_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(launcher, "_assert_port_free", lambda host, port: None)
    project = _project(tmp_path)
    admitted: list[dict[str, str]] = []

    def admit(*, environ, **options):
        admitted.append(dict(environ))
        return _mock_admission(tmp_path)

    launcher.build_plan(
        "",
        environ={"HYPRIAL_GUI_BACKEND_DRIVER": "contract-mock", "HARNESS_STATE_DIR": str(tmp_path / "state")},
        cwd=project.home,
        inspect=_fake_inspect(project),
        admit=admit,
    )

    assert admitted[0]["HYPRIAL_GUI_BACKEND_DRIVER"] == "contract-mock"
