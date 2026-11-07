from __future__ import annotations

import base64
from dataclasses import dataclass
import json
from typing import Any, Callable, cast

import pytest
from pycrdt import Doc

from hyprial.daemon.impl.orgfs import LocalOrgFs
from hyprial.daemon.impl.orgfs.api import NodeInfo, OrgFsError
from hyprial.daemon.impl.orgfs.runtime import OrgFsRuntime
from hyprial.daemon.impl.orgfs.document.structured import DocList, DocMap, DocText, StructuredOrgDoc


@dataclass(frozen=True, slots=True)
class _Envelope:
    update: bytes
    author: str
    actor: str | None


class _Replica:
    """File-local production-shaped commit seam using real pycrdt updates."""

    def __init__(self, client_id: int, *, author: str, actor: str | None) -> None:
        self.client_id = client_id
        self.author = author
        self.actor = actor
        self.document = Doc(client_id=client_id)
        self.envelopes: list[_Envelope] = []
        self.revision = 0
        self.handle = StructuredOrgDoc(self.document, "doc-test", self.commit)

    def commit(self, mutate: Callable[[Doc], None]) -> NodeInfo:
        working = Doc(client_id=self.client_id)
        working.apply_update(self.document.get_update())
        before = working.get_state()
        mutate(working)
        update = working.get_update(before)
        if update in (b"", b"\x00\x00"):
            raise AssertionError("test commit unexpectedly produced no update")
        envelope = _Envelope(update, self.author, self.actor)
        self.envelopes.append(envelope)
        self.document.apply_update(update)
        self.revision += 1
        return NodeInfo(
            space_id="space-test",
            node_id="node-test",
            kind="doc",
            name="test.card",
            path="test.card",
            size=None,
            blob_hash=None,
            doc_id="doc-test",
            version=base64.b64encode(self.document.get_state()).decode("ascii"),
            modified_by=self.author,
            modified_via=self.actor,
            name_conflict=False,
            deleted=False,
            uri="orgfs:test:space-test:node-test",
        )

    def receive(self, envelope: _Envelope) -> None:
        self.document.apply_update(envelope.update)


def _pair() -> tuple[_Replica, _Replica]:
    return (
        _Replica(101, author="user:left", actor="agent:left:test:orgfs"),
        _Replica(202, author="user:right", actor="agent:right:test:orgfs"),
    )


def _seed_and_sync(
    left: _Replica, right: _Replica, mutate: Callable[[DocMap], None]
) -> None:
    left.handle.transact(mutate)
    right.receive(left.envelopes[-1])
    left.envelopes.clear()
    right.envelopes.clear()


def _exchange(left: _Replica, right: _Replica) -> None:
    left_updates = tuple(left.envelopes)
    right_updates = tuple(right.envelopes)
    for envelope in right_updates:
        left.receive(envelope)
    for envelope in left_updates:
        right.receive(envelope)


def _map(handle: StructuredOrgDoc, key: str) -> DocMap:
    value = handle.root().get(key)
    assert isinstance(value, DocMap)
    return value


def _list(handle: StructuredOrgDoc, key: str) -> DocList:
    value = handle.root().get(key)
    assert isinstance(value, DocList)
    return value


def _text(handle: StructuredOrgDoc, key: str) -> DocText:
    value = handle.root().get(key)
    assert isinstance(value, DocText)
    return value


def test_concurrent_map_set_distinct_keys_both_survive() -> None:
    left, right = _pair()
    _seed_and_sync(left, right, lambda root: root.ensure_map("fields"))

    left.handle.transact(lambda root: root.ensure_map("fields").set("left", True))
    right.handle.transact(lambda root: root.ensure_map("fields").set("right", True))
    _exchange(left, right)

    assert _map(left.handle, "fields").get("left") is True
    assert _map(left.handle, "fields").get("right") is True
    assert left.handle.snapshot_json() == right.handle.snapshot_json()


def test_concurrent_list_insert_both_survive() -> None:
    left, right = _pair()

    def seed(root: DocMap) -> None:
        root.ensure_list("items").insert(0, "base")

    _seed_and_sync(left, right, seed)
    left.handle.transact(lambda root: root.ensure_list("items").insert(0, "left"))
    right.handle.transact(lambda root: root.ensure_list("items").insert(1, "right"))
    _exchange(left, right)

    observed = [_list(replica.handle, "items") for replica in (left, right)]
    values = [[items.get(index) for index in range(items.len())] for items in observed]
    assert values[0] == values[1]
    assert sorted(values[0]) == ["base", "left", "right"]


