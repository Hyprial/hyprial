# Send-links wire contract

Normative companion to `contract/wire/send-links.json` and the wire golden
cases WG-030..WG-037.  Source of truth is the design document
`docs/design/design-send-links-2026-10-14.md`, the working-package specs under
`docs/design/send-links-impl/`, and the integration rulings recorded in that
directory's `README.md` (cited as "ruling N").  Where this document and a later
ruling disagree, the ruling wins; the machine-readable half is
`send-links.json`, and `tests/tooling/contracts/test_send_links_contract.py`
(K1..K15) pins both to the source.

## 1. Link governance table (C27, rulings 25/37/45/51)

The link mechanism governs only canonical agent-to-agent traffic.  `sender` and
`recipient` are canonical `agent:<owner>:<node>:<name>` URIs; the decision is
`link_governed(sender, recipient, internal_actors)`, one implementation in
`daemon/impl/dispatch/admission.py`.

| Target / peer | Governed by links | Effect |
| --- | --- | --- |
| agent on any node | yes | must hold an effective mutual link; counts against k, cwnd and W |
| `user:<owner>` (owner channel) | no | never minted, never held, never blocked; still counts against the sender's W |
| `user:<other>` | no | never minted; delivery goes through squire/user-proxy or an adapter.  Unlike the owner channel a new request is refused synchronously (`LINK_WINDOW_FULL`, `detail: W`) when the sender's W is full (ruling 53) |
| `route:`, `adapter:`, `channel:` | no | adapter-owned delivery path |
| `system:` and daemon-internal actor names | no | exempt as sender or recipient on any node, keyed on the parsed actor segment |
| self-send | no | a node does not link to itself |

An unclosed request to any `user:` target still counts against the sender's W:
the person's attention is the budget, not a link slot (ruling 37).  The owner
channel is never blocked at that budget; a non-owner `user:` target is
(`LINK_WINDOW_FULL`, ruling 53).  A `user:` sender never passes the ingress
check (people have no link file, ruling 25).

## 2. Summary and body (C01, C07)

A message is split into a required `summary` (the only text a carrier injects)
and a body (the payload, read on demand).

- `summary`: string, at most `512` UTF-8 bytes (`SUMMARY_MAX_BYTES`).
  Appears on the zenoh delivery frame and in outbox, inbox and custody storage.
  Present on every agent-sent request, reply and event.  `ack-control` control
  frames carry no summary.
- Body: the message payload.  Never injected; read through
  `message.read_body` (or `hyprial message read`).  `bodyBytes` is the UTF-8 byte
  length of the stored payload.
- Derivation (ruling 30): a component relaying a person's words derives the
  summary.  Text at or under the limit is its own summary; longer text keeps a
  deterministic prefix plus the visible marker
  `…[全文 {size} 字节，用 read_body 读取]`.  One non-idempotent consequence is
  recorded in §14: local and remote `workflow-request` summaries can differ.
- `message.pending.list` row (C07): exactly
  `messageId, deliveryId, conversationId, from, to, intent, kind, summary,
  bodyBytes, origin`.  There is no `message` and no `topic` key; a row written
  before the hard cut reports `summary: null` and its body stays readable.

Error shape: a missing, blank, non-string or over-budget summary is
`SUMMARY_REQUIRED` before any side effect.  Over budget:
`data = {summaryBytes, maxBytes}` (ruling 16); missing: `reason == "missing"`.

## 3. IPC methods, parameters (C02, C05, C06, C33)

Transport is the existing daemon Unix-socket IPC.  Every method keeps the
`message.*` sender-authentication fence documented in
`contract/wire/message-sender-auth.md`: a claim is bound to the session that
owns the exact canonical actor.

`message.send` / `message.reply` (C02): add `summary` (string, required after
the hard cut).  `message.send` also accepts `replyTo` (non-empty string;
required to be a real reply, C08).  Both appear in the
`worker_channel.METHOD_PARAMS` allow-list.  A queued delivery keeps the
compatibility field `reason: "target-not-visible"` and adds
`resolutionReason`: `node-offline` when the target node has no presence,
`actor-unknown` when the node is present but the actor is not, or `no-route`
when both are present but direct delivery and custody failed.  Outbox rows add
`failedAttempts`, the precise name for failed network delivery invocations.
The compatibility field `attempts` has the same value; presence-held scans and
successful delivery do not increment either field.

