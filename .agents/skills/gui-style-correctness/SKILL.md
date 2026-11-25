---
name: gui-style-correctness
description: Use when changing or reviewing Hyprial GUI components, page styling, responsive layout, visual hierarchy, interface copy, shared preview behavior, or visual evidence.
---

# GUI Style Correctness

## Overview

Make GUI changes from current product truth, then prove visual intent and runtime behavior separately. Reuse the current theme, components, and page architecture before adding CSS or markup.

**Core principle:** A screenshot can approve a visual direction. Only shared production code, hard gates, browser evidence, and truthful capability states can prove an implementation.

Read [Task 1 decisions](./references/task-1-decisions.md) when the change touches Settings, Connections, Org, overlays, notification behavior, or the B0 visual reference. Read [Verification and handoff](./references/verification-and-handoff.md) before choosing commands or preparing evidence.

## Authority and precedence

Apply authority within its domain:

1. The Owner's latest explicit product and visual decision controls the intended result.
2. An accepted page contract controls that page until the Owner changes it.
3. Current `dev` source, repository rules, tests, public feature boundaries, and capability contracts control implementation truth.
4. A fixed Design System commit informs visual mapping when the task names it.
5. Refactoring UI and general design guidance fill gaps. They do not override items 1–4.

Security, accessibility, and capability truth do not become optional when a visual direction changes. If authorities conflict across domains, stop the affected write and record the exact conflict.

## Choose the workflow speed

### Visual exploration

Use this mode when the Owner has not selected a direction.

- Work outside the product repository.
- Use real page content or clearly marked demonstration content.
- Compare 2–3 distinct directions at 1440 and 390 widths.
- Preserve required behavior in the brief.
- Label capability, contract, and live-backend states as UNKNOWN or BLOCKED.
- Stop after the Owner selects a direction and the page contract is recorded.

Do not run full production gates for directions that can still be discarded.

### Production implementation

Use this mode after the direction and behavior are fixed.

- Start from freshly fetched `dev` and record its exact commit and tree.
- Use one isolated clone or worktree, one branch, and one source writer.
- Follow test-driven development for behavior and contract changes.
- Show the first real App-shell image after focused gates, before expensive full validation.
- Hand an immutable commit to independent QA and Reviewer.

Do not transplant a confirmed visual into an old architecture by restoring deleted paths or CSS.

## Phase 1 — Inspect and freeze

- Read `AGENTS.md`, applicable nested instructions, the GUI standard, shared-entry map, component catalog, and current `gui/package.json`.
- Record `dev` commit, tree, branch, clean status, Node version, lockfile hash, and the exact Design System artifact when one is authoritative.
- Open the current production component, its public feature barrel, styles, adapters, tests, and dev composition.
- Search existing primitives, shared components, `--ui-*` tokens, and page patterns before proposing new code.
- Separate global rules from page-specific Owner decisions.
- Freeze the before-state warning output and the affected desktop/mobile visual baseline.
- List capability states that the visual work must preserve: loading, ready, empty, unsupported, error, unknown, disabled, and stale where applicable.

Use **reuse-first** in this order:

1. Existing semantic HTML and native control.
2. Existing primitive or shared component.
3. Existing page-local composition.
4. A small extension to a public component contract.
5. A new primitive only when at least two consumers need it or it owns complex shared behavior.

If a deleted path has a current `impl/` destination, port semantics to the current destination. Do not recreate the deleted path.

## Phase 2 — Specify and test

Write a compact change contract before product code:

- user task and primary action;
- content hierarchy and retained behavior;
- shared production/dev view path;
- data, capability, and controller differences;
- state matrix;
- semantic HTML and accessibility requirements;
- affected widths and overflow behavior;
- visual values and their token sources;
- focused tests and visual evidence.

For behavior changes, write a focused failing test and observe the expected failure. CSS text assertions are insufficient when cascade, Tailwind utilities, media queries, viewport geometry, focus, scroll, or computed styles decide the result. Add a browser or computed-style assertion for those cases.

For visual-only changes, the RED baseline may be an accepted before-image plus measured computed values. Record the mismatch before editing.

Keep tests tied to user-observable contracts. Do not lock obsolete class names, deleted paths, or incidental markup when role, name, state, geometry, or public composition is the real contract.

## Phase 3 — Implement

### Theme and visual system

- Keep colour, shadow, and font-family literals in `gui/src/styles/themes/hyprial-v5.css`.
- Consume component values through `--ui-*` from `tokens.css`.
- Preserve the v5 light/dark variable contract and shadcn mappings.
- Keep static surfaces flat unless the accepted design gives them elevation.
- Use floating shadows only for overlays such as menus, popovers, dialogs, and toasts.
- Use existing spacing, type, weight, radius, motion, and z-index tokens.
- Record an explicit Owner exception instead of disguising an off-scale value.

### Shared production and development UI

- Reuse the same page, component, reducer, validation, styles, and view model in production and development.
- Keep mock data, fixture selection, capabilities, adapters, controllers, and development tools at composition roots.
- Keep development tools outside product `#root` and production bundles.
- Preserve public barrels and `impl/` boundaries. Cross-feature code must consume public exports.
- Do not add a preview-only product page or branch product components on build mode.

### Semantic HTML and accessibility

- Use `button` for actions, `a` for navigation, real headings, labels, landmarks, lists, and tables.
- Give icon buttons accessible names.
- Preserve visible `:focus-visible` states.
- Keep dialog and menu focus entry, Escape behavior, outside dismissal, keyboard navigation, and focus restoration.
- Associate field errors and hints with their controls.
- Announce transient results once. Keep persistent warnings, errors, unknown states, and information that must be copied at their source.
- Respect reduced motion and do not rely on colour alone.

