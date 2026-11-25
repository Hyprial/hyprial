# Verification and handoff

This command inventory matches `gui/package.json` at `dev@3f2914996ca49a3764fb536a1e412d4a616822dd`. Re-read `gui/package.json` on every new task. If a command is absent, record it as unavailable; do not recreate it from memory.

## Select checks by affected surface

| Change | Minimum fresh evidence |
| --- | --- |
| Skill or GUI documentation only | frontmatter/structure, relative links, source correspondence, STC, secret scan, `git diff --check` |
| Tokens, theme, primitive, or shared style | focused test, theme contract, module boundaries, typecheck, advisory diff |
| Component behavior or accessibility | focused component test, typecheck, keyboard/focus browser check |
| Page state or adapter composition | focused state tests, error/unknown boundaries, shared-entry parity |
| Responsive layout | affected-width browser check, computed geometry, overflow, safe area, long content, browser error/request counts and classifications |
| Development composition | static parity, browser parity, production-copy verification |
| Broad production GUI change | focused checks first, then full `verify` after the visual checkpoint |
| Live backend claim | separately authorized real-backend evidence; GUI package gates cannot prove it |

Do not run a heavy full suite only to make a documentation change look stronger. Do not omit a relevant hard gate because a focused test passed.

## Current repository commands

Run commands from the repository root unless the command changes directory.

### Repository structure

```bash
uv run --no-project --python 3.12 python scripts/ci/check_structural_limits.py
uv run --no-project --python 3.12 python scripts/ci/check_module_boundaries.py
npm --prefix gui run check:module-boundaries
```

### Theme and type hard gates

```bash
npm --prefix gui run typecheck
cd gui && npx vitest run tests/theme-contract.test.ts tests/token-contract.test.ts
```

`tests/theme-contract.test.ts` is hard. It checks literal placement, light/dark variables, theme-variable access, `--ui-*` declaration ownership, dangling variables, shadcn mapping, and bundled font stacks.

### Shared production/development UI

```bash
npm --prefix gui run verify:dev-product-parity
npm --prefix gui run verify:dev-browser-parity
npm --prefix gui run verify:production-copy
```

- `verify:dev-product-parity` checks the product import graph, build-mode branches, dev markers, and registered composition-root exceptions.
- `verify:dev-browser-parity` runs the same input and interaction matrix against development and the production-like `dist-parity` artifact at 375, 760, and 1440 widths.
- `verify:production-copy` checks that release output contains no development tools, fixtures, mock composition, or scenario data.

Do not put test inputs into the production bundle to make parity easier to drive.

### Refactoring UI advisory

```bash
npm --prefix gui run check:refactoring-ui
npm --prefix gui run check:refactoring-ui -- --base "$BASE" --verbose
npm --prefix gui run check:refactoring-ui -- --base "$BASE" --json "$EVIDENCE/refactoring-ui.json"
```

The default command is advisory. `--strict` is not the default repository policy. Do not report an advisory finding as a hard-gate failure.

For each new-line finding, record:

| Field | Required value |
| --- | --- |
| file and line | Exact current path and line |
| rule | Checker rule ID |
| baseline | New, existing, or moved |
| authority | Token contract, Owner decision, page contract, or none |
| disposition | Fix now, keep as a narrow exception, or remove with the feature |
| evidence | Test, computed value, screenshot, or source citation |

A kept exception must be narrow. Example: Org's 18px gap is one `spacing-scale` exception backed by an explicit Owner decision. It does not permit other 18px gaps.

### Focused tests

Choose test files by imports, public behavior, and affected routes. Confirm each path exists before running it.

```bash
cd gui && npx vitest run tests/theme-contract.test.ts
cd gui && npx vitest run tests/dev-product-parity-static.test.mjs
cd gui && npx vitest run tests/tooling/refactoring-ui-check.test.mjs
```

Add the affected component and page test files to the same `npx vitest run` call. Do not reduce workers, retries, timeouts, assertions, or console checks to change the result.

### Full GUI verification

```bash
npm --prefix gui run verify
```

At the v0.1 pin, `verify` expands through module boundaries, typecheck, generated-client checks, static parity, build, Vitest, overlay browser tests, browser parity, production-copy, first-run, safe-r1, J2/J3/J4/J5/J6/J7/J10, Org lifecycle/rework, and linked-browser checks.

Preserve the complete output. If any test fails, the full suite is not GREEN. Record which failures are introduced, baseline, obsolete-contract, scope-out, or unexplained. Classification does not turn a failure into PASS.

