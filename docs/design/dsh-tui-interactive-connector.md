# DSH TUI interactive connector (dsh-refactor)

Status: implementation plan, 2026-10-18. Owner: dsh-refactor. Branch:
`feat/dsh-tui-interactive`, based on `origin/dev` @ `61dd3fddf`.

## 1. Goal and non-goals

Goal: `hyprial agent start dsh` (without `--headless`) launches or resumes a real
`dsh-tui` interactive process, injects the Hyprial MCP surface into that
session, and makes the session addressable on the Harness Network.

The existing `src/hyprial/daemon/impl/harnesses/dsh/` path is a daemon-managed
headless Web API driver (`dsh --profile web`, token banner, HTTP `/api/...`).
It is not the interactive path and is not reused for this feature. It remains
present until the separate headless/SDK line replaces it.

Non-goals in this change:

- reimplementing DSH's headless HTTP protocol;
- attaching to an already-running TUI process by modifying its in-memory MCP
  server list. MCP servers are loaded at TUI composition time, so "take over"
  is implemented as resuming the selected DSH session in a newly launched TUI;
- changing the DSH prompt/model UI;
- merging, pushing, or opening a PR without the coordination process.

## 2. Architecture

The DSH TUI is a CLI-hosted interactive session, like Claude/Pi TUIs. It is not
a daemon-managed headless harness process.

Components:

1. `hyprial agent start dsh` CLI launcher.
   - Resolves the TUI binary (`HARNESS_DSH_TUI_BIN`, else `dsh-tui`).
   - Creates/updates the interactive Agent through `_create_agent_for_start`.
   - Mints a Harness `sessionRef` (or reuses the DSH session id for `--resume`).
   - Writes a launch-scoped profile patch under the Hyprial state dir.
   - Starts `dsh-tui` in the foreground with terminal inherited.
   - Waits for the in-TUI MCP child to register the interactive session.
   - Cleans the launch patch on exit.

2. Launch-scoped DSH patch.
   - A temporary YAML overlay passed through `dsh-tui --patch <file>`.
   - Inserts one `@deepseek-ai/dsh-mcp-client` row with
     `transport: stdio`, `serverName: harness-bridge`.
   - The stdio command is the current Hyprial Python entry point running
     `python -m hyprial.daemon.impl.mcp.entry dsh-carrier`.
   - The patch does not edit `~/.dsh/profiles/dsh-tui/cordis.patch.yml` and is
     removed when the launcher exits.

3. `python -m hyprial.daemon.impl.mcp.entry dsh-carrier`.
   - Serves the existing `harness_*` and workflow MCP tools over stdio.
   - Registers an interactive session with `source=dsh-channel` and
     `runtime=dsh_interactive`.
   - Polls the daemon inbox through the shared `InboxWatch`/`WakeCoordinator`
     core, exactly like a terminal carrier.
   - For every due message, writes a formal wake prompt to the TUI's local
     injection socket and submits it.
   - Never replies/acks on the model's behalf; the model uses
     `harness_read`, `harness_reply`, and `harness_ack`.

4. DSH TUI injection edge.
   - Running dsh-tui sessions publish `~/.dsh-tui/inject/servers.json`.
   - The MCP child discovers its owner entry by explicit DSH session id (when
     resuming), by parent PID, then by unique matching cwd.
   - Wake payload is newline-delimited JSON on the Unix socket:
     `{"type":"prompt.append","text":"..."}` followed by
     `{"type":"command.execute","command":"prompt.submit"}`.

## 3. Data flow

Outbound network use:

```text
model -> mcp__harness-bridge__harness_send -> python -m hyprial.daemon.impl.mcp.entry dsh-carrier
      -> daemon message.send -> recipient
```

Inbound network use:

```text
daemon pending message
  -> dsh-channel InboxWatch
  -> WakeCoordinator
  -> inject "Harness backlog..." via TUI Unix socket
  -> dsh-tui opens a model turn
  -> model calls harness_read / harness_reply / harness_ack
  -> daemon durable reply/ack clears the delivery
```

The wake prompt is deliberately an instruction to poll daemon truth; the
socket injection is an edge, not proof of delivery. Only a durable reply/ack
completes the message.

## 4. Public command and example

```sh
hyprial agent start dsh --name my-dsh --cwd /path/to/work
hyprial agent start dsh --name my-dsh --resume <dsh-session-id>
```

Approximate child shape:

```text
dsh-tui --patch <state-dir>/dsh-channel-<session-ref>.patch.yml \
        --resume <dsh-session-id> /path/to/work
```

Approximate patch:

```yaml
- insert:
    - id: mcp-harness-bridge
      name: '@deepseek-ai/dsh-mcp-client'
      config:
        transport: stdio
        serverName: harness-bridge
        command: <python>
        args: [-m, hyprial.cli, mcp, dsh-channel,
               --actor, <actor>, --session-ref, <ref>, --cwd, <cwd>]
        env:
          HYPRIAL_HOME: <home>
          HYPRIAL_STATE_DIR: <state-dir>
          HYPRIAL_WORKER_ACTOR: <canonical actor URI>
          HYPRIAL_WORKER_SESSION_REF: <session ref>
          HYPRIAL_MANAGED_WORKER: '1'
```

## 5. Capability and lifecycle contract

Interactive DSH declarations:

- `INTERACTIVE_ATTACH`: native, mechanism `dsh_tui`.
- `WAKE_PUSH`: native, via the TUI injection socket.
- `PROACTIVE_SEND`: native, via MCP harness tools.
- `INJECTION`: MCP tools native; skills/plugins are not claimed for this slice.
- `SESSION_LIFECYCLE`: resume by exact DSH session id; no daemon-restart
  recovery for a CLI-hosted TUI.
