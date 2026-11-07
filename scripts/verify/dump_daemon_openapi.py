#!/usr/bin/env python3
"""Write or check the deterministic daemon OpenAPI contract snapshot.

The export is pure declaration data: this script does not start a daemon, read
the operator's home or open a socket.  ``--check`` is the CI/test-facing mode;
``--write`` is the reviewed regeneration step.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from hyprial.daemon import daemon_openapi  # noqa: E402

TARGET = ROOT / "contract" / "daemon-request" / "openapi.json"


def dumps(document: object) -> str:
    return json.dumps(document, sort_keys=True, indent=2, ensure_ascii=False) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="compare the snapshot")
    mode.add_argument("--write", action="store_true", help="rewrite the snapshot")
    args = parser.parse_args(argv)
    expected = dumps(daemon_openapi())
    if args.write:
        TARGET.parent.mkdir(parents=True, exist_ok=True)
        TARGET.write_text(expected, encoding="utf-8")
        print(f"wrote {TARGET.relative_to(ROOT)}")
        return 0
    try:
        actual = TARGET.read_text(encoding="utf-8")
    except FileNotFoundError:
        print(f"missing {TARGET.relative_to(ROOT)}; run with --write", file=sys.stderr)
        return 1
    if actual != expected:
        print(
            f"{TARGET.relative_to(ROOT)} is stale; run "
            "`uv run python scripts/verify/dump_daemon_openapi.py --write`",
            file=sys.stderr,
        )
        return 1
    print(f"PASS {TARGET.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
