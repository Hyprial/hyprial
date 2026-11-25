# Authority set: external apply loop and harness abstraction (2026-11-09)

Status: **approved direction, implementing in a new Draft PR**. The owner decided
on 2026-11-09 (verified Lark p2p `aeacf811`, 08:45:11Z): close #2607, carry its
usable content into a new PR, and dispatch a PAC to implement it. The same
message requires a harness abstraction: Claude is supported now; Codex, Pi and
others must be addable behind the same seam.

A prior message (`b9f84612`, 08:36:55Z) fixed the mechanism:

> 1. 现在,harness添加权限需要重启(主要说的是headless harness)
> 2. 在harness外部添加一个py循环,根据owner的要求修改权限并重启
> 3. 当cell model 上线后,这个工作也提供给agent cell 的head harness,用于调度和修改cell中harness的权限
>
> 注意lark 消息的处理不在这个工作中,lark的消息发给agent后,agent应该转化为对应的authority set任务来修改harness权限,而不是直接把authority set 和IM adapter接线

This document is the design source for `feat/authority-set`. It is not a
rollout plan: nothing here is wired into the daemon or CLI yet.

## 0. Current reading (reproducible)

| # | Reading | Source |
|---|---|---|
| R1 | Claude headless launch reads `allowedTools` / `disallowedTools` once, when the worker process starts | `src/hyprial/daemon/impl/harnesses/agent_sdk/__init__.py:390-396` |
| R2 | The lists come from the frozen `WorkerChannel` (`allowed_tools` / `disallowed_tools`) | `src/hyprial/daemon/impl/harnesses/turn_delivery/channel/worker_channel.py:71-78`, built at `:180-184` from `WORKER_HARNESS_TOOLS` + `WORKER_CLI_ALLOW_TOOLS` (`:52`) + `WORKER_CLI_DENY_TOOLS` (`:55`) |
| R3 | The worker receives the lists through `HYPRIAL_AGENT_SDK_OPTIONS` and validates them at startup | `src/hyprial/daemon/impl/harnesses/agent_sdk/_agent_sdk_worker.py:203-216` |
| R4 | There is no `agent.restart`; the only ways to get a new policy into a running worker are stop and start, which mint a new process | `request_catalog/agent/` has attach/entity/examples/lifecycle/provider/relations/rename/start; no authority method (`git grep -n authority_set origin/dev` at `ba444491` is empty) |
| R5 | The capability table is the existing harness abstraction: one row per (harness, headless) | `src/hyprial/daemon/impl/harnesses/capabilities.py:106-329`; `declare` / `support` at `:347-362` |
| R6 | #2607 head `5a8d94c7` is a per-tool-call `can_use_tool` approval pipe: an in-memory owner store with an 8-character base32 short code and a native reply route. It does not touch `allowedTools` / `disallowedTools`, does not restart a worker, and is not an authority set | closed PR #2607; diff +1841/−8 across 4 files |

R1-R3 are why an authority change is a **task plus a restart**, not a live
prompt: the harness only asks the operating system for a process at launch, and
the permission set is part of that launch description.

## 1. Terms

- **Authority set**: a versioned, audited request to change one managed
  harness's launch-time allow/deny policy. It is a task, not a question.
- **Target**: the one harness an authority set applies to,
  `(kind, name, harness, headless)`. `kind` is `agent` today and `cell` from the
  cell-model phase on.
- **Apply loop**: the Python loop outside the harness. It consumes a pending
  task, restarts the target, and marks the task applied only after the target
  acknowledges the exact policy.
- **Policy digest**: a canonical SHA-256 over `(harness, headless, allow, deny)`
  with rules sorted. It is deliberately independent of the task version: two
  tasks carrying the same effective policy have the same digest.

## 2. Task model (`src/hyprial/daemon/impl/authority_set/model.py`)

An `AuthoritySet` carries:

