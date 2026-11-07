# P3 MCP runtime

The Python MCP facade uses fixed-identity stdio processes launched by HYPRIAL:
`python -m hyprial.daemon.impl.mcp.entry claude-channel` for an interactive Claude session and
`python -m hyprial.daemon.impl.mcp.entry agent-channel` for a daemon-managed worker or a tool-only
interactive dsh-tui MCP child. It uses the official `mcp` Python SDK
high-level server API. The locked SDK is 2.0.0, where the class formerly called
`FastMCP` is named `MCPServer`; the module retains the high-level decorator
model rather than dropping to hand-written JSON-RPC.

An interactive dsh-tui launch additionally runs one launcher-owned
`python -m hyprial.daemon.impl.mcp.entry dsh-carrier` process. That process, not the dsh-mcp-client stdio
child, owns session registration, inbox polling and wake scheduling. The MCP
child is tool-only because dsh-mcp-client probes stdio servers with a separate
temporary process before starting the serving child; a carrier inside that
child would register and inject twice.

## State and reconnect contract

- `harness_send`, `harness_reply`, `harness_read`, `harness_ack`,
  `harness_whoami`, and `harness_targets` are exposed. There is no
  `harness_delegate`: structured delegation (defer the parent turn and resume
  it with the child result) is not implemented in this runtime — the daemon has
  no `message.delegate` dispatch nor the `delegation.inspect/observe/complete`
  methods, and no provider-side turn suppression/resume plumbing exists — so
  the tool is not advertised rather than shipped invoking a `METHOD_NOT_FOUND`
  daemon method.
- Actor and session identity are fixed by the daemon/CLI at child launch and
  carried on every Unix-daemon request; tool callers cannot replace them.
- Every tool call opens a fresh daemon connection. The MCP process has no
  pending-message cache and therefore cannot return a ghost inbox after a
  daemon restart.
- One reconnect retry is automatic. Mutations keep the same request ID across
  that retry so a daemon adapter can deduplicate an accepted-before-disconnect
  operation. The cross-module daemon/wire adapter is intentionally outside P3.

## Wake contract

`WakeCoordinator` reads P2's `interactiveSessions` registry for actor, cwd, and
launch command. Formal work enters the scheduler only through
`enqueue(actor, delivery_id)` and produces a prompt containing that delivery
ID. `preview()` is observation-only: its text starts with `PREVIEW ONLY`, it
does not enqueue, and it never invokes a provider driver. This prevents a Stop
hook preview from impersonating a formal delivery or creating a reply
correlation.

Scheduling is round-robin by actor. A busy or offline actor retains its FIFO
head with bounded exponential backoff, while each other actor gets a turn in
the same dispatch pass. Consequently a busy interactive session cannot starve
formal deliveries for other sessions.

The daemon/provider integration supplies the two Protocol implementations:
the short-lived daemon connection factory and provider-specific wake driver.
The MCP module imports neither provider implementations nor transport/inbox
implementations.

## dsh-tui carrier

`hyprial agent start dsh` launches a real dsh-tui with a launch-scoped profile patch
and an isolated carrier process. The patch loads:

- `@deepseek-ai/dsh-mcp-client` as a tool-only stdio server running
  `python -m hyprial.daemon.impl.mcp.entry agent-channel`; and
- a launch-scoped Cordis plugin written by the launcher. The plugin opens a
  private Unix control socket and queues a synthetic user message through
  `agent.followup()` with `source.kind: "plugin"`. It never touches the TUI
  input box and never uses the editor-oriented `prompt.append` /
  `prompt.submit` protocol.

The launcher-owned `python -m hyprial.daemon.impl.mcp.entry dsh-carrier` process:

- registers `source=dsh-channel`, `runtime=dsh_interactive`, with the TUI
  process pid and birth identity;
- refreshes and heartbeats the shared carrier protocol;
- waits for the plugin control socket to report a live root agent before
  registering;
- writes only the formal wake prompt (actor and delivery id) to the plugin;
  message previews, summaries and bodies never cross the control socket;
