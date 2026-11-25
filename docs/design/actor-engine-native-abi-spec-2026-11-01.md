# Native Actor Engine ABI Specification — `hyprial._core.actors.Engine` v1

Status: P1 deliverable, frozen for `ENGINE_ABI_VERSION = 1` (2026-11-01).
Audience: an independent Rust developer who has never seen this repository's
business code. Everything needed to implement the engine is in this document
plus the conformance suite; no other repository knowledge is required or
assumed. Decision authority: D1 (callback engine), D2 (self-composed Ractor
supervision + dead letters), D3 (v1 scope: actors, supervision, dead letters,
generation-tagged timers, worker pools, two handler kinds), D12
(free-threading readiness) in `docs/design/actor-rustify-prerequisites-2026-11-01.md`.

---

## 1. Scope

You implement one Python extension module: `hyprial._core.actors`, exposing a
class `Engine` and the constant `ENGINE_ABI_VERSION = 1`. The engine hosts
many actors inside one CPython process. It provides:

- mailboxes with bounded admission and strict per-actor FIFO handling;
- one of two handler kinds per actor: a Python callable or a Rust-native
  handler (Section 4);
- a fixed supervision policy machine: restart budget window, capped
  exponential backoff with jitter, per-actor quarantine, generation fencing
  (Section 6), composed from Ractor primitives (Section 6.5);
- dead letters: restart keeps the mailbox; stop and quarantine drain
  remaining messages to a caller-supplied dead-letter callback (Section 7);
- generation-tagged one-shot timers (Section 8);
- worker-pool actors: one logical actor whose payloads are executed by N
  worker threads, with custody-token admission (capacity counts every
  outstanding operation until the token settles) and at-least-once
  completion delivery (Section 9);
- `ask`: send a command and wait for the handler's return value
  (Section 10);
- an event tuple stream and a reason-code dictionary (Section 11).

Non-goals for v1:

- no persistence, no network, no inter-process message transport;
- no message serialization or inspection: messages are opaque Python
  objects; the engine never reads their content;
- no generic supervision trees, no configurable strategies, no one-for-all
  restarts: exactly the one fixed state machine of Section 6;
- no CPU-cost accounting and no default logging sinks: both stay in the
  Python adapter layer that wraps this engine;
- no scheduling of business activity outside the primitives defined here.

## 2. Normative language

The words MUST / MUST NOT / SHOULD / MAY are to be read as in RFC 2119.
Every behavioural clause has at least one conformance case ID from Section 14;
the suite is the executable form of this document. Where this document and
your implementation disagree, this document wins and the suite must be red.

## 3. The ABI (single normative surface)

This section is the only definition of the public surface. No other section
introduces or redefines a public method, parameter, return type or exception;
semantics sections (5–12) reference clauses of this section. Module:
`hyprial._core.actors`. All method names and signatures are exact. `command`
and `payload` always mean opaque Python objects (`Py<PyAny>`); the engine MUST
NOT inspect, serialize, copy-semantics-depend on, or type-check them.

Module-level exports: `ENGINE_ABI_VERSION`, `Engine`, `EffectCompleted`,
`NativeHandlerFactory`, `ActorAdmissionError`, `ActorFailedError`,
`ActorSpawnError`.

### 3.1 Construction

```python
Engine(
    *,
    policies: dict[str, tuple[int, float, float, float, float]],
        # profile -> (max_restarts, restart_window_s, base_backoff_s,
        #             max_backoff_s, jitter_ratio)
    jitter_seed: int | None,          # None -> OS entropy; int -> deterministic
    event_sink: Callable[[tuple], None],
    expected_error_type: type,        # the "expected error" base class
) -> Engine
```

- `policies` MUST be non-empty; unknown profile names are rejected at
  `spawn` / `spawn_pool` (Clause E-1, case ADM-05). Validation errors raise
  `ValueError` (E-8).
- `event_sink` receives every event tuple (Section 11). Sink exceptions MUST
  be swallowed and MUST NOT affect the engine (EVT-03).
- `expected_error_type` is matched with `isinstance` to classify handler
  execution failures as expected rejections (Section 6.2, EXP-01/02). It is
  supplied by the adapter because the type lives in Python. It applies ONLY
  to exceptions raised while executing a command in a handler — never to
  factory calls (6.2) and never to engine-internal faults.

Shipped default policy profiles (the adapter always passes these; treat them
as data, not behaviour):

| profile | max_restarts | window_s | base_s | max_s | jitter_ratio |
|---|---|---|---|---|---|
| `state_authority` | 3 | 60 | 0.1 | 2.0 | 0.1 |
| `external_io` | 5 | 60 | 0.25 | 10.0 | 0.2 |
| `process_lifecycle` | 2 | 120 | 0.5 | 5.0 | 0.1 |

### 3.2 Methods (complete list)

```python
spawn(name: str,
      handler_factory: Callable[[], Handler] | NativeHandlerFactory,
      capacity: int, profile: str,
      dead_letter_sink: Callable[[object, str], None] | None = None,
      event_sink: Callable[[tuple], None] | None = None,
      group: str | None = None,
      key: str | None = None,
      ) -> str                                  # actor_id
spawn_pool(name: str,
           worker_handler_factory: Callable[[], Handler] | NativeHandlerFactory,
           pool_size: int, capacity: int, profile: str,
           dead_letter_sink: Callable[[object, str], None] | None = None,
           event_sink: Callable[[tuple], None] | None = None,
           retry_interval: float = 0.01,
           ) -> str                              # actor_id

tell(actor_id: str, name: str, command: object) -> int
     # 0 = accepted, 1 = overloaded, 2 = closed   (never raises for
     # admission outcomes; Section 5.2)
ask(actor_id: str, name: str, command: object, timeout: float) -> object

send_after(actor_id: str, name: str, delay: float, command: object) -> str
     # timer_id
cancel_after(timer_id: str) -> bool             # True if it was still pending

acknowledge(actor_id: str, name: str,
            operation_id: str, generation: int) -> bool
snapshot(actor_id: str, name: str) -> tuple[str, int, int, int, int]
     # (state, generation, queued, in_flight, failures_in_window);
     # unknown actor -> KeyError
stop(actor_id: str, name: str, timeout: float) -> bool
reset(actor_id: str, name: str) -> bool

drain(actor_ids: list[str] | None, timeout: float) -> tuple[bool, float, list[str]]
shutdown(timeout: float) -> bool

lookup(group: str, key: str) -> tuple[str, str]
     # (actor_id, name) of the live actor holding the pair; a free,
     # released or not-yet-started pair -> KeyError

pool_submit(actor_id: str, name: str, owner_id: str, owner_name: str,
            operation_id: str, generation: int, payload: object) -> int
     # 0 accepted / 1 overloaded / 2 closed (incl. unknown/forged owner:
     # then no token is created)
pool_reserve(actor_id: str, name: str,
             operation_id: str, generation: int) -> int
     # 0 reserved / 1 overloaded / 2 closed — synchronous, O(1),
     # never queued behind worker work
pool_submit_reserved(actor_id: str, name: str, owner_id: str, owner_name: str,
                     operation_id: str, generation: int,
                     payload: object) -> int
     # 0 accepted / 2 closed (no live unsubmitted token for the key, or
     # unknown/forged owner: then the token remains and the caller cancels)
pool_cancel_reservation(actor_id: str, name: str,
                        operation_id: str, generation: int) -> bool

native_handler_factory(name: str) -> NativeHandlerFactory
     # lookup of a Rust-registered factory by name; unknown name -> ValueError
```

`Handler` is the object a factory builds: anything callable as
`handler(command) -> object` (Section 4).

Per-method error contract (exact):

