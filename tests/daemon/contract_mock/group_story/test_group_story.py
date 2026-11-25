"""The published group examples tell one story the Contract Mock can walk.

Decision notes (slice GC6):

- The source is the committed ``contract/daemon-request/openapi.json`` read
  by ``load_contract``, the projection the GUI's contract table is
  generated from, so a stale dump fails here and not only in the GUI.
- The story: alice creates the group "release" with worker and reviewer,
  asks "please cut the release", posts "please tag the release" over HTTP
  (answering her request, mentioning worker), and worker answers the post
  with its ordinary reply.  ``get``, ``list`` with kinds and ``history``
  answer that instant; ``members.remove`` then ``members.add`` (reviewer
  out and back) are true at their own later instants.
- Every id is checked against the formula the handlers mint it with, and
  every agent member against the ``targets`` example, so the GUI can build
  the group screen on the mock without meeting an id that no other answer
  knows.
- ``_first_agreeing`` mirrors the first pass of the JavaScript mock's case
  selection (gui/contract-mock/contract.ts ``selectCase``): the first case
  of the scenario whose declared params agree with the request, an omitted
  key agreeing with anything.  The requests are GUI-shaped: ``from`` is
  host-injected, so they leave it out.
"""

from __future__ import annotations

from typing import Any
from uuid import NAMESPACE_URL, uuid5

from contract_mock import load_contract
import pytest

from hyprial.daemon.impl.application.messaging.inbox_surface.groups.delivery import group_row_id
from hyprial.daemon.impl.application.messaging.inbox_surface.groups.messages import EXCERPT_CHARS
from hyprial.daemon.impl.application.messaging.inbox_surface.groups.requests import _entry_id
from hyprial.kernel import mint_group_id, parse_agent_uri, reply_message_id

CONTRACT = load_contract()


def _case(method: str, name: str) -> dict[str, Any]:
    return dict(CONTRACT.case(method, name))


def _result(method: str, name: str) -> dict[str, Any]:
    return dict(_case(method, name)["result"])


CREATE = _case("conversations.create", "release-group")
GROUP = _result("conversations.create", "release-group")
GROUP_ID = GROUP["conversationId"]
OWNER = GROUP["createdBy"]
GOT = _result("conversations.get", "release-group")
LISTED = _result("conversations.list", "with-groups")
POST = _case("conversations.post", "owner-posts-reply")
HISTORY = _result("conversations.history", "group-page")
REMOVE = _case("conversations.members.remove", "remove-reviewer")
REMOVE_WORKER = _case("conversations.members.remove", "remove-worker")
ADD = _case("conversations.members.add", "add-reviewer")
TARGETS = _result("targets", "deliverable-agent")["targets"]
ENTRIES = HISTORY["entries"]
MESSAGES = [entry for entry in ENTRIES if entry["messageClass"] == "interaction"]
SYSTEM = [entry for entry in ENTRIES if entry["messageClass"] == "system"]


def _entry(message_id: str) -> dict[str, Any]:
    [entry] = [entry for entry in ENTRIES if entry["messageId"] == message_id]
    return entry


def _send_id(key: str) -> str:
    return str(uuid5(NAMESPACE_URL, f"hyprial:send:{key}:{GROUP_ID}:0"))


def _first_agreeing(method: str, scenario_kind: str, request: dict[str, Any]) -> str:
    for case in CONTRACT.methods[method].examples:
        if case["kind"] != scenario_kind:
            continue
        declared = case["params"]
        if all(key not in request or request[key] == value for key, value in declared.items()):
            return str(case["name"])
    raise AssertionError(f"{method}: no {scenario_kind} case agrees with {request}")