- unregisters on carrier stop, and is supervised by the launcher: if the
  carrier dies, the launcher terminates the TUI so the actor cannot remain
  online with no wake edge.

The launch lands straight in a workspace with an agent running: the launcher
pins the dsh-tui agent preset (`--agent-preset <id>`, default `standard`,
handed over as the `DSH_TUI_PRESET` launch instruction) and marks dsh-tui's
first-run wizard and one-shot workspace home seen on a fresh HOME (missing
files only, never overwriting an existing pref). See the design record,
`docs/design/dsh-tui-interactive-connector.md` §10.15.

The design and the safe-injection boundary are in
`docs/design/dsh-tui-interactive-connector.md`.

## Claude Code Channel adapter

Interactive Claude Code sessions can launch one local stdio child with:

```sh
HYPRIAL_HOME=/isolated/hyprial HARNESS_STATE_DIR=/isolated/state \
  python -m hyprial.daemon.impl.mcp.entry claude-channel \
  --actor agent:alice:mac:coordinator \
  --session-ref "$CLAUDE_SESSION_ID" \
  --cwd /workspace \
  --command claude
```

The launcher/plugin must configure that command as a Claude Channel server.
Its initialize result advertises `capabilities.experimental["claude/channel"]`;
it does not advertise a standard MCP extension. The same process exposes all
six Harness tools and polls the daemon's durable inbox for formal delivery
edges. Polling is intentionally state retrieval rather than MCP-owned inbox
state, so daemon restart and CC SessionStart/resume re-signal the same stable
delivery ID.

Only the head `delivery_id`, actor, pending count, and the instruction to call
`harness_read` once for the entire FIFO batch are sent
in `notifications/claude/channel`; message previews/bodies never cross the
Channel notification. A successful stdio write changes the wake state to
`SIGNALLED`, not completed. `harness_read` changes it to `AWAITING_ACK`, and
only a daemon-accepted `harness_reply` or `harness_ack` clears the key. There
is at most one outstanding edge per session; later deliveries remain FIFO and
are returned by that one `harness_read`, so they do not flood a recovering
session with one turn per item.

Background Channel polling is observation-only. A public `harness_read` marks
the entire returned FIFO batch fetched and retires matching same-node sender
outbox rows and pushes a fetch receipt to remote senders. `ACK_RECEIVED` is the
recipient terminal reason; fetch receipt retirement closes the sender's
stranded hold. A missed push causes idempotent redelivery while the recipient is
online, not a second confirmation phase; a session that never calls
`harness_read` leaves the sender hold until `TTL_EXPIRED`.

This is a provider-neutral interactive-session IPC contract, not a Claude-only
exception. `hyprial.contracts.session` owns the `fetched=true` wire marker; the
generic MCP server, Claude Channel adapter, and Pi extension emit it only for
explicit `harness_read`. The daemon and inbox layers own retirement for
`claude-channel`, `dsh-channel`, `pi-extension`, and `codex-app-server` sessions
alike. Future TUI carriers must reuse this contract and must not implement
provider-specific outbox deletion. Headless daemon dispatch remains unchanged;
its completion ACK reaches the same idempotent shared retirement seam.

Hard acceptance invariant: delivery success depends only on consumption, never
on the consumer's reply style or native carrier. For the original message,
each of these completion forms must retire the sender outbox, stop retries, and
produce no delivery alarm:

- `harness_reply(messageId, message)`;
- `harness_ack(messageId)`;
- `hyprial message ack <messageId> --from <actor>`.

All three converge on the shared daemon/InboxService acknowledgement boundary;
carrier implementations must not add their own success or retirement rules.

The cross-node choice combines a fast one-shot push with queryable proof. Online
senders retire immediately; an offline sender may retain the row until its
terminal tick, but the durable query prevents a false DLQ without multiplying
receipt timeouts across every ordinary retry. It does not depend on actor
liveliness recovering. Both paths inherit the existing receipt mechanism's
trust model and validate sender plus message ID against the held row.

