from __future__ import annotations

import json
import os
import signal
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

import hyprial.daemon.impl.configuration.network_profile as network_profile
import hyprial.shell.impl.login.flow as login
from hyprial.daemon.impl.configuration import identity as daemon_identity
from hyprial.daemon.impl.configuration.home_guard import (
    ActiveDaemonHeartbeat,
    HYPRIALHomeInUse,
    claim_wait_timeout_from_environment,
    keepalive_duration_from_environment,
)
from hyprial.daemon.impl.mcp.channel.ownership import _OwnerProcessStatus
from hyprial.kernel import (
    HYPRIALHomeNotInitialized,
    default_hyprial_home,
    require_initialized_hyprial_home,
)

from ..support import (
    CLAIMER_KEEPALIVE_SECONDS,
    HOME_GUARD_REJECTION_BUDGET_SECONDS,
    _copied_home_guard,
    _daemon_environment,
    _record,
    _run_blocked_daemon,
    _short_temp_root,
    _start_daemon,
    _stop_daemon,
    _wait_detected,
    _wait_for,
    _write_record,
)
from hyprial.shell.impl.cli.commands.common.root import app as _cli_root_app
import hyprial.shell.impl.cli.commands.adapter.lark_auth as _cli_owner_1
import hyprial.shell.impl.cli.runtime.entry  # noqa: F401  (installs the explicit CLI composition)


def test_non_init_command_rejects_missing_environment_home(tmp_path: Path) -> None:
    home = tmp_path / "missing-home"
    state = tmp_path / "state"

    result = CliRunner().invoke(
        _cli_root_app,
        ["daemon", "status", "--json"],
        env={"HYPRIAL_HOME": str(home), "HARNESS_STATE_DIR": str(state)},
    )

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["code"] == "HYPRIAL_HOME_NOT_INITIALIZED"
    assert str(home.resolve()) in payload["error"]
    assert "HYPRIAL_HOME environment variable" in payload["error"]
    assert payload["data"] == {
        "path": str(home.resolve()),
        "source": "HYPRIAL_HOME environment variable",
    }
    assert not home.exists()
    assert not state.exists()


