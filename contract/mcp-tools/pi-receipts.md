# Pi Harness receipts and reply settlement

The headless Pi bridge and interactive Pi attachment share version-1 daemon
IPC. Target kinds (agent, user and route), authenticated identity, durable
custody and native delivery remain daemon-owned. Pi does not retry a failed
send or change its destination on behalf of the model.

## Tool results

- Successful application results retain their complete object in details and
  JSON model-visible text, including delivery/native-message identifiers.
- An application result with ok:false sets the Pi tool result isError:true
  without discarding the original receipt. This flag does not prove that no
  delivery occurred: queued/unknown and partial results retain their meaning.
- An IPC error with structured data preserves code and data in details and
  model-visible text. The existing error string remains available. In
  particular SUBMIT_OUTCOME_UNKNOWN must retain commandAdmission,
  outcomeKnown, messageId, submissionCorrelationId and idempotencyKey when
  the daemon supplies them. Missing data is not invented.
- IPC errors keep their CODE: message prefix for existing session-fence and
  retry classification. Transport success is not business success.
- Sends and replies carry a required `summary`; `harness_read` returns the
  daemon's complete `message.pending.batch` / `inbox-batch-v1` response with
  full bodies inline or one spill-file reference. Single-ID recovery remains
  available through `harness_read_body` / `message.read_body`. A first send to
  an agent without a link is held (`ok:true, queued:true, holdReason`); a send
  is never a reply. See `../wire/send-links.md` §2, §4 and §8 and
  `../wire/inbox-batch-v1.md`.

## Interactive completion

A resolved message.reply/message.ack promise alone is insufficient to release
the pending final. Completion requires ok:true, or an already-submitted
receipt explicitly confirming acknowledged:true and (for replies) replied:true.
REPLY_SETTLEMENT_PENDING and other unconfirmed receipts retain the same final
and join the same deterministic daemon receipt on subsequent settlement attempts.
Completed members of a batch are not submitted again.

MESSAGE_REPLY_UNAVAILABLE can mean a missing original or an unavailable reply
path for a still-pending original. An observation-only pending listing must
distinguish those states. A failed or malformed observation is not completion.
An absent original may retire local work; that is not proof of platform delivery
or user readership. No probe may fetch or ACK the original as a side effect.

The bounded never-consumed injection failure retains its honest failure body
on the same settlement lane until confirmed, rather than dropping it on a
temporary native reply deferral.

## Final assistant text

An `agent_end` observation is provisional. RPC silence only triggers a
correlated `get_state` probe; it cannot authorize a reply or failure notice.
Completion requires `agent_settled`, or a successful native state response
with `isStreaming:false`, `isCompacting:false` and integer
`pendingMessageCount:0`. Busy, missing, malformed or unconfirmed state keeps
the turn open. A continuation invalidates an outstanding idle probe. The
same RPC reader processes events and probe/steer responses without dropping
intervening events. This fallback is for Pi versions whose state fields have
these run-level semantics; unsupported state shapes cannot prove settlement.

Only the actual last assistant message may supply final text. An empty,
thinking-only, failed/aborted or whitespace-only final must not reuse an older
assistant's commentary. Nonempty text is preserved verbatim. For older Pi
versions that omit assistant messages from agent_end, the session-text fallback
remains available, but a whitespace-only fallback is not a successful answer.
An explicitly observed empty current final cannot use that fallback.

An interactive empty/failed final uses the existing honest failure notice;
the headless path reports failure. Lark's intentional Agent/Replied-At/Reply-To
headers do not change these body requirements or prove notification/readership.

## Executable coverage

- tests/daemon/harnesses/pi/extension/test_pi_harness_bridge_extension.py
- tests/daemon/harnesses/pi/extension/test_pi_reply_settlement.py
- tests/daemon/harnesses/pi/rpc/test_pi_final_text.py

These run the shipped Pi resources over isolated Unix IPC and the headless
RPC pump over a fake provider. They are not live platform or client-UI proof.