`message.read_body` (C05): params `messageId` plus the same identity
parameters as `message.ack`.  Result `ok, messageId, conversationId, from, to,
intent, summary, message, bodyBytes, origin, pending, fetched`.  It is a
consuming action: it stamps `fetched`, retires the sender's receipt and
publishes a fetch receipt.  It does not close the request.  An id the caller
may not read returns `MESSAGE_BODY_UNAVAILABLE` with `data = {messageId}` and
never leaks whether the message exists (ruling 28).

`message.settle_hint` (C06): params `messageId` plus identity.  Result
`ok, messageId, hint, pending`, where `hint` is `ack` or `keep`.  A row that is
not a request (reply or event) answers `ack` (ruling 56).  A gate that answers
anything else yields `keep` and logs `message.settle_hint.invalid`.  Read-only:
it never acks and never stamps.

`session.turn.started` (C33): params `actor, sessionRef, reportId` (<=128
characters) and optional `deliveryId`.  Result `ok, startedAtMs`.

`session.turn.ended` (C33): params `actor, sessionRef, reportIds` (array of
1..1000 non-empty strings, each <=128 characters), optional `status`
(`completed` or `failed`) and optional `deliveryId`.  Result
`ok, endedAtMs, turnCount, turnCountSinceMs, lastTurnEndedAtMs`.  Both are
idempotent per `(actor, sessionRef, reportId)`; the daemon fixes the instant on
first acceptance.  A new generation for the same actor clears the old
`sessionRef` bookkeeping.

## 4. Send/reply results, holds, reply correlation (C08, C09, C11, C13, C14, C17, C18, C20, C28)

A send result carries the message `deliveries` and, conditionally:

- `queued: true` plus `holdReason` (and `detail`) when the message is held.
  Only `queued` and `holdReason` are promoted to the top level, and only for a
  single-target send; `detail` and `evicted` stay on the `deliveries` entry;
- `evicted` (C13): the peer a mint evicted; absent when none;
- `openRequestsFrom` (C20): per delivery, and at the top level only when
  non-empty.  The CLI prints a stderr hint when a `send` is not a reply but the
  sender still owes the target an answer.
- `replyTo` (C08, C09): a reply body carries the original message id; an agent
  target reply therefore has a different digest from the pre-link era.  An
  empty `replyTo` is `INVALID_ARGUMENT` (ruling 39).

Hold (C11): `ok: true, queued: true, holdReason, detail`.  The key is
`holdReason` (`HOLD_REASON_KEY`), never `reason` (ruling 15/X2).  The code set
is `HOLD_REASONS`: `LINK_MINTING, LINK_PEER_FULL, LINK_SELF_FULL,
LINK_WINDOW_FULL`.  Detail tokens and the codes they may accompany
(`send-links.json.holdDetails`):

| detail | codes |
| --- | --- |
| `minting` | `LINK_MINTING` |
| `awaiting-peer` | `LINK_MINTING`, `LINK_PEER_FULL` |
| `k` | `LINK_SELF_FULL` |
| `churn` | `LINK_SELF_FULL` |
| `org-unavailable` | `LINK_SELF_FULL` |
| `gate-unavailable` | `LINK_SELF_FULL` |
| `cwnd` | `LINK_WINDOW_FULL` |
| `W` | `LINK_WINDOW_FULL` |

A held message stays in the sender's outbox (`link_hold`) and is released by
the daemon; it expires observably at its TTL.  A hold is never `ok: false`.

`LINK_WINDOW_FULL` is deliberately two-sided (X1, ruling 76): for an agent
target it is a hold reason with detail `cwnd` or `W`; for a non-owner `user:`
target a new request whose sender W is full is refused synchronously —
`ok: false`, `code == LINK_WINDOW_FULL`, `detail == "W"`, nothing queued
(ruling 53).  Owner targets are never blocked.

