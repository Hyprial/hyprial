# Decision record (file header; no inline comments by repository rule):
# - Contract: RoutineFacade.list_executed_by(*, agent: str) -> dict shaped
#   like list(): {"routines": [item, ...]}, each item identical to the
#   matching routine.list item and in the same relative order.
# - The executing agent is the existing projection rule
#   ``actor or produces or owner`` (the "actor" key routine.list already
#   publishes). Precedence matters: a borrowed-actor routine registered by
#   the agent belongs to its actor, not to its owner.
# - A routine that names the agent only in escalation addresses
#   (on_task_timeout.escalate_to, policy route escalate_to) is not returned.
#   policy route ``target`` only accepts "self", so it cannot name an agent.
# - Comparison goes through the facade's injected binding_key when present
#   (the same key RoutineRegistry compares bindings by), verbatim otherwise.
"""Agent teleport slice 1: list the routines an agent executes."""

from __future__ import annotations

from pathlib import Path

from .support import (
    ALPHA,
    ALPHA_ALIAS,
    BETA,
    OWNER,
    make_service,
    produced_source_yaml,
    scheduled_yaml,
)


def _names(result: dict[str, object]) -> list[str]:
    routines = result["routines"]
    assert isinstance(routines, list)
    return [item["name"] for item in routines]


def test_lists_actor_produces_and_owner_executed_routines(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    try:
        service.add(yaml_text=scheduled_yaml("borrowed", actor=ALPHA), owner=OWNER)
        service.add(
            yaml_text=produced_source_yaml("produced", produces=ALPHA), owner=OWNER
        )
        service.add(yaml_text=scheduled_yaml("external"), owner=ALPHA)
        service.add(yaml_text=scheduled_yaml("other", actor=BETA), owner=OWNER)

        result = service.list_executed_by(agent=ALPHA)

        assert sorted(_names(result)) == ["borrowed", "external", "produced"]
    finally:
        service.close()


def test_items_match_routine_list_items_in_list_order(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    try:
        service.add(yaml_text=scheduled_yaml("one", actor=ALPHA), owner=OWNER)
        service.add(yaml_text=scheduled_yaml("two", actor=BETA), owner=OWNER)
        service.add(yaml_text=scheduled_yaml("three", actor=ALPHA), owner=OWNER)

        listed = [
            item for item in service.list()["routines"] if item["actor"] == ALPHA
        ]

        assert service.list_executed_by(agent=ALPHA) == {"routines": listed}
    finally:
        service.close()


def test_escalation_only_references_are_not_returned(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    try:
        service.add(
            yaml_text=scheduled_yaml("escalates", actor=BETA, escalate_to=ALPHA),
            owner=OWNER,
        )
        service.add(
            yaml_text=produced_source_yaml("routes", produces=BETA, escalate_to=ALPHA),
            owner=OWNER,
        )

        assert service.list_executed_by(agent=ALPHA) == {"routines": []}
        assert sorted(_names(service.list_executed_by(agent=BETA))) == [
            "escalates",
            "routes",
        ]
    finally:
        service.close()


def test_owner_does_not_claim_a_routine_with_a_borrowed_actor(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    try:
        service.add(yaml_text=scheduled_yaml("borrowed", actor=BETA), owner=ALPHA)
        service.add(
            yaml_text=produced_source_yaml("produced", produces=BETA), owner=ALPHA
        )

        assert service.list_executed_by(agent=ALPHA) == {"routines": []}
    finally:
        service.close()


def test_unknown_agent_lists_nothing(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    try:
        service.add(yaml_text=scheduled_yaml("duty", actor=ALPHA), owner=OWNER)
        assert service.list_executed_by(agent="agent:o:n:nobody") == {"routines": []}
    finally:
        service.close()


def test_paused_routines_are_still_listed(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    try:
        service.add(
            yaml_text=scheduled_yaml("duty", actor=ALPHA), owner=OWNER, enabled=False
        )
        assert _names(service.list_executed_by(agent=ALPHA)) == ["duty"]
    finally:
        service.close()


def test_comparison_uses_the_injected_binding_key(tmp_path: Path) -> None:
    def binding_key(value: str) -> str:
        return ALPHA if value == ALPHA_ALIAS else value

    service = make_service(tmp_path, binding_key=binding_key)
    try:
        service.add(yaml_text=scheduled_yaml("aliased", actor=ALPHA_ALIAS), owner=OWNER)
        service.add(yaml_text=scheduled_yaml("canonical", actor=ALPHA), owner=OWNER)

        assert sorted(_names(service.list_executed_by(agent=ALPHA))) == [
            "aliased",
            "canonical",
        ]
        assert sorted(_names(service.list_executed_by(agent=ALPHA_ALIAS))) == [
            "aliased",
            "canonical",
        ]
    finally:
        service.close()


def test_without_binding_key_comparison_is_verbatim(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    try:
        service.add(yaml_text=scheduled_yaml("aliased", actor=ALPHA_ALIAS), owner=OWNER)
        assert service.list_executed_by(agent=ALPHA) == {"routines": []}
    finally:
        service.close()