The generated interactive settings pulse the same recovery path from
`SessionStart` (`startup|resume|clear|compact`) and from `StopFailure` model/API
failures such as `rate_limit`, `overloaded`, `billing_error`, and
`model_not_found`. The pulse contains no message content and coalesces on disk
per launched session. The Channel child consumes
it on its normal poll loop, re-reads daemon truth, and emits one fresh batch
edge. Claude Code does not expose a dedicated `/model` hook; a model switch
therefore consumes the wake already queued by the preceding `StopFailure`
rather than depending on a fabricated model-change event.

Registration supersede also follows physical session identity. Registering a
new actor URI with the same `source + sessionRef` atomically replaces the old
desired-state entry and withdraws its route/liveness state; calls from the old
URI receive `SESSION_SUPERSEDED`. General agent rename/pin migration remains the
separate rename-migration concern.

### Correlated Lark reply bridge

The following is the public, harness-independent contract for calling
`harness_reply` on a pending message whose sender is
`channel:lark:<adapter>`:

- The daemon accepts one deterministic reply message into its durable outbox
  and returns `replyMessageId` plus `queued`. All MCP and worker harnesses use
  this same daemon method; Claude Channel no longer rejects the operation just
  because the pending sender is a Lark channel actor.
- Only the exact `channel:lark:<adapter>` shape is bridgeable. The daemon sends
  the outbox item to that named supervised adapter worker, which uses Lark's
  native reply API with the original inbound `messageId` correlation and
  persists the existing durable reply index before returning a receipt.
- `queued=false` means the native send and reply-index commit were receipted.
  The daemon then records terminal `fetched`, held by the sender daemon, with
  reason `ACK_RECEIVED`. This is the delivery model's sender-side receipt
  record: the adapter worker is a supervised local transport endpoint, not a
  second Harness inbox.
- An offline worker or transient native-send failure leaves the reply in the
  same durable outbox and returns `queued=true`. Retry continues until a worker
  receipts it or the holder TTL writes terminal `expired` with
  `TTL_EXPIRED`; initial offline deferral therefore shows `attempts=0`
  but cannot outlive `expiresAtMs` without a terminal record.
- A malformed channel actor or an adapter name absent from persistent
  configuration fails immediately with `MESSAGE_REPLY_UNAVAILABLE`. Ordinary
  sends to channel actors remain unsupported; the bridge applies only to a
  correlated reply.
- Pre-bridge rows require no schema migration. If their payload lacks
  `replyTo`, the bridge recovers the original inbound id from the existing
  `reply:<messageId>` idempotency key. On the next retry they either take the
  native path above or reach the same TTL-expired terminal state.

Confirmed interactive Claude uses `ClaudeChannelDriver`. Managed headless
Claude does not select a wake driver from this module: the live
`ClaudeAgentSdkProcess` owns its streaming input queue and receives durable
deliveries through the harness lifecycle boundary. Channel notifications are
not claimed to wake an idle `claude -p` process.

## Daemon-restart resilience (deploy without a session restart)

A daemon restart is the deploy path: it drops the child's Unix socket to the
daemon and resets ephemeral daemon-side routing and presence. It does **not**
drop the Claude Code &lt;-&gt; child stdio transport, which is a separate
connection Claude Code owns. The channel poll loop is therefore built to
survive it in place:

- **The child never crashes on a daemon fault.** `_run_channel_poll_loop`
  guards every daemon interaction against `DaemonDisconnected`, the daemon
  error-envelope `RuntimeError`, and `OSError`/`TimeoutError`. Claude Code
  cannot respawn a dead stdio child, so an unguarded raise here would orphan the
  channel and force a full session restart — the exact failure this loop
  prevents. Cancellation is a `BaseException` and is never swallowed, so
  shutdown still propagates.
- **Reconnect with backoff.** On a daemon-contact failure the loop keeps
  running and retries with bounded exponential backoff (poll interval doubling
  to a 5s cap), so a long daemon outage does not hot-loop the socket.
