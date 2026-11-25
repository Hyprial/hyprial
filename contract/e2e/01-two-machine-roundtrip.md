# E2E-001: two-machine send, reply, and ack

Status: executable Docker two-container automation; the attended two-host run
remains `[REAL ENV]`.

`run-two-machine-roundtrip.py` builds and drives a real Docker mesh. Each
daemon has its own network namespace and IP on an internal bridge. A fifth,
owned container runs the private DERP/STUN labrelay; no port is published and
the bridge has no external route. Real Tailcat traffic crosses containers.
Before launching any daemon, the runner verifies the relay's network and
container inspection: the labelled internal bridge, matching network ID,
running labelled relay, single network attachment, and its RFC1918 IPv4 in
the bridge's IPAM subnet must agree. Only that inspected IPv4 is given to
the sidecar's DERP/STUN override. The current override requires a numeric
IPv4; a Docker DNS name disables its DERP dial families. The relay's actual
dynamic DERP/STUN ports are retained.
The image includes Git because agent creation discovers the workspace through
Git. The runner records each created driver's official `a.<id>` address and
keeps it across container stop/start phases; it does not reconstruct an address
from the agent's display name. The image compiles the current-source Linux
sidecar and labrelay with the pinned Go toolchain and verifies the sidecar
protocol/engine pin. It never copies the macOS host's executable.

On a runner with TLS interception, set `HYPRIAL_E2E_DOCKER_CA_BUNDLE` to a
readable public CA bundle outside the checkout. Docker mounts it as a BuildKit
secret for package and Go dependency installation, with TLS verification enabled. The bundle
is not copied into the image or included in the source-input identity; it only
provides build-time trust. Without that setting the normal platform trust
behavior is retained.

Scouting, gossip and usage fetch are explicitly disabled. Each entrypoint
uses the public device-key initializer in its disposable home, then runs the
daemon as PID 1. The launcher supplies a private Unix Zenoh backend, the
pinned sidecar and private relay on every boot. The creator uses normal
`org create`, and each additional node uses a fresh public invite/join;
invite bearers travel only on stdin. The endpoint proof requires the private
Unix backend and the reported sidecar slots to be dialed, rejecting direct
TCP and foreign filesystem endpoints. This covers the current managed
Tailcat contract while retaining actual cross-namespace traffic.

The test image removes its inherited `/etc/machine-id` template. Before the
first public device-key call, each container creates its own valid, nonzero
OS machine-id in its writable layer; normal `docker stop`/`docker start`
preserves it. Existing valid IDs remain byte-for-byte unchanged; nonempty
invalid IDs and symlinks fail explicitly. Nodes never share a generated ID.
This models one stable test machine per container while retaining the
product's copied-home key rotation and normal membership checks.

Join failures report bounded public error/code and per-step identity/error/code.
Invite URLs, fragments, decoded bearers and private address credentials are
removed; raw command stdout/stderr and other response fields are not echoed.

The runner covers:

1. discovery plus bidirectional send/reply/ack across containers, with the
   request addressed to the advertised `targetUri` verbatim (the `agent:`
   display form) so the copy-paste path is exercised end to end;
2. offline queueing: stop B, A sends, start B, and the message is delivered
   from A's durable outbox and receipted — cross-machine inbox semantics that
   loopback cannot exercise;
3. restart self-heal: stop/start A, the mesh re-establishes, a message B
   queued inside the restart window is delivered, and a second roundtrip
   succeeds;
4. suspend/resume: `docker pause` freezes B while its network namespace and
   sockets remain present — the laptop-lid-close shape stop/start cannot reproduce —
   and A's queued message is delivered exactly once after `unpause`;
5. mesh partition heal: `docker network disconnect` drops the link while
   both containers keep running (real network flapping, not a process
   stop); reconnecting rebuilds presence in both directions, flushes the
   backlog exactly once, and a fresh roundtrip succeeds;