| method | returns | raises |
|---|---|---|
| `spawn` | fresh `actor_id` for keyless and keyed actors | `ValueError` invalid spec / unknown profile / unknown native factory name (E-8); `ValueError` invalid `group`/`key` pair (3.5, KEY-08); `ActorSpawnError` when the factory or pump raises at first start (6.2), including stable reason `key-in-use` for an occupied pair (3.5); `RuntimeError` during an in-progress engine-wide drain, or after engine-wide drained / shutdown (E-7, 5.5) |
| `spawn_pool` | fresh `actor_id` | same as `spawn`; plus `ValueError` when `pool_size < 1` or `retry_interval <= 0` (E-8) |
| `tell` | 0 / 1 / 2, never raises for admission outcomes | — |
| `ask` | handler return object | `TimeoutError`; `ActorAdmissionError`; expected error re-raised; `ActorFailedError` (10) |
| `send_after` | fresh `timer_id` | `KeyError` unknown/forged handle; `ValueError` pool target or `delay` negative (E-8) |
| `cancel_after` | True if the timer was still pending | — (unknown timer_id -> False) |
| `acknowledge` | True when a delivered completion's token was settled | — (no such token -> False) |
| `snapshot` | the 5-tuple | `KeyError` unknown/forged handle (E-6) |
| `stop` | True = stop confirmed (self-stop: accepted, 5.4) | — (unknown/forged handle -> False) |
| `reset` | True = reset executed | — (any state other than quarantined, or while draining -> False) |
| `drain` | `(completed, elapsed_s, remaining)` | `KeyError` unknown id in `actor_ids` |
| `shutdown` | True when all engine threads stopped in time | — (idempotent, LIFE-03) |
| `lookup` | `(actor_id, name)` of the live actor under `(group, key)` | `KeyError` no live actor holds the pair; `ValueError` invalid pair (half, non-string, blank; 3.5) |
| `pool_submit` | 0 / 1 / 2, never raises for admission outcomes | — |
| `pool_reserve` | 0 / 1 / 2, never raises | — |
| `pool_submit_reserved` | 0 / 2, never raises | — |
| `pool_cancel_reservation` | True if a live unsubmitted token was cancelled | — |
| `native_handler_factory` | the registered factory handle | `ValueError` unknown name |

Common argument rules:

- `(actor_id, name)` pairs are opaque handles minted by `spawn` /
  `spawn_pool`. A pair that does not match a live actor record MUST classify
  as closed / `False` / `KeyError` exactly like an unknown id (forged-handle
  rule, ADM-04): `actor_id` alone is not authoritative.
- Timeouts and delays are relative seconds on a monotonic clock. A deadline
  `<= 0` is checked exactly once (no waiting). The suite allows ±50 ms
  tolerance on timing clauses (TOL-1).
- `name` must be non-blank; `capacity >= 1`; `pool_size >= 1`;
  `retry_interval > 0`; violations raise `ValueError` (E-8, ADM-05).
- `spawn`'s `group` / `key` form one optional pair: both given or both
  omitted, each a non-blank `str`, compared without case folding; a half
  pair, a non-string half, or a blank half raises `ValueError` (3.5, KEY-08).
- Serial actors and pool actors are distinct target kinds, fixed at spawn.
  `tell` on a pool returns 2; `ask` on a pool raises
  `ActorAdmissionError(closed)`; `send_after` on a pool raises `ValueError`;
  `pool_*` methods on a serial actor return closed / False (POOLS invalid
  targets, EFF-11).

### 3.3 The completion message

```python
EffectCompleted(operation_id: str, generation: int,
                result: object | None = None,
                error: str | None = None)
```

One concrete, immutable Python type exported by the module; the engine
constructs every instance. Exactly one of the two outcome fields is
meaningful: when the worker handler raised, `error` is the exception type
name and `result` is `None`; when the handler returned, `error` is `None`
and `result` is the returned object (which may itself be `None`). The pool
delivers it to the owner actor named by the submitting call's
`(owner_id, owner_name)` as a plain `tell`. The engine never inspects its
content after construction.

### 3.4 Version

The module MUST expose `ENGINE_ABI_VERSION: int = 1`. The Python adapter
refuses to wire any other value (LIFE-04).

### 3.5 Keyed serial actors (`group` / `key`)

`spawn` may carry a stable `(group, key)` pair (added by the cell-model I2
slice; owner-approved amendment of 2026-11-19). A keyless spawn — both
halves omitted — behaves exactly as before this extension, byte-for-byte,
including its event tuples (KEY-10). Semantics of a keyed spawn:

| operation | A free | B starting | C live | D stopping |
|---|---|---|---|---|
| `spawn(group, key)` | claim B, build and start without the engine key lock, then install C and return the new actor-id; factory or pump failure returns the ordinary spawn error and releases the pair | immediate `ActorSpawnError` with reason `key-in-use`; the factory is not called | immediate `ActorSpawnError` with reason `key-in-use` | immediate `ActorSpawnError` with reason `key-in-use` |
| `lookup(group, key)` | `KeyError` | `KeyError` | `(actor_id, name)` of the actor | `(actor_id, name)` of the actor |
| confirmed stop | — | — | release to A and dead-letter the old mailbox with `stopped` | release to A and dead-letter the old mailbox with `stopped` |
| supervision restart | — | — | remain C with the same key and handle | existing stop behavior |
| engine shutdown | no effect | cancel the start and leave no actor or key when shutdown wins the installation lock | normal actor shutdown and key release | normal actor shutdown and key release |

- Uniqueness: at most one actor occupies a given pair. A keyed spawn first
  claims a free pair in state B before building its handler. Any later spawn
  while the pair is in state B, C or D raises `ActorSpawnError` with the
  stable reason `key-in-use` immediately and does not call its factory. The
  keyed return value has the same actor-id shape as a keyless spawn. The
  existing actor is obtained only through `lookup`.
- Claim before build: the claim is made under the engine key-index lock, then
  the factory and pump start run without that lock. A factory failure or pump
  start failure removes the claim and leaves no actor record, so the pair is
  immediately reusable. Re-entry from the factory naturally observes the
  occupied starting slot and gets `key-in-use`. The pump is established and
  registered before the installation critical section, so an installed keyed
  actor is always fully serviceable and `lookup` never resolves a pair that is
  still starting (KEY-12, KEY-13).
- Release: the pair is released exactly when the actor's stop is confirmed
  (5.4); the remaining mailbox dead-letters with reason `stopped` as usual,
  and the next same-key spawn mints a new actor with an empty mailbox
  (KEY-03, KEY-11). While the actor is `stopping`, `restart_pending` or
  `quarantined`, the pair stays with it: `lookup` returns the original
  actor and a same-key spawn returns `key-in-use` instead of creating a new
  one (KEY-03, KEY-04, KEY-05).
- Supervision restarts keep the record, the mailbox and therefore the pair;
  the handle stays valid across generations (KEY-04).
- `lookup(group, key)` resolves the pair to the live actor's
  `(actor_id, name)`; a free, released, stopped-but-unreleased or
  not-yet-started pair raises `KeyError`; an invalid pair (half,
  non-string, blank) raises `ValueError` (KEY-08).
- Keyed event tuples carry `(group, key)` appended after `restart_delay`;
  keyless tuples keep the eight frozen fields (Section 11, KEY-10).
- Pools have no keys: `spawn_pool` accepts no `group`/`key`.
- Shutdown and keyed installation are decided under the same key-index
  critical section. If shutdown wins, the starter installs no actor or key.
  If installation wins, that actor follows the normal shutdown stop path.
- Linearization (design decision 2026-11-20): a keyed spawn
  linearizes at the moment it installs as live under the key lock. The
  returned actor-id states that this creation was successfully installed; a
  stop or shutdown that lands after the installation and before the caller
  observes the id is not distinguishable from — and needs no distinction
  from — the actor being stopped immediately after `spawn` returned,
  exactly like a keyless spawn. There is no publication protocol and
  shutdown does not wait for an in-flight spawn to return. A start that
  never installed successfully never returns an id: while the pair is
  starting, `lookup` cannot resolve it, and a cancelled or failed start
  leaves no actor, key or pump-registry residue.
- Note (pre-existing behaviour, unchanged and out of I2 scope): after an
  engine shutdown that stopped a normally-installed actor, the native pump
  registry may keep that actor's entry (the fifth review readout 2026-11-20: before
  shutdown `(actors, keys, pumps) = (1, 1, 1)`, after `(1, 0, 1)`; the
  keyless path on origin/dev measures the same `(1, 0, 1)`). This is an
  existing shutdown cleanup gap on origin/dev, not a keyed regression. The
  keyed conformance cases therefore assert, after a shutdown, only the two
  I2 responsibilities — the key index is empty, and a cancelled or failed
  start leaves no actor and no pump registration — and stay silent on
  whether a normally-lived actor's pump registration is cleared.
