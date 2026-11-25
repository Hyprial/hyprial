#!/usr/bin/env python3
"""Validate the shared-channel JSONL corpus with no optional dependencies."""

from __future__ import annotations

import json
import re
from pathlib import Path


HERE = Path(__file__).resolve().parent
FILES = ("cases.jsonl", "sync-targets.jsonl", "controls.jsonl", "mail-rollover-smtp.jsonl")


def main() -> int:
    seen: set[str] = set()
    control_numbers: set[int] = set()
    for name in FILES:
        rows = HERE.joinpath(name).read_text(encoding="utf-8").splitlines()
        if not rows:
            raise SystemExit(f"empty corpus: {name}")
        for line_number, line in enumerate(rows, 1):
            row = json.loads(line)
            if set(row) != {"schemaVersion", "caseId", "contract", "expected"}:
                raise SystemExit(f"invalid keys: {name}:{line_number}")
            if row["schemaVersion"] != 1 or not re.fullmatch(r"UA-\d{3}:[a-z0-9-]+", row["caseId"]):
                raise SystemExit(f"invalid identity: {name}:{line_number}")
            if row["caseId"] in seen or not isinstance(row["expected"], dict):
                raise SystemExit(f"duplicate or invalid case: {name}:{line_number}")
            seen.add(row["caseId"])
            if name == "controls.jsonl":
                number = int(row["caseId"][3:6])
                control_numbers.add(number)
                required = {
                    "authorizationBinding", "signedResponderSetDigest", "endpointKind",
                    "principal", "winnerRows", "responseRows", "replayAccepted",
                }
                if set(row["expected"]) != required:
                    raise SystemExit(f"control authority fields mismatch: {name}:{line_number}")
                if row["expected"]["winnerRows"] != 1 or row["expected"]["responseRows"] != 1:
                    raise SystemExit(f"control authority row count mismatch: {name}:{line_number}")
    if control_numbers != set(range(393, 403)):
        raise SystemExit("controls.jsonl must own exactly UA-393..402")
    print(f"RESULT PASS shared-channel-v1 cases={len(seen)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
