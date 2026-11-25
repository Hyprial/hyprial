# org-members — 组织、邀请与成员

- owner：姚升悦；分支 `feat/gui-org-members`；fixtures `gui/tests/fixtures/org-members/`
- 替代旧 dev-04（组织创建、邀请、领取邀请、加入状态、成员列表）。
- 后端判定三态：**可直接接入** / **需先做 daemon request 改造** / **后端尚无此能力**。
  引用行号基于 `intg/gui-v1` 初始 head（GUI round 2）。

## 范围

组织创建与修复、邀请（发出/领取/加入）、成员列表与移除、组织页 8 个子页签（概览、成员、邀请、
机器、拓扑、统计、文件、设置；交互依据 `gui/docs/org/interaction-design.zh-CN.md` v1.1）、跨 tab
入口（开始页提醒卡、Agent 组织徽标与详情组织分区、聊天被拒提示）、本机 Agent → 对端通信 link、
Agent 迁移向导。

## 页面与组件（现有基础）

- `features/org/`：根部 `index.ts` 显式公共面；`impl/domain/`（OrgGateway 契约与选择器）、
  `impl/workspace/`（组织页外壳）、`impl/sections/`（8 子页签）、`impl/dialogs/`、
  `impl/crosstab/`（跨 tab 组件）；开发数据与内存网关在 `src/dev/org/`。
- 数据源未接通时列表显示「状态未确认」、动作返回「…暂不可用」；写操作最多显示到「已提交，
  等待处理」（`gui/docs/modules/dev-04.md`）。

## 后端状态表

