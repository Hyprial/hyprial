# Cell observability: first slice for 0.5.5.3

Status: **design proposal, implementation not started**. Date: 2026-10-30.
Owner: R&D. Implementation requires Allen's approval and a separate workflow.

## 0. Decision boundary and sources

The immediate requirement is programmatic visibility into an agent in its own terminal:
in-turn, idle, blocked, possibly stuck, or not running. A cell cannot infer these facts
from its existence alone. This slice adds a stable attachment layer and reports the
quality of its observations. It does not change delivery or recovery policy.

Binding instructions, recorded here so implementation does not depend on chat history:

- Allen: cell is a general organization mechanism. First move the agent attachment
  from the harness lifecycle process to a cell actor. The cell monitors that process.
- Allen: add this abstraction without changing existing designs. Cell-as-department,
  whose organelles are agents, comes later.
- Allen approved the OTP semantics document as the planning basis, with the Rust
  library's API taken into account. The library name is **ractor**, not Reactor.
- Allen moved this work from 0.5.5.4 to 0.5.5.3 because terminal agents' state is opaque.
- There is no supervision implementation to cherry-pick from the rustify branches.
  They provide design inputs, not completed prerequisites.

Sources read:

| Source | Pinned revision and relevant scope |
|---|---|
| Current dev code | `c0026bc6c5b55b67020c474d79ed19d0bddeef09`. All code line references below use this revision. |
| `intg/cell`, PRs #1934/#1975 | `57b3e6331fac30595fdb053bb505b74587a6f651`, `docs/design/design-cell-2026-10-15.md` and `docs/design/cell-impl/README.md`. Preserve their authority and identity boundaries. Do not enable their department, routing, tide, memory, or multi-harness packages here. |
| PR #1988 | `d040e0331a073939998eaf928cb2f9434a6df99e`, `docs/design/actor-rustify-otp-semantics-2026-10-27.md`, especially §§3, 5, 9. Monitor means observe-lifecycle. Section 9 permits only native ractor supervision, not a new supervision framework. |
| `intg/actor-rustify` | `42f8e0ecc18d8f9d5abb2726d9e3d1a739b7e9a0`, `docs/design/actor-rustify-runtime-decision-2026-10-24.md`. Selects ractor 0.16.5, Tokio, and a thin PyO3 boundary. |
| Repository rules | `AGENTS.md`: five domains, public interfaces, 7 direct files, 1000 lines, red-first tests, isolated runtime tests. |

The source documents are on separate branches. Resolve them with `git show <revision>:<path>`.
Their historical implementation commands are not instructions to restore retired GUI code.
This document changes none of those documents or their rulings.

## 1. Signal inventory

### 1.1 Definitions and limits

- **OBSERVABLE-NOW**: the current producer contains the necessary fact. A new projection
  may expose it, but no new harness event is needed. This does not mean `ps` exposes it today.
- **NEEDS-SIGNAL**: current reporting does not cover the claim. Add authenticated telemetry
  or a current-state refresh before reporting it as fact.
- **NOT-POSSIBLE**: the proposed observation cannot establish the claim. Examples are
  proving a deadlock from silence, or finding an unregistered terminal's agent identity.

“In-turn” means an admitted/open run, including tool waits and preparation. It does not
mean the model is generating tokens. “Idle” means a known running runtime has no open
run in the producer's covered scope. It does not mean its inbox or PAC graph is empty.
“Not-running” requires positive evidence for the bound execution instance. Lease expiry
only establishes loss of contact. A registry record alone establishes neither execution
nor non-execution.

### 1.2 Code evidence

Paths in this table are repository-relative. The IDs support the matrix in §1.3.

