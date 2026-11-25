"""Synthetic, schema-validated examples for the group conversation methods (slice GC2).

Decision notes (group chat design draft §3 "Examples", brief GC2 item 3):

- One world with the conversation examples (``examples``): the owner
  ``user:alice`` and her agent ``worker``, plus a second agent ``reviewer``.
  A group member is keyed by an ``a.`` id, so here the two agents carry the
  placeholder ids ``a.workeraaaaaaaaaaaaaaaaaaaa`` and
  ``a.revieweraaaaaaaaaaaaaaaaaa`` and their id-form addresses on the node
  ``node``.  The direct conversation examples keep the worker's name-form
  address ``agent:alice:node:worker`` unchanged (GUI tests and the Contract
  Mock read it): an agent has both spellings, and only the id form can be a
  group member.
- The group "release" is created by alice with worker and reviewer.
  Entries of one call are stamped one millisecond apart, so the members
  joined at successive milliseconds.
- Over HTTP the caller is always the owner, so ``CONVERSATION_NOT_MEMBER``
  and ``CALLER_NOT_AUTHORIZED`` (an agent caller) cannot be shown here; the
  domain-error cases show the refusals an owner can meet.
- Slice GC3: ``GROUP_HISTORY_CASES`` show the same group's history, joined to
  the ``conversations.history`` cases in ``examples``.  There is no group
  ``message.send`` case: the send's HTTP profile still requires ``to``, so
  an HTTP example of it would describe a call the HTTP host refuses.
- Slice GH: an HTTP client posts to the group with ``conversations.post``;
  its success case is alice's request "please tag the release" answering
  her earlier "please cut the release" (``_ASKED`` of the history case) and
  mentioning worker.  Its ``messageId`` is the group send formula over the
  example key and the group id.
- Slice GC6: the group methods tell one story at one instant, the one the
  reads (``get``, ``list`` with kinds, ``history``) answer: alice creates
  the group, asks "please cut the release", then posts "please tag the
  release" over HTTP (the ``conversations.post`` success, replying to her
  request and mentioning worker), and worker answers that post with its
  ordinary reply.  Each mutation's answer is true at its own later instant:
  alice then removes reviewer and adds it back.  Every id is the one the
  handlers mint for these params: the group id from the owner and the
  create key, the system entry ids from the group, the method, the caller
  and the key, the posts' ids by the group send formula (alice's request
  was posted with the key ``example-post-release-cut-01``), and worker's
  reply ``reply_message_id`` of its group row for the post (a reply
  through ``message.reply`` to the owner carries no mentions).  worker and reviewer are
  the two agents the ``targets`` example lists.  The refusals of an
  unknown agent and of another owner's agent carry the handler's messages.
  tests/daemon/contract_mock/group_story pins all of this against the
  published contract.
- Slices B1 & B3: ``group-page`` includes ``memberNames`` matching the
  story's members; ``conversations.create``'s ``release-group`` names the
  agent members with their ``a.<id>`` ids (matching what the GUI sends and
  ``selectorParams`` checks); ``conversations.members.remove`` adds
  ``remove-worker`` so removing either agent in the story group works.
"""

from __future__ import annotations

from typing import Any

from hyprial.kernel import MethodExample, MethodExampleError, MethodExampleSet, ipc_errors

OPERATOR = "user:alice"
GROUP_ID = "g.4lvgrfdnb4vtmpxk36wa55phb4"
WORKER_ID = "a.workeraaaaaaaaaaaaaaaaaaaa"
REVIEWER_ID = "a.revieweraaaaaaaaaaaaaaaaaa"
WORKER_ADDRESS = "agent:alice:node:a.workeraaaaaaaaaaaaaaaaaaaa"
REVIEWER_ADDRESS = "agent:alice:node:a.revieweraaaaaaaaaaaaaaaaaa"
CREATED_AT_MS = 1790000120000
TITLE = "release"
_ADD_ENTRY = "f4c36e3a-2b42-5e79-b9e8-4eee2d76d69d"
_REMOVE_ENTRY = "6ec9a083-b66c-5e7f-89c6-6a81c2d5a6cf"
_REMOVE_WORKER_ENTRY = "a15f9ec5-94e9-57ce-a8b4-1b3d26f399cf"
_ASKED = "8a6a4329-6c56-52bd-bf24-4f2dfe08022a"
_POSTED = "4adbe4a7-0c35-5e04-95ea-f5b7ffca27b4"
_ANSWERED = "b602ca69-3f2b-51ff-ac6b-4aa7a54cde63"
_ASKED_AT_MS = CREATED_AT_MS + 60000
_POSTED_AT_MS = CREATED_AT_MS + 120000
_ANSWERED_AT_MS = CREATED_AT_MS + 180000
_ADDED_AT_MS = CREATED_AT_MS + 300000


