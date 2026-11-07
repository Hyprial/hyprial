# Network/daemon decoupling

**Date:** 2026-10-20

**Status:** proposed landing plan

**Target branch:** `dev`

## 1. Decision

A daemon command is successful when the daemon has validated it and has taken
**local durable custody** of the requested work. It is not successful merely
because an in-memory queue accepted it, and it does not wait for remote
settlement.

The following is a hard ownership invariant:

> An IPC handler, OrgFS/space lock owner, inbox actor, or state-writer actor
> MUST NOT wait for network I/O, a remote peer, a sidecar, or an external
> platform API. Such an owner MUST NOT call a function which can do so, even
> when that function has a timeout.

This means:

1. local validation and one local transaction may run on the command path;
2. the transaction records both the state transition and any required outbox
   work atomically;
3. the response reports local acceptance (`accepted`/`queued` and a stable
   operation or message ID);
4. separately owned workers attempt network effects and persist their
   outcomes; and
5. status commands read local projections. A refresh is another asynchronous
   operation, not a long status request.

A timeout contains one wait. It does not satisfy the invariant. A 2-, 10-,
15-, 30-, or 90-second remote wait still occupies finite IPC or actor capacity.

The existing inbox custody/outbox design is the reference architecture. This
plan finishes its command-path separation and reuses the same durable receipt,
idempotency, retry, and projection concepts in OrgFS, workflow, route delivery,
and session recovery. It does not create a second message authority.

## 2. Why this is required

The IPC server admits 64 clients (`src/hyprial/daemon/impl/ipc/server.py:32`),
runs each client on a thread (`server.py:327`), and allows a request to occupy a
request slot for 90 seconds (`server.py:40`, `server.py:543`). It now rejects
overflow and releases timed-out slots, which correctly contains the resource
leak, but the request work can remain blocked below that wrapper.

Therefore 64 independent network waits can produce 64/64 occupied clients.
The daemon then rejects local commands even though its database and local
actors are healthy. The failure is multiplicative when a request is waiting on
an actor which is itself waiting on transport rebuild, an external API, or a
remote receipt.

Three unbounded waits make the outer 90-second deadline particularly unsafe:

- lifecycle reservation calls `answer.get()` with no deadline at
  `src/hyprial/daemon/impl/lifecycle/lifecycle_coordinator.py:574`;
- the squire ledger calls `reply.ready.wait()` with no deadline at
  `src/hyprial/daemon/impl/squire/actors/ledger.py:83`;
- harness `_AwaitSettlement` reaches `reply.wait()` with no timeout at
  `src/hyprial/daemon/impl/harnesses/actor/facade.py:401`.

Timing out the IPC response does not cancel those accepted commands and cannot
prove whether their durable mutation or remote effect occurred. Retrying can
then duplicate an operation unless its stable ID and durable receipt are the
real synchronization point.

### 2.1 Current invariant audit

| Scarce owner | Current verdict | Evidence and boundary |
|---|---|---|
| IPC request/client slot | **FAIL** | send, reply, message status, OrgFS join/content, org, and workflow handlers synchronously await the effect paths enumerated in section 4 |
| OrgFS document/space lock | **PASS narrowly** | document mutation commits and releases its lock before `_post_commit_scope` waits; however, the calling IPC request then waits up to 60 seconds, so the end-to-end command still fails the design |
| OrgFS lifecycle authority actor | **PASS narrowly** | join/serve/open run on the separate four-worker `EffectLane` created at `orgfs/composition/lifecycle.py:80-112`; the facade waits without a deadline at `:276-310`, which is still an availability defect |
| Inbox custody/state actor | **PASS internally, FAIL at facade** | durable queued state precedes `DispatchIoRequested`, and I/O has its own bounded worker; the normal submit facade still joins delivery before returning instead of using the existing deferred path |
| General state-writer actors | **No direct network wait found; not enforced** | local reservation and ledger callers contain unbounded waits (`lifecycle_coordinator.py:574`, `squire/actors/ledger.py:83`); import/context gates are required so a future effect cannot enter these owners transitively |

“Pass narrowly” is not release acceptance. The invariant is complete only when
the transitive call graph is enforced and no IPC facade waits for a separate
effect owner.

## 3. Terms and acceptance boundary

- **Accepted:** local authorization and validation passed, and the domain
  authority committed the command and its required outbox record in the same
  local transaction.
- **Queued:** accepted, but remote settlement has not been recorded. It can
  include `in_flight` and `retry_wait` in the detailed operation state.
- **Settled:** a durable local record says the remote effect succeeded or
  terminally failed. A timeout or missing receipt is not success.
- **Projection:** local persisted state which is safe to return without a
  probe. Every projection carries `observedAtMs` or `updatedAtMs`; network
  projections also carry freshness/staleness.
- **Operation ID:** a stable idempotency key for joining retries and querying
  an asynchronous result. Reusing an ID with different content is an error.

`accepted:true` never means delivered, replied, synchronized, joined, or
reachable. Those are separate settlement fields. If the local transaction is
ambiguous, return a stable operation ID and `outcomeUnknown:true`; the caller
queries that ID rather than issuing a new command.

## 4. Current-path audit

The line references below are to `origin/dev` at `fbc96c96a`.

The synchronous-wait inventory is:

| IPC family | Synchronous remote/external wait today | Effective bound seen by the request |
|---|---|---:|
| agent `message.send` / `message.reply` | inbox direct dispatch after durable insert | inbox facade 2 s; outer IPC 90 s |
| user `message.send` / `message.reply` | Zenoh publish plus receiver receipt poll | receipt poll 3 s; Zenoh `put` itself has no native deadline |
| route `message.send` / `message.reply` | adapter `send_chat*` and settlement actor | actor wait 65 s; native SDK work can continue after caller timeout |
| `message.ack` | none found; local inbox actor/SQLite only | local actor facade 2 s |
| `message.status` | best-effort Zenoh mesh query after local read | default 2 s, caller value capped at 10 s |
| OrgFS mutation | post-commit callback/broadcast effect | 60 s effect wait; outer IPC 90 s |
| OrgFS read/export with missing content | blob request and retry loop | default 10 s, caller value capped at 120 s; outer IPC still 90 s |
| `orgfs.join` | mesh snapshot sync | sync attempt defaults to 3 s, plus unbounded local lifecycle waits |
| `org.join` / `org.execute` | sidecar map, redial, two space joins, publication | join saga defaults to 30 s; native publish/reconfigure can exceed it |
| `org.create` / invite/member changes | Casdoor HTTP plus OrgFS effects/publication | Casdoor request 15 s; OrgFS post-commit wait 60 s |
| `org.fetch` | mesh fetch | default 2 s, caller value capped at 30 s |
| remote workflow/PAC | Zenoh request/reply | wire wait 4 s with 8 slots; preceding `put` has no native deadline |
| `ps` / daemon status | no network wait found on current `dev`; local actor snapshots and route cleanup run synchronously | outer IPC 90 s; `refreshTailnetStatus` is currently a no-op |
| directed `hosts.probe` pending branch | Zenoh liveliness query | branch permits a caller-selected threshold; must not land synchronously |

The numeric bound in the last column is diagnostic, not permission to retain
the wait. In particular, the 90-second IPC wrapper can return while accepted
work below it is still running.

### 4.1 IPC envelope

| Path | Current ownership and wait | Bound | Required disposition |
|---|---|---:|---|
| Any routed request | router calls implementation synchronously; message, OrgFS, and workflow entry points are at `ipc/router.py:438`, `:529-601` | outer request 90 s | local transaction/projection only |
| Client admission | one client thread and one of 64 request slots (`ipc/server.py:32`, `:150`, `:303-304`) | 64 | retain cap; saturation is containment, not flow control for network work |
| Response dispatch | request runs on another thread (`ipc/server.py:515`, `:538`) | 90 s (`:543`) | a timed-out task must not own remote work |
| `message.pending.wait` | waits only for local inbox version changes | 5 s, 24 concurrent (`inbox_surface/queries.py:50-53`, `:330-335`) | retain as the explicit bounded local long-poll exception |

A handler may wait for its own admitted **local** transaction under a short,
measured deadline. It must use a durable command ID so timeout recovery is a
read, not a resubmission. It may not wait for an effect spawned by that
transaction.

### 4.2 Send, reply, and ack

#### Agent/inbox target

`message.send` enters at
`application/messaging/inbox_surface/send.py:72` and eventually calls
`self._inbox.submit` at `:477`. `message.reply` enters at `:631` and submits at
`:798`.

The inbox authority wait is nominally two seconds
(`inbox/authority/facade.py:92-105`). The writer persists a queued result before
requesting I/O; deferred submission is already implemented at
`inbox/actor/custody/submit.py:81-84`. The I/O lane is separate and bounded: it
has default capacity 32 and up to four workers
(`inbox/actor/worker.py:51-81`). However, the normal direct path still asks for
I/O and waits for its completion before returning. This couples the IPC result
to remote settlement despite the durable outbox already existing.

**Change:** all public send/reply paths commit the outbox and complete their
actor command before dispatch. Dispatch is only a post-commit wake-up. The
worker later advances the existing durable submission receipt. Do not replace
this with a new queue.

#### User/squire target

The delivery path calls the user delivery port synchronously at
`application/messaging/delivery/sends.py:173`, `:226`, `:249`, and `:729`.
Zenoh user delivery publishes and polls for a receipt for three seconds in
`squire/addressing.py:673-699`. The remote receiver may then perform platform
work. The ledger's unbounded actor wait is at `squire/actors/ledger.py:83`.

**Change:** represent a user target as an inbox durable outbox destination.
The IPC response follows the same accepted/queued contract as an agent target.
The user delivery worker, receipt poll, and receiver platform effect stay off
both inbox writers and IPC threads.

#### Route/adapter target

Route fan-out enters `_deliver_route_target` at
`application/messaging/delivery/sends.py:521` and synchronously invokes
`gateway.send_chat`, file, or image operations at `:566-581`. The Lark outbound
actor can wait 65 seconds, and explicitly waits without a timeout for settlement
when asked (`adapters/lark/outbound/gateway.py:73`, `:108`).

**Change:** first durably snapshot the text and attachment/blob references,
then insert one idempotent route-delivery outbox item per route. A bounded
adapter worker invokes the gateway. `message.send` returns the message ID and
route operation IDs, not native platform message IDs. Native IDs arrive in the
local settlement projection.

#### Ack

`message.ack` starts at `inbox_surface/queries.py:438`; the inbox authority
executes `AcknowledgeMessageCommand` through its local actor
(`inbox/authority/facade.py:233-242`). Its state transition must remain local
and atomic. Any receipt publication or remote sender retirement is an outbox
effect.

