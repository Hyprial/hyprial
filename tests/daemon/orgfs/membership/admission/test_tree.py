# Decision record
# - 2026-11-25 (F2, design §9 "准入规则的补充"): tests for the root-row rule and
#   the deterministic node / doc id rules.  The shadow-tree test used a shadow
#   whose root row Bob authored; under the root rule that rewrite is refused,
#   so it now asserts that refusal and uses an owner-authored root for the
#   "clean shadow is accepted" half.
"""Tree rows of the §3.6 table (C7 §5.3 items 2, 4 and 19)."""

from __future__ import annotations

import json

import pytest

from hyprial.daemon.impl.orgfs.membership import (
    Accepted,
    Pending,
    Rejected,
    ResultStateAdmission,
    StagedDoc,
    tree_row_wire,
)
from tests.daemon.orgfs.membership.support import (
    OWNER,
    Frame,
    Replica,
    StaticView,
    deliver,
    forge_delete_tree_key,
    forge_move,
    forge_raw_tree_value,
    forge_tree_row,
)

ALICE = "user:alice"
BOB = "user:bob"
DEVICE = "directory/devices/alice/laptop.json"


@pytest.fixture
def admission() -> ResultStateAdmission:
    return ResultStateAdmission()


@pytest.fixture
def origin() -> Replica:
    replica = Replica.create(members={ALICE: "rw", BOB: "rw", "user:reader": "ro"})
    replica.makedirs("directory/devices", author=OWNER)
    replica.makedirs("directory/invites", author=OWNER)
    replica.makedirs("directory/devices/alice", author=ALICE)
    replica.write_text(DEVICE, '{"deviceId":"laptop"}', author=ALICE)
    replica.makedirs("notes/bob", author=BOB)
    return replica


def _rejected(result: object, code: str) -> None:
    assert isinstance(result, Rejected), result
    assert result.code == code, result


def test_path_owner_creates_and_edits_protected_rows(admission, origin) -> None:
    receiver = origin.clone(90)
    for frame in origin.write_text("directory/devices/alice/phone.json", "{}", author=ALICE):
        assert deliver(admission, receiver, frame) == Accepted()
    for frame in origin.write_text(DEVICE, '{"deviceId":"laptop2"}', author=ALICE):
        assert deliver(admission, receiver, frame) == Accepted()


def test_other_member_creating_a_row_under_a_protected_path_is_rejected(admission, origin) -> None:
    receiver = origin.clone(90)
    bob = origin.clone(3)
    tree_frame = bob.write_text("directory/devices/alice/evil.json", "{}", author=BOB)[0]
    _rejected(deliver(admission, receiver, tree_frame), "protected-path")
    assert receiver.resolve("directory/devices/alice/evil.json") is None


def test_other_member_rewriting_a_protected_row_is_rejected(admission, origin) -> None:
    receiver = origin.clone(90)
    node_id = origin.resolve(DEVICE)
    row = dict(origin.tree_value().rows[node_id])
    row["name"] = "renamed.json"
    frame = forge_tree_row(origin.state("tree"), node_id, row, author=BOB, client_id=3)
    _rejected(deliver(admission, receiver, frame), "protected-path")


def test_owner_is_not_a_device_path_author(admission, origin) -> None:
    receiver = origin.clone(90)
    owner = origin.clone(5)
    _rejected(deliver(admission, receiver, owner.remove(DEVICE, author=OWNER)), "protected-path")


def test_soft_delete_by_path_owner_is_accepted_and_by_others_rejected(admission, origin) -> None:
    receiver = origin.clone(90)
    bob = origin.clone(3)
    _rejected(deliver(admission, receiver, bob.remove(DEVICE, author=BOB)), "protected-path")
    alice = origin.clone(2)
    assert deliver(admission, receiver, alice.remove(DEVICE, author=ALICE)) == Accepted()
    row = receiver.tree_value().rows[receiver.resolve(DEVICE)]
    assert row["deleted"] is True and row["deletedBatch"] == "batch-1"


