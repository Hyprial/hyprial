"""Synthetic, schema-validated examples for the HTTP-exposed ``conversations.*`` reads.

Every HTTP-exposed method carries ``x-hyprial-examples`` (the contract mock
may skip none), so the two conversation reads get the shared HTTP mock
coverage set: one success, one empty and one invalid-params case.  The
examples are exported as HTTP examples, where ``from`` is host-injected and
the caller aliases are refused at the entry, so none of the caller rule's
refusals is reachable deterministically; the sets state
``domain_errors_not_applicable`` instead of showing a session-only refusal.

Slice GC2: ``conversations.list`` gains a last case, ``with-groups``, that
asks for ``kinds`` and lists the example group of ``group_examples`` before
the two direct conversations; the earlier cases (the mock's first success
and empty answers) are unchanged.  The group methods' sets live in
``group_examples`` and are appended to this catalog last.

Slice GC3: ``conversations.history`` gains the group cases of
``group_examples`` last (a group page and the unknown-group refusal, its
one domain error), so it no longer states ``domain_errors_not_applicable``.

Slice GC6: the first ``conversations.list`` case declares ``kinds:
["direct"]``, the answer the daemon gives with that value or without
``kinds``.  The Contract Mock takes the first case whose params agree with
a request and an omitted key agrees with anything, so without it a request
for ``["direct", "group"]`` was answered with the direct-only list and the
group head of ``with-groups`` was unreachable; a request without ``kinds``
still gets the first case.  A last case, ``groups-only``, answers
``kinds: ["group"]`` (the CLI's group list) with the group head alone.
"""

from __future__ import annotations

from hyprial.kernel import MethodExample, MethodExampleError, MethodExampleSet, ipc_errors

from .group_examples import GROUP_EXAMPLES, GROUP_HEAD, GROUP_HISTORY_CASES

# One synthetic world shared with the message examples (message/examples.py,
# message/send/examples.py): user alice, the deliverable agent
# ``agent:alice:node:worker``, the conversation ``conversation-1`` and its
# message ``message-1``.  The no-``conversationId`` send example answers with
# the default conversation ``dc.example-default-conversation``, so that
# conversation has a document here too: a first send in the preview finds it.
_OPERATOR = "user:alice"
_AGENT = "agent:alice:node:worker"
_CELL_ID = "c.conversationoneaaaaaaaaaaa"
_LEGACY_ID = "conversation-1"
_DEFAULT_CELL_ID = "c.defaultconversationaaaaaaa"
_DEFAULT_LEGACY_ID = "dc.example-default-conversation"
_MESSAGE_AT_MS = 1790000000000
_DEFAULT_MESSAGE_AT_MS = 1790000060000
_HEAD = {
    "conversationId": _CELL_ID,
    "kind": "agent",
    "peer": _AGENT,
    "legacyConversationId": _LEGACY_ID,
    "lastMessageAtMs": _MESSAGE_AT_MS,
    "lastMessageId": "message-1",
    "messages": 1,
    "messageClass": "interaction",
}
_DEFAULT_HEAD = {
    **_HEAD,
    "conversationId": _DEFAULT_CELL_ID,
    "legacyConversationId": _DEFAULT_LEGACY_ID,
    "lastMessageAtMs": _DEFAULT_MESSAGE_AT_MS,
}
# Same message as the ``message.history`` example, plus ``messageClass``.
_ENTRY = {
    "kind": "message",
    "messageId": "message-1",
    "conversationId": _LEGACY_ID,
    "from": _OPERATOR,
    "to": _AGENT,
    "intent": "request",
    "createdAtMs": _MESSAGE_AT_MS,
    "message": "演示消息，请确认收到。",
    "origin": {},
    "messageClass": "interaction",
}
# The message the no-``conversationId`` send example accepts ("hello").
_DEFAULT_ENTRY = {
    **_ENTRY,
    "conversationId": _DEFAULT_LEGACY_ID,
    "createdAtMs": _DEFAULT_MESSAGE_AT_MS,
    "message": "hello",
}
_NO_DOMAIN_ERROR = (
    "the HTTP principal pins the caller; caller-rule refusals need a real session"
)

