"""``hyprial message group members add|remove`` over the group member methods.

``add`` passes the agent as typed (name, ``a.`` id or address) to
``conversations.members.add``.  ``remove`` takes a member id or a member's
name: it reads the group once (``conversations.get``) and sends the member
id whose id or name matches; a value that matches no member is sent as
typed, and the daemon decides.  Both send a fresh idempotency key and say
when the daemon reports that nothing changed (``entryId`` null).
"""

from __future__ import annotations

import json

import pytest

from tests.shell.cli.groups.support import (
    GROUP_ID,
    OPERATOR,
    REVIEWER_ID,
    assert_declared_params,
    example,
    invoke,
    record,
)

ADDED = example("conversations.members.add", "add-reviewer")
ALREADY = example("conversations.members.add", "already-a-member")
REMOVED = example("conversations.members.remove", "remove-reviewer")
NOT_A_MEMBER = example("conversations.members.remove", "not-a-member")
GROUP = example("conversations.get", "release-group")


def _without_key(params: dict) -> dict:
    key = params.pop("idempotencyKey")
    assert isinstance(key, str) and key
    return params


def test_add_sends_the_agent_as_typed(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = record(monkeypatch, {"conversations.members.add": ADDED})

    result = invoke("members", "add", GROUP_ID, "reviewer")

    assert result.exit_code == 0, result.output
    assert_declared_params(calls)
    [(method, params)] = calls
    assert method == "conversations.members.add"
    assert _without_key(params) == {
        "from": OPERATOR,
        "conversationId": GROUP_ID,
        "member": "reviewer",
    }
    assert result.stdout == (
        f"added reviewer to {GROUP_ID}: 3 members (alice, worker, reviewer)\n"
    )


def test_adding_a_member_again_says_nothing_changed(monkeypatch: pytest.MonkeyPatch) -> None:
    record(monkeypatch, {"conversations.members.add": ALREADY})

    result = invoke("members", "add", GROUP_ID, "worker")

    assert result.exit_code == 0, result.output
    assert result.stdout == (
        f"worker is already a member of {GROUP_ID}: 3 members (alice, worker, reviewer)\n"
    )


def test_each_add_mints_its_own_key(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = record(monkeypatch, {"conversations.members.add": ADDED})

    for _ in range(2):
        assert invoke("members", "add", GROUP_ID, "reviewer").exit_code == 0

    first, second = (params["idempotencyKey"] for _method, params in calls)
    assert first != second


def test_add_json_is_the_daemon_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    record(monkeypatch, {"conversations.members.add": ADDED})

    result = invoke("members", "add", GROUP_ID, "reviewer", "--json")

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == ADDED


@pytest.mark.parametrize("named", ["reviewer", REVIEWER_ID])
def test_remove_by_name_or_id_sends_the_member_id(
    monkeypatch: pytest.MonkeyPatch, named: str
) -> None:
    calls = record(
        monkeypatch,
        {"conversations.get": GROUP, "conversations.members.remove": REMOVED},
    )

    result = invoke("members", "remove", GROUP_ID, named)

    assert result.exit_code == 0, result.output
    assert_declared_params(calls)
    assert calls[0] == ("conversations.get", {"from": OPERATOR, "conversationId": GROUP_ID})
    method, params = calls[1]
    assert method == "conversations.members.remove"
    assert _without_key(params) == {
        "from": OPERATOR,
        "conversationId": GROUP_ID,
        "memberId": REVIEWER_ID,
    }
    assert result.stdout == f"removed {named} from {GROUP_ID}: 2 members (alice, worker)\n"


def test_remove_of_an_unknown_value_is_sent_as_typed(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = record(
        monkeypatch,
        {"conversations.get": GROUP, "conversations.members.remove": NOT_A_MEMBER},
    )

    result = invoke("members", "remove", GROUP_ID, "ghost")

    assert result.exit_code == 0, result.output
    assert calls[1][1]["memberId"] == "ghost"
    assert result.stdout == (
        f"ghost is not a member of {GROUP_ID}; nothing changed: 2 members (alice, worker)\n"
    )


def test_remove_json_is_the_daemon_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    record(monkeypatch, {"conversations.get": GROUP, "conversations.members.remove": REMOVED})

    result = invoke("members", "remove", GROUP_ID, "reviewer", "--json")

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == REMOVED