Both W/cwnd bounds are governed by the daemon operator switch
`links.windowMode` (H1): `observe` (the default) **counts** a full cwnd or W,
delivers the message, and records one `link.window_observed` log line with
`actor, peer, bound, site, observedCount` plus an actor counter in
`link.stats.windowObserved`; `enforce` restores the whole holding table above
— including the synchronous `user:` refusal of ruling 53 — byte for byte.
The switch is read once at daemon start from `$HYPRIAL_HOME/settings.json`
(`{"links": {"windowMode": "observe"|"enforce"}}`); the environment
`HYPRIAL_LINKS_WINDOW_MODE` wins, a present unknown value refuses composition,
and `observe` applies when `windowMode` is unset or `null` -- a `links`
section carrying only `bypass` is valid.  No wire field changes with it: `observe`
simply never produces the `cwnd`/`W` hold nor the `user:` refusal, and a
persisted window hold from a previous `enforce` run is released.

Emergency switch `links.bypass` (env `HYPRIAL_LINKS_BYPASS`, values
`1/true/yes` on and `0/false/no` off).  Env wins over settings.json; an unset
or empty env falls through, and a missing/null setting defaults on.  Any other
value refuses composition.  On, every hold or refusal that rests on
link state alone admits: the send table (minting, peer/self full, churn, k,
org-unavailable, window), the queued-head hold, the `user:` W refusal, the
recipient's dispatch check and the ingress `LINK_UNKNOWN`.  Ingress then
accepts a remote agent sender only when its URI owner owns or is a member of
a link space this node reads; others keep `LINK_UNKNOWN`.  Sender identity
(`CALLER_NOT_AUTHORIZED`), reply validation (`MESSAGE_REPLY_UNAVAILABLE`,
`INVALID_ARGUMENT`) and `LINK_RECLAIMED` are unchanged.  Held rows are
released on the next pass.  Link bookkeeping is frozen: the links store
performs no link-file OrgFS writes (no write_text/mkdir/move/remove), pending
intents are dropped rather than queued, and eviction does not run.  Turning
the bypass off later rebuilds link state from scratch.  Service start logs one
`link.bookkeeping_disabled` event with `reason=bypass`; each bypassed decision
logs `link.bypass_admitted` with `actor, peer,
wouldHoldReason|wouldRefuseCode, direction` (`send`, `release`, `dispatch`,
`ingress`), at most once per (actor, peer, reason) per 60 s.
`link.service_composed`, `link.list` and `link.stats` carry `bypass`.

Gate unbound (C28, ruling 54): a new governed request is held at the sender
(`LINK_SELF_FULL`, detail `gate-unavailable`) and the recipient does not
dispatch it; ingress neither refuses nor fails because a gate is missing.

`message.reply` hold (C17): `ok: true, messageId, replyMessageId, replied:
false, acknowledged: false, queued: true, holdReason, detail`.  A hold whose
submit was not accepted keeps `ok: false` and carries both `code` and
`holdReason`.  A replayed receipt (C18) omits `code`, `reason` and
`openRequestsFrom`; the difference is accepted and covered by the unit tests,
not by a wire case.

## 5. Error codes (C03, C04, C14, C15, C16)

`send-links.json.errorCodes` is frozen in `kernel.ipc_errors.ALL_CODES`:
`SUMMARY_REQUIRED`, `MESSAGE_BODY_UNAVAILABLE`, the four hold reasons,
`LINK_UNKNOWN`, `LINK_RECLAIMED`.

- `SUMMARY_REQUIRED` (C03): refused before any side effect.
  `data = {summaryBytes, maxBytes}` when over budget, else
  `field/reason/maxBytes`.
- `MESSAGE_BODY_UNAVAILABLE` (C04): `data = {messageId}`; says nothing about
  whether the message exists.
- `LINK_UNKNOWN` (C15, ruling 4.6): the receiving node's local orgfs copy holds
  no link file for the sender at all.  Refused at ingress: delivery status
  `state = "refused"`, `ReceiveResult.code = "LINK_UNKNOWN"`,
  `ConsumptionState.refused`; an older node that cannot decode `refused`
  counts it as `undecodable`.  It never self-heals.
