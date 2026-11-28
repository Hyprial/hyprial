# Decisions:
# - Design §3.6 / §3.7 / §6, plan §2.5: the remote path is driven through the
#   space's ``SyncHost.apply_remote`` only; forged updates come from C2's
#   ``forge_*`` tools (tests/daemon/orgfs/membership/support.py).
# - The lying-vector case is C2 report issue 7 replayed on the coordinator:
#   Bob's append to Alice's protected leave record arrives first with an empty
#   result vector, then Alice's own append (its predecessor).
"""Remote apply: admission outcomes, rejection memory, observed deletions."""

from __future__ import annotations

from pycrdt import Doc, Text

from hyprial.daemon.impl.orgfs.api import OrgFsError
from hyprial.daemon.impl.orgfs.membership import (
    Accepted,
    Pending,
    Rejected,
    document_authors,
    local_commit,
)

from tests.daemon.orgfs.membership.support import (
    forge_content,
    forge_member_row,
    forge_tree_row,
)

from .support import EMPTY, OWNER, Cluster, Node, clone_node, doc_id_of, eventually

ALICE = "user:alice"
BOB = "user:bob"


def _pair(name: str = "team") -> tuple[Cluster, Node, Node, str]:
    owner = Node(OWNER, "node-owner")
    space = owner.fs.create_space(name)
    owner.fs.invite(space.space_id, ALICE, "rw")
    owner.fs.invite(space.space_id, BOB, "rw")
    alice = clone_node(owner, space.space_id, ALICE, "node-alice")
    return Cluster(space.space_id, [owner, alice]), owner, alice, space.space_id


def _state(node: Node, space_id: str, doc_id: str) -> bytes:
    found = node.fs.sync_host(space_id).read_update(doc_id, EMPTY)
    assert found is not None
    return found[0]


def test_a_rejection_is_remembered_until_the_policy_generation_changes() -> None:
    cluster, owner, alice, space_id = _pair()
    try:
        host = owner.fs.sync_host(space_id)
        forged = forge_member_row(
            _state(owner, space_id, "meta"),
            "user:mallory",
            {"mode": "rw", "addedBy": ALICE, "addedAt": "2026-11-25T00:00:00.000000Z"},
            client_id=77,
        )
        logged: list[tuple[str, dict]] = []
        owner.fs._logger = lambda level, event, **fields: logged.append((event, fields))

        first = host.apply_remote("meta", forged.update, forged.vector, "node-alice")
        again = host.apply_remote("meta", forged.update, forged.vector, "node-alice")

        assert isinstance(first, Rejected) and first.code == "not-owner"
        assert isinstance(again, Rejected)
        assert owner.fs.status(space_id).rejected_updates == 1
        assert [event for event, _fields in logged] == ["orgfs.update.rejected"]
        assert logged[0][1]["peer"] == "node-alice"
        assert "user:mallory" not in {row.user for row in owner.fs.members(space_id)}

        owner.fs.invite(space_id, "user:carol", "ro")
        third = host.apply_remote("meta", forged.update, forged.vector, "node-alice")
        assert isinstance(third, Rejected)
        assert owner.fs.status(space_id).rejected_updates == 2
    finally:
        owner.close()
        alice.close()


def test_content_before_its_tree_row_is_pending_and_nothing_is_stored() -> None:
    cluster, owner, alice, space_id = _pair()
    try:
        alice.fs.write_text(space_id, "new.md", "hello")
        frames = cluster.take()
        content = [frame for frame in frames if frame[1] not in ("meta", "tree")]
        tree = [frame for frame in frames if frame[1] == "tree"]
        host = owner.fs.sync_host(space_id)
        commits = owner.backing(space_id).commits
        loads = owner.backing(space_id).loads

        (_sender, doc_id, update, vector_bytes), = content
        assert host.apply_remote(doc_id, update, vector_bytes, "node-alice") == Pending("unknown-doc")
        assert owner.backing(space_id).commits == commits
        assert owner.backing(space_id).loads == loads
        assert doc_id not in owner.backing(space_id).documents

        for _sender, tree_id, tree_update, tree_vector in tree:
            assert host.apply_remote(tree_id, tree_update, tree_vector, "node-alice") == Accepted()
        assert host.apply_remote(doc_id, update, vector_bytes, "node-alice") == Accepted()
        assert owner.fs.read_text(space_id, "new.md")[0] == "hello"
        assert owner.fs.stat(space_id, "new.md").modified_by == ALICE
        assert owner.fs.document_authors(space_id, "new.md") == (ALICE,)
    finally:
        owner.close()
        alice.close()


