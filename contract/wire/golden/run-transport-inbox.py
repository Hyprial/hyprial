#!/usr/bin/env python3
"""Execute the transport/inbox-owned wire-golden slice against Python."""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SCOPE = Path(__file__).with_name("transport-inbox.json")


def main() -> int:
    cases = json.loads(SCOPE.read_text(encoding="utf-8"))["cases"]
    suite = unittest.defaultTestLoader.discover(
        str(ROOT / "tests"), pattern="test_*.py"
    )
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    if not result.wasSuccessful():
        print(f"RESULT RED 0/{len(cases)} transport-inbox")
        return 1
    print(f"RESULT GREEN {len(cases)}/{len(cases)} transport-inbox")
    return 0


if __name__ == "__main__":
    sys.exit(main())