6. delivery terminal state across containers: every message ends in one
   durable, after-the-fact verdict — `fetched` (the recipient took it) or
   `expired` (a holder evicted it) — and that verdict is pulled both by the
   sender and by a container that never held the message. When the
   recipient's node never answers that pull, the verdict remains `expired`;
   the missing recipient holder is reported only under diagnostics (6e).

Every recovery phase asserts the same floor: no loss (the receiver's inbox
persists the message), no duplication (dedup holds and no copy resurfaces
after ack), and the sender's outbox drains (the receipt path completes).
The offline-phase diagnostic uses the public read-only command
`hyprial message outbox list --json`; it does not prune or resend a queued message.

## Phase 6: durable delivery terminal state

The delivery-semantics rework replaced "`message.send` returned `accepted`"
with a persisted verdict the sender can pull long afterwards, over
`hyprial message status --from <actor> --message-id <id> --json` (daemon method
`message.status`). The method is now catalog-bound for IPC validation; its
projection and Phase 6 exercise remain unchanged. Phase 6 is the only place
that contract is exercised
between separate network namespaces; everything else about it was verified in
a single process over loopback Zenoh.

A third daemon `hyprial-e2e001-c` joins the same bridge and org through the
normal managed invite/join for this phase only. Its roster supplies sidecar
slots to both existing peers, and it is the only daemon given a short hold TTL
(`HYPRIAL_HOLD_TTL_MS=25000`), so a TTL eviction is observable in seconds while A
and B keep the product's default hold TTL for their outbox phases. It runs last,
after phase 5, so it cannot perturb anything above it.

What each sub-phase is entitled to claim:

- **6a — sender pulls `fetched`.** A sends to B, B commits it, A asks for the
  verdict and gets `fetched` with a full record (message id, sender, holder,
  recording time), not merely a non-error reply.
- **6b — the verdict outlives everything.** The same question about the
  *phase 2* message: queued while B was down, delivered on B's return, and
  asked about only now, after A itself was stopped and restarted in phase 3.
- **6c — `fetched` crosses the container boundary.** Container C pulls the
  verdict for a message between A and B. C never held that message and has no
  record of it, so the answer can only have arrived over the mesh. **This is
  the assertion 6a and 6b cannot make**: a sender mirrors `fetched` into its
  own ledger the moment a receipt confirms
  (`InboxService._attempt_direct`), so a sender asking about its own
  delivered message is answered locally and would still go green with the
  cross-host query leg completely broken.
- **6d — `expired` is real and crosses the boundary.** C sends to an actor
  nobody ever registers on node B — undeliverable, because the actor is
  nobody's, but its node exists and answers — so the message can only end by
  eviction. Before the TTL it reads `pending` with a hold deadline (proving
  `pending` and `unknown` are distinguishable from a verdict); after the TTL
  the holder evicts it unprompted and records `expired` / `TTL_EXPIRED`;
  and A — which never held it — pulls that same verdict, with `holder` still
  naming C. The recipient's node also answers the status query (an empty,
  self-named frame), which is retained as a response-health diagnostic.
- **6e — an absent recipient is `expired`.**
  C sends to an actor on a node that never exists; C still evicts it on its
  own TTL and records `expired` / `TTL_EXPIRED`, but no node will ever
  answer for that recipient. The pull still reports `state=expired` with
  `reason=TTL_EXPIRED`, both on the holder and across the container
  boundary, with `holder` still naming C. The absent recipient remains visible
  under diagnostics as `missingRecipientHolders`; response health never rewrites
  a persisted terminal outcome.

Both defects that measurement exposed here are now fixed, and the phase
asserts the fixed behaviour instead of documenting the flapping.