def test_deleting_a_protected_tree_key_is_rejected_whoever_sends_it(admission, origin) -> None:
    receiver = origin.clone(90)
    node_id = origin.resolve(DEVICE)
    frame = forge_delete_tree_key(origin.state("tree"), node_id, client_id=2)
    empty_probe = StagedDoc("tree")
    empty_probe.apply(frame.update)
    assert empty_probe.materialize().raw == {}
    _rejected(deliver(admission, receiver, frame), "protected-path")
    assert node_id in receiver.tree_value().rows


def test_deleting_an_ordinary_tree_key_is_accepted(admission, origin) -> None:
    receiver = origin.clone(90)
    node_id = origin.resolve("notes/bob")
    frame = forge_delete_tree_key(origin.state("tree"), node_id, client_id=3)
    assert deliver(admission, receiver, frame) == Accepted()
    assert node_id not in receiver.tree_value().rows


def test_row_without_author_is_rejected_invalid_argument(admission, origin) -> None:
    receiver = origin.clone(90)
    row = {"node_id": "n1", "parent": "root", "name": "x", "kind": "dir",
           "doc_id": None, "blob_hash": None, "deleted": False}
    frame = forge_tree_row(origin.state("tree"), "n1", row, author=None, client_id=3)
    _rejected(deliver(admission, receiver, frame), "invalid-argument")


@pytest.mark.parametrize(
    "raw",
    ["{not json", "[1, 2]", 7,
     tree_row_wire({"node_id": "other", "parent": "root", "name": "x", "kind": "dir"},
                   author=BOB)],
)
def test_malformed_rows_are_rejected_invalid_argument(admission, origin, raw) -> None:
    receiver = origin.clone(90)
    frame = forge_raw_tree_value(origin.state("tree"), "n1", raw, client_id=3)
    _rejected(deliver(admission, receiver, frame), "invalid-argument")


def test_row_whose_node_id_differs_from_its_key_is_rejected(admission, origin) -> None:
    receiver = origin.clone(90)
    row = {"node_id": "elsewhere", "parent": "root", "name": "x", "kind": "dir",
           "doc_id": None, "blob_hash": None, "deleted": False}
    frame = forge_tree_row(origin.state("tree"), "n1", row, author=BOB, client_id=3)
    result = deliver(admission, receiver, frame)
    _rejected(result, "invalid-argument")
    assert result.detail["reason"] == "row-format"


def test_honest_move_into_a_protected_directory_is_rejected(admission, origin) -> None:
    bob = origin.clone(3)
    bob.write_text("notes/bob/child.md", "x", author=BOB)
    receiver = bob.clone(90)
    frame = bob.move("notes/bob", "directory/devices/alice", author=BOB)
    _rejected(deliver(admission, receiver, frame), "protected-path")


def test_move_record_without_row_rewrite_cannot_relocate_into_protected_path(admission, origin) -> None:
    receiver = origin.clone(90)
    target = origin.resolve("directory/devices/alice")
    node_id = origin.resolve("notes/bob")
    frame = forge_move(origin.state("tree"), node_id, target, "bob", client_id=2)
    _rejected(deliver(admission, receiver, frame), "protected-path")


def test_move_record_between_ordinary_paths_is_accepted(admission, origin) -> None:
    receiver = origin.clone(90)
    node_id = origin.resolve("notes/bob")
    frame = forge_move(origin.state("tree"), node_id, "root", "bob-top", client_id=2)
    assert deliver(admission, receiver, frame) == Accepted()
    assert receiver.resolve("bob-top") == node_id


def test_concurrent_cycle_making_moves_are_both_accepted(admission, origin) -> None:
    origin.makedirs("a", author=BOB)
    origin.makedirs("b", author=BOB)
    receiver = origin.clone(90)
    left = origin.clone(2)
    right = origin.clone(3)
    first = left.move("a", "b", author=ALICE)
    second = right.move("b", "a", author=BOB)
    assert deliver(admission, receiver, first) == Accepted()
    assert deliver(admission, receiver, second) == Accepted()
    tree = receiver.tree_value()
    assert {tree.paths[receiver.resolve("b/a")]} == {"b/a"}


