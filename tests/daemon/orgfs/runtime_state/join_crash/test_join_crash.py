# Decisions (phase F1, item 3, design §9.2 / R5 / R6, 2026-11-25):
# - A crash during a join is taken as a byte copy of the joining node's state
#   directory at the moment its meta (with the ``space`` root) is committed
#   and before the tree arrives; a new runtime is then opened on the copy.
# - The holder deliberately fails the formal node gate (trust off, no org
#   directory and no hint resolve the owner's node), so the live join is
#   refused with ``supplier-not-member``.  Opening the crashed copy must not
#   turn that refused join into a space: ``complete_join``'s checks (this
#   node in the member table, the holder past the node gate) never ran.
# - A completed join leaves no marker behind and reopens normally.
"""A join that crashes after its meta landed does not complete on restart."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest

from hyprial.daemon.impl.orgfs import OrgFsError

from ..support import Peer, Peers

OWNER = "user:owner@example"
MEMBER = "user:member@example"


def _crash_copy_after_meta(member: Peer, target: Path) -> list[bool]:
    copied: list[bool] = []
    facade = member.fs
    original = facade.apply_remote

    def apply_remote(space_id: str, doc_id: str, *args: Any, **kwargs: Any) -> object:
        outcome = original(space_id, doc_id, *args, **kwargs)
        if doc_id == "meta" and not copied:
            shutil.copytree(member.state_dir, target)
            copied.append(True)
        return outcome

    facade.apply_remote = apply_remote
    return copied


def test_a_join_crashed_after_its_meta_landed_is_discarded_on_restart(peers: Peers, tmp_path: Path) -> None:
    owner = peers.add("node-owner", OWNER, trust=False)
    member = peers.add("node-member", MEMBER, trust=False)
    space_id = owner.fs.create_space("team").space_id
    owner.fs.invite(space_id, MEMBER, "rw")
    owner.fs.write_text(space_id, "shared.md", "base")
    crashed = tmp_path / "crashed"
    copied = _crash_copy_after_meta(member, crashed / "node-member")

    with pytest.raises(OrgFsError) as refused:
        member.fs.join(space_id)
    assert (refused.value.code, refused.value.details.get("reason")) == ("not-a-member", "supplier-not-member")
    assert copied == [True]
    assert (crashed / "node-member" / "orgfs" / "spaces" / space_id).is_dir()

    restarted = Peers(crashed)
    try:
        node = restarted.add("node-member", MEMBER, trust=False)
        assert space_id not in {info.space_id for info in node.fs.spaces()}
        assert not (node.spaces_root / space_id).exists()
        assert "orgfs.space.unfinished-discarded" in node.events.names()
    finally:
        restarted.close()


def test_a_completed_join_reopens_as_a_space(peers: Peers) -> None:
    owner = peers.add("node-owner", OWNER)
    member = peers.add("node-member", MEMBER)
    space_id = owner.fs.create_space("team").space_id
    owner.fs.invite(space_id, MEMBER, "rw")
    member.fs.join(space_id)

    assert [path.name for path in (member.spaces_root / space_id).iterdir() if path.name.startswith("join")] == []
    member.reopen()
    assert space_id in {info.space_id for info in member.fs.spaces()}
