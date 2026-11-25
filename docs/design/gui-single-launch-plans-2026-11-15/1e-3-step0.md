# 1e-3 Step 0 inventory (branch `refactor/gui-browser-scripts-contract-mock`, head `ee779727c` = 1e-2, before any 1e-3 change)

Machine: shared 4-core, Chromium from playwright-core, Node 24.22.3, one browser script at a time.
"Wired" = npm script / `npm run verify` chain / workflow. First failing step is the first assertion or wait that fails.

Re-inventory note: of the 14 dead evidence scripts of plan 1e section 8 only `dev01-v17-visual-compare.mjs` still existed on this head; the other 13
(account-menu-copy-product, bootstrap-unavailable-production, button-product-states, button-variants-computed, buttons-v13-matrix, connections-modules-nendo,
first-run-v13-visual, lark-binding-danger-shots, mobile-bottom-nav, mobile-bottom-nav-375, mobile-chat-gap, v13-visual-snapshot, dev-03/chat-browser,
modules/dev04/org-acceptance-browser) are already deleted; docs that still name two of them (`gui/docs/modules/dev-04.md`, `gui/docs/business-lines/org-members-batch1-plan.zh-CN.md`)
say so as history and need no edit. The Tasks page and `agent-provider-browser.test.mjs` are gone too.

## (a)/(b)/(c) scripts

| Script | Matches | Wired where | Result on head (first failing step) | Decision |
|---|---|---|---|---|
| `tests/agents-second-browser.test.mjs` | a, b (`entry.html?driver=contract-mock`, comment `/api/reference/mock-success`) | `npm run verify:agents-second-browser` (excluded from `npm test`) | PASS 1/1 (14 s) | **Migrate** to helper + product entry |
| `tests/modules/dev04/org-request-browser.mjs` | a, b, c | nothing (run by hand with `node --test`) | FAIL 0/6 (each ~31-35 s): `page.waitForResponse` for `org.list` times out; the reference org entry stays on "账号状态未确认 / 去登录" and never issues org.list | **Migrate** the 3 tests a declared case reaches; delete the 5 that no case reaches (fail on base, wired nowhere); record the gaps in the header |
| `tests/modules/dev04/org-lifecycle-browser.mjs` | a, b, c | `npm run verify:org-lifecycle-browser` (in `verify`) | FAIL (40 s): `getByText('还没有可用的组织。')` timeout, `openEmptyPage` line 61 (same cause: org-entry never issues org.list) | **Leave** (gap, see below) |
| `tests/modules/dev04/org-rework-browser.mjs` | a, b, c | `npm run verify:org-rework-browser` (in `verify`) | FAIL (37 s): `[data-org-mode="ready"]` timeout, `open` line 93 via `checkNetworkError(1440)` | **Leave** (gap, see below) |
| `tests/first-run-browser-contract.mjs` | a (gui-host import only; mock unused: DEV_PREVIEW=1 or production) | `verify:first-run-browser` (in `verify`) | FAIL (8 s): `moduleIndex.count()` 0 !== 1, line 431 (开发模块索引 missing) | **Migrate import** to `scripts/serve.mjs` |
| `tests/safe-r1-browser-contract.mjs` | a (same) | `verify:safe-r1-browser` (in `verify`) | FAIL (125 s): waiting for `select[data-mock-scenario="j1"]`, `support/first-run-browser-journey.mjs:23` | **Migrate import** |
| `tests/modules/dev02/j2-browser-contract.mjs` | a (same) | `verify:j2-browser` (in `verify`) | FAIL (38 s): `[data-mock-scenario="j1"]` selectOption timeout, line 46 | **Migrate import** |
| `tests/modules/dev01/j10-browser-contract.mjs` | a (same) | `verify:j10-browser` (in `verify`) | FAIL (128 s): `select[data-mock-scenario="j1"]`, `first-run-browser-journey.mjs:23` | **Migrate import** |
| `tests/dev01-onboarding-browser.mjs` (+ `.test.mjs`) | a (mock started, never used) | `npm test` (vitest) and CI | PASS 1/1 (17 s) | **Migrate import** |
| `tests/dev01-v17-visual-compare.mjs` | a | nothing | FAIL (1 s): `missing approved prototype` (absolute path under /home/chloemartin), line 149 | **Delete** (owner decision) |
| `tests/reference/proxy.test.ts` | b (`/api/reference/` literals: unit test of the proxy plugin) | `npm test` | PASS 1 file (3 s) | **Leave** (1e-4 deletes it with the proxy) |
| `tests/production-copy-gate.test.mjs` | b (negative sample `/api/reference/mock-success`) | `npm test` | PASS | **Leave** (allowlisted negative sample) |
| `tests/reference/{org-entry,org-agent-entry}.tsx`, `reference-http-plugin.ts` | b | entries opened only by the org scripts; plugin by `gui-host.mjs` | n/a | **Leave** (still used by org-lifecycle and org-rework; 1e-4) |
| `tests/reference/entry.{html,tsx}` | b | only agents-second | n/a | **Delete** after agents-second migrates |
| `tests/reference/{button-component-states,button-variants,mobile-chat,mobile-long-data}-entry.*`, `pr-a-fixtures.*` | opened `tests/reference/*.html` | opened by nothing (their scripts were deleted earlier); `pr-a-fixtures.tsx` imports pages that no longer exist | n/a | **Delete** (dead) |
| `tests/reference/first-run-product-entry.*` | opens `tests/reference/*.html` | used by j10 | n/a | **Leave** (j10 wired, no mock/proxy involved) |

