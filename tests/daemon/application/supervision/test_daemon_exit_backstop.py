"""A daemon that has finished shutting down must actually leave.

The 2026-08-29 outage was not caused by a failed shutdown.  Shutdown
*succeeded*: the lock was released, the socket removed, `daemon.json` deleted.
The process then stayed alive anyway, parked in `threading._shutdown` joining a
non-daemon thread that was blocked on a `queue.get()` with no sentinel -- and
its whole connector fleet, still holding its open pipes, stayed alive with it.
134 processes outlived their daemon by a day, and stalled every subsequent
start until they were killed by hand.

So the property under test is not "shutdown works".  It is the one nobody was
asserting: **the process is gone afterwards**.

The injection point matters.  Asserting that `_close()` returns would pass
today and would have passed throughout the outage -- `_close()` always
returned.  The defect lives strictly after it, so the test has to inject a
thread that cannot be joined and then look at the *process*, not at any value
the code returns.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any

import pytest

# Long enough that a normal interpreter teardown is never mistaken for a hang,
# short enough to keep the negative control cheap.
_BACKSTOP = 3.0
_EXIT_CODE_STUCK = 75


def _program(
    *, arm: bool, owns: bool = True, wedge: bool = True, state_dir: str | None = None
) -> str:
    """A process wedged exactly the way the production daemon was.

    The thread is non-daemon and parked on an empty queue forever, which is
    what `turn_runtime._run` and the pykka actor loops were doing.  CPython
    will not exit while it exists.

    ``wedge=False`` removes that thread, giving the healthy control: the same
    program, the same arming, a process that simply leaves on time.

    The stub's ``_log_trace`` **prints its keywords** rather than dropping
    them.  It used to drop them, which meant the structured record the backstop
    emits was never once looked at by a test -- the exit code proved the
    process left, and nothing proved it said why.
    """

    return textwrap.dedent(
        f"""
        import queue, threading, sys
        import hyprial.daemon.impl.application as app
        import hyprial.daemon.impl.application.shutdown as shutdown_mod

        # The exit-backstop budget lives with the shutdown cluster since the
        # application split; DaemonApplication itself stays on the package.
        shutdown_mod._EXIT_BACKSTOP_SECONDS = {_BACKSTOP}

        def _parked():
            queue.Queue().get()          # no sentinel is ever sent

        if {wedge!r}:
            threading.Thread(
                target=_parked, name="unjoinable", daemon=False
            ).start()

        class _Stub:
            # The backstop only needs somewhere to report; it must not depend
            # on a fully composed daemon to be able to fire.
            _owns_process_exit = {owns!r}
            state_dir = {state_dir!r}
            def _log_trace(self, level, event, **fields):
                print(f"LOGTRACE {{event}} {{fields}}", file=sys.stderr)

        if {arm!r}:
            app.DaemonApplication._arm_exit_backstop(_Stub())

        # Main thread is done.  From here CPython tries to join `unjoinable`,
        # which is precisely where the production daemon stopped.
        """
    )


def _run(
    *,
    arm: bool,
    timeout: float,
    owns: bool = True,
    wedge: bool = True,
    state_dir: str | None = None,
) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(
            [
                sys.executable,
                "-c",
                _program(arm=arm, owns=owns, wedge=wedge, state_dir=state_dir),
            ],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return None


def test_a_thread_that_cannot_be_joined_does_not_keep_the_process_alive() -> None:
    result = _run(arm=True, timeout=_BACKSTOP + 20.0)

    assert result is not None, "backstop did not fire; the process was still hung"
    assert result.returncode == _EXIT_CODE_STUCK, (
        "expected the distinct forced-exit code so an operator can tell a "
        f"refused join from a failed shutdown, got {result.returncode}"
    )


def test_without_the_backstop_the_same_process_hangs_forever() -> None:
    """The control that gives the test above its meaning.

    Without this, a passing first test proves only that the child process
    exited -- which it would also do if the injected thread were joinable, or
    if the wedge never happened.  This asserts the wedge is real, so the other
    test is measuring the backstop rather than measuring nothing.
    """

    assert _run(arm=False, timeout=_BACKSTOP + 8.0) is None, (
        "the injected thread did not actually wedge the process, so the "
        "companion test would pass whether or not the backstop works"
    )


def test_a_daemon_that_does_not_own_the_process_never_forces_an_exit() -> None:
    """Embedding must stay safe, because embedding is what the tests do.

    A DaemonApplication does not always own its interpreter: the suite starts
    real daemons in-process. Arming unconditionally is not a theoretical
    hazard -- it ended a full pytest run at 28% with this very exit code, so
    the guard gets its own test rather than a comment.
    """

    assert _run(arm=True, owns=False, timeout=_BACKSTOP + 8.0) is None, (
        "an embedded daemon forced the host process to exit"
    )


def test_the_forced_exit_says_which_thread_was_blocking() -> None:
    """Leaving is necessary; it is not sufficient for anyone to fix this.

    Before this, every forced exit produced the same line -- the process did
    not exit -- and none of them said which thread kept it.  A year of those
    logs would not narrow the search by one thread, and the state cannot be
    inspected afterwards because the process is gone by the time anyone looks.

    This one owns the **structured record**: the field an operator greps.  The
    stacks live in a file and are asserted by
    ``test_the_stacks_survive_the_restart_that_the_stall_itself_triggers``.
    Kept apart because they fail separately -- the record can lose its
    inventory while the dump still writes, and the dump can land somewhere
    unreadable while the record still looks complete.  That second case is not
    hypothetical: it is exactly what production did on 2026-08-30.
    """

    result = _run(arm=True, timeout=_BACKSTOP + 20.0)

    assert result is not None, "backstop did not fire; the process was still hung"
    assert result.returncode == _EXIT_CODE_STUCK

    logged = [line for line in result.stderr.splitlines() if "LOGTRACE" in line]
    assert logged and "unjoinable" in logged[0], (
        "daemon.exit.forced carried no thread inventory, so the log says the "
        f"process would not leave without saying what held it: {logged!r}"
    )


def test_the_stacks_survive_the_restart_that_the_stall_itself_triggers(
    tmp_path: Any,
) -> None:
    """⚠️ Where the dump lands decides whether it still exists when read.

    A production daemon's stderr is a **per-launch** capture file:
    `cli._daemon_launch_capture` makes a new inode each start and moves the
    stable name onto it. The stall happens *during a restart*, so the next
    launch replaces that file seconds later. On 2026-08-30 a real forced exit
    wrote one hundred and ninety-one thread stacks; by the time anyone looked,
    the file held fifty-three bytes of the next launch's marker.

    The diagnostic reached the scene, recorded everything, and the record was
    erased by the event that made it necessary -- which is worse than not
    dumping, because the structured log still shows a `threads=` summary and
    everything reads as working.

    So this asserts the stacks are in a file of our own, and that the record
    says which file. Nothing here looks at stderr: passing that would prove
    only that something was written somewhere.
    """

    state_dir = tmp_path / "state"
    state_dir.mkdir()
    result = subprocess.run(
        [sys.executable, "-c", _program(arm=True, state_dir=str(state_dir))],
        capture_output=True,
        text=True,
        timeout=_BACKSTOP + 20.0,
    )

    assert result.returncode == _EXIT_CODE_STUCK, result.stderr[-800:]

    from hyprial.daemon.impl.processes.shutdown_stall import stall_dump_path

    dump = Path(str(stall_dump_path(state_dir)))
    assert dump.exists(), (
        "no stall dump file was written; the stacks went wherever stderr "
        "pointed, which in production is replaced by the next launch"
    )
    text = dump.read_text(encoding="utf-8")
    assert "in _parked" in text, (
        f"the dump file exists but holds no stack for the wedged thread: {text[:400]!r}"
    )
    logged = [line for line in result.stderr.splitlines() if "LOGTRACE" in line]
    assert logged and str(dump) in logged[0], (
        f"daemon.exit.forced did not name the dump file, so an operator has "
        f"nothing to open: {logged!r}"
    )


def test_a_healthy_exit_leaves_no_thread_dump_behind(tmp_path: Any) -> None:
    """The control, and the thing that keeps this diagnostic worth reading.

    Same program, same arming, no wedge.  If evidence were taken when the
    backstop is armed rather than when it fires, this would still pass every
    other test in this file while writing a full stack dump on every clean
    shutdown -- and a diagnostic that fires on every healthy exit is one nobody
    reads by the second week, which is the same as not having it.

    ⚠️ A real state dir is passed rather than left unset. With no dir the dump
    would fail to open and be swallowed, so the mutation this guards against
    would sail through on a technicality: the control has to be somewhere the
    dump *could* succeed, or it is only proving that a broken path stays quiet.
    """

    state_dir = tmp_path / "state"
    state_dir.mkdir()
    result = _run(
        arm=True, wedge=False, timeout=_BACKSTOP + 20.0, state_dir=str(state_dir)
    )

    assert result is not None, "the unwedged process did not exit on its own"
    assert result.returncode == 0, (
        f"a healthy process exited {result.returncode}; the backstop fired "
        "when there was nothing to force"
    )

    from hyprial.daemon.impl.processes.shutdown_stall import stall_dump_path

    assert not Path(str(stall_dump_path(state_dir))).exists(), (
        "a clean shutdown wrote a stall dump; on this build every restart "
        "would leave one, and the file stops being a signal"
    )
    assert "LOGTRACE" not in result.stderr, (
        f"a clean shutdown reported a forced exit: {result.stderr[:400]!r}"
    )


def test_an_embedded_daemon_does_not_arm_a_process_wide_watchdog() -> None:
    """Arming is a process-level act and answers to ownership, same as exiting.

    ⚠️ Observed, not hypothetical: ungated, a full suite armed this timer from
    an in-process ``_close()`` and thirty seconds later dumped seventy-nine
    thread stacks into an unrelated end-to-end test that was waiting on a
    thirty-second subprocess budget.  The diagnostic became the disturbance.
    """

    import hyprial.daemon.impl.application as app
    import hyprial.daemon.impl.application.startup as startup_mod

    armed: list[str] = []

    class _Embedded:
        _owns_process_exit = False

    class _Owner:
        _owns_process_exit = True

    original = startup_mod.arm_shutdown_stall_dump
    startup_mod.arm_shutdown_stall_dump = lambda: (armed.append("armed"), True)[1]  # type: ignore[assignment]
    try:
        assert app.DaemonApplication._arm_shutdown_stall_dump_if_owned(_Embedded()) is False
        assert armed == [], "an embedded daemon armed a watchdog over its host"
        assert app.DaemonApplication._arm_shutdown_stall_dump_if_owned(_Owner()) is True
        assert armed == ["armed"], (
            "the owning daemon did not arm; gating it off entirely would pass "
            "the assertion above while removing the diagnostic altogether"
        )
    finally:
        startup_mod.arm_shutdown_stall_dump = original  # type: ignore[assignment]


@pytest.mark.parametrize("close_stage", ["generic", "harness-drain", "session-drain"])
def test_run_arms_backstop_before_a_blocked_close(close_stage: str) -> None:
    """Real run/finally and default stop/close budgets survive a stuck join.

    The drain cases use production stop/close methods, without passing timeout.
    Their injected inner joins ignore the supplied budget. The backstop fires
    after six seconds, beyond the five-second default, instead of parking the
    process forever. No signature-inspection compatibility shim is involved.
    """
    program = textwrap.dedent(
        """
        import threading
        import sys
        from types import SimpleNamespace
        from hyprial.daemon.impl.application.startup import _StartupMixin
        from hyprial.daemon.impl.application.shutdown import _ShutdownMixin
        import hyprial.daemon.impl.application.startup as startup
        from hyprial.daemon.impl.processes.supervisor import ManagedHarnessRuntime
        from hyprial.daemon.impl.harnesses.actor.facade import HarnessRuntimeFacade
        from hyprial.daemon.impl.composition.domains import AgentSessionDomains

        stage = CLOSE_STAGE
        startup.TEARDOWN_BUDGETED_SECONDS = 0.2 if stage == "generic" else 6.0

        def blocked_join(timeout):
            if stage == "harness-drain":
                assert 0 < timeout <= 5.0, timeout
            else:
                # AgentSessionDomains.close allocates 60% of its default 5s
                # to session drain, reserving the rest for agent drain.
                assert timeout == 3.0, timeout
            print("DEFAULT_BUDGET_REACHED_JOIN", file=sys.stderr, flush=True)
            threading.Event().wait()

        harnesses = None
        domains = None
        if stage == "harness-drain":
            facade = object.__new__(HarnessRuntimeFacade)
            facade._pending_lock = threading.Lock()
            facade._pending = {}
            facade._actor = SimpleNamespace(close_runtime=blocked_join)
            def request(command, expected, *, timeout):
                assert timeout == 5.0, timeout
                return SimpleNamespace(drain_complete=True)
            facade._request = request
            harnesses = object.__new__(ManagedHarnessRuntime)
            harnesses._facade = facade
        elif stage == "session-drain":
            domains = object.__new__(AgentSessionDomains)
            domains._close_lock = threading.Lock()
            domains._domains_closed = False
            domains.cells = SimpleNamespace(close=lambda: None)
            domains.session = SimpleNamespace(drain=blocked_join)

        class Stub(_StartupMixin, _ShutdownMixin):
            _owns_process_exit = True
            _lock_stream = None
            _home_guard = SimpleNamespace(claim=lambda: None)
            node_id = "isolated-close-hang"
            state_dir = None
            _harnesses = harnesses
            _agent_session_domains = domains
            def _acquire_lock(self):
                raise RuntimeError("synthetic startup failure")
            def _log(self, *args, **kwargs):
                pass
            def _log_trace(self, *args, **kwargs):
                pass
            def _close(self):
                print("CLOSE_ENTERED", file=sys.stderr, flush=True)
                if stage == "generic":
                    threading.Event().wait()
                self._close_harnesses_before_agent_domains()

        Stub().run()
        """
    ).replace("CLOSE_STAGE", repr(close_stage))
    try:
        result = subprocess.run(
            [sys.executable, "-c", program], capture_output=True, text=True, timeout=14,
        )
    except subprocess.TimeoutExpired as error:
        pytest.fail(f"run() left the blocked close without an exit backstop: {error.stderr!r}")
    assert "CLOSE_ENTERED" in result.stderr
    if close_stage != "generic":
        assert "DEFAULT_BUDGET_REACHED_JOIN" in result.stderr
    assert result.returncode == _EXIT_CODE_STUCK, result.stderr


def test_rearm_supersedes_old_timer_and_new_timer_still_fires() -> None:
    program = textwrap.dedent(
        """
        import threading
        import time
        from hyprial.daemon.impl.application.shutdown import _ShutdownMixin

        class Stub(_ShutdownMixin):
            _owns_process_exit = True
            state_dir = None
            def _log_trace(self, *args, **kwargs):
                pass

        stub = Stub()
        stub._arm_exit_backstop(0.2)
        stub._arm_exit_backstop(0.8)
        time.sleep(0.4)
        print("OLD_DEADLINE_PASSED", flush=True)
        threading.Event().wait()
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", program], capture_output=True, text=True, timeout=8,
    )
    assert result.returncode == _EXIT_CODE_STUCK, result.stderr
    assert "OLD_DEADLINE_PASSED" in result.stdout


@pytest.mark.parametrize("attribute", ["_EXIT_BACKSTOP_SECONDS", "_EXIT_CODE_STUCK"])
def test_the_knobs_this_test_overrides_still_exist(attribute: str) -> None:
    """Renaming either constant would silently un-wire the tests above.

    The child process sets `_EXIT_BACKSTOP_SECONDS` by name; if that name
    moves, the override stops applying and the first test would wait for the
    real budget instead -- passing for the wrong reason, or timing out for one.
    """

    import hyprial.daemon.impl.application.shutdown as app

    assert isinstance(getattr(app, attribute), (int, float))
