"""A stop request that arrives during cleanup is not acted on; no pthread_kill is not a refusal.

Decision record (2026-11-16, PR 6a fix round): the command seals the termination
guard first in its ``finally``, so a stdin stop or a SIGTERM that arrives while
``session.stop()`` runs can neither replace the first outcome (exit status 1 or
the ``GUI_BACKEND_EXITED`` failure) nor cut a child's terminate/escalation short.
The timing is injected from inside ``stop()``: the stop request is produced
there and the watcher thread is joined, so nothing depends on sleeps.  The
capability tests remove ``signal.pthread_kill`` the way a platform without it
(Windows) would look; they are Linux tests of the capability fallback
(Decision stdin-stop-portable-20261121: ``_thread.interrupt_main``), not of
any platform.
"""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import textwrap

import pytest

POSIX = pytest.mark.skipif(os.name == "nt", reason="POSIX external SIGTERM")

REPOSITORY = Path(__file__).resolve().parents[3].parent

_CLEANUP = textwrap.dedent(
    """
    import os
    import signal
    import sys
    import threading
    from pathlib import Path
    import typer

    from hyprial.shell.impl.cli.commands.gui import commands
    from hyprial.shell.impl.gui import launcher, termination
    from hyprial.shell.impl.gui.errors import GuiError

    marker = Path(sys.argv[1])
    source, outcome = sys.argv[2], sys.argv[3]
    read_fd, write_fd = os.pipe()
    termination._stdin_descriptor = lambda: read_fd

    class Session:
        def wait(self):
            if outcome == "raise":
                raise GuiError("GUI_BACKEND_EXITED", "backend exited")
            return 1

        def stop(self):
            if source in {"stdin", "stdin-crlf", "stdin-eof"}:
                if source == "stdin":
                    os.write(write_fd, b"shutdown\\n")
                elif source == "stdin-crlf":
                    os.write(write_fd, b"shutdown\\r\\n")
                else:
                    os.close(write_fd)
                for thread in threading.enumerate():
                    if thread.name == "gui-stdin-stop":
                        thread.join(timeout=10)
            else:
                os.kill(os.getpid(), signal.SIGTERM)
            marker.write_text("cleanup-complete", encoding="utf-8")

    class Runtime:
        @staticmethod
        def execute(operation, **kwargs):
            return operation()

    class Services:
        runtime = Runtime()

    commands.get_services = lambda: Services()
    launcher.build_plan = lambda *args, **kwargs: object()
    launcher.start_session = lambda plan, **kwargs: Session()
    launcher.startup_payload = lambda session: {"ok": True}
    try:
        commands.gui_command(
            action="", ip="127.0.0.1", port=0, no_open=True, gui_home=None,
            json_output=True, stdin_stop=True,
        )
    except typer.Exit as error:
        raise SystemExit(error.exit_code)
    except GuiError as error:
        print("GuiError " + error.code)
        raise SystemExit(9)
    finally:
        for fd in (read_fd, write_fd):
            try:
                os.close(fd)
            except OSError:
                pass
    """
)


def _run(script: str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", script, *args],
        cwd=str(REPOSITORY),
        capture_output=True,
        text=True,
        timeout=60,
        stdin=subprocess.DEVNULL,
    )


@pytest.mark.parametrize(
    "source",
    ["stdin", "stdin-crlf", "stdin-eof", pytest.param("sigterm", marks=POSIX)],
)
def test_a_stop_during_cleanup_keeps_the_first_exit_status(source: str, tmp_path: Path) -> None:
    marker = tmp_path / "marker"
    result = _run(_CLEANUP, str(marker), source, "return")
    assert result.returncode == 1, result.stderr
    assert marker.read_text(encoding="utf-8") == "cleanup-complete"
    assert "Traceback" not in result.stderr


