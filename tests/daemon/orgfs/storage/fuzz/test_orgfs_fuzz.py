from __future__ import annotations

import base64
from dataclasses import dataclass
import json
import os
import random
from types import SimpleNamespace
from typing import Any, Callable, cast

from pycrdt import Array, Doc
import pytest

from hyprial.daemon.impl.orgfs import LocalOrgFs, OrgFsError


OP_FUZZ_SEEDS = (0x15A001, 0x15A002, 0x15A003)
MOVE_FUZZ_SEEDS = (0x15B001, 0x15B002, 0x15B003)


@pytest.fixture(autouse=True)
def owned_facades(monkeypatch: pytest.MonkeyPatch):
    facade_type = LocalOrgFs
    facades: list[LocalOrgFs] = []

    def create(*args, **kwargs):
        facade = facade_type(*args, **kwargs)
        facades.append(facade)
        return facade

    monkeypatch.setattr(f"{__name__}.LocalOrgFs", create)
    yield
    for facade in reversed(facades):
        assert facade.close_effects()


class ReplicaStore:
    """In-file durable seam which exports real pycrdt update bytes."""

    def __init__(self, node: str) -> None:
        self.node = node
        self.space_id: str | None = None
        self.docs: dict[str, Doc] = {}
        self.outbound: list[bytes] = []
        self._seq: dict[str, int] = {}

    def commit(
        self,
        doc_id: str,
        mutate: Callable[[Doc], None],
        *,
        author: str,
        actor: str | None,
    ) -> None:
        document = self.docs.setdefault(doc_id, Doc())
        mutate(document)
        # Export the real pycrdt state update.  Full state updates are valid
        # Yjs updates and let this facade-focused fuzz deliberately scramble
        # envelopes without making the in-file store fake responsible for
        # durable per-writer causal buffering.
        update = document.get_update()
        seq = self._seq.get(doc_id, 0)
        self._seq[doc_id] = seq + 1
        envelope = {
            "schemaVersion": 1,
            "type": "orgfs-update",
            "spaceId": self.space_id or "pending-space-id",
            "docId": doc_id,
            "seq": seq,
            "origin": {
                "writer": f"{self.node}.test.lane",
                "node": self.node,
                "author": author,
                "actor": actor,
                "committedAt": "2026-09-26T00:00:00Z",
                "metaFrontier": "",
            },
            "update": base64.b64encode(update).decode(),
        }
        self.outbound.append(
            json.dumps(envelope, sort_keys=True, separators=(",", ":")).encode()
        )

    def commit_many(self, mutations, *, author: str, actor: str | None) -> tuple[()]:
        for doc_id, mutate in mutations:
            self.commit(doc_id, mutate, author=author, actor=actor)
        return ()

    def bind_space(self, space_id: str) -> None:
        self.space_id = space_id
        rewritten: list[bytes] = []
        for envelope in self.outbound:
            value = json.loads(envelope)
            value["spaceId"] = space_id
            rewritten.append(
                json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
            )
        self.outbound = rewritten

    def import_envelope(self, envelope: bytes) -> None:
        value = json.loads(envelope)
        update = base64.b64decode(value["update"], validate=True)
        self.docs.setdefault(str(value["docId"]), Doc()).apply_update(update)

    def snapshot(self, doc_id: str, *, shallow_since: bytes | None) -> SimpleNamespace:
        del shallow_since
        return SimpleNamespace(snapshot_bytes=self.docs[doc_id].get_update())

    def unconfirmed_commits(self) -> int:
        return 0

    def holders_seen(self) -> tuple[tuple[str, bool], ...]:
        return ()


@dataclass
class Replica:
    fs: LocalOrgFs
    store: ReplicaStore


@dataclass
class OpState:
    rng: random.Random
    live: list[str]
    serial: int = 0


def _seed_values(name: str, fixed: tuple[int, ...]) -> tuple[int, ...]:
    override = os.environ.get(name)
    return fixed if override is None else (*fixed, int(override, 0))


def _make_replicas(
    count: int,
    setup: Callable[[LocalOrgFs, str], None],
) -> tuple[str, list[Replica]]:
    owner_store = ReplicaStore("node-0")
    owner = LocalOrgFs(
        owner_store,
        author="user:0",
        actor="agent:0:test:orgfs",
        node_id="node-0",
    )
    space_id = owner.create_space("fuzz").space_id
    owner_store.bind_space(space_id)
    for index in range(1, count):
        owner.invite(space_id, f"user:{index}", "rw")
    setup(owner, space_id)

    baseline = tuple(owner_store.outbound)
    replicas = [Replica(owner, owner_store)]
    for index in range(1, count):
        store = ReplicaStore(f"node-{index}")
        store.bind_space(space_id)
        for envelope in baseline:
            store.import_envelope(envelope)
        facade = LocalOrgFs(
            store,
            author=f"user:{index}",
            actor=f"agent:{index}:test:orgfs",
            node_id=f"node-{index}",
        )
        facade.load_space(space_id)
        replicas.append(Replica(facade, store))
    for replica in replicas:
        replica.store.outbound.clear()
    return space_id, replicas