def test_a_tree_row_then_its_content_is_one_created_event() -> None:
    cluster, owner, alice, space_id = _pair()
    try:
        seen = []
        owner.fs.watch(space_id, "**", seen.append)
        alice.fs.write_text(space_id, "born.md", "content")
        cluster.deliver()

        def check() -> None:
            assert [(event.kind, event.node.path) for event in seen] == [("created", "born.md")]
            assert seen[0].node.modified_by == ALICE

        eventually(check)
    finally:
        owner.close()
        alice.close()


def test_a_missing_predecessor_is_pending_and_swaps_the_authoritative_tree() -> None:
    cluster, owner, alice, space_id = _pair()
    try:
        alice.fs.mkdir(space_id, "first")
        alice.fs.mkdir(space_id, "second")
        frames = [frame for frame in cluster.take() if frame[1] == "tree"]
        host = owner.fs.sync_host(space_id)
        before = owner.fs._registry(space_id).tree.doc

        _sender, doc_id, update, vector_bytes = frames[1]
        assert host.apply_remote(doc_id, update, vector_bytes, "node-alice") == Pending("dependency")
        assert owner.fs._registry(space_id).tree.doc is not before
        assert [node.path for node in owner.fs.listdir(space_id, "")] == []

        for _sender, doc_id, update, vector_bytes in frames:
            assert host.apply_remote(doc_id, update, vector_bytes, "node-alice") == Accepted()
        assert [node.path for node in owner.fs.listdir(space_id, "")] == ["first", "second"]
    finally:
        owner.close()
        alice.close()


def test_resource_exhausted_maps_to_pending_dependency() -> None:
    cluster, owner, alice, space_id = _pair()
    try:
        host = owner.fs.sync_host(space_id)

        def exhausted(*_args, **_kwargs):
            raise OrgFsError("resource-exhausted", {"message": "full"})

        owner.fs.apply_remote = exhausted
        assert host.apply_remote("meta", b"", EMPTY, "node-alice") == Pending("dependency")
    finally:
        owner.close()
        alice.close()


def test_a_forged_row_without_author_is_rejected_invalid_argument() -> None:
    cluster, owner, alice, space_id = _pair()
    try:
        forged = forge_tree_row(
            _state(owner, space_id, "tree"),
            "evil",
            {"node_id": "evil", "parent": "root", "name": "evil", "kind": "dir",
             "doc_id": None, "blob_hash": None, "deleted": False},
            author=None,
            client_id=55,
        )
        outcome = owner.fs.sync_host(space_id).apply_remote("tree", forged.update, forged.vector, "x")
        assert isinstance(outcome, Rejected) and outcome.code == "invalid-argument"
        assert owner.fs.listdir(space_id, "") == ()
    finally:
        owner.close()
        alice.close()


def test_an_observed_row_disappearance_deletes_the_content_file_and_releases_blobs() -> None:
    cluster, owner, alice, space_id = _pair()
    try:
        owner.fs.mkdir(space_id, "folder")
        text = owner.fs.write_text(space_id, "folder/a.md", "text")
        blob = owner.fs.write_bytes(space_id, "folder/b.bin", b"payload")
        kept = owner.fs.write_bytes(space_id, "kept.bin", b"payload")
        cluster.deliver()
        assert alice.fs.read_text(space_id, "folder/a.md")[0] == "text"
        assert text.doc_id in alice.backing(space_id).documents
        seen = []
        alice.fs.watch(space_id, "**", seen.append)

        owner.fs.remove(space_id, "folder", permanent=True)
        assert text.doc_id not in owner.backing(space_id).documents
        cluster.deliver()

        assert text.doc_id not in alice.backing(space_id).documents
        assert [node.path for node in alice.fs.listdir(space_id, "", recursive=True)] == ["kept.bin"]
        assert alice.fs.trash(space_id) == ()
        assert alice.blobs.released == []
        assert blob.blob_hash == kept.blob_hash
        owner.fs.remove(space_id, "kept.bin", permanent=True)
        cluster.deliver()
        assert alice.blobs.released == [(space_id, kept.blob_hash)]
        assert owner.blobs.released == [(space_id, kept.blob_hash)]
        eventually(lambda: _assert_removed(seen, {"folder", "folder/a.md", "folder/b.bin", "kept.bin"}))
    finally:
        owner.close()
        alice.close()