- `TURN_CONTROL`: unsupported in this slice.
- `EVENT_STREAM`: unsupported in this slice.

The session registration must use `source=dsh-channel`, which is added to the
closed `SESSION_CARRIER_SOURCES` contract so register/refresh/heartbeat and
daemon routing treat it like the other terminal carriers. The MCP child dies
with the TUI; an owner fence and best-effort `session.unregister` clean the
route.

## 6. File plan

- `src/hyprial/kernel/impl/contracts/execution/session.py`
  - add `dsh-channel` to `SESSION_CARRIER_SOURCES`.
- `src/hyprial/daemon/impl/harnesses/capabilities.py`
  - add `dsh_tui` mechanism and the interactive dsh capability row.
- `src/hyprial/shell/impl/cli/commands/agent/runtime/start.py`
  - dispatch interactive dsh through the new launcher.
- `src/hyprial/shell/impl/cli/commands/agent/runtime/interactive.py`
  - implement `_start_interactive_dsh` and launch-patch helpers.
- `src/hyprial/shell/impl/cli/commands/common/services.py`
  - expose the launcher binding.
- `src/hyprial/shell/impl/cli/runtime/entry.py`
  - wire the binding.
- `src/hyprial/shell/impl/cli/commands/meta/mcp.py`
  - add `dsh-channel` command.
- `src/hyprial/daemon/impl/mcp/channel/dsh.py`
  - injector driver, DSH adapter, and stdio session runner.
- Tests:
  - capabilities golden row;
  - CLI launcher: argv/patch/env/resume/cleanup/missing binary;
  - injection driver: discovery and exact socket frames;
  - dsh-channel registration/wake/unregister against a fake daemon;
  - structural limits and ruff.

## 7. Test strategy

Red first:

1. Capability test expects interactive DSH attach/wake/tools native under
   `dsh_tui`; currently unsupported.
2. Launcher test installs a fake `dsh-tui` executable and asserts:
   - `--patch` points to a launch-scoped file;
   - `--resume` maps to the requested DSH session;
   - env pins the daemon home/socket and actor;
   - missing binary fails before agent creation;
   - patch file is removed after process exit / startup failure.
3. `DshTuiWakeDriver` test creates a Unix socket and fake `servers.json`; asserts
   the exact two JSON lines and the discovered socket path.
4. Channel test drives `session.register` with source `dsh-channel` and runtime
   `dsh_interactive`, then a pending list and wake, then graceful unregister.

Real-environment smoke after unit tests:

- isolated `HYPRIAL_HOME` and `DSH_HOME`;
- launch a real dsh-tui with a temporary patch;
- verify `/mcp` shows `harness-bridge`;
- `harness_whoami` returns the launched actor;
- send a message from another actor and observe an injected turn and
  `harness_reply`.

Heavy daemon/E2E runs require coordination with emergent/be-dev-3 under the
current release-window rules.

## 8. Coordination and risks

Agreed boundary with emergent-org-structure-refactor:

- this line owns CLI interactive launch, dsh-tui injection, session
  registration, and the MCP bridge;
- the existing headless `daemon/impl/harnesses/dsh/` path stays untouched and
  non-primary until a separate headless/SDK decision;
- re-use the shared interactive session registration/lease model as it evolves
  in D1 (pi liveness) and S8 (carrier projection);
- summary-required `harness_send`/`harness_reply` must be present in the MCP
  tool schema (the shared MCP server already supplies it).

Risks:

- TUI injection socket protocol is a documented but moving dsh-tui edge;
  isolate it behind one driver and fail loudly if discovery/framing changes.
- No native model-turn signal is available initially. Wake re-signalling only
  suppresses after `harness_read` custody; the injected prompt explicitly
  requires the model to read once and drain the batch.
- Attaching to an *already running* TUI without restart is not possible through
  the public MCP configuration. The supported takeover is resume-by-id in a new
  TUI process.
- DSH/MCP package version skew must be tested against the installed dsh-tui
  0.16.1 / dsh 0.2.0-rc.2.

### External patch-loader smoke (2026-10-18)

A minimal inserted MCP row was checked against the installed DSH 0.2.0-rc.2
loader with an isolated copy of the `dsh-tui` profile:

```text
DSH_HOME=/tmp/dsh-smoke-home \
  dsh --profile dsh-tui --patch /tmp/dsh-mcp-smoke.patch.yml --dump-config
```

Result: exit 0, and the dumped composition contains both
`mcp-harness-bridge` and `harness-bridge`. This proves the `insert` shape is
accepted by the real profile composer. It does not prove that the Carrier
starts or that a model turn is injected; those remain real-TUI E2E checks.

## 10. Review response: v2 design (2026-10-18)

The R&D review returned **ACCEPTED-WITH-CHANGES** with five P1 changes. This
section is the implementation contract for v2. The existing prototype commits
are evidence of the external facts and test direction, but the v1
carrier-in-MCP-child implementation must be refactored before merge.

### 10.1 Correction: package ownership and launch route

- `@deepseek-harness-tui/dsh-tui` is a **third-party/community package**
  (author `chimney`), not an official `@deepseek-ai` package. Version risk and
  supply-chain review are explicit.
- Passing `--patch` makes the launcher use the `dsh --profile dsh-tui ...`
  route instead of the standalone host-entry route. This is an accepted,
  documented difference for this feature; the real smoke must cover this exact
  route.

### 10.2 P1-1: one liveness model, developed in parallel with D1