**Response semantics:** ack returns `accepted:true`, `acked:true` only after the
local row is committed. `receiptQueued:true` states that peer notification is
pending. An already-acked ID returns the same result. Ack never waits for the
sender.

### 4.3 Message status

`message.status` first reads local persisted delivery evidence, then can query
the mesh at `application/messaging/delivery/status_queries.py:110-188`.
The mesh timeout defaults to two seconds and is capped at ten seconds. This is
bounded but still consumes an IPC slot and transport/session capacity.

**Change:** the default and only synchronous result is the local projection.
A caller requesting fresh remote evidence receives an accepted refresh
operation ID. A status worker performs the query and updates the projection.
Repeated refresh requests coalesce by `(messageId, target, generation)`.

### 4.4 OrgFS and organization operations

OrgFS dispatch begins at
`application/messaging/orgfs_bridge.py:264`. Reads may call
`runtime.await_content` at `:297-318`; checkout/export use that wrapper at
`:364` and `:426`. `orgfs.join` calls `fs.join` synchronously at `:547-548`.

The current document commit path does release its space lock before effects,
but then waits up to 60 seconds for the effect at
`orgfs/document/effects.py:268-274`, with the wait reached from the post-commit
scope at `:335-388`. Thus the lock does not wait on the network, but its IPC
caller can.

Other remote waits are:

- join calls `mesh.sync_from(holder)` at
  `orgfs/composition/lifecycle.py:585-607`;
- lifecycle admission and mesh creation wait without deadlines at
  `orgfs/composition/lifecycle.py:276-310` and `:376`;
- refresh calls `mesh.sync_from` at
  `orgfs/composition/content.py:92-114`;
- missing content requests remote blobs at `content.py:167-189`;
- direct blob fetch begins at `content.py:223`;
- the organization service retries space joins with a 30-second default at
  `org/network/service.py:68`, `:689-709`;
- `org.create` and invite handlers call the service inline from
  `org/network/ipc.py:107-137`; create calls Casdoor before its OrgFS writes at
  `org/network/service.py:157-175`;
- `org.join` calls the entire join workflow inline at
  `org/network/ipc.py:165-171`; its map-peer callback can start/control the
  sidecar and its redial callback reconciles transport (`ipc.py:51-101`);
- `org.publish` calls mesh publication inline at
  `application/messaging/status/views.py:634-652`;
- `org.fetch` performs a mesh fetch with a default two-second and maximum
  30-second caller timeout at `status/views.py:657-700`, `:746-749`; and
- Casdoor HTTP has a 15-second request timeout at
  `org/network/casdoor/client.py:28`, `:144`.

`OrgSpaceAuthority._ask` also retries local actor admission and waits for its
completion. Even when the operation is local storage, this needs a finite
admission/commit deadline, a command ID, and outcome recovery. A local actor
wait is permitted only if the admitted operation cannot call network code.

**Change:**

- Local document mutations commit document state plus the existing effect
  journal, signal the effect lane, and return immediately. Remove
  `_wait_for_effect` from the command path.
- Split callbacks from remote broadcast. Local projection/watch callbacks get
  their own bounded post-commit lane; a slow callback cannot block network
  dispatch or a writer.
- `join`, `serve`, organization bootstrap, sync, refresh, and blob acquisition
  become durable operations. `orgfs.join` returns an operation ID in
  `queued/synchronizing`; it reports `joined` only after the local synchronized
  snapshot commits.
- A normal read never fetches. It returns local content or a structured
  `contentPending` result with a refresh operation ID. Explicit `*.wait`
  commands, if retained for CLI ergonomics, poll only local operation state,
  use the same five-second/24-waiter policy as message pending wait, and do not
  own network work.
- Organization membership changes commit their local saga state first.
  Casdoor and peer-space work run as idempotent saga steps; compensations and
  partial failure are visible in operation status.

### 4.5 Workflow/PAC

Workflow handlers are in `application/workflows.py:46-523`. Remote current,
expansion preview, completion/status forwarding, and admission synchronously
call the remote workflow service at `:51`, `:124`, `:219`, `:344`, `:419`, and
`:523`.

The remote wire has eight call slots (`BoundedSemaphore(8)` at
`dispatch/remote/wire.py:77`) and a separate pending-request cap of 32, plus a
four-second request/reply timeout. It publishes and waits at
`dispatch/remote/wire.py:84-107`. The transport publish itself has no native
operation deadline. A durable `remote_workflow_outbox` already exists and is
drained in batches of 32 (`dispatch/remote/__init__.py:166-277`), but some calls
bypass it and one local pending-result join waits four seconds (`:587`).

**Change:** use that outbox for every remote mutation. A local workflow command
commits an admission/transition intent and returns `accepted:true`,
`queued:true`, and `operationId`. Remote inspection/current data is a cached
projection with freshness. Expansion preview which requires a remote owner is
an async preview operation, not an IPC-held RPC. PAC remains a dispatcher and
tracker; it does not execute worker work while waiting.

A transition which depends on remote validation is stored as
`validation_pending`, not prematurely marked complete. The same operation ID
is used across retry and settlement. Existing synchronous response fields
remain readable during migration, but their remote-settled values may be null
until status shows completion.

### 4.6 `ps`, hosts, and daemon status

