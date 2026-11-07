# Shared diagnostic run state (slice C)

`ps.agents[].runState`, `agent.list/get` and the canonical actor-status rows
consumed by `top.snapshot` carry the same additive diagnostic object. Existing
`status`, `running` and `processState` meanings do not change. Unregistered
adapter-only rows are not agent cells and do not acquire fictitious run state.

Fields:

- `state`: `starting | idle | in-turn | blocked | suspected-stuck | stopping |
  not-running | unknown`; `baseState` retains idle/in-turn under an overlay.
- `reason`: stable cause, exercised in `run-state-vectors.json`.
- `certainty`: `observed | inferred | unknown`. Suspected-stuck is inferred.
- `scope`: `whole-session | carrier-deliveries`; partial coverage cannot
  assert whole-session idle/busy. `providerBlocked` stays a separate boolean.
- `sinceMs`, `turnAgeMs`, `lastActivityAtMs`: nullable integers. Unknown times
  are null, never zero or the status-read timestamp. Elapsed turn age uses the
  current producer/daemon monotonic clock, not wall-clock subtraction.
- `observedAtMs`: projection receipt time, not a turn transition/activity time.
- `coverage`: booleans `turnBoundaries`, `permissionWait`, `producerActivity`,
  `processExit`. A heartbeat does not establish any missing capability.
- `observationHealth`: `current | stale | unavailable | partial | resync-required`.
- `suspicions`: independent warning reasons; no execution commands or decisions.
- `policyRevision`: positive integer; `thresholds` reports effective
  `turnLongAfterMs`, `permissionLongAfterMs`, `quietAfterMs` (nullable integers).
- `runtimeIncarnation`: opaque diagnostic hash, not an authorization token.
  `epoch` and `revision` identify one shared request publication cut. They are
  daemon-local, not durable recovery authority or lifecycle watch cursors.
- `lastKnown`: null, or the previous observed/inferred `state`, `baseState`,
  `sinceMs`, `observedAtMs`, `runtimeIncarnation`, retained when current state
  becomes unknown. Never use this history to make a current-state claim.

Precedence: verified exit; live start/stop operation; stale/unavailable,
conflicting or incomplete evidence; explicit permission/provider blocking;
age/producer-quiet warning; complete idle/in-turn evidence. Unbound, absent,
expired or restarted sources do not imply process exit or idle.

Run facts are passive immutable projections from existing owners. Reads do not
start workers, create cells, consume/ack inbox messages, clear notices, or infer
quiet from progress fan-out. Managed process facts use the existing bounded
owner sampling path and become stale after five seconds without an update.
Current generation/process/binding are rechecked after the group read. Unknown
native-hook recovery remains unknown; no log scan or new autonomous polling.
Interactive process outcomes come from the existing PID/birth probes used by
maintenance and `_interactive_process_alive`, via a session-owner cache. Status
reads never invoke those probes. Mere PID/birth registration is not advertised
as process-exit coverage. Samples require a matching entity, binding, session,
PID/birth and session-owner generation before and after sampling. Nonterminal
samples expire after five seconds. Verified exit remains conclusive for that
exact fence; fresh heartbeat/late receipts cannot override it, and rebind or
replacement cannot inherit it. This cache adds no process policy or new poller.

The lifecycle coordinator exposes one cache-only cut of current-instance,
accepted pending operations. Capture is observational, after reservation and
before dispatch; rejected and recovered old-instance requests cannot manufacture
new observations. Matching reservations supply starting/stopping. The harness
owner still supplies native startup while a newly created identity acquires its
binding. Operation fences include entity/binding and the native process instance,
not the command-generation counter that the same native stop increments. A
replacement process cannot inherit the old stop. Terminal operations disappear
from the cut. Verified exit still outranks pending stop; changed mounts discard
old positive exit/operation evidence. All business reservation/dispatch/settlement
and recovery behavior remains unchanged.

Interactive facts are fenced by entity/binding/session, bounded at 1024 IDs;
eviction loses completeness. A heartbeat gap detected by the session owner
remains unknown even when the heartbeat later becomes fresh; only the accepted
current-run refresh acknowledges that recovered contact gap (the owner requires
the owning session, the current generation and a heartbeat still inside the
lease TTL). Rebinding and
restart discard old turn facts. E1 provides the authoritative current-run
refresh (`session.turn.refresh`, see `interactive-turn-refresh.md`): it replaces
the open set after a disconnect, a lost end pulse or a daemon restart, clears an
evicted-receipt gap, publishes no `turn.*` event and reads no inbox row. Coverage
is negotiated by that refresh rather than inferred from a carrier name: without
`capabilities.wholeSessionTurns=true` the row stays `carrier-deliveries`
(partial/unknown), never whole-session idle or in-turn.

An interactive producer declares its own turn coverage on the existing
`session.refresh` call (`turnCoverage`: `whole-session` or
`carrier-deliveries`). The declaration is authenticated and session-fenced,
is daemon-local (dropped on re-register, unregister, and daemon restart), and
is never inferred from a process, a terminal, or timing. A producer that never
declares keeps the pre-existing rule: only the sources listed in
`daemon/impl/application/actors/sessions/coverage.py` claim whole-session
coverage, and every other source is carrier-only. A carrier-only session cannot
support a whole-session idle or busy claim; it reports `unknown` with reason
`partial-turn-coverage` and health `partial`.

