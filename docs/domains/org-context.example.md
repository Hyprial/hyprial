# HyprialOS organization context

This is the initial 2026-08-16 organization view from the approved design.
It is an example, not an automatically trusted runtime document.
An owner must inspect its publisher, metadata and diff, then adopt it
with `hyprial org context import`. Check the fenced YAML block with
`hyprial org context validate --path <file>` before adopting; on the accepted
slot `hyprial org context validate` also checks the adoption record and the
orgfs publish marker.

```yaml
meta:
  version: 1
  issued_at: 2026-08-16
  publisher: alicesmith

lines:
  - {line: hyprial, owner: gregpaws, coordinator: hq-adjutant, board: hyprial-watch, repos: [harness-bridge]}
  - {line: kanban, owner: devusr, watcher: kanban-watcher, boards: [kanban, kanban-tw]}
  - {line: socialware, owner: alicesmith, repos: [socialware]}

members:
  - {username: alicesmith, display: 林懿伦, role: org-owner, home_node: mac-studio, squire: squire}
  - {username: gregpaws, display: 黄佳佳, machines: [feishu-main]}
  - {username: devusr, display: 姚升悦}
  - {username: usrb_developer, display: 李震宇, status: node-pending}
  - {username: fisher_designer, display: 陈瑞华, machines: [codex-main]}

routing:
  - {kind: defect, scope: harness-bridge, to: hq-adjutant}
  - {kind: board-item, to: kanban-watcher}
  - {kind: decision, to: alicesmith}
  - {kind: personal, to: "@sender.squire"}

norms:
  autonomy: [任务域内自治, 交付物与验收指标约定, 上游不介入日常执行, 超域必须升级]
  escalation: 按内容归线不按入口归属;第一接收者本职处理后,跨线后果必须转呈对应线
  etiquette: <hq PR 7 定稿后并入:引荐/委派/撤销/记账>

residents:
  - {name: hq-adjutant, node: mac-studio, role: coordinator}
  - {name: kanban-watcher, node: mac-studio, role: board-keeper}
  - {name: e2e-verifier, node: mac-studio, role: verifier}
  - {name: squire, node: mac-studio, role: squire, serves: alicesmith}
```