@pytest.mark.parametrize(
    "source",
    ["stdin", "stdin-crlf", "stdin-eof", pytest.param("sigterm", marks=POSIX)],
)
def test_a_stop_during_cleanup_keeps_the_first_failure(source: str, tmp_path: Path) -> None:
    marker = tmp_path / "marker"
    result = _run(_CLEANUP, str(marker), source, "raise")
    assert result.returncode == 9, result.stdout + result.stderr
    assert result.stdout.splitlines()[-1] == "GuiError GUI_BACKEND_EXITED"
    assert marker.read_text(encoding="utf-8") == "cleanup-complete"


_CAPABILITY = textwrap.dedent(
    """
    import os
    import signal
    import sys
    import time
    from types import SimpleNamespace
    from hyprial.kernel.impl.manysys.signals import termination as manysys_termination
    from hyprial.shell.impl.gui import termination

    trigger = sys.argv[1]
    before = signal.getsignal(signal.SIGTERM)
    interrupt_calls: list[int] = []
    real_interrupt_main = manysys_termination._thread.interrupt_main
    manysys_termination.signal = SimpleNamespace(SIGTERM=signal.SIGTERM)
    manysys_termination._thread = SimpleNamespace(
        interrupt_main=lambda signum: (
            interrupt_calls.append(int(signum)),
            real_interrupt_main(signum),
        )[-1]
    )
    read_fd, write_fd = os.pipe()
    termination._stdin_descriptor = lambda: read_fd
    outcome = "no-stop"
    try:
        with termination.termination_guard(stdin_stop=True):
            assert signal.getsignal(signal.SIGTERM) != before
            if trigger == "lf":
                os.write(write_fd, b"shutdown\\n")
            elif trigger == "crlf":
                os.write(write_fd, b"noise\\r\\nshutdown\\r\\n")
            else:
                os.close(write_fd)
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                time.sleep(0.05)
    except termination.GuiTerminationRequested as stop:
        outcome = "clean" if stop.clean else "unclean"
    finally:
        for fd in (read_fd, write_fd):
            try:
                os.close(fd)
            except OSError:
                pass
    assert signal.getsignal(signal.SIGTERM) == before
    assert interrupt_calls == [int(signal.SIGTERM)], interrupt_calls
    print(outcome)
    """
)


@pytest.mark.parametrize("trigger", ["lf", "crlf", "eof"])
def test_without_pthread_kill_a_shutdown_line_still_stops_cleanly(trigger: str) -> None:
    result = _run(_CAPABILITY, trigger)
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip() == "clean"


_CLEANUP_NO_PTHREAD_KILL = _CLEANUP.replace(
    "from hyprial.shell.impl.gui import launcher, termination",
    "from types import SimpleNamespace\n"
    "from hyprial.kernel.impl.manysys.signals import termination as manysys_termination\n"
    "from hyprial.shell.impl.gui import launcher, termination",
).replace(
    "termination._stdin_descriptor = lambda: read_fd",
    "termination._stdin_descriptor = lambda: read_fd\n"
    "manysys_termination.signal = SimpleNamespace(SIGTERM=signal.SIGTERM)",
)


@pytest.mark.parametrize("source", ["stdin", "stdin-crlf", "stdin-eof"])
def test_without_pthread_kill_a_stop_during_cleanup_keeps_the_first_exit_status(
    source: str, tmp_path: Path
) -> None:
    marker = tmp_path / "marker"
    result = _run(_CLEANUP_NO_PTHREAD_KILL, str(marker), source, "return")
    assert result.returncode == 1, result.stderr
    assert marker.read_text(encoding="utf-8") == "cleanup-complete"
    assert "Traceback" not in result.stderr