The source added to `SESSION_CARRIER_SOURCES` stays, but it must merge with
be-dev-1's shared interactive-liveness fix, not with a parallel dsh-specific
verdict.

be-dev-1 clarified the current authoritative dev model:

- `AgentLiveness.verdict()` treats an interactive actor with a fresh heartbeat
  as alive; `None` means "not judgeable here" and remains fail-closed for
  `start`.
- `runtime="dsh_interactive"` does not break the binding's liveness because the
  session binding is always recorded as `runtime="interactive"`.
- The lease token is currently accepted only for `claude-channel`.
  `dsh-channel` therefore must not send `channelLeaseToken` until the shared
  lease model is generalized.
- `heartbeat` must follow a successful `refresh` for this daemon generation,
  otherwise the daemon returns `STALE_DAEMON_GENERATION`.
- `SESSION_SUPERSEDED` is terminal (quiet, no re-register);
  `STALE_SESSION` means re-register.
- The TUI process pid + birth identity must be persisted on the session so
  daemon-start cleanup and 706's online verdict can use them.

Merge rule: dsh-channel and be-dev-1's liveness work are developed in parallel
and must be verified on one merged tree before either is declared complete.
We do not add another liveness registry or status projection.

### 10.3 P1-2: carrier moves to the launcher process

`@deepseek-ai/dsh-mcp-client` performs a stdio **probe** startup followed by a
serving process. A carrier inside the MCP child would register twice, may
inject from the probe, and can supersede/unregister the real child.

v2 architecture:

```text
hyprial CLI launcher
  ├─ owns the dsh-tui child Popen and terminal lifecycle
  ├─ runs exactly one DshCarrier (registration + InboxWatch + wake)
  ├─ starts the TUI with a launch-scoped --patch
  └─ MCP child: `python -m hyprial.daemon.impl.mcp.entry agent-channel` = TOOLS ONLY
       (no session.register, no inbox polling, no injection)
```

The carrier is single-instance per launcher/sessionRef. Probe and serving MCP
children are harmless because neither registers or wakes. The MCP child still
signs tool calls with the same fixed actor/sessionRef environment.

Red tests must launch two MCP tool servers with the same sessionRef and assert
that neither creates a carrier registration or an injection; only the launcher
carrier does.

### 10.4 P1-3: draft-safe injection

`prompt.append` + `prompt.submit` submits the entire current input buffer. A
wake arriving while the user is typing would concatenate the user's partial
draft with the Harness instruction and clear the draft. The injection socket
has no read-draft or atomic-send API, so this cannot be made safe by retrying
or prefixing text.

Decision: the normal wake path must not use `prompt.append`/`prompt.submit`.
The v2 implementation adds a small companion Cordis plugin,
`dsh-plugin-hyprial`, which injects a non-human `user/message` through DSH's
native agent/session API rather than through the TUI input box. The carrier
writes a wake to a per-session plugin control socket; the plugin queues one
synthetic user message. The TUI input draft is never touched.

Fallback boundary until the plugin is implemented and packaged:

- `hyprial agent start dsh` may still launch the TUI and inject MCP tools, but
  wake delivery is reported `DEGRADED`/unavailable, not silently switched to
  prompt.append.
- No "online" registration claim is made when the safe injection edge is not
  present.
- Tests must assert the wake frame contains no user draft, no message body and
  no summary; only actor/delivery identifiers and the instruction to call
  `harness_read`.

### 10.5 P1-4: exact TUI process/session binding

Injection discovery must be proof-based:

1. The launcher records the `dsh-tui` Popen pid and birth identity.
2. The carrier walks the process ancestry to prove that the `servers.json`
   entry belongs to a descendant of that launch. A matching cwd alone is never
   enough and is removed as a fallback.
3. The session id may narrow the match inside that proven process tree, but it
   cannot bind to a different pid.
4. The carrier re-resolves the socket before every wake, because `/new`,
   `/resume`, update/restart, or session takeover can replace the socket.
5. Before connect: `lstat` the socket, reject symlinks, reject non-current-uid
   owners, and reject group/world-writable parent directories. Reconnect
   failure maps to `OFFLINE`, never `ACCEPTED`.
6. Socket file permissions/servers.json modes are treated as a same-OS-user
   trust boundary, matching the daemon's existing lease positioning.

The actor binds to the **TUI process**, while the current DSH session id is a
secondary attribute. If the user switches session inside the same TUI process,
the socket follows the new session; if the process dies, the carrier must
unregister.

### 10.6 P1-5: readiness must include the injection edge

`channelConfirmed=True` is automatically set by the daemon for any source in
`SESSION_CARRIER_SOURCES`; it is not proof that injection works.

v2 readiness:

1. Launcher waits for the TUI injection edge to appear and pass the exact
   process binding checks.
2. Carrier registers only after that edge is ready.
3. Launcher's readiness predicate checks both:
   - `source=dsh-channel`, matching `sessionRef`, and
   - the carrier's injection-edge health publication (`ready` and exact TUI
     pid/identity).
4. If the injection socket disappears or a safe plugin control socket becomes
   unavailable, the carrier unregisters/degraded-marks itself so the daemon
   does not report "online but unwakeable".
5. `harness_read` custody remains the v1 turn-custody workaround, bounded and
   retestable, until a native turn signal is confirmed.

### 10.7 P2/P3 resolutions

- **Wake content**: only caller-supplied formal `command.prompt` (actor and
  delivery id) is injected. Never inject preview text, summary or body.
  The model reads daemon truth through `harness_read`.