- `LINK_RECLAIMED` (C16, ruling 8): a reply to a request whose link was
  reclaimed and whose residue was drained; otherwise the reply keeps
  `MESSAGE_REPLY_UNAVAILABLE`.
- `MESSAGE_REPLY_UNAVAILABLE`: the original request is already closed or does
  not exist.  Ack this side's row; to keep talking, send a new message (it
  counts against W).

## 6. link / outbox / top views (C19, C21, C22, C25)

`link.list` (C21): params `from`; result `ok, actor, orgAvailable, bypass,
bookkeeping, k, W, inFlight, owner, links, held`.  `bookkeeping: false` means
the bypass has frozen link-file writes; `link add` then reports
`state: "not-recorded"`, `reason: "bookkeeping-disabled-by-bypass"` instead of
a false `established`.  Each `links` entry reports `peer, state,
cwnd, inFlight, lifecycle, taskId, taskIds, mintedAtMs, lastUsedAtMs,
revision, mine, theirs, committed`.

`link.stats` (C21): params `from`; result `ok, actor, nActive, k, W, wTarget,
tMinMs, tMaxMs, tBarMs, muMax, diff, grad, periodMs, windowObserved, bypass,
links`.
`windowObserved` (H1) is how often `observe` mode skipped a full W/cwnd for
this actor (send or dispatch); it is `0` under `enforce`.

`link.add` (C21): params `from, peer`; result `ok, actor, peer, state` plus the
branch keys (`revision`, `committed`, `written`, `queued`, `holdReason`,
`detail`, `reason`) that the branch constructs; with bookkeeping frozen the
state is `not-recorded` and `reason=bookkeeping-disabled-by-bypass`.

`link.drop` (C22): params `from, peer`; result
`{state: "reclaiming", written: false}` when a link is reclaimed, and
`{state: "absent"}` when there was nothing of this node's to drop.  There is no
`drained` count; the count moves to the `link.reclaimed` event (ruling X3).

`outbox list` (C19): each row carries `holdReason` for a held message.

`top.snapshot` (C25): each actor row gains `capacityW`, `outboundInFlight`
(null while unwired) and `inboundAwaitingInjection`.  The human `hyprial agent top`
table renders `W`, `OUT`, `INJQ`, with `—` when the actor has no value.

## 7. Events (C23, C24, C34, C38)

Hook events (C23, `ports.EVENT_LINK_*`, kernel `EVENT_NAMES`): `link.minted`
and `link.reclaimed`.  They do not appear in `state/logs/*.jsonl`, so
`hyprial log` never shows them; a unit test with a stub publisher pins their
publication and E2E asserts only the visible consequences (mutual files,
`link list` state).  Their payloads are distinct:

Both payloads carry the emitting `actor`.  Beyond it:

- `link.minted`: `atMs, lifecycle, mintedAtMs, peer, revision, side, taskId`
  (`taskId` is null for a regular link; `side` is `minter` or `acceptor`).
- `link.reclaimed`: `atMs, cancelled, drained, drainedMessageIds, lifecycle,
  peer, reason, released, taskId`; a reclaim has no `revision` and no `side`
  (`drainedMessageIds` is truncated to a bounded prefix).

Log events (C24): the `link.*` set is closed both ways by K8 —
`link.apply_failed, link.attach.summary, link.bookkeeping_disabled, link.bypass_admitted, link.drain_failed, link.event_failed, link.file_conflict, link.flush_skipped,
link.file_rejected, link.gate_error, link.gate_unbound,
link.inbound_row_ambiguous, link.intent_dropped, link.intent_rejected,
link.lane_failed, link.local_agents_failed, link.org_available,
link.org_unavailable, link.pac_bind_failed, link.pac_unreadable,
link.service_composed, link.service_failed, link.service_skipped,
link.space_ambiguous, link.space_skipped, link.space_unwritable,
link.spaces_changed, link.subscriber_failed, link.tick_failed,
link.window_observed`. `link.space_skipped` carries `org` and one typed
`reason` from `orgSpaceSkipReasons`; unreadable state adds `errorType`, while a
malformed marker adds `dataReason`. `link.service_composed` and
`link.spaces_changed` carry `skippedOrgSpaces`. The `link.service_*` events carry
`stage` ∈ the closed `serviceStages` set (`orgfs, construct, start, binder,
describe, bind, callback`): `link.service_failed` can report every value up to
`bind`, and `link.pac_bind_failed` reports `bind` or `callback`.  The
`read_body` stage on `link.gate_error` is not a service stage.
`link.e2e_permissive_gate` is retired.

