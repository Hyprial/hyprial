# E2E-013: all-harness lifecycle roundtrip with mid-life harness and model switch

Status: executable via `run-all-harness-lifecycle.py` with real Claude, Pi,
and Codex logins; the Lark leg additionally needs the dedicated test App.

E2E-006 proves the three residents launch and roundtrip; E2E-012 is the
dedicated Codex gate. This scenario is the *agent-entity* gate across all
three harnesses: one full lifecycle per harness (create → start → register →
roundtrip), plus the two moves no other scenario exercises end to end —
**swapping the harness under a living agent mid-life (decision A9)** and
**restarting the same harness with a different model** — and a cross-check
that worker-liveness convergence (#106) holds under `kill -9` for a managed
worker. A real Lark leg proves an inbound DM reaches the *pinned agent*
regardless of which harness currently embodies it, and that the agent can
proactively deliver an image and a file to the user during that same turn.

## Config-load assertions (2026-08-15)

Coverage gap named by Allen: everywhere else the scenario proves config is
*recorded* (desired state echoes the spec) but not that it is *effective*.
Three groups close it:

- **Process cwd (OS level):** each managed worker's actual working
  directory — read with `lsof -a -p <pid> -d cwd -Fn`, since macOS has no
  `/proc` — must equal the agent's configured cwd. Asserted for claude, pi,
  and codex. Worker PIDs are found by the same daemon-PPID-chain filter the
  #106 phase uses; for claude the asserted process is the Agent SDK's
  bundled `claude` CLI child, the only process in its uv → python → CLI
  chain that is given the spec's cwd (the wrappers keep the daemon's own
  cwd by design).
- **Skill load (behavioral):** the pi worker is started with `--skill`
  pointing at a runner-generated skill directory whose SKILL.md demands
  that any message containing `CFGPROBE` be answered starting with a
  bracketed token carrying a per-run random nonce. A CFGPROBE roundtrip
  whose reply carries the token proves the file at the `--skill` path was
  loaded and shaped behavior; the nonce keeps a stale session from an
  earlier run from ever echoing it. **pi-only:** pi is the one harness with
  an explicit skill *launch flag*; claude discovers skills through
  settings/cwd conventions with no path flag on its CLI, and codex has no
  skill mechanism at all, so no equivalent launch-path assertion exists for
  them.
- **Headless busy FIFO:** the managed-worker counterpart of E2E-008's cc
  TUI busy check, run once each against pi and codex. The first message
  makes the worker run `touch <sentinel> && sleep 6` before replying; once
  the sentinel exists (turn provably in progress, no reply yet) the second
  message is delivered. Both must be answered exactly once, in delivery
  order, with no loss — order read from the daemon's arrival-ordered
  pending list, the same FIFO contract E2E-002 pins.

## Reply bridge (landed: PR #110)

The `harness_reply -> Lark native reply` bridge landed on `dev` with PR #110
(`fix/reply-bridge`); the authoritative observable contract is
`docs/reference/p3-mcp.md`, section "Correlated Lark reply bridge". This scenario
asserts both of its observables in the `lark.*` reply group:

- `lark.native-reply` — the pinned worker's `harness_reply` on the
  `adapter:lark:<adapter>` inbound is accepted into the durable outbox and
  bridged through the supervised adapter worker into a **native Lark
  reply** correlated to the original inbound `messageId`; the user receives
  it in the same DM.
- `lark.reply-terminal-state` — after the native send and reply-index
  commit are receipted, the sender-side terminal record reads
  **`fetched` with reason `ACK_RECEIVED`, held by the sender daemon**.
  The adapter worker is a supervised
  local transport endpoint, never a second Harness inbox.

