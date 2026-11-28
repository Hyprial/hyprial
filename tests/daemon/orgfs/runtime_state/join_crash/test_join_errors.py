# Decisions (phase F1, V defect 4, 2026-11-25):
# - A join tries every candidate holder; the error it raises is the most
#   informative one seen, not the last: a node-gate or membership refusal
#   from a holder that answered outranks any other sync error, which
#   outranks an offline holder, which outranks "no holder".  Holders are
#   tried in sorted order, so the offline holder here is tried last and is
#   what the old code reported.
"""Which error a join that failed on several holders reports."""

from __future__ import annotations

import pytest

from hyprial.daemon.impl.orgfs import OrgFsError

from ..support import Peers


def test_a_node_gate_refusal_outranks_a_later_offline_holder(peers: Peers) -> None:
    owner = peers.add("node-a", "user:owner@example", trust=False)
    member = peers.add("node-m", "user:member@example", trust=False)
    offline = peers.add("node-z", "user:other@example", trust=False)
    space_id = owner.fs.create_space("team").space_id
    owner.fs.invite(space_id, member.author, "rw")
    peers.disconnect(offline)

    with pytest.raises(OrgFsError) as raised:
        member.fs.join(space_id)

    assert raised.value.code == "not-a-member"
    assert raised.value.details.get("reason") == "supplier-not-member"
    assert raised.value.details.get("supplier") == "node-a"
    assert str(raised.value).count("not-a-member") == 1


def test_an_offline_holder_outranks_no_holder(peers: Peers) -> None:
    owner = peers.add("node-a", "user:owner@example")
    member = peers.add("node-m", "user:member@example")
    space_id = owner.fs.create_space("team").space_id
    owner.fs.invite(space_id, member.author, "rw")
    peers.disconnect(owner)

    with pytest.raises(OrgFsError) as raised:
        member.fs.join(space_id)

    assert (raised.value.code, raised.value.details.get("reason")) == ("not-a-member", "supplier-not-online")
