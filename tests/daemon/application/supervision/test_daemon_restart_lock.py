"""Regression coverage for the E2E-006 rapid-restart lock race.

A daemon releases its ``daemon.lock`` flock only at the very end of shutdown,
after the control socket it advertises has already been unlinked.  Because
Historically ``hyprial daemon stop`` (and ``IsolatedDaemon.sigterm``) reported
success as soon as that socket disappeared, so a rapid restart could overtake
the old process's teardown.  Startup still waits defensively, and the public
stop command now also waits for the final lock-release completion receipt.
"""

from __future__ import annotations

import fcntl
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from hyprial.daemon.impl.application import DaemonApplication
from hyprial.shell.impl.cli.commands.common import daemon_stop, pending_restart
from hyprial.shell.impl.cli.commands.upgrade import commands as upgrade_commands


from hyprial.shell.impl.daemon_wiring import daemon_dependencies as _app_injections
from hyprial.shell.impl.cli.commands.common import support as _cli_support
from hyprial.shell.impl.cli.commands.common.services import get_services as _cli_get_services
import hyprial.shell.impl.cli.commands.common.daemon_stop as _cli_owner_1
import hyprial.shell.impl.cli.runtime.entry  # noqa: F401  (installs the explicit CLI composition)


def _app(tmp_path: Path) -> DaemonApplication:
    state = tmp_path / "state"
    return DaemonApplication(
        state_dir=state,
        socket_path=state / "daemon.sock",
        node_id="lock-test",
        **_app_injections(),
    )


