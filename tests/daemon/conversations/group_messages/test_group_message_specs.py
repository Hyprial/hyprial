"""Declarations for group sends and group history (slice GC3).

``message.send`` declares the two group refusals and takes ``mentions`` over
IPC only: its HTTP profile is unchanged (one agent in ``to``, no
``replyTo``), because the HTTP constraint projection cannot express "``to``
or a group id" (``project_http_params_schema`` takes ``type``,
``properties`` and ``required`` only).  ``conversations.history`` takes a
``c.`` or a ``g.`` id and declares the group refusals.
"""

from __future__ import annotations

from jsonschema import Draft202012Validator

from hyprial.daemon.impl.request_catalog.conversations import (
    CONVERSATIONS_HISTORY_SPEC,
    CONVERSATIONS_LIST_SPEC,
)
from hyprial.daemon.impl.request_catalog.conversations.examples import CONVERSATION_EXAMPLES
from hyprial.daemon.impl.request_catalog.conversations.group_examples import GROUP_ID
from hyprial.daemon.impl.request_catalog.message.send.specs import MESSAGE_SEND_SPEC
from hyprial.kernel import project_http_params_schema, validate_method_example_set

GROUP = "g.groupexampleaaaaaaaaaaaaaa"
CELL = "c.aaaaaaaaaaaaaaaaaaaaaaaaaa"
GROUP_CODES = {"CONVERSATION_NOT_FOUND", "CONVERSATION_NOT_MEMBER"}


def _codes(spec) -> set[str]:  # type: ignore[no-untyped-def]
    return {error.code for error in spec.errors}


def test_send_declares_the_group_refusals() -> None:
    assert GROUP_CODES <= _codes(MESSAGE_SEND_SPEC)
    assert "DAEMON_UNAVAILABLE" in _codes(MESSAGE_SEND_SPEC)


def test_history_declares_the_group_refusals_and_list_does_not() -> None:
    assert GROUP_CODES | {"DAEMON_UNAVAILABLE"} <= _codes(CONVERSATIONS_HISTORY_SPEC)
    assert not (GROUP_CODES | {"DAEMON_UNAVAILABLE"}) & _codes(CONVERSATIONS_LIST_SPEC)


def test_send_takes_mentions_over_ipc_and_keeps_its_http_profile() -> None:
    properties = MESSAGE_SEND_SPEC.params_schema["properties"]
    http = project_http_params_schema(
        MESSAGE_SEND_SPEC.params_schema, MESSAGE_SEND_SPEC.http_binding.params_constraints
    )

    assert "mentions" in properties
    assert properties["mentions"]["x-hyprial-http-forbidden"] is True
    assert "mentions" not in http["properties"] and "replyTo" not in http["properties"]
    assert "to" in http["required"]


def test_history_takes_a_cell_or_a_group_id() -> None:
    validator = Draft202012Validator(CONVERSATIONS_HISTORY_SPEC.params_schema)

    assert validator.is_valid({"conversationId": CELL})
    assert validator.is_valid({"conversationId": GROUP})
    assert not validator.is_valid({"conversationId": "g.short"})
    assert not validator.is_valid({"conversationId": "dc.aaaaaaaaaaaaaaaaaaaaaaaaaa"})


def test_the_history_examples_show_a_group_page_and_an_unknown_group() -> None:
    [history] = [item for item in CONVERSATION_EXAMPLES if item.method == "conversations.history"]

    validate_method_example_set(CONVERSATIONS_HISTORY_SPEC, history)
    group_page = next(case for case in history.cases if case.name == "group-page")
    unknown = next(case for case in history.cases if case.name == "unknown-group")
    assert group_page.params["conversationId"] == GROUP_ID
    assert [entry["messageClass"] for entry in group_page.result["entries"]][-2:] == [
        "interaction", "interaction",
    ]
    assert group_page.result["entries"][-1]["replyTo"]["from"] == "user:alice"
    assert unknown.error.code == "CONVERSATION_NOT_FOUND"