- Backend note (existing divergence, deliberately not unified in I2): on a
  pump start failure the keyed path wraps the failure in `ActorSpawnError`
  and cleans the claim on every backend, while the reference backend's
  keyless path keeps its origin/dev behavior — the raw start error
  propagates without cleanup. Measured 2026-11-20 (the fourth review) on the reference
  backend with an injected `Thread.start` failure: the keyless spawn raised
  `RuntimeError("injected start")` with the record left behind (`records 1`)
  and a following shutdown hit `RuntimeError("cannot join thread before it
  is started")`; the keyed spawn raised
  `ActorSpawnError("engine failed to start actor: injected pump start
  failure")` with `records 0`, `keys 0`, counters `(0, 0, 0)` and a clean
  shutdown. The native backend cleans and wraps on both paths.

## 4. Handler kinds (D1)

Two handler kinds exist and are treated identically by admission, ordering,
dead letters, timers and pools:

1. **Python callable handler.** `handler_factory` is a Python callable with
   no arguments; the engine calls it once per start (and once per restart,
   Section 6.2) and MUST call the returned object as
   `handler(command) -> object` for each command. The factory is called on an
   engine thread holding the GIL (Section 12).
2. **Rust-native handler.** A `NativeHandlerFactory` is a Rust trait object
   obtained from `native_handler_factory(name)` (3.2). Rust-side shape
   (normative):

   ```rust
   trait NativeHandler: Send + Sync {
       fn handle(&self, command: PyObject) -> PyResult<PyObject>;
   }
   trait NativeHandlerFactory: Send + Sync {
       fn build(&self) -> PyResult<Box<dyn NativeHandler>>;
   }
   ```

   Registration: the extension module maintains a Rust-side registry keyed
   by name. Rust code registers each factory once, during module init,
   before the Python adapter wires anything; duplicate names abort module
   init. Python can only look factories up (`native_handler_factory(name)`,
   unknown name -> `ValueError`, E-8); there is no Python-side registration
   entry point.

   `handle` is called with the GIL held and no engine-internal lock held.
   Native handlers and factories MUST be `Send + Sync`. The engine MUST NOT
   branch on handler kind anywhere in its policy code (D1: one code path).

Factory and instance lifecycle (both kinds, normative):

- the engine calls the factory exactly once per start and once per restart;
  a restarted actor ALWAYS gets a fresh instance built by the factory — the
  engine never reuses a previous generation's handler instance; any state a
  handler needs across commands must be rebuilt inside the factory;
- `spawn_pool` takes a factory, not an instance, exactly like `spawn`: the
  engine calls it once per worker at pool start (pool_size fresh instances,
  one per worker thread); pools do not restart in v1 (9.3), so no per-worker
  rebuild path exists;
- a factory that raises is classified in 6.2 — at first start it fails the
  `spawn` call; at restart it consumes restart budget. A factory failure
  never produces a running actor without a handler.

The engine MUST NOT inspect message content for either kind (D1 retains the
G0 rule that Rust routes nothing by domain message shape).

## 5. Actor state machine and generations

### 5.1 States, counters, and the generation table

Per actor: `state ∈ {running, restart_pending, quarantined, stopping,
stopped}`, `generation` (monotone, first start = 1), mailbox `queued`,
`in_flight`, `failures_in_window` (restart-budget deque, Section 6.3).

There is exactly ONE generation rule: **generation increments exactly when
an actor enters `running`** — the first start sets it to 1, every re-entry
increments it by 1. Nothing else changes generation. The complete transition
table, including the four failure classes:

| # | trigger | generation | state | restart budget | events |
|---|---|---|---|---|---|
| 1 | first start succeeds (factory returned) | set to 1 | → `running` | — | — |
| 2 | handler raises an expected error while executing a command | unchanged | stays `running` | no | `command_rejected` |
| 3 | handler raises any other exception (incl. any `BaseException`, Rust panic) while executing a command | unchanged at failure | `running` → `restart_pending` | +1 | `child_failed`, `restart_scheduled` |
| 4a | restart delay elapsed, factory returns | +1 | `restart_pending` → `running` | — | `child_restarted` |
| 4b | restart delay elapsed, factory raises | unchanged (still the failed generation) | stays `restart_pending` | +1 | `child_failed` (command_type None), `restart_scheduled` |
| 5 | budget exceeded (from row 3 or 4b) | unchanged | → `quarantined` | deque frozen | `child_quarantined`, mailbox drained (reason `quarantined`) |
| 6 | `reset` on a quarantined actor | unchanged now; +1 at re-entry | → `restart_pending`, delay 0 | deque cleared | `restart_scheduled` (delay 0), then `child_restarted` |
| 7 | `stop` accepted | unchanged | any live state → `stopping` | — | — (event at confirmation) |
| 8 | `stop` confirmed | unchanged | `stopping` → `stopped` | — | mailbox drained (reason `stopped`), `child_stopped` |

Consequences (normative):

- an expected error never restarts, never consumes budget and never changes
  the generation (EXP-01/02);
- a restart-factory failure (4b) is a child failure of the generation that
  failed to start: it consumes budget and schedules the next backoff while
  the actor remains in `restart_pending` — there is never a running actor
  without a handler (SUP-06);
- a first-start factory failure never creates an actor record: `spawn`
  raises `ActorSpawnError` (E-10), no generation, no budget, no events, no
  dead letters; `tell`/`snapshot` on that name classify as unknown (SUP-10);
- timers, failure reports and custody are fenced by the generation value
  recorded when they were created (5.3).

### 5.2 Admission (`tell`)

- Count only `queued` against `capacity`; `in_flight` is not counted
  (ADM-02). This is the serial-actor rule; pool actors count outstanding
  custody instead (9.1).
- `queued >= capacity` → return 1 (overloaded). The message is not enqueued
  and the caller retains it.
- Return 2 (closed) without raising when: unknown or forged handle; state is
  `stopping`, `stopped` or `quarantined` (`restart_pending` accepts tells —
  the mailbox is kept across restart, 6.3); the engine is draining the
  actor's cell; the actor's endpoint is gone (ADM-03, ADM-04, STP-05,
  DRN-06).
- Otherwise reserve one queued slot, enqueue, return 0. `tell` MUST NOT
  block on the handler, on locks held across handler execution, or on other
  actors (ORD-04, LIV-01).
- Per-actor order is strict FIFO of accepted tells; concurrent producers
  each observe their own order; no cross-actor ordering is promised
  (ORD-01/02/03).
- At most one handler execution per actor runs at a time (single actor =
  serial; pools are the exception, Section 9) (ORD-01, LIV-05).

### 5.3 Generation fencing

Any late-arriving report or timer whose recorded generation differs from the
actor's current generation MUST be discarded silently: failure reports from
a dead generation and timers scheduled before a restart (SUP-08, TMR-02).
Stop does NOT fence by generation — its timers are cancelled at stop
acceptance and fire-time classification handles the race (5.4, 8). Custody
tokens are keyed `(operation_id, generation)`; operations against a key that
does not exist are rejected (`pool_submit_reserved` → 2, `acknowledge` →
False, `pool_cancel_reservation` → False), never silently re-created
(CUS-06). Snapshot may be read at any time; it never mutates state.

### 5.4 Stop (ordered definition)

`stop(actor_id, name, timeout) -> bool` runs these steps in order:

1. **First call on a live state.** Unknown or forged handle → return
   `False`, no effect. When the actor is `running` / `restart_pending` /
   `quarantined`, this call performs ACCEPTANCE (step 2) and then waits for
   confirmation (steps 4–6) up to its own `timeout`.
2. **Acceptance (exactly once per actor).** State → `stopping`; all pending
   timers of the actor are cancelled; a pending restart delay is cancelled
   (STP-03); admission closes immediately (every later `tell` returns 2).
   Generation unchanged, no event emitted at acceptance. `stop` bumps
   generation never.
3. **Current handler.** A handler executing at acceptance runs to
   completion; its normal events (`command_completed` / `command_rejected`)
   are emitted before any stop-side effect (STP-02).
