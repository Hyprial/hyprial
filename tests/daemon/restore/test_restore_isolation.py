"""A persisted interactive-session conflict is isolated to that one agent.

This is the 2026-09-13 production shape, built from the current desired-state
schema in an isolated home: the same agent has a running headless pi record
and an old Codex interactive-session record, while a second pi agent is the
healthy control. The cleanup case also carries a live-process session and an
unreadable-birth-marker session, so dead, alive, and unknown are observed by
the real process probe in one daemon start. The daemon is a real subprocess
and both pi workers use the repository's existing protocol double.
"""

from __future__ import annotations

from tests.support.runtime._tmp import hyprial_test_tmp_root

import json
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from hyprial.daemon.impl.desired_state import (
    DesiredState,
    DesiredStateStore,
    InteractiveSession,
)

from hyprial.kernel import HarnessLaunchSpec
from hyprial.daemon.impl.state_db import StateDatabase
from tests.daemon.channels.mcp.lifecycle.test_mcp_channel_owner_migration_e2e import IsolatedHarness


OWNER = "restore-owner"
NODE = "node-e2e"
CONFLICT = "conflict"
HEALTHY = "healthy"
CONFLICT_URI = f"agent:{OWNER}:{NODE}:{CONFLICT}"
LIVE = "live-session"
UNKNOWN = "unknown-session"
LIVE_URI = f"agent:{OWNER}:{NODE}:{LIVE}"
UNKNOWN_URI = f"agent:{OWNER}:{NODE}:{UNKNOWN}"


def _persistent_rows(state: Path) -> dict[str, list[tuple[Any, ...]]]:
    database = state / "lifecycle-operations.sqlite3"
    with sqlite3.connect(database) as connection:
        return {
            "harnesses": connection.execute(
                "SELECT * FROM harnesses ORDER BY harness, name"
            ).fetchall(),
            "interactive_sessions": connection.execute(
                "SELECT * FROM interactive_sessions ORDER BY actor"
            ).fetchall(),
        }


def _seed_desired_state(
    harness: IsolatedHarness,
    *,
    conflict: bool,
    dead_fence: tuple[int, str] | None = None,
    live_fence: tuple[int, str] | None = None,
) -> None:
    fake_pi = Path(__file__).resolve().parents[3] / 'tests/fixtures/harnesses/fake_pi_rpc.py'
    command = (sys.executable, str(fake_pi))
    store = DesiredStateStore(
        harness.state / "desired-state.json",
        state_db=StateDatabase(harness.state / "lifecycle-operations.sqlite3"),
    )
    interactive_sessions: tuple[InteractiveSession, ...] = ()
    if conflict:
        interactive_sessions = (
            InteractiveSession(
                actor=CONFLICT_URI,
                cwd=str(harness.root),
                command=(sys.executable, "-m", "codex_app_server"),
                source="codex-app-server",
                session_ref="old-codex-session",
                runtime="codex_interactive",
                process_pid=dead_fence[0] if dead_fence else None,
                process_identity=dead_fence[1] if dead_fence else None,
            ),
            *(
                (
                    InteractiveSession(
                        actor=LIVE_URI,
                        cwd=str(harness.root),
                        command=(sys.executable, "-m", "codex_app_server"),
                        source="codex-app-server",
                        session_ref="live-codex-session",
                        runtime="codex_interactive",
                        process_pid=live_fence[0],
                        process_identity=live_fence[1],
                    ),
                    InteractiveSession(
                        actor=UNKNOWN_URI,
                        cwd=str(harness.root),
                        command=(sys.executable, "-m", "codex_app_server"),
                        source="codex-app-server",
                        session_ref="unknown-codex-session",
                        runtime="codex_interactive",
                        process_pid=live_fence[0],
                        # A future marker scheme has no shared component with
                        # this build's reader: the real probe returns UNKNOWN.
                        process_identity="future-process-birth:opaque",
                    ),
                )
                if live_fence is not None
                else ()
            ),
        )
    store.save(
        DesiredState(
            harnesses=(
                HarnessLaunchSpec(
                    harness="pi",
                    name=CONFLICT,
                    headless=True,
                    cwd=str(harness.root),
                    session_ref="fake",
                    command=command,
                ),
                HarnessLaunchSpec(
                    harness="pi",
                    name=HEALTHY,
                    headless=True,
                    cwd=str(harness.root),
                    session_ref="fake",
                    command=command,
                ),
            ),
            interactive_sessions=interactive_sessions,
        )
    )