| ID | Evidence | Fact and limitation |
|---|---|---|
| I01 | `src/hyprial/daemon/impl/lifecycle_manager/process.py:60-145,156-168,402-422` | Legacy `LifecycleProcessManager` is a durable saga runner with thread health and a best-effort event sink. Its health is not an individual agent's turn state. |
| I02 | `src/hyprial/daemon/impl/application/actors/lifecycle_ops.py:211-228,476-488` | Current composition constructs **LifecycleCoordinator**, not the legacy runner. The ps-visible lifecycle health is manager running/crashed/lastError. Do not attach every cell to one aggregate health bit. |
| I03 | `src/hyprial/daemon/impl/lifecycle/lifecycle_coordinator.py:469-560` | Current coordinator schedules per-resource sagas, retains durable reservations, and uses an existing ActorRuntime. `is_running` means some effect worker is alive. This is not proof that a particular harness runs. |
| I04 | `src/hyprial/daemon/impl/harnesses/turn_delivery/turn/turn_runtime.py:398-454,870-873` | Turn owner sets `in_flight`, emits generation/versioned `TurnStarted`, clears on completion, and offers `read_in_flight`. This is the headless run fact. Multiple message IDs can belong to one turn. |
| I05 | `src/hyprial/daemon/impl/harnesses/streaming/process.py:209-235,251-267,304-349` | Streaming process has running/idle and liveness views. Idle reads the turn owner. Liveness can explicitly be UNKNOWN. Completion hooks are advisory and exceptions do not undo settlement. |
| I06 | `src/hyprial/daemon/impl/harnesses/streaming/turn_io.py:379-393` and `src/hyprial/daemon/impl/hooks/service.py:184-286` | TurnStarted feeds the unified hooks path. Hook publication is gated and carrier payloads omit reportId/sessionRef. Do not reconstruct a reliable per-instance ledger from hooks alone. |
| I07 | `src/hyprial/daemon/impl/application/actors/sessions/turns.py:30-40,51-138,166-199` | Interactive start/end IPC fences the session, stamps daemon time, and deduplicates report IDs. The 1024-entry start/end ledgers are daemon-local memory, not durable state or complete recovery evidence. |
| I08 | `src/hyprial/shell/impl/cli/commands/agent/runtime/claude.py:307-345` | Managed interactive Claude launch installs UserPromptSubmit and Stop pulses. Thus Claude is not “end-only.” Bare Claude without this integration is outside coverage. |
| I09 | `src/hyprial/daemon/impl/mcp/channel/adapter.py:477-520,554-562` | Channel pairs pulses into an open turn, handles late starts after a read, closes a previous unended turn as failed, and confirms start/end through session IPC. It does not report permission-prompt lifecycle here. |
| I10 | `src/hyprial/daemon/impl/harnesses/pi/resources/pi_harness_attach.ts:306-319,412-420,684-739` | Pi opens a turn before consuming a pending batch and on agent_start, ends on agent_settled, and unregisters best-effort on shutdown. A started report can precede actual model execution. Shutdown/IPC failure can lose the last observation. |
| I11 | `src/hyprial/shell/impl/cli/commands/agent/runtime/sessions.py:819-850,875-925` | Interactive Codex registers with optional PID/birth identity and starts a carrier in foreground mode. The detached tmux path returns earlier. Do not assume identical coverage for both modes. |
| I12 | `src/hyprial/daemon/impl/harnesses/codex/carrier.py:119-170,729-748` and `src/hyprial/daemon/impl/harnesses/carrier/codex_carrier_support.py:95-131` | Codex carrier records/retries turn reports for **its injected deliveries**. Polling ends the known native turn. This does not enumerate all user-submitted TUI turns. |
| I13 | `src/hyprial/daemon/impl/harnesses/codex/settlement.py:44-56` and `src/hyprial/daemon/impl/harnesses/codex/app_server.py:560-574` | Reconciliation iterates carrier-owned TURN_STARTED records. `read_turn` reads a thread but selects a supplied turn ID. External TUI turn discovery/reporting still needs wiring. |
| I14 | `src/hyprial/daemon/impl/harnesses/codex/client.py:667-750` | Managed Codex emits producer-local turn-stalled/turn-resumed from correlated notification silence. Poll replies saying inProgress are not activity. This is suspicion, not a confirmed deadlock. |
| I15 | `src/hyprial/daemon/impl/harnesses/streaming/protocol.py:108-125` | Wall-clock turn killing is retired. The idle-timeout setting controls reporting only. Cell diagnostics must not reintroduce timeout cancellation. |
| I16 | `src/hyprial/daemon/impl/harnesses/codex/native_env.py:648-691` | Unattended Codex declines most approval requests and narrowly accepts its own bridge MCP approval. A refusal is not a durable “waiting for a human” condition. No generic permission wait state follows from it. |
| I17 | `src/hyprial/daemon/impl/inbox/tracking/progress.py:1-25,49-73,80-99` and `src/hyprial/daemon/impl/dispatch/runtime/progress.py:44-89` | Progress is delivery-scoped, lossy, coalesced, and non-authoritative. Publication can reject. Its silence cannot establish a stalled runtime, nor can its terminal flag settle a turn. |
| I18 | `src/hyprial/identity/impl/agents/state/liveness.py:282-311` | Existing liveness first uses managed-worker evidence. Interactive liveness combines process evidence with fresh heartbeat. False can mean stale heartbeat, not proven OS death. Reuse this authority, preserving the distinction in diagnostic reasons. |
| I19 | `src/hyprial/daemon/impl/application/actors/sessions/registry.py:101-151,900-923` | Session status separates registration, epoch, heartbeat age, and channel liveness. A durable session can remain registered while stale or unverified. |
| I20 | `src/hyprial/daemon/impl/status/__init__.py:75-112,127-139` | Actor status gives supervised connectors running/stopped. Interactive sessions without a supervised process retain not_applicable. Do not rewrite that to stopped. |
| I21 | `src/hyprial/daemon/impl/application/messaging/status/snapshots.py:70-129` and `src/hyprial/daemon/impl/application/messaging/status/views.py:465-476` | ps builds shared worker facts and includes agents plus interactiveSessions. This is the preferred additive publication surface. |
| I22 | `src/hyprial/daemon/impl/application/actors/agents_registry.py:849-877` | Existing workerState=blocked is restore/provider blockage with a reason. It is not a permission prompt. Preserve its meaning. |
| I23 | `src/hyprial/daemon/impl/pac/actors/daemon.py:60-70,96-119` | PAC observes desired worker presence and interactive use through the existing runtime adapter. A running worker can die without a PAC event. PAC request state is not turn state. |
| I24 | `src/hyprial/kernel/impl/actor_runtime/runtime.py:54-89` | The current runtime exposes start/tell/snapshot/stop/reset/drain, not the proposed data-only observe(after_revision) resource interface. OTP semantics are a target, not an already implemented monitor API. |
| I25 | `src/hyprial/shell/impl/cli/commands/agent/runtime/interactive.py:525-567` | Detached Claude launch detects two startup confirmations and returns CLAUDE_CONFIRMATION_REQUIRED while preserving the pane. This is a launcher result, not continuous permission telemetry or a bound-cell status feed. |

### 1.3 State matrix at this revision

The matrix describes intact, current-session signals. Lost reports, restart, or stale
contact weaken a result to unknown. “Stuck” below means **suspected stuck** only.