def test_non_init_command_names_default_home_source(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("HYPRIAL_HOME", raising=False)
    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    result = CliRunner().invoke(_cli_root_app, ["help", "--json"])

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert str((tmp_path / ".hyprial").resolve()) in payload["error"]
    assert "default value (~/.hyprial)" in payload["error"]


def test_version_succeeds_without_creating_default_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Allen's 2026-09-14 ruling: version works before initialization."""

    monkeypatch.delenv("HYPRIAL_HOME", raising=False)
    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    home = tmp_path / ".hyprial"

    result = CliRunner().invoke(_cli_root_app, ["version", "--json"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["ok"] is True
    assert not home.exists()
    assert not (tmp_path / "state").exists()


def test_present_new_home_is_the_runtime_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("HYPRIAL_HOME", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    current = tmp_path / ".hyprial"
    current.mkdir()

    assert default_hyprial_home() == (
        current.resolve(),
        "default value (~/.hyprial)",
    )


def test_non_directory_new_home_fails_loudly(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("HYPRIAL_HOME", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    current = tmp_path / ".hyprial"
    current.write_text("not a directory", encoding="utf-8")

    with pytest.raises(HYPRIALHomeNotInitialized) as caught:
        require_initialized_hyprial_home()

    assert getattr(caught.value, "path", None) == current.resolve()
    assert getattr(caught.value, "source", None) == "default value (~/.hyprial)"


def test_default_home_consumers_share_the_new_home_without_runtime_fallback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    current = (tmp_path / ".hyprial").resolve()
    env: dict[str, str] = {}

    assert login.credential_path(env).parents[1] == current
    assert login._home_dir(env, None) == current
    assert daemon_identity._settings_path(env, None).parent == current
    assert network_profile.profile_path(env).parent == current
    assert _cli_owner_1._canonical_lark_auth_state_path().parents[1] == current


def test_default_home_selection_has_one_source_authority() -> None:
    root = Path(__file__).resolve().parents[5] / 'src/hyprial'
    bypasses = []
    authorities = {
        root / "kernel" / "impl" / "configuration" / "home.py",
        root / "daemon" / "impl" / "harnesses" / "pi" / "resources" / "pi_daemon_ipc.ts",
    }
    for path in root.rglob("*"):
        if path.suffix not in {".py", ".ts"} or path in authorities:
            continue
        for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            python_bypass = "Path.home()" in line and '".hyprial"' in line
            typescript_bypass = "process.env.HOME" in line and ".hyprial" in line
            if python_bypass or typescript_bypass:
                bypasses.append(f"{path.relative_to(root)}:{line_number}")
    assert bypasses == []


def test_fresh_other_pid_blocks_and_reports_that_pid(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    guard = ActiveDaemonHeartbeat(
        home,
        keepalive_duration=5.0,
        claim_wait_timeout=0.0,
        pid=111,
        monotonic_clock=lambda: 105.0,
        identity_reader=lambda _pid: "self",
        process_status=lambda _pid, _identity: _OwnerProcessStatus.ALIVE,
    )
    (home / ".active_daemon").write_text(
        json.dumps(_record(pid=222, identity="other", heartbeat=100.0)),
        encoding="utf-8",
    )

    with pytest.raises(HYPRIALHomeInUse, match=r"pid 222") as caught:
        guard.claim()

    assert caught.value.pid == 222


def test_claim_waits_out_a_tearing_down_owner_and_takes_over(
    tmp_path: Path,
) -> None:
    """A fresh-but-final heartbeat (rapid restart) yields within the wait."""

    home = tmp_path / "home"
    home.mkdir()
    marker = home / ".active_daemon"
    marker.write_text(
        json.dumps(_record(pid=222, identity="other", heartbeat=100.0)),
        encoding="utf-8",
    )
    calls = {"count": 0}

    def clock() -> float:
        calls["count"] += 1
        if calls["count"] >= 3:
            # The previous daemon finishes its teardown mid-wait.
            marker.unlink(missing_ok=True)
        return 105.0

    guard = ActiveDaemonHeartbeat(
        home,
        keepalive_duration=5.0,
        claim_wait_timeout=15.0,
        pid=111,
        monotonic_clock=clock,
        identity_reader=lambda _pid: "self",
        process_status=lambda _pid, _identity: _OwnerProcessStatus.ALIVE,
    )

    guard.claim()
    try:
        assert json.loads(marker.read_text(encoding="utf-8"))["pid"] == 111
    finally:
        guard.close()


def test_claim_fails_fast_when_the_owner_heartbeat_advances(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    marker = home / ".active_daemon"
    marker.write_text(
        json.dumps(_record(pid=222, identity="other", heartbeat=100.0)),
        encoding="utf-8",
    )
    calls = {"count": 0}

    def clock() -> float:
        calls["count"] += 1
        if calls["count"] == 2:
            # The owner refreshes its heartbeat: it is alive and serving.
            marker.write_text(
                json.dumps(_record(pid=222, identity="other", heartbeat=101.0)),
                encoding="utf-8",
            )
        return 105.0

    guard = ActiveDaemonHeartbeat(
        home,
        keepalive_duration=5.0,
        claim_wait_timeout=3600.0,
        pid=111,
        monotonic_clock=clock,
        identity_reader=lambda _pid: "self",
        process_status=lambda _pid, _identity: _OwnerProcessStatus.ALIVE,
    )

    with pytest.raises(HYPRIALHomeInUse, match=r"pid 222"):
        guard.claim()


def test_own_pid_is_never_rejected_by_its_fresh_heartbeat(tmp_path: Path) -> None:
    """Mutation guard: deleting the ``pid != self`` exemption must fail here."""

    guard = ActiveDaemonHeartbeat(
        tmp_path,
        keepalive_duration=5.0,
        pid=111,
        monotonic_clock=lambda: 105.0,
        identity_reader=lambda _pid: "self",
        process_status=lambda _pid, _identity: _OwnerProcessStatus.ALIVE,
    )

    assert guard._blocking_pid(
        _record(pid=111, identity="self", heartbeat=100.0)
    ) is None


def test_keepalive_duration_is_configurable_and_validated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("HYPRIAL_DAEMON_KEEPALIVE_DURATION", raising=False)
    assert keepalive_duration_from_environment() == 5.0
    monkeypatch.setenv("HYPRIAL_DAEMON_KEEPALIVE_DURATION", "0.25")
    assert keepalive_duration_from_environment() == 0.25
    for invalid in ("0", "-1", "nan", "not-a-number"):
        monkeypatch.setenv("HYPRIAL_DAEMON_KEEPALIVE_DURATION", invalid)
        with pytest.raises(ValueError, match="must be a positive number"):
            keepalive_duration_from_environment()


def test_claim_wait_timeout_is_configurable_and_validated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("HYPRIAL_DAEMON_LOCK_WAIT_TIMEOUT", raising=False)
    assert claim_wait_timeout_from_environment() == 15.0
    monkeypatch.setenv("HYPRIAL_DAEMON_LOCK_WAIT_TIMEOUT", "0")
    assert claim_wait_timeout_from_environment() == 0.0
    for invalid in ("-1", "inf", "nan", "not-a-number"):
        monkeypatch.setenv("HYPRIAL_DAEMON_LOCK_WAIT_TIMEOUT", invalid)
        with pytest.raises(ValueError, match="must be a non-negative number"):
            claim_wait_timeout_from_environment()


@pytest.mark.parametrize(
    ("heartbeat", "status"),
    [
        (90.0, _OwnerProcessStatus.ALIVE),
        (100.0, _OwnerProcessStatus.PID_MISSING),
        (100.0, _OwnerProcessStatus.IDENTITY_MISMATCH),
    ],
)
def test_stale_dead_or_pid_reused_record_allows_takeover(
    tmp_path: Path, heartbeat: float, status: _OwnerProcessStatus
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    guard = ActiveDaemonHeartbeat(
        home,
        keepalive_duration=5.0,
        pid=111,
        monotonic_clock=lambda: 100.0,
        wall_clock=lambda: 1_777_000_000.0,
        identity_reader=lambda _pid: "self",
        process_status=lambda _pid, _identity: status,
    )
    (home / ".active_daemon").write_text(
        json.dumps(_record(pid=222, identity="other", heartbeat=heartbeat)),
        encoding="utf-8",
    )

    guard.claim()
    try:
        claimed = json.loads((home / ".active_daemon").read_text(encoding="utf-8"))
        assert claimed["pid"] == 111
        assert claimed["processIdentity"] == "self"
        assert claimed["heartbeatAt"].endswith("Z")
    finally:
        guard.close()


def test_malformed_record_is_replaced_atomically(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    marker = home / ".active_daemon"
    marker.write_text('{"pid":', encoding="utf-8")
    guard = ActiveDaemonHeartbeat(
        home,
        keepalive_duration=5.0,
        pid=111,
        identity_reader=lambda _pid: "self",
    )

    guard.claim()
    try:
        assert json.loads(marker.read_text(encoding="utf-8"))["pid"] == 111
        assert not tuple(home.glob(".active_daemon.*.tmp"))
    finally:
        guard.close()


def test_two_daemons_share_one_home_then_real_exit_allows_takeover(
) -> None:
    with tempfile.TemporaryDirectory(prefix="hyprial-hg-", dir=_short_temp_root()) as raw:
        root = Path(raw)
        home = root / "home"
        home.mkdir()
        first_env = _daemon_environment(home, root / "state-a", "home-guard-a")
        second_env = _daemon_environment(home, root / "state-b", "home-guard-b")
        first = _start_daemon(first_env)
        replacement: subprocess.Popen[str] | None = None
        try:
            marker = home / ".active_daemon"
            _wait_for(marker.exists)
            _wait_for(lambda: (root / "state-a" / "daemon.sock").exists())
            owner_pid = json.loads(marker.read_text(encoding="utf-8"))["pid"]
            assert owner_pid == first.pid

            _startup, _rejection, blocked, _phases = _run_blocked_daemon(second_env)
            assert blocked.returncode == 1, blocked
            payload = json.loads(blocked.stdout)
            assert payload["code"] == "HYPRIAL_HOME_IN_USE"
            assert payload["data"]["pid"] == first.pid
            assert str(first.pid) in payload["error"]

            _stop_daemon(first)
            _wait_for(lambda: not marker.exists())

            replacement = _start_daemon(second_env)
            _wait_for(marker.exists)
            _wait_for(lambda: (root / "state-b" / "daemon.sock").exists())
            claimed = json.loads(marker.read_text(encoding="utf-8"))
            assert claimed["pid"] == replacement.pid
        finally:
            if replacement is not None:
                _stop_daemon(replacement)
            _stop_daemon(first)


def test_frozen_record_with_live_owner_alerts_and_still_claims(
    tmp_path: Path,
) -> None:
    """The 34h incident shape: a copied home whose record's owner is alive
    on this machine but never tends this copy.  Claiming completes
    immediately; the background check alerts after the window with both
    pids/generations.  Detection and alarm only, no automatic remediation."""

    home = tmp_path / "home"
    home.mkdir()
    _write_record(home, pid=222, identity="other", generation="other-generation")
    now = [111.0]  # record age 11s >= 2*keepalive: stale
    detected: list[dict[str, Any]] = []
    guard = _copied_home_guard(
        home,
        now=now,
        process_status=lambda _pid, _identity: _OwnerProcessStatus.ALIVE,
        detected=detected,
    )

    guard.claim()
    try:
        # Claiming proceeded without paying the window: this daemon owns the home.
        assert json.loads((home / ".active_daemon").read_text(encoding="utf-8"))[
            "pid"
        ] == 111
        _wait_detected(detected)
        assert len(detected) == 1
        detail = detected[0]
        assert detail["recordPid"] == 222
        assert detail["claimantPid"] == 111
        assert detail["recordGeneration"] == "other-generation"
        assert detail["claimantGeneration"] == guard.generation
        assert detail["processStatus"] == "alive"
        assert detail["windowSeconds"] == 10.0
    finally:
        guard.close()


def test_owner_dying_within_window_is_a_normal_restart_not_a_duplicate(
    tmp_path: Path,
) -> None:
    """The tightened criterion's load-bearing cell (批复③, background form):
    a previous daemon still inside its shutdown at claim time exits before
    the window ends.  That is a normal restart and must NOT alert."""

    home = tmp_path / "home"
    home.mkdir()
    _write_record(home, pid=222, identity="other", generation="other-generation")
    now = [111.0]
    detected: list[dict[str, Any]] = []

    def status(_pid: int, _identity: str) -> _OwnerProcessStatus:
        # The old daemon finishes its shutdown (and exits) mid-window.
        if now[0] >= 115.0:
            return _OwnerProcessStatus.PID_MISSING
        return _OwnerProcessStatus.ALIVE

    guard = _copied_home_guard(home, now=now, process_status=status, detected=detected)

    guard.claim()
    try:
        # The check thread burns the (fake) window in real milliseconds via
        # the advancing sleeper; give it a generous real deadline.
        deadline = time.monotonic() + 5.0
        while now[0] < 121.0 and time.monotonic() < deadline:
            time.sleep(0.01)
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline and guard._duplicate_thread is not None:
            time.sleep(0.02)
        assert detected == []
    finally:
        guard.close()


def test_stale_record_with_dead_owner_does_not_alert(tmp_path: Path) -> None:
    """The common crashed-predecessor case: positive death at snapshot time
    means no candidate, and no background check is even started."""

    home = tmp_path / "home"
    home.mkdir()
    _write_record(home, pid=222, identity="other", generation="other-generation")
    now = [111.0]
    detected: list[dict[str, Any]] = []
    guard = _copied_home_guard(
        home,
        now=now,
        process_status=lambda _pid, _identity: _OwnerProcessStatus.PID_MISSING,
        detected=detected,
    )

    guard.claim()
    try:
        assert detected == []
        assert guard._duplicate_thread is None
    finally:
        guard.close()


def test_unverifiable_owner_still_alerts_on_the_fail_safe_side(
    tmp_path: Path,
) -> None:
    """UNKNOWN (cannot prove the record's owner died) lands on the alert
    side, matching live_daemon_pid's fail-safe direction; the event names
    the status so the weaker evidence stays visible."""

    home = tmp_path / "home"
    home.mkdir()
    _write_record(home, pid=222, identity="other", generation="other-generation")
    now = [111.0]
    detected: list[dict[str, Any]] = []
    guard = _copied_home_guard(
        home,
        now=now,
        process_status=lambda _pid, _identity: _OwnerProcessStatus.UNKNOWN,
        detected=detected,
    )

    guard.claim()
    try:
        _wait_detected(detected)
        assert detected[0]["processStatus"] == "unknown"
    finally:
        guard.close()


def test_fresh_record_blocks_via_home_in_use_without_duplicate_alert(
    tmp_path: Path,
) -> None:
    """A fresh heartbeat already fails loudly through HYPRIALHomeInUse;
    duplicate detection must stay out of that path."""

    home = tmp_path / "home"
    home.mkdir()
    _write_record(home, pid=222, identity="other", generation="other-generation")
    now = [101.0]  # record age 1s: fresh
    detected: list[dict[str, Any]] = []
    guard = _copied_home_guard(
        home,
        now=now,
        process_status=lambda _pid, _identity: _OwnerProcessStatus.ALIVE,
        detected=detected,
    )

    with pytest.raises(HYPRIALHomeInUse, match=r"pid 222"):
        guard.claim()
    assert detected == []


def test_claim_latency_is_unaffected_by_the_observe_window(
    tmp_path: Path,
) -> None:
    """裁定钉子: candidate conditions hit, yet claim must not pay the
    observation window -- the cli ready budget (15s) shares this path.
    Real clock, real sleeper, window one hundred times the assertion."""

    home = tmp_path / "home"
    home.mkdir()
    _write_record(home, pid=222, identity="other", generation="other-generation")
    detected: list[dict[str, Any]] = []
    guard = ActiveDaemonHeartbeat(
        home,
        keepalive_duration=5.0,
        claim_wait_timeout=0.0,
        pid=111,
        identity_reader=lambda _pid: "self",
        process_status=lambda _pid, _identity: _OwnerProcessStatus.ALIVE,
        duplicate_detected=detected.append,
        duplicate_observe_window=3600.0,
    )
    # The record's monotonic heartbeat comes from a different clock domain
    # than this guard's (default time.monotonic), so force staleness:
    # heartbeatMonotonic 0.0 is always >= 2*keepalive behind.

    started = time.monotonic()
    guard.claim()
    elapsed = time.monotonic() - started
    try:
        assert elapsed < 36.0  # two orders below the 3600s window
        assert guard._duplicate_thread is not None
    finally:
        guard.close()
    # close() must collect the check thread promptly, not after the window.
    assert guard._duplicate_thread is None


def test_reclaim_during_window_aborts_the_check(tmp_path: Path) -> None:
    """If a third process re-claims the home mid-window, the snapshot's
    situation is gone; a verdict from it would be a dangling conclusion."""

    home = tmp_path / "home"
    home.mkdir()
    now = [111.0]
    detected: list[dict[str, Any]] = []
    guard = _copied_home_guard(
        home,
        now=now,
        process_status=lambda _pid, _identity: _OwnerProcessStatus.ALIVE,
        detected=detected,
    )
    guard.claim()
    try:
        snapshot = {
            "pid": 222,
            "processIdentity": "other",
            "generation": "other-generation",
            "heartbeatAt": "2026-09-14T09:50:23Z",
        }
        sleeps = {"count": 0}

        def sleeper(duration: float) -> None:
            sleeps["count"] += 1
            now[0] += duration
            if sleeps["count"] == 2:
                _write_record(home, pid=333, identity="third", generation="third-gen")

        guard._sleeper = sleeper
        guard._run_duplicate_check(snapshot)
        assert detected == []
    finally:
        guard.close()


def test_duplicate_check_failure_reaches_the_failed_callback(
    tmp_path: Path,
) -> None:
    """The lifecycle-thread lesson: an exception on the check thread must
    become an event, not a silent death."""

    home = tmp_path / "home"
    home.mkdir()
    now = [111.0]
    detected: list[dict[str, Any]] = []
    failed: list[str] = []
    guard = _copied_home_guard(
        home,
        now=now,
        process_status=lambda _pid, _identity: _OwnerProcessStatus.ALIVE,
        detected=detected,
        check_failed=failed,
    )

    guard._duplicate_check_entry({})  # missing snapshot keys -> KeyError

    assert len(failed) == 1
    assert "KeyError" in failed[0]
    assert detected == []


def test_a_stalled_live_owner_is_rejected_not_taken_over() -> None:
    """Default policy renews a still-owned home after a four-second pause.

    Deterministic form of task 15512: the owner (our own child daemon) is
    SIGSTOPped for 4s while the blocked second daemon claims. The owner uses
    the original 1s keepalive and the claimer uses the production 5s default.
    TTL-only self-exit is disabled by default, as on dev; after resume the
    original owner renews and the claimer must reject. Explicit opt-in expiry
    orderings remain covered in test_home_lease_actor.py.
    """
    stall_seconds = 4.0
    assert 2 * 1.0 < stall_seconds < 2 * CLAIMER_KEEPALIVE_SECONDS
    with tempfile.TemporaryDirectory(prefix="hyprial-hg-", dir=_short_temp_root()) as raw:
        root = Path(raw)
        home = root / "home"
        home.mkdir()
        first_env = _daemon_environment(home, root / "state-a", "home-guard-a")
        second_env = _daemon_environment(home, root / "state-b", "home-guard-b")
        first = _start_daemon(first_env)
        stopped = False
        try:
            _wait_for((home / ".active_daemon").exists)
            _wait_for(lambda: (root / "state-a" / "daemon.sock").exists())
            os.kill(first.pid, signal.SIGSTOP)
            stopped = True
            resume_at = time.monotonic() + stall_seconds

            def resume() -> None:
                time.sleep(max(0.0, resume_at - time.monotonic()))
                os.kill(first.pid, signal.SIGCONT)

            resumer = threading.Thread(target=resume)
            resumer.start()
            _startup, _rejection, blocked, _phases = _run_blocked_daemon(second_env)
            resumer.join()
            stopped = False
            assert blocked.returncode == 1, blocked
            payload = json.loads(blocked.stdout)
            assert payload["code"] == "HYPRIAL_HOME_IN_USE"
            assert payload["data"]["pid"] == first.pid
            marker = json.loads((home / ".active_daemon").read_text(encoding="utf-8"))
            assert marker["pid"] == first.pid
        finally:
            if stopped:
                os.kill(first.pid, signal.SIGCONT)
            _stop_daemon(first)


def test_pre_readiness_delay_cannot_consume_the_rejection_budget() -> None:
    """Pre-import startup latency must not eat the fast-rejection budget.

    Mutation guard for the readiness handshake.  The planted delay is
    longer than ``HOME_GUARD_REJECTION_BUDGET_SECONDS`` but well inside the
    startup guard, and it happens *before* the readiness byte.  The
    candidate structure stays green because the rejection clock starts at
    the byte.  If the rejection clock were restarted at process creation --
    or the handshake deleted -- the delay would blow the rejection budget
    and redden this test (the exact shape CI reddened on, tasks
    10155 / 10285, where interpreter + ``import hyprial.cli`` under the
    2-CPU quota consumed the old single 15s budget).
    """
    with tempfile.TemporaryDirectory(prefix="hyprial-hg-", dir=_short_temp_root()) as raw:
        root = Path(raw)
        home = root / "home"
        home.mkdir()
        first_env = _daemon_environment(home, root / "state-a", "home-guard-a")
        second_env = _daemon_environment(home, root / "state-b", "home-guard-b")
        first = _start_daemon(first_env)
        try:
            marker = home / ".active_daemon"
            _wait_for(marker.exists)
            _wait_for(lambda: (root / "state-a" / "daemon.sock").exists())

            delay = HOME_GUARD_REJECTION_BUDGET_SECONDS + 2.0
            startup_seconds, rejection_seconds, blocked, _phases = _run_blocked_daemon(
                second_env, pre_ready_delay=delay
            )
            # The whole planted delay must land in the startup phase...
            assert startup_seconds >= delay, (startup_seconds, delay)
            # ...which leaves the rejection phase its full budget: the fast
            # HYPRIAL_HOME_IN_USE rejection still completes within it.
            assert rejection_seconds < HOME_GUARD_REJECTION_BUDGET_SECONDS, (
                startup_seconds,
                rejection_seconds,
                blocked.stdout,
                blocked.stderr,
            )
            assert blocked.returncode == 1, blocked
            payload = json.loads(blocked.stdout)
            assert payload["code"] == "HYPRIAL_HOME_IN_USE"
            assert payload["data"]["pid"] == first.pid
        finally:
            _stop_daemon(first)