Carrier events (C34, C38): `turn.started`, `turn.completed`, `turn.failed` are
also published for interactive carriers, with source `daemon.turns`,
`fidelity: observed` and empty sender/conversation/prompt/output; all `turn.*`
events share one critical section for `seq`.  `harness.settle.deferred` and
`harness.mechanism_answer.deferred` replace `harness.reply.deferred`;
`harness.reply.already_answered` is deleted; `harness.stranded_sweep_deferred`
is added.

## 8. Carrier contract (C35, C37, C39..C44)

Carriers never answer on their own.  The headless prefix
`hyprial-agent-sdk-reply:` is gone.  Exactly two mechanisms answer: the id
prefix `hyprial-turn-hook-reply:` and `hyprial-mechanism-reply:`; their payload
carries `replyTo` and `origin.mechanism` (`turn-hook` or `python_worker`) and a
summary.  User-proxy format hints continue through the adapter's
`deliver_reply`.  `python_worker` (with user-proxy) relays the original text
(ruling 60); it is a mechanism carrier, and it is the one place "summary only"
does not apply.

Injection (C37): version `summary-v1`, golden
`tests/daemon/harnesses/carrier/injection_golden.json` (`"format":
"summary-v1"`).  Only `summary` plus routing metadata enters the model
context.

**Superseded for model edges (2026-10-22):** `summary-v1` is now the historical
persisted format only.  The model-facing injection is one daemon-rendered
`inbox-batch-v1` batch per injection point, with full bodies inline up to the
inline budget (or one private spill file).  See `contract/wire/inbox-batch-v1.md`.
`summary-v1` rows remain readable and are never re-rendered by a connector.
The mechanism/Python-worker frames above stay scalar, as stated.

Startup settlement scan (C39, ruling 74): at daemon start, already-stamped
pending rows with no terminal failure record are passed once through
`settle_hint_for`; `ack` is acked, `keep` is untouched, the model is never
re-run, and the bound is 256 rows.  The scan covers only the carrier harnesses
the daemon tracks as streaming or headless (their actor URIs); rows for other
actors are not swept.

Reminder (C40): after `turn.completed`, a request injected in that turn and
still pending yields a system notice
`"{N} 条请求未回复：…；回复用 reply，无需回复用 ack"` with payload
`openRequests`.

Per-carrier landing:

| carrier | contract |
| --- | --- |
| claude (C41) | startup settings gain a `UserPromptSubmit` hook; the channel sends `session.turn.started` (with a `harness_read` catch-up) and `session.turn.ended` with `status: failed` on failure; at turn end `message.settle_hint` then `message.ack`. |
| codex (C42) | injection is summary-only (`injection_format = summary-v1`), KEPT state, internal `codex_turn_report` table; log event `worker.carrier.settlement.reply_lost` deleted. |
| codex interactive (C44) | harness-bridge MCP mounted; `capabilities.py` declares `PROACTIVE_SEND` and `TOOL_INJECTION` native. |
| pi attach (C43) | summary-v1 injection, `session.turn.*` reporting, end-of-turn `settle_hint`; prompts no longer promise an automatic reply. |
| interactive codex approval (C49, ruling 77) | started with `-a on-request` (a person is present; tool approval goes to the TUI user).  `-a never` is headless-only and makes codex refuse every MCP call; the native declaration assumes `on-request`. |

## 9. CLI surface (C46)

