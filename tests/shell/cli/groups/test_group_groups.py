"""``hyprial message group create|show|list`` over the group conversation methods.

``create`` sends the title, the members as typed and a fresh idempotency key
to ``conversations.create``; ``show`` reads ``conversations.get``; ``list``
reads ``conversations.list`` with ``kinds=["group"]`` and pages it with
``--limit``/``--cursor``.  ``--json`` emits the daemon answer unchanged.
"""

from __future__ import annotations

import json

import pytest

from tests.shell.cli.groups.support import (
    GROUP_HEAD,
    GROUP_ID,
    OPERATOR,
    REVIEWER_ID,
    WORKER_ID,
    assert_declared_params,
    example,
    invoke,
    record,
)

SUMMARY = f"{GROUP_ID}  release  3 members (alice, worker, reviewer)"


def test_create_sends_title_members_and_a_key(monkeypatch: pytest.MonkeyPatch) -> None:
    answer = example("conversations.create", "release-group")
    calls = record(monkeypatch, {"conversations.create": answer})

    result = invoke("create", "--title", "release", "--member", "worker", "--member", "reviewer")

    assert result.exit_code == 0, result.output
    assert result.stdout == SUMMARY + "\n"
    assert_declared_params(calls)
    [(method, params)] = calls
    assert method == "conversations.create"
    key = params.pop("idempotencyKey")
    assert isinstance(key, str) and key
    assert params == {"from": OPERATOR, "title": "release", "members": ["worker", "reviewer"]}


def test_each_create_mints_its_own_key(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = record(
        monkeypatch, {"conversations.create": example("conversations.create", "release-group")}
    )

    for _ in range(2):
        assert invoke("create", "--title", "release", "--member", "worker").exit_code == 0

    first, second = (params["idempotencyKey"] for _method, params in calls)
    assert first != second


def test_create_without_members_creates_a_group_of_the_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = record(
        monkeypatch, {"conversations.create": example("conversations.create", "release-group")}
    )

    assert invoke("create", "--title", "release").exit_code == 0

    assert calls[0][1]["members"] == []
    assert_declared_params(calls)


def test_create_json_is_the_daemon_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    answer = example("conversations.create", "release-group")
    record(monkeypatch, {"conversations.create": answer})

    result = invoke("create", "--title", "release", "--member", "worker", "--json")

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == answer


def test_show_reads_one_group(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = record(
        monkeypatch, {"conversations.get": example("conversations.get", "release-group")}
    )

    result = invoke("show", GROUP_ID)

    assert result.exit_code == 0, result.output
    assert calls == [("conversations.get", {"from": OPERATOR, "conversationId": GROUP_ID})]
    assert_declared_params(calls)
    lines = result.stdout.splitlines()
    assert lines[0] == SUMMARY
    assert lines[1] == "you are user:alice and can manage members"
    assert lines[2].split() == ["NAME", "MEMBER", "MANAGES", "JOINED"]
    assert lines[3].split() == ["alice", "user:alice", "yes", "2026-09-21T14:15:20Z"]
    assert lines[4].split() == ["worker", WORKER_ID, "no", "2026-09-21T14:15:20Z"]
    assert lines[5].split() == ["reviewer", REVIEWER_ID, "no", "2026-09-21T14:15:20Z"]
    assert len(lines) == 6


def test_show_tells_a_member_agent_it_cannot_manage(monkeypatch: pytest.MonkeyPatch) -> None:
    answer = {
        **example("conversations.get", "release-group"),
        "self": WORKER_ID,
        "canManageMembers": False,
    }
    record(monkeypatch, {"conversations.get": answer})

    result = invoke("show", GROUP_ID)

    assert result.exit_code == 0, result.output
    assert result.stdout.splitlines()[1] == f"you are {WORKER_ID} and cannot manage members"


def test_show_json_is_the_daemon_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    answer = example("conversations.get", "release-group")
    record(monkeypatch, {"conversations.get": answer})

    result = invoke("show", GROUP_ID, "--json")

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == answer


def _listed(heads: list[dict], cursor: str | None = None) -> dict:
    return {"ok": True, "participant": OPERATOR, "conversations": heads, "nextCursor": cursor}


def test_list_asks_for_groups_only(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = record(monkeypatch, {"conversations.list": _listed([GROUP_HEAD])})

    result = invoke("list")

    assert result.exit_code == 0, result.output
    assert calls == [("conversations.list", {"from": OPERATOR, "kinds": ["group"]})]
    assert_declared_params(calls)
    lines = result.stdout.splitlines()
    assert lines[0] == "groups"
    assert lines[1].split() == ["GROUP", "TITLE", "MEMBERS", "MESSAGES", "LAST"]
    assert lines[2].split() == [GROUP_ID, "release", "3", "3", "2026-09-21T14:18:20Z"]
    assert len(lines) == 3


def test_list_pages_with_limit_and_cursor(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = record(monkeypatch, {"conversations.list": _listed([GROUP_HEAD], cursor="next-1")})

    result = invoke("list", "--limit", "1", "--cursor", "abc")

    assert result.exit_code == 0, result.output
    assert calls == [
        (
            "conversations.list",
            {"from": OPERATOR, "kinds": ["group"], "limit": 1, "cursor": "abc"},
        )
    ]
    assert_declared_params(calls)
    assert result.stdout.splitlines()[-1] == "more: --cursor next-1"


def test_an_empty_list_says_so(monkeypatch: pytest.MonkeyPatch) -> None:
    record(monkeypatch, {"conversations.list": _listed([])})

    result = invoke("list")

    assert result.exit_code == 0, result.output
    assert result.stdout == "no groups\n"


def test_list_json_is_the_daemon_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    answer = _listed([GROUP_HEAD], cursor="next-1")
    record(monkeypatch, {"conversations.list": answer})

    result = invoke("list", "--json")

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == answer