4. **Confirmation.** The engine stops the actor's backend resources and
   waits up to the calling `stop`'s `timeout`.
5. **Dead letters, then event.** After confirmation: remaining queued
   messages go to the actor's `dead_letter_sink` with reason `"stopped"` in
   admission order (CUS-03); a quarantined actor's mailbox was already
   drained at quarantine, so its stop sends no dead letters; then
   `child_stopped` is emitted and state becomes `stopped`.
6. **Timeout.** If confirmation exceeds the calling `stop`'s `timeout`:
   that call returns `False`, the actor remains `stopping` (admission stays
   closed), NO dead letters are sent for this attempt (CUS-05).

**Repeated and concurrent `stop` calls** (one rule for all of them):

- a call while the actor is `stopping` — whether it is a retry after a
  timeout, a concurrent duplicate, or a self-stop racing an external stop —
  NEVER re-runs acceptance, NEVER re-runs the backend stop and NEVER
  re-drains (CUS-07): it joins the pending confirmation and waits up to its
  own `timeout`, returning `True` if the stop confirms within that wait and
  `False` otherwise;
- a call on an already `stopped` actor returns `True` immediately with no
  side effects (STP-01).

**Self-stop** (the target's own handler calls `stop` on itself): this is the
highest-priority branch and applies in EVERY state, including when an
external stop was already accepted and the actor is `stopping` (R3-2). The
engine recognizes that the caller is the target's current handler execution
and performs acceptance — or, if already accepted, joins the pending stop —
synchronously and NON-BLOCKING, returning `True` meaning "accepted/joined";
it NEVER waits for confirmation, because confirmation necessarily awaits
this very handler's return (blocking would deadlock). Steps 3–5 execute
after the handler returns, on engine threads, from whichever acceptance came
first; exactly one confirmation and one dead-letter drain ever happen
(CUS-07). `stop` called on OTHER actors from inside a handler follows the
external ordering above (STP-02).

Stop of a pool actor follows the same acceptance/confirmation skeleton; its
worker-side semantics are defined in 9.5.

### 5.5 Drain

`drain(actor_ids | None, timeout) -> (completed, elapsed_s, remaining)`:

- the target set is all actors when `actor_ids` is None (engine-wide
  drain), otherwise exactly the listed actors ("cell"); an unknown id in the
  list → `KeyError` before any state changes;
- at start, each targeted actor enters the draining condition: EXTERNAL
  admission closes (later `tell`/`pool_submit`/`pool_reserve` return
  closed, DRN-06) and its pending timers are cancelled; non-targeted actors
  keep open admission (DRN-03). Engine-internal completion custody (9.4) is
  NOT refused by drain closure: a draining owner keeps receiving
  completions, its handler keeps executing and acking — otherwise an owner
  and its pool in the same drain set could never satisfy the wait condition
  below (DRN-09);
- the engine waits until every targeted actor satisfies ALL of:
  `queued == 0`, `in_flight == 0`, and (pools) no outstanding custody token
  (9.1) — completions keep retrying to drained owners and their handlers
  keep executing, so a healthy system acks and drains;
- on success: each targeted idle actor is stopped (5.4 steps 4–5; queues
  are empty so no dead letters); return `(True, elapsed, [])`;
- on timeout: return `(False, elapsed, remaining)` with `remaining` the
  sorted names of targeted actors not yet drained; targeted actors REMAIN
  draining; a later `drain` call on the same set is the retry (DRN-02);
- spawn rules are scoped by drain kind (R4-1): a CELL drain never blocks
  spawning — new actors are unaffected during and after it (DRN-08); an
  IN-PROGRESS ENGINE-WIDE drain rejects `spawn` / `spawn_pool` with
  `RuntimeError` (DRN-07); once an engine-wide drain has COMPLETED (or after
  `shutdown`), the engine is in the drained state and `spawn` /
  `spawn_pool` keep raising `RuntimeError` forever (E-7, DRN-04);
- `shutdown(timeout)` is specified in 9.5, not as a drain-wait of this
  section: it closes global admission and stops serial actors first, then
  pools; completions to already-stopped owners dead-letter and settle their
  tokens (9.4), so pools reach zero tokens and their workers exit. Its
  spawn effect equals the completed engine-wide drain above (DRN-04);
- events: no dedicated drain event exists; the stop confirmations of
  drained actors emit `child_stopped` as usual (DRN-05); generation fences
  apply unchanged during drain (a command accepted before the drain started
  still executes, a timer already admitted to the mailbox still fires into
  the kept mailbox).

## 6. Supervision (D2)

### 6.1 The fixed state machine

```
running --handler failure--> restart_pending --timer--> running   (generation+1 at re-entry)
running --budget exceeded--> quarantined                          (no restart)
quarantined --reset--> restart_pending --timer(delay 0)--> running (generation+1 at re-entry)
any live state --stop--> stopping --confirmed--> stopped           (generation never changes)
```

There is exactly this machine. No trees, no strategy objects, no escalation.
The generation column of the transition table is 5.1; this section only adds
the budget/backoff mechanics.

### 6.2 Failure classification

When a HANDLER raises while executing a command:

- `isinstance(exc, expected_error_type)` → expected rejection: emit
  `command_rejected` with the exception's `code`/`detail` when present; the
  actor keeps running; generation unchanged; budget untouched; queued
  messages unaffected (EXP-01/02).
- anything else, **including any `BaseException` that is not an
  `Exception`** (`SystemExit`, `KeyboardInterrupt`, custom bases) → child
  failure through the machine in 6.1. There is no class of exception that
  escapes supervision (SUP-01, SUP-09/D9). The current pykka engine fails
  this clause (Section 16 documents the measured defect); your engine MUST
  NOT.
- A Rust panic inside engine code or a native handler MUST be caught at the
  FFI boundary and converted to the same child-failure path; it MUST NOT
  unwind across the FFI (E-9, SUP-09).

When a HANDLER FACTORY raises, `expected_error_type` does NOT apply (a
factory has no command to reject):

- at FIRST START: `spawn` raises `ActorSpawnError` (E-10, SUP-10); no actor
  record, no generation, no budget, no events, no dead letters exist;
- at RESTART: the failure is a child failure of the generation that failed
  to start — `child_failed` (command_type None) + budget +1 + next backoff,
  the actor stays `restart_pending` (SUP-06). It never becomes a running
  actor without a handler; budget exhaustion ends in quarantine (row 5 of
  5.1).

### 6.3 Restart budget and backoff

On child failure at time t (generation g):

1. record t in the failure deque; prune entries older than
   `now - restart_window` (SUP-04);
2. if `len(deque) > max_restarts` → state `quarantined`, emit
   `child_quarantined`, drain mailbox to dead letters with reason
   `quarantined` (Section 7); no restart; generation unchanged;
3. else → state `restart_pending` (generation still g), emit
   `restart_scheduled` with `restart_delay` computed as:

   ```
   attempt      = len(failure deque) after step 1   (1-based)
   nominal      = min(max_backoff, base_backoff * 2^(attempt-1))
   offset       = nominal * jitter_ratio * (2*sample - 1)     # sample ∈ [0,1)
   delay        = max(0.0, nominal + offset)
   ```

   With `jitter_ratio = 0` the delay equals the nominal value exactly
   (SUP-05). `jitter_seed` makes `sample` reproducible for tests.
4. after `delay`, re-run `handler_factory`: success → generation g+1, state
   `running`, emit `child_restarted` (5.1 row 4a); failure → 6.2 restart
   case (5.1 row 4b): budget +1 at generation g, next backoff;
5. mailbox: **kept**. The failed command itself is dropped (it already
   ran); every other queued message stays in FIFO order and is executed by
   the new generation (CUS-01). Nothing is returned at restart time; the
   pre-v1 "restarted-undelivered" return is abolished (D2).

The budget window prunes, it does not accumulate across windows: failures
older than the window stop counting (SUP-04).

### 6.4 Quarantine and reset

A quarantined actor rejects `tell` (closed), keeps its last generation until
`reset`, and its pending mailbox was drained at quarantine (CUS-02). `reset`
MUST return `false` for any state other than `quarantined` or while draining
(SUP-07). After `reset`: failure deque cleared, state `restart_pending`,
restart scheduled with delay 0; generation increments when the actor re-enters
`running` (5.1 row 6).

### 6.5 Composition on Ractor (D2)

Implement the machine with Ractor primitives only; no third-party
supervision library:

- one engine supervisor actor; every serial engine actor is started with
  `spawn_linked` to it;
- `SupervisionEvent::ActorFailed` drives 6.2–6.4; the supervisor keeps the
  failure deque per child and re-runs the child's factory via
  `spawn_linked` after the computed delay;
- delays use Ractor `send_after` timer messages that carry
  `(child_id, generation)`; stale timers are dropped by 5.3;
- `ActorTerminated` (clean stop) drives the `stopped` transitions.

Pool actors are NOT supervised children: no pool failure path exists in v1
(9.3), so pools never appear in `SupervisionEvent` handling (EFF-02).

## 7. Dead letters (D2)

`dead_letter_sink(message, reason)` is the only "return path" the engine
has. Rules:

- restart: mailbox kept (6.3.5); **no** dead-letter call (CUS-01);
- `stop` (confirmed): remaining queued messages are delivered to the
  actor's `dead_letter_sink` with reason `"stopped"`, in admission order,
  after the stop is confirmed (CUS-03);
- quarantine: remaining queued messages, reason `"quarantined"` (CUS-02);
- the engine MUST NOT interpret the message; `reason` is always one of the
  two strings above (CUS-04);
- sink exceptions are swallowed (EVT-03);
- if the stop times out (`stop` returns `false`), dead letters MUST NOT be
  sent for that attempt; a later successful stop sends them exactly once
  (CUS-05);
- repeated `stop` calls MUST result in one backend stop and one
  dead-letter drain (CUS-07).

This is a deliberate change from the shipped pykka engine, which returns
never-begun commands at restart with reason `ACTOR_RESTARTED_UNDELIVERED`;
that reason code and that path are abolished. The Python-side reasons
`stopped` / `quarantined` replace `ACTOR_STOPPED_UNDELIVERED` /
`ACTOR_QUARANTINED_UNDELIVERED`.

Undelivered pool completions (owner stopped or quarantined, 9.4) go to the
POOL's `dead_letter_sink` with the owner's reason; the message handed to the
sink is the `EffectCompleted` instance (3.3). No other dead-letter source
exists (EFF-07).

## 8. Timers (D3)

`send_after(actor_id, name, delay, command) -> timer_id`:

- schedule-time validation (synchronous, before any wait): unknown or forged
  handle → `KeyError`; pool target or negative `delay` → `ValueError` (E-8);
  a valid serial actor in ANY state accepts the timer;
- the timer carries the actor's generation at schedule time; at fire time
  the engine classifies exactly once:

  | target state at fire | generation match | outcome |
  |---|---|---|
  | `running` / `restart_pending` | yes | normal admission into the kept mailbox (5.2); a full mailbox retries at the completion-custody cadence (9.4) until accepted, closed, or cancelled |
  | any | no | dropped silently (generation fence, TMR-02) — this is the restart fence that replaces the retired per-owner scheduler component |
  | `stopping` / `stopped` | any | dead-lettered with reason `"stopped"` (TMR-04) |
  | `quarantined` | any | dead-lettered with reason `"quarantined"` (TMR-04) |

  The generation fence covers ONLY restart-lineage invalidation; stop,
  quarantine and drain handle their timers by cancellation plus the
  fire-time classification above, so a timer that fires in the race window
  between cancellation and stop confirmation is never silently lost
  (TMR-05);
- timers are one-shot; `cancel_after(timer_id)` cancels a pending timer and
  returns whether it was still pending; an unknown or already-fired
  `timer_id` → `False` (TMR-03);
- pending timers are cancelled by stop acceptance (5.4), quarantine,
  drain start (5.5), and `shutdown` (TMR-05).

## 9. Worker-pool actors (D3)

A pool is **one logical actor** (`actor_id`) whose accepted payloads are
executed by up to `pool_size` concurrent workers running handler instances
built by the same factory (4). Pools exist to move blocking work off serial
actors. Pool semantics freeze the shipped `EffectLane` behaviour exactly —
reservation-based capacity with acknowledged completion custody and
error-as-completion; this is a deliberate, binding decision (R2): **no pool
behaviour change is in scope for P1.**

### 9.1 Custody admission (capacity = outstanding)

- Every accepted payload creates a **custody token** keyed
  `(operation_id, generation)`. The token is created by `pool_submit` or
  `pool_reserve` and is removed by EXACTLY ONE of: `acknowledge` (owner
  handled the completion), `pool_cancel_reservation` (before submission),
  or owner-death dead-lettering (9.4: the undelivered completion goes to
  the pool's sink and the token is settled in the same step). Worker
  execution and completion delivery alone do NOT remove it.
- `capacity` bounds the number of live tokens: a `pool_submit` or
  `pool_reserve` that would exceed it returns 1 (overloaded) and creates
  nothing. Capacity therefore covers queued + running + completed-awaiting-
  ACK at all times (EFF-01, EFF-08).
- Admission results are the values of 3.2, never exceptions; closed results
  cover unknown/forged handle, stopping/drained/stopped pool, or engine-wide
  drained state.
- Duplicate `pool_reserve` with a live token returns 0 without re-checking
  capacity (idempotent). Duplicate `pool_submit_reserved` of an already
  submitted live token returns 0 and queues nothing (the first payload
  wins). (EFF-10)
- Execution concurrency is `pool_size`; ordering is part of the frozen
  semantics (R2-2): with `pool_size = 1` payloads execute STRICTLY in
  submission order (single worker, FIFO queue — the current `EffectLane`
  guarantee that most sites rely on); with `pool_size > 1` submission into
  the queue is FIFO but worker pickup and execution order are unspecified
  (the one deliberate exception to ORD-01; such pool users fence by
  `operation_id`/`generation` in their own protocol) (EFF-01).

### 9.2 Reservation protocol (reserve before durable commit)

Callers that must guarantee capacity BEFORE an external side effect (for
example before writing a durable record whose continuation needs the pool)
MUST use `pool_reserve` / `pool_submit_reserved` /
`pool_cancel_reservation` (3.2):

- `pool_reserve` MUST be answered from the token table, not through any
  worker queue, so its latency does not depend on pool backlog (EFF-03).
  It preserves the pre-v1 guarantee "reserve before durable commit, submit
  after, cancel if the commit failed".
- `pool_submit_reserved` of a live unsubmitted token MUST succeed
  regardless of any queue depth (the token already owns the capacity slot)
  and binds the `(owner_id, owner_name)` completion destination of that
  call (EFF-04).
- `pool_cancel_reservation` frees the token and returns True only while the
  token is live and unsubmitted; after submission it returns False (the
  payload may already run) (EFF-05).
- Tokens are fenced by their exact key: operations against a key that is
  not a live token are rejected (`pool_submit_reserved` → 2,
  `pool_cancel_reservation` → False, `acknowledge` → False); a new
  reservation under a new generation is a new token (CUS-06).

### 9.3 Worker execution and errors (no pool supervision)

- A worker executes `handler(payload)`; the handler's return value becomes
  `EffectCompleted.result`.
- A worker handler raising ANY exception — expected, unexpected, or any
  `BaseException` (`SystemExit`, `KeyboardInterrupt`, custom bases), or a
  Rust panic caught at the FFI boundary — becomes an `EffectCompleted` with
  `error = type(exc).__name__` and `result = None`, delivered to the owner
  like any completion. The worker finishes that one operation and CONTINUES
  with the next queued payload. A worker error is NEVER a supervision
  failure of the pool: pools do not restart, do not consume restart budget
  and do not quarantine in v1 (EFF-02). Owners read `EffectCompleted.error`
  and settle their own protocol (9.6).
- The pool actor itself has no user-visible handler and no failure path in
  v1; `snapshot` reports it `running` with generation 1 for its whole
  lifetime.

### 9.4 Completion custody (deliver, retry, dead-letter, acknowledge)

- **Owner designation and validation.** `pool_submit` and
  `pool_submit_reserved` designate the completion destination by their
  `(owner_id, owner_name)` arguments; the engine validates that handle
  SYNCHRONOUSLY at submit time: unknown or forged owner → both return 2
  (closed, forged-handle rule ADM-04); for `pool_submit` no token is
  created, for `pool_submit_reserved` the live token REMAINS (the caller's
  protocol cancels it). Because of submit-time validation, delivery never
  faces an unknown owner — the owner record persists in `stopped` state.
- **Delivery.** When a worker finishes, the pool delivers
  `EffectCompleted(operation_id, generation, result, error)` (3.3) to the
  owner as a plain `tell`. Owners and pools are both serial actors; an
  owner may consume completions from many pools;
  `operation_id`/`generation` route the completion inside the owner.
- **Delivery transition table** — every `tell` outcome has exactly one
  consequence (EFF-06/07):

  | tell outcome at delivery | consequence |
  |---|---|
  | 0 accepted (owner `running` or `restart_pending`) | delivered into the owner mailbox; a `restart_pending` owner's kept mailbox holds the completion and the new generation handles and acks it (CUS-01) — restarting owners need NO special branch |
  | owner targeted by an in-progress drain | NEVER refused: completion custody is engine-internal traffic and bypasses drain-closed external admission (5.5); the draining owner's handler receives it, processes it and acks — this is what lets an owner and its pool share a drain set without a wait cycle (DRN-09) |
  | 1 overloaded (owner mailbox full) | token unchanged; retry at the fixed `retry_interval` given at `spawn_pool` (default 10 ms, monotonic, no backoff); retry waiting MUST release the GIL (GIL-02) |
  | 2 closed, owner `stopping` | keep retrying until the owner reaches `stopped` or `quarantined` (stop may still time out; `stopping` is transient, not terminal) |
  | 2 closed, owner `stopped` | terminal: hand the completion instance ONCE to the POOL's `dead_letter_sink` with reason `"stopped"` and settle the token in the same step (EFF-07) |
  | 2 closed, owner `quarantined` | terminal: same, reason `"quarantined"` (EFF-07) |

  Delivery is **at-least-once**: a retry after a successful `tell` (ack
  slower than the interval) MAY duplicate the completion; `acknowledge` is
  idempotent (second call → False). This replaces the pre-v1 retry-forever
  behaviour, which leaked a retry thread per abandoned completion.
- **Acknowledge.** The owner acks from inside its handler after handling
  the completion — including completions whose generation it rejects as
  stale — with `acknowledge(pool_actor_id, pool_name, operation_id,
  generation)`: the token is settled and its capacity slot freed; `False`
  means no such live token exists (EFF-06/08). Never acknowledging while
  the owner stays alive is a caller defect: the token pins capacity; there
  is no timeout in v1.
- `pool_submit` is `pool_reserve` + `pool_submit_reserved` in one atomic
  step with the same token semantics.

### 9.5 Pool stop, drain, shutdown

- Stop acceptance (5.4) closes token creation (`pool_submit`/
  `pool_reserve` → 2) and cancels nothing else: already-submitted payloads
  still execute and completions still retry.
- Worker threads exit only when the submitted queue is empty AND every
  token is settled (acked, cancelled, or dead-lettered by owner death).
  `stop(timeout)` returns True only then; otherwise it stays `stopping` and
  returns False (retryable). (EFF-09)
- Drain counts a pool as drained when `queued == 0`, `in_flight == 0` and
  no live token remains (5.5).
- Engine `shutdown(timeout)` closes global admission, then stops serial
  actors FIRST, then pools: completions to already-stopped owners take the
  `stopped` terminal row above (dead-letter + token settle), so every pool
  reaches zero tokens, its workers exit, and `shutdown` returns True when
  all engine threads stopped within `timeout` (LIFE-01/02). It is not the
  drain-wait of 5.5; its spawn effect equals the completed engine-wide
  drain (5.5, DRN-04).

### 9.6 What stays outside the engine

The engine does not know why callers reserve, what a completion means, or
how results settle downstream. Two-phase business protocols, durable-commit
ordering, result bookkeeping and owner-side stale-generation rejection stay
in the Python adapter and owners.

## 10. `ask`

`ask(actor_id, name, command, timeout) -> object` returns the handler's
return value (handlers may return values; plain `tell` ignores them):

- admission failure → `ActorAdmissionError` (carries overloaded/closed)
  (ASK-04);
- timeout → `TimeoutError`; the command MAY still execute afterwards; the
  caller MUST NOT assume cancellation (ASK-02);
- expected error → raised as-is (ASK-03);
- other child failure → `ActorFailedError` (ASK-03);
- success → the handler's return object (ASK-01);
- `ask` MUST release the GIL while waiting and MUST NOT block the target's
  handler for other callers (LIV-03, ASK-01).
- closed target → `ActorAdmissionError(closed)`; never a bare `KeyError`
  from `ask` (ASK-04); a pool target classifies as closed (3.2).

## 11. Events and reason codes

Event tuple (Section 3 of the port design, frozen):

```
(kind, actor_id, name, generation, command_type | None, code | None,
 detail | None, restart_delay | None)
```

A keyed actor's tuple appends `group, key` (both `str`) after
`restart_delay`; a keyless actor's tuple keeps exactly the eight fields
above (3.5, KEY-10).

`command_type` is `type(command).__name__` when a command is associated,
else `None`. `kind` is one of:

| kind | when | extra fields |
|---|---|---|
| `command_completed` | handler returned (after return, never before) | command_type |
| `command_rejected` | expected error | command_type, code, detail |
| `child_failed` | supervised failure (any non-expected exception incl. `BaseException`) | code=exception type name, detail |
| `restart_scheduled` | backoff timer set | restart_delay |
| `child_restarted` | factory re-ran successfully | — |
| `child_quarantined` | budget exceeded | code, detail |
| `child_stopped` | stop confirmed | — |

Ordering: `child_failed` is emitted before `restart_scheduled`; delivery of
events for one transition is in this table's order (EVT-01). Events are
best-effort diagnostics, never control flow: a sink exception changes
nothing (EVT-03). Per-actor spec-level `event_sink` (if given at spawn)
receives the actor's events in addition to the engine-level sink; routing
must be: spec sink first, then engine sink (EVT-02).

Reason-code dictionary (closed set):

| code string | meaning |
|---|---|
| `stopped` | dead letters: actor stopped |
| `quarantined` | dead letters: actor quarantined |
| `overloaded` | admission rejected, mailbox full |
| `closed` | admission rejected, actor not running / unknown / draining |

Expected-error `code`/`detail` values are supplied by the Python side via
the `expected_error_type` instance and pass through verbatim.

## 12. Threading, GIL, re-entrancy, finalization

- Every callback into Python (`handler_factory`, `handler`, native
  `handle`, `event_sink`, `dead_letter_sink`) happens on an engine thread
  **holding the GIL and holding no engine-internal lock** (GIL-01). Acquire
  what you need, let go of engine locks, then call.
- Handlers may re-enter the engine: `tell`, `ask`, `send_after`,
  `pool_reserve`, `stop` on **other** actors from inside a handler MUST
  work; an actor telling itself enqueues behind the current command and
  MUST NOT deadlock (ORD-04, LIV-04); a handler stopping itself follows the
  non-blocking self-stop ordering of 5.4 (STP-02).
- `tell` is non-blocking; `ask`, `stop`, `drain`, `shutdown` and completion
  retry waits MUST release the GIL while blocked (GIL-02).
- A blocked handler must not stall other actors: with `pool_size = 1`
  serial actors the engine must still schedule other actors' handlers
  concurrently (thread per actor or equivalent scheduling) (LIV-01).
- The engine MUST NOT rely on the GIL for its internal data structures:
  all engine state is guarded by engine-owned synchronization that stays
  correct if the GIL disappears (D12, FREE-01).
- Callbacks MUST NOT assume cross-actor serialization: two actors' handlers
  may run in parallel today (GIL releases between interpreter
  instructions), and will truly parallelize on free-threaded builds
  (FREE-02).
- Process exit: the Python adapter installs the `atexit` hook that calls
  `shutdown`. The engine MUST NOT acquire the GIL after interpreter
  finalization has begun; actor threads are daemon threads; after a
  successful `shutdown(timeout)` no engine thread remains (LIFE-01/02).
- `shutdown` is idempotent; a second call returns the first result without
  side effects (LIFE-03). `spawn` raises `RuntimeError` during an
  in-progress engine-wide drain and after the engine is drained or shut
  down; a cell drain never blocks spawning (E-7, DRN-04/07/08, 5.5).
- v1 targets standard-GIL CPython 3.12+; importing on a free-threaded
  build MUST NOT crash: with PyO3 ≥ 0.28 declare the module GIL-compatible
  (`gil_used = false`) so free-threaded interpreters do not re-enable the
  GIL because of this module (FREE-03). Behaviour on free-threaded builds
  is exercised by a non-blocking CI job, not required to pass in v1
  (D12).

## 13. Error mapping

| condition | raised / returned | clause |
|---|---|---|
| blank name, capacity < 1, pool_size < 1, retry_interval <= 0, negative timer delay, unknown profile, invalid policy table, unknown native factory name | `ValueError` at the call | E-8 |
| factory raises at first start | `ActorSpawnError` (cause = factory exception); no actor record | E-10 |
| `spawn`/`spawn_pool` during an in-progress engine-wide drain, or after engine-wide drained / shutdown | `RuntimeError` (cell drain never blocks spawn) | E-7, 5.5 |
| `snapshot` of unknown/forged handle | `KeyError` | E-6 |
| `send_after` of unknown/forged handle | `KeyError` | E-6 |
| `tell` admission outcomes | `0 / 1 / 2`, never an exception | E-1 |
| `pool_submit` / `pool_reserve` / `pool_submit_reserved` outcomes | `0 / 1 / 2` (0/2 for submit_reserved), never an exception | E-2 |
| `ask` timeout | `TimeoutError` | E-3 |
| `ask` on closed/overloaded (incl. pool targets) | `ActorAdmissionError` | E-4 |
| `ask` expected error | the original exception instance | E-5 |
| `ask` other failure | `ActorFailedError` | E-5 |
| Rust panic | converted to child failure / `EffectCompleted(error=…)` / `RuntimeError` at FFI; never unwinds across | E-9 |
| deadline `<= 0` | checked once, no wait | TOL-2 |

`ActorAdmissionError`, `ActorFailedError` and `ActorSpawnError` are Python
exception classes exported by the module; the adapter re-exports them.
`ActorSpawnError` chains the factory exception as its `__cause__`.

## 14. Conformance suite: case ID ↔ clause table

The suite lives at `conformance/actor_engine/` in the repository (batches
P3a/P3b; the reference Python backend `reference_abi.py` is the executable
oracle until your backend lands). Parameterized by
`--actor-backend=pykka | reference-abi | native`. Your backend MUST run the
same cases with native handlers where the case says so.

| group | case | clause(s) |
|---|---|---|
| ADM | ADM-01 accepted command executes | 5.2 |
| ADM | ADM-02 queued-full → overloaded; in_flight not counted | 5.2 |
| ADM | ADM-03 closed after stop acceptance / quarantined / draining (restart_pending still open) | 5.2 |
| ADM | ADM-04 unknown and forged handles → closed | 3.2 |
| ADM | ADM-05 invalid spec → ValueError (blank name, capacity<1, pool_size<1, retry_interval<=0, unknown profile) | 3.1, E-8 |
| ORD | ORD-01 per-actor FIFO and single-handler serialization | 5.2 |
| ORD | ORD-02 concurrent producers keep per-producer order | 5.2 |
| ORD | ORD-03 10 000-message soak keeps order | 5.2 |
| ORD | ORD-04 self-tell from handler does not deadlock | 5.2, 12 |
| EXP | EXP-01 expected error → rejected event, actor survives, generation unchanged, budget untouched | 5.1 row 2, 6.2 |
| EXP | EXP-02 subclass of expected type behaves the same | 6.2 |
| SUP | SUP-01 failure → restart, generation+1 at re-entry, child_restarted | 5.1 row 3/4a, 6.1 |
| SUP | SUP-02 only the failed child restarts | 6.1 |
| SUP | SUP-03 budget exceeded → quarantined, generation unchanged | 5.1 row 5, 6.3 |
| SUP | SUP-04 window prunes old failures | 6.3.1 |
| SUP | SUP-05 backoff formula exact at jitter_ratio=0; jitter bounds | 6.3.3 |
| SUP | SUP-06 restart factory failure consumes budget, stays restart_pending, never a running actor without handler | 5.1 row 4b, 6.2 |
| SUP | SUP-07 reset only from quarantined; reset restarts at delay 0; generation+1 at re-entry | 5.1 row 6, 6.4 |
| SUP | SUP-08 stale failure reports dropped (generation fence) | 5.3 |
| SUP | SUP-09 BaseException (`SystemExit`, `KeyboardInterrupt`, custom base) and Rust panic → child failure | 6.2, E-9 |
| SUP | SUP-10 first-start factory failure → ActorSpawnError, no actor record, tell/snapshot classify unknown | 5.1, 6.2, E-10 |
| CUS | CUS-01 restart keeps mailbox; failed command dropped; no restart-time dead letters | 6.3.5, 7 |
| CUS | CUS-02 quarantine drains mailbox, reason `quarantined`; later stop sends no dead letters | 5.4, 7 |
| CUS | CUS-03 confirmed stop drains mailbox, reason `stopped`, admission order, after confirmation | 5.4, 7 |
| CUS | CUS-04 engine never interprets dead-lettered messages | 7 |
| CUS | CUS-05 stop timeout → no dead letters; retryable; later success sends once | 5.4, 7 |
| CUS | CUS-06 custody token fencing: operations against non-existent keys rejected, no silent re-creation | 5.3, 9.2 |
| CUS | CUS-07 repeated stop → one backend stop, one drain | 5.4, 7 |
| STP | STP-01 repeated/concurrent stop and stop on stopped: joins confirmation (own timeout, no re-acceptance, no re-drain); stopped → True; exactly one child_stopped, generation unchanged | 5.4 |
| STP | STP-02 self-stop in BOTH orders (external stop accepted first, or self-stop first): non-blocking accept/join returning accepted; current handler completes first; exactly one confirmation after; no deadlock | 5.4 |
| STP | STP-03 stop during restart_pending cancels restart | 5.4 |
| STP | STP-04 reset rejects non-quarantined | 6.4 |
| STP | STP-05 tell/ask during stopping/stopped/endpoint-gone → closed / ActorAdmissionError | 5.2, 5.4 |
| DRN | DRN-01 drain completes when targets reach (queued 0, in_flight 0, no live tokens) and stop | 5.5 |
| DRN | DRN-02 drain timeout → (False, elapsed, remaining sorted); targets stay draining; retry continues | 5.5 |
| DRN | DRN-03 cell drain does not touch non-targeted actors' admission | 5.5 |
| DRN | DRN-04 spawn after engine-wide drain completes raises RuntimeError | 5.5, E-7 |
| DRN | DRN-05 drained actors stop normally (child_stopped); fences apply during drain | 5.5, 11 |
| DRN | DRN-06 tell/pool_submit during drain → closed | 5.5 |
| DRN | DRN-07 spawn during an in-progress engine-wide drain raises RuntimeError | 5.5, E-7 |
| DRN | DRN-08 spawn during and after a cell drain is allowed; new actors unaffected | 5.5 |
| DRN | DRN-09 owner+pool in the same drain set, worker completes after drain start: completion admitted to the draining owner, acked, token settles, both drain; no wait cycle | 5.5, 9.4 |
| EVT | EVT-01 event fields and transition order | 11 |
| EVT | EVT-02 spec-level + engine-level sink routing | 11 |
| EVT | EVT-03 sink exceptions isolated | 11 |
| SNP | SNP-01 snapshot fields accurate (serial and pool meanings) | 3.2, 9.3 |
| SNP | SNP-02 unknown actor → KeyError | E-6 |
| ASK | ASK-01 ask returns handler value; GIL released while waiting | 10 |
| ASK | ASK-02 ask timeout; command may still run | 10 |
| ASK | ASK-03 expected error re-raised; other failure → ActorFailedError | 10 |
| ASK | ASK-04 closed/overloaded/pool-target → ActorAdmissionError | 10, 3.2 |
| LIV | LIV-01 blocked handler does not stall other actors | 12 |
| LIV | LIV-02 200 stopped actors return resources to baseline | 12 |
| LIV | LIV-03 stop/drain/ask/completion-retry release GIL while waiting | 12, 9.4 |
| LIV | LIV-04 self-tell enqueues behind the current command | 5.2, 12 |
| LIV | LIV-05 pool concurrency honored | 9.1 |
| TMR | TMR-01 timer fires after delay | 8 |
| TMR | TMR-02 timer fenced by generation (restart lineage); mismatch → silent drop | 5.3, 8 |
| TMR | TMR-03 cancel_after returns pending state; unknown/fired → False | 8 |
| TMR | TMR-04 fire-time classification: full mailbox retries; stopping/stopped/quarantined → dead letters with reason | 8 |
| TMR | TMR-05 stop acceptance/quarantine/drain/shutdown cancel pending timers; cancellation-vs-fire race never silently lost | 5.4, 5.5, 8 |
| TMR | TMR-06 schedule-time validation: unknown → KeyError; pool target / negative delay → ValueError | 8, E-8 |
| EFF | EFF-01 custody capacity counts queued+running+awaiting-ACK; pool_size=1 executes strictly in submission order; pool_size>1 pickup/execution order unspecified | 9.1 |
| EFF | EFF-02 worker error (any BaseException, Rust panic) → EffectCompleted(error=…); worker continues; no pool restart/quarantine | 9.3 |
| EFF | EFF-03 reserve O(1), not queued behind backlog | 9.2 |
| EFF | EFF-04 submit_reserved succeeds regardless of queue depth; binds owner | 9.2 |
| EFF | EFF-05 cancel_reservation after submit → False; live unsubmitted → True and frees slot | 9.2 |
| EFF | EFF-06 completion retried until acknowledge (mailbox full, owner restarting); at-least-once duplicates allowed; ack idempotent | 9.4 |
| EFF | EFF-07 owner stopped/quarantined → completion once to pool dead-letter sink with owner reason, token settled in the same step; stopping/draining owners keep retrying | 9.4, 7 |
| EFF | EFF-08 ack frees capacity; unacked token pins capacity while the owner is alive (no timeout in v1); owner-death dead-letter settles the token | 9.1, 9.4 |
| EFF | EFF-09 pool stop: queue executes, custody settles (ack, cancel, or owner-death dead-letter), workers exit only then | 9.5 |
| EFF | EFF-10 duplicate reserve / submit_reserved are idempotent | 9.1 |
| EFF | EFF-11 wrong target kind: tell/ask/send_after on pool, pool_* on serial actor | 3.2 |
| EFF | EFF-12 EffectCompleted fields exactly (operation_id, generation, result, error); delivered as plain tell to the designated owner | 3.3, 9.4 |
| EFF | EFF-13 submit-time owner validation: unknown/forged owner → pool_submit returns 2 without creating a token; pool_submit_reserved returns 2 with the token remaining | 9.4 |
| LIFE | LIFE-01 clean subprocess exit with live actors and pools | 12, 9.5 |
| LIFE | LIFE-02 no engine thread survives shutdown | 12 |
| LIFE | LIFE-03 shutdown idempotent | 12 |
| LIFE | LIFE-04 ENGINE_ABI_VERSION mismatch refused by adapter | 3.4 |
| ADM/TOL | TOL-1 timing tolerance ±50 ms | 3.2 |
| TOL | TOL-2 deadline <= 0 checks once | 3.2 |
| FREE | FREE-01 engine state correct without GIL reliance (review + stress) | 12 |
| FREE | FREE-02 callbacks not serialized across actors (stress) | 12 |
| FREE | FREE-03 module importable on free-threaded build without re-enabling GIL | 12 |
| KEY | KEY-01 idle key is claimed; keyed spawn returns the actor-id shape | 3.2, 3.5 |
| KEY | KEY-02 starting key rejects immediately without running the second factory | 3.5 |
| KEY | KEY-03 live, restart_pending and quarantined keys reject spawn; lookup returns the actor | 3.5, 5.1 |
| KEY | KEY-04 stopping key rejects until confirmed stop releases it | 3.5, 5.4 |
| KEY | KEY-05 supervision restart keeps the pair and handle while spawn is rejected | 3.5, 6 |
| KEY | KEY-06 factory and pump-start failures release the pair for reuse | 3.5, 6.2 |
| KEY | KEY-07 factory reentry is key-in-use and outer success or failure settles the pair | 3.5 |
| KEY | KEY-08 invalid key forms on spawn and lookup raise ValueError | 3.2, 3.5 |
| KEY | KEY-09 eight concurrent first spawns yield one success and seven key-in-use refusals | 3.5 |
| KEY | KEY-10 keyed events append the pair and keyless events keep eight fields | 3.5, 11 |
| KEY | KEY-11 confirmed stop dead-letters the mailbox and respawn starts empty | 3.5, 5.4, 7 |
| KEY | KEY-12 pump-start hold: lookup stays absent and a second spawn is key-in-use | 3.5 |
| KEY | KEY-13 shutdown during pump start resolves in one lifecycle decision (shutdown wins / installation wins) | 3.5, 9.5 |
| KEY | KEY-14 pump-start failure and a concurrent shutdown leave no registry residue | 3.5, 6.2 |
| KEY | KEY-15 confirmed stop and a same-key spawn race in both orders without a third outcome | 3.5, 5.4 |
| KEY | KEY-16 shutdown while stopping confirms the stop and releases the pair | 3.5, 5.4, 9.5 |

Pass criteria (release gate): three consecutive fully green runs of
`--actor-backend=native` for all cases; the same suite stays green on
`pykka` and `reference-abi`.

## 15. How to run the suite

```bash
# build your extension into the current virtualenv
maturin develop -m crates/hyprial-py/Cargo.toml

# run the conformance suite against your backend
uv run --python 3.12 pytest conformance/actor_engine \
    --actor-backend=native -q

# the suite MUST FAIL CLOSED when the native module is absent:
# a missing `hyprial._core.actors` is an error, never a skip.
```

CI: `pykka` and `reference-abi` jobs are mandatory on every change; a
`native` job runs on the integration branch. Suite layout and the import
closure case (the suite must not import any business package) are defined in
the suite's own README.

## 16. Non-normative: measured pykka baseline

Recorded 2026-11-01 (`docs/notes/actor-rustify-p1-2026-11-01/`,
`bench_engine_overhead_results.json`, Darwin arm64, CPython 3.12.13, one
producer, one `state_authority` actor, capacity 128, N=20 000, producer
spin-retries on overloaded; engine share = (wall − handler CPU)/wall):

| handler work | wall/msg | handler CPU/msg | engine overhead/msg | engine share |
|---|---|---|---|---|
| no-op | 26.4 µs | ~0.2 µs | ~26 µs | 99.4 % |
| ~100 µs | 103.2 µs | 79.0 µs | ~24 µs | 23.4 % |
| ~1 ms | 872.4 µs | 811.5 µs | ~61 µs | 7.0 % |

Plain `tell` admission costs ≈ 22 µs on this machine. Read: for short
handlers the engine pipeline dominates, so the native engine's first win is
throughput of small commands; for ≈1 ms handlers the engine is noise.
Numbers are machine-specific and non-normative.

Measured pykka defect your engine fixes (SUP-09): with the shipped engine, a
handler raising `SystemExit`, `KeyboardInterrupt` or any custom
`BaseException` kills the actor thread **silently** — no `child_failed`
event, no restart, no budget consumption; `snapshot` keeps reporting
`running`, queued commands are never delivered anywhere, and every later
`tell` returns closed forever. See
`probe_base_exception_results.json` for the recorded probe output.

## 17. Clause index

E-1 admission results as values (3.2) · E-2 pool admission results as
values (13) · E-3 TimeoutError (10) · E-4 ActorAdmissionError (10) · E-5
expected-error passthrough / ActorFailedError (10) · E-6 KeyError (3.2) ·
E-7 RuntimeError spawn/drain lifecycle (5.5, 12) · E-8 ValueError
validation (3.1) · E-9 panic containment (6.2) · E-10 ActorSpawnError
first-start factory failure (6.2) · TOL-1/2 timing (3.2).
