# A05 import-ratchet 42-row classification (analysis only)

Status: historical analysis; the final implementation is at `069a10e41`
(ledger 539, exactly 8 reviewed exceptions, contract gate PASS). No blanket
42-row allowance is used. The analysis below records the classification that
led to that implementation. The accepted 548 ceiling and the A03 seed remain
untouched. "Present in dev" is provenance, not approval. This document does not
move helpers, copy logic or add re-export shims.

The 42 unique `(path, module, symbol)` triples in the frozen `069a10e41` tree
(product identical to `702420535`) are grouped below by the capability they
actually exercise. Counts are from
`docs/evidence/cli-command-surface/a05-merged-import-ratchet.md`.

## A. 31 upgrade preload imports (socialware/commands.py)

These are not command business paths. `_prime_post_install_restart_imports()`
imports them before `uv tool install` replaces the running distribution, so the
old process can finish the restart tail without discovering a new-package
module. The 31 rows are:

| Domain | Count | Symbols | Classification |
| --- | ---: | --- | --- |
| `hyprial.daemon` | 25 | env flags, desired-state/forwarding/service/restart types, startup-failure readers, upgrade-step runner, MCP entry, TCP probe | Upgrade bootstrap warmup |
| `hyprial.identity` | 5 | transaction lock/error/env, user-profile store/error | Upgrade bootstrap warmup |
| `hyprial.kernel` | 1 | `DaemonLaunchResult` | Pure protocol data type read by the restart reader; not initialization |

Convergence option (design.md §5 bootstrap/offline whitelist, per capability; protocol data under §11 item 1):

1. Add one public daemon bootstrap capability, e.g.
   `hyprial.daemon.prime_post_install_imports()`, owned by the daemon
   interface. It imports its own 25 modules and returns the loaded set; it may
   reuse the existing public `prime_upgrade_alert_imports()` precedent.
2. Add one public identity bootstrap capability, e.g.
   `hyprial.identity.prime_post_install_imports()`, for the 5 identity
   modules.
3. Keep `DaemonLaunchResult` as protocol data on the daemon public face
   (design.md §11 item 1; it is a value type, not a warmup), so the CLI imports
   it once through the public daemon surface rather than through the 25-symbol
   bulk.

That would replace 30 preload edges with two public capability edges and one
protocol edge, preserving the exact warmup behavior. Each capability needs its
own §5 per-item whitelist decision and a negative control proving only approved
modules are warmed; the 31 rows must not be approved as one bulk package. This is a design
proposal only; no implementation is included in A05.

## B. 11 non-preload rows

| Path | Symbol | Count change | Classification | Minimal compliant disposition |
| --- | --- | ---: | --- | --- |
| `adapter/admin.py` | `hyprial.daemon.downgrade_adapter_state` | 0→1 | Offline maintenance (local state mutation, no daemon IPC) | Per-item offline-maintenance capability, or move behind a public daemon maintenance operation; not a transport request |
| `adapter/admin.py` | `hyprial.daemon.DaemonOwnershipBusy` | 1→2 | Protocol/error vocabulary for the same offline operation; duplicate occurrence | Deduplicate to the accepted single import; no behavior change |
| `adapter/admin.py` | `hyprial.kernel.DesiredStateError` | 1→2 | Protocol/error vocabulary; duplicate occurrence | Deduplicate to the accepted single import; no behavior change |
| `agent/runtime/start.py` | `hyprial.daemon.with_reasoning_effort` | 0→1 | Behavior function (launch reasoning-effort handling), not pure vocabulary | Fold into the public launch request or approve a named §5 bootstrap/offline capability; do not call it protocol vocabulary |
| `common/support.py` | `hyprial.biz.worker_binding` | 0→1 | Identity binding helper that reads credential/binding files; behavior, not pure vocabulary | Consume a public identity binding operation or approve a named §5 offline/identity capability; not vocabulary |
| `common/support.py` | `hyprial.identity.PAC_PRINCIPAL_UNVERIFIED` | 0→1 | Pure identity error vocabulary | Protocol/error vocabulary under §11 item 1; per-item review |
| `common/support.py` | `hyprial.identity.PacError` | 0→1 | Pure identity error vocabulary | Protocol/error vocabulary under §11 item 1; per-item review |
| `doctor/checks/names.py` | `hyprial.identity.AgentRegistry` | 0→1 | Online direct read of `agents.sqlite3`; direct business storage access, not an offline diagnostic | Replace with a public identity query/request; do not grant an offline exception for an online direct store read |
| `doctor/checks/names.py` | `hyprial.kernel.ChannelConfiguration` | 0→1 | Pure configuration/protocol data | Protocol data under §11 item 1; per-item review |
| `message/send.py` | `hyprial.biz.worker_binding` | 0→1 | Duplicate of the `common/support.py` binding import | Consolidate the caller on the one public binding helper; duplicate edge disappears |
| `org/maintenance.py` | `hyprial.daemon.TailnetMaintenance` | 0→1 | Offline maintenance (local tailnet state) | Per-item offline-maintenance capability, or a public daemon maintenance operation; no daemon transport |

Summary of the 11: 3 duplicate occurrences (two adapter errors and the second
`worker_binding`), 3 pure protocol/data entries (two PAC errors and
`ChannelConfiguration`), 2 behavior functions (the support `worker_binding`
reads credential/binding files, and `with_reasoning_effort`), and 3 direct or
offline operations (`downgrade_adapter_state`, the online `AgentRegistry`
store read, and `TailnetMaintenance`).

## C. Recommended order

1. Deduplicate the three duplicate occurrences; this shrinks counts without any
   behavior or design change.
2. Review the 3 pure protocol/data entries under design.md §11 item 1, one
   symbol at a time; the two behavior functions need a §5 capability or a real
   public request/operation, not a vocabulary exception.
3. Implement the two public bootstrap capabilities for the 30 warmup imports,
   with per-capability tests and negative controls; keep `DaemonLaunchResult` as
   protocol.
4. Decide the 3 real direct/offline consumers individually: either a public
   capability or a named §5 exception. Do not request a blanket 42-row ledger
   amendment.

No step is authorized by this document. The current gate must continue to fail
until the reviewed changes (or individually approved exceptions) are actually
implemented and independently verified.