| Agent kind / covered scope | In-turn | Idle | Blocked-on-permission | Suspected stuck | Not-running |
|---|---|---|---|---|---|
| Managed streaming headless (Claude/Codex/Pi) | OBSERVABLE-NOW: I04/I05 | OBSERVABLE-NOW: running plus empty in-flight, I05 | NEEDS-SIGNAL: no common wait-open/wait-close report, I16 | OBSERVABLE-NOW: turn age. Codex also has producer quiet watch, I14. Other runtimes need activity telemetry for a quiet-time claim. | OBSERVABLE-NOW for positive managed stop/exit, I05/I18. Unknown liveness remains unknown. |
| Interactive Claude via installed channel hooks | OBSERVABLE-NOW for covered turns, I07-I09 | OBSERVABLE-NOW after confirmed end with no newer start and fresh session. Initial/recovered idle NEEDS-SIGNAL. | NEEDS-SIGNAL | OBSERVABLE-NOW for open-turn age only. Tool/model silence NEEDS-SIGNAL. | OBSERVABLE-NOW only with verified process exit. Heartbeat loss alone NEEDS-SIGNAL for OS death, I18/I19. |
| Interactive Pi with attach extension | OBSERVABLE-NOW as open run/preparation, I07/I10 | OBSERVABLE-NOW after settled and fresh session. Initial/recovered idle NEEDS-SIGNAL. | NEEDS-SIGNAL for any extension permission UI | OBSERVABLE-NOW for open-run age, not proof of execution stall | OBSERVABLE-NOW with verified exit. Missing shutdown/heartbeat alone NEEDS-SIGNAL. |
| Interactive Codex foreground, carrier-injected turns | OBSERVABLE-NOW: I12 | OBSERVABLE-NOW only for carrier queue, **not whole TUI** | NEEDS-SIGNAL | OBSERVABLE-NOW for known turn age. Do not assign managed-client quiet-watch coverage to this adapter. | OBSERVABLE-NOW if bound PID/birth exit is verified, I11/I18 |
| Interactive Codex user-entered turns, including detached TUI | NEEDS-SIGNAL: enumerate/report native TUI activity, I11-I13 | NEEDS-SIGNAL for whole-session idle | NEEDS-SIGNAL | NEEDS-SIGNAL for whole-session run age | Same positive process-evidence rule as foreground |
| PAC-owned workers | OBSERVABLE-NOW to the underlying managed runtime's coverage | OBSERVABLE-NOW to that runtime's coverage, regardless of requested/running graph node | NEEDS-SIGNAL to that runtime's coverage | OBSERVABLE-NOW for measured turn age, not from graph deadline alone | OBSERVABLE-NOW through runtime evidence, not task completion, I23 |
| PAC borrowed actors | Inherit the actual carrier's row | Inherit the actual carrier's row | Inherit the actual carrier's row | Inherit the actual carrier's row | Inherit the actual carrier's row. PAC cannot stop or re-own them. |
| Unregistered bare terminal / remote actor with presence only | NOT-POSSIBLE from these daemon signals | NOT-POSSIBLE | NOT-POSSIBLE | NOT-POSSIBLE | NOT-POSSIBLE for OS execution. Presence loss is not exit. |

For **every** kind, “confirmed internal deadlock” is NOT-POSSIBLE from timing alone.
A new heartbeat also cannot prove useful work. Progress summaries do not prove intent,
correctness, permission status, or completion. Non-streaming managed harnesses must
advertise their own coverage, not inherit the streaming row by name.

## 2. Cell is an additional attachment layer

### 2.1 Identity, mounting, and authority

```text
same canonical agent URI, grants, inbox, route and PAC ownership
                         |
                    cell actor
              stable attachment resource
                 /               \
  observe harness lifecycle     read run facts
                 |               |
       existing harness/session/turn owners
                 |
       existing harness process in host/container/terminal
```

One cell resource represents each local agent incarnation. The public URI does not
change. The cell is not a new agent registry record, sender, inbox, or PAC worker.
An internal mount record associates that incarnation with the current harness/session
binding revision. It keeps existing process identity and generation fences. The agent
remains externally one member/identity. This does not constrain its future internal
harness count or assert that every cell is internally a single-member cell.

The cell becomes the agent's attachment target. Its harness reference is a monitored
child resource, not the public identity's lifetime. A harness exit does not delete the
cell or settle messages. Destroying the agent uses the existing lifecycle operation.
Cell registration does not claim that a runtime exists.

“Cell actor” here means a stable keyed logical actor/resource, not a new Python actor
class or mailbox. In 0.5.5.3, host its mount and immutable diagnostic projection inside
existing serialized owners. Use ordinary pure reducers and data-only ports. Do not
instantiate another ActorSpec, queue, dispatch thread, handler factory, or callback
subscription for the cell. A native ractor actor can later host the same resource.
This is an abstraction-layer delivery, not a claim that native cell execution ships now.
If implementation requires a new execution loop, stop and return to design review.

The monitored key identifies one harness lifecycle **instance**, not the global
LifecycleCoordinator. Use `(host, group, key)` with `group=harness.lifecycle`, a canonical
agent incarnation in `key`, and epoch for the current runtime binding. Never use a
short display name or PID alone. Internally retain identity's entity token. Publish
only a separate opaque diagnostic incarnation ID, not authority credentials.

The coordinator's aggregate health is an additional dependency observation. If it
fails, report degraded observation/control capability. Do not mark all child processes
dead. Interactive terminals do not become daemon-owned subprocesses by being mounted.
For them, the child resource describes the existing carrier/session and known process
facts, with custody explicitly `external`.

No behavior changes to spawning, respawning, stop, restore suppression, provider blocks,
turn injection, settlement, ACK, routing, grants, thread ownership, or home layout are
included. In particular, do not run the older cell sandbox migration, start organelles,
implement tissue, enable tide, or move inbox authority. Observability does not grant
permission to restart, cancel, acknowledge, reply, or approve anything.

### 2.2 Minimal Python monitor interface

Implement the read-only subset of OTP §3 on the current owners. Do not pretend I24
already provides it. This is the first slice's explicit API addition:

```text
LifecycleTarget = (host_id, group, key)
Cursor = (epoch, revision)

snapshot(target, namespace="lifecycle") -> LifecycleSnapshot
observe(target, namespace="lifecycle", after_revision, epoch,
        limit, wait_ms) -> ObservationBatch

LifecycleSnapshot:
  target, epoch, revision, observedAtMs, state, cause, available
ObservationBatch:
  events, cursor, resyncRequired, current
LifecycleEvent:
  target, epoch, revision, transition, cause, observedAtMs
```

