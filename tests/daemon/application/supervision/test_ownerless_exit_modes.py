"""The ownerless loop's exit mode: own the exit unless the run stops for logout.

Decision record (2026-11-16): ``daemon run`` on an ownerless home must return
to the bootstrap loop after a pure logout, so it cannot claim unconditional
ownership of the process exit.  It also cannot give the exit up entirely: a
non-daemon thread left behind by a dependency keeps an interpreter alive
forever once the loop returns.  ``exit_unless_logout`` therefore owns the exit
for every stop except a pure logout, keeps the close-hang budget for logout
too (exiting beats hanging; the forced exit is the same ``os._exit`` a final
stop uses), and releases the timers once a logout has returned.  The tests run
the real ``run`` finally-block in a child interpreter, since the property under
test is what happens to the process.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
import threading

import pytest

from hyprial.daemon.impl.application.onboarding.lifecycle.stop_reason import (
    STOP_LOGOUT,
    StopFlags,
)
from hyprial.daemon.impl.bootstrap.exit_independence import (
    LOGOUT_STOP_REASON,
    exit_guards_wanted,
    process_exit_owned,
    release_exit_guards_for_logout,
)

_EXIT_CODE_STUCK = 75


def _program(*, source: str, wedge: bool, close: str, tail: str) -> str:
    return textwrap.dedent(
        f"""
        import os, queue, sys, threading, time
        from types import SimpleNamespace
        import hyprial.daemon.impl.application.startup as startup
        import hyprial.daemon.impl.application.shutdown as shutdown
        from hyprial.daemon.impl.application.startup import _StartupMixin
        from hyprial.daemon.impl.application.shutdown import _ShutdownMixin

        startup.TEARDOWN_BUDGETED_SECONDS = 1.0
        shutdown._EXIT_BACKSTOP_SECONDS = 1.5
        if {wedge!r}:
            threading.Thread(target=queue.Queue().get, name="unjoinable").start()

        class Stub(_StartupMixin, _ShutdownMixin):
            _owns_process_exit = False
            _exit_unless_logout = True
            _lock_stream = None
            _home_guard = SimpleNamespace(claim=lambda: None, close=lambda _t: True)
            node_id = "exit-modes"
            _exit_process = staticmethod(os._exit)
            state_dir = None
            _harnesses = None
            _agent_session_domains = None
            def _acquire_lock(self):
                raise RuntimeError("synthetic")
            def _log(self, *a, **k): pass
            def _log_trace(self, *a, **k): pass
            def _close(self):
                {close}

        stub = Stub()
        stub.stop_policy.note({source!r})
        try:
            stub.run()
        except BaseException as error:
            print("RUN_RAISED", type(error).__name__, flush=True)
        print("RETURNED", flush=True)
        {tail}
        """
    )


def _run(program: str, timeout: float) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(
            [sys.executable, "-c", program],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return None


def test_final_stop_with_a_lingering_non_daemon_thread_leaves_within_budget() -> None:
    result = _run(
        _program(source="signal", wedge=True, close="pass", tail="pass"), timeout=20
    )

    assert result is not None, "a final stop left the process hanging on a thread"
    assert result.returncode == 1, result.stderr[-600:]
    assert "RETURNED" not in result.stdout


def test_the_same_run_without_the_mode_hangs_on_that_thread() -> None:
    program = _program(source="signal", wedge=True, close="pass", tail="pass").replace(
        "_exit_unless_logout = True", "_exit_unless_logout = False"
    )

    assert _run(program, timeout=6) is None, (
        "the injected thread did not wedge the process, so the mode test above "
        "would pass without the mode"
    )


def test_a_pure_logout_returns_to_the_caller_and_releases_the_guards() -> None:
    tail = (
        "time.sleep(2.5); "
        "print('ALIVE', stub._exit_backstop_cancel.is_set(), flush=True)"
    )
    result = _run(
        _program(source="auth-logout", wedge=False, close="pass", tail=tail), timeout=20
    )

    assert result is not None
    assert result.returncode == 0, result.stderr[-600:]
    assert "RETURNED" in result.stdout
    assert "ALIVE True" in result.stdout, (
        "the exit backstop stayed armed after a logout returned to the loop"
    )
    assert "Thread 0x" not in result.stderr, "the stall dump fired after a logout"


def test_a_logout_whose_teardown_hangs_is_forced_out_like_a_final_stop() -> None:
    result = _run(
        _program(
            source="auth-logout",
            wedge=False,
            close="threading.Event().wait()",
            tail="pass",
        ),
        timeout=20,
    )

    assert result is not None, "a hung logout teardown was not bounded"
    assert result.returncode == _EXIT_CODE_STUCK, result.stderr[-600:]
    assert "RETURNED" not in result.stdout


def test_logout_literal_matches_the_stop_reason_the_loop_compares() -> None:
    assert LOGOUT_STOP_REASON == STOP_LOGOUT


class _App:
    def __init__(self, *, owns: bool, unless: bool, source: str | None) -> None:
        self._owns_process_exit = owns
        self._exit_unless_logout = unless
        self.flags = StopFlags()
        if source is not None:
            self.flags.note(source)

    @property
    def stop_reason(self) -> str:
        return self.flags.reason


@pytest.mark.parametrize(
    ("owns", "unless", "source", "owned", "guards"),
    [
        (False, False, "signal", False, False),
        (True, False, "auth-logout", True, True),
        (False, True, "auth-logout", False, True),
        (False, True, "signal", True, True),
        (False, True, None, True, True),
    ],
)
def test_exit_ownership_truth_table(
    owns: bool, unless: bool, source: str | None, owned: bool, guards: bool
) -> None:
    app = _App(owns=owns, unless=unless, source=source)

    assert process_exit_owned(app) is owned
    assert exit_guards_wanted(app) is guards


def test_a_logout_overridden_by_any_other_stop_owns_the_exit() -> None:
    app = _App(owns=False, unless=True, source="auth-logout")
    app.flags.note("signal")

    assert process_exit_owned(app) is True


def test_a_backstop_that_expired_before_the_logout_release_does_not_exit(monkeypatch) -> None:
    import hyprial.daemon.impl.application as application_module
    import hyprial.daemon.impl.application.shutdown as shutdown_module

    exits: list[int] = []
    monkeypatch.setattr(shutdown_module.os, "_exit", exits.append)
    monkeypatch.setattr(shutdown_module, "dump_live_threads", lambda **_kwargs: None)

    class Stub:
        _exit_unless_logout = True
        stop_reason = LOGOUT_STOP_REASON
        state_dir = None

        def _log_trace(self, level, event, **fields) -> None:
            del level, event, fields

    stub = Stub()
    real = threading.Lock()

    class ReleaseBeforeTheCommitCheck:
        def __init__(self) -> None:
            self.pending = True

        def __enter__(self):
            if self.pending and threading.current_thread().name == "daemon-exit-backstop":
                self.pending = False
                release_exit_guards_for_logout(stub)
            real.acquire()
            return self

        def __exit__(self, *_exc) -> None:
            real.release()

    stub._exit_backstop_lock = ReleaseBeforeTheCommitCheck()
    started: list[threading.Thread] = []

    class RecordingThread(threading.Thread):
        def start(self) -> None:
            started.append(self)
            super().start()

    monkeypatch.setattr(threading, "Thread", RecordingThread)

    application_module.DaemonApplication._arm_exit_backstop(stub, 0)
    (backstop,) = started
    backstop.join(timeout=30)

    assert not backstop.is_alive()
    assert stub._exit_backstop_cancel.is_set()
    assert exits == [], "an expired backstop exited after the logout had released it"
