# A05 import-ratchet rework — minimal concrete patch plan

Status: implementation plan for the formal backedge. No product, ledger, seed
or gate change is included in this document. The frozen candidate stays
`b298cbe7b`; no new wheel or broad focused/install rerun is planned until the
new request arrives.

## Baseline accounting

Observed at b298: 590 CLI→backend edges; A03 accepted ledger 548. The 42
unique growth triples are exactly those in
`docs/evidence/cli-command-surface/a05-merged-import-ratchet.md`.
The plan removes 34 rows and keeps 8 exact, individually justified rows:

`590 = 548 + 8 planned exceptions` (the 34 removed rows are not in the 548
ledger, so the ledger itself remains byte-identical).

## P0. Real semantic dedup (no behavior change)

| File | Current duplicate | Patch | Result |
| --- | --- | --- | --- |
| `adapter/admin.py` | `DaemonOwnershipBusy` and `DesiredStateError` imported in both `adapter_downgrade_state` and `adapter_remove` | one module-local `_adapter_state_errors()` helper returns the two exception classes; both functions use it | counts 2→1 each; no error mapping changes |
| `message/send.py` | `hyprial.biz.worker_binding` imported again although `common/support.py` already uses it | add `support._worker_binding()` wrapper; `message/send` calls it | second `worker_binding` edge removed; support's single behavior edge remains |

Tests: existing adapter surface tests, `tests/daemon/forwarding/membership/`,
`tests/shell/cli/workflows/`, and the request-contract gate. Estimation: 0.25
day.

## P1. 31 preload rows → one public shell bootstrap capability

Add `hyprial.shell.prime_post_install_imports() -> frozenset[str]`:

- implementation: `src/hyprial/shell/impl/upgrade_bootstrap.py`
- public facade: add the exact symbol to `src/hyprial/shell/interface.py`
  `_FACADE_EXPORTS`/`__all__`
- it imports the 25 daemon, 5 identity and 1 kernel symbol currently in
  `socialware/commands.py` and returns the loaded module set;
- `socialware/commands.py::_prime_post_install_restart_imports` becomes a
  thin call to that public capability (one shell import, not a backend edge).

This is a real shell bootstrap boundary, not a scanner blind spot: the
post-install fence executes the real profiles and still proves the modules are
loaded before the swap. The 31 CLI→backend rows disappear; no helper is copied
and no backend object is returned. Rule: design.md §5 per-item bootstrap
capability (B05) + §11 item 1 for the returned module set. Tests: existing
`post_install/test_post_install_fence.py` profiles, plus an equivalence test
that the shell capability returns the same primed set as the old helper.
Estimation: 1 day.

## P2. Doctor name-collision check

`doctor/checks/names.py` currently reads `channels.json` and
`agents.sqlite3` directly (`AgentRegistry.local_names`) even when the daemon is
online.

- Online path: use existing public IPC `agent.list` and `adapter.list` through
  `services.runtime.request`; build the collision set from those responses.
  No direct store read.
- Offline path: only when `DaemonUnavailableError` is observed, use a B06
  offline diagnostic capability. The exact two symbols remain
  `hyprial.identity.AgentRegistry` and `hyprial.kernel.ChannelConfiguration`,
  but only in the daemon-unavailable branch; this is a per-item B06 exception,
  not a general offline allowance.
- Tests: new `tests/shell/cli/lifecycle/doctor/test_name_collision.py` with a
  fake online request proving no `AgentRegistry` call, and a daemon-unavailable
  fake proving the B06 read still works. Estimation: 0.5 day.

## P3. Exact 8-row exception manifest (no ledger expansion)

The plan is explicitly **not** a 42-row allowance and does not edit
`scripts/ci/a03-contract-seed.json` or its reviewed digests. Add
`scripts/ci/import-ratchet-exceptions.json`, bound to the A03 seed and reviewed
as an exact list (path, module, symbol, count, category, condition). The gate
subtracts only exact matches before comparing to the 548 ledger; any extra row,
count change, wildcard or changed manifest digest fails.

| Path | Symbol | Category / condition |
| --- | --- | --- |
| `adapter/admin.py` | `hyprial.daemon.downgrade_adapter_state` | §5 offline maintenance: `adapter downgrade-state`; daemon stopped, ownership lock held |
| `agent/runtime/start.py` | `hyprial.daemon.with_reasoning_effort` | §5 offline launch bootstrap: native args built before daemon start |
| `common/support.py` | `hyprial.biz.worker_binding` | §5 offline identity bootstrap: reads session-ref file before any request |
| `common/support.py` | `hyprial.identity.PAC_PRINCIPAL_UNVERIFIED` | §11 item 1 pure identity error vocabulary |
| `common/support.py` | `hyprial.identity.PacError` | §11 item 1 pure identity error vocabulary |
| `doctor/checks/names.py` | `hyprial.identity.AgentRegistry` | B06 offline-only diagnostic after `DaemonUnavailableError` |
| `doctor/checks/names.py` | `hyprial.kernel.ChannelConfiguration` | B06 offline-only config data after `DaemonUnavailableError` |
| `org/maintenance.py` | `hyprial.daemon.TailnetMaintenance` | §5 offline maintenance: `org repin`; daemon stopped, fence/preview/commit |

No `DaemonLaunchResult` exception is needed: P1 moves it into the shell
bootstrap. No `worker_binding` duplicate remains after P0. No adapter error
counts remain above the accepted 548.

## P4. Gate and negative controls (seed preserved)

- Keep `scripts/ci/a03-contract-seed.json` and its hardcoded reviewed digests
  byte-identical; keep the 548 `import-ratchet-baseline.json` unchanged.
- The exceptions manifest is separate, exact and digest-reviewed; the gate
  verifies the seed first, then subtracts only listed rows, then applies the
  ordinary base-ref growth check. A missing/extra/changed exception fails.
- Required controls in `tests/tooling/architecture/ipc/test_import_ratchet.py`
  and `test_request_contract_gate.py`:
  1. seed byte/digest tamper → fail;
  2. new or grown CLI→backend edge → fail even with a valid manifest;
  3. re-added preload edge (e.g. `socialware` `ServiceManager`) → fail;
  4. exception row removed/changed/duplicated → fail;
  5. ledger expansion or wildcard entry → fail;
  6. clean b298-equivalent tree + exact manifest → pass;
  7. base-ref missing → fail (existing behavior).
- Existing contract/OpenAPI/method gates unchanged. Estimation: 0.5 day.

## P5. Order and estimate

1. P0 dedup (0.25d) — can start immediately; no behavior change.
2. P1 shell bootstrap (1d) — removes 31 rows; run post-install fence.
3. P2 doctor online/offline split (0.5d).
4. P3 exact 8-row manifest + P4 gate controls (1d).
5. Final differential: touched-file focus, full Ruff/structure/boundary,
   contract isolated; reuse 702/16ad installed evidence because the wheel
   product/validator bytes do not change, and build a new artifact only if the
   final product diff is non-empty.

Total estimate: **2.75–3 days** of focused work. This is not a C migration, a
generic allowlist, or an empty helper proxy.
