# CLI command 重构 · 完整实施排程（v1，2026-10-10）

本文是 [`cli-refactor-plan.md`](cli-refactor-plan.md) 的**实施排程**：把 27 项（已完成的 M0.1–M0.3 除外）排成**波次**，
每波 **≤2 个并行 PR**（Allen 2026-10-07 推进方式），依赖一律以计划 §5 的表为准；每项给出 PR 形状、
同步清单要点、owner 与当前状态。排程本身不改计划语义；冲突时以计划为准。

另含 **F9「闲置」定义的定稿设计**（Allen 2026-10-10 批准按 cli-developer 建议）。

---

## 0. 当前落地基线（2026-10-10，intg tip `2515bc545`）

| 项 | 状态 | 证据 |
|---|---|---|
| M0.1 下沉公共层（#1206） | ✅ 已落地 | `docs/evidence/cli-refactor/M0.1-REPORT.md`；intg 祖先 |
| M0.2 组转 package（#1237） | ✅ 已落地（**部分**：主要包已建，`delivery/outbox/network/lark-auth/squire/autoupdate/dispatch/mcp/keep/media` 等旧组仍在，归各自 R 收口） | `M0.2-REPORT.md`；一级注册表实测 |
| M0.3 叶子拆文件（#1308） | ✅ 已落地（PASS，纯移动零行为变更）；**未进 dev** | `M0.3-REPORT.md` |
| R13 user 定形 | ⛔ **未落地**：`R13-user-REPORT.md`（证据文档）在档，但实现分支 `feat/cli-user-identity`「local commit only, no push」，且**不能在 daemon 侧身份 IPC 落地前合** | R13 报告 |
| R7 service | 🔶 部分：service connect 已随 #1199 落地；剩余"删一级 `ps`、`network`→`service`"未做 | 计划 R7 行＋一级注册表实测 |
| R17 org work | ✅ 已落地（`commands/org/` 的 `org work …`＋`work` 组） | 计划 R17 行；一级注册表实测 |
| R14 `agent home` CLI 入口 | 🔶 部分：`commands/agent/home.py` 已在；provision 行为按卡 905a6523 核 | 一级/子组实测 |
| F1–F11 | ⛔ 全部未落地（F3 的 keep 存储、F5 的两份停启、F7 的两套 outbox 序列化、F8 的 `autoupdate install`、F9 的三处阈值、F11 的 `require_exit` 均实测仍在） | 计划 F 节＋一级注册表实测 |

---

## 1. F9「闲置」定义 · 定稿设计（Allen 2026-10-10 批准）

**统一判定**：闲置＝`now − last_activity ≥ threshold`；`last_activity` 以 **daemon 收到的上一个 turn
结束时间**为准（`operations/top.py:302` 现状），`process_started_at_ms` 兜底。**不是**"有没有 turn 刷新"
的布尔，也不是 base URL 流量。

**阈值收拢（三处 → 两档）**：
| 消费者 | 定稿 |
|---|---|
| 恢复判定 | `RESTORE_THRESHOLD_MS = 12h`（**保留**，属恢复策略语义，不动） |
| `agent list --inactive-since` | 保留为**参数**（本来就是显式传参，不算"定义"） |
| `top` | **保留 30 分钟窗口，但把展示语义改名为"近期无输出（quiet）"**，不再叫"闲置"——`top` 是运维实时视图，12h 会让它失去"发现卡住的 agent"的价值；"闲置"这个词只留给恢复语义 |
**不做**：base URL 流量监控 hook——harness 进程直连 provider API，流量不过 daemon；观测点不存在，
出站代理＝架构大改＋隐私影响（org-improver 旁证一致）。

**增强（可选，独立小 PR，不与 F9 捆绑）**：把 **harness 会话文件 mtime** 增补为 `last_activity` 的只读旁证
（补 daemon 回执缺失时的盲区）；跨 harness 的会话文件路径归一为实施难点，故独立成片。

F9 的 PR = 阈值收拢 + `top` 改语义 + 增补旁证开关（若增强同期做）；先红后绿三控照旧。

---

## 2. 波次排程（每波 ≤2 并行；完成后下一波解锁）