def test_one_group_id_is_minted_by_create_and_named_everywhere() -> None:
    assert GROUP_ID == mint_group_id(f"{OWNER}\0{CREATE['params']['idempotencyKey']}".encode())
    assert GOT["conversationId"] == GROUP_ID
    assert [head["conversationId"] for head in LISTED["conversations"]
            if head["kind"] == "group"] == [GROUP_ID]
    assert HISTORY["conversationId"] == HISTORY["legacyConversationId"] == GROUP_ID
    for case in (POST, REMOVE, REMOVE_WORKER, ADD):
        assert case["params"]["conversationId"] == case["result"]["conversationId"] == GROUP_ID
    assert {entry["conversationId"] for entry in ENTRIES} == {GROUP_ID}
    assert {delivery["target"] for delivery in POST["result"]["deliveries"]} == {GROUP_ID}


def test_every_agent_member_is_an_agent_the_directory_lists() -> None:
    directory = {row["agentId"]: row for row in TARGETS}
    agents = [member for member in GROUP["members"] if member["kind"] == "agent"]
    assert [member["memberId"] for member in agents] == CREATE["params"]["members"]
    for member in agents:
        row = directory[member["memberId"]]
        assert row["displayName"] == member["displayName"]
        owner, node, _name = parse_agent_uri(row["targetUri"])
        assert parse_agent_uri(member["address"]) == (owner, node, member["memberId"])
    assert {member["memberId"] for member in agents} == set(directory)


def test_the_reads_answer_one_instant() -> None:
    assert {key: GOT[key] for key in GROUP} == GROUP
    assert GOT["self"] == OWNER and GOT["canManageMembers"] is True
    added = [entry["system"]["memberId"] for entry in SYSTEM]
    assert added == [member["memberId"] for member in GOT["members"]]
    assert [entry["system"]["event"] for entry in SYSTEM] == (
        ["created"] + ["member-added"] * (len(SYSTEM) - 1))
    [head] = [head for head in LISTED["conversations"] if head["kind"] == "group"]
    assert _result("conversations.list", "groups-only")["conversations"] == [head]
    assert head["title"] == GOT["title"]
    assert head["memberCount"] == len(GOT["members"])
    assert head["lastMessageAtMs"] == ENTRIES[-1]["createdAtMs"]
    assert head["lastMessageId"] == MESSAGES[-1]["messageId"]
    assert head["messages"] == len(MESSAGES)
    stamps = [entry["createdAtMs"] for entry in ENTRIES]
    assert stamps == sorted(set(stamps))
    others = [item["lastMessageAtMs"] for item in LISTED["conversations"] if item is not head]
    assert head is LISTED["conversations"][0] and all(at < head["lastMessageAtMs"] for at in others)
    assert HISTORY["memberNames"] == {
        member["memberId"]: member["displayName"] for member in GOT["members"]
    }


def test_create_writes_the_entries_history_shows() -> None:
    for index, entry in enumerate(SYSTEM):
        assert entry["messageId"] == _entry_id(GROUP_ID, "create", str(index))
        assert entry["createdAtMs"] == GROUP["createdAtMs"] + index
        assert entry["from"] == OWNER
    joined = [member["joinedAtMs"] for member in GOT["members"]]
    assert joined == [entry["createdAtMs"] for entry in SYSTEM]


def test_the_post_lands_in_history_with_its_mention_and_reply() -> None:
    params, answer = POST["params"], POST["result"]
    assert answer["messageId"] == _send_id(params["idempotencyKey"])
    assert answer["actingPrincipal"] == OWNER
    posted = _entry(answer["messageId"])
    assert posted["from"] == OWNER
    assert posted["message"] == params["message"]
    assert posted["mentions"] == params["mentions"]
    assert posted["replyTo"]["messageId"] == params["replyTo"]
    assert posted["intent"] == "reply"
    members = {member["memberId"] for member in GOT["members"]}
    assert set(params["mentions"]) <= members - {OWNER}
    asked = _entry(params["replyTo"])
    assert asked["messageId"] == _send_id("example-post-release-cut-01")
    assert asked["from"] == OWNER and "replyTo" not in asked


def test_every_reply_answers_an_earlier_message_as_the_handler_copies_it() -> None:
    replies = [entry for entry in MESSAGES if "replyTo" in entry]
    assert replies
    for reply in replies:
        answered = _entry(reply["replyTo"]["messageId"])
        assert answered["messageClass"] == "interaction"
        assert answered["createdAtMs"] < reply["createdAtMs"]
        assert reply["replyTo"] == {"messageId": answered["messageId"], "from": answered["from"],
                                    "excerpt": answered["message"][:EXCERPT_CHARS]}
        assert reply["intent"] == "reply"


