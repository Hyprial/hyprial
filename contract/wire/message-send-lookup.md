# Original send lookup

`message.send.lookup` resolves an uncertain, single-agent `message.send` by
its original request fields. It is a read-only daemon method, not a retry.

Parameters are `from`/`actor`, optional `sessionRef`, `idempotencyKey`,
`conversationId`, `target` and `text`. The last four fields must be the
original values; `target` is one canonical agent URI. Other fields are
rejected. The caller is authenticated by the existing message principal
chain. IPC claims without an authenticated operator, adapter or live session
are rejected even when the older sender-auth rollout is in observe mode.
No caller can provide another participant selector or a guessed message ID.

The lookup reads the authenticated sender's `sender_copies` row for the
existing agent delivery key `idempotencyKey:target:0`. Sender, conversation,
recipient, request intent, durable lifecycle and frozen text must agree.
This contract is for the GUI's single-target text send; it does not resolve
route/user sends, replies, or aggregate multi-target outcomes.

Successful acceptance evidence has exactly this shape:

```json
{
  "status": "accepted",
  "commandId": "original-idempotency-key",
  "conversationId": "original-conversation",
  "messageId": "stored-authoritative-message-id"
}
```

Acceptance means the sending daemon durably recorded the message. The
sender copy commits atomically with the original outbox entry, before
delivery I/O, and remains after the outbox entry is deleted. This evidence
does not claim delivery, fetching, a reply, or user reading. Those outcomes
retain their existing separate interfaces.

An unresolved lookup returns `{"status":"unknown","reason":"not-found"}`.
Other reasons are `request-mismatch` (frozen text differs), `invalid-record`
(malformed or ambiguous durable evidence), and `unavailable` (the store
cannot be read). None returns a message ID. Missing evidence never proves
that the send failed or permits a resend: the original request may not have
committed yet. Repeated reads can change unknown to accepted without sending
another message. Reads never fetch, acknowledge, enqueue or update receipts.

The host operation is `sessions.send.lookup` with parameters
`{"sessionId":"configured-session","request":{"text":"original text","idempotencyKey":"original key"}}`.
`AppChatAdapter.send_lookup` supplies identity, target and conversation from
the trusted binding, requires one target, and verifies the returned command
and conversation IDs. The operation is advertised by `capabilities.get` and
uses the same session authorization policy as history/send. Unknown sessions,
expired authorization, invalid read requests and malformed/transport answers
are read failures, never definitive rejection of the original send. Host
error frames for lookup therefore carry `definitive:false`.

Send IPC error envelopes with `SUBMIT_OUTCOME_UNKNOWN`, `INVALID_JSON`,
`INVALID_RESPONSE`, `IPC_RESPONSE_TOO_LARGE` or `VERSION_MISMATCH` also remain
uncertain. The daemon client wrapper preserves the original error data.

Clients must retain the original request before sending, restore an
interrupted in-flight request as unknown, and reconcile with lookup. They
must not resubmit an unknown request or generate a replacement key. A
successful lookup of another key/session cannot resolve the original one.

The existing general `message.send` replay shortcut still identifies prior
receipts by deterministic message ID without comparing all command fields;
simultaneous replay can also precede settlement. Hardening that general
replay path is follow-up work. This lookup neither invokes nor changes it.

Validation: `tests/daemon/delivery/message/test_send_lookup.py` and
`tests/shell/gui/adapter/lookup/test_send_lookup.py` cover lost responses,
restart/outbox deletion, pending delivery, committed snapshot reads, caller
and payload isolation, malformed evidence, and uncertain error envelopes.