def _exchange_all(replicas: list[Replica], seed: int) -> None:
    outbound = [tuple(replica.store.outbound) for replica in replicas]
    for target_index, target in enumerate(replicas):
        deliveries = [
            envelope
            for source_index, envelopes in enumerate(outbound)
            if source_index != target_index
            for envelope in envelopes
        ]
        random.Random(seed ^ (target_index << 16)).shuffle(deliveries)
        for envelope in deliveries:
            value = json.loads(envelope)
            assert base64.b64decode(value["update"], validate=True)
            target.store.import_envelope(envelope)
            target.fs.apply_envelope(_space_id(target.fs), envelope)


def _space_id(fs: LocalOrgFs) -> str:
    spaces = fs.spaces()
    assert len(spaces) == 1
    return spaces[0].space_id


def _visible_snapshot(fs: LocalOrgFs, space_id: str) -> tuple[tuple[object, ...], ...]:
    rows: list[tuple[object, ...]] = []
    seen: set[str] = set()

    def visit(parent: str) -> None:
        for item in fs.listdir(space_id, parent):
            assert item.node_id not in seen, f"cycle or duplicate projection at {item.node_id}"
            seen.add(item.node_id)
            content = fs.read_text(space_id, f"id:{item.node_id}")[0] if item.kind == "doc" else None
            rows.append((item.node_id, item.kind, item.path, content))
            if item.kind == "dir":
                visit(f"id:{item.node_id}")

    visit("")
    rows.extend((item.node_id, item.kind, item.name, "deleted") for item in fs.trash(space_id))
    return tuple(sorted(rows))


def test_two_replica_randomized_public_api_operations_converge() -> None:
    """Review item 15a: fixed seeds 0x15A001..3 plus ORGFS_OP_FUZZ_SEED."""

    for seed in _seed_values("ORGFS_OP_FUZZ_SEED", OP_FUZZ_SEEDS):
        def setup(fs: LocalOrgFs, space_id: str) -> None:
            for index in range(2):
                fs.mkdir(space_id, f"r{index}")
                fs.mkdir(space_id, f"r{index}/nest")

        space_id, replicas = _make_replicas(2, setup)
        states = [OpState(random.Random(seed ^ index), []) for index in range(2)]
        schedule = [
            (index, operation)
            for index in range(2)
            for operation in ["create", "write", "move", "rename", "remove"] * 5
        ]
        random.Random(seed).shuffle(schedule)
        for replica_index, operation in schedule:
            replica = replicas[replica_index]
            state = states[replica_index]
            rng = state.rng
            live = state.live
            serial = state.serial
            if operation == "create" or not live:
                serial += 1
                info = replica.fs.write_text(
                    space_id,
                    f"r{replica_index}/f{serial}.txt",
                    f"created-{replica_index}-{serial}",
                )
                live.append(info.node_id)
            elif operation == "write":
                node_id = rng.choice(live)
                current, version = replica.fs.read_text(space_id, f"id:{node_id}")
                replica.fs.write_text(
                    space_id,
                    f"id:{node_id}",
                    current + f"|w{serial}",
                    base_version=version,
                )
            elif operation == "move":
                node_id = rng.choice(live)
                current = replica.fs.stat(space_id, f"id:{node_id}")
                destination = (
                    f"r{replica_index}"
                    if "/nest/" in f"/{current.path}/"
                    else f"r{replica_index}/nest"
                )
                replica.fs.move(space_id, f"id:{node_id}", destination)
            elif operation == "rename":
                node_id = rng.choice(live)
                current = replica.fs.stat(space_id, f"id:{node_id}")
                serial += 1
                parent = current.path.rpartition("/")[0]
                destination = "/".join(filter(None, (parent, f"renamed-{serial}.txt")))
                replica.fs.move(space_id, f"id:{node_id}", destination)
            else:
                node_id = rng.choice(live)
                replica.fs.remove(space_id, f"id:{node_id}")
                live.remove(node_id)
            state.serial = serial

        _exchange_all(replicas, seed)
        assert _visible_snapshot(replicas[0].fs, space_id) == _visible_snapshot(
            replicas[1].fs, space_id
        )