def test_an_agent_answers_the_post_with_its_ordinary_reply() -> None:
    posted = POST["result"]["messageId"]
    [answer] = [entry for entry in MESSAGES if entry["from"] != OWNER]
    assert answer is MESSAGES[-1]
    assert answer["replyTo"]["messageId"] == posted
    assert answer["messageId"] == reply_message_id(group_row_id(GROUP_ID, posted, answer["from"]))
    assert "mentions" not in answer
    assert answer["from"] in {row["agentId"] for row in TARGETS}


def test_membership_changes_come_after_the_conversation() -> None:
    reviewer = REMOVE["params"]["memberId"]
    remove_key = REMOVE["params"]["idempotencyKey"]
    assert REMOVE["result"]["entryId"] == _entry_id(GROUP_ID, "remove", OWNER, remove_key)
    assert REMOVE["result"]["members"] == [
        member for member in GOT["members"] if member["memberId"] != reviewer]
    add_key = ADD["params"]["idempotencyKey"]
    assert ADD["result"]["entryId"] == _entry_id(GROUP_ID, "add", OWNER, add_key)
    [*kept, back] = ADD["result"]["members"]
    assert kept == REMOVE["result"]["members"]
    assert back["memberId"] == reviewer and back["displayName"] == ADD["params"]["member"]
    assert back["joinedAtMs"] > ENTRIES[-1]["createdAtMs"]
    worker = REMOVE_WORKER["params"]["memberId"]
    remove_worker_key = REMOVE_WORKER["params"]["idempotencyKey"]
    assert REMOVE_WORKER["result"]["entryId"] == _entry_id(
        GROUP_ID, "remove", OWNER, remove_worker_key
    )
    assert REMOVE_WORKER["result"]["members"] == [
        member for member in GOT["members"] if member["memberId"] != worker
    ]


@pytest.mark.parametrize(
    ("method", "request_params", "expected"),
    [
        ("conversations.list", {}, "one-conversation"),
        ("conversations.list", {"kinds": ["direct"]}, "one-conversation"),
        ("conversations.list", {"kinds": ["direct", "group"]}, "with-groups"),
        ("conversations.list", {"kinds": ["group"]}, "groups-only"),
        ("conversations.get", {"conversationId": GROUP_ID}, "release-group"),
        ("conversations.history", {"conversationId": GROUP_ID}, "group-page"),
        (
            "conversations.create",
            {
                "title": "release",
                "members": [member["memberId"] for member in GROUP["members"] if member["kind"] == "agent"],
            },
            "release-group",
        ),
        (
            "conversations.members.remove",
            {
                "conversationId": GROUP_ID,
                "memberId": REMOVE["params"]["memberId"],
            },
            "remove-reviewer",
        ),
        (
            "conversations.members.remove",
            {
                "conversationId": GROUP_ID,
                "memberId": REMOVE_WORKER["params"]["memberId"],
            },
            "remove-worker",
        ),
    ],
)
def test_the_mock_selects_the_story_for_gui_requests(
    method: str, request_params: dict[str, Any], expected: str,
) -> None:
    assert _first_agreeing(method, "success", request_params) == expected


def test_the_mock_walks_create_post_history() -> None:
    request = {key: value for key, value in CREATE["params"].items() if key != "from"}
    created = _result("conversations.create", _first_agreeing(
        "conversations.create", "success", request))
    group_id = created["conversationId"]
    post = {key: value for key, value in POST["params"].items() if key != "from"}
    posted = _result("conversations.post", _first_agreeing(
        "conversations.post", "success", {**post, "conversationId": group_id}))
    page = _result("conversations.history", _first_agreeing(
        "conversations.history", "success", {"conversationId": group_id}))
    assert posted["messageId"] in [entry["messageId"] for entry in page["entries"]]
