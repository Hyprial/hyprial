# Decisions:
# - Phase D T1: ``test_r10_forged_fill_only_delays_update_until_anti_entropy``
#   runs on the state host (``device_rows/support``).  There is no pending
#   queue to fill (design §3.6): an update whose predecessor is missing is
#   ``Pending``, nothing is stored, and one §4.3 pull from the supplier brings
#   the predecessor and the update.
# - Package S (2026-11-25): the journal-only tests (supplier buckets, empty
#   frontiers, varint frontiers, replica priming) were deleted with the
#   journal store, mesh and replicas.
"""ROUND 10: an update that arrives before its predecessor."""

from __future__ import annotations

import pytest

from hyprial.daemon.impl.orgfs.membership import Pending

from .support import ALICE, Org, new_row
from .test_round4 import BOB, OWNER


@pytest.fixture
def state_org():
    value = Org()
    try:
        yield value
    finally:
        value.close()


def test_r10_forged_fill_only_delays_update_until_anti_entropy(state_org: Org) -> None:
    """An update that arrives before its predecessor waits; one pull converges."""

    org = state_org
    space_id = org.space_id
    forged = org.forge_rows(
        BOB,
        [new_row("round10-forged", parent="round10-missing-parent", name="forged.json",
                 doc_id="doc-00000000-0000-4000-8000-000000001010")],
        author=BOB,
    )
    org.apply(ALICE, forged, peer="node-owner")
    frames = org.capture(OWNER, lambda: (
        org.fs(OWNER).mkdir(space_id, "round10-parent"),
        org.fs(OWNER).mkdir(space_id, "round10-parent/child"),
    ))
    parent, child = [frame for frame in frames if frame.doc_id == "tree"]

    assert isinstance(org.apply(ALICE, child, peer="node-owner"), Pending)
    assert org.missing(ALICE, "round10-parent")
    assert org.rejected(ALICE) == 0

    assert org.cluster.pull(org[ALICE], org[OWNER]) >= 1
    assert org.fs(ALICE).stat(space_id, "round10-parent/child").kind == "dir"
    assert parent != child
