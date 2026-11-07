#!/usr/bin/env python3
"""Deterministic child process for daemon adapter lifecycle tests."""

from __future__ import annotations

import json
import os
import signal
import sys
import time
from pathlib import Path


running = True


def stop(_signum: int, _frame: object) -> None:
    global running
    running = False


signal.signal(signal.SIGTERM, stop)
signal.signal(signal.SIGINT, stop)
name = sys.argv[-1]
ready_delay = float(os.environ.get("HYPRIAL_FAKE_LARK_READY_DELAY", "0"))
ready_release = os.environ.get("HYPRIAL_FAKE_LARK_READY_RELEASE")
if ready_delay > 0 or ready_release:
    # Test-only synchronization seam.  The production launcher never sets this
    # variable; regression tests use it to prove that the daemon has entered a
    # pre-ready gate without relying on import time or scheduler timing.
    delay_probe = os.environ.get("HYPRIAL_FAKE_LARK_DELAY_PROBE")
    if delay_probe:
        Path(delay_probe).write_text(
            json.dumps(
                {
                    "nonce": os.environ.get("HYPRIAL_FAKE_LARK_DELAY_NONCE"),
                    "workerPid": os.getpid(),
                    "daemonMarkerExisted": (
                        Path(os.environ["HARNESS_STATE_DIR"]) / "daemon.json"
                    ).exists(),
                }
            ),
            encoding="utf-8",
        )
    if ready_release:
        deadline = time.monotonic() + 30.0
        while running and not Path(ready_release).exists():
            if time.monotonic() >= deadline:
                raise TimeoutError("test never released worker readiness")
            time.sleep(0.02)
    else:
        time.sleep(ready_delay)
if running:
    # Simulate the real Lark SDK, which writes its own connection log to stdout.
    # The ready signal must survive sharing the process with such noise.
    print(
        "[Lark] [2026-08-08 19:52:50,488] [INFO] connected to "
        "wss://msg-frontier.feishu.cn/...",
        flush=True,
    )
    payload = json.dumps({"status": "online", "name": name})
    ready_fd = os.environ.get("HYPRIAL_LARK_READY_FD")
    if ready_fd:
        os.write(int(ready_fd), payload.encode("utf-8") + b"\n")
    else:
        print(payload, flush=True)
while running:
    time.sleep(0.05)

# Optional exact cleanup proof for subprocess lifecycle regressions.  The
# production launcher never sets this variable; a test that owns this worker
# can require the marker before deleting its isolated state root.
stop_probe = os.environ.get("HYPRIAL_FAKE_LARK_STOP_PROBE")
if stop_probe:
    Path(stop_probe).write_text(
        json.dumps(
            {
                "nonce": os.environ.get("HYPRIAL_FAKE_LARK_DELAY_NONCE"),
                "workerPid": os.getpid(),
            }
        ),
        encoding="utf-8",
    )