| Field | Meaning |
|---|---|
| `target` | `AuthorityTarget(kind, name, harness, headless)`; `harness` must be in `DECLARED_HARNESSES` |
| `allow`, `deny` | The harness's existing tool-rule strings, verbatim; this layer never parses or rewrites them |
| `version` | Positive integer, monotonically increasing per target; a stale version is rejected, never applied |
| `source` | Who requested the change (owner / agent / operator), bounded text |
| `task_id` | Idempotency key for the task carrier |
| `created_at_ms` | Task creation time, ordered by the store |
| `status`, `detail` | Lifecycle state; `detail` is bounded and never contains the rules |

Validation is fail-closed (`AuthoritySetError`, never a permissive default):

- rules are strings, non-empty, at most 256 rules of at most 200 characters;
- duplicates are rejected;
- an allow/deny overlap is rejected because the harness evaluates deny before
  allow, so an overlapping task has no single meaning;
- control characters and leading/trailing spaces are rejected;
- `version` must be a positive `int`; `created_at_ms` a non-negative `int`.

`policy_digest()` is the digest described in §1. `resolved_policy()` returns the
harness-neutral `HarnessPolicy(allow, deny, digest)` the adapter will map.

### 2.1 Ready handshake

A restarted harness is trusted only if its ready frame acknowledges the policy:

```json
{"authority": {"enabled": true, "protocol": 1,
               "policyDigest": "<sha256>", "version": 3}}
```

`require_policy_ack` rejects a missing/disabled acknowledgement, a protocol
mismatch, a different digest, or a different version. The frame carries no rule
text. This is carried over from #2607's `require_authority_capability`; a launch
that does not acknowledge the policy is a failed apply, not a warning.

## 3. Durable store (`store.py`)

Append-only JSONL; the last record per target wins.

- `append`: one canonical JSON object per line, `O_APPEND` + `O_NOFOLLOW`,
  created `0600`, `fsync` before returning.
- `load`: strict. A missing trailing newline, an empty line, duplicate JSON
  keys, `NaN`/`Infinity`, an unknown/missing key, an invalid rule list, or an
  unsupported schema raises `AuthorityStoreError`. A permission-task store that
  silently skips a record is a privilege-escalation surface, so the reader never
  guesses.
- `pending()`: the newest record per target, returned only while its status is
  `pending`, sorted by `(created_at_ms, task_id)`. An older pending record is
  superseded by a newer record for the same target; history is not rewritten.
- `current(target)`: the last `applied` record for the target, even if a later
  task rolled back. A rollback restores this policy, so this is the effective
  policy.

## 4. Apply loop (`supervisor.py`)

`AuthoritySupervisor.run_once()` walks `store.pending()` sequentially, one target
at a time. For each task:

1. Refuse `cell` targets: they belong to the cell head harness (§6).
2. Find an adapter by harness; no adapter means reject without a restart.
3. Compare with `store.current(target)`; a task whose version is not newer is
   rejected.
4. Ask the adapter for the resolved policy.
5. Audit `apply.begin` and append the `applying` snapshot **before** touching
   the process. If the audit write fails, nothing is restarted.
6. `lifecycle.stop(target)`; `lifecycle.start(target, policy)`;
   `lifecycle.wait_ready(target, timeout)`.
7. `adapter.verify_ready(policy, ready, version=...)`. Only then audit
   `apply.applied` and append the `applied` snapshot.
8. On any failure in 6-7: audit `apply.rollback`, stop the target, start it with
   the last applied policy, wait and verify again. Success is `rolled_back`.
   With no previous set, finish `failed` with
   `"...; no previous authority set; target left stopped"` and alert.
9. A rollback that itself fails finishes `failed` and alerts. A partial
   application is never reported as applied.

One target's failure does not stop the remaining targets. A corrupt store
propagates out of `run_once`: the loop must not guess which tasks exist.
`run_forever(stop_event, interval_seconds=...)` is the long-running form; the
daemon owns the thread and the stop event in a later slice.

