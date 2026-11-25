# UUID 寻址迁移路径（draft v0.1，2026-10-15）

> ⚠️ **已被取代（supersede，非 overwrite）**：决策与裁定以 `uuid-addressing-migration-path.md` 为准。
> 本文件保留作**出处**（当时的三阶段划分与未决项 U1–U3）；原文 sha16 `0a0a4fa30b3ce65c`（4,330 B）。
> 其中 U1（兼容窗）已被 Allen 2026-10-23 裁定为 **0，不留窗**，见 path 文档 §9。

- 作者：hyprial-cli-developer（研发部）
- 状态：draft，待 Allen 批
- 日期：2026-10-15
- 依据：Allen 启动令 2026-10-15；侦察笔记（agent workspace `uuid-addressing-recon.md`）

## 0. 问题与目标

现状全网寻址按**短名**：`agent:<owner>:<节点>:<短名>`。改名=地址变，授予/home 行/adapter pin/会话引用全断，等效销毁重建；无 rename 命令。

目标：**寻址键切到不可变 agent id**，短名降级为可变 display alias。

## 1. 已有地基（不新建）

1. `Agent.agent_id`（registry/_base.py）：`a.`+base32(sha256(entityToken)[:16])，26 字符；rename 稳定、destroy/recreate 换新。
2. URI 文法 P1（kernel/.../uri.py）：`_AGENT_ID_SEGMENT = a\.[a-z2-7]{26}` 等谓词已落地；`native_actor` 拒绝 id 形状作创建名 ⇒ 名/id 永不冲突。
3. `agent_secret_grants` 已有 entity_token 列（过渡锚点）。

## 2. 已批决策（Allen 2026-10-15「全都批准」）

| # | 决策 | 取值 |
|---|---|---|
| D1 | id 稳定性 | incarnation 级：复用现 `agent_id`（destroy/recreate 换新 id，属预期） |
| D2 | 兼容窗 | 后定（建议：≥1 个 release 周期，双读期间度量旧名流量归零后关闭） |
| D3 | 短名终态 | 兼容期保留为寻址别名（只读解析），终态移除；短名始终可作 display |

## 3. 目标态

- 官方地址：`agent:<owner>:<node>:<agent_id>`（a.xxx 段）。
- 短名：display alias + 兼容期寻址别名（解析→id，不反向铸造）。
- 存量按名键的表全部加 id 键或经别名表解析。

## 4. 迁移路径（三阶段）

### P2 解析（先读后写）
- `parse_agent_uri` 已接受任意非空段 ⇒ a.xxx 天然可解析；新增：registry 按 agent_id 解析（`registry.get_by_agent_id`，唯一索引）。
- `resolve_agent_alias`：短名→agent 记录时同时返回 id；所有名→uri 铸造点（33 处）逐步改为「id 优先、名兜底」双轨 helper。
- 验收：以 id URI 寻址的 send/start/grant 全链可用；短名全链行为不变。

### P3 铸造（切写）
- `agent create` 返回/记录官方 URI= id 形态；`agent list/ps` 显示 `短名(a.xxx)`。
- 新增 `hyprial agent rename <名> --display <新名>`：只改 display 列，不动地址/home/授予。
- daemon 对外宣告（liveliness/route）双 key：id URI 为主，短名 URI 兼容期同宣告（presence 层双 token）。

### P4 存量回填与终态
- 逐表：agents(+agent_id 列唯一索引)、lifecycle_resources(agent-home:<id> 新行+旧行映射)、agent_secret_grants(键改 id+entity_token 校验)、retired_session_refs、adapters 路由绑定、routines actor 字段、desired-state harnesses(name→id 引用)。
- 存量消息行（inbox/custody/delivery_status/dlq 的 sender/recipient）：**不回填**，读侧经别名解析；兼容窗关闭后旧行只作历史。
- 终态（D2 窗口到期）：移除短名寻址别名，短名仅 display。

## 5. 兼容与回滚

- 双读窗口内新旧 URI 并存；回滚=P3 关闭铸造开关退回名 URI（P2 解析层无破坏性）。
- 风险面：presence 双宣告的 wake 匹配、zenoh inbox key 兼容（id 段无 `:`/特殊字符，base32 安全）、wf-* worker 名含 workflow id（迁移时随图走，不在首批）。
- wf-*：PAC worker 自管，**末批**随 PAC 图 schema 升级一并处理。

## 6. 测试与验收

- 单测：id 解析唯一性/rename 稳定性/别名解析回退。
- E2E：双 URI 寻址全链（send/start/grant/routine/adapter pin）；rename 后旧引用不断。
- 度量：兼容窗内旧名 URI 流量计数（复用 without_pac 式 record-only 仪表，落 biz/hooks），归零触发终态评审。

## 7. 与在轨工作的先后

- #1457（send-links 重构）：动 PAC 发送链 ⇒ **先合 #1457 再落 P2**，避免双改冲突。
- #1464（cell 部门化）：组织结构机制化，若其定址模型变化需对齐；建议 UUID P2 与其 R 裁决并行、P3 在其后。
- without_pac 迁 biz：无冲突，并行。

## 8. 未决（批时定）

- U1 兼容窗时长（D2）具体值。
- U2 rename 命令的权限口径（部长/owner/本人）。
- U3 `agent-home:<id>` 行迁移是否复用 #1467 ensure-home 的两阶段机制（建议是）。
