"""Bounded rejection memory and policy generation (design §3.7)."""

from __future__ import annotations

from pycrdt import Doc, Map

from hyprial.daemon.impl.orgfs.membership import (
    REJECTION_MEMORY_CAPACITY,
    RejectionMemory,
    policy_generation,
)


def test_policy_generation_is_canonical_and_follows_meta_vector() -> None:
    first = Doc(client_id=4)
    first.get("members", type=Map)["user:a"] = Map({"mode": "rw"})
    second = Doc(client_id=2)
    second.apply_update(first.get_update())
    second.get("members", type=Map)["user:b"] = Map({"mode": "rw"})
    first.apply_update(second.get_update())
    assert policy_generation(first.get_state()) == policy_generation(second.get_state())
    before = policy_generation(first.get_state())
    first.get("members", type=Map)["user:c"] = Map({"mode": "ro"})
    assert policy_generation(first.get_state()) != before


def test_memory_hits_only_same_doc_update_and_generation() -> None:
    memory = RejectionMemory()
    assert not memory.seen("doc-1", b"u", "g1")
    memory.remember("doc-1", b"u", "g1")
    assert memory.seen("doc-1", b"u", "g1")
    assert not memory.seen("doc-2", b"u", "g1")
    assert not memory.seen("doc-1", b"v", "g1")
    assert not memory.seen("doc-1", b"u", "g2")


def test_memory_is_bounded_lru() -> None:
    memory = RejectionMemory(capacity=3)
    for index in range(3):
        memory.remember("doc", bytes([index]), "g")
    assert memory.seen("doc", bytes([0]), "g")
    memory.remember("doc", bytes([3]), "g")
    assert memory.seen("doc", bytes([0]), "g")
    assert not memory.seen("doc", bytes([1]), "g")
    assert len(memory) == 3


def test_default_capacity_is_1024() -> None:
    assert REJECTION_MEMORY_CAPACITY == 1024
    memory = RejectionMemory()
    for index in range(1100):
        memory.remember("doc", index.to_bytes(2, "big"), "g")
    assert len(memory) == 1024