- **Duplicate wake cost**: a DSH wake consumes a full model turn. Resignalling
  policy is DSH-specific and bounded; do not copy Claude's default cadence
  unchanged. v1 target: at most one outstanding wake per delivery until
  `harness_read` custody or reply/ack; tests cover long turns and repeated
  daemon lists.
- **Runtime process claim**: session `processPid`/`processIdentity` point to
  the TUI process, not the MCP child or probe, so S8/706/daemon-start cleanup
  have the right fence.
- **Launcher cleanup**: launcher `finally` performs a best-effort fenced
  unregister as well as patch cleanup. Startup also removes stale launch
  patches for dead launchers. Patch mode `0600`, directory `0700`.
- **MCP patch**: include `failOnStartupError: true`; a failed MCP child must
  surface through the launcher readiness gate rather than silently leaving a
  TUI without tools.
- **Capabilities**: interactive DSH `PLUGIN_INJECTION` is `DEGRADED` with MCP
  only (matching Codex's "MCP only, no skills" precedent).
  `SESSION_LIFECYCLE` is `DEGRADED` until exact session/process mapping and
  daemon-restart behavior are proven. `TURN_CONTROL` and `EVENT_STREAM` remain
  unsupported in this slice.
- **Provider/model**: interactive DSH v1 does not own model selection. Explicit
  `--provider`/`--model` on `hyprial agent start dsh` must be refused with a clear
  message until a real mapping exists; do not record metadata and silently
  ignore it.
- **Session data**: "global profile untouched" means Hyprial does not modify
  `~/.dsh/profiles/dsh-tui/cordis.patch.yml`. DSH itself still owns its normal
  global session storage under the user's DSH home. Any agent-home isolation is
  a separate P2/runtime decision.
- **Version policy**: do not hard-pin dsh-tui forever. Track the latest
  compatible dsh/dsh-tui/dsh-mcp-client versions; CI emits an update reminder
  and the integration test suite is the gate.
- **Contracts/docs/skills**: new source `dsh-channel`, runtime
  `dsh_interactive`, mechanism `dsh_tui`, CLI error codes and capability changes
  require contract updates or an explicit `Contract-Impact` note. Update
  `docs/reference/p3-mcp.md`, `hyprial-e2e`, and `hyprial-ops`. Doctor coverage
  for dsh-channel is a follow-up.
- **707**: interactive DSH switching to headless cannot preserve the DSH
  sessionRef because the existing headless DSH path cannot resume a session.
  This transition must be refused or explicitly defined by 707; v1 does not
  claim it.
- **Headless path**: still retained as non-primary and untouched. The
  headless Python-SDK track is a separate design and is not implemented here;
  priority/scope are pending be-dev-3's evaluation.

### 10.8 v2 implementation slices

1. Design correction and evidence (this section).
2. Launcher-owned single carrier + tool-only MCP child; probe red tests.
3. Exact TUI process/session discovery and stale-socket handling.
4. Injection plugin contract and dummy-plugin red tests; no prompt.append.
5. Readiness/degraded/unregister and all exit-path cleanup tests.
6. D1/be-dev-1 liveness integration on a combined tree.
7. Isolated real E2E: isolated HOME/HYPRIAL_HOME/HARNESS_STATE_DIR/socket,
   no production daemon, mock provider, verify wake->read->reply without
   touching drafts.

### 10.9 v2 implementation update (2026-10-18)

The two highest-risk P1s are now implemented in the branch:

- **P1-2**: the MCP child is tool-only (`python -m hyprial.daemon.impl.mcp.entry agent-channel`). A
  separate launcher-owned `python -m hyprial.daemon.impl.mcp.entry dsh-carrier` process owns session
  registration, InboxWatch, wake scheduling and unregister. The probe child
  cannot register or inject.
- **P1-3**: the launcher writes a launch-scoped Cordis plugin and passes its
  absolute path in the same `--patch`. The plugin opens a private Unix control
  socket and queues a synthetic user message via `agent.followup()` with
  `source.kind: "plugin"`. The normal path never uses
  `prompt.append`/`prompt.submit` and never touches the TUI input draft.
- The carrier waits for the plugin control socket and a real live root agent
  (`ctx.agents.roots()`) before it claims readiness.
- The carrier process is supervised by the launcher; if it exits before or
  after readiness, the TUI is terminated and the launch fails. Both children
  and the launch-scoped directory are cleaned on normal exit, spawn failure,
  registration timeout, child exit, and nonzero TUI exit.
- Plugin file mode is 0600; launch directory is 0700; socket is chmod 0600 by
  the plugin and revalidated by the carrier.
- `dsh --profile dsh-tui --patch ... --dump-config` accepts the new absolute
  plugin row and emits `hyprial-dsh-bridge`.
- A fake-Cordis Node runtime test proves the plugin answers ping, queues one
  `agent.followup()` message with plugin source attribution, and accepts the
  wake frame without any input-box command.

Current focused result: 85 passed, ruff clean, structural and module-boundary
gates clean. Still required before merge: isolated real dsh-tui smoke,
same-tree verification with the be-dev-1 liveness line, and R&D re-review.

### 10.10 Coordination corrections and E2E boundary (2026-10-18)

Two coordination facts changed after the first review cycle:

- There is **no `D1` branch** and no `fix/pi-interactive-liveness` branch owned
  by be-dev-1. The authoritative liveness/lease model is already on
  `origin/dev`; the process-fence online verdict is 706, owned by be-dev-3
  (`fix/carrier-reap-and-switch`, worktree `/private/tmp/hf-carrier-reap`,
  final HEAD `cf86a996d` at the time of writing, not yet pushed). dsh-channel
  therefore tests against `origin/dev` now and against 706's tree when
  be-dev-3 publishes a SHA.
- 707 (`fix/carrier-reap-and-switch`) defines same-actor replacement through
  daemon preflight/`--replace-interactive`; the dsh launcher must not kill a
  previous carrier itself. dsh carrier exit/unregister participates in that
  same route lifecycle.
- be-dev-1 clarified that `session.processPid/processIdentity` is used by the
  daemon liveness path; this design records the **TUI process** there. The
  launcher-owned carrier is the heartbeat producer, and the launcher watchdog
  terminates the TUI if the carrier dies, so a session cannot remain online
  with a dead carrier. `unknown` process identity remains fail-closed, never
  "dead"; only `pid-missing`/`identity-mismatch` may be reclaimed.
- 706 may land after this branch. The merge gate is: same-tree
  register/refresh/heartbeat + supersede/stale + process-fence tests with the
  706 SHA, not a fabricated D1 branch.

Real-TUI E2E status in the current execution environment: **BLOCKED**. The
sandbox denies `tmux` fork and PTY allocation (`out of pty devices`), so a real
interactive dsh-tui process cannot be launched here. The isolated smoke must
run on a PTY-capable executor using emergent's rules (isolated HOME,
HYPRIAL_HOME, HARNESS_STATE_DIR, HARNESS_SOCKET_PATH and DSH_HOME; no
production credentials; mock provider; teardown daemon and TUI).

