# Decisions (phase F1, T3 report §4-1, 2026-11-25):
# - ``break_cycles`` is pinned as a pure function over a placed row map and
#   the move records: the cycle member whose latest move sorts last loses and
#   returns to its ``oldParent`` (or the root), the result does not depend on
#   the order rows or moves are listed in, and acyclic placements are left
#   alone.  The facade-level reproductions are the two fuzz tests in
#   ``storage/fuzz/test_orgfs_fuzz.py``.
"""Deterministic cycle breaking of the state tree's materialization."""

from __future__ import annotations

import itertools
from typing import Any

from hyprial.daemon.impl.orgfs.document.coordinator.registry import break_cycles


def _placed(parents: dict[str, str | None]) -> dict[str, dict[str, Any]]:
    return {node: {"node_id": node, "parent": parent, "name": node} for node, parent in parents.items()}


def _move(ts: int, peer: str, node: str, old: str, new: str) -> dict[str, Any]:
    return {"ts": ts, "peer": peer, "node": node, "oldParent": old, "newParent": new, "name": node}


def _parents(placed: dict[str, dict[str, Any]]) -> dict[str, object]:
    return {node: row["parent"] for node, row in placed.items()}


def test_mutual_moves_keep_the_earlier_one_whatever_the_listing_order() -> None:
    parents = {"root": None, "a": "b", "b": "a"}
    moves = [_move(3, "p1", "a", "root", "b"), _move(3, "p2", "b", "root", "a")]
    results = set()
    for ordered_moves in itertools.permutations(moves):
        for ordered_rows in itertools.permutations(parents.items()):
            placed = break_cycles(_placed(dict(ordered_rows)), list(ordered_moves))
            results.add(tuple(sorted(_parents(placed).items(), key=lambda item: item[0])))
    assert results == {(("a", "b"), ("b", "root"), ("root", None))}


def test_the_loser_returns_to_its_old_parent_when_that_is_acyclic() -> None:
    parents = {"root": None, "home": "root", "a": "b", "b": "a"}
    moves = [_move(1, "p1", "a", "root", "b"), _move(2, "p1", "b", "home", "a")]
    assert _parents(break_cycles(_placed(parents), moves)) == {
        "root": None, "home": "root", "a": "b", "b": "home",
    }


def test_a_three_node_cycle_is_broken_once_and_everything_reaches_the_root() -> None:
    parents = {"root": None, "a": "b", "b": "c", "c": "a"}
    moves = [_move(1, "p1", "a", "root", "b"), _move(2, "p2", "b", "root", "c"), _move(3, "p3", "c", "root", "a")]
    assert _parents(break_cycles(_placed(parents), moves)) == {"root": None, "a": "b", "b": "c", "c": "root"}


def test_an_acyclic_placement_is_unchanged() -> None:
    parents = {"root": None, "a": "root", "b": "a"}
    assert _parents(break_cycles(_placed(parents), [_move(9, "p", "b", "root", "a")])) == parents


def test_a_cycle_without_move_records_falls_to_the_largest_node_id() -> None:
    parents = {"root": None, "a": "b", "b": "a"}
    assert _parents(break_cycles(_placed(parents), [])) == {"root": None, "a": "b", "b": "root"}