These names describe a narrow port, not Python spawn/send/receive/GenServer APIs.
A `monitor` is the use of `observe` for `namespace=lifecycle`. No callable crosses the
port. No `link(pid)`, trap_exit, Python mailbox, or event-handler registration is added.
Zero-wait observation is sufficient for 0.5.5.3. A bounded wait may reuse existing
resource notification primitives, but must not create a waiting thread per cell.

Rules:

1. First observation returns current state even when the target already stopped or
   does not exist. `cause=noproc` means no watched resource, not proof of OS death.
2. Snapshot and cursor share one publication boundary. Install observation before
   reading current state, or compare revisions and retry. A stop between these steps
   must appear in the snapshot or subsequent events, never disappear.
3. Revision is monotonic per key within an epoch. The cursor is not an inbox cursor.
   Epoch change, retention loss, or an invalid old cursor returns resyncRequired.
4. Keep only bounded diagnostic history. A proposal is 128 transitions per key and
   64 simultaneous observers per key, with explicit overload and closed results.
   These are reviewable capacity settings, not a new admission policy for work.
5. A slow reader resynchronizes from the current snapshot. It cannot backpressure
   harness execution or acquire durable custody. No diagnostic retention can delete
   inbox data or lifecycle receipts.
6. Shutdown releases observation resources. Observation cancellation never stops the
   watched process. Missing or unavailable source state remains unavailable.
7. Expose only transitions the owner actually knows. Do not fabricate quarantined,
   restarting, or execution_lost from business error strings or elapsed turn time.
8. Do not add restart intensity, retry scheduling, failure propagation, or a Python
   supervision tree. OTP's remaining supervision clauses are outside this subset.

### 2.3 ractor 0.16.5 mapping and non-equivalences

Checked the versioned ActorCell source and SupervisionEvent documentation on 2026-10-30:

