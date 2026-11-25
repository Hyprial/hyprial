"""The Contract Mock pattern fixture is Python's real verdict, not a copy of it.

The JavaScript mock replays ``pattern-parity.fixture.json`` in vitest with no
Python at CI time.  This test runs the generator in ``--check`` mode so the
fixture cannot drift from ``re.search`` or from the contract's pattern set.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
GENERATOR = ROOT / "scripts" / "verify" / "gen_pattern_parity.py"
FIXTURE = ROOT / "gui" / "tests" / "contract-mock" / "pattern-parity.fixture.json"


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(GENERATOR), *args],
        capture_output=True,
        text=True,
        check=False,
        cwd=ROOT,
    )


def test_the_committed_fixture_is_what_python_re_produces() -> None:
    result = _run("--check")
    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith("PASS")


def test_the_check_fails_when_a_verdict_is_flipped(tmp_path: Path) -> None:
    document = json.loads(FIXTURE.read_text(encoding="utf-8"))
    entry = document["patterns"][0]
    flipped = "1" if entry["verdicts"][0] == "0" else "0"
    entry["verdicts"] = flipped + entry["verdicts"][1:]
    copy = tmp_path / "fixture.json"
    copy.write_text(json.dumps(document), encoding="utf-8")
    result = _run("--check", "--fixture", str(copy))
    assert result.returncode == 1
    assert "differs from Python re" in result.stderr


def test_the_fixture_records_the_edge_cases_that_motivated_it() -> None:
    document = json.loads(FIXTURE.read_text(encoding="utf-8"))
    by_pattern = {entry["pattern"]: entry for entry in document["patterns"]}
    key = by_pattern[r"^[A-Za-z0-9][A-Za-z0-9._:-]*$"]
    verdict = dict(zip(key["inputs"], key["verdicts"], strict=True))
    assert verdict["opkey-0123456789abcdef\n"] == "1"
    assert verdict["opkey-0123456789abcdef\n\n"] == "0"
    nonspace = dict(zip(by_pattern[r"\S"]["inputs"], by_pattern[r"\S"]["verdicts"], strict=True))
    assert nonspace["\x1c"] == "0" and nonspace["\x85"] == "0" and nonspace["﻿"] == "1"
