# Decisions (phase D, package T3, 2026-11-25):
# - Replicas are ``LocalOrgFs`` nodes (built directly, independent of
#   ``HYPRIAL_ORGFS_IMPL``) from the coordinator tests' in-process helpers
#   (``tests/daemon/orgfs/coordinator/support.py``, imported read-only): the
#   owner sets the space up, every other replica is a clone of the owner's
#   durable state, and a ``Cluster`` captures each replica's publications.
# - Delivery keeps the old fuzz's scrambling: every replica receives the other
#   replicas' publications in a seeded random order through
#   ``sync_host(space_id).apply_remote``; an update that arrives before its
#   predecessor is answered ``Pending`` (design §3.6) and the replicas then pull
#   from each other (design §4.3) until a round changes nothing.
from __future__ import annotations

from dataclasses import dataclass
import os
import random
from typing import Callable

import pytest

from hyprial.daemon.impl.orgfs import OrgFsError
from hyprial.daemon.impl.orgfs.document.coordinator import LocalOrgFs

from tests.daemon.orgfs.coordinator.support import Cluster, Node, clone_node


OP_FUZZ_SEEDS = (0x15A001, 0x15A002, 0x15A003)
MOVE_FUZZ_SEEDS = (0x15B001, 0x15B002, 0x15B003)


@pytest.fixture(autouse=True)
def owned_nodes():
    nodes: list[Node] = []
    _OWNED.append(nodes)
    yield
    _OWNED.remove(nodes)
    for node in reversed(nodes):
        node.close()


_OWNED: list[list[Node]] = []


@dataclass
class Replica:
    fs: LocalOrgFs
    node: Node


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
) -> tuple[str, list[Replica], Cluster]:
    owner = Node("user:0", "node-0", actor="agent:0:test:orgfs")
    _OWNED[-1].append(owner)
    space_id = owner.fs.create_space("fuzz").space_id
    for index in range(1, count):
        owner.fs.invite(space_id, f"user:{index}", "rw")
    setup(owner.fs, space_id)

    nodes = [owner]
    for index in range(1, count):
        node = clone_node(
            owner, space_id, f"user:{index}", f"node-{index}", actor=f"agent:{index}:test:orgfs"
        )
        _OWNED[-1].append(node)
        nodes.append(node)
    cluster = Cluster(space_id, nodes)
    return space_id, [Replica(node.fs, node) for node in nodes], cluster


def _exchange_all(cluster: Cluster, replicas: list[Replica], seed: int) -> None:
    frames = cluster.take()
    for target_index, target in enumerate(replicas):
        deliveries = [frame for frame in frames if frame[0] != target.node.node_id]
        random.Random(seed ^ (target_index << 16)).shuffle(deliveries)
        host = cluster.host(target.node)
        for sender, doc_id, update, vector in deliveries:
            host.apply_remote(doc_id, update, vector, sender)
    cluster.settle()


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

        space_id, replicas, cluster = _make_replicas(2, setup)
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

        _exchange_all(cluster, replicas, seed)
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

        space_id, replicas, cluster = _make_replicas(3, setup)
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

        _exchange_all(cluster, replicas, seed)
        snapshots = [_visible_snapshot(replica.fs, space_id) for replica in replicas]
        assert snapshots[1:] == snapshots[:-1]
        assert {row[0] for row in snapshots[0]} == set(node_ids)


def test_mutual_move_uses_facade_and_real_updates_without_cycles() -> None:
    """Review item 15c: A->B || B->A uses facade clocks and real update bytes."""

    ids: dict[str, str] = {}

    def setup(fs: LocalOrgFs, space_id: str) -> None:
        ids["a"] = fs.mkdir(space_id, "a").node_id
        ids["b"] = fs.mkdir(space_id, "b").node_id

    space_id, replicas, cluster = _make_replicas(2, setup)
    replicas[0].fs.move(space_id, f"id:{ids['a']}", f"id:{ids['b']}")
    replicas[1].fs.move(space_id, f"id:{ids['b']}", f"id:{ids['a']}")

    _exchange_all(cluster, replicas, 0x15C)
    snapshots = [_visible_snapshot(replica.fs, space_id) for replica in replicas]
    assert snapshots[0] == snapshots[1]
    assert {row[0] for row in snapshots[0]} == set(ids.values())


def test_concurrent_text_writes_preserve_both_insertions() -> None:
    """Review item 15d: each facade writes from its own base_version."""

    node_id = ""

    def setup(fs: LocalOrgFs, space_id: str) -> None:
        nonlocal node_id
        node_id = fs.write_text(space_id, "shared.txt", "").node_id

    space_id, replicas, cluster = _make_replicas(2, setup)
    for index, replica in enumerate(replicas):
        _content, base_version = replica.fs.read_text(space_id, f"id:{node_id}")
        replica.fs.write_text(
            space_id,
            f"id:{node_id}",
            chr(ord("A") + index),
            base_version=base_version,
        )

    _exchange_all(cluster, replicas, 0x15D)
    contents = [replica.fs.read_text(space_id, f"id:{node_id}")[0] for replica in replicas]
    assert contents[0] == contents[1]
    assert sorted(contents[0]) == ["A", "B"]
