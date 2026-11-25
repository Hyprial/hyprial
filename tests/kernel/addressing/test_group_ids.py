"""Group conversation (``g.``) id grammar and minting (slice GC1)."""

from __future__ import annotations

import pytest

from hyprial.kernel import (
    is_agent_id_segment,
    is_cell_id_segment,
    is_device_id_segment,
    is_group_id_segment,
    is_thread_id_segment,
    is_user_id_segment,
    mint_cell_id,
    mint_group_id,
    mint_thread_id,
)
from hyprial.kernel.interface import is_group_id_segment as face_is_group_id_segment
from hyprial.kernel.interface import mint_group_id as face_mint_group_id


def test_a_seeded_group_id_is_pinned_and_deterministic() -> None:
    assert mint_group_id(b"release-key") == "g.2coj3ltqxbu75xcwzm3hxr4kt4"
    assert mint_group_id(b"") == "g.y3apvz3grim7dt6a4sq56xolnm"
    assert mint_group_id(b"release-key") == mint_group_id(b"release-key")
    assert mint_group_id(b"release-key") != mint_group_id(b"release-key2")


def test_an_unseeded_group_id_is_random_and_well_formed() -> None:
    first, second = mint_group_id(), mint_group_id()
    assert first != second
    assert is_group_id_segment(first) and is_group_id_segment(second)


def test_the_group_domain_tag_differs_from_thread_and_cell() -> None:
    seed = b"same-seed"
    group = mint_group_id(seed)
    assert group[2:] != mint_cell_id(seed)[2:]
    assert group[2:] != mint_thread_id(seed)[2:]


@pytest.mark.parametrize(
    ("segment", "expected"),
    [
        ("g." + "a" * 26, True),
        ("g." + "a" * 25, False),
        ("g." + "a" * 27, False),
        ("g." + "1" * 26, False),
        ("g." + "A" * 26, False),
        ("G." + "a" * 26, False),
        ("c." + "a" * 26, False),
        ("dc." + "a" * 26, False),
        ("g" + "a" * 26, False),
    ],
)
def test_group_id_segment_exact_boundaries(segment: str, expected: bool) -> None:
    assert is_group_id_segment(segment) is expected


@pytest.mark.parametrize("seed", [None, b"release-key", b"x" * 64])
def test_a_group_id_matches_no_other_id_shape(seed: bytes | None) -> None:
    group = mint_group_id(seed)
    for other in (
        is_agent_id_segment,
        is_thread_id_segment,
        is_cell_id_segment,
        is_device_id_segment,
        is_user_id_segment,
    ):
        assert not other(group)
    assert not group.startswith(("a.", "t.", "c.", "d.", "u.", "dc.", "p."))


def test_the_interface_face_exports_the_group_helpers() -> None:
    assert face_mint_group_id is mint_group_id
    assert face_is_group_id_segment is is_group_id_segment