- [ActorCell source](https://docs.rs/ractor/0.16.5/src/ractor/actor/actor_cell.rs.html),
  especially lines 381-417 (link, monitor, unmonitor).
- [ActorCell API](https://docs.rs/ractor/0.16.5/ractor/actor/actor_cell/struct.ActorCell.html).
- [SupervisionEvent](https://docs.rs/ractor/0.16.5/ractor/actor/messages/enum.SupervisionEvent.html).

These sources, not search summaries, support the exact names below. No Rust build or
native monitor experiment ran for this design.

| Our concept | Exact native concept / API | Boundary |
|---|---|---|
| A watched runtime incarnation | `ractor::actor::actor_cell::ActorCell`, `ActorCell::get_id()` | ActorId identifies a native incarnation. The adapter binds it to our stable key and epoch. It is not an OS PID. |
| Cell watches harness resource | `observer.monitor(watched)` / `ActorCell::monitor` | Enable the `monitors` feature. Observer is `self`, target is `who`. Monitoring does not make the target a supervised child. |
| Detach observer | `observer.unmonitor(watched)` / `ActorCell::unmonitor` | Exact name is **unmonitor**, not demonitor. Python cancels observation, not the target. |
| lifecycle.started | `SupervisionEvent::ActorStarted(ActorCell)` | Native start means resource actor start. Harness readiness is a separate process/carrier fact. |
| lifecycle.stopped / down(exit) | `SupervisionEvent::ActorTerminated(ActorCell, Option<BoxedState>, Option<String>)` | Preserve the available exit reason; do not assume every termination was requested. Monitor copies omit non-cloneable state. No Python BoxedState or handler is exposed. |
| lifecycle.down(failure) | `SupervisionEvent::ActorFailed(ActorCell, ActorProcessingErr)` | Resource failure is not automatically harness death. OS exit must come from the process owner. |
| Parent-child supervision, if a later native package needs it | `child.link(supervisor)` / `ActorCell::link`, `ActorCell::unlink`, `ActorCell::try_get_supervisor`, `ActorCell::get_children` | One supervisor, potentially multiple monitors. This is not Erlang's symmetric peer link. Do not call link just to implement read-only monitoring. |
| Native event receipt | `Actor::handle_supervisor_evt` | Rust implementation detail only. Do not expose a Python callback. The adapter publishes data for observe. |

The primitives map one-for-one as shown. The **full observe contract is not identical
to native monitor**. ActorCell::monitor registers a monitor, without a revision cursor,
durable replay, or an immediate OTP noproc result. Our existing resource-state boundary
must supply initial state, cursor retention, and resync. Those are observation adapters,
not supervision policy. A native acceptance test must cover register-versus-exit races. The retained transition
window is immutable diagnostic history, not a queue of work or a Python mailbox.

There is no corresponding built-in SupervisionEvent for in-turn, idle, permission wait,
or suspected stuck. These remain daemon domain facts. This slice does not translate
such facts into ActorFailed, nor use Factory replacement as turn recovery. Broader
OTP restart/alarms remain deferred, subject to the native-only ruling.

## 3. Run-state projection

### 3.1 Shape and precedence

Publish one additive `runState` object per local agent. Existing `status`, `state`,
`processState`, `workerState`, and `restoreStatus` retain their exact meanings.

```json
{
  "state": "in-turn",
  "baseState": "in-turn",
  "reason": "turn-open",
  "certainty": "observed",
  "scope": "whole-session",
  "runtimeIncarnation": "opaque-diagnostic-id",
  "epoch": "daemon-observation-epoch",
  "revision": 17,
  "observedAtMs": 1793344600000,
  "sinceMs": 1793344580000,
  "turnAgeMs": 20000,
  "lastActivityAtMs": null,
  "coverage": {
    "turnBoundaries": true,
    "permissionWait": false,
    "producerActivity": false,
    "processExit": true
  },
  "observationHealth": "current",
  "suspicions": [],
  "policyRevision": 1
}
```

All fields above are proposed, not current contract. Time values are illustrative.
`scope` is whole-session or carrier-deliveries. A carrier-only idle observation must
not produce whole-session idle. Expose `unknown` with reason `partial-turn-coverage`
for the whole agent, while retaining the scoped observation as diagnostic detail.

`state` is one of `unknown`, `starting`, `idle`, `in-turn`, `blocked`, `suspected-stuck`,
`stopping`, `not-running`. `baseState` preserves the factual state under a suspicion.
Reason is a closed code, not a provider's raw text. `certainty` is observed, inferred,
or unknown. Unknown times remain null. Unsupported capabilities are false, not zero
activity. Current means current observation, not certainty about the model's intent.

Deterministic precedence:

1. Verified exit or authoritative absence of the managed execution instance → not-running.
2. An accepted start/stop operation with unresolved process outcome → starting/stopping.
   Operation completion alone cannot prove harness readiness (I02).
3. Unavailable source, lost epoch, stale carrier, incomplete coverage, or conflicting
   current-instance facts → unknown. Preserve last-known evidence separately.
4. Explicit open permission wait or existing provider/restore block → blocked, with
   distinct reason and baseState. Do not overwrite the existing provider-block fields.
5. Current turn plus exceeded diagnostic threshold → suspected-stuck.
6. Current turn → in-turn. Otherwise a current, complete runtime view → idle.

A provider-blocked but stopped agent has state=not-running and retains its block detail.
An open permission wait stays blocked even after a long wait. Add permission-wait-long
as a suspicion rather than relabeling it a deadlock.

### 3.2 Transitions and driving facts

| From → to | Required input | Existing source / gap |
|---|---|---|
| unknown/not-running → starting | Accepted lifecycle start for current binding | I02/I03, projected without changing the saga |
| starting → idle | Ready, running, and complete no-open-turn snapshot | I04/I05 for managed. Interactive initial state needs §4 refresh. |
| idle/unknown → in-turn | Current-instance open turn | I04 or I07. Preserve prepared versus executing distinction for Pi. |
| in-turn → idle | Matching confirmed end and no newer/open turn, fresh runtime | I04/I07-I10. End of one message is not necessarily end of a batch turn. |
| in-turn/idle → blocked | Authenticated wait-open or provider block | Permission is new telemetry. Provider block already exists, I22. |
| blocked → in-turn/idle | All matching waits close, or authority clears provider block, plus current run view | Permission wait-close is new telemetry. Never clear on a progress summary. |
| in-turn → suspected-stuck | Age threshold or producer-local quiet condition | I04/I07 age, I14 quiet condition, with rules below |
| suspected-stuck → in-turn/idle | Quiet condition clears or turn ends | I14 resumed, matching end. New activity cannot clear an age-over-budget reason. |
| any → stopping → not-running | Existing stop intent then verified completion/exit | Existing lifecycle/process owners, no new stop policy |
| any → unknown | Epoch change, observation gap, stale heartbeat, unavailable source | I07/I17-I19. Reconcile before restoring a stronger state. |
| any → not-running | Verified bound process exit | Positive process evidence only. Late turn reports cannot revive an exited incarnation. |

Consume turn facts at their owners before lossy hooks/progress fan-out. For managed
runs, use the turn projection, not hook delivery counts. For interactive runs, project
validated IPC receipts with reportId and session fence before hook payload reduction.
This adds state derivation, not another turn authority or durable business ledger.

An old-generation end cannot close a new-generation start. Duplicate reports do not
reset sinceMs. End-before-start replay must not reopen an ended report ID. On conflicting
or evicted history, request current state and publish unknown until it arrives. Do not
subtract bounded start/end dictionaries and assume completeness forever.

### 3.3 Diagnostic thresholds

Proposed per-carrier/mode settings, overridable per agent through validated configuration:

| Setting | Proposed value | Use |
|---|---|---|
| `turnLongAfterMs` | 1,800,000 | Open run over 30 minutes adds turn-over-budget. It is a warning, not an execution deadline. Null disables it. |
| `quietAfterMs` | 900,000 where producer activity is supported | Producer-local elapsed time since correlated activity. Reuse the existing Codex idle-timeout setting when present. Do not run a competing detector. |
| `permissionLongAfterMs` | 300,000 | Explicit permission wait older than five minutes adds permission-wait-long. No automatic approval/denial. |
| `refreshIntervalMs` | 5,000 | Maximum normal projection refresh interval. One bounded group scan, not per-agent timer threads. |
| `observationStaleAfterMs` | Existing lease TTL for carrier contact | Do not invent a second liveness timeout. A separate fresh run snapshot is required after resync. |

Numbers are recommendations for Allen, not production measurements. A long tool call
can be valid. A fresh heartbeat is contact, not work progress. Consumer-side silence
on route C never drives quietAfterMs because I17 permits drops and coalescing.

Use monotonic elapsed time within a source epoch. Record wall time for presentation.
A daemon restart does not compare old monotonic values or reset a known run's age to
zero. Recover a carrier state snapshot with elapsed time and mark its provenance, or
report unknown age. Fresh activity clears only the corresponding quiet suspicion.
No threshold cancels a turn, restarts a harness, changes PAC deadlines, or sends a prompt.

### 3.4 Publication and reads

- Extend `ps`'s existing `agents[]` rows with runState. Reuse the shared request snapshot
  and one cell projection revision cut. Also use that projection for agent status/top,
  rather than independently deriving state in each renderer.
- Other local programs use `hyprial ps --json`, selecting the canonical actor URI.
  JSON remains one object with ok. An old daemon's missing field means unsupported,
  never idle. A disconnected daemon is unavailable, never an all-stopped fleet.
- The internal monitor uses §2.2. A public resumable run-state event command is not
  required for 0.5.5.3. Add one later only through the existing observation conventions
  and contracts, not a second inbox or progress-message protocol.
- Remote presence continues to mean presence. This release does not publish full
  run-state across nodes or infer remote OS health.
- No new Lark notification defaults. Diagnostics remain visible in status. Any later
  supervision notification uses configuration, never a hard-coded owner or group.
- Expose bounded reason codes and timestamps. Do not expose prompt text, tool arguments,
  permission details, tokens, native resume handles, or full session credentials.

## 4. Interactive-terminal gaps and minimum additions

### 4.1 What the cell knows

A cell knows facts only from a bound producer. Claude hooks and Pi attach already report
local user turns, with the preparation and loss caveats above. Codex's current carrier
reports its own injected turns but not every user-entered TUI turn. The tmux branch
must receive a separate coverage test. A live terminal window, tmux pane, or PID cannot
prove a live model turn. A live channel subprocess cannot prove its parent model is idle.

Neither a cell nor ractor sees permission UI automatically. I25 is a narrow existing
exception: the launcher can identify two Claude tmux startup confirmations. Forwarding
that trusted result into cell status would need wiring; it does not cover in-turn
permission prompts. Do not generalize its pane matching into transcript surveillance.
Lack of a prompt event must produce permission coverage=false. The initial release must display that limitation,
not claim “not blocked.” Bare terminals without registration remain outside coverage.

### 4.2 Minimum carrier telemetry

Reuse session.turn.started/ended for boundaries. Add a data-only, session-fenced current
run-state refresh, with capability negotiation, for initial registration and recovery.
Recommended payload concepts:

- Native run/report ID, current open-run set, and producer sequence within its boot epoch.
- Capability flags for whole-session turns, preparation/execution distinction, permission
  waits, and producer activity. A missing capability is not an empty active set.
- Explicit wait-open/wait-close with a stable wait ID, run ID, and kind=permission.
  Several simultaneous waits require a set, not one boolean. Close includes cancelled
  or answered, without the secret answer or command contents.
- Current waits and run phase on refresh. This prevents a lost close event from leaving
  a permanent blocked state. Daemon receipt time is authoritative for observation time.
- Producer-local elapsed activity/turn duration, if available, with epoch and provenance.
  Unsupported elapsed time stays null. Do not scrape full terminal text or transcripts.

The daemon binds all reports to the authenticated current session and process generation.
Do not accept actor, owner, incarnation, or wall time as authority merely because a caller
supplied them. Retain existing lease/fence checks. Telemetry never renews a lease unless
it uses the existing heartbeat operation. Old reports cannot cross a harness switch.

Carrier-specific work:

| Carrier | Minimum work | Release boundary |
|---|---|---|
| Claude channel | Reuse start/Stop pairing. Add a current open-turn refresh and explicit failed-end recovery. Probe supported native permission lifecycle hooks before adding wait reports. | Refresh fits 0.5.5.3. Permission support needs its own real prompt test. Unsupported native hooks remain a visible gap. |
| Pi attach | Refresh open run and preparation state after reconnect. Use a supported permission mechanism only if the installed extension/runtime provides one. | Refresh fits 0.5.5.3. Do not invent a universal Pi permission event. |
| Codex foreground/tmux | Observe all native TUI turn IDs through the supported app-server view/events and report boundaries, including user input. Keep carrier delivery settlement unchanged. | Whole-session coverage is part of the recommended 0.5.5.3 acceptance gate. Permission waits are a separate adapter test. |
| All carriers | Report explicit waits where supported, clear by ID, refresh after disconnect, preserve capability=false otherwise. | Land independently per proven mode. Unproven modes remain blocked for that capability, not for honest partial status. |

If native APIs cannot report permission state, NEEDS-SIGNAL becomes NOT-POSSIBLE for
that adapter/version without an upstream extension. Do not infer it from a stalled tool.
Future permission telemetry changes no approval policy.

## 5. PR-sized implementation plan

Dependency order: **A → B → C → D**. E1/E2 use A's values and can proceed alongside C.
C supports incomplete coverage, but whole-session terminal acceptance requires E1/E2.
E3 packages are independent after the telemetry schema review. Each PR includes its own
contract and tests. All paths marked new are proposals, not existing implementation.

### A. Read-only lifecycle monitor subset — 0.5.5.3

Files/modules:

- New `src/hyprial/kernel/impl/observation/{__init__,contracts}.py` for owned values and
  read-only protocol. Export through `kernel/interface.py`.
- New `src/hyprial/daemon/impl/lifecycle/observation/{__init__,ports,projection}.py`.
- Adapt `lifecycle/lifecycle_coordinator.py`, `application/actors/lifecycle_ops.py`, and
  harness/session projection owners. Do not extend only the unused legacy manager.
- New tests under `tests/daemon/lifecycle/observation/` and kernel contract tests.

Red-first acceptance:

1. Observe an already stopped or absent key: first result is current stopped/noproc.
2. Force exit between snapshot and observe: result includes it or requires resync.
3. Overflow retention and change epoch: explicit resync, never empty-success-as-idle.
4. Saturate observers and close one: bounded refusal/release, target keeps executing.
5. Crash aggregate coordinator while a fake child still runs: child not marked dead.
6. AST/import checks reject callable fields and any new mailbox/handler registration.

Risks: confusing operation state with process state, implementing a second supervisor,
and attaching to legacy code. No native ractor support is claimed by these tests.

### B. Cell mount without execution changes — 0.5.5.3

Files/modules:

- New `src/hyprial/daemon/impl/cells/observability/{__init__,ports,mount,model}.py`.
- New `src/hyprial/daemon/impl/composition/cell_observation/{__init__,wiring}.py`.
- Thin wiring in `application/actors/lifecycle_ops.py`, `application/actors/sessions/registry.py`,
  and existing composition views. Public exports use the daemon interface.
- Tests under `tests/daemon/cells/observability/mount/`.

Red-first acceptance:

1. Start/stop/rebind the existing fake runtime through production composition. Public
   actor URI, session fencing, inbox row IDs, sender, grants, and route count stay identical.
2. Kill the runtime: the cell attachment survives with a down observation. No extra
   spawn/stop/reply/ACK occurs. Existing lifecycle recovery remains the sole decision-maker.
3. Recreate an agent with the same short name: old incarnation cannot update the new cell.
4. Attach an external terminal: custody remains external. Detach monitor leaves it alive.
5. Fail mounting midway and restart: no duplicate route, orphan observer, or home migration.

Risks: double registration or ownership transfer by accident. Store mount metadata only
through the existing state owner if persistence is necessary. Runtime facts stay derived.

### C. Run-state derivation and ps publication — 0.5.5.3

Files/modules:

- New `cells/observability/{reducer,policy,projection}.py`, keeping that directory at 7 files.
- Thin owner-side fact adapters near `harnesses/turn_delivery/turn/`, `harnesses/actor/`,
  and `application/actors/sessions/turns.py`. Cells receive values, not private owner objects.
- `status/__init__.py`, `application/actors/agents_registry.py`, and
  `application/messaging/status/snapshots.py` expose the additive projection.
- Tests in separate `tests/daemon/cells/observability/{state,recovery,publication}/` directories.
- Add the runState schema and vectors to `contract/` in the same PR.

Red-first acceptance:

1. Table-driven tests cover every §3.2 transition and precedence conflict.
2. Duplicate start/end, end-before-start, batch delivery fan-out, old generation, and
   evicted report IDs cannot manufacture busy/idle transitions.
3. Drop all hook and route-C progress events: managed state still follows its owner.
   Route-C silence alone never marks stuck.
4. Fake clock crosses each threshold exactly. No process stop, PAC mutation, or message
   settlement occurs. New activity clears quiet suspicion but not age suspicion.
5. Restart during a turn, lose carrier contact, and deliver stale refresh: unknown until
   current evidence arrives. Old monotonic time never yields a fabricated duration.
6. Permission wait-open/close vectors work in fake telemetry. Missing capability remains
   unknown coverage. Existing provider block retains its original contract.
7. Repeated ps queries neither fetch messages nor clear notices. N agents use a bounded
   number of shared source snapshots, not N supervisor RPCs or N filesystem scans.

Risks: hooks omit identity and can lose events, partial turn coverage, and status-query cost.
No new durable turn ledger or interpretation of message text is permitted.

### D. CLI/status display — 0.5.5.3

Files/modules: the existing daemon/agent status and top commands under
`src/hyprial/shell/impl/cli/commands/daemon/` and `.../agent/`, their render helpers,
and `shell/impl/cli/output`. Select exact helpers after tracing command registration.
Tests belong under `tests/shell/cli/lifecycle/` subdirectories and status contract vectors.

Red-first acceptance:

- Golden fixtures show busy, idle, permission-blocked, provider-blocked, suspected-stuck,
  stopping, not-running, unknown, and partial carrier coverage without conflating them.
- Old daemon without runState displays unsupported/unknown. Daemon unavailable is an error,
  not an empty fleet. JSON is exactly one object with ok and the unchanged prior fields.
- Human output includes reason and age. It does not infer state by parsing log text.
- Update the operations skill with the supported commands and caveats in this PR.

Risk: repurposing existing status fields or implying global coverage from one carrier.

### E1. Claude/Pi refresh and loss recovery — 0.5.5.3

Files/modules: session IPC contracts/router, `application/actors/sessions/turns.py`,
`mcp/channel/adapter.py`, `mcp/channel/loops.py`, and
`harnesses/pi/resources/pi_harness_attach.ts`. Keep format/schema versions explicit.
Tests: existing channel and Pi carrier test directories plus isolated adapter fixtures.

Red-first acceptance: begin with a user turn, disconnect before end, reconnect without
new inbox traffic, and verify current state replaces stale state. Exercise initial idle,
failed start, queued preparation, user abort, duplicate pulse, daemon restart, and old lease.
No extra model turn or message consumption may occur just to discover status.

Risk: native event timing and installed SDK versions. Real Claude/Pi coverage requires
isolated native runs. Mocks prove protocol handling, not native event availability.

### E2. Codex whole-terminal turns — 0.5.5.3 recommended release gate

Files/modules: `harnesses/codex/app_server.py`, `carrier.py`, carrier report relay,
`shell/impl/cli/commands/agent/runtime/sessions.py`, and focused Codex tests.

Red-first acceptance: type a user prompt without any Harness inbox delivery in foreground
and tmux. Observe in-turn then idle. Repeat after reconnect and with an injected delivery
interleaved. One native turn gets one observation identity. Existing settlement receipts
and no-timeout-kill behavior remain unchanged. A detached mode without a producer reports
partial/unsupported, never idle by omission.

Risk: external app-server protocol support and ownership of TUI events. A thread read can
reveal current state, but polling cannot guarantee retention of every short historical turn.
Report snapshot scope honestly. If this gate fails, the release cannot claim full Codex
terminal observability, even if A-D pass.

### E3. Permission waits, one PR per proven carrier — 0.5.5.3 where verified, else 0.5.5.4

Files/modules: shared session telemetry schema from E1, then each carrier's native adapter
and launcher hook configuration. Tests use distinct subdirectories per carrier/mode.

Red-first acceptance: real pending approval → blocked(permission), answer/deny/cancel →
correct base state, concurrent wait IDs, lost-close refresh, late old-session close, and
startup trust prompt before registration. Pre-registration prompts cannot claim a bound
cell state without a trusted launcher observation. Unattended automatic denial is not
shown as an ongoing human wait. No telemetry changes approval decisions.

Risk: unavailable native hooks. Such capability stays explicitly unsupported. Do not hold
all observability for an impossible prompt signal, and do not count a skipped test as PASS.

### F. Native supervision integration — 0.5.5.4 or later, separate approval

Replace only the observation adapter with the ractor resource implementation when rustify
provides it. Verify monitor/unmonitor direction, already-dead target races, epoch mapping,
shutdown, and absence of Python callbacks. Do not extend the Python bridge into a general
supervisor. Native-only restart/Factory/alarm work follows the rustify design and review.
Cell-as-department, organelles-as-agents, multiple harnesses, and scheduling remain out of
scope even for this follow-up. They require their existing separate plans.

### 5.1 Structure, merge, and test discipline

At the code baseline, `daemon/interface.py` has 997 lines and status `views.py` has 993.
Both `daemon/impl/lifecycle/` and `daemon/impl/composition/` already contain 7 direct files.
Use subpackages for additions. Extract cohesive existing exports/render logic before a
limit is reached. Do not compress lines, rename code to data, or add a per-file exemption.
Keep tests/helpers/goldens under the same 7-file rule.

A owns the kernel contract and must coordinate with the rustify K slice's sole kernel
writer before changing shared ports. B owns composition. C owns the new status contract.
E1 owns carrier telemetry schema. These shared files merge serially. Every implementation PR
starts from the current release head and rechecks the source inventory and structure.
This design branch started from dev as requested. The accept node must check the delta
to `release/v0.5.5.3` before opening the PR or dispatching implementation.

Before product/test structure changes:

```sh
uv run --no-project --python 3.12 python scripts/ci/check_structural_limits.py
uv run --no-project --python 3.12 python scripts/ci/check_module_boundaries.py
```

Each implementation PR records a failing focused test before code changes, then its
passing result. Run focused tests first, then:

```sh
uv run --with libcst==1.9.0 pytest tests/
uv run ruff check .
./contract/run-all.sh --isolated-only
```

Runtime acceptance uses isolated home/socket/cache/ports and `HYPRIAL_NETWORK_ISOLATED=1`.
Never inspect production terminals, homes, credentials, or daemon state for evidence.
Record each real carrier/mode as PASS, FAIL, or BLOCKED. A-D unit success cannot substitute
for E1-E3 native evidence. Status queries must have no business side effects.

## 6. Questions for Allen

These questions concern release scope and policy, not the already-decided cell direction.
No implementation worker may treat the recommendations as authorization.

1. **0.5.5.3 terminal coverage gate?**
   - A: A-D plus E1/E2 for Claude, Pi, and Codex user turns (recommended).
   - B: Ship A-D first with explicit partial/unknown terminal coverage, then E1/E2 in 0.5.5.4.
   - C: Require every permission adapter as well before releasing any observability.
2. **First cell actor's execution form?**
   - A: Logical keyed cell resource hosted by existing owners, with no new Python mailbox
     (recommended additive abstraction). Native ractor hosting follows separately.
   - B: Require a native ractor cell actor now. Make the minimal native observation resource
     a blocking prerequisite, and re-estimate the 0.5.5.3 scope.
   - C: Deliver signal/status design only and defer the mount change. This is not completion
     of the requested cell mechanism.
3. **Long-run suspicion policy?**
   - A: Proposed 30-minute run-age warning, existing 15-minute Codex quiet sensitivity,
     and 5-minute explicit permission-wait warning, all configurable (recommended).
   - B: Disable run-age warnings by default. Show elapsed time and existing producer warnings only.
   - C: Specify different thresholds by carrier/mode before implementation.
   All choices retain the no-automatic-kill rule.
4. **Missing permission API in one carrier/version?**
   - A: Ship honest capability=false for that mode and deliver verified adapters separately
     (recommended). No claim of complete permission coverage.
   - B: Block that mode's observability acceptance until an upstream signal exists.
   - C: Exclude permission-state support from 0.5.5.3 entirely.
5. **Diagnostic notification scope?**
   - A: Local status/ps only in this slice (recommended). Broader native supervision alarms
     remain in the rustify plan.
   - B: Add a separate PR for configured owner notifications after native supervision review.
   - C: Also require cross-node run-state subscriptions now, with a separate visibility contract.

## 7. Design validation and handoff

This is a documentation-only change. No product or test implementation is included.
The inventory is static code inspection, not real-terminal acceptance. Versioned ractor
source was checked, but no Rust feature/build experiment ran.

- Product run-state tests and native carrier scenarios: **UNKNOWN / NOT RUN**.
- Native supervision and permission-event support: **BLOCKED pending implementation and isolated evidence**.
- Production state, user configuration, and credentials: not inspected or modified.
- Workflow inspect/complete calls are limited to this assigned workflow's control plane.
Static checks run on this documentation-only worktree:

| Check | Result |
|---|---|
| `git diff --cached --check` after staging the document | PASS |
| `uv run ruff check .` | PASS after installing the declared test extra with `uv run --extra test ruff check .`. The initial environment lacked ruff. |
| Structural-limits command in §5.1 | FAIL on unchanged baseline files: `daemon/impl/operations/service.py` (1010 lines) and `shell/impl/cli/commands/doctor/commands.py` (1042). Both paths are under `src/hyprial/`. |
| Module-boundaries command in §5.1 | FAIL: two existing public-face-bypass findings at `src/hyprial/shell/impl/cli/commands/daemon/service.py:14`. |
| Full product tests / cross-implementation contracts | NOT RUN: no product or contract implementation changed. |

Only this design document is changed. Baseline gate failures are reported, not fixed by
this design task. The committed SHA is recorded in the workflow handoff. These checks
are not runtime acceptance.

Contract-Impact: design only, no released field or behavior changes. A/C/E must update
contracts when implemented. Skill-Impact: current operations unchanged. D/E must update
the operations and E2E skills with coverage, recovery, and unsupported-state semantics.
