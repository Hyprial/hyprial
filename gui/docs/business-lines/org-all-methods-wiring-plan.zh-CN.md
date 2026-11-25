# org 全方法 GUI 接线计划（2026-11-02）

- 分支：`feat/gui-org-all-methods`
- 基线：`intg/gui-org-lifecycle` @ `aa0dfe659`（批次 1–6 + 集成清单修正）
- 目标：现有 GUI 动作凡有 daemon MethodSpec/HTTP 的，全部走生成 client 的真实调用；
  daemon 确实没有方法的动作继续显式 `unavailable`，不伪造成功。
- ACL 按用户裁决不进入 GUI（生成 client 已排除 IPC-only 声明）。

## 1. 接线映射

### org 生命周期

| GUI 动作 | daemon method | 说明 |
| --- | --- | --- |
| `refresh` | `org.list`、`org.network`、`hosts`、`orgfs.spaces`、`orgfs.ls`、`orgfs.trash` | 列表 + 当前组织明细 |
| `createOrg` | `org.create` | 已接 |
| `createInvite` | `org.invite` | 已接 |
| `joinByLink` | `org.join` | 已接；`org.execute` 是同一 handler 的别名，不重复暴露 |
| `syncContext` | `org.fetch` | 已接 |
| `previewRepair` / `repairOrg` | `org.repair`（apply=false/true） | 预览候选 → 确认执行 |
| `previewRemoveMember` / `removeMember` | `org.remove`（confirmed=false/true） | 保存预览的 operationId/deviceIds，过期统一 `stale` |
| `previewLeave` / `leaveOrg` | `org.leave`（confirmed=false/true） | 同上 |
| `previewDelete` / `deleteOrg` | `org.delete`（confirmed=false；再 confirmed=true + 预览 operationId） | 打开对话框时取预览并展示，owner 输入组织名后确认；只有确认阶段 `complete` 才算删除；`AUTHORITY_REQUIRED`、子组织 `OPERATION_CONFLICT` 如实显示；确认失败刷新列表并提示可能已部分执行 |
| `retryMachine` | `org.publish` | 发布本机组织记录后刷新；不按机器 id 伪造单机重试 |
| 拓扑/机器明细 | `org.network`、`hosts` | 映射目录设备与在线状态 |
| `setCurrentOrg` | 无 | 显式 `unavailable` |
| `claimInvite` | 无 | 显式 `unavailable` |
| `renameOrg` | 无 | 显式 `unavailable` |
| 模板/配置导入校验/发布候选/采信 | 无 | 显式 `unavailable` |
| 迁移 check/plan/submit/start/cancel/rollback、decideMigration | 无 | 显式 `unavailable` |
| `joinAgentOrg` / `leaveAgentOrg` | 无 | 显式 `unavailable` |

### OrgFS

| GUI 动作 | daemon method |
| --- | --- |
| `saveFile` | `orgfs.write` |
| `createEntry`（文件/文件夹） | `orgfs.write` / `orgfs.mkdir` |
| `moveEntry` | `orgfs.move` |
| `deleteEntry` | `orgfs.remove` |
| `restoreEntry` | `orgfs.stat`（取版本）+ `orgfs.restore` |
| `purgeEntry` | `orgfs.purge_plan` + `orgfs.purge` |
| 空间/目录/回收站快照 | `orgfs.spaces` / `orgfs.ls` / `orgfs.trash` |

`orgfs.history`、`read`、`watch`、`status`、`serve`、`checkout`、`members`、`invite`、
`join`、`remove_member`、`create`、`space_purge`、`unban`、`resolve`、`stat_at`、`read_at`
已有 HTTP 方法；当前 GUI 没有对应动作/渲染位，不新造 UI，不伪装成已接。

### link / hosts / top

以下 `link.*` 行已从表格删除。它们属于已退役的历史接线计划，对应 daemon 方法已随 T2b 删除，不得再接入或声称可用；仅 `hosts` / `top` 相关说明保留参考。

| GUI 动作 | daemon method | 说明 |
| --- | --- | --- |
| 机器在线状态 | `hosts` | 与 `org.network` 目录合并 |
| `top.snapshot` | 无对应 GUI 渲染位 | 保留 HTTP，不新造仪表盘 |

## 2. 三态纪律

- 写方法不自动重试；响应后校验失败抛 `OrgResultShapeError`（dispatch unknown）。
- 预览过期：`REMOVAL_CONFIRMATION_STALE` / `revision-confirmation` → `failed.code='stale'`，
  提示重新预览，不自动重放。
- 宿主绑定与所选 Agent 不一致：显式失败，不把别的 Agent 的结果显示成成功。
- 任何无方法动作继续 `unavailable`，消息说明“暂不可用”。

## 3. 验证

- GUI：`npm run typecheck`、`npm test`；新增 adapter/live source 单测。
- Python：全量留 intg 后 coordinator；本分支不改 Python 产品。
- 真实浏览器 / mesh / Casdoor：未实测项写 UNKNOWN/BLOCKED。