**The off-node pull collected an incomplete set of holders.** Not because of
the decode guard, and not on the leg that was suspected. Zenoh's default
reply consolidation (`AUTO`, which resolves to `Latest` for a concrete key)
keeps replies in a map **keyed by key expression** — and every queryable
replies under the key that was queried. So N holders answering one status
query collapse to exactly one surviving reply, chosen by arrival race, with
no error and nothing reported missing. Measured before the fix, with the
answering side instrumented: A and B both logged `message.status.served
records=1 answered=True` for the same query, and C reported
`meshReplies=1 meshHolders=['…-a'] undecodable=0 noKnownLoss=True`. The
record that went missing at C was **B's**, not A's as previously assumed.
`query_delivery_status` now queries with consolidation off, and 6c asserts
`meshHolders` contains both holders — an assertion the old code cannot pass.

**The authoritative record lost the merge to a mirror.** A delivered message
ends with two `fetched` / `ACK_RECEIVED` records: the recipient's direct
observation and the sender's receipt mirror. `merge_delivery_status` ranked `fetched`
records earliest-first on the stated assumption that a mirror "can only be
later"; `InboxService.submit` stamped the mirror from a clock read taken
*before* the blocking delivery, so it was reliably earlier and the hearsay
copy won. Measured, same message: mirror `@…747`, authoritative `@…751`. Four
milliseconds — well inside ordinary clock skew between two hosts, so
reversing the assumption would only have made the bug rarer. The two records
are written on two different machines, so ranking them by timestamp is the
comparison the design forbids everywhere else (section 8.3, and `HoldPolicy`
on foreign clocks). Precedence is now by evidence class — recipient's own
observation beats a mirror beats an eviction — with timestamps demoted to a
tie-break *within* a class. The clock read was corrected too, so a record no
longer claims to predate the event it records, but that is cleanup: the
ranking no longer depends on it.

6a, 6b and 6c therefore pin `holder` and `reason` exactly, and every phase
asserts `noKnownLoss` — no reply was dropped on the way into the verdict.

## Phase 7 — mailbox custody (`as_mailbox`)

Design section 4 (decision G1): with a mailbox present, the mailbox **takes**
an undeliverable message — ownership transfer, not a copy — and without one
the sending daemon keeps it. Phases 2-5 cover the second half. Nothing
covered the first, because no daemon ever set `as_mailbox`; doing so turned
out to be impossible in practice, which is the third defect this branch
fixes: the role declared a liveliness token and nothing else, so senders saw
a mailbox and published custody to it while no node subscribed to
`custody/<node>`. The custody receipt never came back, transfer reported
failure, and the sender silently kept the message. A fourth container, D,
now runs with `HYPRIAL_AS_MAILBOX=1` and a short hold TTL. Phase 7 runs last,
because once a mailbox exists every send to an offline recipient takes the
custody path rather than the sender-holds-it path the earlier phases assert.

- **7a — ownership really transfers.** B is stopped before the send. Within
  the original shared 90s budget, A must no longer report B as `online` or
  advertise any canonical agent target on B, and a read-only inspection of
  this test's B container must show `Running=false`, `Status=exited`. A
  retained `unknown` host row remains unknown; its disappearance is not a
  requirement, nor is that row alone evidence of physical stop. This reaches
  the custody path deliberately. After the transfer A holds the message in
  **neither** of the two places a message can be held
  (`outboxCount == 0 and custodyCount == 0`),
  D holds it, and D reports it as `pending` with a hold deadline — the marker
  of "this node is the holder". A, having given it away, reports `unknown`.
- **7b — `fetched` through a mailbox.** B returns, D delivers the custody it
  was holding, and D releases it. The merged verdict names **B**, not D: B
  observed possession first-hand and D only mirrored a receipt, so ranking D
  above B would mean promoting hearsay over direct observation — the defect
  6a fixes. D's record is asserted where it belongs, in the holder set
  (`meshHolders` contains both D and B).
