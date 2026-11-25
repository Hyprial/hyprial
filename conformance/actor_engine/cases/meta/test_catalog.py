# Decision record (file header; no inline comments by repository rule):
# - These meta cases tie the suite to the frozen spec: the catalog must equal
#   spec section 14 exactly, every ID must have at least one test, and the
#   generated COVERAGE.md must be current. They carry no case marker because
#   they test the suite, not the engine.
"""Suite integrity: catalog == spec section 14, full coverage, fresh table."""

from __future__ import annotations

import re
from pathlib import Path

from support.catalog import CASES
from support.coverage import TABLE, render, scan

SPEC = Path(__file__).resolve().parents[4] / "docs/design/actor-engine-native-abi-spec-2026-11-01.md"
ROW = re.compile(r"\| (\S+) \| ([A-Z]+-\d+) (.+?) \| (.+?) \|$")


def test_catalog_matches_spec_section_14() -> None:
    rows = []
    for line in SPEC.read_text(encoding="utf-8").splitlines():
        match = ROW.match(line.strip())
        if match:
            rows.append((match.group(2), match.group(1), match.group(3).strip(), match.group(4).strip()))
    assert len(rows) == 100
    assert tuple(rows) == CASES


def test_every_case_id_has_a_test() -> None:
    missing = [case_id for case_id, tests in scan().items() if not tests]
    assert missing == []


def test_coverage_table_is_current() -> None:
    assert TABLE.read_text(encoding="utf-8") == render(scan()), (
        "COVERAGE.md is stale: run `python -m support.coverage` in conformance/actor_engine")