def _daemon_events(harness: IsolatedHarness) -> list[dict[str, Any]]:
    log = harness.state / "logs" / "daemon.jsonl"
    return [json.loads(line) for line in log.read_text().splitlines()]


def _start_fixture(
    *, conflict: bool, fenced_conflict: bool = True
) -> tuple[IsolatedHarness, Path, tuple[subprocess.Popen[bytes], ...]]:
    root = Path(tempfile.mkdtemp(dir=hyprial_test_tmp_root(), prefix="hyprial-restore-isolation-"))
    harness = IsolatedHarness(root)
    harness.environment.update(
        {
            "FAKE_PI_MODE": "settled",
            "HYPRIAL_DAEMON_KEEPALIVE_DURATION": "0.1",
        }
    )
    harness.write_owner(OWNER)
    owners: tuple[subprocess.Popen[bytes], ...] = ()
    dead_fence: tuple[int, str] | None = None
    live_fence: tuple[int, str] | None = None
    if conflict and fenced_conflict:
        from hyprial.daemon.impl.mcp.channel.ownership import (
            _owner_process_status,
            _read_process_identity,
        )

        dead = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)"])
        live = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)"])
        owners = (dead, live)
        dead_identity = _read_process_identity(dead.pid)
        live_identity = _read_process_identity(live.pid)
        assert dead_identity is not None
        assert live_identity is not None
        assert _owner_process_status(live.pid, live_identity).value == "alive"
        assert (
            _owner_process_status(
                live.pid, "future-process-birth:opaque"
            ).value
            == "unknown"
        )
        dead_fence = (dead.pid, dead_identity)
        live_fence = (live.pid, live_identity)
        dead.kill()
        dead.wait(timeout=5.0)
    _seed_desired_state(
        harness,
        conflict=conflict,
        dead_fence=dead_fence,
        live_fence=live_fence,
    )
    return harness, root, owners


def _stop_fixture(
    harness: IsolatedHarness,
    root: Path,
    owners: tuple[subprocess.Popen[bytes], ...],
) -> None:
    try:
        for name in (CONFLICT, HEALTHY):
            try:
                harness.ipc("down", {"name": name, "provider": "pi"})
            except Exception:  # noqa: BLE001 - cleanup must not mask the proof
                pass
        if harness.daemon is not None:
            harness.stop_daemon()
    finally:
        for process in owners:
            if process.poll() is not None:
                continue
            process.terminate()
            try:
                process.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5.0)
        shutil.rmtree(root, ignore_errors=True)