`ps` starts at `application/messaging/status/views.py:147`. The current path
builds a local worker snapshot, but it also expires and cleans routes. Its
`refreshTailnetStatus` parameter is passed at `status/views.py:416-418`, but the
current implementation returns an empty local projection without I/O
(`application/netendpoints/endpoints.py:532-543`). Thus no synchronous network
wait was found in current `ps`; the remaining defect is that an observational
read performs cleanup and that future sidecar-backed refresh work could be
added on this path.

**Change:**

- `ps`, `hosts`, endpoint status, and daemon health are projection-only.
- Stale route expiry, route cleanup, and tailnet refresh move to periodic
  maintenance/effect owners.
- Rows include `observedAtMs`, `ageMs`, `stale`, and the last error. Absence of
  fresh evidence is `unknown`, never inferred `down`.
- `refresh:true` enqueues/coalesces a refresh and returns its operation ID plus
  the current snapshot. It never changes the current request into a probe.
- The IPC server's own admission/saturation counters remain in-memory and must
  be readable even when every network effect lane is unhealthy.

The directed liveliness probe on `origin/feat/network-check-v1` performs a
transport query in a `hosts.probe` handler. It must be adapted to this refresh
contract before landing; its persisted probe ledger and verdict model can be
reused.

## 5. Session rebuild and redial

`TransportSessionAuthority` has bounded facade waits
(`transport/session_actor.py:372`, `:509-510`), but publish, query, and
reconfigure are still effects whose completion callers can await. Publish is
at `:824`, query at `:827`, and reconfigure at `:932`.

The Zenoh transport waits up to 30 seconds to acquire/rebuild a session
(`transport/zenoh/config.py:28`, `transport/zenoh/transport.py:135`).
`put()` at `transport.py:674` has no operation deadline beyond native Zenoh.
Queries at `:695` have an explicit timeout. Reconfiguration at `:311` can enter
native session work.

Forwarding startup synchronously lists reachable endpoints at
`application/netendpoints/forwarding.py:168`, and endpoint changes call
`transport.reconfigure_connect` at `:253`. Since Zenoh fixes its connect set at
session open today, peer churn can serialize callers behind rebuild/redial.

The target is a generation transition owned by the session supervisor:

1. a local route/slot change commits the desired generation and returns;
2. one single-flight supervisor reconciles desired and active generations;
3. publishers never initiate rebuild while holding another owner; they either
   enqueue durable work or receive a fast `SESSION_NOT_READY` from an effect
   attempt;
4. the last valid local session projection remains readable during rebuild;
5. successful activation atomically publishes the new generation, after which
   old registrations drain; and
6. a failed generation records error/backoff without invalidating local daemon
   commands.

A caller timeout alone cannot make an unbounded native call safe. Every native
operation needs a library-level deadline. Where Zenoh or an SDK cannot provide
one, run it across a killable process/sidecar boundary. Do not abandon
uncancellable Python threads per timeout. The worker may be unhealthy; IPC and
state writers must remain healthy.

Fixed slot endpoints from the pending slot-connect work reduce reconfigure
frequency: Zenoh connects once to stable local sidecar slots and the sidecar
reassigns peers. This is a useful prerequisite, not proof of decoupling.
Sidecar control, reconciliation, and slot health are still network/process
operations and stay in the session supervisor lane.

## 6. Target execution model

### 6.1 Domain transaction

Each mutating command follows this sequence:

```text
IPC -> validate/authenticate -> domain writer transaction
                              -> mutate local state
                              -> INSERT domain outbox(operation_id, payload_ref, ...)
                              -> COMMIT
                              -> signal scheduler (best effort)
    <- accepted + queued + operation_id

scheduler -> lease due row -> bounded effect -> persist attempt/outcome
                                     | failure -> retry_wait/backoff
                                     ` success -> settled projection