Historical note (pre-#110 builds): `message.reply` refused `channel:*`
senders outright with `MESSAGE_REPLY_UNAVAILABLE`, and the pinned worker's
model would burn a long turn trying alternative routes. On `dev` with #110
that refusal remains only for malformed channel actors or adapters absent
from persistent configuration. `--skip-reply-bridge` classifies the reply
group `BLOCKED` and is only meaningful for checkouts predating #110.

## Agent attachment boundary

This release deliberately keeps `harness_reply` text-only: its MCP schema has
no attachment field. An agent that needs to send an attachment uses an
independent configured route (`hyprial message send --image` or `hyprial message send --file`) during
the turn, then calls `harness_reply` with text after both sends succeed. A
crafted `message.reply` carrying non-empty `resourcePaths` must fail loudly
with `MESSAGE_REPLY_RESOURCES_UNSUPPORTED` before pending-message lookup or
acknowledgement; the error states that the original pending message was not
acknowledged and no reply was queued. Silently discarding the resource and
sending only text is forbidden.

## Preconditions

- [REAL ENV] `claude`, `pi`, and `codex` executables on `PATH`, plus one
  DeepSeek API key. Every leg selects `deepseek/deepseek-flash` through
  the public Hyprial options. The same-harness model-id switch uses a Claude
  sonnet alias that DeepSeek's Anthropic endpoint maps back to
  `deepseek-flash`, so it exercises configuration replacement without
  consuming Anthropic quota.
- [REAL ENV] Lark leg only: `lark-cli` at `/opt/homebrew/bin/lark-cli` with a
  verified *user* identity, and the dedicated test App `allenwoods-hyprial-e2e`
  (`cli_aaf9ea19d4b8dbe4`) with its local credential file
  `~/.hyprial/secrets/lark-allenwoods-hyprial-e2e.json` readable. The runner copies
  only this test-App credential into its disposable home and never prints a
  secret byte. The caller must have an existing p2p DM with the App's bot
  (resolved live by name; DM chat ids are never committed). **The production
  Apps — `hq-adjutant-feishu`, `hyprial-kanban`, `mac-studio-feishu`,
  `allen-squire` — are a red line and are never touched.** No other consumer
  of the test App's event stream may be online during the run (the
  production daemon does not run this adapter; a second connected E2E-004
  gateway would steal events and surface here as a missing OnIt reaction).
- [REAL ENV] Lark leg only: pi writes session transcripts under
  `~/.pi/agent/sessions`. The runner only reads this directory
  (content-matched by nonce) to prove the pinned worker received the DM; it
  never writes into the real pi home.
- The runner owns disposable `HYPRIAL_HOME` and `HARNESS_STATE_DIR` roots under a
  short temp path, strips inherited Zenoh endpoints via the shared
  isolated-daemon helper, and never uses the production daemon socket. A
  probe Lark gateway with fabricated credentials is written into the
  disposable home for pin semantics only and is never started.

## Steps

1. Preflight: verify the three harness binaries (and Codex auth), then start
   a real Python daemon in a temporary state root. Before `hyprial init`, write
   the disposable `channels.json`: the never-started `e2e013-pin-probe`
   gateway, plus (when the Lark prerequisites hold) the `allenwoods-hyprial-e2e`
   gateway with the copied test-App credential and one direct outbound route
   whose `nativeId` is the caller's p2p DM resolved live during preflight (the
   private chat id is never committed or printed raw). Also generate the disposable
   CFGPROBE skill directory (SKILL.md with a fresh random nonce token) in
   the runner's temporary root — it must exist before the pi worker launches
   with `--skill` pointing at it.
2. Per harness (claude, pi, codex):
   1. `hyprial agent create --name e2e013-<h> --provider <vendor> --model
      <model> --preferred-harness <h>` — the durable identity exists before
      any connector.
   2. `hyprial agent start <h> --name e2e013-<h> --nickname <nick> --cwd <temp>
      --headless --json -- <model args>` — starting the *same* name is a
      rebinding, not a conflict (A5 reuse path). The pi args additionally
      carry `--skill <generated dir>` for step 3.
   3. Assert the connector reports `running=true`, the agent's canonical URI
      is `online` in `hyprial message targets`, and schema-v1 desired state records the
      spec exactly (provider, name, nickname, cwd, headless, ownership,
      args).
   4. Assert the worker's OS-level cwd: locate the single worker process
      descending from the daemon PID (claude: the Agent SDK's bundled
      `claude` CLI child; pi: the `pi` RPC child; codex: the `codex
      app-server` child) and assert `lsof -a -p <pid> -d cwd -Fn` reports
      exactly the agent's configured cwd.
   5. [REAL ENV] Send a nonce probe through `hyprial message send`, await the correlated
      reply in the same conversation, verify the reply text carries the
      demanded marker, and acknowledge it.
3. [REAL ENV] Skill-load probe (pi only; see "Config-load assertions" for
   why no claude/codex equivalent exists): send a message containing
   `CFGPROBE` to the pi worker and assert the correlated reply carries the
   bracketed nonce token that exists only inside the runner-generated
   SKILL.md — the skill at the `--skill` path was loaded and shaped
   behavior.
