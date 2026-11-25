# Task 1 decisions and source pins

Use this reference when a GUI task touches Task 1 surfaces or needs examples of authority handling. These examples do not authorize a new implementation.

## Source pins

| Source | Pin | Meaning |
| --- | --- | --- |
| GUI Style Skill v0.1 base | `dev@3f2914996ca49a3764fb536a1e412d4a616822dd` | Repository scripts and architecture inspected for v0.1 |
| Design System B0 | `ed625a0dead02f5acc44138e0d0d5a4f3014b6af` | Owner-approved Hyprial Bento visual reference |
| B0 specimen | SHA-256 `4d2146be8aadee47be6dbbb4b172a4592a8f83f6eb34aa3bd7491581c483301a` | Exact `specimen.html` bytes |
| B0 merge | `31db29eecefea6c5563e845616df1cea81b4506e` | B0 entered main through PR #18 |
| Cached Design System main context | `c91a2bae475195f1dc405578af1c5762fe9625fb` | Contains B1 tokens; not claimed as the current remote head |
| B1 token commit | `a20691b38c1a58a251d3fe85e56f84435b7fd434` | Adds a semantic-token contract without changing the B0 specimen |
| Accepted Settings R7 source | SHA-256 `6f141499b7e5c42e1791a5871b7cc90bb9cfa64c9523ffb95b5cb75bfdf118b7` | Page-specific Settings visual contract |
| Task 1 fusion decisions | SHA-256 `493b529cb6b5eac2ade118933a97c4c6feb06b4e15f47a04f1475281947dc165` | Resolved OD-1 through OD-7 |
| Workflow retrospective | SHA-256 `aab430c8b389fd6522e0783c25a35c232555e9d65ea00e955694844b17bcbf71` | Two-speed visual workflow |

A fresh unauthenticated GitHub probe was unavailable during v0.1 authoring. Do not describe the cached Design System main ref as the current remote head. B0 is still the visual pin because:

- B0 is in main ancestry, not only on a topic branch.
- The B0 specimen has the same SHA at B0 and cached main.
- The B0-to-cached-main specimen diff is empty.
- B1 adds token machinery and does not replace the approved specimen.
- Explicit Owner GUI corrections remain higher visual authority than B0.

## Global rules

The following decisions apply across Task 1 GUI work:

- Fresh `dev` is the implementation base. Keep v5 theme architecture, `impl/` modules, public barrels, HYP/IM shell, bundled fonts, and production/dev parity.
- `gui/src/styles/themes/hyprial-v5.css` is the only source for colour, shadow, and font-family literals. Components consume `--ui-*`.
- Production and development use the same product pages and components. Mock inputs and development tools stay at composition roots.
- Static surfaces stay flat. Floating elevation belongs to overlays.
- Semantic HTML, keyboard access, focus restoration, reduced motion, and capability truth remain required.
- Info and success notifications last 4 seconds. Warning and error notifications persist until dismissal and enter Notification Center history.
- Copy success uses a transient success toast plus an accessible announcement. Content that must remain inspectable stays at its source.
- Unavailable and unknown states remain in-page and persistent.
- Owner decisions override the visual reference. Security and capability truth remain authoritative in their domains.

Repository details live in:

- [GUI design and development standard](../../../../gui/docs/ui-development-standard.zh-CN.md)
- [Shared product and development entry map](../../../../gui/docs/shared-ui-entry.zh-CN.md)
- [Refactoring UI advisory](../../../../gui/docs/refactoring-ui-advisory.zh-CN.md)

## Page-specific examples

These examples show how to preserve authority scope. Do not apply them to unrelated pages without a new decision.

### Settings

The accepted visible order is:

1. 账号
2. 当前设备
3. 模型服务
4. 本机服务与维护
5. 本机服务管理
6. 服务更新状态
7. 主机列表
8. 在线诊断
9. 本机诊断
10. 用户与平台身份绑定
11. 个人消息代理
12. 日志事件摘要
13. 网络配置

Settings rules from the accepted page contract:

- The visible account surface contains login and refresh only. Underlying truthful state and accessibility remain.
- Current Device and Model Service remain visible.
- Auto Upgrade capability remains in code but is not rendered.
- First Setup remains capability-gated and is not mounted in the controlled fixture.
- Duplicate Device and Org navigation links are removed because those destinations already exist in the rail.
- Model Service has no global Agent selector. Per-Agent provider binding stays in Agent Credentials.
- The page uses a 720px content measure and the accepted internal card hierarchy.

The Owner rejected a Settings directory after reviewing the R6 preview. This **rejected directory** lesson is page-specific: remove the Settings directory. It does not ban directories, tables of contents, side navigation, or jump links on every GUI page. A future page may use one when the Owner or page contract requires it and responsive/accessibility evidence supports it.

### Connections

- Keep one visible Model Service title and one Add Model Service entry.
- Provider login remains a capability inside the add-model flow. Do not render a separate provider module or delete the capability.
- Keep one full-card four-step Feishu connection flow and a Channel Status surface.
- Do not restore a standalone Agent binding card.
- Preserve disabled, unsupported, unavailable, error, and unknown semantics.

### Org

- Use six primary tabs.
- Members and Machines are secondary views under Overview.
- Keep old deep links functional by normalizing them to the secondary view.
- Keep all six primary tabs visible or wrapped at 375px.
- The secondary control-to-content gap is exactly 18px. This is an Owner-approved `spacing-scale` advisory exception, not a new spacing token.

### Overlay

- Account and Notification panels anchor to their own triggers.
- Keep 8px proximity, viewport clamping, natural height, capped height, and internal scrolling.
- Preserve Escape, outside dismissal, keyboard navigation, and focus restoration.
- Do not anchor either panel to an unrelated notice or shared rail constant.

## Observed baseline failures before this Skill

Task 1 provides the RED baseline for this process skill:

- Visual feedback arrived after expensive implementation and full evidence work.
- CSS source assertions passed while runtime cascade and computed geometry were wrong.
- Old WIP paths and pre-v5 CSS created fusion pressure against current `impl/` architecture.
- Static HTML, component fixtures, controlled contract-mock, and live backend evidence were conflated.
- Screenshots omitted below-fold content or captured stale interaction states.
- A Reviewer could approve package integrity while the Owner still rejected visual fidelity.
- Advisory warnings tempted cleanup by rule weakening instead of explicit disposition.
- A Settings navigation choice risked becoming a universal layout rule.

Use these failures to test whether a future Skill revision changes behavior. Do not erase the history when a later revision improves the workflow.
