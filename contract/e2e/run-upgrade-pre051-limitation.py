#!/usr/bin/env python3
"""Explicit expected failure for a pre-v0.5.1 channel; not a release gate pass."""

import os
from pathlib import Path
import subprocess
import sys

BASELINE = "bbe80d4402394916a87f54ca2a605d0bdc71e0e1"


def expected_failure(code: int, output: str) -> bool:
    return (
        code == 1
        and "RESULT PASS E2E-010 old-channel-running" in output
        and any(
            line.startswith("RESULT FAIL E2E-010 upgrade-continuity ")
            and "Channel probe exited during upgrade-auto-restart" in line
            and "ModuleNotFoundError: No module named 'rich.table'" in line
            for line in output.splitlines()
        )
    )


def main() -> int:
    result = subprocess.run(
        [sys.executable, str(Path(__file__).with_name("run-upgrade-continuity.py"))],
        env={**os.environ, "HYPRIAL_E2E_UPGRADE_FROM": BASELINE,
             "HYPRIAL_E2E_UPGRADE_CASE": "known-limitation-pre051"},
        text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=1800,
    )
    print(result.stdout, end="")
    if not expected_failure(result.returncode, result.stdout):
        print("RESULT FAIL KNOWN-LIMITATION pre-v0.5.1-channel: expected rich.table failure was not observed")
        return 1
    print("RESULT PASS KNOWN-LIMITATION pre-v0.5.1-channel: expected rich.table failure observed; reconnect required; not supported-upgrade evidence")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
