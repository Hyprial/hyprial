"""Structure queries: next hops and user push targets."""

from __future__ import annotations

import pytest

from hyprial.daemon.impl.cells.structure import (
    HopEnd,
    HopToFailOpen,
    HopToMember,
    PushTarget,
    next_hop,
    parse_structure,
    user_push_targets,
)

from tests.daemon.cells.structure.structure_support import design_cell, structure_cell


def test_next_hop_follows_the_matching_connection() -> None:
    structure = parse_structure(design_cell())
    assert next_hop(structure, "router", "code") == HopToMember("coder")
    assert next_hop(structure, "router", "quick") == HopToMember("worker")


def test_next_hop_without_a_match_goes_to_fail_open() -> None:
    structure = parse_structure(design_cell())
    assert next_hop(structure, "router", "unknown-label") == HopToFailOpen("coder")
    assert next_hop(structure, "worker", "anything") == HopToFailOpen("coder")


def test_next_hop_without_a_match_or_fail_open_ends() -> None:
    structure = parse_structure(
        structure_cell(
            (("a", "harness"),),
            {"cell.connections": [{"from": "a", "on": "x", "to": "a"}]},
        )
    )
    assert next_hop(structure, "a", "y") == HopEnd()


def test_next_hop_on_a_self_loop_returns_the_member_itself() -> None:
    structure = parse_structure(
        structure_cell(
            (("a", "harness"),),
            {"cell.connections": [{"from": "a", "on": "again", "to": "a"}]},
        )
    )
    assert next_hop(structure, "a", "again") == HopToMember("a")


def test_two_connections_forming_a_cycle_hop_by_hop() -> None:
    structure = parse_structure(
        structure_cell(
            (("a", "harness"), ("b", "harness")),
            {
                "cell.connections": [
                    {"from": "a", "on": "hand-off", "to": "b"},
                    {"from": "b", "on": "return", "to": "a"},
                ]
            },
        )
    )
    assert structure.problems == ()
    assert next_hop(structure, "a", "hand-off") == HopToMember("b")
    assert next_hop(structure, "b", "return") == HopToMember("a")


def test_next_hop_rejects_a_non_string_label() -> None:
    structure = parse_structure(design_cell())
    with pytest.raises(TypeError, match="label"):
        next_hop(structure, "router", 5)
    with pytest.raises(TypeError, match="label"):
        next_hop(structure, "router", None)
    with pytest.raises(TypeError, match="label"):
        next_hop(structure, "router", "")


def test_next_hop_matches_any_label_of_a_list() -> None:
    structure = parse_structure(
        structure_cell(
            (("a", "harness"), ("b", "harness")),
            {
                "cell.connections": [
                    {"from": "a", "on": ["code", "review"], "to": "b"}
                ]
            },
        )
    )
    assert next_hop(structure, "a", "code") == HopToMember("b")
    assert next_hop(structure, "a", "review") == HopToMember("b")
    assert next_hop(structure, "b", "code") == HopEnd()


def test_next_hop_after_a_duplicate_takes_the_first_connection() -> None:
    structure = parse_structure(
        structure_cell(
            (("a", "harness"), ("b", "harness"), ("c", "harness")),
            {
                "cell.connections": [
                    {"from": "a", "on": "x", "to": "b"},
                    {"from": "a", "on": "x", "to": "c"},
                ]
            },
        )
    )
    assert [problem.code for problem in structure.problems] == [
        "connection-duplicate"
    ]
    assert next_hop(structure, "a", "x") == HopToMember("b")


def test_next_hop_from_an_unknown_member_just_never_matches() -> None:
    structure = parse_structure(design_cell())
    assert next_hop(structure, "ghost", "code") == HopToFailOpen("coder")


def test_push_targets_follow_the_members_own_user_connections() -> None:
    structure = parse_structure(design_cell())
    assert user_push_targets(structure, "coder") == (
        PushTarget("lark", "project-chat"),
    )
    assert user_push_targets(structure, "router") == ()


def test_push_targets_default_to_every_adapter_when_no_connection_uses_user() -> None:
    structure = parse_structure(
        structure_cell((("a", "harness"), ("lark", "adapter"), ("slack", "adapter")))
    )
    assert user_push_targets(structure, "a") == (
        PushTarget("lark"),
        PushTarget("slack"),
    )


def test_push_targets_stay_empty_when_another_member_uses_user() -> None:
    structure = parse_structure(
        structure_cell(
            (("a", "harness"), ("b", "harness"), ("lark", "adapter")),
            {
                "cell.connections": [
                    {"from": "b", "on": "user", "to": "lark", "route": "ops"}
                ]
            },
        )
    )
    assert user_push_targets(structure, "a") == ()
    assert user_push_targets(structure, "b") == (PushTarget("lark", "ops"),)