def _assert_removed(events, paths: set[str]) -> None:
    assert {event.node.path for event in events if event.kind == "removed"} == paths


def test_a_lying_vector_cannot_smuggle_a_write_past_admission() -> None:
    owner = Node(OWNER, "node-owner")
    space = owner.fs.create_space("group-acme")
    space_id = space.space_id
    owner.fs.invite(space_id, ALICE, "rw")
    owner.fs.invite(space_id, BOB, "rw")
    owner.fs.mkdir(space_id, "directory")
    owner.fs.mkdir(space_id, "directory/leaves")
    alice = clone_node(owner, space_id, ALICE, "node-alice")
    cluster = Cluster(space_id, [owner, alice])
    try:
        alice.fs.write_text(space_id, "directory/leaves/alice.json", "v1")
        cluster.deliver()
        doc_id = doc_id_of(owner.fs, space_id, "directory/leaves/alice.json")
        sender = Doc(client_id=2)
        sender.apply_update(_state(owner, space_id, doc_id))
        start = sender.get_state()
        _append(sender, "+alice", ALICE)
        middle = sender.get_state()
        predecessor = (sender.get_update(start), middle)
        _append(sender, "+bob", BOB)
        smuggled = sender.get_update(middle)
        host = owner.fs.sync_host(space_id)

        assert host.apply_remote(doc_id, smuggled, sender.get_state(), "node-bob") == Pending("dependency")
        assert host.apply_remote(doc_id, smuggled, EMPTY, "node-bob") == Accepted()
        assert host.apply_remote(doc_id, *predecessor, "node-alice") == Accepted()

        assert owner.fs.read_text(space_id, "directory/leaves/alice.json")[0] == "v1+alice"
        assert owner.fs.document_authors(space_id, "directory/leaves/alice.json") == (ALICE,)
        reopened = Node(OWNER, "node-owner-2")
        reopened.stores.backings[space_id] = owner.backing(space_id)
        reopened.fs.load_space(space_id)
        try:
            assert reopened.fs.read_text(space_id, "directory/leaves/alice.json")[0] == "v1+alice"
        finally:
            reopened.close()
    finally:
        owner.close()
        alice.close()


def test_a_forged_content_edit_of_a_protected_record_is_rejected() -> None:
    owner = Node(OWNER, "node-owner")
    space_id = owner.fs.create_space("group-acme").space_id
    owner.fs.invite(space_id, ALICE, "rw")
    owner.fs.invite(space_id, BOB, "rw")
    owner.fs.mkdir(space_id, "directory")
    owner.fs.mkdir(space_id, "directory/leaves")
    alice = clone_node(owner, space_id, ALICE, "node-alice")
    cluster = Cluster(space_id, [owner, alice])
    try:
        alice.fs.write_text(space_id, "directory/leaves/alice.json", "v1")
        cluster.deliver()
        doc_id = doc_id_of(owner.fs, space_id, "directory/leaves/alice.json")
        forged = forge_content(
            doc_id, _state(owner, space_id, doc_id), "v2", author=BOB, clock=True, client_id=9
        )
        outcome = owner.fs.sync_host(space_id).apply_remote(doc_id, forged.update, forged.vector, "b")
        assert isinstance(outcome, Rejected) and outcome.code == "protected-path"
        assert owner.fs.read_text(space_id, "directory/leaves/alice.json")[0] == "v1"
        with_bob = owner.fs._content_doc(owner.fs._space(space_id), doc_id)
        assert document_authors(with_bob.doc) == (ALICE,)
    finally:
        owner.close()
        alice.close()


