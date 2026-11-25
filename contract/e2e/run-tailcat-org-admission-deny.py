#!/usr/bin/env python3
"""E2E-029: v6 control admission refusal and business-stream revocation."""
import sys
from pathlib import Path

# Stand on its own when loaded by path, not only when run as a script.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _tailcat_join_scenarios import run  # noqa: E402

SCENARIO = "E2E-029"

if __name__ == "__main__":
    raise SystemExit(run(SCENARIO, "TestE2E029AdmissionDeny"))