def _hold_lock(state_dir: Path) -> Any:
    state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    holder = (state_dir / "daemon.lock").open("a+", encoding="utf-8")
    fcntl.flock(holder.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    return holder


def test_acquire_lock_succeeds_without_contention(
    tmp_path: Path, own_daemon_application
) -> None:
    app = own_daemon_application(_app(tmp_path))
    app._acquire_lock()
    try:
        assert app._lock_stream is not None
    finally:
        app._lock_stream.close()


def test_acquire_lock_waits_for_previous_daemon_to_release(
    monkeypatch: Any, tmp_path: Path, own_daemon_application
) -> None:
    monkeypatch.delenv("HYPRIAL_DAEMON_LOCK_WAIT_TIMEOUT", raising=False)  # default window
    app = own_daemon_application(_app(tmp_path))
    holder = _hold_lock(app.state_dir)

    # Release the previous holder only when this test says so, mimicking the
    # old daemon finishing _close() shortly after its socket already vanished.
    # A timer-based release made the outcome a bet on which wall-clock fired
    # first (the release vs the acquire's give-up); the Event removes the bet.
    errors: list[BaseException] = []
    acquired = threading.Event()

    def acquire() -> None:
        try:
            app._acquire_lock()  # must wait for release rather than fail immediately
        except BaseException as error:  # noqa: BLE001 - asserted below
            errors.append(error)
        finally:
            acquired.set()

    waiter = threading.Thread(target=acquire, name="lock-waiter")
    try:
        waiter.start()
        # One-directional: the lock cannot come back before this line, so a
        # return here means the waiter gave up while the holder still held it.
        assert not acquired.wait(0.3), (
            f"acquire returned while the previous daemon still held the lock: {errors!r}"
        )
    finally:
        fcntl.flock(holder.fileno(), fcntl.LOCK_UN)
        waiter.join(timeout=10)
        if app._lock_stream is not None:
            app._lock_stream.close()
        holder.close()

    assert errors == []
    assert app._lock_stream is not None


def test_acquire_lock_times_out_with_diagnostic_message(
    monkeypatch: Any, tmp_path: Path, own_daemon_application
) -> None:
    monkeypatch.setenv("HYPRIAL_DAEMON_LOCK_WAIT_TIMEOUT", "0.3")
    app = own_daemon_application(_app(tmp_path))
    holder = _hold_lock(app.state_dir)
    try:
        started = time.monotonic()
        with pytest.raises(RuntimeError) as excinfo:
            app._acquire_lock()
        waited = time.monotonic() - started
        # The message must name the real situation, not the misleading
        # "daemon is already running" that made the original incident opaque.
        assert "still held" in str(excinfo.value)
        assert waited >= 0.25  # exhausted the configured window before failing
        assert app._lock_stream is None
    finally:
        fcntl.flock(holder.fileno(), fcntl.LOCK_UN)
        holder.close()


def test_acquire_lock_fail_fast_when_wait_disabled(
    monkeypatch: Any, tmp_path: Path, own_daemon_application
) -> None:
    monkeypatch.setenv("HYPRIAL_DAEMON_LOCK_WAIT_TIMEOUT", "0")
    app = own_daemon_application(_app(tmp_path))
    holder = _hold_lock(app.state_dir)
    try:
        started = time.monotonic()
        with pytest.raises(RuntimeError) as excinfo:
            app._acquire_lock()
        assert (time.monotonic() - started) < 0.2  # no waiting
        assert "still held" in str(excinfo.value)
    finally:
        fcntl.flock(holder.fileno(), fcntl.LOCK_UN)
        holder.close()


def test_operator_stop_waits_for_complete_daemon_teardown(
    monkeypatch: Any, tmp_path: Path
) -> None:
    state = tmp_path / "state"
    holder = _hold_lock(state)
    monkeypatch.setattr(_cli_get_services(), "_state_dir", lambda: state)
    monkeypatch.setattr(_cli_get_services(), "_socket_path", lambda: state / "daemon.sock")
    monkeypatch.setattr(
        _cli_support,
        "_daemon_request",
        lambda method, timeout: {
            "ok": True,
            "stopped": method == "shutdown",
            "pid": 999_991,
            "stateDir": str(state),
            "lockPath": str(state / "daemon.lock"),
        },
    )
    monkeypatch.setattr(
        "hyprial.daemon.impl.mcp.channel.ownership._read_process_identity",
        lambda _pid: "pid:test-old",
    )
    # ⚠️ The fake must model a daemon that actually exits. It used to return
    # False forever — a process that never dies — and the test still asserted
    # success, which is the belief that took production down on 2026-08-30.
    # Here the healthy sequence is modelled: the lock comes back, then the
    # process goes. What this test is really about — that the public stop waits
    # for the lock instead of returning when the socket vanishes — is unchanged.
    exited = threading.Event()

    def _release_teardown() -> None:
        fcntl.flock(holder.fileno(), fcntl.LOCK_UN)

    monkeypatch.setattr(
        _cli_get_services(), "_stopped_process", lambda _pid, _identity: exited.is_set()
    )
    # The lock comes back and the process goes only when this test says so --
    # not on a wall-clock timer, so the wait cannot be lost to a scheduling
    # delay (the flake this replaces).
    errors: list[BaseException] = []
    finished = threading.Event()
    observed: dict[str, Any] = {}

    def stop() -> None:
        try:
            observed["result"] = _cli_owner_1._stop_daemon_for_operator()
        except BaseException as error:  # noqa: BLE001 - asserted below
            errors.append(error)
        finally:
            finished.set()

    waiter = threading.Thread(target=stop, name="graceful-stop")
    try:
        waiter.start()
        # One-directional: the lock cannot be released and the process cannot be
        # gone before this line, so a return here is the early-return bug this
        # test exists to catch.
        assert not finished.wait(0.3), (
            f"graceful stop returned while the lock was still held: {observed!r}"
        )
    finally:
        _release_teardown()
        returned_before_exit = finished.wait(0.3)
        exited.set()
        waiter.join(timeout=10)
        holder.close()
    assert returned_before_exit, (
        "operator stop waited for process exit after teardown completed"
    )

    assert errors == []
    result = observed["result"]
    assert result["ok"] is True
    assert result["stopped"] is True
    assert result["teardownCompleted"] is True
    assert result["survivingPid"] == 999_991


@pytest.mark.parametrize("expected_epoch", [None, "expected-epoch"])
def test_operator_stop_reports_the_pid_when_the_old_process_does_not_exit(
    monkeypatch: Any, tmp_path: Path, expected_epoch: str | None
) -> None:
    state = tmp_path / "state"
    state.mkdir()
    lock = state / "daemon.lock"
    lock.write_text("")
    monkeypatch.setattr(
        _cli_support,
        "_daemon_request",
        lambda method, *args, **kwargs: {
            "ok": True,
            "stopped": method == "shutdown",
            "pid": 999_993,
            "stateDir": str(state),
            "lockPath": str(lock),
        },
    )
    monkeypatch.setattr(_cli_get_services(), "_stopped_process", lambda _pid, _identity: False)

    result = _cli_owner_1._stop_daemon_for_operator(expected_epoch=expected_epoch)

    assert result["ok"] is True
    assert result["stopped"] is True
    assert result["teardownCompleted"] is True
    assert result["survivingPid"] == 999_993


def test_teardown_lock_timeout_is_distinct_from_socket_timeout(
    monkeypatch: Any, tmp_path: Path
) -> None:
    state = tmp_path / "state"
    holder = _hold_lock(state)
    monkeypatch.setattr(_cli_get_services(), "_stopped_process", lambda _pid, _identity: False)
    try:
        with pytest.raises(_cli_get_services().CliError) as excinfo:
            _cli_owner_1._wait_daemon_teardown_receipt(
                {"stateDir": str(state), "lockPath": str(state / "daemon.lock")},
                time.monotonic() + 0.1,
                old_pid=999_992,
                old_identity=None,
            )
        assert excinfo.value.code == "DAEMON_STOP_TIMEOUT"
        assert "teardown did not finish" in str(excinfo.value)
    finally:
        fcntl.flock(holder.fileno(), fcntl.LOCK_UN)
        holder.close()


def test_a_replacement_taking_over_does_not_prove_the_old_process_exited(
    monkeypatch: Any, tmp_path: Path
) -> None:
    """⚠️ This case used to assert the opposite, and that is what broke production.

    It pinned "old process never exits, but a replacement holds the lock and
    ``daemon.json`` moved" ⇒ **stop returns success** — which is precisely the
    2026-08-30 outage: autoupdate stopped the daemon, teardown finished, every
    receipt said done, and the process lived on for nine hours holding one
    hundred and six descendants that blocked the replacement.

    The old assertion was not wrong about *waiting*: following a replacement
    forever would also be a bug. It was wrong about *what a receipt proves*. So
    the wait still refuses to hang on the replacement — it now ends by raising
    instead of by returning success, because with a pid in hand "the old one is
    gone" is the only thing that can end it well.
    """

    state = tmp_path / "state"
    old_holder = _hold_lock(state)
    replacement_ready = threading.Event()

    def hand_over() -> None:
        fcntl.flock(old_holder.fileno(), fcntl.LOCK_UN)
        replacement = (state / "daemon.lock").open("r+", encoding="utf-8")
        fcntl.flock(replacement.fileno(), fcntl.LOCK_EX)
        (state / "daemon.json").write_text('{"pid":999994}\n', encoding="utf-8")
        replacement_ready.set()
        time.sleep(0.4)
        fcntl.flock(replacement.fileno(), fcntl.LOCK_UN)
        replacement.close()

    monkeypatch.setattr(_cli_get_services(), "_stopped_process", lambda _pid, _identity: False)
    thread = threading.Thread(target=hand_over)
    thread.start()
    try:
        with pytest.raises(_cli_get_services().CliError) as excinfo:
            _cli_owner_1._wait_daemon_teardown_receipt(
                {"stateDir": str(state), "lockPath": str(state / "daemon.lock")},
                time.monotonic() + 1.0,
                old_pid=999_993,
                old_identity=None,
            )
        # ⭐ A distinct code, not merely distinct prose. The caller has to
        # branch on this to decide whether to alert, and matching on a sentence
        # is a coupling that breaks silently the first time someone rewords it.
        assert excinfo.value.code == "DAEMON_STOP_SURVIVOR"
        assert excinfo.value.code != "DAEMON_STOP_TIMEOUT", (
            "the survivor case must not share a code with an ordinary timeout: "
            "one means 'we stopped watching, it is probably fine', the other "
            "means 'we watched, it is definitely still there' -- and they ask "
            "for opposite actions"
        )
        assert excinfo.value.data == {"oldPid": 999_993, "teardownCompleted": True}
        assert "did not exit" in str(excinfo.value), (
            "the message must say the process survived teardown, not that the "
            "stop failed: the operator has to go looking for a survivor and its "
            "children, and 'teardown did not finish' sends them the wrong way"
        )
        assert replacement_ready.wait(timeout=0.5), (
            "the replacement scenario must still be the one under test; if the "
            "hand-over never happened this proves nothing about it"
        )
    finally:
        thread.join(timeout=1.0)
        old_holder.close()


def test_receipts_still_end_the_wait_when_there_is_no_pid_to_watch(
    tmp_path: Path,
) -> None:
    """The reason the receipt exits exist at all — keep them working.

    ``_stopped_process`` returns False for an unknown pid, so without the lock
    receipt a stop that could not learn the old pid would always run to the
    deadline. That is the case the exits were for; removing them outright would
    have traded one silent failure for a guaranteed timeout on a healthy stop.
    """

    state = tmp_path / "state"
    holder = _hold_lock(state)
    fcntl.flock(holder.fileno(), fcntl.LOCK_UN)
    try:
        _cli_owner_1._wait_daemon_teardown_receipt(
            {"stateDir": str(state), "lockPath": str(state / "daemon.lock")},
            time.monotonic() + 1.0,
            old_pid=None,
            old_identity=None,
        )
    finally:
        holder.close()


def test_public_stop_does_not_wait_on_replacement_socket(
    monkeypatch: Any, tmp_path: Path
) -> None:
    state = tmp_path / "state"
    socket_path = state / "daemon.sock"
    old_holder = _hold_lock(state)
    socket_path.touch()
    replacement_ready = threading.Event()

    def hand_over() -> None:
        fcntl.flock(old_holder.fileno(), fcntl.LOCK_UN)
        replacement = (state / "daemon.lock").open("r+", encoding="utf-8")
        fcntl.flock(replacement.fileno(), fcntl.LOCK_EX)
        (state / "daemon.json").write_text('{"pid":999997}\n', encoding="utf-8")
        replacement_ready.set()
        time.sleep(0.4)
        fcntl.flock(replacement.fileno(), fcntl.LOCK_UN)
        replacement.close()

    monkeypatch.setattr(_cli_get_services(), "_socket_path", lambda: socket_path)
    monkeypatch.setattr(
        _cli_support,
        "_daemon_request",
        lambda method, timeout: {
            "ok": True,
            "stopping": method == "shutdown",
            "pid": 999_996,
            "stateDir": str(state),
            "lockPath": str(state / "daemon.lock"),
        },
    )
    monkeypatch.setattr(
        "hyprial.daemon.impl.mcp.channel.ownership._read_process_identity",
        lambda _pid: "pid:old-daemon",
    )
    # The old daemon really goes once it has handed over; see the note in
    # test_public_stop_waits_for_complete_daemon_teardown for why a fake that
    # never exits must not be paired with an assertion of success.
    monkeypatch.setattr(
        _cli_get_services(), "_stopped_process", lambda _pid, _identity: replacement_ready.is_set()
    )
    thread = threading.Thread(target=hand_over)
    thread.start()
    try:
        result = _cli_get_services()._stop_daemon_gracefully()
        assert result["stopping"] is True
        assert replacement_ready.wait(timeout=0.5)
        assert socket_path.exists()
    finally:
        thread.join(timeout=1.0)
        old_holder.close()


def test_teardown_receipt_never_creates_untrusted_state_path(
    monkeypatch: Any, tmp_path: Path
) -> None:
    wrong_state = tmp_path / "does-not-exist"
    monkeypatch.setattr(_cli_get_services(), "_stopped_process", lambda _pid, _identity: True)
    _cli_owner_1._wait_daemon_teardown_receipt(
        {
            "stateDir": str(wrong_state),
            "lockPath": str(wrong_state / "daemon.lock"),
        },
        time.monotonic() + 0.1,
        old_pid=999_995,
        old_identity=None,
    )
    assert not wrong_state.exists()


# ---------------------------------------------------------------------------
# ⭐ The survivor case has to reach a person, and not by the timeout's words.
# ---------------------------------------------------------------------------


def test_the_two_timeouts_do_not_share_a_message_or_a_code() -> None:
    """One says "we stopped watching"; the other says "it is definitely there".

    They ask for opposite actions -- wait, versus go and find the survivor --
    so they must not arrive looking the same. Checked on the source because
    the property is about two raises that no single call can exercise at once.
    """

    source = (
        Path(daemon_stop.__file__)
    ).read_text("utf-8")
    # ⚠️ Bounded by the function, not by a character count. The first version
    # sliced 8000 characters; the function grew past it and the window silently
    # stopped covering the second raise -- a fixed window that no longer covers
    # what it was written to cover, while still looking like it does.
    start = source.index("def _wait_daemon_teardown_receipt")
    after = source.index("\ndef ", start + 1)
    body = source[start:after]
    # Both must be present and they must not be the same string. (Counting
    # occurrences would pin an implementation detail: the survivor case is
    # raised from two places now -- immediately for `daemon stop`, and at the
    # deadline for the upgrade path -- and that is not what this test is about.)
    assert "DAEMON_STOP_SURVIVOR" in body and "DAEMON_STOP_TIMEOUT" in body
    assert "DAEMON_STOP_SURVIVOR" != "DAEMON_STOP_TIMEOUT"
    # The stale literal that survived the budget change: the message said "10s"
    # long after the wait became TEARDOWN_BUDGETED_SECONDS. Elapsed time cannot
    # go stale the way a copied constant can.
    assert "within 10s" not in body, (
        "the timeout message hardcodes a budget again; it will drift from the "
        "actual wait and nothing compares prose against the code beside it"
    )


def test_a_survivor_alerts_a_person_while_an_ordinary_timeout_does_not() -> None:
    """The whole point of 甲: still launch, but do not do it silently.

    On 2026-08-30 the cost was not that the replacement was blocked -- it was
    that nine hours passed with nobody knowing. This asserts the survivor branch
    reaches the alert channel, and that the plain timeout does not: an alert on
    every timeout would be back to noise, and noise is what made the last one
    invisible.
    """

    source = (
        Path(pending_restart.__file__)
    ).read_text("utf-8")
    # ⚠️ Window widened again for the fail-closed locked-install helpers.
    # This remains a bounded structural check over the whole upgrade/report
    # path, with headroom rather than a byte-exact boundary. F5 moved the
    # stop/survivor/launch skeleton into `restart_daemon` (same module,
    # same discipline, now also shared with `hyprial daemon restart`); the guard
    # follows the skeleton.
    skeleton = source[source.index("def restart_daemon") :][:32000]
    assert 'stop_error.code == "DAEMON_STOP_SURVIVOR"' in skeleton, (
        "the upgrade path cannot tell a survivor from an ordinary timeout"
    )
    survivor_at = skeleton.index('stop_error.code == "DAEMON_STOP_SURVIVOR"')
    alert_at = skeleton.index("notify_upgrade_failure(", survivor_at)
    # The alert must sit inside the survivor branch, not before it -- otherwise
    # every timeout alerts and the distinction buys nothing.
    assert alert_at > survivor_at
    assert "survivingOldProcess" in skeleton
    # Still launches: refusing would trade a possible failure for a certain
    # absence, which is the bug #315 fixed.
    launch_at = skeleton.index("_launch_daemon_process(")
    assert launch_at > survivor_at, (
        "the survivor branch must not return early; refusing to launch would "
        "reintroduce the outage #315 fixed"
    )
    # And the upgrade path must actually route through the skeleton -- the
    # discipline is shared, not copied back.
    upgrade = source[source.index("def _restart_daemon_onto_install") :][:32000]
    assert "restart_daemon(" in upgrade, (
        "the upgrade path must call the shared restart_daemon skeleton"
    )


# ---------------------------------------------------------------------------
# ⭐ Relax the wait, never the answer.
# ---------------------------------------------------------------------------


def test_daemon_stop_answers_at_once_and_reports_a_survivor_as_a_fact(
    tmp_path: Path,
) -> None:
    """It must not wait for an exit its caller has no use for -- nor lie about it.

    On 2026-08-31 `hyprial daemon stop` inherited the upgrade path's requirement to
    see the process exit, and every caller giving it twenty seconds timed out:
    four CI failures on a change that was right for the *other* caller.

    ⚠️ The first fix attempt failed the command when the process was still
    alive. Measurement killed it: a moment after teardown the process is almost
    always still there, because it is exiting. That version reported a survivor
    on nearly every healthy stop -- not honesty, just a different false
    statement.

    So the survivor comes back as a **fact**, and the caller decides:
    `teardownCompleted` always, `survivingPid` only when someone is still there.
    """

    state = tmp_path / "state"
    state.mkdir()
    lock = state / "daemon.lock"
    lock.write_text("")
    monkey = pytest.MonkeyPatch()
    monkey.setattr(_cli_get_services(), "_stopped_process", lambda _pid, _identity: False)
    try:
        began = time.monotonic()
        observed = _cli_owner_1._wait_daemon_teardown_receipt(
            {"stateDir": str(state), "lockPath": str(lock)},
            # A budget it must NOT spend: waiting this out is the bug.
            time.monotonic() + 50.0,
            old_pid=999_991,
            old_identity=None,
            require_exit=False,
        )
        elapsed = time.monotonic() - began
    finally:
        monkey.undo()

    assert elapsed < 5.0, (
        f"took {elapsed:.1f}s -- still waiting out the upgrade path's budget"
    )
    assert observed["survivingPid"] == 999_991, (
        "a process still alive after teardown has to reach the caller; "
        "silently succeeding is how a survivor goes unnoticed"
    )
    assert observed["teardownCompleted"] is True


def test_a_process_that_did_exit_leaves_no_surviving_pid_field_at_all(
    tmp_path: Path,
) -> None:
    """⭐ Absent, not null and not 0.

    A null in this field would read exactly like "we never looked", and this
    field exists for one purpose: to say whether anyone is still there. The two
    must not share a representation.
    """

    state = tmp_path / "state"
    state.mkdir()
    lock = state / "daemon.lock"
    lock.write_text("")
    monkey = pytest.MonkeyPatch()
    monkey.setattr(_cli_get_services(), "_stopped_process", lambda _pid, _identity: True)
    try:
        observed = _cli_owner_1._wait_daemon_teardown_receipt(
            {"stateDir": str(state), "lockPath": str(lock)},
            time.monotonic() + 50.0,
            old_pid=999_992,
            old_identity=None,
            require_exit=False,
        )
    finally:
        monkey.undo()

    assert "survivingPid" not in observed, (
        f"the field must be absent, not {observed.get('survivingPid')!r}"
    )
    assert observed["teardownCompleted"] is True


def test_restart_and_upgrade_paths_still_have_to_see_the_process_exit() -> None:
    """The replacement paths must not be relaxed with operator stop.

    If the upgrade path accepted receipts too, this change would have quietly
    undone the fix it is built on: teardown finished, lock released,
    `daemon.json` moved -- and a replacement launched over a process that was
    still alive with 106 descendants. That is the nine-hour outage.

    Structural, because the two callers differ by an argument rather than by
    behaviour any single call can show.
    """

    source = "\n".join(
        Path(module.__file__).read_text("utf-8")
        for module in (daemon_stop, pending_restart)
    )
    # Both operator-stop branches opt out explicitly.
    operator = source[source.index("def _stop_daemon_for_operator") :][:1800]
    assert operator.count("require_exit=False") == 2
    # The default stays strict, so replacement paths that omit the flag require
    # process-exit proof.
    assert "def _stop_daemon_gracefully(*, require_exit: bool = True)" in source
    restart = source[source.index("def restart_daemon") :]
    restart = restart[: restart.index("\ndef ", 1)]
    assert "_stop_daemon_gracefully()" in restart
    assert "require_exit=False" not in restart
    # The upgrade path must not opt out.
    upgrade = source[source.index("def _restart_daemon_onto_install") :][:24000]
    # M0.1 moved the restart helper into common/; _perform_upgrade stayed in
    # the upgrade command module and keeps the guard's original reach.
    upgrade_command = Path(upgrade_commands.__file__).read_text("utf-8")
    upgrade += upgrade_command[upgrade_command.index("def _perform_upgrade") :]
    assert "require_exit=False" not in upgrade, (
        "the upgrade path dropped the exit requirement; it is about to start a "
        "replacement, and receipts do not prove the old process is gone"
    )