The audit record contains `action`, `taskId`, `version`, `target`, `harness`,
`digest`, `status`, `detail`, `source`, plus `atMs`. It is written before the
store snapshot, and the audit write's failure aborts the step.

## 5. Harness abstraction

Two pieces, both already visible in the tree:

### 5.1 Capability row

`Capability.PERMISSION_SET = "permission_set"` was added to
`src/hyprial/kernel/impl/facts/capabilities.py:18`. Tonight's rows:

| Harness, mode | Level | Note |
|---|---|---|
| `claude`, headless | native (`agent_sdk`) | the Agent SDK launch fields are the policy |
| `claude`, interactive | unsupported | operator-owned; the loop manages daemon-launched workers |
| `pi`, headless | unsupported | no launch-time allow/deny wiring yet; adapter seam reserved |
| `pi`, interactive | unsupported | operator-owned |
| `codex`, headless | unsupported | codex exposes approval and sandbox policy, not an allow/deny tool list; adapter seam reserved |
| `codex`, interactive | unsupported | operator-owned |
| `dsh`, `jev`, `user-proxy` | unsupported | no such policy or no tool surface |

A capability row is a claim about tonight. Unlocking one later changes the row,
the adapter, and the golden tests in the same PR.

### 5.2 Adapter protocol

`adapters.py` defines:

```python
class HarnessAuthorityAdapter(Protocol):
    harness: str
    def policy(self, authority_set) -> HarnessPolicy: ...
    def launch_options(self, policy) -> Mapping[str, object]: ...
    def verify_ready(self, policy, ready, *, version) -> None: ...
```

`ClaudeAuthorityAdapter` maps `HarnessPolicy.allow/deny` to the Agent SDK's
`allowedTools` / `disallowedTools` and verifies the ready handshake. The registry
holds only `claude` tonight. Adding Codex or Pi means adding one adapter and
flipping one capability row; the loop itself never branches on a harness name.

`ports.py` defines the other half of the seam:

```python
class HarnessLifecyclePort(Protocol):
    def stop(self, target) -> None: ...
    def start(self, target, policy) -> None: ...
    def wait_ready(self, target, *, timeout_seconds) -> Mapping[str, object]: ...
```

The daemon supplies a port backed by its existing stop/start machinery. The
tests supply a fake lifecycle; no test needs a real worker process.

## 6. Cell model interface

`AuthorityTarget.kind == "cell"` is modelled now so the task carrier does not
need a schema change when the cell model ships. This daemon's loop refuses cell
targets with a bounded detail. When the cell model is online, the **cell head
harness** runs the same loop (same task model, same store, same adapter
protocol) with a lifecycle port whose targets are the harnesses inside the
cell. That is the owner's item 3: this work is provided to the cell head
harness for scheduling and changing member harness permissions.

No cell implementation is in this phase.

## 7. IM boundary

Lark is not part of this work. An agent turns an owner IM message into an
authority-set task (`source` names the owner and the message), and the apply
loop consumes the task. The authority set is **not** wired to the IM adapter:
no module in `authority_set/` imports Lark, a squire, or a delivery adapter, and
none should.

## 8. What is reused from #2607, and what is not

Carried over (with attribution in the module docstrings):

- the owner-only append-only audit log (`JsonlAuditLog`): `O_APPEND`,
  `O_NOFOLLOW`, `0600`, `fsync`, fail-abort;
- strict wire parsing and bounded values (`_require_text`, rule bounds);
- the ready-capability handshake (`require_policy_ack`).

Not carried over:

- the in-memory short-code approval store and its native reply route;
- the per-tool-call `can_use_tool` mainline. A `can_use_tool`-style live prompt
  may exist later as an optional layer inside a harness, but it is not how an
  authority set is applied;
- the #2607 PR body/branch: #2607 was closed on 2026-11-09 and its branch is
  retained for history.

## 9. Acceptance matrix

