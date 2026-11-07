# HyprialOS documentation

This is the map of `docs/`. Most pages are written in Chinese, with technical
terms in English. Start with the [project README](../README.md)
([中文](i18n/README.zh-CN.md)).

## Layout

```text
docs/
├── guides/      How to install, operate and recover a node
├── reference/   Facts about the current system (interfaces, boundaries, known problems)
├── design/      Design records and plans; one folder per multi-file topic
├── domains/     Material for one external domain (Lark, GUI client, Squire template)
├── i18n/        Translations of root documents
├── evidence/    Reviewer-owned raw evidence (kept byte for byte)
└── notes/       Evidence, repro material and handoffs (WORM: append only)
```

Rules for this tree:

- Put a new page in one of the buckets above. Do not add files directly under `docs/`.
- A topic with more than two related files (design, plan, reviews) gets its own folder
  under `design/`, for example `design/pac-expand/`.
- `notes/` and `evidence/` are append-only. Do not edit, move or re-format files there;
  their bytes are cited by hash in reviews.
- Some files are exact test inputs (see `EXACT_TEST_INPUTS` in `scripts/ci/docs_only.py`).
  Moving or renaming them needs a matching test change.

## Guides — operate a node

| Page | Use it to |
| --- | --- |
| [Operator guide](guides/operator-guide.md) | Install, first start, upgrade, profiles, login and device keys |
| [Onboarding checklist](guides/onboarding-checklist.md) | Join a new machine to an existing network |
| [Wheel distribution](guides/wheel-distribution.md) | One-line installers, tracks and how wheels are published |
| [Upgrade tracks](guides/upgrade-tracks.md) · [Release rules](guides/release-rules.md) | `stable` / `nightly` / `internal` and how they move |
| [Lark App onboarding](guides/lark-app-onboarding.md) | Create and authorize a Lark (Feishu) App |
| [Service connect](guides/service-connect.md) | Connect to an operator-approved service |
| [Zenoh two-machine setup](guides/zenoh-two-machine.md) · [Network isolation](guides/network-isolation.md) | Explicit endpoints and isolated test daemons |
| [Agent secrets](guides/agent-secrets.md) · [Agent readiness preflight](guides/agent-readiness-preflight.md) | Manage agent credentials and check an agent before start |
| [Daemon recovery runbook](guides/daemon-recovery-runbook.md) · [Pi projection recovery](guides/pi-projection-recovery.md) | Recover a broken daemon or Pi setup |
| [Cross-machine agent move](guides/transfer-at12-cross-machine-runbook.md) | Support matrix and rollback for moving an agent |
| [Retired TypeScript install](guides/cutover-runbook.md) | Remove an old TypeScript install before a fresh install |
| [GUI source mode](guides/gui-dev-mode.md) | Run `hyprial gui` and `hyprial gui dev` |

Maintainer process guides: [dev source-branch rule](guides/dev-source-branch-rule.md),
[merge procedure](guides/merge-procedure-pr-merge-guard.md),
[release checklist template](guides/release-checklist-template.md),
[homepage changelog sync](guides/homepage-changelog-sync.md).
Frontend work: [frontend components](guides/frontend-components.md),
[dev-03 components](guides/dev-03-components.md),
[dev-03 live integration](guides/dev-03-live-integration.md),
[daemon request Contract Mock](guides/daemon-request-contract-mock.md),
[GUI prototype handoff](guides/gui-prototype-handoff/README.md).

## Reference — how the system is today

- [Known problems](reference/known-problems.md) — read before you fix a bug.
- Architecture boundaries: [P1 transport/inbox](reference/p1-transport-inbox.md),
  [P2 daemon](reference/p2-daemon.md), [P2 providers](reference/p2-providers.md),
  [P3 MCP](reference/p3-mcp.md), [P3 Lark event dispatch](reference/p3-lark-event-dispatch.md),
  [P4 Squire](reference/p4-squire.md), [internal protocol](reference/proto.md).
- Harnesses and models: [harness model providers](reference/harness-model-providers.md),
  [turn hooks](reference/turn-hooks.md), [agent restore policy](reference/agent-restore-policy.md).
- CLI and requests: [CLI top-level commands](reference/cli-top-level-command-consolidation.md),
  [daemon request agent methods](reference/daemon-request-agent-methods.md),
  [daemon request baseline](reference/daemon-request-consumer-baseline.md),
  [PAC CLI principal](reference/pac-cli-principal.md),
  [route outbound](reference/route-outbound.md),
  [workflow node observation](reference/workflow-node-observation.md).
- CI: [integration routing](reference/ci-integration-routing.md),
  [performance baseline](reference/ci-performance.md),
  [pytest impact selection](reference/pytest-impact-selection.md),
  [logging](reference/logging.md).

## Design — why the system is this way

- [Architecture overview](design/design.md) — the original three-layer design
  (adapter/connector · harness network · Zenoh). Later records refine it.
- [PAC workflow](design/design-pac-workflow.md) ·
  [PAC v2 graph/flag reactor](design/design-pac-graph-flag-reactor.md) ·
  [PAC expand](design/pac-expand/design-pac-expand.md)
- Service connect: the operator guide is [guides/service-connect.md](guides/service-connect.md); its design record is internal
- [CLI target command surface](design/cli-surface/expected-commands.md) ·
  [CLI refactor plan](design/cli-surface/cli-refactor-plan.md)
- [Tailcat cutover and sidecar protocol](design/tailnet-cutover-architecture-2026-10-03.md)
- [Public snapshot denylist proposal](design/public-snapshot-denylist-2026-11-03.md)

Dated files (`*-YYYY-MM-DD.md`) are records of a decision at that date. When two
records disagree, the newer one wins unless it says otherwise.

## Elsewhere in the repository

- [GLOSSARY.md](../GLOSSARY.md) — canonical terms
- [ROADMAP.md](../ROADMAP.md) — long-term goals and release obligations
- [CHANGELOG.md](../CHANGELOG.md) — release history
- [contract/README.md](../contract/README.md) — cross-implementation contract tests
- [gui/README.md](../gui/README.md) — the shared web GUI
