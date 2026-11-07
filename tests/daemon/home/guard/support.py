"""Public-seam coverage for HYPRIAL_HOME initialization and daemon ownership."""

from __future__ import annotations

import json
import os
import select
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
from typing import Any


from hyprial.daemon.impl.configuration.home_guard import (
    ActiveDaemonHeartbeat,
)

BLOCKED_CHILD = Path(__file__).resolve().parents[4] / 'tests/fixtures/home/home_guard_blocked_child.py'

# Bounded startup guard for the blocked second daemon: interpreter start +
# the CLI import + the ``daemon run`` body's lazy daemon imports + the
# pre-ready test seam, up to the readiness byte.  This is a diagnostic
# bound, NOT the fast-rejection budget.  Idle-host readings put that phase
# at p50 ~4.5s / max ~5.7s (N=8+6, this branch, 2026-09-15); the CI unit
# job's 2-CPU quota historically pushed interpreter+import past the old
# single 15s budget (tasks 10155 / 10285), so the provisional guard here is
# 30s = ~5x the idle max, to be revisited with the container split
# distribution before the final PR freezes it.
HOME_GUARD_STARTUP_GUARD_SECONDS = 30.0

# Fast-rejection budget for ``HYPRIAL_HOME_IN_USE``: from the readiness
# byte (all imports done) to the exit-1 JSON payload -- home migration
# check, state-dir fence, ``DaemonApplication.from_environment`` up to the
# home-guard claim, and the payload.  Idle-host readings: p50 ~2.6s /
# max ~3.0s.  Provisional 15s = the old single total budget, i.e. strictly
# no tighter on the path it guards, while startup can no longer steal from
# it; to be revisited with the container split distribution before the
# final PR freezes it.
HOME_GUARD_REJECTION_BUDGET_SECONDS = 15.0


















def _record(*, pid: int, identity: str, heartbeat: float) -> dict[str, Any]:
    return {
        "schemaVersion": 1,
        "pid": pid,
        "processIdentity": identity,
        "generation": "other-generation",
        "heartbeatAt": "2026-08-24T00:00:00Z",
        "heartbeatMonotonic": heartbeat,
        "keepaliveDurationSeconds": 5.0,
    }


















def _wait_for(predicate: Any, *, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError("condition did not become true")


def _daemon_environment(home: Path, state: Path, node: str) -> dict[str, str]:
    from tests.support.runtime.environment import isolated_environment

    environment = isolated_environment(home.parent)
    for name in tuple(environment):
        if name.startswith("HYPRIAL_") or name.startswith("HARNESS_"):
            environment.pop(name)
    environment.update(
        {
            "HYPRIAL_HOME": str(home),
            "HARNESS_STATE_DIR": str(state),
            "HYPRIAL_NODE_ID": node,
            # A daemon needs a user identity to mint its actor URIs, and this
            # helper deliberately strips every HYPRIAL_* variable to isolate the
            # subprocess from ambient state -- including the one the unit
            # fixture exports.  The homes here are created with a bare
            # ``mkdir``, never ``hyprial init``, so no settings.json supplies it
            # either.  Before ``resolve_node_owner`` dropped its
            # ``getpass.getuser()`` fallback that gap was invisible: the host
            # login silently stood in for an identity nobody had configured.
            # Now an uninitialized home cannot run a daemon, which is the
            # intended behaviour -- so the identity is supplied explicitly.
            "HYPRIAL_OWNER": "home-guard-owner",
            "HYPRIAL_NETWORK_ISOLATED": "1",
            "HYPRIAL_PEER_DISCOVERY": "0",
            "HYPRIAL_ZENOH_LISTEN": "",
            "HYPRIAL_ZENOH_CONNECT": "",
            # The home-guard claim side treats a heartbeat as stale after
            # 2x the *claimer's* keepalive.  At 0.1s that window is 0.2s:
            # one fsync hiccup on the live owner's refresh loop made the
            # blocked second daemon read "stale", take the home over, and
            # serve forever -- a nondeterministic takeover that looks
            # exactly like a slow rejection (it is the second failure shape
            # hiding behind tasks 10155 / 10285).  1.0s keeps rejection
            # fast (the claim raises within one heartbeat period) while
            # making the false-stale window 2.0s.
            "HYPRIAL_DAEMON_KEEPALIVE_DURATION": "1.0",
        }
    )
    return environment


def _start_daemon(environment: dict[str, str]) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [sys.executable, "-m", "hyprial.cli", "daemon", "run", "--json"],
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        text=True,
    )


