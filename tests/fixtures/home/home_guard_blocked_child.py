#!/usr/bin/env python3
"""Test-only launcher for the blocked second daemon in home-guard tests.

``daemon run --json`` under an owned home must reject with
``HYPRIAL_HOME_IN_USE``.  The rejection itself is fast: the live owner's
heartbeat advances twice per claim re-read, so the claim loop raises on its
next iteration.  But ``python -m hyprial.cli`` first pays interpreter
startup plus the CLI import, and the ``daemon run`` command body lazily
imports the daemon application modules; the previous single
``subprocess.run(..., timeout=15)`` let all of that startup latency consume
the failure-path budget (CI tasks 10155 / 10285: "timed out after 15
seconds" on a healthy guard, under the CI unit job's 2-CPU cgroup).

This launcher separates the phases: it imports the CLI *and every module
the ``daemon run`` body imports lazily*, then writes one readiness byte on
a dedicated inherited FD (stdout stays a clean JSON channel), and only then
runs the CLI main exactly as ``-m hyprial.cli`` would.  The parent's
fast-rejection budget starts at the byte, so interpreter, CLI-import and
daemon-import latency can no longer inherit it; what remains inside the
budget is the bounded path: home migration check, state-dir ownership
fence, ``DaemonApplication.from_environment`` up to the home-guard claim,
and the exit-1 JSON payload.

``HYPRIAL_HOME_GUARD_CHILD_PRE_READY_DELAY`` sleeps *before* the byte.  It
exists only for the mutation guard: a pre-readiness delay must stay green,
proving the delay lands outside the rejection budget.

``HYPRIAL_HOME_GUARD_CHILD_PROBE``, when set, is a path this child writes
phase timestamps to (spawn, cli_imported, daemon_imported, byte_written,
main_returned) so a measurement run can attribute the wall time without a
debugger.
"""

from __future__ import annotations

import json
import os
import sys
import time

_T0 = time.monotonic()


def _stamp(phase: str) -> None:
    """Append a monotonic phase timestamp for measurement runs (no-op by default)."""
    path = os.environ.get("HYPRIAL_HOME_GUARD_CHILD_PROBE")
    if not path:
        return
    record: dict[str, float] = {}
    try:
        with open(path) as handle:
            loaded = json.load(handle)
        if isinstance(loaded, dict):
            record = loaded
    except (OSError, ValueError):
        record = {}
    record[phase] = time.monotonic() - _T0
    with open(path, "w") as handle:
        json.dump(record, handle)


def main() -> None:
    _stamp("spawn")
    pre_ready_delay = os.environ.get("HYPRIAL_HOME_GUARD_CHILD_PRE_READY_DELAY")
    if pre_ready_delay is not None:
        time.sleep(float(pre_ready_delay))
    # Import cost is exactly what the readiness handshake excludes from the
    # rejection budget; pay ALL of it before signalling readiness.  These
    # are the same modules the ``daemon run`` body imports lazily, so after
    # the byte no first-import latency remains on the measured path.
    from hyprial.shell.impl.cli.runtime import entry as cli
    from hyprial.daemon.impl.application import DaemonApplication  # noqa: F401
    from hyprial.daemon.impl.application import _lock_wait_timeout  # noqa: F401
    from hyprial.daemon.impl.configuration.ownership import (  # noqa: F401
        DaemonOwnershipBusy,
        DaemonStateOwnershipFence,
    )

    _stamp("cli_imported")
    _stamp("daemon_imported")
    os.write(int(os.environ["HYPRIAL_HOME_GUARD_READY_FD"]), b"R")
    _stamp("byte_written")

    sys.argv = [sys.argv[0], "daemon", "run", "--json"]
    try:
        cli.main()
    finally:
        _stamp("main_returned")


if __name__ == "__main__":
    main()
