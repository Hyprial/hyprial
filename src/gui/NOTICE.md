# Sources and third-party notices

The reconstructed first-party files derive from this repository's D0 extraction
snapshot and retain the repository Apache-2.0 terms. The original input hashes,
changes and missing upstream source-commit mapping are recorded in provenance;
an npm artifact digest is not a Git commit. The exact enclosing repository
Apache-2.0 license text is included in this standalone source payload at
`legal/LICENSE`; third-party dependency notices remain in their packages.

Pinned runtime dependencies:

| Package | Version | License |
| --- | --- | --- |
| @deepseek-ai/cordis | 4.0.1 | MIT |
| @deepseek-ai/cosmokit | 1.8.5 | MIT |
| @standard-schema/spec | 1.1.0 | MIT |
| react / react-dom | 18.3.1 | MIT |
| proper-lockfile | 4.1.2 | MIT |
| graceful-fs | 4.2.11 | ISC |
| retry | 0.12.0 | MIT |
| signal-exit | 3.0.7 | ISC |
| scheduler | 0.23.2 | MIT |
| loose-envify | 1.1.0 | MIT |
| js-tokens | 4.0.0 | MIT |

The dependencies retain their original license and copyright notices in their
packages. Any future redistribution/bundle must include those notices; this
private checkpoint is not a distribution approval. The D0 ui-layout vendor MIT
license is preserved with the evidence input snapshot; dormant vendor files are
not copied into the reconstructed runtime dependency graph. The Cordis 4.0.1 pin
is the one validated by the prototype. No automatic latest/version upgrade occurs.

## Reconstruction provenance

Baseline: `4382f94689adcc1c4ca1cacc3961ad2af734f544`. D0 first-party source
anchor: `c3fa0178c5fb6ce3d043e384e163c2089627a12a` plus path and byte hash.
The D0 delivery documents were untracked; they are not part of that commit.

The repository-level `docs/evidence/gui-deep-modules-d1-2026-10-02/`
contains the unchanged 65-file D0 source snapshot and a separate, non-self-
referencing source/runtime hash inventory. Old bootstrap and SHASUMS bugs remain
in the historical snapshot; the new runtime never loads it. Reproduction uses
only this project's delivered source, fixtures, lockfile and a fresh npm install.

| Original D0 input | Reconstructed owner | Deliberate changes |
| --- | --- | --- |
| src/integration/gui-studio.mjs | studio/core.mjs, fs-persistence.mjs | Persistence injection, private core, one shared presentation authority |
| src/integration/gui-studio-host.mjs | studio/index.mjs | Explicit trusted context, no default-user path, neutral tool registry |
| src/integration/gui-reference-authoring.mjs | studio/reference-authoring.mjs | Retained first-party authoring guidance |
| src/shared/gui-style.mjs | presentation/theme.mjs | One shared implementation for validation/rendering; bounded facade |
| src/adapter/mock-adapter.mjs, fixtures/mock | session/index.mjs, session/fixtures | Context-scoped payload-aware deduplication, opaque cursors, history cursor as sole body, send-accepted ACK |
| src/composer/composer-intent.mjs | client/composer.mjs | Immutable pending send intent, explicit retry, late-ACK isolation, original cancel target |
| vendor/gui-src/client/gui-workspace.inc.js and the tested session/preview regions | client/workspace.mjs | Static ESM extraction; no runtime source evaluation, only tested rendering subset |
| plugins/trusted-panel-plugin.mjs and Cordis demo | client/ui-runtime.mjs, plugins/trusted-panel.mjs | Fixed trusted registry, lifecycle/slot facade, arrow-plugin constraint |
| src/host.mjs | host/index.mjs | Injected session/Studio/authentication ports, no daemon startup |

Exact transitive versions and integrity digests are in package-lock.json and
the evidence inventory. The npm-artifact-to-upstream-Git-commit mapping is
UNKNOWN; integrity digests are not presented as source commits. This is an
architecture checkpoint: tested mock, SSR, Studio storage and trusted Cordis
behavior are retained, not full legacy GUI/browser/plugin parity. No real
business session identity, transcript owner, daemon, model or credentials were
copied into a new authority.