- **Recovery refresh and immediate re-wake.** After any daemon-contact failure
  the loop confirms the same persisted actor/session with `session.refresh`,
  polls pending work immediately, and re-arms that existing backlog once. This
  bypasses the pre-recovery notification delay without creating or requiring a
  new inbound message; it never completes a delivery, which still requires a
  successful `harness_reply` or `harness_ack`.
- **State stays daemon-authoritative.** `StatelessDaemonProxy` opens a fresh
  socket per call, and `poll_once` prunes wake keys for deliveries that vanished
  across the restart (`_complete_out_of_band`). `deliveryId` is a volatile
  handle used only for wake scheduling; reply/ack are keyed on the durable
  `messageId`. So after a restart the channel re-resolves the new generation's
  `deliveryId`s, drops the old ones, and read/reply/ack self-heal — no ghost
  inbox, no session restart, no `/mcp reconnect`.

The result: deploying new **daemon** code requires no Claude Code session
restart. Tests `tests/test_mcp_channel_restart.py` pin this end to end,
including a held-open MCP client session that reads generation-2 truth and acks
a generation-2 `messageId` after the daemon is torn down and replaced
mid-session.

## Transport: why the coordinator channel stays stdio

The card proposed also moving this channel from stdio to HTTP/SSE for Claude
Code auto-reconnect. That is **not viable for the coordinator channel**: Claude
Code channels — the server→client wake edge — are documented as stdio-only
(Claude Code spawns the channel as a subprocess and communicates over stdio;
`code.claude.com/docs/en/channels-reference`). An `http`-transport MCP server
cannot push the unsolicited `notifications/claude/channel` wake, so switching
the coordinator to HTTP would silently delete wake — the channel's entire
purpose. The daemon-restart problem is solved above without changing transport,
so the channel deliberately stays stdio. Managed headless runtimes use their
fixed-identity `agent-channel` stdio child for tools and the provider-owned
streaming queue for input; no alternate MCP HTTP serve/build path is exposed.

## Version compatibility

Because Claude Code spawns the stdio child at session start and cannot respawn
it, the child's **MCP server code** is pinned to the version present when the
session started. Consequences:

- Deploying new **daemon** code: no session restart needed — the resilient loop
  reconnects and re-registers against the new daemon (above).
- Deploying new **MCP channel** code (tools, wake logic): still requires a new
  Claude Code session to take effect, because the running child keeps its
  start-time code. This is a Claude Code stdio-channel constraint, not a daemon
  limitation. Operators upgrading MCP-side behavior should plan a session
  restart; daemon-only deploys do not need one.
- The daemon inbox is authoritative and the proxy is stateless, so an older
  in-session child against a newer daemon keeps its older tool surface and never
  produces a ghost inbox or stale-handle ack failure. A daemon response an older
  child cannot parse is caught like any other daemon-contact fault (the child
  logs and retries rather than crashing; `_DAEMON_CONTACT_ERRORS` includes the
  `TypeError`/`ValueError` a shape mismatch raises), so even a response-shape
  change degrades safely instead of forcing a session restart.

Each current Channel includes three additive, non-secret fields in
`session.register`: `channelBuildVersion`, `channelProtocolVersion`, and
`ownerFence`. New daemons persist them and expose a derived
`channelGeneration` in `hyprial daemon status --json`; old daemons ignore the extra request
fields. Registrations created by an older child remain readable with all three
fields absent and are classified `legacy_or_unknown`, so the desired-state
schema stays at v1 and rollback remains possible. Build versions accept only a
short package-version token; process arguments, paths, owner identities, and
credentials are never copied into telemetry.

`hyprial doctor` reports counts only. It warns when an interactive Claude
registration is legacy/unknown or did not attest the stable owner PID/birth
fence, and recommends a fresh `hyprial agent start claude` launch or resume. This is
deliberately **generation evidence, not a liveness probe**: interactive session
records are durable and a daemon restart restores them even if the stdio child
is gone. A live tool call or an explicit future heartbeat is still required to
prove current process health.