def test_the_default_and_the_empty_answer_are_distinguishable() -> None:
    silent = structure_cell(
        (("a", "harness"), ("b", "harness"), ("lark", "adapter")),
    )
    speaking = structure_cell(
        (("a", "harness"), ("b", "harness"), ("lark", "adapter")),
        {
            "cell.connections": [
                {"from": "b", "on": "user", "to": "lark", "route": "ops"}
            ]
        },
    )
    assert user_push_targets(parse_structure(silent), "a") == (PushTarget("lark"),)
    assert user_push_targets(parse_structure(speaking), "a") == ()


def test_user_push_targets_skip_hops_that_are_not_adapter_members() -> None:
    structure = parse_structure(
        structure_cell(
            (("a", "harness"), ("b", "harness"), ("lark", "adapter")),
            {
                "cell.connections": [
                    {"from": "a", "on": ["user", "note"], "to": "b"},
                    {"from": "a", "on": "user", "to": "lark"},
                ]
            },
        )
    )
    assert structure.problems == ()
    assert user_push_targets(structure, "a") == (PushTarget("lark"),)


def test_a_user_connection_to_a_harness_member_pushes_nowhere_by_default() -> None:
    structure = parse_structure(
        structure_cell(
            (("a", "harness"), ("b", "harness"), ("lark", "adapter")),
            {"cell.connections": [{"from": "a", "on": "user", "to": "b"}]},
        )
    )
    assert user_push_targets(structure, "a") == ()
    assert user_push_targets(structure, "b") == ()


def test_user_push_targets_keep_order_and_optional_routes() -> None:
    structure = parse_structure(
        structure_cell(
            (("a", "harness"), ("lark", "adapter"), ("slack", "adapter")),
            {
                "cell.connections": [
                    {"from": "a", "on": "user", "to": "lark", "route": "ops"},
                    {"from": "a", "on": "user", "to": "slack"},
                ]
            },
        )
    )
    assert structure.problems == ()
    assert user_push_targets(structure, "a") == (
        PushTarget("lark", "ops"),
        PushTarget("slack"),
    )


def test_user_push_targets_allow_several_routes_on_one_adapter() -> None:
    structure = parse_structure(
        structure_cell(
            (("a", "harness"), ("lark", "adapter")),
            {
                "cell.connections": [
                    {"from": "a", "on": "user", "to": "lark", "route": "ops"},
                    {"from": "a", "on": "user", "to": "lark", "route": "proj"},
                ]
            },
        )
    )
    assert structure.problems == ()
    assert user_push_targets(structure, "a") == (
        PushTarget("lark", "ops"),
        PushTarget("lark", "proj"),
    )


def test_a_repeated_user_push_row_is_a_duplicate_and_pushes_once() -> None:
    structure = parse_structure(
        structure_cell(
            (("a", "harness"), ("lark", "adapter")),
            {
                "cell.connections": [
                    {"from": "a", "on": "user", "to": "lark", "route": "ops"},
                    {"from": "a", "on": "user", "to": "lark", "route": "ops"},
                ]
            },
        )
    )
    assert [problem.code for problem in structure.problems] == [
        "connection-duplicate"
    ]
    assert user_push_targets(structure, "a") == (PushTarget("lark", "ops"),)


def test_next_hop_on_the_reserved_label_takes_the_first_connection() -> None:
    structure = parse_structure(
        structure_cell(
            (("a", "harness"), ("lark", "adapter"), ("slack", "adapter")),
            {
                "cell.connections": [
                    {"from": "a", "on": "user", "to": "lark", "route": "ops"},
                    {"from": "a", "on": "user", "to": "slack"},
                ]
            },
        )
    )
    assert next_hop(structure, "a", "user") == HopToMember("lark")


def test_cell_push_targets_take_the_user_connections_in_document_order() -> None:
    from hyprial.daemon.impl.cells.structure import cell_push_targets

    structure = parse_structure(structure_cell(
        (("im", "adapter"), ("ops", "adapter")),
        {"cell.connections": [
            {"from": "ops", "on": "user", "to": "ops", "route": "alerts"},
            {"from": "im", "on": "user", "to": "im"},
            {"from": "im", "on": "user", "to": "ops", "route": "dm"},
        ]},
    ))
    assert cell_push_targets(structure) == (
        PushTarget("ops", "alerts"), PushTarget("im"), PushTarget("ops", "dm"),
    )


def test_cell_push_targets_default_to_every_adapter_member_without_a_user_row() -> None:
    from hyprial.daemon.impl.cells.structure import cell_push_targets

    structure = parse_structure(structure_cell((("im", "adapter"), ("ops", "adapter"))))
    assert cell_push_targets(structure) == (PushTarget("im"), PushTarget("ops"))
    assert cell_push_targets(parse_structure(structure_cell())) == ()


def test_cell_push_targets_refuse_a_cell_with_a_harness_member() -> None:
    from hyprial.daemon.impl.cells.structure import cell_push_targets

    structure = parse_structure(structure_cell((("a", "harness"), ("im", "adapter"))))
    with pytest.raises(TypeError):
        cell_push_targets(structure)