### Responsive and static surfaces

- Start from the smallest affected width, then add wider composition.
- Give grid and flex children a shrink strategy such as `min-width: 0`.
- Reflow or locally scroll content instead of imposing a desktop minimum width.
- Preserve touch targets, safe areas, sticky controls, long text, and 200% zoom behavior.
- Do not infer platform from width; consume explicit capabilities.
- Keep static cards and sections visually subordinate to the page's primary action.

### Capability truth

- Render UNKNOWN when evidence is missing. Do not convert missing data to zero or empty.
- Render BLOCKED when a known dependency prevents the check.
- Keep unsupported, error, empty, and unknown distinct.
- Do not turn a save receipt into connected, authenticated, sent, replied, or complete.
- Do not retry an unknown write automatically.
- Never use fixture, Contract Mock, static HTML, screenshots, or build success as live-backend proof.

## Phase 4 — Verify and hand off

1. Run the focused tests that cover the changed public behavior.
2. Run theme, module-boundary, type, and production/dev parity hard gates when their surfaces are affected.
3. Run the Refactoring UI advisory against the frozen base. Classify every new warning by rule, file, reason, and disposition.
4. Run browser checks at the affected widths. Include 1440 and 390 for a page-level visual change; add 760 and 375 when narrow desktop or legacy regression contracts are affected. Capture console errors, uncaught page errors, failed requests, and unexpected external requests. Classify expected fixture, controlled contract-mock, and development traffic, and preserve zero and nonzero counts. A screenshot alone is insufficient.
5. Capture before/after evidence from the real `App → AppShell → Rail` composition when the task changes product pages.
6. State the data source on every visual package. Use “controlled contract-mock; not live-backend proof” when applicable.
7. Run the full GUI verification only when the task or affected surface requires it. Preserve every failure and classify it.
8. Run link checks, `git diff --check`, secret scanning, and document checks.
9. Commit an ordinary readable change. Do not amend, rebase, reset, squash, or force-push.
10. Stop writing and hand the exact commit, tree, files, hashes, commands, exit codes, warning dispositions, images, limits, and UNKNOWN/BLOCKED items to independent QA and Reviewer.

A writer's self-check is not independent acceptance. Any change after Reviewer approval requires review of the new exact version.

## Hard gates

The following failures block a positive implementation claim when the changed surface is in scope:

- `theme-contract.test.ts` reports a literal, bypass, missing variable, light/dark mismatch, or font violation.
- Module-boundary checks report a new cross-feature `impl/` import or public-surface violation.
- Static parity (`verify:dev-product-parity`) finds product build-mode branches, dev imports, dev markers, or stale exceptions.
- Browser parity (`verify:dev-browser-parity`) finds different normalized product DOM, layout, route, or interaction results for the same input.
- Production-copy verification finds fixtures, development tools, scenario data, or mock composition in release output.
- Keyboard, focus, semantic HTML, or accessible-name behavior regresses.
- Responsive evidence shows horizontal overflow, hidden actions, covered input, unsafe fixed UI, or an unreachable state.
- Evidence labels a fixture, static preview, or controlled contract-mock as a real backend.

`check:refactoring-ui` is advisory unless the repository command uses `--strict`. A warning must still be fixed or explained; deleting the checker, adding a blanket exemption, or hiding the value is not a disposition.

## Common failure patterns

| Failure | Required response |
| --- | --- |
| The first Owner image arrives after full tests | Move the first 1440/390 image to the exploration or focused-gate checkpoint. |
| A screenshot is accepted but production uses another component | Trace both entries to the shared view and run DOM/browser parity. |
| A CSS source assertion passes but runtime geometry is wrong | Add a real browser computed-style or geometry assertion. |
| Old CSS is easier to merge than current `impl/` styles | Port behavior into the current destination and keep the old path deleted. |
| A warning conflicts with an explicit Owner value | Keep the narrow value and record the authority, scope, and warning ID. |
| The page looks empty because data is unavailable | Show unknown or unsupported; do not invent records or claim an empty result. |
| One page rejected a directory or helper navigation | Treat that as a page decision. Do not create a universal ban. |
| A static preview has polished success states | Mark demonstration data and keep live capability claims UNKNOWN. |
| Evidence contains only the first viewport or first screen | Add mobile and below-fold coverage for affected content. |
| A full suite has unrelated failures | Preserve the log, classify provenance, and do not report GREEN. |

## Four-phase executable checklist

Before stopping, answer each item with PASS, FAIL, BLOCKED, UNKNOWN, or NOT RUN.

### Phase 1

- [ ] Authority, page contract, base commit, tree, and warning baseline are exact.
- [ ] Existing components, public boundaries, and tokens were inspected before new code.
- [ ] Visual, contract, capability, and live-backend proof are separated.

### Phase 2

- [ ] The change contract names shared paths, states, widths, accessibility, and evidence.
- [ ] Each behavior change has an observed RED test or measured visual mismatch.
- [ ] Tests assert user-observable behavior rather than obsolete implementation details.

### Phase 3

- [ ] Production and development share one product implementation.
- [ ] v5 theme, semantic HTML, keyboard, focus, announcements, and responsive rules are preserved.
- [ ] Loading, empty, unsupported, error, unknown, disabled, and stale states remain truthful.

### Phase 4

- [ ] Focused tests and affected hard gates have fresh results.
- [ ] New advisory warnings have written dispositions.
- [ ] Desktop, narrow, mobile, long-content, and below-fold behavior were checked as applicable.
- [ ] Browser error and request evidence includes counts and traffic classifications; screenshots do not replace the record.
- [ ] Visual evidence states its composition and backend boundary.
- [ ] Handoff binds an immutable version and lists every limit.