| # | Assertion | Where |
|---|---|---|
| A1 | Rules are normalized to tuples; duplicates, overlap, control characters and bad types are rejected | `tests/daemon/authority_set/test_model.py` |
| A2 | The digest is order-independent and changes with harness/mode/rules | same |
| A3 | The handshake accepts exactly the policy and version; everything else is a handshake error | same |
| A4 | The store round-trips, is 0600, rejects a symlink, and fails closed on truncated/empty/duplicate-key/invalid records | `test_store.py` |
| A5 | The audit log is canonical JSONL, 0600, fsynced, and raises instead of skipping | `test_audit.py` |
| A6 | A successful apply stops, starts, waits, verifies, and records `applying` then `applied` | `test_supervisor.py` |
| A7 | A wrong acknowledgement rolls back to the last applied policy | same |
| A8 | No previous policy means `failed` and the target left stopped | same |
| A9 | No adapter / cell target / stale version are rejected without a restart | same |
| A10 | One target's failure does not stop the others; a corrupt store propagates | same |
| A11 | An audit failure aborts before any restart | same |
| A12 | Only headless Claude declares `permission_set` native; all other rows are unsupported with a note; the registry holds only Claude | `test_capabilities.py` |
| A13 | The existing capability golden tests still pass, and the architecture gates report zero violations | `tests/daemon/harnesses/contracts/test_harness_capabilities.py`; `scripts/ci/check_module_boundaries.py`; `scripts/ci/check_structural_limits.py` |

## 10. Non-goals in this phase

- Cell implementation, Lark/IM wiring, merge, or rollout. The daemon
  lifecycle, request surface (`authority.set` / `authority.get` /
  `authority.list`), and `hyprial agent authority` CLI are provided by the
  lifecycle and request slices; none of them performs a rollout.
- A launcher that accepts `HarnessPolicy` and injects `allowedTools` /
  `disallowedTools`; today's daemon still bakes the lists into `WorkerChannel`.
- A worker-side `authority` ready block. The handshake is implemented on the
  consumer side; the producer side is the same slice as the launcher.

## 11. As implemented (2026-11-09)

This section records what the launch, lifecycle, and request slices shipped.
Sections 1-10 remain the approved design and are not restated as new decisions.

### Request surface

- `authority.set` takes `target {kind,name,harness,headless}`, `allow[]`,
  `deny[]`, `version`, `source`, `taskId`, and `createdAtMs`. It validates
  through `AuthoritySet`, appends a `pending` record only, and returns the task
  snapshot. It never restarts a worker and never waits for readiness.
- A version not newer than the newest recorded task for the target is refused
  with `INVALID_ARGUMENT`; the store is not appended.
- `authority.get` returns the current applied set, the latest task status, and
  its detail.
- `authority.list` returns target-filtered or all history, newest first, with
  `limit` bounded to 1..200 and a `truncated` flag.
- A missing store reads as empty and is not created. A malformed store is
  `DAEMON_ERROR`; no record is skipped or guessed.
- CLI: `hyprial agent authority set|get|list`. `set` accepts repeatable
  `--allow`/`--deny`, `--version`, `--source`, `--task-id`, and
  `--headless/--no-headless`; a generated `cli-` task id is used when omitted.
  Naming a target requires `--harness`.

### Switch and paths

- `HYPRIAL_AUTHORITY_SUPERVISOR` enables the background service. Truthy values
  are `1`, `true`, `yes`, and `on`; the default is off.
- Off means no supervisor thread, no service-created store, no audit file, and
  no worker restart. An explicit `authority.set` still creates the task
  directory and store file so a task can be queued while the service is off.
- `HYPRIAL_AUTHORITY_STORE` defaults to
  `<daemon state>/authority/tasks.jsonl`.
- `HYPRIAL_AUTHORITY_AUDIT` defaults to
  `<daemon state>/authority/audit.jsonl`.
- When enabled, a target must resolve to a registered agent and an exact
  `(harness, headless)` durable launch intent. The service never creates an
  agent.

### Apply and rollback semantics

- A successful apply transitions `pending -> applying -> applied` only after the
  restarted worker acknowledges the exact policy digest and version.
