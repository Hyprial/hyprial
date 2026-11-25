# Native actor engine ABI spec v1 — clarifications (2026-11-03)

Status: binding rulings by the HQ R&D lead (spec owner) on questions raised in PR #2251.
They resolve ambiguities in `actor-engine-native-abi-spec-2026-11-01.md` without changing its semantics.
Cite as `CL-n` next to the clause and case IDs.

## CL-1 — the failure that exceeds the restart budget still emits `child_failed`

Question (PR #2251): when a supervised failure (5.1 row 3 or row 4b) pushes the actor over its restart budget (row 5), is `child_failed` emitted, or only `child_quarantined`?

Ruling: **both, in this order** — `child_failed` (same `code`/`detail`, and the same `command_type` as row 3 / `None` as row 4b), then `child_quarantined`. **No** `restart_scheduled`.

Why: Section 9 defines `child_failed` as the event for every supervised failure (any non-expected exception, `BaseException`, panic). Row 5 describes the *consequence* of that failure, not a different kind of failure; dropping `child_failed` there would make the budget-crossing failure the only supervised failure that observers cannot see, which is the class of silent loss Section 16 (SUP-09) exists to forbid. The ordering rule in Section 9 (`child_failed` before `restart_scheduled`) generalises to `child_failed` before `child_quarantined`.

Cases: SUP-03 and SUP-06 assert this sequence; mark the assertions `CL-1`.

## CL-2 — native handler injection for the conformance suite

Question (PR #2251): how does the conformance suite obtain native handlers for cases that require them?

Ruling: the suite uses the engine's Rust-side `register_native_factory` as a **test-only injection channel**. It is not part of the public ABI surface: application code MUST NOT depend on it, and its presence or absence does not affect ABI conformance. The suite README documents the channel; a backend that lacks it fails the native-handler cases closed (error, never skip).

If a later ABI version needs a public native-handler registration API, it is specified then; until then this clarification governs.

---

## CL-3 … CL-24 — the conformance suite's readings, ratified (HQ ruling R1, PR #2251 comment 32150)

HQ ratified the suite's current readings of the open questions A1–A20 (evidence note `docs/notes/actor-rustify-conformance-acceptance-2026-11-03.md` §7) without changing the spec's semantics. One entry per question. "Pinned by" names the case IDs (and test functions) that assert the reading. "Not pinned" means the suite deliberately leaves the behaviour implementation-defined, and no backend may be marked failing for it. Numbering starts at CL-3 (待 HQ 追认: "接着 CL-3 往下排" is read as "the first new entry is CL-3").

| CL | Question (A#) | Ratified reading | Pinned by |
|---|---|---|---|
| CL-3 | A1: a timer that fires into a draining actor and meets closed external admission | Implementation-defined. Drain start cancels pending timers (§5.5), so only a timer scheduled after drain start can reach this path. The reference drops it. | Not pinned. Related: TMR-05 (`test_tmr_05_*drain*`), DRN-05 |
| CL-4 | A2: §8 fire table, a stopping/stopped/quarantined target whose generation also differs | Implementation-defined between the two rows. When generations match, a timer racing stop confirmation is dead-lettered `stopped` exactly once. | TMR-05 (race row), TMR-04; mismatch combination not pinned |
| CL-5 | A3: a stop that timed out, with no further `stop` call waiting | Confirmation may proceed in the background or on the next `stop` call. In either case a later `stop` returns True, the dead letters arrive exactly once, and there is exactly one `child_stopped`. | CUS-05 (`test_cus_05_stop_timeout_sends_no_dead_letters_then_once`), CUS-07, EFF-07 (`test_eff_07_stopping_owner_keeps_retrying_until_stopped`) |
| CL-6 | A4: order of `stop` returning True vs `child_stopped` / `state == stopped`; row 4a state vs `child_restarted` | Within one confirmation the order is fixed: dead letters (admission order), then `child_stopped`. `stop` returning True only guarantees that both become observable promptly afterwards; `state` may lag the event. The same holds for row 4a. | CUS-03 (timeline order), STP-01, SUP-01 (bounded waits, no instant reads) |
| CL-7 | A5: pool snapshot `queued` / `in_flight` | `queued` = submitted payloads not yet picked up by a worker; `in_flight` = payloads executing now; a reserved-but-unsubmitted token is in neither; `failures_in_window` = 0; generation = 1. | SNP-01 (`test_snp_01_pool_fields`) |
| CL-8 | A6: does a pool emit `child_stopped`? | Yes: exactly one, generation 1, at stop confirmation (§5.4 skeleton applies to pools). | EFF-09 (`test_eff_09_pool_stop_executes_queue_and_waits_for_acks`) |
| CL-9 | A7: `failures_in_window` | The length of the restart-budget deque: appended and pruned only at a failure (6.3.1), frozen at quarantine (5.1 row 5), cleared by `reset` (6.4). It is not re-pruned when read. | SUP-03 (frozen count), SUP-04 (`test_sup_04_failures_inside_window_accumulate`, `…window_prunes…`), SUP-07 |
| CL-10 | A8: `child_quarantined` code/detail; an expected error without `code`/`detail` attributes | `child_quarantined` content is implementation-defined beyond kind and generation. A missing attribute on an expected error yields `None` in the event field. | SUP-03 (kind, generation); attribute default not pinned |
| CL-11 | A9: a sink raising a `BaseException` that is not an `Exception` | Sinks raising `Exception` subclasses are isolated (required). For non-`Exception` `BaseException`, behaviour is implementation-defined. Conservative choice for the product engine: swallow it as well (待 HQ 追认). | EVT-03 (`Exception` only) |
| CL-12 | A10: a timed-out engine-wide drain and spawn; idle targets in `remaining` | After a timed-out engine-wide drain, whether spawn is still refused is implementation-defined (the reference keeps refusing). `remaining` must list every target not yet stopped; whether idle-but-unstopped targets appear is implementation-defined. | DRN-02 (all targets busy), DRN-07; the rest not pinned |
| CL-13 | A11: a handler or restart factory that fails after stop acceptance | An expected error during stop is a normal `command_rejected` and stop then completes. A non-expected failure after acceptance is implementation-defined; it must not restart the actor or leave it running. | STP-02 (`test_stp_02_stop_while_current_handler_rejects_expected_error`) |
| CL-14 | A12: "endpoint gone" (§5.2) | Includes "after engine `shutdown`": `tell` → 2, `ask` → `ActorAdmissionError("closed")`. | STP-05 (`test_stp_05_tell_and_ask_closed_when_endpoint_gone`) |
| CL-15 | A13: negative `generation` arguments | `generation` is a non-negative int. Negative values are outside the ABI: the backend may refuse them (e.g. `OverflowError`/`ValueError`) or treat them as distinct keys, but must not crash. | Not pinned |
| CL-16 | A14: policy-table numeric bounds | An empty table, or a value that is not a 5-tuple, raises `ValueError` at construction. Further bounds (negative values, `max < base`, `jitter > 1`) are implementation-defined; the reference refuses them. | ADM-05 (`test_adm_05_invalid_policy_table_at_construction`) |
| CL-17 | A15: module path | `hyprial._core.actors`, inside the namespace package `hyprial._core`. The ADR's `hyprial/_core.abi3.so` is superseded (R5). | LIFE-04, import-closure meta case |
| CL-18 | A16: how `ActorAdmissionError` carries its reason | `args[0]` is exactly `"overloaded"` or `"closed"`. | ASK-04, STP-05, DRN-06 |
| CL-19 | A17: scope of `jitter_seed` determinism | Two engines constructed with the same `jitter_seed` and the same failure history produce identical `restart_delay` sequences. | SUP-05 (`test_sup_05_jitter_bounds_and_seed_determinism`) |
| CL-20 | A18: SUP-08 (stale failure reports) through the ABI | A stale report cannot be produced through the ABI. The case pins the observable consequence: each failure is attributed to its own generation, no report is applied twice, and generations rise by exactly one per re-entry. | SUP-08 |
| CL-21 | A19: a restart-time factory raising a non-`Exception` `BaseException` | A row-4b child failure on every backend: `child_failed` with `command_type` None and code = type name, budget +1, stays `restart_pending`. §16 describes the pre-P2 pykka engine; after P2 (ruling R2/R3) the product pykka engine conforms, and the suite's §16 xfail is removed. | SUP-09 + SUP-06 (`test_sup_09_restart_factory_base_exception_is_row_4b`) |
| CL-22 | A20: a `BaseException` in one actor and other actors | It must not affect any other actor: no stop, no admission change, no lost messages elsewhere. The pre-P2 pykka engine violated this (`ActorRegistry.stop_all()`). P2 adds a bystander assertion. | SUP-09 (bystander assertion added in P2), SUP-02 |
| CL-23 | TOL-1 measurement under load (HQ round-1 review, macOS load ≈ 12) | ±50 ms is the engine's tolerance, not the host's. The lower bound is strict on every attempt. The upper bound allows the scheduling overshoot that an equivalent plain-Python wait shows in the same moment (control), and is met if one of N independent trials (default 5) passes. TOL never grows, and exact values (e.g. `restart_delay` at jitter 0) stay exact. Hang bounds (WAIT) are not semantic bounds. | TOL-1, TOL-2, SUP-05, TMR-01 and every case using `support.helpers.assert_timing` |
| CL-24 | FREE-03 on a host without a free-threaded interpreter | Reported as explicitly unavailable: the case is skipped with the prefix `BLOCKED:`, and the session verdict is BLOCKED (exit status 7). It never passes. Any other skip fails the session. | FREE-03, `cases/meta/test_verdict.py` |
