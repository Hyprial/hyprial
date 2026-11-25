"""Group entry line format, member ids and the membership fold (slice GC1)."""

from __future__ import annotations

import base64
import itertools
import json

import pytest

from hyprial.daemon.impl.conversations.groups import (
    GROUP_RECORD_FORMAT,
    GroupEntry,
    ReplyTo,
    fold_group,
    member_id_for,
)
from hyprial.daemon.impl.user_agent.naming import USER_AGENT_NAME
from hyprial.kernel import is_group_id_segment
from tests.daemon.conversations.groups.group_support import (
    GROUP,
    OWNER,
    REVIEWER,
    WORKER,
    added,
    created,
    left,
    message,
    removed,
)


def _line(entry: GroupEntry) -> dict:
    return json.loads(entry.to_line())


def _parse(document: dict) -> GroupEntry | None:
    return GroupEntry.from_line(json.dumps(document))


def test_the_example_ids_have_the_right_shapes() -> None:
    assert is_group_id_segment(GROUP)
    assert member_id_for(WORKER) == (WORKER, "agent")


def test_a_message_line_round_trips_with_every_optional_field() -> None:
    entry = message(
        "m1", WORKER, at=5, mentions=(REVIEWER,),
        reply_to=ReplyTo(message_id="m0", sender=OWNER, excerpt="please cut"),
    )
    document = _line(entry)
    assert document["format"] == GROUP_RECORD_FORMAT == "hyprial-group-record/v1"
    assert document["entryId"] == "m1"
    assert document["from"] == WORKER
    assert document["kind"] == "message"
    assert document["mentions"] == [REVIEWER]
    assert document["replyTo"] == {"messageId": "m0", "from": OWNER, "excerpt": "please cut"}
    assert base64.b64decode(document["body"]) == b"hello"
    assert "hops" not in document
    assert GroupEntry.from_line(entry.to_line()) == entry
    assert entry.key == "m1"


def test_a_system_line_round_trips() -> None:
    for entry in (created(), added(WORKER, at=2), removed(WORKER, at=3), left(WORKER, at=4)):
        assert GroupEntry.from_line(entry.to_line()) == entry
    document = _line(added(WORKER, at=2))
    assert document["messageClass"] == "system"
    assert document["system"]["event"] == "member-added"
    assert document["system"]["memberId"] == WORKER
    assert document["system"]["member"]["memberId"] == WORKER
    assert document["system"]["member"]["kind"] == "agent"
    assert set(document["system"]["member"]) == {
        "memberId", "kind", "displayName", "manager", "joinedAtMs", "addedBy", "address",
    }


@pytest.mark.parametrize(
    "field",
    ["format", "entryId", "conversationId", "kind", "messageClass", "from",
     "createdAtMs", "recordedAtMs", "writer", "intent", "summary", "payloadSha256", "body"],
)
def test_a_message_line_missing_a_required_field_is_rejected(field: str) -> None:
    document = _line(message("m1", OWNER, at=1))
    del document[field]
    assert _parse(document) is None


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("format", "hyprial-group-record/v2"),
        ("format", "hyprial-conversation-record/v1"),
        ("conversationId", "c.aaaaaaaaaaaaaaaaaaaaaaaaaa"),
        ("kind", "event"),
        ("messageClass", "noise"),
        ("intent", "notify"),
        ("createdAtMs", "1"),
        ("createdAtMs", True),
        ("payloadSha256", "nothex"),
        ("body", "!!not base64!!"),
        ("mentions", "a.x"),
        ("mentions", [1]),
        ("replyTo", {"messageId": "m0"}),
        ("fromAddress", 5),
        ("system", {"event": "created"}),
    ],
)
def test_a_message_line_with_a_bad_value_is_rejected(field: str, value: object) -> None:
    document = _line(message("m1", OWNER, at=1))
    document[field] = value
    assert _parse(document) is None


@pytest.mark.parametrize(
    "mutate",
    [
        lambda system: system.update(event="renamed"),
        lambda system: system.pop("event"),
        lambda system: system.pop("memberId"),
        lambda system: system.pop("member"),
        lambda system: system["member"].pop("kind"),
        lambda system: system["member"].update(kind="robot"),
        lambda system: system["member"].update(memberId=REVIEWER),
        lambda system: system["member"].update(manager="yes"),
    ],
)
def test_a_system_line_with_a_bad_event_is_rejected(mutate) -> None:
    document = _line(added(WORKER, at=2))
    mutate(document["system"])
    assert _parse(document) is None


def test_a_created_line_needs_a_title_and_no_body() -> None:
    document = _line(created())
    del document["system"]["title"]
    assert _parse(document) is None
    document = _line(created())
    document["body"] = ""
    assert _parse(document) is None


def test_a_body_and_body_omitted_are_exclusive() -> None:
    document = _line(message("m1", OWNER, at=1))
    document["bodyOmitted"] = "too-large"
    assert _parse(document) is None
    del document["body"]
    omitted = _parse(document)
    assert omitted is not None and omitted.body_omitted == "too-large" and omitted.body == b""


def test_unknown_optional_fields_are_tolerated() -> None:
    entry = message("m1", OWNER, at=1)
    document = _line(entry)
    document["futureField"] = {"x": 1}
    document["replyTo"] = None
    document.pop("replyTo")
    assert _parse(document) == entry
    system = _line(added(WORKER, at=2))
    system["system"]["future"] = 1
    system["system"]["member"]["future"] = 2
    assert _parse(system) == added(WORKER, at=2)