`hyprial message send`/`hyprial message reply` take `--summary`; a missing or over-budget
summary is refused locally with `SUMMARY_REQUIRED` (exit 1, `--json` carries
`data.summaryBytes/maxBytes`) and no message is sent.  `hyprial message read <id>` is
the new body reader.  The `hyprial link list|stats|add|drop` group mirrors
§6.  `hyprial agent top` renders `W`, `OUT`, `INJQ`.  All of this is exercised by
`contract/cli-blackbox/`.

## 10. Reserved names (C26, ruling 63)

`dispatch-coordinator` and `_turn-hooks` are daemon-internal actor names.
Registration (`agent.create`, `session.register`/`register_guarded`,
host-invite, transfer-hosted) refuses them with the existing
`INVALID_AGENT_NAME` and the text
`is reserved for a daemon internal actor`.  The match is exact on the actor
segment, independent of owner and node.  A look-alike such as
`dispatch-coordinator2` is an ordinary agent.

## 11. PAC (C31, C32)

PAC notices, routine tasks and remote offers are delivered through the same
paths and now carry a `summary`; `InboxDeliveryIoAdapter.deliver` requires it.
The summary is not guaranteed to agree across nodes (X5, ruling 76): a local
`workflow-request` summary is derived from the stored event text before the
task is rebuilt, a remote one from the rebuilt full text.  Whether to unify
them is pending an Allen ruling (§14, X12).

## 12. orgfs link file format (C30)

Directory `link/` (constant `LINK_DIR`) under each joined org space (default a
in ruling 50; still pending, see §14).  One file per canonical actor URI:
`link/<canonical actor uri>.json`.  If concurrent offline creation leaves
multiple root-level directories named `link`, readers include every sibling
and writers use only the sibling with the lexicographically smallest stable
node id.  Directory identity, not replica arrival order, determines that
choice; the name conflict alone does not make the org unavailable.  Write
access is re-probed on every link refresh, so a transiently unwritable space
can recover without a daemon restart.

Only the daemon that hosts the agent writes its file: an actor is local to the
node whose registry holds it and whose node segment equals that node.  The
owner segment never decides the writer, so a hosted agent (a visitor's owner,
this node) is written by this node's daemon, not by another daemon that merely
shares the visitor's owner.  Content: the list of accepted peers, a revision
number and a lifecycle marker.  A link is effective only when both sides'
files reference each other; each side is written by its own daemon.  With
several joined org spaces the file is mirrored into each (ruling 50 default
a).

## 13. Retired names

`send-links.json.retired` and `.retiredPromptPhrases` are the normative list:
`hyprial-agent-sdk-reply:`, `harness.reply.deferred`,
`harness.reply.already_answered`, `worker.carrier.settlement.reply_lost`,
`link.e2e_permissive_gate`, and the prompt phrases `sends your final reply
back` / `your final reply is sent back`.  The frozen TS `autoReply` field in
the wire golden was renamed `replyOwed`; the renamed and new cases carry
`hyprialOnly: true` and cite the design document, a deliberate departure from
the frozen TS reference (X6, ruling 76).

## 14. Known limits

- `LINK_PEER_FULL` is an estimate in v1: it means the peer's regular link count
  is at least this node's k; an exact answer needs a file-format change (C29,
  ruling 48).
- The startup settlement scan is bounded at 256 rows; 256 or more kept rows can
  delay later reply rows until TTL.
- A headless row is stamped at the end of the turn, so the inbound ledger and
  the injection sink happen on completion, not at injection.
- `link.drop` that crashes between draining the ack and writing the link file
  loses the retraction; the link returns after restart.
- Local and remote `workflow-request` summaries are not guaranteed to agree
  (§11, X5).
- Several org spaces: mirroring (ruling 50 a) is the current default, still
  pending an Allen ruling (X12).  Also pending: ruling 7 (a PAC task-bound link
  does not count against k), wp2-composition G7 (after an owner change the old
  link stops being effective and the new owner re-mints), and whether to unify
  local/remote summaries.

Contract-Impact: this document plus `send-links.json` are the contract half of
WP8; the 49 collected items (C01..C49) map to the sections above and to the
golden cases WG-030..WG-037.