### Final hygiene

```bash
git diff --check
git status --porcelain=v1
git rev-parse HEAD
git rev-parse HEAD^{tree}
git diff --name-status "$BASE"...HEAD
```

The repository has no dedicated Skill lint/package script at the v0.1 pin. If an approved external Skill validator is already available, run it and record its path and version. Otherwise validate YAML frontmatter, directory shape, and relative links directly and mark the missing repository command as unavailable.

## Browser and visual evidence

### Required widths

For page-level visual changes, use:

- 1440px desktop;
- 390px mobile.

Add these when affected:

- 900px tablet baseline;
- 760px narrow desktop;
- 375px regression width.

Use a viewport height that exposes the changed state. Add overlapping scroll segments or a contact sheet for below-fold content. A full-page screenshot with fixed navigation repeated in the middle is not sufficient mobile evidence.

### Required states

Capture the states changed by the task, including:

- initial and primary-action state;
- long content;
- loading, empty, error, unsupported, unknown, and disabled states when applicable;
- open overlays with natural and capped height;
- keyboard focus and focus restoration;
- mobile wrapping, local scroll, safe area, and no horizontal overflow.

Do not fabricate a state by injecting product DOM. Drive the real shared component with an approved controller, fixture, or controlled contract-mock.

### Browser runtime record

For each browser run, preserve:

- console errors and their count;
- uncaught page errors and their count;
- failed requests and their count;
- unexpected external requests and their count.

Classify each finding as expected fixture, controlled contract-mock, development traffic, or unexpected external traffic. Preserve zero and nonzero counts. A screenshot alone is insufficient. Expected fixture, controlled contract-mock, and development traffic are not live-backend proof.

### Labels

Every evidence index must state:

- source commit and tree;
- route and viewport;
- production component path;
- data source;
- whether the result is static preview, fixture, controlled contract-mock, or live backend;
- tested interaction;
- UNKNOWN, BLOCKED, and NOT RUN items.

Use this sentence when accurate:

> Real `App → AppShell → Rail` composition with controlled contract-mock; not live-backend proof.

A static HTML direction may be called a visual proposal. It may not be called production DOM, an integrated GUI, or live-backend proof.

## Semantic and accessibility pass

Check the rendered result, not only source:

- one `h1` and ordered heading levels;
- `button` for actions and `a` for navigation;
- visible labels and accessible names;
- logical Tab order and visible focus;
- menu/dialog Escape, outside dismissal, focus trap where modal, and focus restoration;
- status/alert semantics without duplicate announcements;
- error and state meaning not conveyed by colour alone;
- reduced-motion behavior;
- 200% zoom and long text;
- 44px minimum mobile touch target when the current component contract requires it.

Record untested assistive-technology behavior as UNKNOWN. Do not write “accessible” from a unit test alone.

## Evidence and handoff format

The writer receipt must include:

```text
Task: GUI task ID and attempt
Authority: Owner decision or page contract version
Base: commit and tree
Head: commit and tree
Branch: development branch
Files: relative paths and SHA-256
Commits: ordinary commit list
Focused checks: command, exit code, pass/fail counts
Hard gates: command and exit code
Advisory: base ref, new warning count, per-warning disposition
Browser evidence: route, viewport, state, composition, data source, path and SHA-256
Browser runtime: console errors, uncaught page errors, failed requests, unexpected external requests, counts, classifications, and dispositions
Full verification: PASS, FAIL, BLOCKED, UNKNOWN, or NOT RUN with exact reason
Limits: live backend, platform, screen-reader, package, or release gaps
Status: clean or exact remaining paths
Redaction: no secret, private launch URL, credential, personal data, or production state
Next: independent QA/Reviewer and Owner visual checkpoint as required
```

QA and Reviewer must receive the exact immutable head. They must not edit the writer's files. A later code or document change invalidates approval for the affected bytes.

## Secret and truth scan

Before handoff:

- inspect staged and committed diffs for tokens, passwords, keys, cookies, authorization headers, private URLs, local handoff files, and personal data;
- confirm screenshots contain no real credentials, invite material, private chat, or unrelated local content;
- keep raw launch URLs, ports, process IDs, sockets, and private runtime receipts out of the deliverable;
- report redaction as a checked result, not a general assurance.

If evidence requires a real credential or production restart, stop and request the exact missing authorization. Do not borrow another actor's credentials or convert a mock run into a live claim.
