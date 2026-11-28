# Decisions:
# - Phase D T1: rewritten onto the state host (``device_rows/support``): honest
#   writes are local facade calls delivered by the in-process cluster, forged
#   tree updates (rows or bare move records) go through
#   ``SyncHost.apply_remote``.  Receivers reject with ``protected-path``
#   (design §3.6); the journal ``pending`` and ``commits`` checks became
#   "the forged change never shows up in the receiver's tree".
# - A member's local move of an ordinary folder into its own protected
#   subtree is refused with ``protected-path`` (design §9.2 "准入规则的补充";
#   phase F4 changed it from the old bare ``not-a-member``).
# - A row under a protected parent whose id is not the deterministic id of
#   its path was refused by the old receivers; the state host accepts it, so
#   ``test_r7_create_under_protected_parent_requires_deterministic_child_id_and_author``
#   fails on purpose (T1 report, defect repros).
"""ROUND 7 deterministic protected-tree identities and move admission."""

from __future__ import annotations

import base64

import pytest

from hyprial.daemon.impl.orgfs.membership import Accepted, Rejected
from hyprial.identity import (
    parse_protected_directory_node_id,
    protected_directory_node_id,
)

from .support import ALICE, BOB, CAROL, OWNER, Org, new_row, refused


@pytest.fixture
def org():
    value = Org()
    try:
        yield value
    finally:
        value.close()


def _expected_node_id(space_id: str, path: str, principal: str) -> str:
    values = (space_id, principal, path)
    payload = "".join(f"{len(value)}:{value}" for value in values).encode()
    token = base64.b32encode(payload).decode("ascii").rstrip("=").lower()
    return f"node-orgdir-v1-{token}"


def _code(outcome: object) -> str | None:
    return outcome.code if isinstance(outcome, Rejected) else None


def test_r7_every_protected_tree_node_uses_its_deterministic_id(org: Org) -> None:
    space_id = org.space_id
    paths = {
        "directory": f"user:{OWNER}",
        "directory/devices": f"user:{OWNER}",
        "directory/people": f"user:{OWNER}",
        "directory/leaves": f"user:{OWNER}",
    }
    for path, principal in paths.items():
        expected = _expected_node_id(space_id, path, principal)
        assert protected_directory_node_id(space_id, path, space_owner=f"user:{OWNER}") == expected
        assert parse_protected_directory_node_id(expected) == (space_id, principal, path)
        assert org.fs(OWNER).stat(space_id, path).node_id == expected

    org.fs(ALICE).mkdir(space_id, f"directory/devices/{ALICE}")
    org.fs(ALICE).write_text(space_id, f"directory/devices/{ALICE}/node-a.json", "{}")
    org.deliver()
    for user in (OWNER, ALICE, BOB):
        for path in (f"directory/devices/{ALICE}", f"directory/devices/{ALICE}/node-a.json"):
            assert org.fs(user).stat(space_id, path).node_id == _expected_node_id(
                space_id, path, f"user:{ALICE}"
            )


def test_r7_create_under_protected_parent_requires_deterministic_child_id_and_author(
    org: Org,
) -> None:
    space_id = org.space_id
    org.fs(ALICE).mkdir(space_id, f"directory/devices/{ALICE}")
    org.deliver()
    parent = org.node_id(OWNER, f"directory/devices/{ALICE}")
    expected = _expected_node_id(space_id, f"directory/devices/{ALICE}/child", f"user:{ALICE}")

    wrong_id = org.forge_rows(
        ALICE, [new_row("random-child", parent=parent, name="child", kind="dir")], author=ALICE
    )
    wrong_author = org.forge_rows(
        BOB, [new_row(expected, parent=parent, name="child", kind="dir")], author=BOB
    )
    verdicts = {
        (label, receiver): isinstance(org.apply(receiver, frame), Rejected)
        for label, frame in (("wrong-id", wrong_id), ("wrong-author", wrong_author))
        for receiver in (OWNER, BOB)
    }
    assert verdicts == {key: True for key in verdicts}
    for receiver in (OWNER, BOB):
        assert org.missing(receiver, f"directory/devices/{ALICE}/child")


def test_r7_moves_are_refused_when_any_operation_id_is_protected(org: Org) -> None:
    space_id = org.space_id
    bob = org.fs(BOB)
    bob.mkdir(space_id, f"directory/devices/{BOB}")
    folder = bob.mkdir(space_id, "ordinary")
    bob.write_text(space_id, "ordinary.txt", "ordinary")
    org.deliver()

    assert refused(
        lambda: bob.move(space_id, f"id:{folder.node_id}", f"directory/devices/{BOB}/ordinary")
    ) == "protected-path"
    assert bob.stat(space_id, f"id:{folder.node_id}").path == "ordinary"

    moved = bob.move(space_id, "ordinary.txt", "ordinary/ordinary.txt")
    assert moved.path == "ordinary/ordinary.txt"
    org.deliver()
    for user in (OWNER, ALICE):
        assert org.text(user, "ordinary/ordinary.txt") == "ordinary"


def test_r7_delete_of_protected_id_is_owner_or_encoded_user_only(org: Org) -> None:
    space_id = org.space_id
    org.fs(BOB).mkdir(space_id, f"directory/devices/{BOB}")
    row = org.fs(BOB).write_text(space_id, f"directory/devices/{BOB}/row.json", "{}")
    org.deliver()

    forged = org.forge_rewrite(ALICE, row.node_id, author=ALICE, deleted=True)
    for receiver in (OWNER, BOB):
        assert _code(org.apply(receiver, forged, peer="node-alice")) == "protected-path"
        assert org.fs(receiver).stat(space_id, f"id:{row.node_id}").deleted is False

    org.fs(BOB).remove(space_id, f"id:{row.node_id}")
    org.deliver()
    for user in (OWNER, ALICE):
        assert org.missing(user, f"directory/devices/{BOB}/row.json")


@pytest.mark.parametrize(
    ("order", "receiver_name"),
    [
        (("alice", "bob"), "owner"),
        (("bob", "alice"), "owner"),
        (("alice", "bob"), "carol"),
        (("bob", "alice"), "carol"),
    ],
)
def test_r7_959_repro_converges_in_both_orders_on_two_replicas(
    org: Org, order: tuple[str, str], receiver_name: str
) -> None:
    space_id = org.space_id
    org.fs(BOB).mkdir(space_id, f"directory/devices/{BOB}")
    org.deliver()
    folder = org.fs(ALICE).mkdir(space_id, "F")
    child = org.fs(ALICE).write_text(space_id, "x.txt", "x")
    org.deliver()
    bob_parent = org.node_id(BOB, f"directory/devices/{BOB}")
    moves = {
        "alice": org.forge_move(
            ALICE, child.node_id, old_parent="root", new_parent=folder.node_id, name="x.txt"
        ),
        "bob": org.forge_move(
            BOB, folder.node_id, old_parent="root", new_parent=bob_parent, name="F"
        ),
    }
    receiver = OWNER if receiver_name == "owner" else CAROL

    verdicts = {actor: org.apply(receiver, moves[actor], peer=f"node-{actor}") for actor in order}

    assert verdicts["alice"] == Accepted()
    assert _code(verdicts["bob"]) == "protected-path"
    assert org.text(receiver, "F/x.txt") == "x"
    assert org.missing(receiver, f"directory/devices/{BOB}/F")
