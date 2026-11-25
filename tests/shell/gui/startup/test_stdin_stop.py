"""``--stdin-stop``: EOF or a ``shutdown`` line on stdin stops the GUI like SIGTERM, but exits 0.

Decision record (2026-11-16, PR 6a): the stop is requested by a watcher thread
that signals the main thread, so the existing guard and its ``finally`` reclaim
the children exactly as for SIGTERM; only the exit status differs.  Without the
flag stdin is not read at all.  A non-blocking stdin (an O_NONBLOCK pipe handed
over by a parent) is "no data yet", never EOF: only real EOF or a ``shutdown``
line stops the GUI.  The tests run the real command function in a
subprocess with real child process groups.
Decision: portable-process-cleanup-20261119.  Liveness is ``process_table.alive`` (``kill 0``
plus ``ps`` state), not a ``/proc`` read, so a zombie counts as gone on macOS too.
Decision (2026-11-21): ``stop-noisy-stderr`` waits for both 128 KiB stderr
bursts to drain while the child stays alive before closing ``stdin``, so the
stdin-stop watcher cannot interrupt ``Session.wait()`` mid-write.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import queue
import signal
import subprocess
import sys
import textwrap
import threading
import time

import pytest

from hyprial.kernel import probe_process

POSIX = pytest.mark.skipif(
    os.name == "nt", reason="POSIX external SIGTERM and O_NONBLOCK pipe semantics"
)

_SCRIPT = textwrap.dedent(
    """
    import os
    from pathlib import Path
    import signal
    import subprocess
    import sys
    from types import SimpleNamespace
    import typer

    from hyprial.kernel import spawn_kwargs
    from hyprial.kernel.impl.manysys.signals import termination as manysys_termination
    from hyprial.shell.impl.cli.commands.gui import commands
    from hyprial.shell.impl.gui import launcher

    marker = Path(sys.argv[1])
    mode = sys.argv[2]
    stdin_stop = mode in {"stop", "stop-without-pthread-kill", "stop-noisy-stderr"}
    interrupt_calls: list[int] = []
    if mode == "stop-without-pthread-kill":
        real_interrupt_main = manysys_termination._thread.interrupt_main
        manysys_termination.signal = SimpleNamespace(SIGTERM=signal.SIGTERM)
        manysys_termination._thread = SimpleNamespace(
            interrupt_main=lambda signum: (
                interrupt_calls.append(int(signum)),
                real_interrupt_main(signum),
            )[-1]
        )

    class Session(launcher.GuiSession):
        def wait(self, *, sleep=None):
            if mode == "stop-noisy-stderr":
                sys.stderr.write("w" * 131072 + "\\n")
                sys.stderr.flush()
            return (
                super().wait()
                if sleep is None
                else super().wait(sleep=sleep)
            )

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
            [sys.executable, "-c", "import time; time.sleep(60)"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            **spawn_kwargs(),
        )

    def _group_or_none(pid: int) -> int | None:
        try:
            return os.getpgid(pid)
        except (AttributeError, OSError):
            return None

    def start_session(plan, **kwargs):
        if mode == "stop-noisy-stderr":
            sys.stderr.write("e" * 131072 + "\\n")
            sys.stderr.flush()
        frontend, backend = spawn(), spawn()
        return Session(
            plan=SimpleNamespace(backend_owned=True),
            frontend=launcher.GuiChild("frontend", frontend, _group_or_none(frontend.pid)),
            backend=launcher.GuiChild("backend", backend, _group_or_none(backend.pid)),
        )

    commands.get_services = lambda: Services()
    launcher.build_plan = lambda *args, **kwargs: object()
    launcher.start_session = start_session
    launcher.startup_payload = lambda session: {
        "ok": True,
        "frontend_pid": session.frontend.process.pid,
        "backend_pid": session.backend.process.pid,
    }
    try:
        commands.gui_command(
            action="",
            ip="127.0.0.1",
            port=0,
            no_open=True,
            gui_home=None,
            json_output=True,
            stdin_stop=stdin_stop,
        )
    except typer.Exit as error:
        if mode == "stop-without-pthread-kill":
            assert interrupt_calls == [int(signal.SIGTERM)], interrupt_calls
        raise SystemExit(error.exit_code)
    """
)


def _alive(pid: int) -> bool:
    if os.name != "nt":
        from tests.support.process_table import alive as posix_alive

        return posix_alive(pid)
    try:
        probe_process(pid)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _gone(*pids: int, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not any(_alive(pid) for pid in pids):
            return True
        time.sleep(0.05)
    return False


class _Run:
    def __init__(self, tmp_path: Path, mode: str, nonblocking: bool = False) -> None:
        self.marker = tmp_path / "stopped"
        self.writer = None
        self._stderr_chunks: list[str] = []
        self._stderr_lock = threading.Lock()
        stdin: int = subprocess.PIPE
        if nonblocking:
            read_end, write_end = os.pipe()
            os.set_blocking(read_end, False)
            self.writer = os.fdopen(write_end, "w")
            stdin = read_end
        self.process = subprocess.Popen(
            [sys.executable, "-c", _SCRIPT, str(self.marker), mode],
            cwd=str(Path(__file__).resolve().parents[4]),
            stdin=stdin,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        if nonblocking:
            os.close(stdin)
        self._stderr_thread = threading.Thread(
            target=self._drain_stderr, daemon=True
        )
        self._stderr_thread.start()
        lines: queue.Queue[str] = queue.Queue()
        threading.Thread(
            target=lambda: lines.put(self.process.stdout.readline()), daemon=True
        ).start()
        try:
            line = lines.get(timeout=15)
        except queue.Empty:
            line = ""
        assert line, "no startup record; stderr=" + self._stderr()
        record = json.loads(line)
        self.pids = (int(record["frontend_pid"]), int(record["backend_pid"]))

    def _drain_stderr(self) -> None:
        if self.process.stderr is None:
            return
        for chunk in iter(lambda: self.process.stderr.read(8192), ""):
            with self._stderr_lock:
                self._stderr_chunks.append(chunk)

    def _stderr(self) -> str:
        if self.process.poll() is not None:
            self._stderr_thread.join(timeout=2)
        with self._stderr_lock:
            return "".join(self._stderr_chunks)

    def close(self) -> None:
        if self.writer is not None and not self.writer.closed:
            self.writer.close()
        if self.process.poll() is None:
            self.process.kill()
            self.process.wait(timeout=5)
        for stream in (
            self.process.stdin,
            self.process.stdout,
            self.process.stderr,
        ):
            if stream is not None and not stream.closed:
                stream.close()
        for pid in self.pids:
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/PID", str(pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                    timeout=5,
                )
                continue
            try:
                os.killpg(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass


@pytest.fixture
def run_factory(tmp_path: Path):
    runs: list[_Run] = []

    def make(mode: str, nonblocking: bool = False) -> _Run:
        run = _Run(tmp_path, mode, nonblocking)
        runs.append(run)
        return run

    yield make
    for run in runs:
        run.close()


def test_eof_on_stdin_stops_cleanly_with_exit_zero(run_factory) -> None:
    run = run_factory("stop")
    assert run.process.stdin is not None
    run.process.stdin.close()
    assert run.process.wait(timeout=20) == 0, run._stderr()
    assert run.marker.read_text(encoding="utf-8") == "stopped"
    assert _gone(*run.pids)
    assert "Traceback" not in run._stderr()


def test_eof_without_pthread_kill_stops_cleanly_and_reaps_children(run_factory) -> None:
    """The capability fallback a platform without pthread_kill (Windows) takes."""
    run = run_factory("stop-without-pthread-kill")
    assert run.process.stdin is not None
    run.process.stdin.close()
    assert run.process.wait(timeout=20) == 0, run._stderr()
    assert run.marker.read_text(encoding="utf-8") == "stopped"
    assert _gone(*run.pids)
    assert "Traceback" not in run._stderr()


@pytest.mark.parametrize(
    "payload",
    [b"noise\n\nshutdown\n", b"noise\r\n\r\nshutdown\r\n"],
)
def test_a_shutdown_line_stops_cleanly_while_stdin_stays_open(
    run_factory, payload: bytes
) -> None:
    run = run_factory("stop")
    assert run.process.stdin is not None
    run.process.stdin.buffer.write(payload)
    run.process.stdin.buffer.flush()
    assert run.process.wait(timeout=20) == 0, run._stderr()
    assert run.marker.read_text(encoding="utf-8") == "stopped"
    assert _gone(*run.pids)


def test_heavy_stderr_output_does_not_block_startup_record_or_stdin_stop(
    run_factory,
) -> None:
    run = run_factory("stop-noisy-stderr")
    deadline = time.monotonic() + 10.0
    while len(run._stderr()) < 262144 and time.monotonic() < deadline:
        if run.process.poll() is not None:
            break
        time.sleep(0.02)
    assert len(run._stderr()) >= 262144, (
        f"stderr_len={len(run._stderr())}, rc={run.process.poll()}, "
        f"tail={run._stderr()[-1024:]!r}"
    )
    assert run.process.poll() is None, (
        f"rc={run.process.poll()}, tail={run._stderr()[-1024:]!r}"
    )
    assert run.process.stdin is not None
    run.process.stdin.close()
    assert run.process.wait(timeout=20) == 0, run._stderr()
    assert run.marker.read_text(encoding="utf-8") == "stopped"
    assert _gone(*run.pids)
    assert len(run._stderr()) >= 262144


def test_other_lines_do_not_stop_the_gui(run_factory) -> None:
    run = run_factory("stop")
    assert run.process.stdin is not None
    run.process.stdin.buffer.write(b"hello\r\nshut down\n")
    run.process.stdin.buffer.flush()
    time.sleep(1.0)
    assert run.process.poll() is None
    assert all(_alive(pid) for pid in run.pids)


@POSIX
def test_sigterm_still_exits_143_with_the_flag(run_factory) -> None:
    run = run_factory("stop")
    run.process.send_signal(signal.SIGTERM)
    assert run.process.wait(timeout=20) == 143, run._stderr()
    assert _gone(*run.pids)


def test_without_the_flag_closed_stdin_changes_nothing(run_factory) -> None:
    run = run_factory("ignore")
    assert run.process.stdin is not None
    run.process.stdin.close()
    time.sleep(1.0)
    assert run.process.poll() is None
    assert all(_alive(pid) for pid in run.pids)
    if os.name != "nt":
        run.process.send_signal(signal.SIGTERM)
        assert run.process.wait(timeout=20) == 143, run._stderr()
        assert _gone(*run.pids)


@POSIX
def test_a_nonblocking_stdin_is_not_taken_for_eof(run_factory) -> None:
    run = run_factory("stop", nonblocking=True)
    time.sleep(1.0)
    assert run.process.poll() is None, run._stderr()
    assert all(_alive(pid) for pid in run.pids)
    assert run.writer is not None
    run.writer.write("hello\n")
    run.writer.flush()
    time.sleep(0.5)
    assert run.process.poll() is None
    run.writer.write("shutdown\n")
    run.writer.flush()
    assert run.process.wait(timeout=20) == 0, run._stderr()
    assert run.marker.read_text(encoding="utf-8") == "stopped"
    assert _gone(*run.pids)


@POSIX
def test_a_nonblocking_stdin_still_stops_on_real_eof(run_factory) -> None:
    run = run_factory("stop", nonblocking=True)
    time.sleep(0.5)
    assert run.process.poll() is None, run._stderr()
    assert run.writer is not None
    run.writer.close()
    assert run.process.wait(timeout=20) == 0, run._stderr()
    assert _gone(*run.pids)