What is verified without a real PTY:

- the real DSH 0.2.0-rc.2 profile composer accepts the absolute plugin row in
  `--dump-config`;
- the real dsh-mcp-client row shape is accepted by the installed composer;
- the generated plugin runs under Node with a fake Cordis context, queues one
  `agent.followup()` synthetic user message, and answers ping/wake over its
  control socket;
- launcher fake-process tests cover tool-only MCP activation, carrier
  lifecycle, watchdogs, and cleanup on every tested exit path.

### 10.11 Documentation, contract and doctor impact

The interactive dsh line publishes these new or changed semantic fields:

- carrier source `dsh-channel`;
- runtime spelling `dsh_interactive`;
- capability mechanism `dsh_tui`;
- hidden launcher-internal command `python -m hyprial.daemon.impl.mcp.entry dsh-carrier`;
- CLI error codes `DSH_TUI_SPAWN_FAILED`, `DSH_CARRIER_SPAWN_FAILED`,
  `CHANNEL_REGISTRATION_FAILED`, `DSH_CARRIER_EXITED`;
- interactive dsh capability rows downgraded as described in §10.7.

Documentation updated in the same change:

- `docs/reference/p3-mcp.md`: tool-only MCP child, launcher-owned carrier,
  launch-scoped Cordis plugin, source list;
- `.agents/skills/hyprial-ops/SKILL.md`: `hyprial agent start dsh` capability and
  the no-prompt.append/no-provider-model boundary;
- `.agents/skills/hyprial-e2e/SKILL.md`: manual PTY E2E acceptance checklist,
  isolation rules, and PASS/FAIL/BLOCKED evidence requirements.
- `hyprial doctor`: new `dsh-tui-carrier` check reports TUI process fence and
  channel confirmation, without claiming wake health from `online` alone.

The E2E scenario number is intentionally not assigned until emergent/review
decides whether it becomes a numbered gate. The current checklist lives in
the E2E skill so operators can collect evidence without creating an
unregistered `scenarios.json` entry.

### 10.12 Review v2 follow-up: N-1 to N-10 (2026-10-18, second pass)

This section records how the second review's findings were closed and what
stays open. Tests are named by their function; every one is in
`tests/daemon/channels/mcp/test_dsh_channel.py` or
`tests/shell/cli/agents/test_cli_dsh_interactive.py` unless stated otherwise.

- **N-1 (P1) plugin message shape** — the plugin builds
  `{id: crypto.randomUUID(), role: 'user', content, source}` and deep-freezes
  it before `agent.followup()`. `source.kind` is `hyprial-bridge`, the
  producer's own kind (dsh-llm explicitly has no shared catch-all `plugin`
  kind). The Node fake-Cordis runtime asserts a non-empty uuid `id`,
  `role === 'user'` and both frozen flags.
- **N-2 (P2) dead carrier leaves an online registration** —
  (a) the carrier polls the TUI pid/identity every second and stops on
  `pid-missing`/`identity-mismatch`; (b) the `python -m hyprial.daemon.impl.mcp.entry dsh-carrier` command
  now handles `SIGHUP` as a normal stop, so a closed terminal unregisters;
  (c) the launcher's `finally` sends a fenced `session.unregister` as a last
  resort, but only when the daemon's stored record still names this launch's
  TUI `processPid` and `source=dsh-channel` — a newer same-actor registration
  is left alone; (d) the same-tree run with 706 remains a merge gate.
- **N-3 (P2) shared process group** — the carrier is spawned with
  `start_new_session=True` and the launcher remains its only terminator; a
  fake-launcher test asserts the `Popen` kwarg.
- **N-4 (P2) no runtime degradation** — the carrier polls the plugin with
  `ping` instead of testing socket presence, and cancels after
  `INJECTION_EDGE_LOSS_GRACE_SECONDS` of not-ready. `NO_AGENT` (a resumed
  session the user switched away from with `/new` or `/resume`, or more than
  one root) and a vanished socket reach the same verdict, so the actor is
  withdrawn rather than silently online. **Answer to the Q2 binding
  question:** lifecycle/fencing binds the **TUI process**
  (`processPid`/`processIdentity`), while injection binds the **DSH
  session**; a fresh launch follows the single live root, `--resume` pins the
  id, and both withdraw as soon as the edge stops resolving to a live agent.