- A wrong acknowledgment transitions to `rolling_back` and then `rolled_back`
  after the previous applied set is restarted and acknowledged.
- With no previous applied set, the target is stopped and the task becomes
  `failed` with a detail stating that the target was left stopped.
- Stale versions, a missing adapter, and `cell` targets become `failed` without
  stopping or starting a worker, and record the `apply.rejected` audit action.
- Audit records are fsynced before each transition. A corrupt store raises
  instead of guessing which tasks exist.

### Differences from the design draft

- Version monotonicity is enforced against the newest recorded task, including
  a pending one, not only the applied set. This prevents a queued version from
  being reused and then rejected as stale inside the loop.
- `authority.set` validates the task model but does not resolve or create an
  agent. The supervisor resolves the target and fails closed when it does not
  exist or has no exact launch intent.
- The p1 model target key remains `kind:name`. Harness and headless are
  validated and passed through, but they do not form separate version
  histories.
- Only Claude has a launch-time authority adapter. Pi and Codex are rejected
  by the loop rather than receiving a guessed policy.

### Not done

- No cell implementation, no Lark/IM/squire wiring, and no default enablement
  or rollout.
- Containerized authority launches still fail closed at worker startup because
  the isolated worker has no adjacent `authority_set/model.py`; legacy launches
  are unaffected and this path is untested.
- Remote CI is not part of the recorded evidence.

## Hardening (2026-11-09)

This section records the post-acceptance hardening pass. It does not change the
approved design in sections 1-10, and it supersedes only the section 11 "Not
done" paragraph that said the launcher still baked the tool lists into
`WorkerChannel`.

- **Capability gate.** The apply loop asks the shared capability table before it
  asks for an adapter. Only `SupportLevel.NATIVE` proceeds; every other
  `(harness, headless)` row is rejected with no stop, start, or ready wait.
  `ClaudeAuthorityAdapter.policy` also refuses a non-headless target.
- **One launch mapping.** The production launch path asks
  `ClaudeAuthorityAdapter.launch_options` for the option bundle exactly once;
  the registry carries that bundle to the worker channel and the Agent SDK
  copies it verbatim. The launcher does not rebuild `allowedTools` /
  `disallowedTools` from the task, and the apply loop never branches on a
  harness name.
- **`rollback_authorized=same-as-write`.** A rollback writes only back to the
  same target and remains inside the same task and authorization unit. This PR
  does not perform a rollout; rollback happens only under the fake lifecycle in
  tests. A future enablement loop must require the owner's enablement
  authorization to cover rollback as well.
- **Effective-policy verification.** An apply is `applied` only after the
  worker's ready frame carries the `policyDigest` computed over the allow/deny
  lists actually received, plus the task `version`, and `adapter.verify_ready`
  accepts both. Acceptance also reads `authority.get` and requires its current
  digest and version to equal the task. "The service started" is not evidence
  of an applied policy.
- **`enforced_by=convention`.** "Push only `feat/authority-set`" exists only in
  dispatch text; the server does not constrain which branch a PAC selects.
  Merge separately has a server-side whitelist.
- **Restart basis.** A restart cycle captures its launch basis, the resolved
  target and durable launch intent, before the stop, and reuses that basis for
  stop, start, and ready wait. Start never re-reads the desired row that the
  stop removed.
- **A11 audit failure.** If the rollback-intent audit write fails, nothing is
  restarted: at most one stop is attempted for fail-closed containment, and the
  final status is `failed`, never `rolled_back`.
- **Protocol type.** The ready frame's `protocol` must be an `int` equal to 1;
  `true`, `1.0`, and `"1"` are rejected.
- **Store symlinks.** A dangling store symlink fails closed on read instead of
  reading as an empty history; a symlink to a valid store is also refused by
  `AuthorityStore`'s `O_NOFOLLOW`. A genuinely missing path still reads as an
  empty history, and `prepare_authority_store` leaves the write path to the
  same `O_NOFOLLOW` refusal.