| 波 | 并行 PR | 前置（计划 #） | PR 形状要点 |
|---|---|---|---|
| W1 | **F2** ＋ **F3** | #5 / #6 | F2：`agent tiers probe` 收口 `classify_run`，删 dispatch 诊断路径；F3：删 keep 存储/`agent keep` 命令，启动迁移 keep→always（幂等三控）。两者同碰 `agent tiers`/doctor 的面在 W3/R1 再收口 |
| W2 | **F5** ＋ **F6** | #8 / #9 | F5：抽 `common/restart_daemon`，三入口共用（命令名不动）；F6：`_ipc_hosts` 调 `_online_host_nodes`。都是"只留一份实现"，零命令面变化 |
| W3 | **F7** ＋ **F10** | #10 / #13 | F7：`query … outbox` 改 `_outbox_entry_json`（JSON 契约以 outbox list 为准）；F10：`agent grant` help 按能力写实话（help 断言测试） |
| W4 | **F9** ＋ **R3** | #12（F3）/ #17（M0） | F9 按上文定稿设计；R3：删 `org execute`、`org show/status/fetch/import`→`org context …`，同 PR 改 squire 模板两份 |
| W5 | **R9** ＋ **R10** | #23 / #24 | R9：`lark-auth`→`adapter user-auth`、`onboard` 并入 `add`、`doctor/authorize`→`scopes …`、删 `pins`；R10：`install`→`socialware install`（恒 COMING_SOON） |
| W6 | **R11** ＋ **R14** | #25 / M0.3＋卡905a6523 | R11：channel 服务器改模块私有入口＋同 PR 改 `worker_channel.py:123,138`、`loops.py`、e2e runner，删 `mcp claude-turn-ended`；R14：`agent home provision` CLI 入口（daemon/IPC 侧卡 905a6523 已核） |
| W7 | **F8** ＋ **R4** | #11（F5）/ #18（F6） | F8：删定时器安装与 `autoupdate install/uninstall`，启动时检测卸载记日志，`run` 无 daemon 即退；R4：一级 `network`→`host`、`hosts`→`host list`（预留 `host status/create` 形态在本 PR 定） |
| W8 | **F11** ＋ **R15** | #14（F5）/ M0.3 | F11：`daemon stop` 改严格版（先等退出再判定；PR 附等待窗口实测）；R15：`workflow overview serve` / `routine overview show\|serve` 统一（模型/页面/SSE 各一份，默认 127.0.0.1） |
| W9 | **R5** ＋ **R2** | #19（F5,F8,F11）/ #16（F7） | R5：`restart`→`daemon restart`、新增 `daemon start`、`autoupdate status/restart/run`→`upgrade status/apply/run`（run 隐藏＋同 PR 改调度器 spawn argv）、`--source/--ref` help 隐藏、`upgrade` 附带解包 GUI；R2：`delivery status`/`trajectory`/`outbox`/`query`/`targets` → `message …`（B7/B9 同 PR） |
| W10 | **R1** ＋ **R16** | #15（F3,F9）/ M0.3＋#1210 | R1（最大改名）：`start/down→agent start/stop`、`top→agent top`、transfer→`agent move`＋隐藏、`restore-policy/-threshold→agent restore`、`home-census/migrate→agent home …`、grant 动词化＋删 revoke/`grants <ACTOR>`/一级 `grants-check/-visibility`；**同 PR 改 `daemon/impl/transfer/orchestrator.py` 远端 argv 与冻结基线 epoch.json**。R16：`workflow progress <GRAPH> <NODE> --note`（daemon 侧 #1210 已核） |
| W11 | **R7 收口** ＋ **F4** | #21（R1）/ #7（F1） | R7 收口：删一级 `ps`、`network expose/unexpose/exposures/peer-key`→`service …`；F4：squire setup/onboarding 改走 `agent.create`＋`adapter.pin` 同一实现（依赖 F1 落地） |
| W12 | **F1** ＋ **R12**（最后） | #4（M0＋org-improver 解析器上线＋Allen）/ #26（R2,R3） | F1：删三条旧身份路径（⛔ 解析器上线前不得删；先做只读迁移清单与 union_id 发送两件可前置）；R12：`send/ack/reply`→`message …`＋`fs` 内部重组（同 PR 改 skill、squire 模板两份、desktop、`contract/cli-blackbox/run.py` 的 `send/ack` 字面量）——爆炸半径最大，按计划放最后 |
| W13 | **R8** ＋ **R13** | #22（F1,F2,F4,R1）/ #27（F1＋Allen 裁定） | R8：`squire probe`→`agent tiers probe`、`user-proxy`→`user proxy`、`squire` 只留 setup、`onboarding plan/apply`→`init …`（同 PR 改 desktop `onboarding/cli.rs`）；R13：`user` 二级定形＋删 `adapter identities upsert`、`squire setup --owner-open-id` 独立入口（**daemon 侧身份 IPC 落地后**才可合；实现分支 `feat/cli-user-identity` 已有本地成果可续用） |