CONVERSATION_EXAMPLES = (
    MethodExampleSet(
        method="conversations.list",
        cases=(
            MethodExample(
                name="one-conversation", kind="success",
                description=(
                    "the owner-user's two conversations with the worker agent, "
                    "newest first: the default one and conversation-1"
                ),
                params={"from": _OPERATOR, "kinds": ["direct"]},
                result={
                    "ok": True,
                    "participant": _OPERATOR,
                    "conversations": [_DEFAULT_HEAD, _HEAD],
                    "nextCursor": None,
                    "recordAvailable": True,
                },
            ),
            MethodExample(
                name="no-conversations", kind="empty",
                description="the caller has no conversation in the record or the queue",
                params={"from": _OPERATOR},
                result={
                    "ok": True,
                    "participant": _OPERATOR,
                    "conversations": [],
                    "nextCursor": None,
                    "recordAvailable": True,
                },
                reason="a new operator has sent and received nothing",
            ),
            MethodExample(
                name="unknown-parameter", kind="invalid-params",
                description="only declared conversation read parameters are accepted",
                params={"from": _OPERATOR, "extra": True},
                error=MethodExampleError(ipc_errors.INVALID_ARGUMENT, "extra is not allowed"),
            ),
            MethodExample(
                name="with-groups", kind="success",
                description=(
                    "the owner asks for direct and group conversations: the release "
                    "group, last written by worker's reply, is newest, then the two "
                    "direct conversations"
                ),
                params={"from": _OPERATOR, "kinds": ["direct", "group"]},
                result={
                    "ok": True,
                    "participant": _OPERATOR,
                    "conversations": [GROUP_HEAD, _DEFAULT_HEAD, _HEAD],
                    "nextCursor": None,
                    "recordAvailable": True,
                },
            ),
            MethodExample(
                name="groups-only", kind="success",
                description="the owner asks for group conversations only: the release group",
                params={"from": _OPERATOR, "kinds": ["group"]},
                result={
                    "ok": True,
                    "participant": _OPERATOR,
                    "conversations": [GROUP_HEAD],
                    "nextCursor": None,
                    "recordAvailable": True,
                },
            ),
        ),
        domain_errors_not_applicable=_NO_DOMAIN_ERROR,
    ),
    MethodExampleSet(
        method="conversations.history",
        cases=(
            MethodExample(
                name="one-message", kind="success",
                description="one page of the owner's conversation cell",
                params={"from": _OPERATOR, "conversationId": _CELL_ID},
                result={
                    "ok": True,
                    "conversationId": _CELL_ID,
                    "entries": [_ENTRY],
                    "nextCursor": None,
                    "legacyConversationId": _LEGACY_ID,
                    "recordAvailable": True,
                },
            ),
            MethodExample(
                name="default-conversation-message", kind="success",
                description=(
                    "the default conversation a send without conversationId joins, "
                    "holding the message that send accepted"
                ),
                params={"from": _OPERATOR, "conversationId": _DEFAULT_CELL_ID},
                result={
                    "ok": True,
                    "conversationId": _DEFAULT_CELL_ID,
                    "entries": [_DEFAULT_ENTRY],
                    "nextCursor": None,
                    "legacyConversationId": _DEFAULT_LEGACY_ID,
                    "recordAvailable": True,
                },
            ),
            MethodExample(
                name="body-omitted", kind="success",
                description=(
                    "a message the record holds without its body, after the local "
                    "queue no longer holds it"
                ),
                params={"from": _OPERATOR, "conversationId": _CELL_ID},
                result={
                    "ok": True,
                    "conversationId": _CELL_ID,
                    "entries": [
                        _ENTRY,
                        {
                            **_ENTRY,
                            "messageId": "message-2",
                            "createdAtMs": _MESSAGE_AT_MS + 60000,
                            "message": "",
                            "bodyOmitted": "redacted",
                        },
                    ],
                    "nextCursor": None,
                    "legacyConversationId": _LEGACY_ID,
                    "recordAvailable": True,
                },
            ),
            MethodExample(
                name="unknown-conversation", kind="empty",
                description="an unknown cell, or one the caller has no row of",
                params={"from": _OPERATOR, "conversationId": _CELL_ID},
                result={
                    "ok": True,
                    "conversationId": _CELL_ID,
                    "entries": [],
                    "nextCursor": None,
                    "legacyConversationId": None,
                    "recordAvailable": True,
                },
                reason="a conversation the caller has no row of answers an empty page",
            ),
            MethodExample(
                name="malformed-conversation-id", kind="invalid-params",
                description="the conversation cell id must match the declared pattern",
                params={"conversationId": "not-a-cell"},
                error=MethodExampleError(
                    ipc_errors.INVALID_ARGUMENT, "conversationId is malformed"
                ),
            ),
            *GROUP_HISTORY_CASES,
        ),
    ),
    *GROUP_EXAMPLES,
)

__all__ = ["CONVERSATION_EXAMPLES"]
