"""Guard for the AT12 support matrix (card 361 item 1).

The matrix is a document, but it is a document other people are supposed to
trust when they decide whether a transfer may land.  This keeps it honest:
every row carries a status from the fixed set and non-empty evidence, a PASS
row has to say how it was produced, and the landing methods the card names are
both represented.
"""

from __future__ import annotations

import json
from pathlib import Path

MATRIX = Path(__file__).resolve().parents[4] / 'docs/evidence/transfer-at12-support-matrix.json'

STATUSES = {"PASS", "UNSUPPORTED", "NOT_RUN"}
REQUIRED_KEYS = {
    "harness",
    "version",
    "operatingSystem",
    "landingMethod",
    "status",
    "evidence",
}


def _matrix() -> dict:
    return json.loads(MATRIX.read_text(encoding="utf-8"))


def test_every_row_is_well_formed_and_carries_evidence() -> None:
    matrix = _matrix()
    rows = matrix["rows"]
    assert rows, "the support matrix has no rows"
    assert set(matrix["statusValues"]) == STATUSES
    for row in rows:
        assert REQUIRED_KEYS <= set(row), row
        assert row["status"] in STATUSES, row
        assert row["evidence"].strip(), row
        assert row["landingMethod"] in {"directory-restore", "container"}, row


def test_both_landing_methods_are_represented() -> None:
    methods = {row["landingMethod"] for row in _matrix()["rows"]}
    assert methods == {"directory-restore", "container"}


def test_a_pass_row_says_how_it_was_produced() -> None:
    for row in _matrix()["rows"]:
        if row["status"] != "PASS":
            continue
        evidence = row["evidence"]
        # A PASS without a dated observation or a named test is a claim, not
        # evidence -- and this repo's whole point here is not to make those.
        assert "2026-" in evidence or "tests/" in evidence, row
        # ... and a unit-level PASS must not pretend to be a real worker run.
        assert "not a real" in evidence or "unit-level" in evidence, row


def test_the_runbook_table_agrees_with_the_machine_readable_matrix() -> None:
    """The prose table and the JSON are one matrix; they must not drift."""

    runbook = MATRIX.parents[1] / "guides" / "transfer-at12-cross-machine-runbook.md"
    in_table = False
    doc_statuses: list[str] = []
    for line in runbook.read_text(encoding="utf-8").splitlines():
        if line.startswith("## 1."):
            in_table = True
            continue
        if in_table and line.startswith("## "):
            break
        if not in_table or not line.startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if len(cells) != 6 or cells[-1] in {"依据", "---"}:
            continue
        status = cells[-2]
        assert status.startswith("`") and status.endswith("`"), line
        doc_statuses.append(status.strip("`"))

    json_statuses = [row["status"] for row in _matrix()["rows"]]
    assert sorted(doc_statuses) == sorted(json_statuses), (doc_statuses, json_statuses)