- **7c — a non-sender, non-recipient holder records `expired`, and the sender
  pulls it.** A message addressed to the actor nobody registers on an online
  node (B, back up after 7b) can only end by eviction, and because that node
  answers status queries the eviction stays a certain `expired` under the
  #519 projection. D evicts it on D's own TTL and records
  `expired` / `TTL_EXPIRED` / `holder=D`; A pulls exactly that record
  across containers, with `localRecords == 0` proving A answered from the
  mesh and not from a copy it kept. This is the shape PR #93 named as
  unreachable without a mailbox node.

D's hold TTL is 60s rather than phase 6's 25s, and 7b runs before 7c's wait,
for the same reason: `custody_ttl_ms` is the same knob as the hold TTL, so
the mailbox is counting down on the message it is supposed to *deliver* too.
7b has to finish — including B's entire container restart — before that clock
expires, while 7c's message has nothing to do but wait it out.

Single-holdership is asserted where it is checkable — the transfer settles
with exactly one holder — and **not** claimed instantaneously; see the
"Not covered" list.

Not covered, deliberately:

- **That a pull collects *every* holder's record.** 6c and 7c assert that the
  holders known to exist are all present, and the diagnostics count every
  known way a reply can be lost. Neither proves completeness, and nothing
  can: how many holders exist on a mesh is not knowable, which is why the
  record is persistent and re-pullable rather than pushed once. `noKnownLoss`
  is exactly what it says — nothing that answered was discarded — not a
  completeness claim.
- **That exactly one holder exists at every instant.** `submit` commits the
  outbox row, transfers custody, then deletes the row, so a window exists in
  which both rows are on disk, and no external observer can sample inside it.
  A crash inside that window leaves a permanent double hold that nothing
  reconciles. 7a asserts the transfer *settles* with one holder, which is the
  checkable statement.
- **That a sender can tell "a mailbox is holding it" from "nobody knows".**
  `held_expiry_ms` is a local-only lookup, so after custody transfers the
  sender reads `unknown` — the same answer it would give if no holder were
  reachable at all. 7a asserts this as observed behaviour, not as desirable.
- **A sender that is offline at the instant its message terminates.** Reaching
  `fetched` requires the sender's own delivery pump to run, which writes the
  mirror in the same breath. 6b covers the adjacent, reachable claim: the
  verdict is still pullable long after the fact and across a sender restart.
- **`--local` mode.** The loopback fallback cannot give a third daemon its own
  hold TTL, so phase 6 is Docker-mode only.

Real deployment steps (endpoints, firewall) are documented in
`docs/guides/zenoh-two-machine.md`.

All lines marked `[REAL ENV]` require two Tailnet hosts, network transport,
and dedicated test actors. Never point this scenario at production actor
names or state directories.

## Preconditions

- Docker available on the runner host (OrbStack/Desktop/daemon; Linux, arm64
  or amd64). First run pulls `python:3.12-slim` and builds `hyprial-e2e001:local`;
  later runs reuse the image. All runner resources use the fixed `hyprial-e2e001-*`
  prefix; cleanup is guaranteed (pre-clean + try/finally + SIGINT/SIGTERM
  handlers + post-run self-check), and failure keeps only evidence files, never
  containers. Manual fallback:
  `docker rm -f $(docker ps -aq --filter name=hyprial-e2e001)`.
- `[REAL ENV]` Host A and Host B can discover each other through the
  configured Harness transport (explicit `listen`/`connect`; multicast and
  gossip are off).
- `[REAL ENV]` Both hosts use dedicated test `HYPRIAL_HOME` and
  `HARNESS_STATE_DIR` values and test-owned daemon processes.
- Host A owns `agent:contract-a:host-a:sender`; Host B owns
  `agent:contract-b:host-b:receiver`.
- Each daemon starts with explicit endpoints; endpoints persist in desired
  state across restarts.

## Steps

1. Start both daemons with explicit endpoints and register the two test
   actors.
2. From Host A, query `hyprial message targets --json` and resolve Host B by
   its canonical actor URI.
3. Send `CONTRACT_E2E_REQUEST_<nonce>` from A to B and retain the
   receipt's `operationId`, `messageId`, and `conversationId`.
