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

`conversationId` is the id the original message was stored under. A send that
passed none was stored under the default conversation of its sender and
target (a `dc.` id, see `contract/daemon-request/README.md`); that id is the
one its `message.send` response returned and is the value to look up with.
Before 2026-11-17 such a send was stored under a random id that only its
response carried.

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

Expired authorization, invalid read requests and malformed or transport
answers are read failures, never a definitive rejection of the original send.

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

Validation: `tests/daemon/delivery/message/test_send_lookup.py` covers lost responses,
restart/outbox deletion, pending delivery, committed snapshot reads, caller
and payload isolation and malformed evidence;
`tests/shell/gui/adapter/lookup/test_public_daemon_client.py` covers that the
client wrapper keeps the data of an unknown result.