def test_conflicting_interactive_recovery_is_isolated_and_reported() -> None:
    harness, root, owners = _start_fixture(conflict=True)
    before = _persistent_rows(harness.state)
    try:
        harness.start_daemon()

        status = harness.ipc("ps", {})
        events = _daemon_events(harness)
        event_names = [item.get("event") for item in events]
        failures = [
            item for item in events if item.get("event") == "agent.recovery.failed"
        ]
        cleanups = [
            item for item in events if item.get("event") == "agent.recovery.cleaned"
        ]
        agents = {item["actor"]: item for item in status["agents"]}
        connectors = {item["name"]: item for item in status["connectors"]}

        assert "daemon.ready" in event_names
        assert failures == []
        assert len(cleanups) == 1
        assert {
            key: cleanups[0][key]
            for key in (
                "actor",
                "source",
                "harness",
                "runtime",
                "sessionId",
                "processPid",
            )
        } == {
            "actor": CONFLICT_URI,
            "source": "codex-app-server",
            "harness": "codex",
            "runtime": "codex_interactive",
            "sessionId": "old-codex-session",
            "processPid": owners[0].pid,
        }
        assert cleanups[0]["liveness"] in {"pid-missing", "identity-mismatch"}
        assert connectors[CONFLICT]["running"] is True
        assert connectors[HEALTHY]["running"] is True
        assert agents[CONFLICT]["status"] == "online"
        assert agents[CONFLICT]["recoveryStatus"] == "cleaned"
        assert agents[CONFLICT]["recoveryCleanup"]["liveness"] in {
            "pid-missing",
            "identity-mismatch",
        }
        assert agents[HEALTHY]["status"] == "online"
        assert "recoveryStatus" not in agents[HEALTHY]
        sessions = {
            item["actor"]: item for item in status["interactiveSessions"]
        }
        assert CONFLICT_URI not in sessions
        assert sessions[LIVE_URI]["sessionRef"] == "live-codex-session"
        assert sessions[UNKNOWN_URI]["sessionRef"] == "unknown-codex-session"
        endpoint_event = next(
            item for item in events if item.get("event") == "zenoh.endpoints.resolved"
        )
        assert endpoint_event["discovery"] == "disabled"
        assert endpoint_event["discovered"] == 0
        after = _persistent_rows(harness.state)
        assert after["harnesses"] == before["harnesses"]
        assert {row[0] for row in before["interactive_sessions"]} == {
            CONFLICT_URI,
            LIVE_URI,
            UNKNOWN_URI,
        }
        assert {row[0] for row in after["interactive_sessions"]} == {
            LIVE_URI,
            UNKNOWN_URI,
        }
        before_by_actor = {
            row[0]: row for row in before["interactive_sessions"]
        }
        after_by_actor = {
            row[0]: row for row in after["interactive_sessions"]
        }
        assert after_by_actor[LIVE_URI] == before_by_actor[LIVE_URI]
        assert after_by_actor[UNKNOWN_URI] == before_by_actor[UNKNOWN_URI]
    finally:
        _stop_fixture(harness, root, owners)


def test_unfenced_conflicting_session_remains_for_operator_resolution() -> None:
    harness, root, owners = _start_fixture(
        conflict=True, fenced_conflict=False
    )
    before = _persistent_rows(harness.state)
    try:
        harness.start_daemon()

        status = harness.ipc("ps", {})
        events = _daemon_events(harness)
        failures = [
            item for item in events if item.get("event") == "agent.recovery.failed"
        ]
        connectors = {item["name"]: item for item in status["connectors"]}
        agents = {item["actor"]: item for item in status["agents"]}

        assert all(
            item.get("event") != "agent.recovery.cleaned" for item in events
        )
        assert len(failures) == 1
        assert failures[0]["actor"] == CONFLICT_URI
        assert failures[0]["errorCode"] == "AGENT_ALREADY_RUNNING"
        assert connectors[CONFLICT]["running"] is True
        assert connectors[HEALTHY]["running"] is True
        assert agents[CONFLICT]["recoveryStatus"] == "failed"
        assert _persistent_rows(harness.state) == before
    finally:
        _stop_fixture(harness, root, owners)


def test_recovery_without_a_conflict_is_a_clean_noop() -> None:
    harness, root, owners = _start_fixture(conflict=False)
    try:
        harness.start_daemon()

        status = harness.ipc("ps", {})
        events = _daemon_events(harness)
        agents = {item["actor"]: item for item in status["agents"]}

        assert all(item.get("event") != "agent.recovery.failed" for item in events)
        assert {CONFLICT, HEALTHY}.issubset(agents)
        assert all(agents[name]["status"] == "online" for name in (CONFLICT, HEALTHY))
        assert all("recoveryStatus" not in agents[name] for name in (CONFLICT, HEALTHY))
    finally:
        _stop_fixture(harness, root, owners)
