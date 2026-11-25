# E2E-020: PAC v2 slice-2 FIRST WAVE — §B context and §C public events

## Preconditions

Isolated real daemons and inert inbox identities, no external provider/service.
`real_environment_required: false`. The B fixture is the Allen graph, created
through public CLI; the consumer uses CLI JSON only, with no PAC import/SQLite.

Run (only after the separate post-CHECKPOINT-3 gate):

```sh
uv run --locked --python 3.12 --extra test python contract/e2e/run-pac-slice-2.py
```

This is the complete **authorized first-wave** matrix: B1, B2-ABSENT, C. B2 is the
explicitly ruled absence contract while no actor nodes exist. Positive actor
information, §A actor lifecycle and §D real dispatch are **NOT RUN**, not stubbed
or claimed as passing. End-node automatic close likewise awaits wave 2.

## Steps

1. **B1**: create/activate the actual 13-node Allen fixture, register inert owner
   identities, and set/reset the prescribed source flags through demo. Observe
   the planned pm notification. Read `workflow inspect <graph> --node pm-confirm-2 --context --json`:
   own briefRef, completedCount=0, currentActivation.round=1, demo predecessor
   reasonRef/setBy/setAt at the same public snapshot cursor. The referenced file
   body must not appear in context.
2. **B2-ABSENT**: back request to pm-helper yields completedCount=1 versus
   currentActivation.round=2; reset preserves the activation identity/count.
   Despite registered agent owners, `actor`, actorName and launchRef are absent.
   Owner closes the task/clock run: currentActivation=null, preserved completed
   count, no synthetic actor lifecycle data. Stop/clean up the B daemon.
3. **C**: reuse `run-pac-public-events.py` as the independent public
   subscription gate: snapshot→strict suffix across every cursor, duplicate
   consumption, cross-graph gaps, structure invalidation/freeze, back/withdraw,
   overdue, daemon-down→real resend receipt, resync, follow and teardown.
   Follow deliberately inherits SIGINT=SIG_IGN and must still exit on SIGINT,
   SIGTERM or idle output-pipe EOF; /dev/null stdin must remain usable.
   An opaque original slice-1 image seeds an additional isolated root; public
   CLI migration establishes floor=2, positive after=1 must resync with the same
   journal ID. Real v5 resend receipts must preserve snapshot structure v6.
   This is an actual migration boundary, not claimed retention/pruning coverage.
   Clock triggering is test setup through hidden `pac debug clock`; the consumer
   reads only public `workflow events` JSON, never internal state.

## Assertions

- B1 refs/flag attribution and current activation agree with one snapshot cursor;
  completion count does not masquerade as a requested round.
- B2 proves **absence**, not positive actor information or runtime ownership.
- Close is an explicit owner action, not an inferred flag completion/actor death.
- C reconstructs flags/refs/assignments/delivery state from the public protocol;
  duplicates are harmless and unsupported/expired cursors cause resync.
- B teardown uses the helper's actual `root` and `wait_stopped()` (no fictitious
  `state` attribute), and the combined runner must reach C before it can pass.
- All daemons use disposable roots and teardown on failure. No real worker
  launch, v1 executor calls, production state, or actor start/down stub shapes.
- PASS is explicitly `FIRST_WAVE B1/B2-ABSENT/C = 3/3`; it is not full §A/§D
  release acceptance. A/B-positive-actor/D must be added against real interfaces
  when wave 2 is authorized, not turned into skip-based green results.