def test_concurrent_text_edit_merges_no_stomp() -> None:
    left, right = _pair()

    def seed(root: DocMap) -> None:
        root.ensure_text("body").insert(0, "middle")

    _seed_and_sync(left, right, seed)
    left.handle.transact(lambda root: root.ensure_text("body").insert(0, "left-"))
    right.handle.transact(lambda root: root.ensure_text("body").insert(6, "-right"))
    _exchange(left, right)

    assert _text(left.handle, "body").str() == "left-middle-right"
    assert left.handle.snapshot_json() == right.handle.snapshot_json()


def test_same_list_item_concurrent_reorder_behavior_recorded() -> None:
    left, right = _pair()

    def seed(root: DocMap) -> None:
        items = root.ensure_list("items")
        for index, value in enumerate(("a", "x", "b", "c")):
            items.insert(index, value)

    _seed_and_sync(left, right, seed)

    def move_left(root: DocMap) -> None:
        items = root.ensure_list("items")
        value = items.get(1)
        items.delete(1)
        items.insert(3, value)

    def move_right(root: DocMap) -> None:
        items = root.ensure_list("items")
        value = items.get(1)
        items.delete(1)
        items.insert(0, value)

    left.handle.transact(move_left)
    right.handle.transact(move_right)
    _exchange(left, right)

    values = []
    for replica in (left, right):
        items = _list(replica.handle, "items")
        values.append([items.get(index) for index in range(items.len())])
    assert values[0] == values[1]
    assert values[0].count("x") == 2
    assert sorted(values[0]) == ["a", "b", "c", "x", "x"]


def test_transact_is_single_commit_single_envelope_with_attribution() -> None:
    replica = _Replica(303, author="user:author", actor="agent:author:host:structured")

    def mutate(root: DocMap) -> None:
        fields = root.ensure_map("fields")
        fields.set("done", True)
        fields.set("title", "one durable transaction")

    result = replica.handle.transact(mutate)

    assert len(replica.envelopes) == 1
    assert replica.envelopes[0].author == "user:author"
    assert replica.envelopes[0].actor == "agent:author:host:structured"
    assert result.modified_by == "user:author"
    assert result.modified_via == "agent:author:host:structured"


def test_transact_exception_rolls_back_no_envelope() -> None:
    replica = _Replica(404, author="user:author", actor=None)
    before = replica.handle.snapshot_json()

    def fail(root: DocMap) -> None:
        root.ensure_map("fields").set("partial", True)
        raise RuntimeError("abort")

    with pytest.raises(RuntimeError, match="abort"):
        replica.handle.transact(fail)

    assert replica.envelopes == []
    assert replica.handle.snapshot_json() == before


def test_open_doc_transact_exception_rolls_back_at_facade_seam(
    tmp_path, monkeypatch
) -> None:
    """Review items 11/28: facade and store failures never poison a live handle."""

    facades: list[tuple[LocalOrgFs, object | None]] = [
        (LocalOrgFs(author="user:owner"), None)
    ]
    runtime = OrgFsRuntime(
        tmp_path / "state", node_id="node-owner", author="user:owner"
    )
    facades.append((runtime.facade, runtime))
    try:
        for facade, owner in facades:
            space = facade.create_space("rollback")
            node = facade.write_text(space.space_id, "note.md", "base")
            handle = facade.open_doc(space.space_id, f"id:{node.node_id}")
            before = handle.snapshot_json()

            def abort(root: DocMap) -> None:
                root.ensure_map("fields").set("partial", True)
                raise RuntimeError("abort facade transaction")

            with pytest.raises(RuntimeError, match="abort facade transaction"):
                handle.transact(abort)
            assert handle.snapshot_json() == before

            if owner is not None:
                store = runtime.stores.get(space.space_id)
                real_commit_many = store.commit_many

                def fail_commit(*_args, **_kwargs):
                    raise RuntimeError("journal unavailable")

                monkeypatch.setattr(store, "commit_many", fail_commit)
                with pytest.raises(OrgFsError):
                    handle.transact(
                        lambda root: root.ensure_map("fields").set("leaked", True)
                    )
                assert handle.snapshot_json() == before
                monkeypatch.setattr(store, "commit_many", real_commit_many)

            handle.transact(
                lambda root: root.ensure_map("fields").set("committed", True)
            )
            snapshot = json.loads(handle.snapshot_json())
            assert snapshot["fields"] == {"committed": True}
    finally:
        runtime.close()