| 能力 | CLI / IPC 入口 | MethodSpec | HTTP 暴露 | 判定 |
| --- | --- | --- | --- | --- |
| 组织列表 `org.list` | `hyprial org list`；IPC `org.list`（ipc-inventory.md DR-27）；HTTP `POST /daemon/request/org.list` | 有：`src/hyprial/daemon/impl/request_catalog/org/__init__.py:161`（`ORG_LIST_SPEC`） | `http_exposed=True`，读画像（`mutates=false`、`retry=safe`、identity `none`） | **改造：本线** / MethodSpec 已声明 / 已注册 / 已准入 / 可调用；真实多节点验收 UNKNOWN |
| 组织配置同步 `org.fetch` | `hyprial org context fetch`；IPC `org.fetch`（ipc-inventory.md）；HTTP `POST /daemon/request/org.fetch` | 有：`src/hyprial/daemon/impl/request_catalog/org/__init__.py:177`（`ORG_FETCH_SPEC`） | `http_exposed=True`，写画像（`mutates=true`、`retry=never`、confirmation `none`、identity `none`） | **改造：本线** / MethodSpec 已声明 / 已注册 / 已准入 / 可调用；只有显式「同步组织配置」动作调用；真实候选需组织 mesh，单机 0 应答与真实候选结果 UNKNOWN/BLOCKED |
| 创建/修复组织 | `hyprial org create/repair`；IPC `org.create`、`org.repair`（ipc-inventory.md） | 无 | 无 | **需先做 daemon request 改造**（写；经 H-03 后接 HTTP） |
| 邀请与加入 | `hyprial org invite/join/pending`、`org group invite`；IPC `org.invite`、`org.join`（ipc-inventory.md） | 无 | 无 | **需先做 daemon request 改造**（`org.pending` 后端尚无，拟新增，见 cli-inventory.md） |
| 离开/删除组织 | `hyprial org leave/delete`；IPC `org.leave`、`org.delete`（ipc-inventory.md） | 无 | 无 | **需先做 daemon request 改造** |
| 移除成员 | `hyprial org remove`；IPC `org.remove`（ipc-inventory.md） | 无 | 无 | **需先做 daemon request 改造**（预览+确认） |
| pin/迁移状态 | `hyprial org repin/pin-authority/migration-status`；IPC 分发表（ipc-inventory.md org 分支） | 无 | 无 | **需先做 daemon request 改造**；v1 状态显示优先 |
| 组织网络诊断 | `hyprial org network`；IPC `org.network`（ipc-inventory.md） | 无 | 无 | **需先做 daemon request 改造** |
| 通信 link 读（拓扑通信视图） | 已退役（原 `link.list`、`link.stats` 已于 T2a/T2b 移除） | 无（`link.py` 已移除） | 无 | **已退役**（无对应 daemon request / IPC 方法） |
| 通信 link 写（添加/收回） | 已退役（原 `link.add`、`link.drop` 已于 T2a/T2b 移除） | 无（`link.py` 已移除） | 无 | **已退役**（无对应 daemon request / IPC 方法） |
| ACL | `hyprial org acl grant/revoke/ls`；IPC `org.acl.grant/revoke/ls`（ipc-inventory.md） | 无 | 无 | **需先做 daemon request 改造**；v1 只显示通信权限状态 |
| 组织文档/配置 | `hyprial org context validate/show/status/fetch/import`；IPC `org.fetch`、`org.publish`（ipc-inventory.md） | `org.fetch` 见上；其余无 | `org.fetch` 已准入；其余无 | **需先做 daemon request 改造**（validate/show/status/import 后端尚无，拟 `org.context.*`，见 cli-inventory.md）；候选采信入口仍不可用 |
| Agent 迁移（向导/迁移单） | `hyprial agent teleport`；IPC `teleport.run`（旧 `agent.migrate.*` 与 `transfer.*` 已移除） | 无 | 无 | **需先做 daemon request 改造**（契约以 `teleport.run` 为准，不固化旧名） |
| 组织文件 OrgFS | `hyprial org fs *`（27 入口）；IPC `orgfs.ls/read/write/mkdir/move/remove/history/watch/import/export/checkout/spaces/trash/restore/purge*` 等（ipc-inventory.md fs 族） | 无 | 无 | **需先做 daemon request 改造**；「文件」子页签 v1 保留交互，接线按改造进度 |
| 组织服务暴露 | `hyprial org service connect/disconnect/list/guide/expose/unexpose/exposures/whois`；IPC `service.connect/disconnect/list/guide`、`network.expose/unexpose/exposures/peerKey`（ipc-inventory.md DR-06） | 无 | 无 | **需先做 daemon request 改造**；v1 不做页面 |
| 机器子页签 | IPC `hosts`、`ps`、`top.snapshot`（ipc-inventory.md DR-07） | 无 | 无 | **需先做 daemon request 改造**；dev-05 机器面契约未定，缺数据时「状态未确认」 |
| 统计子页签 | IPC 无对应方法 | 无 | 无 | **后端尚无此能力**（拟统计查询方法，缺口登记） |
| 未加入组织引导（`orgs: []` 空状态） | 依赖 `org.list`（org 分发表） | 复用：`ORG_LIST_SPEC` | 已准入 | **复用：org-members**（`org.list` 已声明/注册/准入/可调用；空列表合法且正常；引导 UI 接线未做，UNKNOWN） |

已可接线的读方法为 `org.list`；`org.fetch` 作为显式写动作已准入（原 `link.list`/`link.stats`/`link.add`/`link.drop` 已退役移除）。其余组织写路径
仍需改造。本线是 fixtures 规则（Allen 原文）中「org 线」的权威方：org fixtures 完成后，user-settings、
agents、chat-channels 在最终提交时 cherry-pick 本线的实现替换各自自产版本。

## fixtures

- 必须提供：org 快照（无组织/工作组织所有者/成员视角）、成员与待审批、邀请各态（待领取/已跳过/
  过期）、迁移单各态（等待同意/迁移中-可取消/已完成）、拓扑与通信 link 数据、文件与历史层。
  现有开发情形（owner/member/no-orgs/invites/partial/template/migration/history-anomaly）
  迁入 `gui/tests/fixtures/org-members/`。
- 需要消费：agent（组织内 Agent 徽标/筛选——agents 线）、机器与 link 运行事实（chat-channels 渠道
  状态、user-settings 本机状态）。
- 邀请链接是凭据：fixture 用占位值，不写真实链接（ui 规范 §4.5）。
