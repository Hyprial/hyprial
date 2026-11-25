"""``conversations.get`` and ``conversations.members.add``/``.remove`` (slice GC2).

Only the owner manages members; a member agent may read the group and leave
it; the owner cannot leave; anyone else is not a member.  Replays are
answered from the document, so a call made twice with one key changes it
once.
"""

from __future__ import annotations

from hyprial.kernel import mint_group_id
from tests.daemon.conversations.group_requests.group_request_support import (
    OWNER,
    group_daemon,
    member_ids,
)
from tests.support.golden.app import golden_daemon as golden_daemon

NOT_FOUND = "CONVERSATION_NOT_FOUND"
NOT_MEMBER = "CONVERSATION_NOT_MEMBER"
NOT_AUTHORIZED = "CALLER_NOT_AUTHORIZED"
INVALID = "INVALID_ARGUMENT"
UNAVAILABLE = "DAEMON_UNAVAILABLE"


def _group(booted, *names: str) -> str:  # type: ignore[no-untyped-def]
    answer = booted.create(list(names), key="k-group")
    booted.advance()
    return str(answer["conversationId"])


def test_get_by_the_owner_and_by_a_member_agent(golden_daemon, monkeypatch) -> None:
    booted = group_daemon(golden_daemon, monkeypatch)
    group = _group(booted, "alpha")
    created = booted.create(["alpha"], key="k-group")

    mine = booted.call("conversations.get", conversationId=group)
    theirs = booted.call("conversations.get", "alpha", conversationId=group)

    assert {key: mine[key] for key in created} == created
    assert (mine["self"], mine["canManageMembers"]) == (OWNER, True)
    assert (theirs["self"], theirs["canManageMembers"]) == (booted.agent_id("alpha"), False)
    assert member_ids(theirs) == member_ids(mine)


def test_get_by_a_non_member_and_for_an_unknown_group(golden_daemon, monkeypatch) -> None:
    booted = group_daemon(golden_daemon, monkeypatch)
    group = _group(booted, "alpha")
    unknown = mint_group_id(b"never-created")

    stranger = booted.refused("conversations.get", "beta", conversationId=group)
    missing = booted.refused("conversations.get", conversationId=unknown)
    missing_for_agent = booted.refused("conversations.get", "beta", conversationId=unknown)

    assert (stranger.code, stranger.data) == (NOT_MEMBER, {"conversationId": group})
    assert (missing.code, missing.data) == (NOT_FOUND, {"conversationId": unknown})
    assert missing_for_agent.code == NOT_FOUND


def test_get_while_the_document_is_unreadable(golden_daemon, monkeypatch) -> None:
    booted = group_daemon(golden_daemon, monkeypatch)
    group = _group(booted, "alpha")
    booted.fs.down = True

    assert booted.refused("conversations.get", conversationId=group).code == UNAVAILABLE


def test_add_then_add_again_is_a_no_op(golden_daemon, monkeypatch) -> None:
    booted = group_daemon(golden_daemon, monkeypatch)
    group = _group(booted, "alpha")
    beta = booted.agent_id("beta")

    added = booted.call("conversations.members.add", conversationId=group, member="beta",
                        idempotencyKey="add-beta")
    again = booted.call("conversations.members.add", conversationId=group, member=beta,
                        idempotencyKey="add-beta-again")

    assert member_ids(added) == [OWNER, booted.agent_id("alpha"), beta]
    assert isinstance(added["entryId"], str) and added["entryId"]
    assert again == {**added, "entryId": None}
    assert added["members"][-1]["addedBy"] == OWNER


def test_adding_the_user_agent_is_the_owner_and_changes_nothing(golden_daemon, monkeypatch) -> None:
    booted = group_daemon(golden_daemon, monkeypatch)
    group = _group(booted, "alpha")

    for index, reference in enumerate(("hyprial-user", booted.user_agent().uri, OWNER)):
        answer = booted.call("conversations.members.add", conversationId=group,
                             member=reference, idempotencyKey=f"add-user-{index}")
        assert answer["entryId"] is None
        assert member_ids(answer) == [OWNER, booted.agent_id("alpha")]


def test_a_replayed_add_after_a_remove_does_not_add_again(golden_daemon, monkeypatch) -> None:
    booted = group_daemon(golden_daemon, monkeypatch)
    group = _group(booted, "alpha")
    beta = booted.agent_id("beta")
    first = booted.call("conversations.members.add", conversationId=group, member="beta",
                        idempotencyKey="add-1")
    booted.advance()
    removed = booted.call("conversations.members.remove", conversationId=group, memberId=beta,
                          idempotencyKey="remove-1")
    booted.advance()

    replayed = booted.call("conversations.members.add", conversationId=group, member="beta",
                           idempotencyKey="add-1")
    replayed_remove = booted.call("conversations.members.remove", conversationId=group,
                                  memberId=beta, idempotencyKey="remove-1")

    assert beta not in member_ids(removed)
    assert replayed["entryId"] == first["entryId"]
    assert member_ids(replayed) == member_ids(removed)
    assert replayed_remove == removed


