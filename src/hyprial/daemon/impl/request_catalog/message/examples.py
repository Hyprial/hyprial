"""Synthetic, schema-validated examples for HTTP-exposed message reads.

Decision record (chat layout increment, 2026-11-05)
- ``targets``, ``message.query``, ``message.history``, ``message.send`` and
  ``message.send.key_lookup`` now describe one synthetic world: user alice, the
  deliverable agent ``agent:alice:node:worker``, conversation ``conversation-1``
  and message ``message-1``.  The previous split world (a ``demo`` agent and a
  ``demo-conversation`` in the reads, ``alice``/``conversation-1`` in the writes)
  was individually schema-valid but made the ordinary chat page unusable against
  the Contract Mock: the discovered conversation never belonged to a directory
  agent, so history could not be read and sending stayed unconfirmed.
- ``message.query`` publishes one success and one empty case per view.  The view
  is echoed in the result, so a single case cannot answer both inbox and
  outbox; the Mock selects the case whose declared params agree with the
  request instead of always serving the first one.
- The outbox entry carries the sender/recipient pair the GUI directory reads, so
  a screen reader and the conversation grouping see the same agent the inbox row
  names.
"""

from __future__ import annotations

from hyprial.kernel import MethodExample, MethodExampleError, MethodExampleSet, ipc_errors

_USER = "user:alice"
_TARGET = "agent:alice:node:worker"
_CONVERSATION = "conversation-1"
_MESSAGE = "message-1"
_TEXT = "演示消息，请确认收到。"
_ENTRY = {
    "kind": "message", "messageId": _MESSAGE,
    "conversationId": _CONVERSATION, "from": _USER, "to": _TARGET,
    "intent": "request", "createdAtMs": 1790000000000,
    "message": _TEXT, "origin": {},
}
_OUTBOX_ENTRY = {**_ENTRY, "sender": _USER, "recipient": _TARGET}

MESSAGE_EXAMPLES = (
    MethodExampleSet(
        method="message.history",
        cases=(
            MethodExample(
                name="one-message", kind="success",
                description="synthetic participant conversation with one message",
                params={"conversationId": _CONVERSATION, "limit": 20},
                result={"ok": True, "conversationId": _CONVERSATION,
                        "entries": [_ENTRY], "nextCursor": None},
            ),
            MethodExample(
                name="empty-conversation", kind="empty",
                description="a conversation can have no visible messages",
                params={"conversationId": _CONVERSATION},
                result={"ok": True, "conversationId": _CONVERSATION,
                        "entries": [], "nextCursor": None},
                reason="a participant-scoped history read may legitimately be empty",
            ),
            MethodExample(
                name="missing-conversation", kind="invalid-params",
                description="conversationId is required",
                params={}, error=MethodExampleError(ipc_errors.INVALID_ARGUMENT,
                                                    "conversationId is required"),
            ),
        ),
        domain_errors_not_applicable="a safe deterministic domain error needs a real session or participant",
    ),
    MethodExampleSet(
        method="message.query",
        cases=(
            MethodExample(
                name="one-inbox-message", kind="success",
                description="synthetic inbox with one message from the deliverable agent",
                params={"view": "inbox"},
                result={"ok": True, "actor": _USER, "view": "inbox", "entries": [_ENTRY]},
            ),
            MethodExample(
                name="one-outbox-message", kind="success",
                description="synthetic outbox with the same message the inbox holds",
                params={"view": "outbox"},
                result={"ok": True, "actor": _USER, "view": "outbox", "entries": [_OUTBOX_ENTRY]},
            ),
            MethodExample(
                name="empty-inbox", kind="empty",
                description="an inbox can contain no messages",
                params={"view": "inbox"},
                result={"ok": True, "actor": _USER, "view": "inbox", "entries": []},
                reason="a fresh owner's inbox is empty",
            ),
            MethodExample(
                name="empty-outbox", kind="empty",
                description="an outbox can contain no messages",
                params={"view": "outbox"},
                result={"ok": True, "actor": _USER, "view": "outbox", "entries": []},
                reason="a fresh owner's outbox is empty",
            ),
            MethodExample(
                name="missing-view", kind="invalid-params",
                description="inbox or outbox view is required",
                params={}, error=MethodExampleError(ipc_errors.INVALID_ARGUMENT, "view is required"),
            ),
        ),
        domain_errors_not_applicable="no deterministic domain error exists without external identity state",
    ),
    MethodExampleSet(
        method="message.send.lookup",
        cases=(
            MethodExample(
                name="accepted-send", kind="success",
                description="synthetic durable send record matching the original request",
                params={"target": _TARGET, "text": _TEXT, "conversationId": _CONVERSATION,
                        "idempotencyKey": "demo-send-key"},
                result={"status": "accepted", "commandId": "demo-send-key",
                        "conversationId": _CONVERSATION, "messageId": _MESSAGE},
            ),
            MethodExample(
                name="send-not-found", kind="empty",
                description="the original send has no durable record",
                params={"target": _TARGET, "text": _TEXT, "conversationId": _CONVERSATION,
                        "idempotencyKey": "demo-missing-key"},
                result={"status": "unknown", "reason": "not-found"},
                reason="absence is reported as unknown, never as accepted",
            ),
            MethodExample(
                name="missing-send-key", kind="invalid-params",
                description="the original idempotency key is required",
                params={"target": _TARGET, "text": _TEXT, "conversationId": _CONVERSATION},
                error=MethodExampleError(ipc_errors.INVALID_ARGUMENT,
                                         "idempotencyKey is required"),
            ),
        ),
        domain_errors_not_applicable="a deterministic domain refusal needs a real sender session",
    ),
)

OUTBOX_EXAMPLES = (MethodExampleSet(
    method="outbox.list",
    cases=(
        MethodExample(name="queued-row", kind="success", description="A queued message awaits delivery.",
            params={}, result={"ok": True, "entries": [{
                "messageId": _MESSAGE, "conversationId": _CONVERSATION, "sender": _USER,
                "recipient": _TARGET, "intent": "request", "createdAtMs": 1790000000000,
                "attempts": 0, "failedAttempts": 0, "nextAttemptMs": 1790000000001,
                "expiresAtMs": 1790000001000, "undeliverableScheme": False,
            }]}),
        MethodExample(name="empty", kind="empty", description="There are no queued messages.",
            reason="The outbox can be empty.", params={}, result={"ok": True, "entries": []}),
        MethodExample(name="non-object", kind="invalid-params",
            description="The IPC server rejects non-object parameters.", params=[],
            error=MethodExampleError(ipc_errors.INVALID_ARGUMENT, "params must be an object")),
    ),
    domain_errors_not_applicable="The reader has no deterministic domain-specific refusal.",
),)