def test_invite_records_are_owner_only(admission, origin) -> None:
    receiver = origin.clone(90)
    alice = origin.clone(2)
    _rejected(
        deliver(admission, receiver, alice.write_text("directory/invites/x.json", "{}", author=ALICE)[0]),
        "protected-path",
    )
    owner = origin.clone(5)
    for frame in owner.write_text("directory/invites/x.json", "{}", author=OWNER):
        assert deliver(admission, receiver, frame) == Accepted()


def test_protection_applies_only_in_org_directory_spaces(admission) -> None:
    home = Replica.create(members={ALICE: "rw", BOB: "rw"}, name="home")
    home.makedirs("directory/devices/alice", author=ALICE)
    receiver = home.clone(90)
    plain = ResultStateAdmission()
    bob = home.clone(3)
    for frame in bob.write_text("directory/devices/alice/x.json", "{}", author=BOB):
        assert deliver(plain, receiver, frame) == Accepted()


def test_a_member_cannot_replace_the_tree_with_a_shadow_tree(admission, origin) -> None:
    receiver = origin.clone(90)
    before = set(receiver.tree_value().rows)
    shadow = Replica.create(owner=BOB, members={}, client_id=33)
    shadow.makedirs("directory/devices/alice", author=BOB)
    frame = Frame("tree", shadow.state("tree"), shadow.vector("tree"))
    _rejected(deliver(admission, receiver, frame), "protected-path")
    rooted_shadow = Replica.create(owner=BOB, members={}, client_id=35)
    rooted_shadow.makedirs("elsewhere", author=BOB)
    rerooted = Frame("tree", rooted_shadow.state("tree"), rooted_shadow.vector("tree"))
    result = deliver(admission, receiver, rerooted)
    _rejected(result, "protected-path")
    assert result.detail["nodeId"] == "root"
    clean_shadow = Replica.create(owner=OWNER, members={}, client_id=34)
    clean_shadow.makedirs("elsewhere", author=BOB)
    clean = Frame("tree", clean_shadow.state("tree"), clean_shadow.vector("tree"))
    assert deliver(admission, receiver, clean) == Accepted()
    assert before <= set(receiver.tree_value().rows)
    assert receiver.resolve(DEVICE) is not None
    side = deliver(admission, receiver, Frame(
        "tree-00000000-0000-0000-0000-000000000001", clean_shadow.state("tree"), b"\x00"
    ))
    assert side == Pending("unknown-doc")


def test_new_joiner_accepts_full_history_with_departed_authors(admission, origin) -> None:
    origin.write_text("notes/bob/old.md", "from bob", author=BOB)
    origin.remove_member(BOB, by=OWNER)
    joiner = Replica(client_id=95)
    view = StaticView(OWNER, {})
    meta = admission.admit("meta", origin.state("meta"), origin.vector("meta"), joiner.staged("meta"), view)
    assert meta == Accepted()
    joiner.apply("meta", origin.state("meta"))
    tree = deliver(admission, joiner, Frame("tree", origin.state("tree"), origin.vector("tree")))
    assert tree == Accepted()
    doc_ids = [doc_id for doc_id in origin.docs if doc_id not in ("meta", "tree")]
    for doc_id in doc_ids:
        frame = Frame(doc_id, origin.state(doc_id), origin.vector(doc_id))
        assert deliver(admission, joiner, frame) == Accepted()
    assert joiner.view().member_mode(BOB) is None


def test_design_gap_removed_member_honest_write_is_accepted_by_admission(admission, origin) -> None:
    origin.remove_member(BOB, by=OWNER)
    receiver = origin.clone(90)
    bob = origin.clone(3)
    frames = bob.write_text("notes/bob/after.md", "late", author=BOB)
    assert [deliver(admission, receiver, frame) for frame in frames] == [Accepted(), Accepted()]


def test_rejection_detail_names_doc_and_node(admission, origin) -> None:
    receiver = origin.clone(90)
    node_id = origin.resolve(DEVICE)
    frame = forge_delete_tree_key(origin.state("tree"), node_id, client_id=2)
    result = deliver(admission, receiver, frame)
    assert isinstance(result, Rejected)
    assert result.detail["docId"] == "tree"
    assert result.detail["nodeId"] == node_id
    assert json.dumps(dict(result.detail))