def test_three_replica_randomized_concurrent_moves_converge_without_loss() -> None:
    """Review item 15b: fixed seeds 0x15B001..3 plus ORGFS_MOVE_FUZZ_SEED."""

    for seed in _seed_values("ORGFS_MOVE_FUZZ_SEED", MOVE_FUZZ_SEEDS):
        node_ids: list[str] = []

        def setup(fs: LocalOrgFs, space_id: str) -> None:
            for index in range(7):
                node_ids.append(fs.mkdir(space_id, f"n{index}").node_id)

        space_id, replicas = _make_replicas(3, setup)
        for replica_index, replica in enumerate(replicas):
            rng = random.Random(seed ^ replica_index)
            successful = 0
            for _ in range(40):
                node_id, parent_id = rng.sample(node_ids, 2)
                try:
                    replica.fs.move(space_id, f"id:{node_id}", f"id:{parent_id}")
                except OrgFsError as exc:
                    assert exc.code == "invalid-argument"
                else:
                    successful += 1
                if successful == 12:
                    break
            assert successful == 12

        _exchange_all(replicas, seed)
        snapshots = [_visible_snapshot(replica.fs, space_id) for replica in replicas]
        assert snapshots[1:] == snapshots[:-1]
        assert {row[0] for row in snapshots[0]} == set(node_ids)


def test_mutual_move_uses_facade_and_real_updates_without_cycles() -> None:
    """Review item 15c: A->B || B->A uses facade clocks and real update bytes."""

    ids: dict[str, str] = {}

    def setup(fs: LocalOrgFs, space_id: str) -> None:
        ids["a"] = fs.mkdir(space_id, "a").node_id
        ids["b"] = fs.mkdir(space_id, "b").node_id

    space_id, replicas = _make_replicas(2, setup)
    replicas[0].fs.move(space_id, f"id:{ids['a']}", f"id:{ids['b']}")
    replicas[1].fs.move(space_id, f"id:{ids['b']}", f"id:{ids['a']}")

    _exchange_all(replicas, 0x15C)
    snapshots = [_visible_snapshot(replica.fs, space_id) for replica in replicas]
    assert snapshots[0] == snapshots[1]
    assert {row[0] for row in snapshots[0]} == set(ids.values())


def test_concurrent_text_writes_preserve_both_insertions() -> None:
    """Review item 15d: each facade writes from its own base_version."""

    node_id = ""

    def setup(fs: LocalOrgFs, space_id: str) -> None:
        nonlocal node_id
        node_id = fs.write_text(space_id, "shared.txt", "").node_id

    space_id, replicas = _make_replicas(2, setup)
    for index, replica in enumerate(replicas):
        _content, base_version = replica.fs.read_text(space_id, f"id:{node_id}")
        replica.fs.write_text(
            space_id,
            f"id:{node_id}",
            chr(ord("A") + index),
            base_version=base_version,
        )

    _exchange_all(replicas, 0x15D)
    contents = [replica.fs.read_text(space_id, f"id:{node_id}")[0] for replica in replicas]
    assert contents[0] == contents[1]
    assert sorted(contents[0]) == ["A", "B"]


def test_same_list_item_concurrent_delete_insert_duplicates_observed() -> None:
    """Review item 15j: pycrdt delete+insert observes one duplicate after merge."""

    node_id = ""

    def setup(fs: LocalOrgFs, space_id: str) -> None:
        nonlocal node_id
        node_id = fs.write_text(space_id, "list.json", "").node_id

    space_id, replicas = _make_replicas(2, setup)
    documents = [
        cast(Any, replica.fs.open_doc(space_id, f"id:{node_id}"))._document.doc
        for replica in replicas
    ]
    items = documents[0].get("items", type=Array)
    before_base = documents[0].get_state()
    items.extend(["a", "x", "b", "c"])
    base_update = documents[0].get_update(before_base)
    documents[1].apply_update(base_update)

    before = [document.get_state() for document in documents]
    left = documents[0].get("items", type=Array)
    left.pop(1)
    left.insert(3, "x")
    right = documents[1].get("items", type=Array)
    right.pop(1)
    right.insert(0, "x")
    updates = [document.get_update(state) for document, state in zip(documents, before)]
    documents[0].apply_update(updates[1])
    documents[1].apply_update(updates[0])

    observed = [document.get("items", type=Array).to_py() for document in documents]
    assert observed[0] == observed[1]
    assert observed[0].count("x") == 2
    assert sorted(observed[0]) == ["a", "b", "c", "x", "x"]
