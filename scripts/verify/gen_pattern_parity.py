#!/usr/bin/env python3
"""Record Python `re` verdicts for every request-params `pattern` of the contract.

Decision record (1e-2 after review, 2026-11-15)
- The daemon validates request params with Python `jsonschema` (Draft 2020-12),
  which evaluates `pattern` with `re.search`.  The JavaScript Contract Mock must
  answer like it.  This script is the one place that asks Python: it collects
  every distinct `pattern` of the committed `openapi.json` params schemas,
  applies each to a fixed edge-character input set, and writes the verdicts to
  `gui/tests/contract-mock/pattern-parity.fixture.json`.
- A vitest replays the fixture against the mock's translated patterns, so CI
  needs no Python for the differential.  `--check` regenerates in memory and
  fails when the committed file differs, so the fixture cannot drift from
  Python's behaviour or from the contract's pattern set.
- The input set is not random: the characters on which ECMAScript and Python
  differ (newline before the end anchor, the `\\s` sets, `.` and line
  terminators, U+FEFF, U+180E, U+200B) plus ordinary neighbours, each placed
  alone, before, after and around a per-pattern valid sample.
- The fixture is test data under `gui/tests/`; it is not a mock data source
  for `gui/src` and carries no daemon method name.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
OPENAPI = ROOT / "contract" / "daemon-request" / "openapi.json"
FIXTURE = ROOT / "gui" / "tests" / "contract-mock" / "pattern-parity.fixture.json"

EDGE = [
    "\n", "\r", "\r\n", "\t", "\v", "\f", "\x00", "\x1b", "\x1c", "\x1d", "\x1e", "\x1f",
    " ", "\x7f", "\x84", "\x85", "\x86", "\x9f", "\xa0", "\xa1", " ", "᠎",
    " ", " ", "​", " ", " ", " ", " ", "　",
    "﻿", ":", ".", "_", "-", "a", "A", "0", "é", "\U0001f600",
]

KEY = ["\n", "\r", "\x1c", "\x1f", "\x85", "\xa0", "\u2028", "\ufeff", "\u180e", "\u200b", "\u3000", " ", "\t", ":", "\x7f"]

SAMPLES: dict[str, list[str]] = {
    r"^[0-9a-f]{64}$": ["0" * 64, "a" * 64, "A" * 64, "0" * 63],
    r"^[A-Z][A-Z0-9_]*$": ["A", "AB_1", "a"],
    r"^pk\.[a-z2-7]{26}$": ["pk." + "a" * 26, "pk." + "a" * 25],
    r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$": ["a", "a" * 128, "a" * 129, "a.b-c_d"],
    r"[\r\n]": ["", "x"],
    r"^[A-Za-z0-9][A-Za-z0-9._:-]*$": ["opkey-0123456789abcdef", "a:b.c", "-a"],
    r"\S": ["a", " "],
    r"^c\.[a-z2-7]{26}$": ["c." + "a" * 26],
    r"^t\.[a-z2-7]{26}$": ["t." + "a" * 26],
    r"^[A-Za-z0-9_-]+$": ["abc", "a-b_c"],
    r"^agent:(?:[^:\s]|[^:\s][^:]*[^:\s]):(?:[^:\s]|[^:\s][^:]*[^:\s]):(?:[^:\s]|[^:\s][^:]*[^:\s])$": [
        "agent:a:b:c", "agent:a b:b:c", "agent:ab:cd:ef",
    ],
    r"[\u0000-\u001f\u007f-\u009f]": ["", "x"],
}


def collect(schema: Any, out: set[str]) -> None:
    if isinstance(schema, list):
        for item in schema:
            collect(item, out)
    elif isinstance(schema, dict):
        value = schema.get("pattern")
        if isinstance(value, str):
            out.add(value)
        for child in schema.values():
            collect(child, out)


def contract_patterns(openapi: dict[str, Any]) -> list[str]:
    found: set[str] = set()
    for item in openapi["paths"].values():
        body = item["post"].get("requestBody", {}).get("content", {}).get("application/json", {})
        collect(body.get("schema"), found)
    for entry in openapi.get("x-hyprial-methods", []):
        collect(entry.get("paramsSchema"), found)
    return sorted(found)


def inputs_for(pattern: str) -> list[str]:
    seeds = SAMPLES.get(pattern, ["a", "A0"])
    out: list[str] = ["", *EDGE]
    for seed in seeds[:3]:
        out.append(seed)
        for edge in KEY:
            out.extend([seed + edge, edge + seed, seed + edge + seed])
        out.append(seed + "\n\n")
    for edge in KEY:
        out.extend(["a" + edge, edge + "a", "a" + edge + "a"])
    seen: set[str] = set()
    unique = []
    for item in out:
        if item not in seen:
            seen.add(item)
            unique.append(item)
    return unique


def build() -> dict[str, Any]:
    openapi = json.loads(OPENAPI.read_text(encoding="utf-8"))
    entries = []
    for pattern in contract_patterns(openapi):
        compiled = re.compile(pattern)
        inputs = inputs_for(pattern)
        verdicts = "".join("1" if compiled.search(text) else "0" for text in inputs)
        entries.append({"pattern": pattern, "inputs": inputs, "verdicts": verdicts})
    return {
        "version": 1,
        "engine": "python-re-search",
        "generator": "scripts/verify/gen_pattern_parity.py",
        "patterns": entries,
    }


def render(document: dict[str, Any]) -> str:
    lines = [json.dumps(entry, ensure_ascii=True, separators=(",", ":")) for entry in document["patterns"]]
    head = json.dumps({key: value for key, value in document.items() if key != "patterns"}, ensure_ascii=True)
    return head[:-1] + ',"patterns":[\n' + ",\n".join(lines) + "\n]}\n"


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--check", action="store_true", help="fail when the committed fixture differs")
    parser.add_argument("--fixture", type=Path, default=FIXTURE, help="fixture path (default: the committed one)")
    args = parser.parse_args(argv)
    target: Path = args.fixture
    text = render(build())
    if args.check:
        current = target.read_text(encoding="utf-8") if target.exists() else ""
        if current != text:
            print(f"FAIL {target} differs from Python re; run without --check", file=sys.stderr)
            return 1
        print(f"PASS {target} matches Python re")
        return 0
    target.write_text(text, encoding="utf-8")
    print(f"wrote {target} ({len(text)} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
