# Cell model phase B — executor brief (HQ, 2026-11-03)

- Source: HQ R&D lead (emergent-org-structure-refactor), relayed by Allen on 2026-11-03 at 14:08:53Z.
- Executor: the cloud-claude cloud session. Allen's instruction: start phase B after phase A is complete.
- Target: 0.5.5.5. All work lands on `intg/cell-model-phase-a`. Never push `dev` or any `release/*`.

## Inputs

- Design: `docs/design/cell-thread-conversation-model-2026-11-03.md` (#2313), §9, §10 and §10.1.
- Phase A plan: `docs/design/cell-model-phase-a-plan-2026-11-03.md`. The fixed interfaces in §1.1 are consumed, never redefined.
- AGENTS.md rules.

## Scope and order

1. S8 attach: `hyprial agent attach` / `detach`, an agent cell with one `custody=external` harness member. It depends only on S3.
2. After S4 (threads) and S6 (conversations write side) are on the intg branch:
   - **S6 read side:** new `threads.*` and `conversations.*` daemon methods. The old methods (`sessions.*` and so on) stay as aliases with unchanged names, params, results and error codes.
   - **S10, squire becomes an ordinary agent** (Allen, 2026-11-03):
     - created from a template through the generic agent create; role data lives in cell data;
     - the owner's Lark DM becomes an ordinary adapter thread;
     - the squire address becomes an alias resolving to that thread;
     - `TARGET_SQUIRE_*` folds into the generic error codes;
     - profile `squire_channel` / `squire_adapter` become thread bindings;
     - retire `daemon/impl/squire/` and the squire special cases in shutdown, PAC, dispatch and onboarding;
     - no provider-key grant exception for squire.
   - **CLI:** new parameters for the new methods.
   - **S9 wrap-up:** rename `route_registration`; a GLOSSARY anchor CI gate; module-boundary rules (network and delivery must not import orgfs, cells or conversations).
3. **Wire protocol, option B** (Allen, 2026-11-03):
   - The sender authoritatively writes `threadId`, `conversationId` (cell id) and `class` into the payload's inner schema.
   - Nothing is added to the `hyprial-delivery-frame/v1` frame (proto.md §2.4).
   - Receivers prefer the sender's values and fall back to the phase A local inference when they are absent.
   - Two-way compatibility with 0.5.5.3.3 nodes must be verified.
4. **Out of scope:**
   - GUI reading threads and conversations (the GUI line does this);
   - external renames and deleting expired aliases (plan only, no execution).

## Constraints

- **New daemon methods** go through the unified chain:
  - a complete MethodSpec (params, result, errors, side effects, retry, identity) bound to a real handler;
  - served through `POST /daemon/request/<method>`;
  - calling domain public ports only.
  - Not allowed: empty handlers, private REST, copied IPC clients, HTTP running the CLI, cross-domain `impl` imports.
  - References: `docs/reference/daemon-request-consumer-baseline.md`, `docs/design/daemon-request/http-integration-2026-10-30.md`.
- **Contracts:** published contract changes sync `contract/` and regenerate openapi; old methods' generated artefacts stay byte-identical (`--check` PASS).
- **OrgFS:** messaging must work while OrgFS is unavailable.
- **Overlapping lines:**
  - actor-rustify Python batch (63 `ActorRuntime()` construction sites, `intg/actor-rustify`);
  - org membership (#2333).
  - Before each batch, merge the latest intg into the batch branch; each batch report lists files overlapping these lines.
- **Quality:**
  - uv only; red tests first; 7 files per directory and 1000 lines per file;
  - per-batch gates: structural limits, module boundaries, `uv run ruff check .`, touched test directories, `./contract/run-all.sh --isolated-only`;
  - tests use isolated HOME and TMPDIR.
- **Parallel sub-agents:** a single owner for shared files (registry, openapi, generated artefacts).
- **Commit identity:** `cloud-claude <cloud-claude@agents.hyprial.com>` plus a `Co-Authored-By: Claude <noreply@anthropic.com>` trailer. No force-push, no history rewrite.

## PRs and reporting

- **PR mode:** first test whether the token can create a PR against `intg/cell-model-phase-a`.
  - **A (can create):** one PR per batch. Branch `<type>/<slug>` (for example `feat/cell-attach`, `refactor/squire-ordinary-agent`), target `intg/cell-model-phase-a`. Batches may stack; sync by merge, never rebase.
  - **B (cannot create):** one PR branch, with commit subjects prefixed by batch (`[S8]`, `[S10]`).
  - The first progress report states which mode is used.
- **Batch reports:**
  - each batch report is a PR review comment with commands and PASS / FAIL / BLOCKED / UNKNOWN results;
  - do not wait for review; review fixes jump the queue;
  - uncovered decisions take the most conservative reversible option, marked 待 HQ 追认;
  - stop and ask only if a phase A §1.1 fixed interface must change;
  - the final report ends with "ready for HQ alignment".
