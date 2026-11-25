#!/usr/bin/env python3
"""Validate migration phase fixtures and owner-migration inventory facts."""

from __future__ import annotations

import json
import re
from pathlib import Path


HERE = Path(__file__).resolve().parent
PHASES = {f"M{index}" for index in range(7)}


def main() -> int:
    seen: set[str] = set()
    for name in ("cases.jsonl", "owner-migration.jsonl"):
        for number, line in enumerate(HERE.joinpath(name).read_text(encoding="utf-8").splitlines(), 1):
            row = json.loads(line)
            if set(row) != {"schemaVersion", "caseId", "phase", "expected"}:
                raise SystemExit(f"invalid keys: {name}:{number}")
            if row["schemaVersion"] != 1 or row["phase"] not in PHASES:
                raise SystemExit(f"invalid phase: {name}:{number}")
            if not re.fullmatch(r"UA-\d{3}:[a-z0-9-]+", row["caseId"]):
                raise SystemExit(f"invalid case id: {name}:{number}")
            if row["caseId"] in seen or not isinstance(row["expected"], dict):
                raise SystemExit(f"duplicate or invalid case: {name}:{number}")
            seen.add(row["caseId"])
    print(f"RESULT PASS migration-v1 cases={len(seen)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
