"""Test-launched entry point: one bootstrap wire host in its own process.

Decision: daemon-first-z1-process-entry-20261115.  This is a test helper, not a
CLI command.  It opens the ownerless home it is given, prints ``READY`` once the
socket answers, stops on SIGTERM and prints how serving ended.
"""

from __future__ import annotations

import signal
import sys
from pathlib import Path

from hyprial.daemon import BootstrapWireHost


def main(home: str, state: str) -> int:
    host = BootstrapWireHost.open(Path(home), state_dir=Path(state))
    signal.signal(signal.SIGTERM, lambda *_: host.request_stop())
    print("READY", flush=True)
    try:
        outcome = host.serve()
    finally:
        host.close()
    print(f"OUTCOME {outcome}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1], sys.argv[2]))
