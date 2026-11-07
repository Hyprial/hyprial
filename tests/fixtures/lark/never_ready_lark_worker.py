#!/usr/bin/env python3
"""A Lark worker that stays alive but never signals ready on the dedicated fd.

It writes the ready signal to STDOUT only -- the channel the daemon *drains and
discards*, never the dedicated ``HYPRIAL_LARK_READY_FD`` the launcher reads.  This
reproduces both an adapter that can never reach its gateway and the historical
stale-fixture defect (a worker built before PR #30 moved the handshake to the
fd).  The daemon must not let such a worker wedge its dispatcher.
"""

from __future__ import annotations

import json
import signal
import sys
import time

running = True


def stop(_signum: int, _frame: object) -> None:
    global running
    running = False


signal.signal(signal.SIGTERM, stop)
signal.signal(signal.SIGINT, stop)
name = sys.argv[-1]
# Deliberately the WRONG channel: stdout, not HYPRIAL_LARK_READY_FD.
print(json.dumps({"status": "online", "name": name}), flush=True)
while running:
    time.sleep(0.05)