def _root_row(replica: Replica) -> dict[str, object]:
    return dict(replica.tree_value().rows["root"])


@pytest.mark.parametrize("name", ["group-acme", "home"])
@pytest.mark.parametrize(
    "change",
    [{"deleted": True}, {"kind": "doc"}, {"name": "renamed"}, {"parent": "elsewhere"}],
)
@pytest.mark.parametrize("author", [OWNER, BOB])
def test_root_row_keeps_its_kind_place_and_life_for_everyone(admission, name, change, author) -> None:
    origin = Replica.create(members={ALICE: "rw", BOB: "rw"}, name=name)
    origin.makedirs("elsewhere", author=BOB)
    receiver = origin.clone(90)
    row = {**_root_row(origin), **change}
    if row["parent"] == "elsewhere":
        row["parent"] = origin.resolve("elsewhere")
    frame = forge_tree_row(origin.state("tree"), "root", row, author=author, client_id=3)
    result = deliver(admission, receiver, frame)
    _rejected(result, "protected-path")
    assert result.detail["nodeId"] == "root"
    assert result.detail["path"] == ""
    assert receiver.tree_value().rows["root"]["kind"] == "dir"


@pytest.mark.parametrize("name", ["group-acme", "home"])
def test_root_row_other_fields_change_only_for_the_owner(admission, name) -> None:
    origin = Replica.create(members={ALICE: "rw", BOB: "rw"}, name=name)
    receiver = origin.clone(90)
    row = {**_root_row(origin), "blob_hash": "h"}
    forged = forge_tree_row(origin.state("tree"), "root", row, author=BOB, client_id=3)
    _rejected(deliver(admission, receiver, forged), "protected-path")
    honest = forge_tree_row(origin.state("tree"), "root", row, author=OWNER, client_id=5)
    assert deliver(admission, receiver, honest) == Accepted()


@pytest.mark.parametrize("name", ["group-acme", "home"])
def test_root_key_cannot_be_deleted_or_renamed_by_a_move_record(admission, name) -> None:
    origin = Replica.create(members={ALICE: "rw", BOB: "rw"}, name=name)
    origin.makedirs("elsewhere", author=BOB)
    receiver = origin.clone(90)
    deleted = forge_delete_tree_key(origin.state("tree"), "root", client_id=5)
    _rejected(deliver(admission, receiver, deleted), "protected-path")
    renamed = forge_move(origin.state("tree"), "root", None, "renamed", client_id=5)
    _rejected(deliver(admission, receiver, renamed), "protected-path")
    under_child = forge_move(origin.state("tree"), "root", origin.resolve("elsewhere"), "", client_id=5)
    assert deliver(admission, receiver, under_child) == Accepted()
    assert receiver.tree_value().placed["root"]["parent"] is None
    assert "root" in receiver.tree_value().rows


def test_joiner_accepts_the_root_row_in_the_first_tree_state(admission, origin) -> None:
    joiner = Replica(client_id=95)
    joiner.apply("meta", origin.state("meta"))
    frame = Frame("tree", origin.state("tree"), origin.vector("tree"))
    assert deliver(admission, joiner, frame) == Accepted()
    assert joiner.tree_value().rows["root"]["kind"] == "dir"


def test_protected_child_with_a_random_node_id_is_rejected(admission, origin) -> None:
    receiver = origin.clone(90)
    parent = origin.resolve("directory/devices/alice")
    row = {"node_id": "random-child", "parent": parent, "name": "sub", "kind": "dir",
           "doc_id": None, "blob_hash": None, "deleted": False}
    frame = forge_tree_row(origin.state("tree"), "random-child", row, author=ALICE, client_id=2)
    result = deliver(admission, receiver, frame)
    _rejected(result, "protected-path")
    assert result.detail["path"] == "directory/devices/alice/sub"
    assert receiver.resolve("directory/devices/alice/sub") is None


