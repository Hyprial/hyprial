"""``conversations.create``: who may create, how members resolve, and replays (slice GC2)."""

from __future__ import annotations

import pytest

from hyprial.daemon.impl.conversations.groups import GroupLog
from hyprial.kernel import is_group_id_segment
from tests.daemon.conversations.group_requests.group_request_support import (
    OWNER,
    group_daemon,
    member_ids,
)
from tests.support.golden.app import golden_daemon as golden_daemon

CODE_NOT_AUTHORIZED = "CALLER_NOT_AUTHORIZED"
CODE_AGENT_NOT_FOUND = "AGENT_NOT_FOUND"
CODE_UNSUPPORTED = "UNSUPPORTED_TARGET"
CODE_INVALID = "INVALID_ARGUMENT"
CODE_UNAVAILABLE = "DAEMON_UNAVAILABLE"


def test_create_answers_the_group_with_the_owner_as_manager(golden_daemon, monkeypatch) -> None:
    booted = group_daemon(golden_daemon, monkeypatch)
    alpha, beta = booted.agent_id("alpha"), booted.agent_id("beta")

    answer = booted.create(["alpha", beta])

    assert is_group_id_segment(answer["conversationId"])
    assert (answer["kind"], answer["title"], answer["createdBy"]) == ("group", "release", OWNER)
    assert member_ids(answer) == [OWNER, alpha, beta]
    owner, first, second = answer["members"]
    assert (owner["kind"], owner["manager"], owner["displayName"]) == ("user", True, "op")
    assert "address" not in owner
    assert (first["kind"], first["manager"], first["displayName"]) == ("agent", False, "alpha")
    assert first["address"] == booted.address("alpha")
    assert second["displayName"] == "beta"
    assert {member["addedBy"] for member in answer["members"]} == {OWNER}
    assert answer["createdAtMs"] == owner["joinedAtMs"]


@pytest.mark.parametrize("spelling", ["name", "id", "address"])
def test_a_member_resolves_by_name_id_or_address(golden_daemon, monkeypatch, spelling) -> None:
    booted = group_daemon(golden_daemon, monkeypatch)
    reference = {
        "name": "gamma",
        "id": booted.agent_id("gamma"),
        "address": booted.address("gamma"),
    }[spelling]

    answer = booted.create([reference])

    assert member_ids(answer) == [OWNER, booted.agent_id("gamma")]


def test_every_member_of_one_create_is_kept_at_a_frozen_clock(golden_daemon, monkeypatch) -> None:
    booted = group_daemon(golden_daemon, monkeypatch)

    answer = booted.create(["alpha", "beta", "gamma"])

    names = ["alpha", "beta", "gamma"]
    assert member_ids(answer) == [OWNER, *(booted.agent_id(name) for name in names)]
    joined = [member["joinedAtMs"] for member in answer["members"]]
    assert joined == sorted(joined) and len(set(joined)) == len(joined)


def test_a_duplicate_member_is_refused(golden_daemon, monkeypatch) -> None:
    booted = group_daemon(golden_daemon, monkeypatch)

    error = booted.refused("conversations.create", title="t",
                           members=["alpha", booted.agent_id("alpha")], idempotencyKey="k")

    assert error.code == CODE_INVALID


@pytest.mark.parametrize("spelling", ["name", "id", "address", "user"])
def test_naming_the_user_agent_is_the_owner_again_and_refused(
    golden_daemon, monkeypatch, spelling
) -> None:
    """The owner is always a member as user:<owner>: the user agent is that member."""

    booted = group_daemon(golden_daemon, monkeypatch)
    agent = booted.user_agent()
    reference = {
        "name": "hyprial-user",
        "id": booted.agent_id("hyprial-user"),
        "address": agent.uri,
        "user": OWNER,
    }[spelling]

    error = booted.refused("conversations.create", title="t", members=["alpha", reference],
                           idempotencyKey="k")

    assert error.code == CODE_INVALID


def test_an_unknown_agent_is_agent_not_found(golden_daemon, monkeypatch) -> None:
    booted = group_daemon(golden_daemon, monkeypatch)

    for reference in ("ghost", "a.ghostaaaaaaaaaaaaaaaaaaaaa", "agent:op:node-1:a.ghostaaaaaaaaaaaaaaaaaaaaa"):
        error = booted.refused("conversations.create", title="t", members=[reference],
                               idempotencyKey=f"k-{reference}")
        assert error.code == CODE_AGENT_NOT_FOUND, reference
        assert error.data == {"member": reference}


@pytest.mark.parametrize(
    "reference", ["agent:bob:node-1:a.bobagentaaaaaaaaaaaaaaaaaa", "user:bob"],
)
def test_another_owners_agent_or_user_is_unsupported(golden_daemon, monkeypatch, reference) -> None:
    booted = group_daemon(golden_daemon, monkeypatch)

    error = booted.refused("conversations.create", title="t", members=[reference],
                           idempotencyKey="k")

    assert error.code == CODE_UNSUPPORTED
    assert error.data == {"member": reference}


def test_a_name_form_agent_is_refused(golden_daemon, monkeypatch) -> None:
    booted = group_daemon(golden_daemon, monkeypatch, id_form=False)

    error = booted.refused("conversations.create", title="t", members=["alpha"],
                           idempotencyKey="k")

    assert error.code == CODE_INVALID


@pytest.mark.parametrize("title", ["", "   "])
def test_an_empty_title_is_refused(golden_daemon, monkeypatch, title) -> None:
    booted = group_daemon(golden_daemon, monkeypatch)

    error = booted.refused("conversations.create", title=title, members=["alpha"],
                           idempotencyKey="k")

    assert error.code == CODE_INVALID


def test_a_replay_with_the_same_key_answers_the_same_group(golden_daemon, monkeypatch) -> None:
    booted = group_daemon(golden_daemon, monkeypatch)
    first = booted.create(["alpha"], key="same")
    booted.advance()
    log = GroupLog(placement=_placement(booted), writer="node-1")
    written = log.read(first["conversationId"])

    again = booted.create(["alpha"], key="same")
    other = booted.create(["alpha"], key="other")

    assert again == first
    assert log.read(first["conversationId"]) == written
    assert other["conversationId"] != first["conversationId"]


def test_a_member_agent_cannot_create(golden_daemon, monkeypatch) -> None:
    booted = group_daemon(golden_daemon, monkeypatch)

    error = booted.refused("conversations.create", "alpha", title="t", members=["beta"],
                           idempotencyKey="k")

    assert error.code == CODE_NOT_AUTHORIZED


def test_an_unwritable_document_is_daemon_unavailable(golden_daemon, monkeypatch) -> None:
    booted = group_daemon(golden_daemon, monkeypatch)
    booted.fs.down = True

    error = booted.refused("conversations.create", title="t", members=["alpha"],
                           idempotencyKey="k")

    assert error.code == CODE_UNAVAILABLE
    booted.fs.down = False
    assert member_ids(booted.create(["alpha"], key="k")) == [OWNER, booted.agent_id("alpha")]


def _placement(booted):  # type: ignore[no-untyped-def]
    from hyprial.daemon.impl.composition.conversations.placement import OwnerHomeGroupPlacement

    return OwnerHomeGroupPlacement(booted.app._conversation_placement, OWNER)