4. On B, observe exactly one pending `intent=request` delivery and
   publish `CONTRACT_E2E_REPLY_<nonce>` through the structured reply surface.
5. On A, observe one `intent=reply` delivery whose `replyTo` equals
   the request `messageId`, then acknowledge it without another reply.
6. Query both pending views and the isolated structured logs.
7. Offline queueing: stop B, send from A (durably queued in A's outbox),
   start B, and wait for delivery plus receipt (A's outbox drains).
8. Restart self-heal: stop and start A with a plain start, then repeat steps
   2-6 to prove endpoints survive in the launcher environment across a
   restart.
9. Delivery terminal state: start a third container with a short hold TTL,
   wait for presence in both directions, send one message to an actor nobody
   registers on an online node and one to an actor on a node that never
   exists, then ask for the terminal verdict of (a) a fresh A→B delivery,
   (b) the step-7 offline-queued message, (c) the same A→B delivery but from
   the third container, (d) the undeliverable-to-an-online-node message — a
   certain `expired` — both on its holder and from a container that never
   held it, and (e) the undeliverable-to-an-absent-node message — also
   `expired`, with the absent holder retained in diagnostics — on both legs.
10. `[REAL ENV]` Stop and restart both daemons, then repeat steps 2-8 to prove
    the same contract on physical hosts.

## Assertions

- `hyprial daemon status --json` reports the configured `zenoh.listen`/`zenoh.connect`.
- Target discovery returns the canonical Host B actor and no guessed route.
- Initial send is durably accepted before provider completion.
- B sees the exact request once; the reply preserves `conversationId` and sets
  `replyTo` to the original `messageId`.
- A's ack-only action removes the pending reply and publishes no new envelope.
- Re-reading both inboxes produces no automatic reply loop.
- Offline send is accepted and queued; the message is delivered exactly once
  after the recipient restarts, and the sender's outbox drains on receipt.
- After a plain restart, discovery and a second roundtrip succeed from desired
  state alone (no environment variables, no init flags).
- A delivered message's terminal state is `fetched`, returned as a complete
  record (matching `messageId`/`sender`, a named `holder`, a recording time) —
  never a bare non-error reply.
- The same verdict is still pullable after the sender has been restarted and
  four later phases have run.
- A container that never held the message pulls the same `fetched` verdict,
  proving the verdict crosses network namespaces rather than being answered
  from the asker's own ledger.
- An undeliverable message reads `pending` with a hold deadline until its
  holder's TTL fires, then `expired` with reason `TTL_EXPIRED` and the
  holder still named — recorded without any operator command. When the
  recipient's node is online and answers the status query (the actor was
  never registered; the node was), the recipient's node appears in the
  responder diagnostics.
- That `expired` verdict is pulled from a different container, which holds no
  record of the message.
- A message whose recipient node never answers still reads `state=expired`
  and `reason=TTL_EXPIRED`, with the terminal holder named. The absent
  recipient is listed in `diagnostics.missingRecipientHolders` on both the
  holder and cross-boundary query legs.
- Evidence contains sanitized JSON receipts and correlation-only log rows; it
  contains no credentials or unrelated message bodies.

## send-links 硬切：组织空间前置（WP8 返工 2-E，2026-10-15）

硬切后 agent→agent 首发需要同一可写组织空间；E2E-001 的 Docker A/B（以及 phase6/7 的 C/D）在发送前由 A `org create group-e2e001-<ts>`、邀请对方 owner 并 `fs join`，等待 `link list` 的 `orgAvailable=true` 后才进 phase1。实现见 _two_machine/phases.py 的 create_org_space/invite_org_owner/join_org_space 与 image.py 的 run_docker；--local 回退同样先建/加入空间。实测：phase0 orgAvailable=true、phase1 roundtrip PASS；phase2 起为另一失败前沿（离线投递），不属 org 前置。