def _member(member_id: str, name: str, at: int, *, address: str | None = None) -> dict[str, Any]:
    record: dict[str, Any] = {
        "memberId": member_id,
        "kind": "user" if address is None else "agent",
        "displayName": name,
        "manager": address is None,
        "joinedAtMs": at,
        "addedBy": OPERATOR,
    }
    if address is not None:
        record["address"] = address
    return record


_OWNER = _member(OPERATOR, "alice", CREATED_AT_MS)
_WORKER = _member(WORKER_ID, "worker", CREATED_AT_MS + 1, address=WORKER_ADDRESS)
_REVIEWER = _member(REVIEWER_ID, "reviewer", CREATED_AT_MS + 2, address=REVIEWER_ADDRESS)
_LATE_REVIEWER = _member(REVIEWER_ID, "reviewer", _ADDED_AT_MS, address=REVIEWER_ADDRESS)
_GROUP = {
    "ok": True,
    "conversationId": GROUP_ID,
    "kind": "group",
    "title": TITLE,
    "createdBy": OPERATOR,
    "createdAtMs": CREATED_AT_MS,
    "members": [_OWNER, _WORKER, _REVIEWER],
}
GROUP_HEAD = {
    "kind": "group",
    "conversationId": GROUP_ID,
    "legacyConversationId": GROUP_ID,
    "title": TITLE,
    "memberCount": 3,
    "lastMessageAtMs": _ANSWERED_AT_MS,
    "lastMessageId": _ANSWERED,
    "messages": 3,
    "messageClass": "interaction",
}
_UNKNOWN_GROUP = "g.unknowngroupaaaaaaaaaaaaaa"
_HTTP_ONLY_OWNER = (
    "over HTTP the caller is the owner; an empty answer is a successful call "
    "that changed nothing, shown as a success"
)


def _key(name: str) -> str:
    return f"example-{name}"


_POST_KEY = "example-post-release-tag-01"


def _post_params(**overrides: Any) -> dict[str, Any]:
    return {"from": OPERATOR, "conversationId": GROUP_ID, "message": "please tag the release",
            "summary": "please tag the release", "idempotencyKey": _POST_KEY,
            "replyTo": _ASKED, "mentions": [WORKER_ID], **overrides}


#: ``conversations.post`` (slice GH), the last set of ``GROUP_EXAMPLES``.
GROUP_POST_EXAMPLES = MethodExampleSet(
    method="conversations.post",
    cases=(
        MethodExample(
            name="owner-posts-reply", kind="success",
            description=(
                "alice answers her own request in the release group and mentions worker; "
                "one entry, one delivery whose target is the group"
            ),
            params=_post_params(),
            result={
                "ok": True, "actingPrincipal": OPERATOR, "operationId": _POST_KEY,
                "conversationId": GROUP_ID, "messageId": _POSTED,
                "deliveries": [{"target": GROUP_ID, "messageId": _POSTED,
                                "conversationId": GROUP_ID, "accepted": True,
                                "queued": False}],
            },
        ),
        MethodExample(
            name="direct-conversation-id", kind="invalid-params",
            description="only a group id is posted to; a direct send is message.send",
            params=_post_params(conversationId="conversation-1"),
            error=MethodExampleError(
                ipc_errors.INVALID_ARGUMENT, "conversationId is malformed"
            ),
        ),
        MethodExample(
            name="unknown-group", kind="domain-error",
            description="no readable chat space holds this group",
            params=_post_params(conversationId=_UNKNOWN_GROUP),
            error=MethodExampleError(
                ipc_errors.CONVERSATION_NOT_FOUND, "no such group conversation",
                {"conversationId": _UNKNOWN_GROUP},
            ),
        ),
    ),
    empty_not_applicable="a post writes one entry or is refused",
)