## Wired browser scripts that do not touch the reference host (context for the before/after report, unchanged by 1e-3)

| Script | Wired | Result on head |
|---|---|---|
| `verify:j3-browser` | verify | FAIL (42 s) `.chat-workspace` timeout, `support/shared-views-browser.mjs:40` |
| `verify:j5-browser` | verify | FAIL (37 s) `.org-workspace` timeout, `shared-views-browser.mjs:60` |
| `verify:j6-browser` | verify | FAIL (36 s) same as j5 |
| `verify:j7-browser` | verify | FAIL (38 s) same as j5 |
| `verify:j4-browser` | verify | PASS (25 s) |
| `verify:linked-browser` | verify | FAIL (37 s) `.chat-workspace` timeout |
| `test:browser` | verify, CI | PASS (79 s) |
| `test:daemon-request-browser` | none in CI | FAIL 6 pass / 10 fail of 16 (960 s; real CLI, unrelated to the mock) |
| `verify:dev-browser-parity` | verify, CI | not run in Step 0 (see the report) |

## (c) `route.fulfill` users

Only the three org scripts above (`org-lifecycle` 1, `org-request` 5, `org-rework` 3 rows in the ledger).

## Gaps: states the product entry cannot reach under any declared mock case

Why the two verify-chain org scripts stay unchanged (their first failing step is the same as on the base, PRE-EXISTING: the reference org entry never issues org.list):

- `org-lifecycle` L1+L2: after a successful `org.join` the `org.list` refresh must turn the empty list into the new org (needs state or a sequence); L3: a join result whose `steps[0].kind` is invalid (needs a result no declared example has).
- `org-rework`: F2.1 org.network fails first, then recovers (sequence); F3 two orgs in `org.list` and a per-org `org.network` (no two-org example); F3.1 a delayed first read that lands after a refresh (ordering control); F1/F1b `org.repair` all-manual and a partial publish result (examples: `repairable-candidate`, `empty-candidate-list` only); F4 `link.add`/`link.drop` change what `link.list` returns (state); F5 `org.create` declared unavailable by the catalog (the mock host publishes every method).

Deleted org-request tests (5): partial folder restore (per-node orgfs.restore results), recovery after a first org.list failure (sequence), two damaged non-envelope org.fetch replies (HTTP 502/401 with a text body; the mock only sends envelopes), a not-dispatched org.fetch failure (the mock marks a declared domain error as dispatched; only invalid-params examples are not-dispatched).

Dropped from agents-second: "the shell hides the Agent entry when the catalog has no agent.list" (the mock host publishes every method); covered by `tests/features/agents/agents-refusals.test.tsx` and `tests/app/page-availability-gating.test.tsx`.