- **N-5 (P2) carrier-death UX** — owner decision: keep terminating the TUI,
  because a bridge that cannot inject must not stay online, but make the
  failure actionable. The carrier publishes the bound DSH session id to
  `dsh-session-id` (0600) in the launch directory, and both
  `CHANNEL_REGISTRATION_FAILED` and `DSH_CARRIER_EXITED` name the id and print
  `hyprial agent start dsh --resume <id>`. The alternative (retry the carrier once,
  keep the TUI offline) is deferred until the PTY E2E can measure its cost.
- **N-6 (P2) surviving mutations** — each review mutation now has a failing
  test: M1 `test_carrier_does_not_register_before_plugin_ready`;
  M3 `test_plugin_ping_reports_not_ready_without_a_live_root_agent`;
  M4 `test_driver_rejects_unsafe_socket_paths`; M5
  `test_start_dsh_carrier_crash_after_ready_stops_tui_and_reports` (asserts
  the TUI was terminated with SIGTERM); M6
  `test_start_dsh_does_not_claim_ready_from_a_different_carrier_source`.
  This pass also added TUI-watch, injection-edge, session-id and fenced
  unregister tests (M7-M9: `..._the_tui_process_disappears`,
  `..._the_plugin_reports_not_ready`, `..._unregisters_only_its_fenced_session`
  and `..._keeps_a_newer_registration_with_another_pid`). Missing `node` is now
  `pytest.skip`, not a silent `return`.
- **N-7 (P2) input and socket paths** — `--resume` must match
  `^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$`; the launch directory name is a random
  16-hex value with no user input, so `..` cannot escape the state directory;
  the control-socket path is checked against 100 bytes (under the 104-byte
  `sun_path` limit) before anything is created and fails with a clear error.
- **N-8 (P3) runtime arguments** — `--backend`, `--`, dsh-tui subcommands
  (`doctor`, `help`, `migrate`, `safe`, `update`, `version`) and any positional
  argument are rejected, so the fixed `--backend dsh` and the trailing cwd
  cannot be overridden or reinterpreted.
- **N-9 (P3) socket defence in depth** — the trust boundary stays "same OS
  user plus a 0700 launch directory plus a 0600 socket"; the carrier does not
  check peer credentials. A one-shot wake token in the patch config is a
  reasonable hardening but is not claimed here.
- **N-10 (P3)** — the capability comment describes the Cordis plugin;
  interactive `MODEL_SELECT` is explicitly `unsupported` ("choose the model
  inside dsh-tui") instead of being absent; carrier stderr still inherits the
  terminal and the doctor confirmed-counter is unchanged, both tracked as
  follow-ups.

Contract/impact: the change is additive (new `source=dsh-channel`,
`runtime=dsh_interactive`, mechanism `dsh_tui`, four CLI error codes, and
capability rows). No `contract/` file enumerates interactive sources or
runtimes, so `scripts/verify/check_contract_impact.py --base origin/dev`
measures zero candidates.

Still open, and explicitly **not** claimed by this PR:

1. Real PTY E2E (`scripts/verify/dsh_tui_e2e.py`, both `--crash-mode kill` and
   `--crash-mode socket`) — BLOCKED in the dsh-refactor sandbox; R&D runs it on
   a PTY-capable executor.
2. The same-tree run with 706 (`fix/carrier-reap-and-switch`) and the
   `hyprial-cli-developer` quality gate.
3. Headless dsh on the official Python SDK — a separate plan (S0-S8), not
   implemented here.

### 10.13 Third review: the launcher topology and the runtime hang (2026-10-19)

The third re-check rejected the branch on two P1s that the previous rounds
could not see without a real PTY. This section records the fix and the
evidence, and supersedes the carrier-ownership wording in §10.10.

**P1 (process fence): the carrier now owns the TUI.** `session.register`'s
fence (`registry.py:224-271`) accepts exactly two relations: the IPC peer is
an ancestor of the claimed process, or the claimed process is the peer's
direct child. The launcher used to start the TUI and the carrier as
**siblings**, so registration was refused with `CALLER_NOT_AUTHORIZED` in
every real-PTY run (and on the 706 same-tree run). `start_new_session` does
not change parentage.

Option chosen (Allen, 2026-10-19): **the carrier spawns the TUI**, which is
the fence's existing direct-child branch (the inverse of Codex's shape, where
the IPC CLI Popen()s the claimed app-server). Consequences, all deliberate:

- The launcher no longer spawns dsh-tui. It hands the carrier a JSON
  `--tui-argv` plus the launch directory, then waits on the carrier.
- The carrier publishes `tui-pid`, `tui-identity`, `tui-exit-code` and
  `tui-spawn-error` (all 0600) in the launch directory; the launcher reports
  the TUI's exit code and can take the TUI down if the carrier dies first
  (identity-checked before any signal).
- The carrier must stay in the terminal's session so the TUI keeps the
  controlling tty; `start_new_session` is gone. To keep the bridge alive
  across terminal signals, the carrier **ignores SIGINT** (Ctrl-C belongs to
  the TUI) and treats SIGTERM/SIGHUP as a normal stop; it always terminates
  the TUI in its own `finally`.
- The fence is not weakened. A sibling claim is still refused and is now a
  test (`tests/daemon/harnesses/dsh_tui/test_process_fence_topology.py`,
  real processes: direct child accepted, siblings refused, foreign identity
  refused).