def test_termination_guard_delegates_stdin_watcher_and_delivery_to_manysys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import signal
    import threading
    from hyprial.shell.impl.gui import termination

    assert not hasattr(termination, "_StdinWatcher")
    assert not hasattr(termination, "_PTHREAD_KILL")

    events: list[tuple[str, object]] = []
    delivered: list[int | None] = []

    class FakeWatcher:
        def __init__(
            self,
            descriptor: int = 0,
            *,
            on_stop=None,
            signum: int | None = None,
            thread_name: str = "gui-stdin-stop",
        ) -> None:
            self.pending = threading.Event()
            self._on_stop = on_stop
            self._signum = signum
            events.append(("init", descriptor, thread_name))

        def start(self) -> None:
            events.append(("start", None))
            self.pending.set()
            if self._on_stop is not None:
                self._on_stop()
            else:
                termination.manysys.deliver_main_termination(self._signum)

        def close(self) -> None:
            events.append(("close", None))

    def fake_deliver(signum: int | None = None) -> None:
        delivered.append(signum)
        signal.raise_signal(signal.SIGTERM)

    monkeypatch.setattr(termination, "_stdin_descriptor", lambda: 42)
    monkeypatch.setattr(termination.manysys, "StdinStopWatcher", FakeWatcher)
    monkeypatch.setattr(
        termination.manysys, "deliver_main_termination", fake_deliver
    )

    with termination.termination_guard(stdin_stop=False) as plain_guard:
        assert plain_guard.sealed is False
    assert events == []
    assert delivered == []

    with pytest.raises(termination.GuiTerminationRequested) as caught:
        with termination.termination_guard(stdin_stop=True):
            pytest.fail("FakeWatcher triggers stop during start()")
    assert caught.value.clean is True
    assert delivered == [int(signal.SIGTERM)]
    assert events == [
        ("init", 42, "gui-stdin-stop"),
        ("start", None),
        ("close", None),
    ]


def test_seal_waits_for_an_in_flight_stdin_delivery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import threading
    from hyprial.shell.impl.gui import termination

    entered = threading.Event()
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    class FakeWatcher:
        def __init__(self, _descriptor: int, *, on_stop, **_kwargs: object) -> None:
            self.pending = threading.Event()
            self._on_stop = on_stop

        def start(self) -> None:
            def run() -> None:
                started.set()
                self._on_stop()

            threading.Thread(target=run).start()
            assert started.wait(1.0)
            assert entered.wait(1.0)

        def close(self) -> None:
            return None

        def join(self, timeout: float | None = None) -> None:
            assert timeout == 1.0

    def deliver(_signum: int) -> None:
        entered.set()
        assert release.wait(1.0)
        finished.set()

    monkeypatch.setattr(termination, "_stdin_descriptor", lambda: 42)
    monkeypatch.setattr(termination.manysys, "StdinStopWatcher", FakeWatcher)
    monkeypatch.setattr(termination.manysys, "deliver_main_termination", deliver)

    with termination.termination_guard(stdin_stop=True) as handle:
        seal_thread = threading.Thread(target=handle.seal)
        seal_thread.start()
        assert not finished.wait(0.05)
        release.set()
        seal_thread.join(timeout=1.0)
        assert finished.is_set()


def test_a_failed_readability_wait_backs_off_through_manysys_without_spinning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace
    from hyprial.kernel import manysys
    from hyprial.kernel.impl.manysys.signals import stdin_stop

    def unavailable(*_args: object) -> tuple[list[int], list[int], list[int]]:
        raise OSError("select is unavailable for this descriptor")

    waited: list[float | None] = []
    monkeypatch.setattr(stdin_stop, "select", SimpleNamespace(select=unavailable))
    watcher = manysys.StdinStopWatcher(0, on_stop=lambda: None)
    try:
        real_wait = watcher._closed_event.wait
        monkeypatch.setattr(
            watcher._closed_event,
            "wait",
            lambda timeout=None: (waited.append(timeout), real_wait(0.0))[-1],
        )
        assert watcher._block_until_input() == "fallback_nonblocking"
        assert waited == [stdin_stop._STDIN_FALLBACK_POLL_SECONDS]
    finally:
        watcher.close()
