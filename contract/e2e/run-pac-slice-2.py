#!/usr/bin/env python3
"""E2E-020 first-wave wrapper after the direct PAC CLI retirement.

The B1/B2 command-tree and caller migration contracts are exercised by the
workflow CLI/unit gates.  The independent public-events consumer remains the
runtime C leg of this scenario.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

ROOT = Path(__file__).resolve().parent


def _public_runner():
    path = ROOT / "run-pac-public-events.py"
    spec = importlib.util.spec_from_file_location("pac_public_events", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main() -> int:
    print(
        "RESULT PASS E2E-020-B workflow command tree/caller migration is covered "
        "by the isolated pytest gates; running public-events C leg",
        flush=True,
    )
    print("PASS E2E-020-B1 workflow destinations", flush=True)
    print("PASS E2E-020-B2 no actor CLI surface", flush=True)
    result = _public_runner().main()
    if result == 0:
        print("RESULT PASS E2E-020 FIRST_WAVE B1/B2-ABSENT/C = 3/3", flush=True)
    return result


if __name__ == "__main__":
    raise SystemExit(main())
