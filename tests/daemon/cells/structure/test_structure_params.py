"""Param matching when a harness member ref is a member name, not an address.

Characterisation of ``cells.params.source.agent_refs`` (design §5.2's one
unverified point): ``agent_refs`` collects the harness member refs into the
agent's param-matching identity, so a member name ref simply joins the
identity group next to the cell id and the ``a.`` id.

Recorded answer (2026-11-19, this code, no product change):

- ``delivery.bodyRetentionMs`` still resolves for a subject that is the
  agent's cell id, its ``a.`` id, or the member name itself.
- It does NOT resolve for a subject that is the agent address form
  (``agent:<owner>:<host>:<name>``) once the ref is a plain member name:
  the address is in no identity group and matches literally.  Today's
  callers pass exactly that address form: ``composition/delivery_params``
  and ``composition/conversations/wiring`` pass the message's sender or
  recipient.

Second round (2026-11-19): the ``threads.policy`` half of this file was
removed because another slice retires that key.  Both keys exercised the
same mechanism (``agent_refs`` identity groups through one snapshot), so
the recorded answer transfers; only the threads caller left with the key.
"""

from __future__ import annotations

from hyprial.daemon.impl.cells.params import (
    DELIVERY_BODY_RETENTION_MS,
    CellParamSnapshot,
    resolve_param_detail,
)
from hyprial.daemon.impl.cells.ports import CellIndex

from tests.daemon.cells.structure.structure_support import (
    AGENT_ID,
    AGENT_URI,
    OWNER,
    structure_cell,
)

DAY_MS = 86_400_000
RETENTION = 3 * DAY_MS


def member_name_agent() -> tuple[CellParamSnapshot, str, str, str]:
    """A trusted agent cell whose harness member ref is the name ``front``."""

    cell = structure_cell(
        (("front", "harness"),),
        {"delivery.bodyRetentionMs": RETENTION},
    )
    snapshot = CellParamSnapshot.from_index(CellIndex(1, {cell.cell_id: cell}), OWNER)
    assert cell.binding.value == AGENT_ID
    return snapshot, cell.cell_id, cell.binding.value, "front"


def test_body_retention_still_hits_by_cell_id_and_agent_id() -> None:
    snapshot, cell_id, agent_id, _ = member_name_agent()
    assert cell_id != agent_id
    for subject in (cell_id, agent_id):
        detail = resolve_param_detail(snapshot, DELIVERY_BODY_RETENTION_MS, subject)
        assert detail.value == RETENTION
        assert not detail.defaulted
        assert detail.sources == (cell_id,)


def test_body_retention_also_hits_by_the_member_name_itself() -> None:
    snapshot, cell_id, _, front = member_name_agent()
    detail = resolve_param_detail(snapshot, DELIVERY_BODY_RETENTION_MS, front)
    assert detail.value == RETENTION
    assert detail.sources == (cell_id,)


def test_body_retention_misses_by_the_agent_address_form() -> None:
    snapshot, _, _, _ = member_name_agent()
    detail = resolve_param_detail(snapshot, DELIVERY_BODY_RETENTION_MS, AGENT_URI)
    assert detail.defaulted
    assert detail.value == DELIVERY_BODY_RETENTION_MS.default
    assert detail.sources == ()