Codex whole-terminal coverage (E2): the interactive launcher owns one app-server
per terminal and the TUI joins it as a client, so the foreground interactive
carrier shares that app-server. It subscribes with `thread/resume`, receives
native `turn/started` / `turn/completed` notifications for turns the user types
in the terminal, and resynchronises identity with `thread/read` after a
subscribe or a missed boundary. `thread/status/changed` is broadcast to every
client and is used only to notice a turn boundary that the subscribed events did
not deliver. The native turn id is the report id, so one native turn has one
observation identity; turns the carrier injected keep their existing settlement
receipt and are not re-reported here. A session that cannot subscribe declares
`carrier-deliveries` instead. The detached tmux mode starts no carrier, declares
nothing, and therefore stays partial/unsupported — never idle by omission.
Socket loss immediately clears the observer's subscription claim. Reconnection
opens and initializes a new client socket, reinstalls listeners, resubscribes,
and reconciles the native snapshot's complete current open set before restoring
coverage. A failed snapshot adoption or report-store write also withdraws
coverage and is retried; report dedup and open-set changes follow durable relay
acceptance, not the attempted write. A pending snapshot boundary is an ordering
obligation: it must be accepted before any queued live boundary is applied or
submitted. Later submissions cannot replace or clear that obligation. The
observer retains one pending report and at most 4096 queued events; overflow
withdraws coverage and records a coverage gap, not lossless continuity. Queued
boundaries retain order across a transient sink failure, and coverage returns
only after pending reports are accepted and snapshot reconciliation succeeds. Recovery closes a previously observed open turn from
its native terminal status; an absent retained turn is reported failed, never
completed by inference. This is observation reporting, not turn cancellation.

The carrier publishes a coverage downgrade even if the socket recovers between
its polls, then publishes restored coverage on a later poll. Failed refresh
requests do not advance the local publication checkpoint. The diagnostic
`codex.whole-terminal.coverage-gap` records `startedAtMs` and `endedAtMs` in its
detail when recovery finishes; it does not assert continuous event retention
or recover short turns that existed only during the gap. E1 current-state
refresh remains the daemon's authoritative recovery path. No threshold or
reconnect changes delivery, starts a model turn, or restarts the app-server.

## CLI display (slice D)

The current grouped CLI uses `hyprial agent list [--json]` and
`hyprial agent top [--wide] [--json]`. The design's old root `ps` spelling
was retired by R7; this slice does not restore it. The daemon's `ps` IPC
remains an internal publication surface. `daemon status` retains its existing
daemon-only payload and offline/restoring behavior; it is not an agent view.

Both agent views format the supplied `runState` with one helper. Human output
includes state, bounded reason, base state, measured turn age, certainty, scope,
observation health, permission capability and any provider block or suspicions.
`turn-age=-` means unknown/not supplied, not zero. The renderer does not infer
age from timestamps or state from logs, process samples, presence or lastKnown.
`permission=unsupported` means coverage=false, not "no pending permission".
Missing runState from an old daemon displays `unknown (unsupported)`; a failed
agent query remains an error, not a successful empty fleet. Missing permission
capability is `unknown`, not supported or a negative wait observation.

Top retains its old statistics and `state/stateDetail` JSON meanings. Its
human column is now explicitly `LEGACY` (log/contact diagnostics), separate
from `RUN STATE` (cell observation). Sorting and process-resource sampling are
unchanged; neither supplies cell state. `--json` bypasses human formatting and
host process sampling, preserving all supplied fields in one object with `ok`.
List retains filters and other status details. Neither command adds a request,
notification, lifecycle action, message fetch, or mutation of the response.

`run-state-display-vectors.json` pins the shared human summary for all states,
permission versus provider blocking, partial and stale evidence, measured-zero
versus unknown age, and old-daemon compatibility. The isolated lifecycle runner
executes these goldens; CLI tests exercise both registered commands and JSON.
These fixture tests do not certify native permission signals or whole-terminal
coverage. E1/E2 and native ractor acceptance remain separate gates.

## Operator thresholds

Optional `<hyprial-home>/cell-observation.json`, loaded once at daemon composition:

```json
{
  "revision": 1,
  "defaults": {"turnLongAfterMs": 1800000, "permissionLongAfterMs": 300000},
  "agents": {"agent:owner:node:worker": {"turnLongAfterMs": 2400000}}
}
```

Only these keys are accepted. Agent keys must be canonical agent URIs. Values
must be positive integer milliseconds, or null to disable that warning. This
file has no stop/restart/approve/deny policy. Config changes require daemon
restart; the revision is operator-supplied diagnostic metadata.

The existing Codex producer quiet detector remains the only quiet detector.
Its default is **900 seconds (15 minutes)**. Existing `idleTimeoutSeconds` launch
configuration overrides `HYPRIAL_TURN_IDLE_TIMEOUT_SECONDS`, then the default;
zero disables reporting. `quietAfterMs` reports this actual producer value (null
when unavailable/disabled), rather than establishing a competing cell timer.
The run-age default is **30 minutes**; permission-wait-long is **5 minutes** and
is only eligible when explicit permission evidence exists. Native permission
signal plumbing and refresh are E1, not claimed by C. No threshold kills,
interrupts, approves, denies or restarts execution.

`run.py` executes all vectors through the shared projection and checks
`run-state.schema.json`. Product tests also
exercise grouped reads, ps/top publication, source fences and a real fake-Codex
process with all progress fan-out discarded. Native-carrier E2E is separate.