def test_write_outside_transact_raises_invalid_argument() -> None:
    replica = _Replica(505, author="user:author", actor=None)
    leaked: list[DocMap] = []

    def seed(root: DocMap) -> None:
        fields = root.ensure_map("fields")
        fields.set("done", False)
        leaked.append(fields)

    replica.handle.transact(seed)
    fields = _map(replica.handle, "fields")

    for write in (
        lambda: replica.handle.root().set("outside", True),
        lambda: replica.handle.root().ensure_list("items"),
        lambda: fields.set("done", True),
        lambda: leaked[0].set("escaped", True),
    ):
        with pytest.raises(OrgFsError) as error:
            write()
        assert error.value.code == "invalid-argument"


def test_m10_direct_root_tombstone_cross_layer_contract_is_declared() -> None:
    """M10: public P1/structured entry points pin the declared asymmetry."""

    facade = LocalOrgFs(author="user:writer")
    space = facade.create_space("structured-delete")
    created = facade.write_text(space.space_id, "body.md", "remove me")
    handle = facade.open_doc(space.space_id, f"id:{created.node_id}")

    handle.transact(lambda root: root.delete("text"))

    assert "text" not in json.loads(handle.snapshot_json())
    assert facade.read_text(space.space_id, f"id:{created.node_id}")[0] == ""
    facade.write_text(space.space_id, f"id:{created.node_id}", "p1-resurrected")
    assert "text" not in json.loads(handle.snapshot_json())
    handle.transact(lambda root: root.set("text", "structured-restored"))
    assert json.loads(handle.snapshot_json())["text"] == "structured-restored"
    assert "Structured tombstones" in (LocalOrgFs.open_doc.__doc__ or "")


def test_open_doc_version_matches_node_version_source() -> None:
    facade = LocalOrgFs(author="user:writer")
    space = facade.create_space("structured-version")
    created = facade.write_text(space.space_id, "body.md", "one")
    handle = facade.open_doc(space.space_id, f"id:{created.node_id}")
    assert (
        handle.version() == facade.stat(space.space_id, f"id:{created.node_id}").version
    )
    result = handle.transact(lambda root: root.ensure_map("fields").set("n", 2))
    assert handle.version() == result.version


def test_text_file_body_visible_and_mergeable_via_container() -> None:
    facade = LocalOrgFs(author="user:writer", actor="agent:writer:test:orgfs")
    space = facade.create_space("structured")
    created = facade.write_text(space.space_id, "body.md", "middle")
    _content, base_version = facade.read_text(space.space_id, f"id:{created.node_id}")
    source = cast(Any, facade.open_doc(space.space_id, f"id:{created.node_id}"))
    source_document = source._document.doc
    baseline = source_document.get_update()

    left, right = _pair()
    left.document.apply_update(baseline)
    right.document.apply_update(baseline)
    left.handle.transact(lambda root: root.ensure_text("text").insert(0, "left-"))

    before_write = source_document.get_state()
    facade.write_text(
        space.space_id,
        f"id:{created.node_id}",
        "middle-right",
        base_version=base_version,
    )
    write_text_update = _Envelope(
        source_document.get_update(before_write),
        "user:writer",
        "agent:writer:test:orgfs",
    )
    right.receive(write_text_update)
    left.receive(write_text_update)
    right.receive(left.envelopes[-1])

    assert _text(left.handle, "text").str() == "left-middle-right"
    assert left.handle.snapshot_json() == right.handle.snapshot_json()


def test_snapshot_json_reflects_committed_state() -> None:
    replica = _Replica(606, author="user:author", actor=None)
    before = replica.handle.version()

    def mutate(root: DocMap) -> None:
        fields = root.ensure_map("fields")
        fields.set("title", "Card")
        fields.set("temporary", True)
        fields.delete("temporary")
        root.ensure_list("checklist").insert(0, {"done": False, "text": "Ship"})
        body = root.ensure_text("body")
        body.insert(0, "Evidence!")
        body.delete(8, 1)

    replica.handle.transact(mutate)

    assert replica.handle.version() != before
    assert json.loads(replica.handle.snapshot_json()) == {
        "body": "Evidence",
        "checklist": [{"done": False, "text": "Ship"}],
        "fields": {"title": "Card"},
    }
