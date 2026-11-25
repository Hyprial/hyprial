#!/usr/bin/env python3
"""E2E-015: local graph lifecycle and cross-node delegated completion."""
from pathlib import Path
import sys
import runpy

sys.path.insert(0, str(Path(__file__).resolve().parent))

if __name__ == "__main__":
    for script in ("run-pac-workflow-unified.py", "run-pac-cross-node.py"):
        scenario = runpy.run_path(str(Path(__file__).with_name(script)))
        result = scenario["main"]()
        if result not in (None, 0):
            raise SystemExit(result)
    print("RESULT PASS E2E-015 local and cross-node PAC workflow")
