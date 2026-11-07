"""Foreground SIGTERM cleanup for the source-mode GUI command."""

from __future__ import annotations

import json
import os
from pathlib import Path
import queue
import signal
import socket
import subprocess
import sys
import textwrap
import threading
import time

import pytest
from typer.testing import CliRunner

from hyprial.shell.impl.gui import launcher, termination
from hyprial.shell.impl.cli.commands.common.root import app as _cli_root_app
import hyprial.shell.impl.cli.runtime.entry  # noqa: F401  (installs the explicit CLI composition)

POSIX = pytest.mark.skipif(
    os.name == "nt", reason="POSIX SIGTERM and process-group semantics"
)


@POSIX
def test_termination_guard_translates_and_restores_in_a_real_process() -> None:
    worktree = Path(__file__).resolve().parents[4]
    script = textwrap.dedent(
        """
        import os
        import signal
        import time
        from hyprial.shell.impl.gui import termination

        previous = signal.getsignal(signal.SIGTERM)
        try:
            with termination.termination_guard():
                os.kill(os.getpid(), signal.SIGTERM)
                time.sleep(0.05)
                raise AssertionError("SIGTERM did not interrupt the guard")
        except termination.GuiTerminationRequested:
            pass
        assert signal.getsignal(signal.SIGTERM) == previous
        raise SystemExit(termination.SIGTERM_EXIT_CODE)
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=str(worktree),
        capture_output=True,
        text=True,
        timeout=15,
    )

    assert result.returncode == 143, result.stderr
    assert "AssertionError" not in result.stderr
    assert result.stdout == ""


def test_cli_stops_the_session_and_exits_143_on_termination(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Session:
        def __init__(self) -> None:
            self.stopped = False

        def wait(self) -> int:
            raise termination.GuiTerminationRequested()

        def stop(self) -> None:
            self.stopped = True

    session = Session()
    monkeypatch.setattr(launcher, "build_plan", lambda *args, **kwargs: object())
    monkeypatch.setattr(launcher, "start_session", lambda plan, **kwargs: session)
    monkeypatch.setattr(
        launcher, "startup_payload", lambda active: {"ok": True, "mode": "normal"}
    )

    result = CliRunner().invoke(_cli_root_app, ["gui", "--json", "--port", "5173"])

    assert result.exit_code == 143, result.output
    assert session.stopped is True
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert "INTERRUPTED" not in result.output


@POSIX
def test_cli_sigterm_reclaims_recorded_child_process_groups(tmp_path: Path) -> None:
    """A real SIGTERM exits 143 only after real children are reclaimed."""
    worktree = Path(__file__).resolve().parents[4]
    marker = tmp_path / "stopped"
    script = textwrap.dedent(
        """
        import os
        from pathlib import Path
        import subprocess
        import sys
        from types import SimpleNamespace
        import typer

        from hyprial.shell.impl.cli.commands.gui import commands
        from hyprial.shell.impl.gui import launcher

        marker = Path(sys.argv[1])
        child_code = "import time; time.sleep(60)"

        class Session(launcher.GuiSession):
            def stop(self):
                super().stop()
                marker.write_text("stopped", encoding="utf-8")

        class _Runtime:
            @staticmethod
            def execute(operation, **kwargs):
                return operation()

        class Services:
            runtime = _Runtime()

        def spawn():
            return subprocess.Popen(
                [sys.executable, "-c", child_code],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )

        def start_session(plan, **kwargs):
            frontend = spawn()
            backend = spawn()
            return Session(
                plan=SimpleNamespace(backend_owned=True),
                frontend=launcher.GuiChild(
                    "frontend", frontend, os.getpgid(frontend.pid)
                ),
                backend=launcher.GuiChild(
                    "backend", backend, os.getpgid(backend.pid)
                ),
            )

        def startup_payload(session):
            return {
                "ok": True,
                "frontend_pid": session.frontend.process.pid,
                "backend_pid": session.backend.process.pid,
            }

        commands.get_services = lambda: Services()
        launcher.build_plan = lambda *args, **kwargs: object()
        launcher.start_session = start_session
        launcher.startup_payload = startup_payload
        try:
            commands.gui_command(
                action="",
                ip="127.0.0.1",
                port=4173,
                no_open=True,
                gui_home=None,
                json_output=True,
            )
        except typer.Exit as error:
            raise SystemExit(error.exit_code)
        """
    )
    process = subprocess.Popen(
        [sys.executable, "-c", script, str(marker)],
        cwd=str(worktree),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    first_line: queue.Queue[str] = queue.Queue()

    def read_first_line() -> None:
        first_line.put(process.stdout.readline() if process.stdout else "")

    threading.Thread(target=read_first_line, daemon=True).start()
    payload: dict[str, object] | None = None
    try:
        try:
            line = first_line.get(timeout=10)
        except queue.Empty:
            line = ""
        assert line, (
            "no startup record before SIGTERM; stderr="
            + (process.stderr.read() if process.stderr else "")
        )
        payload = json.loads(line)
        frontend_pid = int(payload["frontend_pid"])
        backend_pid = int(payload["backend_pid"])
        process.send_signal(signal.SIGTERM)
        process.wait(timeout=20)
        stderr = process.stderr.read() if process.stderr else ""
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        if payload is not None:
            _kill_leftovers(
                int(payload["frontend_pid"]), int(payload["backend_pid"])
            )

    assert process.returncode == 143, stderr
    assert marker.read_text(encoding="utf-8") == "stopped"
    assert _wait_until_dead(frontend_pid, backend_pid), (
        f"children survived SIGTERM cleanup: frontend={frontend_pid} "
        f"backend={backend_pid}"
    )
    assert "Traceback" not in stderr


def _wait_until_dead(*pids: int, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if all(_is_dead(pid) for pid in pids):
            return True
        time.sleep(0.05)
    return False


def _is_dead(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    return False


def _ports_free(*ports: int, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if all(_port_free(port) for port in ports):
            return True
        time.sleep(0.05)
    return False


def _port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        try:
            sock.connect(("127.0.0.1", port))
        except ConnectionRefusedError:
            return True
        except OSError:
            return False
        return False


def _kill_leftovers(*pids: int) -> None:
    if os.name == "nt":
        return
    for pid in pids:
        if _is_dead(pid):
            continue
        try:
            os.killpg(os.getpgid(pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass


@POSIX
@pytest.mark.parametrize(
    ("signal_number", "expected_code"),
    [(signal.SIGTERM, 143), (signal.SIGINT, 130)],
)
def test_cli_signal_during_blocking_opener_reclaims_children(
    tmp_path: Path, signal_number: signal.Signals, expected_code: int
) -> None:
    """SIGTERM/SIGINT before startup JSON must reclaim the started session.

    The opener is deliberately blocked on the main thread where Python runs
    signal handlers.  The CLI must exit 143/130 and reclaim both child groups
    even though ``start_session`` has not returned to assign ``session`` yet.
    """
    worktree = Path(__file__).resolve().parents[4]
    script = textwrap.dedent(
        """
        import json
        import os
        from pathlib import Path
        import socket
        import subprocess
        import sys
        import time
        import typer

        from hyprial.shell.impl.cli.commands.gui import commands
        from hyprial.shell.impl.gui import launcher

        home = Path(sys.argv[1])
        home.mkdir(parents=True, exist_ok=True)
        frontend_port_file = Path(sys.argv[2])
        backend_port_file = Path(sys.argv[3])
        pids = []
        backend_code = (
            "import json, socket, sys, time\\n"
            "sock = socket.socket()\\n"
            "sock.bind(('127.0.0.1', 0))\\n"
            "sock.listen()\\n"
            "port = sock.getsockname()[1]\\n"
            "open(sys.argv[1], 'w').write(str(port))\\n"
            "print('HYPRIAL_STUB_READY ' + json.dumps("
            "{'service': 'hyprial-gui-stub', 'host': '127.0.0.1', "
            "'port': port, 'endpoint': 'http://127.0.0.1:' + str(port)}), "
            "flush=True)\\n"
            "time.sleep(60)\\n"
        )
        frontend_code = (
            "import socket, sys, time\\n"
            "sock = socket.socket()\\n"
            "sock.bind(('127.0.0.1', 0))\\n"
            "sock.listen()\\n"
            "open(sys.argv[1], 'w').write(str(sock.getsockname()[1]))\\n"
            "time.sleep(60)\\n"
        )

        def spawn(argv, **kwargs):
            if kwargs.get("stdout") is not subprocess.PIPE:
                kwargs["stdout"] = subprocess.DEVNULL
            kwargs["stderr"] = subprocess.DEVNULL
            process = subprocess.Popen(argv, **kwargs)
            pids.append(process.pid)
            return process

        real_start_session = launcher.start_session

        def read_port(path):
            deadline = time.monotonic() + 5
            while not path.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            return int(path.read_text()) if path.exists() else None

        def blocking_opener(_url):
            print(
                json.dumps(
                    {
                        "pids": pids,
                        "frontend_port": read_port(frontend_port_file),
                        "backend_port": read_port(backend_port_file),
                    }
                ),
                flush=True,
            )
            time.sleep(60)

        def start_session(plan, **kwargs):
            return real_start_session(
                plan, popen=spawn, opener=blocking_opener, **kwargs
            )

        class _Runtime:
            @staticmethod
            def execute(operation, **kwargs):
                return operation()

        class Services:
            runtime = _Runtime()

        commands.get_services = lambda: Services()
        project = launcher.GuiProject(
            home=home,
            worktree=home,
            branch="test",
            node="/fake/node",
            npm="/fake/npm",
            node_version="v24",
            npm_version="11",
            scripts={},
        )
        plan = launcher.GuiLaunchPlan(
            mode="normal",
            project=project,
            host="127.0.0.1",
            port=4173,
            url="http://127.0.0.1:4173/",
            backend_endpoint=None,
            backend_owned=True,
            backend_argv=(
                sys.executable,
                "-c",
                backend_code,
                str(backend_port_file),
            ),
            frontend_argv=(
                sys.executable,
                "-c",
                frontend_code,
                str(frontend_port_file),
            ),
            env=dict(os.environ),
            open_browser=True,
            build_argv=None,
        )
        launcher.build_plan = lambda *args, **kwargs: plan
        launcher.start_session = start_session
        launcher._wait_for_http = lambda *args, **kwargs: None
        try:
            commands.gui_command(
                action="",
                ip="127.0.0.1",
                port=4173,
                no_open=False,
                gui_home=None,
                json_output=True,
            )
        except typer.Exit as error:
            raise SystemExit(error.exit_code)
        """
    )
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            script,
            str(tmp_path),
            str(tmp_path / "frontend.port"),
            str(tmp_path / "backend.port"),
        ],
        cwd=str(worktree),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    first_line: queue.Queue[str] = queue.Queue()

    def read_first_line() -> None:
        first_line.put(process.stdout.readline() if process.stdout else "")

    threading.Thread(target=read_first_line, daemon=True).start()
    pids: list[int] = []
    ports: list[int] = []
    stderr = ""
    children_dead = False
    ports_free = False
    try:
        try:
            line = first_line.get(timeout=10)
        except queue.Empty:
            line = ""
        assert line, (
            "no blocking-opener marker before SIGTERM; stderr="
            + (process.stderr.read() if process.stderr else "")
        )
        marker = json.loads(line)
        assert "pids" in marker, (
            f"expected opener marker before startup JSON, got {line!r}"
        )
        pids = [int(pid) for pid in marker["pids"]]
        assert len(pids) == 2, marker
        ports = [int(marker["frontend_port"]), int(marker["backend_port"])]
        assert all(port > 0 for port in ports), marker
        process.send_signal(signal_number)
        process.wait(timeout=20)
        stderr = process.stderr.read() if process.stderr else ""
        children_dead = _wait_until_dead(*pids)
        ports_free = _ports_free(*ports)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        if not children_dead:
            _kill_leftovers(*pids)
        if process.stdout is not None:
            process.stdout.close()
        if process.stderr is not None:
            process.stderr.close()

    assert process.returncode == expected_code, stderr
    assert children_dead, (
        f"children survived blocking-opener {signal_number.name}: "
        f"pids={pids} ports={ports}"
    )
    assert ports_free, (
        f"child ports survived blocking-opener {signal_number.name}: "
        f"pids={pids} ports={ports}"
    )
    assert "Traceback" not in stderr


@POSIX
@pytest.mark.parametrize(
    ("signal_number", "expected_code"),
    [(signal.SIGTERM, 143), (signal.SIGINT, 130)],
)
def test_cli_signal_before_startup_json_exits_143_or_130(
    signal_number: signal.Signals, expected_code: int
) -> None:
    """Discovery/auto-worktree startup is covered by the termination guard."""
    worktree = Path(__file__).resolve().parents[4]
    script = textwrap.dedent(
        """
        import time
        import typer

        from hyprial.shell.impl.cli.commands.gui import commands
        from hyprial.shell.impl.gui import launcher

        class _Runtime:
            @staticmethod
            def execute(operation, **kwargs):
                return operation()

        class Services:
            runtime = _Runtime()

        def blocking_build_plan(*args, **kwargs):
            print("PRE_STARTUP_BUILD_PLAN", flush=True)
            time.sleep(60)
            return object()

        commands.get_services = lambda: Services()
        launcher.build_plan = blocking_build_plan
        try:
            commands.gui_command(
                action="dev",
                ip="127.0.0.1",
                port=4173,
                no_open=True,
                gui_home=None,
                json_output=True,
            )
        except typer.Exit as error:
            raise SystemExit(error.exit_code)
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
    stderr = ""
    try:
        try:
            line = first_line.get(timeout=10)
        except queue.Empty:
            line = ""
        assert line.strip() == "PRE_STARTUP_BUILD_PLAN", (
            "no pre-startup marker before signal; stderr="
            + (process.stderr.read() if process.stderr else "")
        )
        process.send_signal(signal_number)
        process.wait(timeout=20)
        stderr = process.stderr.read() if process.stderr else ""
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        if process.stdout is not None:
            process.stdout.close()
        if process.stderr is not None:
            process.stderr.close()

    assert process.returncode == expected_code, stderr
    assert "Traceback" not in stderr