@pytest.mark.parametrize("line", ["", "not json", "[]", "null", '{"format": 1}'])
def test_garbage_lines_are_rejected(line: str) -> None:
    assert GroupEntry.from_line(line) is None


def test_member_ids() -> None:
    assert member_id_for(OWNER) == (OWNER, "user")
    assert member_id_for(f"agent:alice:host-1:{WORKER}") == (WORKER, "agent")
    assert member_id_for(f"agent:alice:host-2:{WORKER}") == (WORKER, "agent")
    assert member_id_for(WORKER) == (WORKER, "agent")


@pytest.mark.parametrize("host", ["host-1", "host-2"])
def test_any_machines_user_agent_address_is_the_owner(host: str) -> None:
    assert member_id_for(f"agent:alice:{host}:{USER_AGENT_NAME}") == (OWNER, "user")


def test_pending_merge_a_user_agent_id_form_address_is_an_agent_member() -> None:
    """Pinned for the later merge step: the pure fold recognises the name form only.

    Each machine's user agent has its own registry-minted id, so its id form is
    an ordinary agent id here; the member resolver reaches this machine's own
    instance through its registry marker before this function is asked.
    """

    ident = "a.vataro664zkhlfnya2ubzymqxq"
    assert member_id_for(f"agent:alice:host-2:{ident}") == (ident, "agent")


@pytest.mark.parametrize(
    "address",
    [
        "agent:alice:host-1:worker",
        "worker",
        "",
        "user:",
        "user:a:b",
        "adapter:lark:alice:bot",
        "route:lark:main",
        GROUP,
        "a.short",
    ],
)
def test_a_name_form_agent_or_other_address_is_refused(address: str) -> None:
    with pytest.raises(ValueError):
        member_id_for(address)


def test_fold_create_add_remove_leave() -> None:
    state = fold_group([
        created(1),
        added(WORKER, at=2),
        added(REVIEWER, at=3),
        removed(REVIEWER, at=4),
        left(WORKER, at=5),
        added(REVIEWER, at=6),
    ])
    assert state.title == "release"
    assert state.created_by == OWNER
    assert state.created_at_ms == 1
    assert [m.member_id for m in state.members] == [OWNER, REVIEWER]
    assert state.members[0].manager is True and state.members[0].kind == "user"
    assert state.members[1].joined_at_ms == 6
    assert state.member(REVIEWER) is not None and state.member(WORKER) is None


def test_fold_of_nothing_is_an_unknown_group() -> None:
    state = fold_group([])
    assert state.title is None and state.created_by is None and state.members == ()


def test_a_duplicate_add_keeps_the_first_record() -> None:
    state = fold_group([created(1), added(WORKER, at=2), added(WORKER, at=3)])
    assert [(m.member_id, m.joined_at_ms) for m in state.members] == [(OWNER, 1), (WORKER, 2)]


def test_removing_a_non_member_or_the_manager_changes_nothing() -> None:
    base = [created(1), added(WORKER, at=2)]
    expected = fold_group(base)
    assert fold_group([*base, removed(REVIEWER, at=3)]) == expected
    assert fold_group([*base, removed(OWNER, at=3)]) == expected
    assert fold_group([*base, left(OWNER, at=3)]) == expected


def test_a_second_created_and_entries_before_created_change_nothing() -> None:
    expected = fold_group([created(1), added(WORKER, at=2)])
    state = fold_group([
        added(REVIEWER, at=0),
        created(1),
        added(WORKER, at=2),
        created(3, title="other", entry_id="e-created-2"),
    ])
    assert state == expected


def test_the_same_entry_id_twice_counts_once() -> None:
    state = fold_group([created(1), added(WORKER, at=2), removed(WORKER, at=3),
                        added(WORKER, at=2)])
    assert [m.member_id for m in state.members] == [OWNER]


def test_out_of_order_entries_from_two_writers_converge() -> None:
    entries = [
        created(1),
        added(WORKER, at=2, writer="node-a"),
        added(REVIEWER, at=3, writer="node-b"),
        message("m1", OWNER, at=4, writer="node-b"),
        removed(WORKER, at=5),
        added(WORKER, at=6, writer="node-b", entry_id="e-readd"),
        left(REVIEWER, at=7),
    ]
    states = {fold_group(order) for order in itertools.permutations(entries)}
    assert len(states) == 1
    (state,) = states
    assert [m.member_id for m in state.members] == [OWNER, WORKER]
    assert state.member(WORKER).joined_at_ms == 6


def test_entries_from_a_removed_member_change_nothing() -> None:
    base = [created(1), added(WORKER, at=2), added(REVIEWER, at=3), removed(WORKER, at=4)]
    expected = fold_group(base)
    later = [
        message("m-late", WORKER, at=5),
        left(WORKER, at=6),
        added(WORKER, at=7, author=WORKER),
        removed(REVIEWER, at=8, author=WORKER),
    ]
    assert fold_group([*base, *later]) == expected


def test_an_agent_member_cannot_manage_members() -> None:
    base = [created(1), added(WORKER, at=2)]
    expected = fold_group(base)
    assert fold_group([*base, added(REVIEWER, at=3, author=WORKER)]) == expected
    assert fold_group([*base, removed(WORKER, at=3, author=REVIEWER)]) == expected