**P1 (runtime hang): the stop waiter is polling, not a blocking thread.**
`wait_for_stop` awaited `anyio.to_thread.run_sync(stop_event.wait)`, which is
not cancellable, so the injection-edge, TUI-gone and readiness-timeout paths
never returned and never unregistered (measured blocked past 25 s); with the
old `start_new_session` this also orphaned the carrier (`ppid=1`). It is now
a cancellable `is_set()`/`sleep(0.2)` poll. `abandon_on_cancel=True` was
rejected: it unblocks the await but leaves the abandoned worker thread for
interpreter shutdown to join. The two carrier stop tests now pass a real
`stop_event` (the CLI always does) and fail on the old waiter.

**N-4 leftovers.** The 300 s resend curve and the read-retry delay are now
pinned by a test (a DSH wake costs a whole model turn, so the default
1s..60s cadence is wrong). The grace period still starts only after
registration. The plugin still answers `ok` immediately after
`followup()`, so a failed turn remains silent; mapping `ok` to "queued"
rather than "accepted" is tracked as a follow-up because it changes the
custody/retry contract.

**Runner R-2..R-6 (third review, R&D-measured).**

- R-3: `run_tui.sh` now exports an explicit allowlist of host variables
  (`_TUI_ENV_ALLOWLIST`), never `**sanitized_env(os.environ)`. Canary check:
  `HF_HOST_CANARY`, `CLAUDE_CODE_MESSAGING_TOKEN` and `XDG_CONFIG_HOME` no
  longer appear; `HARNESS_STATE_DIR`, `DEEPSEEK_BASE_URL` and
  `HARNESS_DSH_TUI_BIN` do. Failure artifacts default outside the checkout
  (`HYPRIAL_E2E_ARTIFACTS` under the temp root) instead of `<cwd>/e2e-artifacts`.
- R-2: the daemon and tmux are stopped **inside** the scenario tempdir
  (`_stop_scenario`), so `TemporaryDirectory` no longer raises `ENOTEMPTY`
  and hides the real failure; the outer `finally` is only a backstop.
- R-4: the runner composes one local org space (`ensure_local_org`) after the
  sender agent exists, so sends no longer park at `LINK_SELF_FULL`.
- R-5: the launch directory is searched under the daemon's
  `HARNESS_STATE_DIR` (`node_root/state`), not `root/state`.
- R-6: `wake.signalled` is read from the carrier worker log
  (`state/logs/workers/dsh-*.jsonl`); `hyprial log --actor` does not surface
  those entries.
- R-1 is mitigated by the random 16-hex launch directory and the 100-byte
  socket-path pre-check in the launcher.

Still open after this round: a Messages mock that emits `tool_use` (without
it, wake→read→reply is BLOCKED, not PASS), the `/mcp` emptiness root cause,
the carrier's stderr drawing over the TUI, and the doctor confirmed counter.
The official runner remains BLOCKED in this sandbox and must be executed on
a PTY-capable host.

### 10.14 R&D take-over: real-PTY acceptance and the remaining items (2026-10-19)

R&D took the branch over from dsh-refactor at `e5dccd2ef` and ran the runner
on a real tmux PTY (own `tmux -L` server, isolated daemon, no key). Readings
and raw `/mcp` text: `docs/evidence/dsh-tui-interactive-2026-10-19/`.

**What the PTY showed, and the fix for each.**

- **The §10.13 fixes hold.** With the carrier owning the TUI, registration is
  accepted by the unchanged fence (`processPid` = the TUI the carrier
  spawned); the cancellable waiter unregisters on socket loss. Mutations:
  restoring the blocking `run_sync(stop_event.wait)` hangs the stop tests
  (killed by a 60 s alarm); a fence that also accepts same-parent siblings
  fails `test_a_sibling_claim_is_refused`; dropping SIGHUP or the 300 s curve
  is red.
- **`/mcp` root cause: the MCP server never started.** dsh-mcp-client spawns
  `command` with `args`; the launch patch put `sys.executable` in both, so the
  server ran `python python -m hyprial.cli ...`, died, and
  `failOnStartupError` dropped the row without a visible error. The model was
  offered 26 tools, none from harness-bridge, and `/mcp` (built from the same
  `tools` runtime) said "No MCP servers configured". It was not
  `session.capabilities.mcp` being unavailable. After the fix `/mcp` lists
  `harness-bridge (35 tools)` and the runner asserts it again. The row now
  comes from one shared builder (`dsh_tui/mcp_row.py`) that refuses args
  repeating an interpreter; #1582's `mcp_client_row` can delegate to it.
- **A tool-calling Messages mock** (`contract/e2e/_messages_tool_mock.py`)
  replaces "wake→read→reply BLOCKED": it answers a wake with
  `harness_read`, the read result with `harness_reply`, and streams the
  shape dsh's adapter validates (the text-only fake lacked `usage` and a
  stop reason).
- **Wake `ok` now means the turn's outcome (E.4).** The plugin watches its own
  message: `agent/inbox/claimed`, `agent/inbox/discarded`, `agent/error` for
  the claimed turn, and the first **content** chunk on `agent/assistant-stream`.
  Two PTY runs disproved weaker signals: the stream's `start` frame and a
  terminal `finish` chunk carrying the adapter failure are both published
  without the model answering. Inside the window carried by the frame (8 s)
  the reply is `accepted` / `claimed` / `queued` (ok) or `TURN_ERROR` /
  `WAKE_DISCARDED` / `FOLLOWUP_REJECTED` (not ok). The carrier maps not-ok to
  `WakeStatus.FAILED`: a warn-level `wake.failed` with the code and detail,
  retried at 30 s doubling to 300 s (each retry is a model turn; the shared
  1 s..60 s failure curve is wrong for DSH). PTY proof: `--fail-first-wake`
  makes the mock answer the first wake turn with HTTP 400; the worker log
  shows `wake.failed … TURN_ERROR (mock: injected wake-turn failure)`, then
  `wake.signalled … accepted` on attempt 2 and the reply.