4. [REAL ENV] Headless busy FIFO, once each on pi and codex:
   1. Send probe 1 instructing the worker to run `touch <sentinel> &&
      sleep 6` via its shell tool, then reply with marker 1.
   2. Wait for the sentinel file; assert no reply has arrived yet (the turn
      is provably in progress), then send probe 2 demanding marker 2, and
      assert again that no reply landed before the second send completed.
   3. Await both correlated replies; assert exactly two replies, each marker
      in exactly one reply, the marker-1 reply ahead of the marker-2 reply
      in the arrival-ordered pending list, acknowledgement of both, and no
      extra reply within a grace window after ack (exactly once, FIFO, no
      loss).
5. Mid-life harness switch (A9) on a dedicated agent `e2e013-morph`:
   1. Create the agent, bind the never-started probe adapter to its cell
      (`hyprial agent cell show e2e013-morph` then `hyprial agent cell apply
      e2e013-morph --expected-revision N --file CELL.json` adding the adapter
      member), and start it on
      claude. The first start must carry **no** `harnessHandover`.
   2. [REAL ENV] Seed conversation context: a roundtrip that hands the agent
      an unguessable token `CTX<nonce>` and demands a fixed marker reply.
   3. Snapshot the agent record: `uri`, `createdAtMs`,
      `lastHarness == "claude"`, `lastSessionId` (the managed headless path
      records no session id today, so this clue may be null; an interactive
      leg would record one — the assertion pins whatever was recorded).
   4. `hyprial agent stop claude e2e013-morph`, then `hyprial agent start pi --name e2e013-morph
      --headless -- --provider <vendor> --model <model>` — same name, new
      harness.
   5. Assert the start response carries `harnessHandover` with
      `previousHarness == "claude"`, `previousSessionId` equal to the
      snapshotted clue, `contextCarriedOver == false`, and a notice naming
      both harnesses.
   6. Assert the same agent entity: one record, `uri` and `createdAtMs`
      unchanged; `lastHarness` is now `"pi"`.
   7. Assert the pin still maps `e2e013-pin-probe` to the same canonical URI
      (a pin binds the agent, never the harness).
   8. [REAL ENV] Context zeroing + post-switch roundtrip in one probe: ask
      the pi-embodied agent to reveal the `CTX` token if it knows it, else
      reply a fixed marker. The correlated reply must arrive (roundtrip
      works) and must NOT contain the token (context does not carry).
   9. `hyprial agent stop pi e2e013-morph` (leaves exactly one pi worker for step 7).
6. Same-harness model switch on `e2e013-claude`:
   1. `hyprial agent stop claude e2e013-claude`, then restart the same name and
      harness with `--model <alt>`.
   2. Assert the response carries **no** `harnessHandover` (same harness is
      not a handover), desired-state `args` reflect the new model verbatim,
      and [REAL ENV] a correlated roundtrip completes on the new model.