def test_protected_doc_row_with_an_ordinary_doc_id_is_rejected(admission, origin) -> None:
    receiver = origin.clone(90)
    path = "directory/devices/alice/linked.json"
    node_id, _ = origin._new_ids(path, "doc")
    row = {"node_id": node_id, "parent": origin.resolve("directory/devices/alice"),
           "name": "linked.json", "kind": "doc", "doc_id": "doc-ordinary",
           "blob_hash": None, "deleted": False}
    frame = forge_tree_row(origin.state("tree"), node_id, row, author=ALICE, client_id=2)
    result = deliver(admission, receiver, frame)
    _rejected(result, "protected-path")
    assert result.detail["path"] == path


def test_protected_doc_id_linked_elsewhere_is_rejected_even_for_the_owner(admission, origin) -> None:
    receiver = origin.clone(90)
    linked = str(origin.tree_value().rows[origin.resolve(DEVICE)]["doc_id"])
    row = {"node_id": "alias", "parent": "root", "name": "public-alias.json", "kind": "doc",
           "doc_id": linked, "blob_hash": None, "deleted": False}
    for author in (OWNER, ALICE):
        frame = forge_tree_row(origin.state("tree"), "alias", row, author=author, client_id=5)
        result = deliver(admission, receiver, frame)
        _rejected(result, "protected-path")
        assert result.detail["path"] == "public-alias.json"
    assert receiver.resolve("public-alias.json") is None


@pytest.mark.parametrize("path", ["directory", "directory/devices", "directory/invites"])
def test_owner_cannot_rename_or_move_a_protected_root(admission, origin, path) -> None:
    receiver = origin.clone(90)
    node_id = origin.resolve(path)
    renamed = {**origin.tree_value().rows[node_id], "name": "moved-away"}
    frame = forge_tree_row(origin.state("tree"), node_id, renamed, author=OWNER, client_id=5)
    _rejected(deliver(admission, receiver, frame), "protected-path")
    moved = {**origin.tree_value().rows[node_id], "parent": origin.resolve("notes/bob")}
    frame = forge_tree_row(origin.state("tree"), node_id, moved, author=OWNER, client_id=5)
    _rejected(deliver(admission, receiver, frame), "protected-path")
    assert receiver.resolve(path) == node_id


def test_owner_soft_deletes_and_restores_a_protected_root(admission, origin) -> None:
    receiver = origin.clone(90)
    owner = origin.clone(5)
    node_id = origin.resolve("directory/invites")
    assert deliver(admission, receiver, owner.remove("directory/invites", author=OWNER)) == Accepted()
    restored = {**owner.tree_value().rows[node_id], "deleted": False}
    restored.pop("deletedBatch", None)
    frame = forge_tree_row(owner.state("tree"), node_id, restored, author=OWNER, client_id=5)
    assert deliver(admission, receiver, frame) == Accepted()
    assert receiver.tree_value().rows[node_id]["deleted"] is False


def test_path_owner_soft_deletes_and_restores_a_protected_document(admission, origin) -> None:
    receiver = origin.clone(90)
    alice = origin.clone(2)
    assert deliver(admission, receiver, alice.remove(DEVICE, author=ALICE)) == Accepted()
    for frame in alice.write_text(DEVICE, '{"deviceId":"back"}', author=ALICE):
        assert deliver(admission, receiver, frame) == Accepted()
    assert receiver.tree_value().rows[receiver.resolve(DEVICE)]["deleted"] is False


def test_honest_org_directory_build_up_is_accepted_from_an_empty_tree(admission) -> None:
    origin = Replica.create(members={ALICE: "rw", BOB: "rw"})
    receiver = origin.clone(90)
    frames = [
        *origin.makedirs("directory/devices", author=OWNER),
        *origin.makedirs("directory/devices/alice", author=ALICE),
        *origin.makedirs("directory/people", author=OWNER),
        *origin.makedirs("directory/invites", author=OWNER),
        *origin.makedirs("directory/leaves", author=OWNER),
    ]
    frames += origin.write_text("directory/org.json", "{}", author=OWNER)
    frames += origin.write_text("directory/leaves/alice.json", "{}", author=ALICE)
    for frame in frames:
        assert deliver(admission, receiver, frame) == Accepted(), frame
