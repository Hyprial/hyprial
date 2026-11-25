# Decisions (phase F1, item 1, 2026-11-25):
# - The table pins what each runtime does with ``trust_tailnet_members=False``
#   (``trust_upper_layers`` is constantly ``False``) for a plain space, the
#   org directory space and its ACL space.
# - Package S (2026-11-25): the journal runtime and its rows are deleted.
#   They were the evidence of the dev regression (nothing joins or syncs: the
#   resolver was asked about a UUID; F1 gate report).
# - State rows follow the node gate ruling (rule G): every kind syncs both
#   ways, and with trust off from the start the member's device row reaches
#   the owner.  Before the ruling the plain rows were refused and the cold
#   rows never carried member -> owner (reports/f1-gate.md, §1).
# - ``F1_MATRIX_OUT`` appends every observed row as JSON for the report.
"""Production-configuration node gate of the state runtime."""

from __future__ import annotations

import json
import os

import pytest

from .probe import IMPLS, KINDS, run_cell

COLUMNS = ("join_directory", "join_target", "member_row_at_owner", "owner_to_member", "member_to_owner")
BOTH_WAYS = ("ok", "ok", "yes", "yes", "yes")
EXPECTED = {
    ("state", "plain", "cold"): BOTH_WAYS,
    ("state", "directory", "cold"): BOTH_WAYS,
    ("state", "acl", "cold"): BOTH_WAYS,
    ("state", "plain", "warm"): BOTH_WAYS,
    ("state", "directory", "warm"): BOTH_WAYS,
    ("state", "acl", "warm"): BOTH_WAYS,
}


def _observed(row: dict[str, str]) -> tuple[str, ...]:
    flow = {"broadcast": "yes", "pull": "yes"}
    return tuple(flow.get(row[column], row[column]) for column in COLUMNS)


@pytest.mark.parametrize("bootstrap", ["cold", "warm"])
@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("impl", list(IMPLS))
def test_production_gate_matrix(peers, impl: str, kind: str, bootstrap: str) -> None:
    row = run_cell(peers, impl, kind, bootstrap).row()
    out = os.environ.get("F1_MATRIX_OUT")
    if out:
        with open(out, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(row) + "\n")
    assert _observed(row) == EXPECTED[(impl, kind, bootstrap)], row
