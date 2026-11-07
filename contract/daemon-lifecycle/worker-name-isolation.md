# Worker kind and local adapter-name isolation

Desired state can contain an adapter and a worker with the same name in legacy
homes. Its storage key remains `(harness, name)`. A worker-kind lookup selects
only kinds in `DECLARED_HARNESSES`. Zero or multiple matching worker rows yield
no kind; an adapter row never wins by ordering. The single-row `harness_spec`
read uses this rule, including when `excluding` is supplied. The shared worker
kind vocabulary is exported by the kernel; daemon capability declarations must
cover exactly those kinds.

Adapter launch rows do not constitute agent config-rebind custody or identify
agent inboxes for the startup fetched-row settlement sweep. Worker status
identity lookup cannot use an adapter's pinned owner. Routine orphan cleanup
requires both a worker kind and its ownership marker. Legacy adapter stop must
not deactivate an agent or release its binding when the adapter client or row
is absent. Spec-to-agent lifecycle conversion requires a declared worker kind,
including internal PAC launches; other kinds receive `INVALID_ARGUMENT` before
agent URI conversion or lifecycle submission.

Unknown kinds have no execution mechanism. They support prompt injection but
receive only scalar summary notices, never raw payloads or full-body batches.
This also applies to mechanism-hook payloads when the recipient kind is unknown.
Known model carriers retain the inbox-batch-v1 behavior; known mechanism workers
retain their scalar protocol frames.

A failure while dispatching or reconciling a single actor does not abort other
actors or the remaining timer phases. No delivery is acknowledged on error.
The next tick may retry the actor.

## Admission and diagnostics

- Adapter create/add/onboard and explicit start refuse an existing local agent
  name. Agent create, host-invite, start preflight, and explicit lifecycle start
  refuse a configured local adapter name (including stopped adapters).
- `--force` does not bypass this check. CLI/IPC refusals use existing
  `INVALID_ARGUMENT` and suggest `<name>-feishu` for the adapter.
- Legacy restore and identity recovery remain permitted. This is not an
  automatic rename or migration.
- `hyprial doctor` reports configured legacy collisions as a check named
  `agent-adapter-name-collision`, with `status: warn`. `--fix` never changes
  either identity, configuration, or credentials for this check.

## Structured daemon log events

These are log events, not HookBus subscriptions or new delivery wire frames.

| Event | Level | Fields | Cadence |
| --- | --- | --- | --- |
| `harness.kind.unknown` | warn | `actor`, `harness` (string or null) | Once per actor per bridge lifetime when delivery needs an unknown/ambiguous kind |
| `harness.actor.failed` | error | `actor`, `phase`, `errorType` | Once per failed actor/phase per tick; no raw exception or message body |
| `harness.dispatch.agent_timeout` | warn | `agent`, `durationMs`, `code: DISPATCH_AGENT_TIMEOUT` | Once per timed-out dispatch operation; the agent is skipped for that round and its pending row is retried later |
| `inbox.retry_pump.starved` | warn | `consecutiveYields: 20` | At the twentieth consecutive `STALE_COMMAND` yield; once per streak |

`phase` is `dispatch`, `registration`, or `registration.close`. Existing
`inbox.retry_pump.yielded` info events continue. Any successful `retry_due`
call (including an empty result) or a non-stale failure ends the stale streak.
Timer kicks without a retry attempt neither increment nor reset it. The count
is in memory and resets on pump restart. Twenty yields usually represent about
five minutes at the retry floor; this is an attempt threshold, not a time SLA.

Regression corpus: `tests/daemon/harnesses/collision/` and
`tests/daemon/authority/test_desired_projection_reads.py`.