def test_local_writes_check_the_protected_path_rule() -> None:
    owner = Node(OWNER, "node-owner")
    space_id = owner.fs.create_space("group-acme").space_id
    try:
        owner.fs.invite(space_id, ALICE, "rw")
        owner.fs.mkdir(space_id, "directory")
        owner.fs.mkdir(space_id, "directory/leaves")
        before = owner.fs.listdir(space_id, "", recursive=True)
        try:
            owner.fs.write_text(space_id, "directory/leaves/alice.json", "forged by owner")
        except OrgFsError as error:
            assert error.code == "protected-path"
        else:
            raise AssertionError("the owner wrote alice's leave record")
        assert owner.fs.listdir(space_id, "", recursive=True) == before
    finally:
        owner.close()


def _append(doc: Doc, text: str, author: str) -> None:
    with local_commit(doc, "content", author):
        body = doc.get("text", type=Text)
        body.insert(len(body), text)



def test_a_failed_remote_commit_is_pending_dependency_and_retried_cleanly() -> None:
    cluster, owner, alice, space_id = _pair()
    try:
        alice.fs.mkdir(space_id, "from-alice")
        ((_sender, doc_id, update, vector_bytes),) = [f for f in cluster.take() if f[1] == "tree"]
        host = owner.fs.sync_host(space_id)
        owner.fs._registry(space_id).store.fail_next("commit")

        assert host.apply_remote(doc_id, update, vector_bytes, "node-alice") == Pending("dependency")
        assert owner.fs.listdir(space_id, "") == ()
        assert host.apply_remote(doc_id, update, vector_bytes, "node-alice") == Accepted()
        assert [item.path for item in owner.fs.listdir(space_id, "")] == ["from-alice"]
    finally:
        owner.close()
        alice.close()


def test_a_remote_commit_that_breaks_the_store_reopens_it() -> None:
    cluster, owner, alice, space_id = _pair()
    try:
        alice.fs.mkdir(space_id, "from-alice")
        ((_sender, doc_id, update, vector_bytes),) = [f for f in cluster.take() if f[1] == "tree"]
        registry = owner.fs._registry(space_id)
        store = registry.store
        store.fail_next("commit.after")

        outcome = owner.fs.sync_host(space_id).apply_remote(doc_id, update, vector_bytes, "node-alice")

        assert outcome == Pending("dependency")
        assert registry.store is not store and registry.store.recovered_batch
        assert [item.path for item in owner.fs.listdir(space_id, "")] == ["from-alice"]
        owner.fs.mkdir(space_id, "after")
    finally:
        owner.close()
        alice.close()


def test_modified_by_and_via_come_from_the_row_on_every_node() -> None:
    owner = Node(OWNER, "node-owner")
    space_id = owner.fs.create_space("team").space_id
    owner.fs.invite(space_id, ALICE, "rw")
    alice = clone_node(owner, space_id, ALICE, "node-alice", actor="agent:helper")
    cluster = Cluster(space_id, [owner, alice])
    try:
        created = alice.fs.write_text(space_id, "by-agent.md", "x")
        assert (created.modified_by, created.modified_via) == (ALICE, "agent:helper")
        cluster.deliver()
        remote = owner.fs.stat(space_id, "by-agent.md")
        assert (remote.modified_by, remote.modified_via, remote.version) == (
            ALICE,
            "agent:helper",
            created.version,
        )
        owner.fs.write_text(space_id, "by-agent.md", "y")
        local = owner.fs.stat(space_id, "by-agent.md")
        assert (local.modified_by, local.modified_via) == (OWNER, None)
        host = owner.fs.sync_host(space_id)
        assert host.requester_allowed(ALICE) and not host.requester_allowed("user:stranger")
        owner.fs.remove_member(space_id, ALICE)
        assert not host.requester_allowed(ALICE)
    finally:
        owner.close()
        alice.close()