def test_remove_a_non_member_is_a_no_op(golden_daemon, monkeypatch) -> None:
    booted = group_daemon(golden_daemon, monkeypatch)
    group = _group(booted, "alpha")

    answer = booted.call("conversations.members.remove", conversationId=group,
                         memberId=booted.agent_id("beta"), idempotencyKey="remove-beta")

    assert answer["entryId"] is None
    assert member_ids(answer) == [OWNER, booted.agent_id("alpha")]


def test_an_agent_leaves_and_can_no_longer_read(golden_daemon, monkeypatch) -> None:
    booted = group_daemon(golden_daemon, monkeypatch)
    group = _group(booted, "alpha", "beta")
    alpha = booted.agent_id("alpha")

    left = booted.call("conversations.members.remove", "alpha", conversationId=group,
                       memberId=alpha, idempotencyKey="leave")

    assert member_ids(left) == [OWNER, booted.agent_id("beta")]
    assert left["entryId"]
    assert booted.refused("conversations.get", "alpha", conversationId=group).code == NOT_MEMBER
    replay = booted.call("conversations.members.remove", "alpha", conversationId=group,
                         memberId=alpha, idempotencyKey="leave")
    assert replay == left


def test_the_owner_cannot_leave(golden_daemon, monkeypatch) -> None:
    booted = group_daemon(golden_daemon, monkeypatch)
    group = _group(booted, "alpha")

    error = booted.refused("conversations.members.remove", conversationId=group,
                           memberId=OWNER, idempotencyKey="leave")

    assert error.code == INVALID
    assert OWNER in member_ids(booted.call("conversations.get", conversationId=group))


def test_a_member_agent_cannot_manage_members(golden_daemon, monkeypatch) -> None:
    booted = group_daemon(golden_daemon, monkeypatch)
    group = _group(booted, "alpha", "beta")

    add = booted.refused("conversations.members.add", "alpha", conversationId=group,
                         member="gamma", idempotencyKey="k")
    remove_other = booted.refused("conversations.members.remove", "alpha", conversationId=group,
                                  memberId=booted.agent_id("beta"), idempotencyKey="k")
    remove_manager = booted.refused("conversations.members.remove", "alpha",
                                    conversationId=group, memberId=OWNER, idempotencyKey="k2")

    assert (add.code, remove_other.code, remove_manager.code) == (NOT_AUTHORIZED,) * 3
    assert member_ids(booted.call("conversations.get", conversationId=group)) == [
        OWNER, booted.agent_id("alpha"), booted.agent_id("beta"),
    ]


def test_a_non_member_is_not_a_member_and_an_unknown_group_is_not_found(
    golden_daemon, monkeypatch
) -> None:
    booted = group_daemon(golden_daemon, monkeypatch)
    group = _group(booted, "alpha")
    unknown = mint_group_id(b"never-created")
    gamma = booted.agent_id("gamma")

    codes = [
        booted.refused("conversations.members.add", "gamma", conversationId=group,
                       member="beta", idempotencyKey="k").code,
        booted.refused("conversations.members.remove", "gamma", conversationId=group,
                       memberId=gamma, idempotencyKey="k").code,
        booted.refused("conversations.members.add", conversationId=unknown, member="beta",
                       idempotencyKey="k").code,
        booted.refused("conversations.members.remove", conversationId=unknown,
                       memberId=gamma, idempotencyKey="k").code,
    ]

    assert codes == [NOT_MEMBER, NOT_MEMBER, NOT_FOUND, NOT_FOUND]


def test_add_refuses_what_create_refuses(golden_daemon, monkeypatch) -> None:
    booted = group_daemon(golden_daemon, monkeypatch)
    group = _group(booted, "alpha")

    unknown = booted.refused("conversations.members.add", conversationId=group, member="ghost",
                             idempotencyKey="k1")
    foreign = booted.refused("conversations.members.add", conversationId=group,
                             member="agent:bob:node-1:a.bobagentaaaaaaaaaaaaaaaaaa",
                             idempotencyKey="k2")
    booted.fs.down = True
    down = booted.refused("conversations.members.add", conversationId=group, member="beta",
                          idempotencyKey="k3")

    assert (unknown.code, foreign.code, down.code) == (
        "AGENT_NOT_FOUND", "UNSUPPORTED_TARGET", UNAVAILABLE,
    )
