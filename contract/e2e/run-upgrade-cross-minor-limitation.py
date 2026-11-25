#!/usr/bin/env python3
"""Expected pre-fix Python 3.12 -> 3.14 upgrade failure (card 3066f189)."""

import os
from pathlib import Path
import subprocess
import sys


def expected_failure(code: int, output: str) -> bool:
    return (
        code == 1
        and "RESULT PASS E2E-010 old-channel-running" in output
        and any(
            line.startswith("RESULT FAIL E2E-010 upgrade-continuity ")
            and "No module named" in line
            and "hyprial.identity.impl.identity_transaction" in line
            and "hyprial upgrade --json exited 1" in line
            for line in output.splitlines()
        )
    )


def main() -> int:
    if sys.version_info[:2] != (3, 12):
        print("RESULT BLOCKED KNOWN-LIMITATION cross-minor requires an initial Python 3.12 interpreter")
        return 2
    result = subprocess.run(
        [sys.executable, str(Path(__file__).with_name("run-upgrade-continuity.py"))],
        env={**os.environ, "HYPRIAL_E2E_UPGRADE_FROM": "v0.5.2",
             "HYPRIAL_E2E_UPGRADE_CASE": "known-limitation-cross-minor"},
        text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=1800,
    )
    print(result.stdout, end="")
    if not expected_failure(result.returncode, result.stdout):
        print("RESULT FAIL KNOWN-LIMITATION cross-minor: expected old-CLI import failure was not observed")
        return 1
    print("RESULT PASS KNOWN-LIMITATION cross-minor: expected old-CLI failure observed; not supported-upgrade evidence")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
