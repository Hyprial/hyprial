"""Thread (``t.``) and cell (``c.``) stable-id grammar and minting."""

from __future__ import annotations

import pytest

from hyprial.kernel import (
    is_cell_id_segment,
    is_identity_id_segment,
    is_thread_id_segment,
    mint_cell_id,
    mint_thread_id,
)


@pytest.mark.parametrize(
    ("segment", "expected"),
    [
        ("t." + "a" * 26, True),
        ("t." + "a" * 25, False),
        ("t." + "a" * 27, False),
        ("t." + "0" * 26, False),
        ("t." + "A" * 26, False),
        ("T." + "a" * 26, False),
        ("c." + "a" * 26, False),
    ],
)
def test_thread_id_segment_exact_boundaries(segment: str, expected: bool) -> None:
    assert is_thread_id_segment(segment) is expected


@pytest.mark.parametrize(
    ("segment", "expected"),
    [
        ("c." + "a" * 26, True),
        ("c." + "a" * 25, False),
        ("c." + "a" * 27, False),
        ("c." + "1" * 26, False),
        ("C." + "a" * 26, False),
        ("t." + "a" * 26, False),
    ],
)
def test_cell_id_segment_exact_boundaries(segment: str, expected: bool) -> None:
    assert is_cell_id_segment(segment) is expected


def test_thread_and_cell_ids_are_identity_shaped_so_names_cannot_take_them() -> None:
    assert is_identity_id_segment("t." + "a" * 26)
    assert is_identity_id_segment("c." + "a" * 26)


def test_group_ids_are_identity_shaped_so_names_cannot_take_them() -> None:
    assert is_identity_id_segment("g." + "a" * 26)
    assert not is_identity_id_segment("g." + "a" * 25)
    assert not is_identity_id_segment("g." + "1" * 26)


def test_minting_from_a_seed_is_deterministic_and_domain_separated() -> None:
    seed = b"agent:o:m:a\0agent:o:m:b"
    thread = mint_thread_id(seed)
    assert thread == mint_thread_id(seed)
    assert is_thread_id_segment(thread)
    cell = mint_cell_id(seed)
    assert is_cell_id_segment(cell)
    assert thread[2:] != cell[2:]
    assert mint_thread_id(seed + b"x") != thread


def test_minting_without_a_seed_is_random() -> None:
    first, second = mint_thread_id(), mint_thread_id()
    assert first != second
    assert is_thread_id_segment(first) and is_thread_id_segment(second)
    assert mint_cell_id() != mint_cell_id()


def test_keyed_cell_id_is_deterministic_and_separates_kind_authority_and_key() -> None:
    from hyprial.kernel import keyed_cell_id

    cell = keyed_cell_id("conversation", "user:alice", "t.x\0")
    assert is_cell_id_segment(cell)
    assert cell == keyed_cell_id("conversation", "user:alice", "t.x\0")
    variants = {
        keyed_cell_id("department", "user:alice", "t.x\0"),
        keyed_cell_id("conversation", "user:bob", "t.x\0"),
        keyed_cell_id("conversation", "user:alice", "t.y\0"),
        keyed_cell_id("conversation", "user:alice\0t.x", ""),
    }
    assert cell not in variants and len(variants) == 4


def test_subject_cell_id_differs_from_keyed_and_from_plain_minting() -> None:
    from hyprial.kernel import keyed_cell_id, subject_cell_id

    agent_id = "a." + "b" * 26
    cell = subject_cell_id("agent", agent_id)
    assert is_cell_id_segment(cell)
    assert cell == subject_cell_id("agent", agent_id)
    assert cell != subject_cell_id("department", agent_id)
    assert cell != keyed_cell_id("agent", "", agent_id)
    assert cell != mint_cell_id(agent_id.encode())


@pytest.mark.parametrize("kind", ["", "Agent", "a\0b", "x" * 65])
def test_cell_id_helpers_reject_an_invalid_kind(kind: str) -> None:
    from hyprial.kernel import keyed_cell_id, subject_cell_id

    with pytest.raises(ValueError):
        keyed_cell_id(kind, "user:alice", "k")
    with pytest.raises(ValueError):
        subject_cell_id(kind, "a." + "b" * 26)


def test_subject_cell_id_rejects_an_empty_subject() -> None:
    from hyprial.kernel import subject_cell_id

    with pytest.raises(ValueError):
        subject_cell_id("agent", "")