**并行 2 的纪律**：同波两个 PR 不得触碰同一文件；若依赖表迫使同文件，按计划"同一文件上的 PR 串行"顺延到下一波。

**每 PR 固定动作（计划 §4 摘要）**：实现侧（装饰器名 / `entry.py` add_typer / transfer 注册 / re-export 层）；
测试 argv 字面量（`tests/shell/cli/**`、`tests/integration/transfer/**`）；冻结基线（仅 R1）；
概念守护三件（help_inventory / top_level_concepts / retired_surface）＋help_coverage；契约（R2 改 targets、R7 改 ps、R12 改 send/ack，其余写 `Contract-Impact:`）；
模板与 skill（squire 模板两份、`.agents/skills/**`，PR 写 `Skill-Impact`）；desktop argv（R8 必改，其余 grep 确认）；文档与《目标》回填。

## 3. intg → dev 回流（攒批点）

按 Allen 2026-10-07 口径"攒成几批再整体进 dev"，建议三个回流点：
1. **R1 之前**：W1–W4（F2/F3/F5/F6/F7/F9/F10/F9 + R3）——行为统一批次；
2. **R12 之前**：W5–W8（R9/R10/R11/R14/F8/R4/F11/R15）——改名第一批；
3. **全部完成后**：W9–W13（R5/R2/R1/R16/R7/F4/F1/R12/R8/R13）——改名第二批＋最大爆炸半径项。
M0.3（`#1308`）随第 1 批回流（无需专项 PR）。回流动作＝intg→dev 的快进式合并 PR＋dev 闸门，
由线 owner（cli-developer）在 hq/Allen 确认的窗口执行。

## 4. owner 与执行形态

- **线 owner / 评审与落地**：hyprial-cli-developer（本线）。
- **执行**：PAC worker（tier 按角色：执行=fast、设计与高风险项=strong）；F1 与 R12 因爆炸半径大，建议 strong＋逐项评审。
- **外部依赖**：F1/R13 依赖 org-improver 身份解析器上线与 Allen 裁定；R16 的 daemon 侧（#1210）已核。
- **冻结期**：S1 冻结期间本排程**只规划不落地**（ Allen 既有裁定）；每波开 PR 前先合当前 intg（base-freshness 门）。

## 5. 变更记录

- v1（2026-10-10）：初版。基线 intg `2515bc545`；F9 设计定稿并入（Allen 批准）；27 项排程定稿。

## 6. tier → 模型映射（Allen 2026-10-10 指令：新代）

- `fast` → **gpt-6-luna**；`strong` → **gpt-6.1-sol**；`super`（高风险/设计）→ **gpt-6-astra**。
- ⛔ 旧代 `gpt-5.6-sol` 停用；派单 `launch` 显式写 `harness: codex` + `model: <上表>`（defaults 同时给 tier 与 model 会被 plan 拒：choose tier OR explicit harness/provider/model）。
- 执行形态（2026-10-10 更正，Allen 指出）：**claude / codex / pi 均有 headless 托管形态**（tests/daemon/harnesses/{pi/headless, claude} 等在案），dsh 亦为托管 harness；**"托管只有 codex"不成立**。派单时 `launch.harness` 可显式选择；未写时默认 codex。
- ⛔ 修正记录：v1 曾写"托管只有 codex"，系误读（把交互 TUI 形态当成了全部）。

## 7. 波次执行台账（更新于此，勿另开文档）

| 波 | 状态 | 图 / PR |
|---|---|---|
| W1 F2 | ✅ 交付 | 图 `wf-aa8b4049…` completed；**PR #1362**（head `5cf56fb02`，评审 75 passed 等）；⚠️ 由旧代 gpt-5.6-sol worker 产出（2026-10-10 15:1xZ，早于新代指令；内容已由线 owner 独立评审，保留） |
| W1 F3 | 🔶 重派 | 旧图 `wf-8be6af4d…` cancelled（旧代）；部分进度保留在分支 `feat/cli-f3-keep-to-always`（`ad0836c5e`+`6959658c4`）；新图 **`wf-909c93bf…`**（model **gpt-6.1-sol**）running，从该 HEAD 续作 |
