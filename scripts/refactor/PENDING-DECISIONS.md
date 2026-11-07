# Pending decisions for the deep-module refactor (S1-S6)

Status: **all items below are unresolved**. The toolchain in this directory is
implementation-complete; the nine review findings (gates, authority schemas,
idempotency state, scope policy, verify semantics, plan/apply equivalence,
failpoints, edge semantics) are fixed with first-fail/then-pass regressions.
No real migration rule is approved, so nothing here is executable against the
product. The main model owns every decision listed here.

Evidence baseline: `ad4ce68b6ccd08bd64dd9fdce448dd91a8329f43`
(`docs/evidence/refactor-c0-ad4ce68b-2026-10-01/`, authoritative; the
`613893e4` and `b1023d69` directories are WORM history). Product invariants
hold at this baseline: 354 files / 212,872 lines, 74 reverse sites
edge-identical, 129 exemptions byte-identical.

Additional pending items since the b1023d69 delivery:

- GUI bridge/build modules (gui_apps, gui_runtime, gui_product, gui_bundle,
  gui_build_hook): per-module draft proposals and IR-1..IR-10 responses live
  in `integration-contract.python.json`. Awaiting: shell-face exports, the
  process-identity extraction decision, gui_product/bundle stay-or-move.
- `src/gui/integration/pac-service.py` (IR-6) imports
  `hyprial.pac.context.node_context`; compatibility re-export vs coordinated
  edit is an open choice for the main model with the GUI owner.
- Authority verification now requires a `pin_sha256` per preserved_from
  baseline and validates complete-record multisets (identity fields per
  manifest shape: actor path/symbol/reason, supervision +kind/+owner/+card,
  inbox path/owner/operation[+reason]).
- apply tokens additionally bind the tool code hash (`tool_code_sha256`);
  stage status records `applied` separately from verify-recorded
  `verified_at`.

## 1. Reverse-dependency disposition, site by site (C0 §7 item 1)

The 74 forbidden import sites (6 domain pairs, 45 source-module edges,
`scan-final/boundaries.json` of the first round; identical in the rerun) each
need a per-symbol decision: shared contract extraction vs. upper-layer
injection vs. moving presentation logic. The original plan's "twelve leaks"
estimate is obsolete. Per-domain details that must be settled (C0 §4 table):

- lifecycle receipts / launch spec: data validation, serialization, exception
  types and required dependencies for the shared contract.
- counters / alarm / availability: is `counters` infrastructure; are alarm
  data and delivery core; is usage/quota policy biz?
- routine/usage assembly and IPC: which layer owns start, resume, shutdown,
  late callbacks and handler ownership.
- workflow/cli, PAC/CLI errors, `orgfs/_state_dir`: presentation goes to
  shell; base paths and protocol exceptions go to their semantic owner; CLI
  arguments/output must not change; lower layers must not import CliError.
- identity/harness/profile/principal: pure contract sink or port injection;
  the whole PAC/harness/profile implementations do not move into kernel.
- home initialization / Windows owned process: split base path/process facts
  from assembly/migration; timing of home migration and dispatch-policy init.

Demonstration finding: `hyprial.daemon.desired_state.HarnessLaunchSpec` alone
has **98 tracked import sites** at baseline (product, `contract/`,
`desktop/`, `docs/notes/`) — see `preview/manifest.demo.json`. Scope decisions
required: (a) which trees are in scope for a batch import rewrite, (b) whether
WORM-adjacent `.py` files under `docs/notes/` are ever rewritten (default:
no), (c) the real target module.

## 2. Public export allowlists and lazy behaviour (C0 §7 item 2)

Per domain: the approved `__all__`, the explicit lazy forwards/factories, and
cross-module public-face rules. `facades.py` can only generate from an
approved list; `from .interface import *` is never generated. Conflicts
exports and non-tool-managed files are refused today; the allowlists
themselves are the main model's call. Existing `ports` usage (83 product
sites across 10 modules at baseline) must be folded into these allowlists.

## 3. Application collaborators (C0 §7 item 3)

For each of the 284 `DaemonApplication` direct methods (per-method
self-read/write/call data in `scan-final/extraction.json`): state owner,
start/close owner, callback and compensation rules. `class_member_extract`
requires per-`self.X` classification (`field` / `host` / `class_attr`) and a
trusted `wiring` statement; temporary host access must be listed exactly and
carries a narrowing condition — it must not be recorded as deep-module
completion (C0 §4).

## 4. Runtime entries, resources, config and CI paths (C0 §7 item 4)

Every active runtime entry needs an explicit migration rule: `-m` entries,
`importlib.import_module`/`__import__` sites, `importlib.resources.files`
owners (9 candidates), generated configs (JSON/TOML fields), cross-language
path consumers and `__file__` parent-depth calculations (440 observations;
the C0 counts are evidence, not a rewrite list). CI special paths
(`scripts/ci-scope.mjs`, `scripts/pytest-impact-map.json` equivalents) and
test root-directory computations must move in the same stage as the files
they reference. Historical/WORM content stays byte-identical; only active
indexes and links are rewritten.

## 5. Authority exemptions (3 + 66 + 60)

The 129 existing entries keep their identity and reason through every move
(`layout_move` `manifest_updates` migrates only matched `path` values;
`verify`'s `authority_manifests` check enforces identity preservation).
Deletion of any entry requires behavioral-disappearance evidence plus a
mutation test on the gate — a main-model decision, never a tool action.

## 6. Demonstration artifacts (not approvals)

`preview/manifest.demo.json` + `preview/plan.json` + `preview/diff.patch`
contain two **draft** rules against the real tree at baseline `b1023d69`
(98-site import rewrite, 1 module-string rewrite). They exist to prove the
engine runs against real product files and that its match counts agree with
the C0 rerun evidence. The target modules (`hyprial.kernel`) are placeholders
from the C0 design example; they are not domain decisions and the plan is
marked not executable.

## 7. Known-stale references (pre-existing, out of scope)

Roughly 15 references point at notes files that never existed (e.g.
`notes/spec-codex-worker-git-commit-2026-09-14.md`,
`notes/design-hyprial-transfer.md`,
`notes/spec-h2b-login-u2-identity-2026-09-04.md`; scattered across `tests/`,
`src/hyprial/`, `docs/`). These predate the notes → `docs/notes/` migration.
Record them as pre-existing staleness during any future reference scan; do
not treat them as migration breakage and do not "fix" them silently.