```

The scheduler signal is not authoritative. Restart scans the durable outbox.
A crash after commit but before signal is safe. A crash after remote success
but before local settlement repeats the effect, so every transport receives a
stable idempotency key and must deduplicate or reconcile by receipt.

Domain authority remains explicit:

- inbox message/receipt rows own message delivery;
- the OrgFS journal owns document broadcast/sync effects;
- `remote_workflow_outbox` owns remote PAC effects;
- route delivery adds a table attached to the message transaction rather than
  writing an unrelated global queue;
- session desired-generation state owns redial/reconfigure.

A generic operation projection may index these records, but it is not a second
source of truth.

### 6.2 Bounded admission and scheduling

In-memory queues are always bounded and use non-blocking admission from writer
actors. Initial limits are:

| Resource | Limit | Overload result |
|---|---:|---|
| IPC clients | existing 64 | immediate `IPC_SATURATED` |
| local pending waits | existing 24, max 5 s | immediate `WAIT_CAPACITY` |
| inbox dispatch | existing 32 credits, max 4 workers | row remains durable/due; no writer wait |
| OrgFS local callback lane | 32 credits, 2 workers | callback remains due; coalesce by commit/space |
| OrgFS network lane | 64 credits, 4 workers | journal remains due |
| workflow remote lane | 32 credits, 4 workers | outbox remains due |
| route/platform lane | 32 credits per adapter, 4 workers | outbox remains due |
| session generation lane | one active reconcile | later desired generations coalesce to newest |
| status refresh lane | 32 global, 1 per target | duplicate refresh joins existing operation |

These are scheduler credits, not durability limits. Reaching a worker queue
limit leaves a committed item in `due` state for the next scan; no actor waits
for queue space.

Durable backlogs also need explicit protection. Before general availability,
measure production payload sizes and set configurable per-domain row and byte
quotas. The default gate is conservative: 10,000 unsettled operations per
domain and 256 MiB of referenced payload per daemon, with a 1,024-operation
per-target fairness cap. Existing accepted rows are never evicted to admit new
ones. At the limit, new commands fail before mutation with
`LOCAL_OUTBOX_FULL`, including counts, bytes, oldest age, and retry guidance.
Control/status/ack traffic has reserved local admission and is not blocked by
a full network outbox. Attachment blob storage has its own quota and must be
committed before acceptance.

Workers lease rows; leases expire after worker death. They use exponential
backoff with jitter, a per-target circuit breaker, and round-robin target
selection. One unreachable peer cannot consume all worker credits. Terminal
failure follows domain TTL/policy and remains queryable; it is not silently
dropped.

### 6.3 Deadlines

Each effect records both an attempt deadline and an operation policy/TTL.
Suggested initial attempt budgets are three seconds for peer publish/receipt,
ten seconds for an external platform request, and 15 seconds for Casdoor. A
budget is passed through to the native client. It is not implemented merely by
stopping the wait in the caller.

Local actor admission and local commit have separate short deadlines. No call
uses `Queue.get()`, `Event.wait()`, or `reply.wait()` without a finite bound.
On a local completion timeout, read the durable receipt by operation ID. Do not
repeat external work from the facade.

The three known unbounded waits are changed as follows:

- lifecycle `answer.get()`: bounded local result wait; reservation ID is
  persisted and queryable;
- squire ledger `reply.ready.wait()`: bounded actor admission/result, while
  platform delivery is an outbox effect;
- harness `_AwaitSettlement`: forbidden on an IPC/state-owner call chain;
  adapters receive local acceptance and subscribe/poll the operation
  projection for eventual settlement.

## 7. API and compatibility contract

During migration, existing keys remain and new keys are additive:

```json
{
  "ok": true,
  "accepted": true,
  "queued": true,
  "operationId": "...",
  "messageId": "...",
  "settlement": "pending"
}
```

A local validation/admission failure remains `ok:false`. A remote failure after
acceptance does not retroactively change the original response; it changes the
operation status to `failed` with a stable error code.

Specific meanings:

- `message.send`: `accepted` means every returned target has a durable outbox
  item (or a local inbox commit). Partial local admission is disallowed unless
  the request explicitly opts into per-target results.
- `message.reply`: the reply intent and deterministic reply message ID are
  durable. The source pending request stays recoverable while settlement is
  pending. It closes only according to the durable reply/receipt policy, not
  because an IPC socket returned.
- `message.ack`: local ack is committed; peer receipt may be queued.
- `orgfs.*` mutation: local revision is committed; `broadcastQueued` can be
  true. `join`/sync returns an operation rather than a fictitious local
  revision.
- workflow mutation: local intent is committed; remote transition can be
  `validation_pending` or `delivery_pending`.
- `ps`/status: always current local projection plus freshness; no hidden
  network refresh.

This changes published field semantics and therefore requires matching
`contract/` fixtures and version notes. For one compatibility release, clients
which do not understand `settlement:"pending"` may use the old synchronous
facade, but that facade must run outside daemon IPC capacity (a CLI-side poll
of operation status). There is no server-side legacy mode which restores a
network wait.

## 8. Observability and proof

Every domain exports, without network access:

- unsettled row count/bytes and oldest age;
- due, leased, retrying, succeeded, and terminal-failure counts;
- in-memory credits in use and rejected scheduler wake-ups;
- attempts, latency, timeout, circuit state, and last error by effect kind and
  target (target values redacted/hashed where required);
- session desired/active generation and last transition error;
- IPC active/limit/saturated and request duration by method;
- projection age and whether refresh is queued.

Logs correlate `requestId`, `operationId`, `messageId`, attempt number, and
session generation. They do not log message bodies or credentials.

The invariant is enforced, not just reviewed:

1. Add a dependency test which rejects imports of transport, sidecar, Casdoor,
   or adapter SDK modules from IPC handlers and state-writer packages.
2. Mark effect ports and use a test tracer which fails when they are invoked
   under `ipc_request`, `orgfs_space_lock`, `inbox_writer`, or
   `state_writer` context.
3. A lock probe test blocks each transport/API indefinitely and proves local
   send/ack/status/OrgFS mutation and actor commits still complete.
4. Saturation tests issue more than 64 remote operations against a black-hole
   peer and prove `ping`, local status, ack, and a local durable send remain
   available.
5. Restart tests kill the daemon at each commit/effect boundary and prove
   eventual retry with no duplicate remote effect.
6. Queue/quota/fairness tests prove immediate overload, reserved control
   admission, and progress for a healthy peer beside a failed peer.
7. Session tests block native open/put/reconfigure and prove desired generation
   and local projections remain responsive.

A rollout stage does not pass because tests skip unavailable real
infrastructure. Real Zenoh, sidecar, Lark, and multi-daemon scenarios report
PASS, FAIL, or BLOCKED.

### 8.1 Required instability acceptance scenario

Run one daemon in an isolated temporary home with an injected loopback fake
peer and fake sidecar. The peer has independently selectable modes:

- **drop:** accept a route/session and then disappear;
- **black-hole:** accept bytes but never provide a reply/receipt; and
- **slow:** delay every reply beyond the effect attempt deadline and flap
  between reachable and unreachable.

Queue at least 128 remote sends, OrgFS refreshes, workflow forwards, and probes,
including at least 64 operations to one failed peer and work for one healthy
peer. While the failure remains active, continuously issue these local IPC
operations from separate clients: `ps`, `org list`, send to a local agent,
`message.pending.list`, `message.read_body`, and `message.ack`.

The acceptance assertions are mechanical:

1. every local IPC call completes in less than 2.0 seconds wall clock;
2. every remote command returns local `accepted/queued` in less than 2.0
   seconds or immediately reports a local quota error;
3. after the admission burst, active IPC slots return to at most eight within
   two seconds, never reach `clientLimit=64` because of remote waits, and
   `saturated` remains false;
4. inbox/OrgFS/state writer progress counters continue advancing;
5. the healthy peer settles work despite the failed peer backlog;
6. remote drop does not call session open/close/reconfigure when the fixed slot
   session remains native-open; and
7. daemon restart recovers every accepted row and produces no duplicate remote
   settlement.

Capture per-request latency, IPC active/high-water/limit, outbox age, effect
credits, peer circuit state, and session generation as test artifacts. A test
which only observes the final CLI exit code is insufficient.

Use the repository's directory-run gate before directory-level pytest. Every
pytest/script/CLI invocation uses a newly created temporary `HOME`,
`HYPRIAL_HOME`, harness state/socket, cleared managed-worker identity variables,
and `HYPRIAL_NETWORK_ISOLATED=1`. The fake peer and sidecar bind loopback only;
the test never reads or controls the production daemon.

### 8.2 Static CI enforcement

A simple import ban is necessary but insufficient because an IPC handler can
reach transport through an injected service. Add a conservative generated call
graph rooted at the router method table. Mark transport
`put/get/query/reconfigure`, Zenoh liveliness, sidecar control, adapter SDK,
Casdoor, and remote probe ports as network-tainted. CI fails if a tainted call
is reachable from an IPC root without crossing both:

1. a recognized atomic durable-outbox insertion; and
2. a registered asynchronous effect-worker entry point.

Unknown dynamic calls fail closed or require a reviewed boundary declaration.
The runtime owner-context tracer remains the second line of defense for
protocol dispatch and dependency injection which static analysis cannot
resolve.

## 9. Landing plan

Each item lands as a small branch from the then-current `dev`, with behavior
changes covered by a test that first demonstrates the blocked path.

### Stage 0 — prerequisites and baselines

1. Preserve #1632 IPC slot cleanup and saturation diagnostics as the safety
   net; add a 64/64 black-hole regression test.
2. Add invariant tracing/test helpers and current-path metrics without changing
   response behavior.
3. Add operation/freshness fields to contracts as optional and teach CLI/GUI
   to render accepted-versus-settled explicitly.
4. Inventory all unbounded `wait/get/join/result` calls in daemon product code;
   CI fails on a new unannotated unbounded wait.

**Exit:** the current coupling fails deterministic tests and dashboards expose
where capacity is held.

### Stage 1 — remove unbounded local waits

1. Replace lifecycle `answer.get()`, squire ledger `reply.ready.wait()`, and
   harness `_AwaitSettlement` with bounded local admission/completion APIs.
2. Add stable command receipts and read-after-timeout recovery wherever absent.
3. Ensure timeout never resubmits a command which can have performed an
   effect.

This stage is containment. It does not claim network decoupling.

**Exit:** no daemon owner has an unbounded wait; cancellation/outcome-unknown
tests pass.

### Stage 2 — complete inbox, user, and route outboxes

1. Make public inbox send/reply always use the existing deferred-direct durable
   path. Complete the actor response immediately after its transaction.
2. Move user/squire delivery and receipt polling behind inbox dispatch.
3. Add durable route/platform items, including attachment references, and move
   `gateway.send_chat*` entirely to the adapter lane.
4. Keep ack local; publish receipts asynchronously.
5. Add per-target fairness, quotas, and accepted/settled API fields.

**Exit:** black-hole Zenoh and Lark SDK calls cannot delay IPC send/reply/ack or
the inbox writer; restart and idempotency tests pass.

### Stage 3 — OrgFS and organization sagas

1. Stop waiting in `_post_commit_scope`; split local callbacks from remote
   broadcast and drain the existing journal asynchronously.
2. Make space authority admission/result bounded and receipt-based.
3. Convert join/sync/refresh/blob acquisition to durable operations; normal
   reads become local-only.
4. Convert organization bootstrap and Casdoor synchronization to durable saga
   steps.

**Exit:** a blocked mesh/Casdoor cannot hold an OrgFS lock, state writer, or IPC
slot; joins survive restart and expose partial state.

### Stage 4 — workflow/PAC

1. Route all remote mutations through `remote_workflow_outbox`.
2. Convert remote current/inspect/preview to cached projections and async
   refresh operations.
3. Represent remote validation explicitly; do not report completion early.
4. Remove direct `wire.call` from workflow IPC paths.

**Exit:** remote PAC peers may disappear indefinitely without delaying local
workflow admission, status, or unrelated workers.

### Stage 5 — projection-only status

1. Remove route expiry/cleanup and tailnet refresh from `ps`/status reads.
2. Add maintenance owners and coalesced refresh operations.
3. Adapt the directed network-check work to update its durable probe projection
   asynchronously.
4. Make all health/freshness fields available while transport is blocked.

**Exit:** source review and tracer tests prove every status IPC path is
network-free and side-effect-free.

### Stage 6 — session generation cutover

1. Land fixed sidecar slots, then make desired session generation durable and
   reconciliation single-flight.
2. Ensure publishers cannot initiate or wait for rebuild/redial.
3. Add native deadlines or move uncancellable calls behind a killable
   process/sidecar boundary.
4. Land route replay/retry against the new generation lifecycle.
5. Run real-session peer churn, sidecar restart, delayed peer, and shutdown
   tests.

**Exit:** blocked open/put/reconfigure can exhaust only its replaceable effect
owner, never IPC or a domain writer; a healthy peer still progresses.

### Stage 7 — remove compatibility waits

1. Flip all clients to local acceptance plus status polling/subscription.
2. Remove deprecated synchronous settlement response paths.
3. Make the import/context invariant and queue/quota tests required CI gates.
4. Update release notes, contract version, operator runbooks, and rollback
   procedure.

**Exit:** no production command path can opt back into server-side network
settlement waiting.

### 9.8 Reviewable PR cuts

The stages above land as the following ordered PRs, not as one large rewrite.
Every behavior PR starts with a separate failing-test commit which demonstrates
the wait/capacity failure; only a later commit adds the fix. A green test added
with its implementation is not sufficient evidence.

1. **Invariant harness and metrics (PR 1a):** black-hole owner-context tracer,
   IPC high-water metrics, static-call-graph negative fixtures, and additive
   Zenoh link-delete/map-remap diagnostics. Zenoh 1.9 does not expose a remote
   close cause, so this PR reports null rather than guessing when no local
   session-close cause exists. **PR 1b** adds bounded Tailcat `engine.splice`
   connection telemetry: map port/slot, peer, bytes in each direction,
   duration, close side, and both `io.Copy` outcomes, exposed through the
   existing status/diagnostic channel with aggregation or rate limiting and
   red-first Go tests. It does not add per-packet logging.
2. **Fixed-slot transport prerequisite:** rebased protocol-v4 sidecar/daemon
   work and conflict resolution from section 10.1.
3. **Session generation owner:** desired/active projection, single-flight
   reconcile, and no rebuild on remote drop.
4. **Route replay:** rebase/adapt PR #1542 against the generation owner.
5. **Bounded local completions:** lifecycle reservation first, then squire
   ledger and harness settlement as separate commits with durable recovery.
6. **Inbox acceptance:** make the existing deferred durable path the public
   send/reply path; do not add a second outbox.
7. **User and route delivery:** one PR for squire receipt polling and a separate
   PR for adapter text/attachment outbox custody.
8. **OrgFS commit effects:** remove the 60-second post-commit wait and split
   local callbacks from network broadcast.
9. **Org/OrgFS operations:** one PR for join/sync/blob operation records and a
   separate PR for sidecar/Casdoor sagas.
   PR 9 must remove both #1677 `org.create` paths through `ensure_group -> _bearer` and `_casdoor_require_group -> member_state`.
10. **Workflow remote effects:** route mutations through the existing outbox,
    followed by a separate cached inspect/current/preview PR.
11. **Projection-only status:** remove cleanup/refresh from reads and add the
    generic coalesced refresh worker.
12. **Directed probes:** adapt and land PR #1513 on that refresh boundary.
13. **Contract cleanup:** migrate all clients, remove compatibility waits, and
    make invariant checks required CI.

Schema changes are expand-first and precede their producers; removals happen
only in PR 13. PRs 2-4 may proceed in parallel with domain schema preparation,
but no domain producer flips before its durable worker and rollback path are
present.

## 10. Existing and pending network work

Feasibility was rechecked on 2026-10-20 with the requested command,
`git merge-tree --write-tree origin/dev origin/<branch>`, after `origin/dev`
advanced to `36dcdb6e4`. This creates synthetic trees only; no pending branch
was merged into this design branch.

### 10.1 Fixed Zenoh slot pool

**Branches:** `origin/feat/zenoh-slot-connect` and
`origin/intg/sidecar-zenoh-slots`.

Allen's 2026-10-08 ruling is the target behavior: Zenoh keeps one fixed local
slot connect set and recovers remote loss itself; a remote host disappearing
does not rebuild the daemon session. The work contributes sidecar protocol v4,
persisted peer-to-slot assignment, daemon fixed-slot connect wiring, slot
authority/reconciliation, and real Unix/TCP coverage. It does not by itself
move publish/query callers out of IPC.

The actual overlap requiring reconciliation is in these files:

- `tests/fixtures/network/fake_forwarding_sidecar.py`;
- `src/hyprial/shell/impl/cli/commands/doctor/commands.py`;
- `tests/integration/native_harnesses/test_native_resident_tripwires.py`; and
- `tests/shell/cli/lifecycle/test_cli_doctor.py`.

The protocol golden fixtures do not conflict. Retain the current doctor and
native-tripwire coverage while adding the protocol-v4 fixed-slot behavior to
these overlapping files. Do not retain the historical branch's repeated
integration merges: rebase/cherry-pick one coherent protocol, sidecar, daemon,
and E2E series onto current `dev`.

**Landing:** first in the network batch, before session-generation cutover.
**Risk:** protocol v4 is a coordinated daemon/sidecar compatibility change;
upgrade order, Windows TCP slots, persisted assignment migration, capacity
exhaustion, and rollback all require contract and real-session evidence.

### 10.2 PR #1542 — settled-route retry and rebuild-window routes

**Branch:** `origin/fix/session-route-retry`.

The exact merge-tree completed without conflict and produced
`ce969384fa85a9ce380e113b46feda947e18cb05`. Its six commits provide red tests
and fixes for bounded settled-route retry, rebuild-window route replay, and
pruning intents for replaced/retired sessions.

Retain that bounded retry and route coverage for genuine local session
replacement (native close, local configuration, daemon restart). Adapt it so a
remote peer loss under fixed slots does not create a rebuild intent, and so
route replay observes the desired/active generation projection rather than
making an IPC caller wait for session settlement.

**Landing:** rebase after the fixed-slot contract and in the same PR train as
the session-generation API; keep its existing red-test/fix commit ordering.
**Risk:** landing it against the old redial model can legitimize rebuilding on
ordinary peer loss or preserve a synchronous settlement join.

### 10.3 PR #1513 — directed daemon-liveness probe, Stage 1

**Branch:** `origin/feat/network-check-v1`; core commit `b0af8c7ad`.

The exact merge-tree completed without conflict and produced
`a6545f120a08db55bb58e71c8f4303590ba96318`. Reuse its verdict classifier,
directed target selection, append-only RTT ledger, CLI presentation, and
negative controls. Do not land its synchronous `hosts.probe` handler: that
handler performs the Zenoh query while holding the IPC request.

Change the API to atomically enqueue/coalesce a probe and return the latest
persisted probe plus `operationId`. A status/effect worker performs the query
and persists `ok/timeout/fail`; CLI compatibility waits by polling local
operation status outside one long-lived daemon request.

**Landing:** Stage 5 after the generic refresh-operation contract.
**Risk:** probe fan-out can become a denial of service unless it has global and
per-target credits, coalescing, TTL, authorization, and redacted metrics.
Liveliness is never a delivery guarantee.

### 10.4 PR #1558 — native-closed/rebuilding diagnostics

PR #1558 is already on `dev` at merge commit `cff24f1fe`. It contributes the
truthful session projection for native-closed and rebuilding states and keeps
failed-open evidence. Preserve it as the local status source and extend it with
desired/active generation, transition age, and last reconcile error.

No merge is required. It is observability, not decoupling: callers still must
stop initiating or joining open/rebuild/reconfigure. The async supervisor owns
those transitions, while `ps` reads #1558's projection without refresh.

### 10.5 Integrated order

1. Preserve #1558 diagnostics and add the red 64/64 invariant harness.
2. Rebase and land the fixed-slot protocol/sidecar/daemon series.
3. Add durable desired/active session generation and remove caller-initiated
   redial waits.
4. Rebase/adapt #1542 onto that generation model.
5. Land the domain outbox stages from section 9.
6. Adapt and land #1513 only after projection-only status/refresh exists.

For every rebase, record exact conflict resolution in the PR. Run
structural/module gates, focused tests, the full Python suite, sidecar
contracts, and real Zenoh tests under the repository isolation/gating rules.
No branch is a prerequisite merely because it had green historical CI against
an older base.

## 11. Rollout and rollback

Use per-domain flags only during migration:

- `inbox_async_settlement`
- `route_delivery_outbox`
- `orgfs_async_effects`
- `workflow_async_remote`
- `projection_only_status`
- `session_generation_reconcile`

Enable them in that order for canaries. A flag may switch the producer between
old and new formats only while no accepted new-format rows exist. Once rows
exist, rollback disables new admission but leaves the new worker draining; it
must not strand or reinterpret accepted custody. Database migrations are
expand-first and contract-last. Old readers ignore additive operation fields;
new workers can drain all expanded rows before columns or old code are removed.

Rollback never means restoring a network wait in IPC. If an async worker is
faulty, stop its admission, keep local reads/acks/control available, preserve
its outbox, and deploy the corrected worker. Operators can inspect and retry or
terminally fail an operation by stable ID with an audited local command.

Promotion criteria for each flag are zero custody loss, zero duplicate remote
settlement, bounded IPC latency during a black-hole test, stable backlog age,
and successful restart recovery. Disable promotion when any criterion is
UNKNOWN.

## 12. Non-goals

- This plan does not promise immediate remote delivery.
- It does not treat a timeout as remote failure or a liveliness probe as a
  delivery guarantee.
- It does not add a second durable inbox, workflow authority, or OrgFS history.
- It does not let PAC execute worker tasks.
- It does not make shutdown wait forever for network effects. Shutdown records
  remaining durable work and closes within its bounded drain policy.
- It does not hide overload. Local disk/quota exhaustion is an explicit local
  admission failure.

## 13. Completion checklist

The decoupling is complete only when all answers are **yes**:

- Can `ping`, local status, ack, and local durable submission complete while
  Zenoh `put/get/open/reconfigure`, sidecar control, Lark, and Casdoor are each
  blocked indefinitely?
- Can no IPC request own remote work after its response deadline?
- Can no OrgFS/space lock, inbox writer, lifecycle writer, ledger writer, or
  workflow writer reach an effect port?
- Does every accepted remote effect have one durable authority row, a stable
  idempotency key, restart recovery, bounded scheduling, and observable
  settlement?
- Are `ps`, hosts, message status, and workflow status local projections by
  default, with freshness shown rather than hidden refresh?
- Do full-capacity, quota, fairness, crash-boundary, and real-network tests
  pass?
- Are slot-connect, session-route retry, and directed probes adapted to the
  same ownership invariant rather than merely merged?

Until then, bounded timeouts reduce blast radius but the daemon remains coupled
to network health.
