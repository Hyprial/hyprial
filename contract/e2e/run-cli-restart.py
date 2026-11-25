#!/usr/bin/env python3
"""Isolated smoke for the public ``hyprial daemon restart`` command."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _daemon_runner import IsolatedDaemon, hyprial_executable, scenario_tempdir


def main() -> int:
    with scenario_tempdir("cli-restart") as root:
        daemon = IsolatedDaemon(hyprial_executable(), root, "cli-restart-node")
        try:
            started = daemon.start()
            before_pid = started.get("pid")
            if not isinstance(before_pid, int) or before_pid <= 0:
                raise AssertionError(f"initial daemon has no valid pid: {started}")
            if not isinstance(started.get("version"), str):
                raise AssertionError(f"initial daemon has no version: {started}")

            restarted = daemon.command("daemon", "restart", "--yes", "--json", timeout=45.0)
            after = restarted.get("after")
            if not isinstance(after, dict) or after.get("pid") == before_pid:
                raise AssertionError(f"restart did not yield a fresh pid: {restarted}")
            if not isinstance(after.get("version"), str):
                raise AssertionError(f"restart report has no daemon version: {restarted}")
            status = daemon.ps(transient_timeout=15.0)
            current = status.get("daemon")
            if not isinstance(current, dict) or current.get("pid") != after.get("pid"):
                raise AssertionError(f"ps did not answer from the new daemon: {status}")
            print(
                "RESULT PASS cli-restart "
                f"before={before_pid} after={after['pid']} ps=answering"
            )
            return 0
        finally:
            daemon.stop()
            daemon.wait_stopped()


if __name__ == "__main__":
    raise SystemExit(main())
