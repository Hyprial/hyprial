# PAC workflow unification

Status: implemented, verified and delivered on intg/pac-v2-full-impl. User-approved scope, 2026-09-23.
Base: `7324b4f547295f6d74587f2a41856f421260c738`.
Delivery: `impl/pac-v2-full` into `intg/pac-v2-full-impl`; no production upgrade.

## Product contract

- Keep `hyprial workflow` as the high-level graph entry point, and `hyprial workflow`
  as the graph/flag/journal interface. New workflow tasks have exactly one PAC
  graph and no legacy run/executor. Reject old YAML execution semantics loudly.
- Each executable workflow node owns a new temporary worker by default. Start
  only when dependencies permit. Explicit worker keys share one worker across
  nodes. Explicit existing actor references borrow without adopt/stop authority.
- Graph close reclaims its owned workers. Routine owns a persistent agent,
  normally a coordinator (role dispatch by default) which can dispatch child workflows. It can also do
  work itself. Routine removal retires only its own agent.
- Routine has explicit scheduled and source modes. Scheduled occurrences get
  distinct durable identities. Run serially, skip overlapping or missed slots,
  never catch up after downtime, and expose skips. Source tasks retain UUID
  deduplication. Keep the existing interval scheduling syntax.
- Expose failure policy at dispatch/preview: `terminate` (default, fail entire
  graph and reclaim owned workers), `continue` (finish independent branches),
  or `hold` (keep open for explicit owner action). Completion is explicit flag
  evidence; no reply-text matcher, automatic retry, deadline extension or
  implicit report. Reports, approvals and rework are explicit graph nodes.
- Preserve dispatch role, first-output ETA and human-gate declarations. They
  are admission/communication metadata, not implicit approval execution.
- At cutover, terminate and archive unfinished legacy tasks, preserving their
  prior states and cutover reason. Never replay/convert those tasks, adopt
  existing actors, or manufacture successful results. Seal old write paths.
  Fresh homes also install an empty sealed legacy write-barrier database so an
  old binary cannot recreate legacy dispatch tables after a downgrade. This is
  explicitly a barrier, not fabricated history or an empty migration claim.
- Include GUI workbench migration, read-only legacy history, packaged skills,
  active templates and test contracts. Historical taskbooks are not replayed.
- Existing actor/model selection and session authentication boundaries remain
  authoritative. Node completion must reject stale activation/request evidence.

## Implementation sequence

1. Strict graph workflow schema, normalized plan and graph compiler; immutable
   launch/brief references; PAC-backed operation idempotency and projections.
2. Native dispatch integration, actor lifecycle, explicit node outcome/failure
   handling and ownership-safe cancel/close; no parallel legacy executor.
3. Routine scheduled/source modes, persistent ownership, slot/skips journal,
   restart recovery, and removal behavior.
4. Idempotent cutover/archive and shared assign/alarm separation; retire legacy
   execution imports, timer and writers; preserve authorized history reading.
5. GUI definitions/preview/start/progress/completion/cancel and stale-write
   handling; templates/skills/tools updated to the actual command contract.
6. Focused regression, isolated daemon/CLI E2E and relevant GUI/full checks;
   inspect diff, commit, merge integration branch and verify remote commit.

## Verification obligations

| Area | Required evidence |
| --- | --- |
| Admission | malformed/old YAML and invalid actor/model input have no dispatch effects |
| Graph | dependency fan-out/join, explicit approval/report/back edges, idempotent replay |
| Ownership | lazy launch, shared worker, borrowed actor never stopped, close cleanup |
| Failure | all policies, fixed deadline, crash, explicit failure, no retry |
| Completion | owner binding and stale request/activation refused atomically |
| Routine | both modes, occurrence identity, source dedup, busy/downtime skip, removal |
| Cutover | prior-state preservation, unfinished termination, restart/interruption, sealed writes |
| GUI | current graph projection, history read-only, authorization and stale operation refusal |
| Isolation | dedicated homes/state/sockets, no production processes, exact PID cleanup |

Every result must name its tested tree and scope. Focused green is not a full
suite or live-provider result. Development may fix discovered product defects;
retain failing evidence and rerun affected checks.

## Verification and locality scope

Native-protocol E2E uses a deterministic fixture with real daemon/CLI/worker
processes. It does not claim verification against paid model providers.

Actors are created locally; explicit actor URIs may borrow from another machine
or owner within the existing trusted tailnet. Both daemons must support the
PAC workflow delegation protocol. The owning daemon checks actor existence and
role before graph admission; unavailable or older daemons fail admission before
publication. Remote user principals are not routable execution actors.

The origin holds the only graph. The execution daemon imports a request binding,
never an actor lifecycle claim or a graph copy. Its normal local session fence
verifies the actor before forwarding completion/failure. An origin-minted HMAC
capability binds origin, graph, node, actor, request, transitive inputs, fixed
deadline, body digest and message identity. Capabilities stay in daemon state;
they are not returned by status/inspect, embedded in prompts, or sent as session
credentials. The origin rechecks readiness and commits completion and its replay
receipt together. Explicit failures have the same deadline and input fence.

This preserves design.md's existing trusted-tailnet boundary: it does not add
public-key node enrollment or defend against a malicious daemon already inside
the trusted mesh. Actor URI text alone and ordinary replies do not authorize
completion. Local caller impersonation and modified request grants are rejected.

Network return retries are transport retries, never task/model retries. The
execution daemon persists completion/failure before sending it. `returnState`
is `pending` until the origin acknowledges, then `accepted` or `rejected`.
Exact retries return the original receipt; conflicting outcomes are refused.
The queue and origin receipts survive either daemon restarting. An expired or
withdrawn request cannot complete after reconnection. `workflow inspect GRAPH
--node NODE` reports the authoritative node and local return state; while the
origin is unavailable it reports `current: null` and `authorityAvailable: false`.

Before native execution, imported deliveries check current authority through
the local daemon. Network uncertainty holds queued work; it is not a withdrawal.
A definite withdrawal or elapsed deadline prevents a queued model turn. Already
running borrowed actors are never stopped. Any late result is still fenced at
the origin. Native failures use the same durable return queue.

`workflow complete/fail` resolve the imported request automatically. The bound
agent form of `workflow complete --expected-request` uses the same return path;
an expansion-enabled remote completion additionally requires the signed
`pac-expansion-v1` feature and durably carries text, digest, and attempt number.
Only a definitive validation rejection permits a corrected attempt on the same
current request; ordinary remote completion semantics are unchanged.
`workflow reset` supports explicit rework with an origin receipt so replay cannot
reset a newer round. Reset is synchronous: loss of its acknowledgement is a
retryable operation, not an accepted queued completion. Generic graph creation,
activation and closure remain on the origin; this is not unrestricted remote IPC.

Schema 12 adds imported requests, a private signing secret, durable return outbox
and atomic outcome receipts. Schema 11 data is preserved. Borrowed remote
routine actors use this same protocol per occurrence; routine removal closes
its origin graphs without acquiring authority to stop the remote actor.