GROUP_EXAMPLES = (
    MethodExampleSet(
        method="conversations.create",
        cases=(
            MethodExample(
                name="release-group", kind="success",
                description="alice creates the group release with worker and reviewer",
                params={"from": OPERATOR, "title": TITLE, "members": [WORKER_ID, REVIEWER_ID],
                        "idempotencyKey": _key("create")},
                result=_GROUP,
            ),
            MethodExample(
                name="missing-title", kind="invalid-params",
                description="a group needs a title",
                params={"from": OPERATOR, "members": ["worker"],
                        "idempotencyKey": _key("create")},
                error=MethodExampleError(ipc_errors.INVALID_ARGUMENT, "title is required"),
            ),
            MethodExample(
                name="unknown-agent", kind="domain-error",
                description="a member that names no agent of alice on this machine",
                params={"from": OPERATOR, "title": TITLE, "members": ["ghost"],
                        "idempotencyKey": _key("create-ghost")},
                error=MethodExampleError(
                    ipc_errors.AGENT_NOT_FOUND,
                    "no agent 'ghost' of this owner on this machine",
                    {"member": "ghost"},
                ),
            ),
            MethodExample(
                name="other-owner", kind="domain-error",
                description="an agent of another owner cannot join a group this round",
                params={"from": OPERATOR, "title": TITLE,
                        "members": ["agent:bob:node:a.bobagentaaaaaaaaaaaaaaaaaa"],
                        "idempotencyKey": _key("create-bob")},
                error=MethodExampleError(
                    ipc_errors.UNSUPPORTED_TARGET,
                    "a member of another owner cannot join this round",
                    {"member": "agent:bob:node:a.bobagentaaaaaaaaaaaaaaaaaa"},
                ),
            ),
        ),
        empty_not_applicable="a create always answers the group it made or replayed",
    ),
    MethodExampleSet(
        method="conversations.get",
        cases=(
            MethodExample(
                name="release-group", kind="success",
                description="the group as its owner sees it",
                params={"from": OPERATOR, "conversationId": GROUP_ID},
                result={**_GROUP, "self": OPERATOR, "canManageMembers": True},
            ),
            MethodExample(
                name="malformed-id", kind="invalid-params",
                description="the group id must match the declared pattern",
                params={"from": OPERATOR, "conversationId": "not-a-group"},
                error=MethodExampleError(
                    ipc_errors.INVALID_ARGUMENT, "conversationId is malformed"
                ),
            ),
            MethodExample(
                name="unknown-group", kind="domain-error",
                description="no readable chat space holds this group",
                params={"from": OPERATOR, "conversationId": _UNKNOWN_GROUP},
                error=MethodExampleError(
                    ipc_errors.CONVERSATION_NOT_FOUND, "no such group conversation",
                    {"conversationId": _UNKNOWN_GROUP},
                ),
            ),
        ),
        empty_not_applicable="a group always has its owner as a member",
    ),
    MethodExampleSet(
        method="conversations.members.add",
        cases=(
            MethodExample(
                name="add-reviewer", kind="success",
                description="alice adds reviewer back after removing it",
                params={"from": OPERATOR, "conversationId": GROUP_ID, "member": "reviewer",
                        "idempotencyKey": _key("add")},
                result={"ok": True, "conversationId": GROUP_ID,
                        "members": [_OWNER, _WORKER, _LATE_REVIEWER], "entryId": _ADD_ENTRY},
            ),
            MethodExample(
                name="already-a-member", kind="success",
                description="adding a current member changes nothing",
                params={"from": OPERATOR, "conversationId": GROUP_ID, "member": WORKER_ID,
                        "idempotencyKey": _key("add-again")},
                result={"ok": True, "conversationId": GROUP_ID,
                        "members": [_OWNER, _WORKER, _REVIEWER], "entryId": None},
            ),
            MethodExample(
                name="missing-member", kind="invalid-params",
                description="the member to add is required",
                params={"from": OPERATOR, "conversationId": GROUP_ID,
                        "idempotencyKey": _key("add")},
                error=MethodExampleError(ipc_errors.INVALID_ARGUMENT, "member is required"),
            ),
            MethodExample(
                name="unknown-agent", kind="domain-error",
                description="a member that names no agent of alice on this machine",
                params={"from": OPERATOR, "conversationId": GROUP_ID, "member": "ghost",
                        "idempotencyKey": _key("add-ghost")},
                error=MethodExampleError(
                    ipc_errors.AGENT_NOT_FOUND,
                    "no agent 'ghost' of this owner on this machine",
                    {"member": "ghost"},
                ),
            ),
        ),
        empty_not_applicable=_HTTP_ONLY_OWNER,
    ),
    MethodExampleSet(
        method="conversations.members.remove",
        cases=(
            MethodExample(
                name="remove-reviewer", kind="success",
                description="alice removes reviewer after worker's reply",
                params={"from": OPERATOR, "conversationId": GROUP_ID,
                        "memberId": REVIEWER_ID, "idempotencyKey": _key("remove")},
                result={"ok": True, "conversationId": GROUP_ID,
                        "members": [_OWNER, _WORKER], "entryId": _REMOVE_ENTRY},
            ),
            MethodExample(
                name="remove-worker", kind="success",
                description="alice removes worker from the release group",
                params={"from": OPERATOR, "conversationId": GROUP_ID,
                        "memberId": WORKER_ID, "idempotencyKey": _key("remove-worker")},
                result={"ok": True, "conversationId": GROUP_ID,
                        "members": [_OWNER, _REVIEWER], "entryId": _REMOVE_WORKER_ENTRY},
            ),
            MethodExample(
                name="not-a-member", kind="success",
                description="removing someone who is not a member changes nothing",
                params={"from": OPERATOR, "conversationId": GROUP_ID,
                        "memberId": REVIEWER_ID, "idempotencyKey": _key("remove-again")},
                result={"ok": True, "conversationId": GROUP_ID,
                        "members": [_OWNER, _WORKER], "entryId": None},
            ),
            MethodExample(
                name="missing-member-id", kind="invalid-params",
                description="the member id to remove is required",
                params={"from": OPERATOR, "conversationId": GROUP_ID,
                        "idempotencyKey": _key("remove")},
                error=MethodExampleError(ipc_errors.INVALID_ARGUMENT, "memberId is required"),
            ),
            MethodExample(
                name="owner-cannot-leave", kind="domain-error",
                description="the owner is a manager and cannot leave its own group",
                params={"from": OPERATOR, "conversationId": GROUP_ID,
                        "memberId": OPERATOR, "idempotencyKey": _key("leave")},
                error=MethodExampleError(
                    ipc_errors.INVALID_ARGUMENT, "the owner cannot leave a group"
                ),
            ),
        ),
        empty_not_applicable=_HTTP_ONLY_OWNER,
    ),
    GROUP_POST_EXAMPLES,
)