7. Worker-liveness convergence cross-check (#106) on `e2e013-pi`:
   1. Locate the single pi worker child that descends from the daemon PID
      (the pi CLI rewrites its process title to a bare `pi`, so the match
      accepts that title as well as an argv-preserving `--mode rpc`
      spelling) and `kill -9` it.
   2. Assert one observable stopped interval: `ps` reports the connector
      `running=false` within a bounded window (the supervisor reaps the
      child, withdraws the actor registration, and arms a one-second restart
      backoff instead of hiding the death behind a same-tick replacement).
   3. Assert desired-state recovery: `running=true` again within a bounded
      window with a **fresh** child PID.
   4. [REAL ENV] Assert the recovered worker completes a correlated
      roundtrip.
8. [REAL ENV] Lark leg (test App `allenwoods-hyprial-e2e`):
   1. `hyprial adapter start allenwoods-hyprial-e2e` — the real worker connects the
      WebSocket event stream and reports `online`.
   2. Bind `allenwoods-hyprial-e2e` to the pi-embodied agent's cell
      (`hyprial agent cell apply e2e013-pi …`) — inbound DMs route to
      the pi-embodied agent.
   3. Resolve the caller's p2p DM with the bot live by App name. Create a
      nonce-bearing PNG and text file under the disposable root, then assert
      that a crafted attachment-bearing `message.reply` from the pinned agent
      fails with `MESSAGE_REPLY_RESOURCES_UNSUPPORTED` before lookup/ack and
      queues no reply (`lark.harness-reply-attachment-rejected`).
   4. Send a user DM carrying the nonce, the two exact shell commands, and a
      demanded final reply marker via `lark-cli`. The pinned agent must, in
      this same turn and in order, run `hyprial message send --image <png>` and `hyprial message send
      --file <file>` to the configured test-App route, require both commands
      to exit zero, and only then call text-only `harness_reply` once.
   5. Assert custody: the user's message gains the `OnIt` reaction.
   6. Assert the pinned agent received the DM: the probe nonce appears in
      the pinned worker's own session transcript (read-only over the real
      `~/.pi/agent/sessions`, content-matched — the same technique E2E-012
      uses on Codex rollouts). The inbound being queued under the agent's
      canonical URI is recorded as supplementary detail.
   7. As the independent user identity, observe an app-sent native `image`
      message created after the turn began, download it through the IM
      resource API, and compare it byte-for-byte with the generated PNG
      (`lark.agent-image-outbound`). The agent transcript must also carry the
      route result's `resourceDeliveries` and matching `nativeMessageId`.
   8. Likewise observe the nonce-named native `file` message, download it,
      and compare it byte-for-byte with the generated file
      (`lark.agent-file-outbound`). These checks prove user delivery and
      content identity, not merely a successful agent-side CLI exit.
   9. Assert the bridged reply per the PR #110 contract: the user receives
      the agent's reply as a native Lark message in the same DM (polled via
      `lark-cli`; when the listing exposes a parent id, it must equal the
      original inbound `messageId`), and the sender-side terminal record
      for the reply reads `fetched` with reason `ACK_RECEIVED`, held
      by the sender daemon (`hyprial message status --from <pinned agent
      URI>`). With
      `--skip-reply-bridge` (pre-#110 checkouts only) this group reports
      `BLOCKED`, not `FAIL`.
9. Teardown persistence on `e2e013-codex` (reclamation semantics: `hyprial agent stop`
   = stop the process + remove from desired state; a **manual** trigger —
   there is no automatic reclamation):
   1. `hyprial agent stop codex e2e013-codex` and assert the worker process
      terminates (no `codex app-server` descendant of the daemon remains),
      the entry is removed from schema-v1 desired state, the connector
      disappears from `ps`, and the agent's URI is no longer `online` in
      `message targets`.
   2. Assert down is not destroy: the agent record persists, offline.
   3. **The key differential (no resurrection):** restart the daemon and
      assert — against the same restart — that the desired workers
      (`e2e013-claude`, `e2e013-pi`) revive to `running=true` while the
      downed worker does not come back in any form: no connector in `ps`,
      no app-server child, no desired-state entry, not `online` in
      `message targets`. E2E-006 proves desired workers DO revive; this asserts a
      downed worker MUST NOT (the gap behind the 2026-08-15 production
      incident where four unreclaimed historical workers revived on every
      daemon restart).
10. Teardown: `hyprial agent stop --all` (also stops the Lark adapter), daemon stop,
    disposable roots removed. Assert no App secret byte appeared in any
    report line.

## Assertions

- `hyprial agent create` persists provider, model, preferred harness, the
  default minimal home config, and the default home workspace on the durable
  record before any connector exists, and echoes them back.
- Each of claude, pi, and codex: managed headless launch succeeds, the
  connector reports `running=true`, the canonical agent URI is visible and
  `online` in `message targets`, and schema-v1 desired state is exact for provider,
  name, nickname, cwd, headless, ownership, and args.
- Each managed worker's OS-level process cwd (read with `lsof` on the
  single daemon-descendant worker PID — claude's bundled CLI child, pi's
  RPC child, codex's app-server child) equals the agent's configured cwd:
  the cwd is effective, not merely recorded.
- [REAL ENV] Each of claude, pi, and codex completes a correlated roundtrip:
  the reply arrives in the same conversation, carries the demanded marker,
  and the acknowledgement succeeds.
- [REAL ENV] Skill load is behavioral, pi-only: a CFGPROBE roundtrip against
  the worker started with `--skill <runner-generated dir>` replies with the
  bracketed per-run nonce token that exists only inside that SKILL.md
  (claude has no skill launch-path flag and codex no skill mechanism, so
  the assertion has no equivalent there).
- [REAL ENV] Headless busy FIFO on pi and on codex: a second message
  delivered while the first turn is provably mid-`sleep` (shell sentinel
  on disk, no reply yet) yields exactly two replies — each probe answered
  exactly once, in delivery order per the arrival-ordered pending list,
  with no extra reply after acknowledgement (no loss, no duplication, no
  reorder).
- Mid-life harness switch (A9): `hyprial agent stop` + `hyprial agent start <other> --name
  <same>` rebinds the *same* agent entity — canonical URI and `createdAtMs`
  unchanged, exactly one record throughout.
- The switch emits `harnessHandover` exactly once (absent on first start and
  on a same-harness restart): `previousHarness` names the harness being left,
  `previousSessionId` equals the recorded clue (null on today's managed
  headless path), `contextCarriedOver` is `false`, and the notice text names
  both harnesses.
- `lastHarness` tracks the new binding after the switch.
- [REAL ENV] Conversation context does not carry across the switch: a token
  seeded on the first harness is not reproducible by the second (the reply
  must not contain the token).
- The adapter binding maps to the same canonical agent URI before and after the
  switch: cell documents bind the agent, never the harness.
- [REAL ENV] The switched agent completes a correlated roundtrip on the new
  harness.
- Same-harness model switch: the restart carries no `harnessHandover`,
  desired-state `args` record the new model verbatim, and [REAL ENV] a
  correlated roundtrip completes on the new model.
- #106 convergence: after `kill -9` of the managed worker child, `ps`
  exposes one observable `running=false` interval, then restores
  `running=true` from desired state alone with a fresh child PID, and
  [REAL ENV] the recovered worker roundtrips. The same killed PID is recorded
  as `worker.exited` with `returnCode=-9` in
  `state/logs/workers/pi-<name>.jsonl`; this filename and event model remain
  the compatibility seam for #106 diagnostics.
- [REAL ENV] Lark: the adapter for the dedicated test App reaches `online`,
  the inbound user DM gains the `OnIt` reaction, and the pinned agent
  demonstrably receives it (the probe nonce appears in the pinned worker's
  own session transcript).
- [REAL ENV] `lark.agent-image-outbound`: in the pinned agent's Lark turn,
  its `hyprial message send --image` produces a user-visible native `image`; downloading
  that message as the user yields bytes identical to the disposable PNG, and
  the agent transcript carries matching route-delivery evidence.
- [REAL ENV] `lark.agent-file-outbound`: in the same turn, its `hyprial message send
  --file` produces a user-visible native `file` with the nonce-bearing name;
  the user download is byte-identical to the disposable source and the agent
  transcript carries matching route-delivery evidence.
- [REAL ENV] `lark.harness-reply-attachment-rejected`: because
  `harness_reply` remains text-only, non-empty `resourcePaths` on
  `message.reply` is rejected loudly as
  `MESSAGE_REPLY_RESOURCES_UNSUPPORTED` before lookup/ack, with no reply
  queued. Attachments must use the independent route commands above; none
  may be silently discarded.
- [REAL ENV] The user receives the agent's reply as a native Lark message
  in the same DM, correlated to the original inbound message (PR #110
  contract).
- [REAL ENV] The bridged reply's sender-side terminal record reads
  `fetched` with reason `ACK_RECEIVED`, held by the sender daemon (the adapter worker is a supervised local
  transport endpoint, not a second inbox).
- Teardown persistence: `hyprial agent stop` terminates the worker process, removes
  the entry from desired state, withdraws the registration (`ps` and
  `message targets`), and leaves the agent record intact offline (down is not
  destroy).
- No resurrection: across one daemon restart, desired workers revive while
  the downed worker stays down in every observable (no connector, no child
  process, no desired-state entry, not online in targets).
- No App secret byte and no raw `oc_`/`ou_` native identifier is emitted in
  any report line.
- Every line and the final summary is classified `PASS`, `FAIL`, or
  `BLOCKED`.

## Known non-fatal symptom

Real `codex app-server` runs can log `ERROR ... failed to refresh available
models: timeout` on stderr shortly after startup (same symptom as E2E-012).
The runner never uses stderr content or a transient `connectors[].error`
string as a failure criterion — only `running`, desired state, registration,
and the roundtrips themselves are asserted.

## Interaction with worker-liveness convergence (#106)

Step 7 asserts the #106 semantics *positively* (one stopped interval, then
backoff recovery). Everywhere else the runner only polls for convergence
within a bounded window and never asserts instant recovery, so it is
compatible with the one-second restart backoff by construction.

Run:

```sh
uv run python contract/e2e/run-all-harness-lifecycle.py
# only on checkouts predating PR #110 (no reply bridge yet):
uv run python contract/e2e/run-all-harness-lifecycle.py --skip-reply-bridge
```

## send-links 硬切：组织空间前置（WP8 返工 2-E，2026-10-15）

两机/多 harness 的 agent→agent 段在硬切后需要同一组织空间（invite+join 后等 `orgAvailable=true`）。本条为前置说明；该跑者本轮未改造（记 UNKNOWN，不报通过）。