def _stop_daemon(process: subprocess.Popen[str]) -> None:
    if process.poll() is None:
        os.kill(process.pid, signal.SIGTERM)
    try:
        process.wait(timeout=15)
    except subprocess.TimeoutExpired:
        # An unreaped child PID cannot be reused; kill only this test's child.
        os.kill(process.pid, signal.SIGKILL)
        process.wait(timeout=5)


def _short_temp_root() -> str:
    """A writable short root for AF_UNIX sockets (same shape as the E2E
    runners' short_temp_root): Darwin's 104-byte sun_path limit rules out
    pytest's tmp_path, and /private/tmp does not exist on the Linux CI."""

    configured = os.environ.get("HYPRIAL_TEST_TMP_ROOT")
    if configured:
        # CI points this at the /ci-tmpfs RAM disk: daemons started on the
        # shared btrfs stall on its commits during startup (infra-ops,
        # 2026-09-28).  Locally it is unset and the lookup below is unchanged.
        return os.path.realpath(configured)
    for candidate in ("/private/tmp", "/tmp"):
        if os.path.isdir(candidate) and os.access(candidate, os.W_OK | os.X_OK):
            return candidate
    return tempfile.gettempdir()


def _read_probe_phases(probe_path: Path) -> dict[str, float]:
    try:
        loaded = json.loads(probe_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(loaded, dict):
        return {}
    return {
        key: float(value)
        for key, value in loaded.items()
        if isinstance(value, (int, float))
    }


#: Keepalive of the blocked (claiming) child in ``_run_blocked_daemon``; the
#: production default, so its false-stale window is 10s.
CLAIMER_KEEPALIVE_SECONDS = 5.0


def _run_blocked_daemon(
    environment: dict[str, str], *, pre_ready_delay: float | None = None
) -> tuple[float, float, subprocess.CompletedProcess[str], dict[str, float]]:
    """Launch the blocked second daemon through the readiness handshake.

    Returns ``(startup_seconds, rejection_seconds, result, phases)`` where
    ``startup_seconds`` covers process creation to the readiness byte --
    interpreter start, the CLI import, and the ``daemon run`` body's lazy
    daemon imports (bounded by ``HOME_GUARD_STARTUP_GUARD_SECONDS``) -- and
    ``rejection_seconds`` covers the readiness byte to process exit
    (bounded by ``HOME_GUARD_REJECTION_BUDGET_SECONDS``): home migration
    check, state-dir fence, ``DaemonApplication.from_environment`` up to
    the home-guard claim, and the exit-1 JSON.  ``phases`` carries the
    child's monotonic probe stamps when attribution is needed.  Splitting
    the two budgets is the point: startup and import latency can no longer
    consume the fast-failure budget that guards ``HYPRIAL_HOME_IN_USE``.
    """
    read_fd, write_fd = os.pipe()
    probe_path = Path(environment["HYPRIAL_HOME"]).parent / "blocked-child-probe.json"
    child_environment = dict(environment)
    # The claimer judges the owner's heartbeat stale at 2x the CLAIMER's
    # keepalive.  With the shared 1.0s that window is 2s, and one CI stall
    # of the live owner's refresh longer than that let the blocked child
    # take the home over (task 15512: "took the home over instead of
    # rejecting").  The claimer runs the production default instead: a 10s
    # false-stale window.  Rejection speed is unchanged -- the claim loop
    # raises as soon as the OWNER's heartbeat advances, and the owner still
    # refreshes every 1.0s.
    child_environment["HYPRIAL_DAEMON_KEEPALIVE_DURATION"] = str(
        CLAIMER_KEEPALIVE_SECONDS
    )
    child_environment["HYPRIAL_HOME_GUARD_READY_FD"] = str(write_fd)
    child_environment["HYPRIAL_HOME_GUARD_CHILD_PROBE"] = str(probe_path)
    if pre_ready_delay is not None:
        child_environment["HYPRIAL_HOME_GUARD_CHILD_PRE_READY_DELAY"] = str(
            pre_ready_delay
        )
    process = subprocess.Popen(
        [sys.executable, str(BLOCKED_CHILD)],
        env=child_environment,
        pass_fds=(write_fd,),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    os.close(write_fd)
    try:
        startup_started = time.monotonic()
        startup_deadline = startup_started + HOME_GUARD_STARTUP_GUARD_SECONDS
        while True:
            remaining = startup_deadline - time.monotonic()
            assert remaining > 0, (
                "blocked daemon child never reached CLI-import readiness "
                f"within {HOME_GUARD_STARTUP_GUARD_SECONDS}s"
            )
            readable, _, _ = select.select([read_fd], [], [], remaining)
            assert readable, (
                "blocked daemon child never reached CLI-import readiness "
                f"within {HOME_GUARD_STARTUP_GUARD_SECONDS}s"
            )
            chunk = os.read(read_fd, 1)
            if chunk == b"R":
                break
            raise AssertionError(
                f"unexpected readiness pipe data from child: {chunk!r}"
            )
        startup_seconds = time.monotonic() - startup_started

        rejection_started = time.monotonic()
        try:
            stdout, stderr = process.communicate(
                timeout=HOME_GUARD_REJECTION_BUDGET_SECONDS
            )
        except subprocess.TimeoutExpired as error:
            stuck_at = _read_probe_phases(probe_path)
            process.kill()
            process.wait()
            takeover = False
            try:
                marker = json.loads(
                    (Path(environment["HYPRIAL_HOME"]) / ".active_daemon").read_text(
                        encoding="utf-8"
                    )
                )
                takeover = marker.get("pid") == process.pid
            except (OSError, ValueError):
                takeover = False
            detail = (
                "the blocked child took the home over instead of rejecting "
                "(the owner heartbeat went stale mid-read: a live owner's "
                "refresh hiccup raced the claim)"
                if takeover
                else "the bounded path (fence, from_environment, claim) hung"
            )
            raise AssertionError(
                "blocked daemon exceeded the fast-rejection budget "
                f"({HOME_GUARD_REJECTION_BUDGET_SECONDS}s) after CLI-import "
                f"readiness: {detail}; child phase stamps: {stuck_at}"
            ) from error
        rejection_seconds = time.monotonic() - rejection_started
        result = subprocess.CompletedProcess(
            process.args, process.returncode, stdout, stderr
        )
        phases = _read_probe_phases(probe_path)
        print(
            "home_guard blocked-daemon split: "
            f"startup={startup_seconds:.3f}s (guard {HOME_GUARD_STARTUP_GUARD_SECONDS}s) "
            f"rejection={rejection_seconds:.3f}s "
            f"(budget {HOME_GUARD_REJECTION_BUDGET_SECONDS}s) "
            f"phases={ {k: round(v, 3) for k, v in sorted(phases.items())} }",
            flush=True,
        )
        return startup_seconds, rejection_seconds, result, phases
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        # communicate() closes these only when it returns normally.  On the
        # startup-guard and rejection-timeout paths it never did, so both
        # pipes stayed open and _fd_hygiene added a second, misleading
        # teardown ERROR to the real failure.
        for stream in (process.stdout, process.stderr):
            if stream is not None:
                stream.close()
        os.close(read_fd)




# --------------------------------------------------------------------------
# Copied-home duplicate detection: snapshot at claim + windowed background check
# --------------------------------------------------------------------------


def _copied_home_guard(
    home: Path,
    *,
    now: list[float],
    process_status: Any,
    detected: list[dict[str, Any]],
    check_failed: list[str] | None = None,
    window: float = 10.0,
    sleeper: Any = None,
) -> ActiveDaemonHeartbeat:
    def default_sleeper(duration: float) -> None:
        now[0] += duration

    return ActiveDaemonHeartbeat(
        home,
        keepalive_duration=5.0,
        claim_wait_timeout=0.0,
        pid=111,
        monotonic_clock=lambda: now[0],
        identity_reader=lambda _pid: "self",
        process_status=process_status,
        duplicate_detected=detected.append,
        duplicate_check_failed=(
            check_failed.append if check_failed is not None else None
        ),
        duplicate_observe_window=window,
        sleeper=sleeper or default_sleeper,
    )


def _write_record(home: Path, *, pid: int, identity: str, generation: str) -> None:
    record = _record(pid=pid, identity=identity, heartbeat=100.0)
    record["generation"] = generation
    (home / ".active_daemon").write_text(json.dumps(record), encoding="utf-8")


def _wait_detected(detected: list[dict[str, Any]], *, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not detected and time.monotonic() < deadline:
        time.sleep(0.01)
    assert detected, "duplicate check did not alert within the timeout"






















# Shared builders/data and fixture registrations used by the focused modules.
__all__ = [
    'BLOCKED_CHILD',
    'CLAIMER_KEEPALIVE_SECONDS',
    'HOME_GUARD_REJECTION_BUDGET_SECONDS',
    'HOME_GUARD_STARTUP_GUARD_SECONDS',
    '_copied_home_guard',
    '_daemon_environment',
    '_read_probe_phases',
    '_record',
    '_run_blocked_daemon',
    '_short_temp_root',
    '_start_daemon',
    '_stop_daemon',
    '_wait_detected',
    '_wait_for',
    '_write_record',
]