_CREATE_ENTRIES = (
    "5ab32029-8c44-5b8e-bcbe-c86ec030c109",
    "1671b8fb-a962-5c8b-8691-d1b916ad24f1",
    "8b41f47b-b650-54ea-bd75-560d3a59193b",
)


def _system_entry(entry_id: str, at: int, event: str, member: str, sentence: str
                  ) -> dict[str, Any]:
    return {"kind": "message", "messageId": entry_id, "conversationId": GROUP_ID,
            "from": OPERATOR, "to": GROUP_ID, "intent": "request", "createdAtMs": at,
            "message": sentence, "origin": {}, "messageClass": "system",
            "system": {"event": event, "memberId": member}}


def _message_entry(entry_id: str, at: int, author: str, text: str, **extra: Any
                   ) -> dict[str, Any]:
    return {"kind": "message", "messageId": entry_id, "conversationId": GROUP_ID,
            "from": author, "to": GROUP_ID, "intent": "reply" if "replyTo" in extra else "request",
            "createdAtMs": at, "message": text, "origin": {}, "messageClass": "interaction",
            **extra}



#: ``conversations.history`` cases for the group (slice GC3), in this world.
GROUP_HISTORY_CASES = (
    MethodExample(
        name="group-page", kind="success",
        description=(
            "the release group: its creation, two members joining, alice's request, "
            "her post that answers it and mentions worker, and worker's reply to the post"
        ),
        params={"from": OPERATOR, "conversationId": GROUP_ID},
        result={
            "ok": True,
            "conversationId": GROUP_ID,
            "entries": [
                _system_entry(_CREATE_ENTRIES[0], CREATED_AT_MS, "created", OPERATOR,
                              'alice created the group "release"'),
                _system_entry(_CREATE_ENTRIES[1], CREATED_AT_MS + 1, "member-added", WORKER_ID,
                              "alice added worker"),
                _system_entry(_CREATE_ENTRIES[2], CREATED_AT_MS + 2, "member-added",
                              REVIEWER_ID, "alice added reviewer"),
                _message_entry(_ASKED, _ASKED_AT_MS, OPERATOR, "please cut the release"),
                _message_entry(
                    _POSTED, _POSTED_AT_MS, OPERATOR, "please tag the release",
                    replyTo={"messageId": _ASKED, "from": OPERATOR,
                             "excerpt": "please cut the release"},
                    mentions=[WORKER_ID],
                ),
                _message_entry(
                    _ANSWERED, _ANSWERED_AT_MS, WORKER_ID, "tag is pushed",
                    replyTo={"messageId": _POSTED, "from": OPERATOR,
                             "excerpt": "please tag the release"},
                ),
            ],
            "nextCursor": None,
            "legacyConversationId": GROUP_ID,
            "recordAvailable": True,
            "memberNames": {
                OPERATOR: "alice",
                WORKER_ID: "worker",
                REVIEWER_ID: "reviewer",
            },
        },
    ),
    MethodExample(
        name="unknown-group", kind="domain-error",
        description="no readable chat space holds this group",
        params={"from": OPERATOR, "conversationId": _UNKNOWN_GROUP},
        error=MethodExampleError(
            ipc_errors.CONVERSATION_NOT_FOUND, "no such group conversation",
            {"conversationId": _UNKNOWN_GROUP},
        ),
    ),
)

__all__ = [
    "GROUP_EXAMPLES",
    "GROUP_HISTORY_CASES",
    "GROUP_HEAD",
    "GROUP_POST_EXAMPLES",
    "GROUP_ID",
    "OPERATOR",
    "REVIEWER_ID",
    "WORKER_ID",
]
