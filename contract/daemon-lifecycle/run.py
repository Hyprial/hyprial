#!/usr/bin/env python3
"""Public daemon lifecycle contract independent of harness implementations."""

from __future__ import annotations

import json
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from hyprial.daemon import DesiredStateStore  # noqa: E402
from hyprial.daemon.impl.runtime import DaemonEventBridge  # noqa: E402
from hyprial.daemon.impl.processes.supervisor import ManagedHarnessRuntime  # noqa: E402
from hyprial.daemon.impl.application.messaging.status.refresh import (  # noqa: E402
    CoalescedRefreshAuthority,
)


class Process:
    running = True

    def stop(self) -> None:
        self.running = False


class Launcher:
    def __init__(self) -> None:
        self.starts = 0

    def start(self, _spec):  # type: ignore[no-untyped-def]
        self.starts += 1
        return Process()


class Transport:
    def declare_liveliness(self, _key: str):  # type: ignore[no-untyped-def]
        return Process()

    def close(self) -> None:
        pass


class Inbox:
    def retry_due(self):  # type: ignore[no-untyped-def]
        return []

    def close(self) -> None:
        pass


def exercise(root: Path, *, crash_marker: bool) -> None:
    """Two halves of daemon recovery, at the seams that actually own them.

    This contract used to assert that ``DaemonEventBridge.start`` restored the
    declared harnesses.  It stopped doing that deliberately -- see
    ``tests/daemon/application/composition/runtime/test_daemon_runtime.py::
    test_event_bridge_start_never_restores_harness_processes`` -- and the job
    moved to whoever composes the daemon.  Asserting the old arrangement left
    this contract red for 38 hours while the real defect (nobody had taken the
    job up in ``application.py``) went unexamined behind it.

    ⚠️ The property has not been dropped, it has moved: "daemon startup asks
    for the restore" is now ``tests/daemon/application/restore/test_daemon_harness_recovery.py::
    test_daemon_startup_actually_asks_for_the_restore``, which lives in the
    suite people run.  What stays here is what this composition can honestly
    observe -- that start() does not restore, and that restore() does.
    """

    root.mkdir(parents=True)
    desired = root / "desired-state.json"
    Path(f"{desired}.v1").write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "asMailbox": False,
                "providers": [
                    {
                        "provider": "pi",
                        "name": "contract-agent",
                        "headless": True,
                        "args": [],
                        "ownership": "managed",
                    }
                ],
                "interactiveSessions": [],
            }
        ),
        encoding="utf-8",
    )
    if crash_marker:
        (root / "daemon-run.json").write_text('{"schemaVersion":1,"pid":1}')
    launcher = Launcher()
    runtime = DaemonEventBridge(
        state_dir=root,
        node_id="contract-node",
        desired_state=DesiredStateStore(desired),
        transport=Transport(),
        inbox=Inbox(),
        harnesses=ManagedHarnessRuntime(launcher),
    )
    result = runtime.start()

    # Half one: the event bridge does not start harness processes.  Asserted
    # rather than assumed, because the summary now reports 0 unconditionally
    # -- reading those numbers proves nothing, and launcher.starts is the only
    # observation here that can tell "did not restore" from "cannot report".
    assert launcher.starts == 0, (
        "DaemonEventBridge.start restored a harness; that responsibility moved "
        "to the composer, and two owners is what the deleted harness_selector "
        "existed to prevent"
    )
    assert result.attempted == result.restored == result.failed == 0
    assert result.previous_run_unclean is crash_marker

    # Half two: the recovery machinery itself works when asked.  Without this
    # the contract would pass just as happily if restore() were broken, which
    # is most of what made the outage hard to see -- everything was reachable
    # and correct, and nothing called it.
    restored = runtime.harnesses.restore(DesiredStateStore(desired).load().harnesses)
    assert (restored.attempted, restored.restored, restored.failed) == (1, 1, 0)
    assert launcher.starts == 1

    runtime.stop()


def exercise_status_projection() -> None:
    now_ms = [10_000]
    authority = CoalescedRefreshAuthority(
        stale_after_ms=1_000,
        expire_after_ms=2_000,
        settlement_limit=2,
        settlement_retention_ms=1_000,
        clock_ms=lambda: now_ms[0],
    )
    authority.record_observation("host:peer", "online")
    assert authority.status("host:peer")["stale"] is False
    now_ms[0] = 11_001
    assert authority.status("host:peer")["stale"] is True
    assert authority.status("host:peer")["expired"] is False
    now_ms[0] = 12_001
    assert authority.status("host:peer")["expired"] is True

    admission = authority.request("endpoint", lambda: {"state": "open"})
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        status = authority.status("endpoint")
        if status["refreshState"] == "settled":
            break
        time.sleep(0.01)
    else:
        raise AssertionError("status refresh did not settle")
    assert status["refreshOperationId"] == admission.operation_id
    assert authority.snapshot("endpoint") == {"state": "open"}

    later = authority.request("endpoint", lambda: {"state": "later"})
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        later_status = authority.status(
            "endpoint", operation_id=later.operation_id
        )
        if later_status["refreshState"] == "settled":
            break
        time.sleep(0.01)
    else:
        raise AssertionError("later status refresh did not settle")
    earlier_status = authority.status(
        "endpoint", operation_id=admission.operation_id
    )
    assert earlier_status["refreshOperationId"] == admission.operation_id
    assert earlier_status["refreshState"] == "settled"
    assert earlier_status["lastError"] is None

    newest = authority.request("endpoint", lambda: {"state": "newest"})
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        if authority.status(
            "endpoint", operation_id=newest.operation_id
        )["refreshState"] == "settled":
            break
        time.sleep(0.01)
    else:
        raise AssertionError("newest status refresh did not settle")
    expired_by_count = authority.status(
        "endpoint", operation_id=admission.operation_id
    )
    assert expired_by_count["refreshState"] == "unknown-expired"
    assert expired_by_count["lastError"] is None
    now_ms[0] += 1_001
    assert authority.status(
        "endpoint", operation_id=later.operation_id
    )["refreshState"] == "unknown-expired"
    assert authority.close()


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="hyprial-daemon-contract-") as directory:
        root = Path(directory)
        exercise(root / "normal", crash_marker=False)
        exercise(root / "crash", crash_marker=True)
    exercise_status_projection()
    from run_state import exercise_run_state, exercise_run_state_display
    from turn_refresh import (
        exercise_contact_gap_acknowledgement,
        exercise_turn_refresh,
    )
    count = exercise_run_state()
    display_count = exercise_run_state_display()
    refresh_count = exercise_turn_refresh()
    gap_count = exercise_contact_gap_acknowledgement()
    print(f"RESULT PASS daemon lifecycle: restore 2/2; status freshness/settlement; run-state {count}/{count}; display {display_count}/{display_count}; turn-refresh {refresh_count}/{refresh_count}; gap-ack {gap_count}/{gap_count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