- **Carrier stderr.** The carrier spawns the TUI on the terminal, then
  `dup2`s its own fd 2 onto `launch-dir/carrier.stderr` (0600); the launcher
  quotes its last 20 lines in a carrier failure, since teardown removes the
  directory.
- **Doctor.** `confirmed` is gone (the daemon sets `channelConfirmed` for every
  carrier source, so it always equalled `total`); `processGone` counts fenced
  registrations whose TUI pid is missing or reused (positive evidence only)
  and turns the check to warn.
- **Runner.** Session lookup by actor URI (bare-name matching could never
  succeed); dsh-tui's first-run guide and one-shot workspace home are marked
  seen (neither has a root agent, so the plugin is never ready); the wrapper
  `exec env -i` with the allowlist (the tmux server otherwise leaks the host
  environment to the launcher, carrier and dsh); the provider key reaches the
  wrapper only through the new tmux server's environment, never a file or an
  argv; `--evidence-dir` outside the checkout; `crash-cleanup` also requires
  the session withdrawn and no carrier/TUI/dsh/MCP-server process left.

**DESIGN-GAP (not implemented here): atomic fenced unregister.** The
launcher's last-survivor cleanup (`_fenced_dsh_unregister`) reads `ps`, checks
that the row still names this launch's `processPid`, then sends a plain
`session.unregister {actor, sessionRef}`. Between the two calls a newer launch
can re-register the same `(actor, sessionRef)` (only possible with
`--resume <same id>`), and the daemon would remove it. Proposed daemon-side
shape, for R&D's daemon owner: `session.unregister` accepts optional
`processPid` + `processIdentity`; when present the registry removes the row
only if the stored fence equals them, inside the same registry transaction,
and otherwise returns a distinct "fence moved" result (not an error) so the
launcher can stop. The carrier's own unregister needs no change (it holds the
lease). Until then the window is one `ps` round-trip on a teardown path.

**Still open, deliberately.**

- A turn that fails **after** the 8 s window (a slow provider erroring late)
  is still reported `claimed`/`queued` and only resignalled after 300 s.
  Closing it needs an asynchronous outcome channel from the plugin to the
  carrier (or a native turn signal), not a longer window.
- A first launch on a fresh HOME lands on dsh-tui's guide/workspace home,
  where no agent exists; the carrier then fails registration after 30 s with
  a resume hint. The runner seeds both prefs; a real first-time user still
  meets this. Options: start dsh-tui with a workspace target that skips the
  home, or let registration wait for the first agent. Needs a product call.
- `carrier.stderr` is not rotated; it lives only as long as the launch.
- dsh-tui writes an `EIO` crash entry to `~/.dsh-tui/crash.log` when the
  carrier terminates it after its PTY is gone (teardown only, observed in
  every crash-mode run); harmless but noisy.
- The dsh version-watch CI script shared with #1582 was not needed here and
  is not part of this PR.

### 10.15 Workspace start: the launcher owns the first-run screens (2026-10-19)

Allen's ruling on the last open item of §10.14: `hyprial agent start dsh` must
launch straight into a workspace with an agent running, using agent preset
`standard` by default, configurable. "Let registration wait for the first
agent" was rejected: a launch that spends its first 30 s on a wizard and
then fails is not a launch.

The launcher therefore owns the two screens dsh-tui gates on its own prefs
files, both of which a fresh HOME shows before any agent exists:

- **Agent preset.** The launcher hands the TUI the documented
  `DSH_TUI_PRESET` launch instruction, which outranks the persisted `/preset`
  choice and the cordis.yml default. The default is `standard`; the
  `hyprial agent start --agent-preset <id>` option overrides it for one launch.
  The id is validated against dsh-agent-presets' own boundary (lowercase
  letters, digits and dashes) before any daemon call, because a value
  outside it composes a session without a preset instead of failing.
  `--agent-preset` is refused off interactive dsh.
- **First-run screens.** Before the carrier spawns the TUI, the launcher
  marks dsh-tui's first-run wizard (`~/.dsh-tui/onboarding.json`,
  `{"completed": true, "version": 1}`) and one-shot workspace home
  (`~/.dsh-tui/home.json`, `{"seen": true}`) seen — only where the file is
  missing (O_EXCL). An existing pref is the user's own state and is never
  overwritten. Best effort: a write failure only leaves dsh-tui's own
  wizard in place, exactly as without hyprial; the wizard stays available
  inside the TUI through `/setup`. The workspace target itself was already
  covered — the launcher appends the cwd positionally, which dsh-tui reads
  as `DSH_TUI_WORKSPACE_TARGET` and which already skips the home screen.

The E2E runner no longer seeds the prefs or presses Escape on a guide
(that workaround hid the product gap): the pane captured at the registered
stage is now the proof of a straight workspace start on a genuinely fresh
HOME. Evidence:
`docs/evidence/dsh-tui-workspace-start-2026-10-19/`.

Structure note: the additions pushed `interactive.py` past the 1000-line
module limit, so the whole dsh launch contract moved to
`shell/impl/cli/commands/agent/runtime/dsh/launch.py` (pure move;
`interactive.py` keeps the shared launch runtime the other harnesses
consume).
