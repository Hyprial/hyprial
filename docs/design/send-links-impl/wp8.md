# WP8：收尾包——skill、contract/ 同步、隔离 E2E、合 dev 就绪（规格）

- 版本：v2，2026-10-15。并入 wp8-review 的阻断 B1–B4 与建议 A1–A8（逐项对照见 §0.1），并入 README 裁定 77（interactive codex 以 `on-request` 启动：§2.1 第 8 条、§0 表 C49）；§5.3 与 §5.2 的基线数字按本分支 HEAD 重新实测。评审原文保留在文末「评审」节。

- 设计依据：[design-send-links-2026-10-14.md](../design-send-links-2026-10-14.md) §7、§8、§9、§11；README 工作包表第 8 行与「收尾」节；裁定 1–75；各 `wp*-acceptance-*.md` 的 Contract-Impact 与「集成者合并时要做」。
- 分支：`wp8/send-links`。规格基线 HEAD `54db6fda2`（集成头，含 WP0/1/2-A/2-B1/B2/B3/3-A/4/5-A/5-C-claude/5-D/6/7）。
- 实施前置（§8）：WP5-C pi、WP5-C codex、WP5-B、WP3-B 全部合入集成分支 `refactor/org-structure-constrain`；实施从合入后的集成头重新拉 `wp8/send-links`。
- 本规格只写文档；`src/`、`tests/`、`contract/` 的改动全部属实施阶段。

## 0. 结论

WP8 做四件事，按此顺序：

1. 修 t11（`tests/shell/login/device/test_boundaries.py::test_t11_daemon_tree_never_imports_login_or_names_the_credential`）：在 `tests/integration/native_harnesses/test_native_resident_tripwires.py` 的 `NATIVE_ORIGINS` 为 send-links 新增的 daemon 文件逐个登记 `("resident", ("new-module: send-links WPx …",))`。规格基线 `54db6fda2` 实测 17 个，本分支 HEAD（含 WP5-C codex）实测 18 个未分类文件（§5.2；多出 `harnesses/carrier/codex_carrier_support.py`）；实施时按当时的集成头重测，把 WP3-B/WP5-B/WP5-C 新增的文件一并登记。不加目录前缀豁免（该门的 `test_new_unclassified_files_fail_closed_even_in_other_original_clusters` 不允许）。
2. `contract/` 同步：新增一份规范说明 `contract/wire/send-links.md` 与一份机读登记表 `contract/wire/send-links.json`，由新测试 `tests/tooling/contracts/test_send_links_contract.py` 双向钉到 kernel、ports 与源码常量；wire golden 增加 summary 与 8 个新用例；`autoReply` 改名 `replyOwed`；全部 E2E 跑者补 `--summary` 与新 pending 行形状，并按硬切补 org 空间。
3. skill：改 `src/hyprial/skills/hyprial-ops/SKILL.md` 的发送章节、`.agents/skills/hyprial-workflow/SKILL.md` 的 PAC 通知与回复规则、`.agents/skills/hyprial-e2e/SKILL.md` 的场景表、squire `SKILL-template.md` 两份副本的 send 示例，以及 MCP 与 pi bridge 的工具说明。Skill-Impact: updated。
4. 新增两条 E2E：E2E-026（单节点隔离，进 CI 隔离门）与 E2E-027（跨节点与真实载体，受控门，含 `[REAL ENV]` 行）。然后按 §5 的清单让 #1457 进入可合 dev 状态：全量测试、CI、PR 正文、去 WIP、经 hq-adjutant 报 Allen。

收集到的 Contract-Impact 条目共 **49 条**（下表；C49 为裁定 77 新增）。表中「契约落点」是 WP8 要改的 `contract/` 文件；「钉住的测试」是实施后让该条变回归即红的测试。K 开头的测试在 `tests/tooling/contracts/test_send_links_contract.py`（§6.1）；`WG-` 为 wire golden 用例，经既有 `tests/tooling/contracts/test_wire_contract_adapter.py::test_every_wire_golden_case_runs_through_python_stack` 执行；`E26-`/`E27-` 为 E2E 断言编号（§4）。

| # | 条目 | 来源 | 契约落点 | 钉住的测试 |
| --- | --- | --- | --- | --- |
| C01 | 信封新增可选键 `summary`（zenoh 帧、outbox/inbox/custody 存储），≤512 UTF-8 字节 | 设计 §7；WP3 §6；wp3-acc-3 | `wire/golden/envelopes.json` 全部 agent 发出的 request/reply/event 夹具加 `summary`；`send-links.md` §2；`send-links.json.summaryMaxBytes` | K9；K14；WG-030 |
| C02 | `message.send`/`message.reply` 入参 `summary`，WP3-B 后必填 | WP3/WP3-B | `send-links.md` §3；`wire/message-sender-auth.md` 参数表 | WG-030；E26-12 |
| C03 | `SUMMARY_REQUIRED`；超长时 `data = {summaryBytes, maxBytes}`；缺失时 `reason == "missing"` | 裁定 16；WP3 T8 | `send-links.json.errorCodes`、`.summaryRequiredDataKeys`；`send-links.md` §5 | K1；WG-031；E26-12 |
| C04 | `MESSAGE_BODY_UNAVAILABLE`，`data = {messageId}`，不暴露消息是否存在 | 裁定 28；wp3-acc-3 | `send-links.json.errorCodes`；`send-links.md` §5 | K1；WG-033 |
| C05 | 新 IPC `message.read_body`：入参 messageId 加身份参数；返回 ok、messageId、conversationId、from、to、intent、summary、message、bodyBytes、origin、pending、fetched；消费动作，盖 fetched，不闭合；身份规则同 `message.ack` | WP3-A；wp3-acc-3 | `send-links.json.ipc["message.read_body"]`；`message-sender-auth.md` | K4；WG-033；E26-03 |
| C06 | 新 IPC `message.settle_hint`：返回 ok、messageId、hint∈{ack,keep}、pending；非 request 的 pending 行答 ack（裁定 56）；gate 非法答案按 keep 并记 `message.settle_hint.invalid` | WP3-A；WP5-A | `send-links.json.ipc["message.settle_hint"]`；`send-links.md` §8 | K4；WG-034；WG-035 |
| C07 | `message.pending.list` 行键恰为 `{messageId, deliveryId, conversationId, from, to, intent, kind, summary, bodyBytes, origin}`（WP3-B 后无 `message`、`topic`）；旧行 `summary: null` | WP3 §2.5、T2、T10 | `send-links.json.pendingRowKeys`；`inbox-watch/README.md`；`send-links.md` §3 | K4；WG-032；E26-03 |
| C08 | `replyTo` 必须是非空字符串，否则 `INVALID_ARGUMENT` | 裁定 39 | `send-links.md` §4 | WG-036 |
| C09 | agent 目标的回复 body 新增 `replyTo`（digest 变化）；`message.send` 带 `replyTo` 同样 | 裁定 39/55；wp2b-acc-1 §5.4 | `send-links.md` §4；golden `reply-basic` 夹具 | WG-036；E26-06 |
| C10 | kernel `worker_channel.METHOD_PARAMS`：send/reply 放行 `summary`，新增 read_body、settle_hint | 裁定 33/41 | `send-links.json.workerChannelMethods` | K4 |
| C11 | hold：`ok: true, queued: true, holdReason, detail`；码集 `HOLD_REASONS` 四个；键 `holdReason`（不复用 `reason`） | 裁定 14/15；设计 §9 | `send-links.json.holdReasons`、`.holdReasonKey`；`send-links.md` §4 | K2；E26-02；E26-07 |
| C12 | hold detail 词表 8 个：`minting`、`awaiting-peer`、`k`、`churn`、`org-unavailable`、`gate-unavailable`、`cwnd`、`W` | WP1 §（5 个）；裁定 54；wp2b3-acc-1 #10 | `send-links.json.holdDetails`（值→适用码） | K3 |
| C13 | 铸造时 delivery 带 `evicted`（被淘汰的对端） | wp2b-acc-1 §5.1 | `send-links.json.sendResultFields` | K4 |
| C14 | `LINK_WINDOW_FULL` 的同步拒绝：非 owner `user:` 目标在 W 满时 `ok:false`，`data = {target, detail: "W"}`；owner 目标永不挡 | 裁定 53 | `send-links.md` §4、§5（与 C11 的 hold 用法并列写明） | K1；E26-12（隔离等价断言：无 `LINK_*` 码、无 hold 行；W 满不挡 owner 的语义在单元测试） |
| C15 | `LINK_UNKNOWN` ingress 拒收：delivery status `state="refused"`、`ReceiveResult.code="LINK_UNKNOWN"`、`ConsumptionState.refused`；旧节点解码 `refused` 计入 `undecodable` | WP4 §T16；wp4-acc-1 #2 | `send-links.md` §5、§6；golden 新夹具 `request-from-unknown-sender` | WG-037；E27-04 |
| C16 | `LINK_RECLAIMED`：对已排干请求回复；否则沿用 `MESSAGE_REPLY_UNAVAILABLE` | 裁定 8；设计 §4.4 | `send-links.md` §5 | K1；E26-08 |
| C17 | `message.reply` 被 hold：`{ok: true, messageId, replyMessageId, replied: false, acknowledged: false, queued: true, holdReason, detail}`；hold 而 submit 未被接受：`ok: false` 同时带 `code` 与 `holdReason` | wp2b-acc-1 §5.2 | `send-links.json.replyHoldFields`；`send-links.md` §4 | K4 |
| C18 | 重放回执比首次少 `code`/`reason`/`openRequestsFrom` | wp2b-acc-1 §5.6 | `send-links.md` §4（已知差异） | 无新测（文档条目，单元测试已覆盖 W5） |
| C19 | `outbox list` 行带 `holdReason` | WP2-B 集成对账；wp2b-acc-1 §5.5 | `send-links.json.outboxListFields` | K4；E26-07 |
| C20 | `openRequestsFrom`：每条 delivery 上与顶层（仅非空时）；CLI stderr 提示行 | 裁定 17；WP7；wp7-acc-1 | `send-links.json.sendResultFields`；`send-links.md` §4 | K4；E26-05 |
| C21 | 新 IPC `link.list`、`link.stats`、`link.add`、`link.drop` 的参数与返回形状 | WP6 §2.1；裁定 13 | `send-links.json.ipc["link.*"]`；`send-links.md` §6 | K4；E26-02；E26-07 |
| C22 | `link.drop` 返回 `{state: "reclaiming", written: false}`，无 `drained` 计数；计数移到 `link.reclaimed` 事件 | wp2a-acc-2/3；README 运行记录 | `send-links.json.ipc["link.drop"].result`；`send-links.md` §6 | K4；E26-07 |
| C23 | hook 事件 `link.minted`、`link.reclaimed`（`ports.EVENT_LINK_*`）及其 payload | WP0；wp2a-acc-1 #7 | `send-links.json.hookEvents` | K6；P1（§6.2，钉发布）；E26-02（只验可见后果） |
| C24 | 日志事件约 26 个 `link.*`（含 `link.service_composed/skipped/failed`，`stage` 取值含 `bind`、`callback`、`binder`、`describe`）；删除 `link.e2e_permissive_gate` | wp2b3-acc-1 #10；wp2b3-acc-2 | `send-links.json.logEvents`、`.serviceStages`、`.retired` | K8；K10 |
| C25 | `top.snapshot` 行新增 `capacityW`、`outboundInFlight`（未接线为 null）、`inboundAwaitingInjection`；CLI `top` 三列 `W`、`OUT`、`INJQ`，缺失显示 `—` | 裁定 18；WP6 §1.3；wp2b3-acc-2 #6 | `send-links.json.topFields`；`send-links.md` §6 | K5；E26-04 |
| C26 | 保留名：`agent.create`、`session.register`/`register_guarded`、host-invite、transfer-hosted 对 `dispatch-coordinator`、`_turn-hooks`（精确相等，不限 owner/node）返回既有码 `INVALID_AGENT_NAME`，新文案 `is reserved for a daemon internal actor` | 裁定 63；wp4-acc-3/4 | `send-links.json.reservedActorNames`、`.reservedNameText`；`send-links.md` §10 | K7；E26-10 |
| C27 | link 治理表：owner 目标、全部 `user:` 目标、`route:`、`adapter:`/`channel:`/`system:`、daemon 内部 actor（按名、两个方向）不受 link 约束；`user:` 未闭合请求计入发送方 W | 设计 §4.1；裁定 25/37/45/51 | `send-links.md` §1 | K7（内部 actor 名）；E26-12（owner）|
| C28 | gate 未绑定：受管新请求在发送方 hold（`LINK_SELF_FULL`/`gate-unavailable`），接收方不派发；ingress 不因缺 gate 拒收 | 裁定 26/54 | `send-links.md` §4 | K3 |
| C29 | `LINK_PEER_FULL` 在 v1 是估计值（对端常规 link 数 ≥ 本节点 k） | 裁定 48 | `send-links.md` §14 已知限制 | 无新测（文档） |
| C30 | orgfs link 文件：`link/<canonical actor uri>.json`，只由该 agent 所在 daemon 写；内容是接受的对端列表、修订号、lifecycle；两份互引才有效；多 org 空间时镜像到每个已加入空间（裁定 50 缺省 a，待 Allen 裁） | 设计 §6；WP2 store；裁定 50 | `send-links.md` §12；`send-links.json.linkFile` | K15；E26-02；E27-01 |
| C31 | PAC 通知、routine 任务、远端 offer 投递的 inbox 行与 zenoh 帧开始带 summary；`InboxDeliveryIoAdapter.deliver` 内部签名 summary 必填 | WP5-D；wp5d-acc-1 | `pac/context/README.md` 正向说明；`send-links.md` §11 | E26-11（可选：PAC 不进 E2E-026，见 §4.3） |
| C32 | 本地 workflow-request 的 summary 取重建前的事件文本，远端由接收端对重建后全文派生，两者不同 | wp5d-acc-1 观察 | `pac/context/README.md` 写明「summary 不保证跨节点一致」 | 无新测（文档；见 §8 冲突 X5） |
| C33 | 新 IPC `session.turn.started`（actor、sessionRef、reportId≤128、可选 deliveryId；返回 `{ok, startedAtMs}`）；`session.turn.ended` 接受任何当前 interactive 会话，入参 `reportIds`（1–1000 个非空、各 ≤128 字符的数组），新增可选 `status`（completed/failed）、`deliveryId`，返回 `{ok, endedAtMs, turnCount, turnCountSinceMs, lastTurnEndedAtMs}`；按 `(actor, sessionRef, reportId)` 幂等，时刻由 daemon 首次接受时固定；同 actor 换代清旧 sessionRef 记账 | WP5-A §10；wp5a-acc-2 §7 | `send-links.json.ipc["session.turn.*"]`；`send-links.md` §8 | K4 |
| C34 | `turn.started/completed/failed` 覆盖 interactive 载体：source `daemon.turns`，`fidelity: observed`，sender/conversation/prompt/output 为空串；全部 `turn.*` 同一临界区取 seq | WP5-A | `send-links.md` §8；`wire/trajectory.md` | K6 |
| C35 | 载体不自动回复：headless 前缀 `hyprial-agent-sdk-reply:` 消失；机制应答只两种，id 前缀 `hyprial-turn-hook-reply:`、`hyprial-mechanism-reply:`，payload 带 `replyTo` 与 `origin.mechanism`（`turn-hook`/`python_worker`），带 summary；user-proxy 格式提示经 adapter `deliver_reply` 送达 | WP5 §10；裁定 58/59 | `send-links.json.mechanismReplyPrefixes`、`.retired`；`e2e/run-resident-providers.py` 去旧前缀 | K10；K11；E27-03 |
| C36 | wire golden `WG-001` 的 `autoReply: true` 与新语义不符 | WP5 §10；wp5a-acc-2 §7 | `wire/golden/expectations.json` 键改名 `replyOwed`（WG-001/002/004/005）；`ipc/wire.py` 映射 | WG-001/002/004/005 |
| C37 | 注入文本格式 `summary-v1`（golden `tests/daemon/harnesses/carrier/injection_golden.json`） | WP5 §2.3 | 不搬 golden；`send-links.md` §8 引用格式与 golden 路径；`send-links.json.injectionFormat` | K13 |
| C38 | 日志事件 `harness.settle.deferred`、`harness.mechanism_answer.deferred` 取代 `harness.reply.deferred`；删除 `harness.reply.already_answered`；新增 `harness.stranded_sweep_deferred` | wp5a-acc-2 §7 | `send-links.json.logEvents`、`.retired` | K8；K10 |
| C39 | 启动结算扫描（裁定 74）：已盖戳、无失败记录的 pending 行按 `settle_hint_for` 处理一次，ack 则 ack，keep 不动，模型不重跑，上限 256 行 | 裁定 74；wp5a-acc-2 | `send-links.md` §8（含 256 上限的随手 1 限制，见 §5.8） | E26-11 |
| C40 | 提醒：`turn.completed` 后对本 turn 注入过且仍 pending 的请求写 system notice，文本 `"{N} 条请求未回复：…；回复用 reply，无需回复用 ack"`，payload `openRequests` | WP7 §2.4 | `send-links.md` §8 | E27-03 |
| C41 | claude：启动 settings 新增 `UserPromptSubmit` hook；channel 发 `session.turn.started`（含 harness_read 补报）与 `status: failed` 的 `session.turn.ended`；轮末 `message.settle_hint` 后 `message.ack` | wp5c-claude-acc-1 | `send-links.md` §8 载体表 | E27-05（`[REAL ENV]`） |
| C42 | codex：注入只带 summary（`injection_format = summary-v1`）、KEPT 状态、`codex_turn_report` 表（内部）、删除日志事件 `worker.carrier.settlement.reply_lost` | wp5c.md §3 | `send-links.json.retired`；`send-links.md` §8 | K10；E27-06（`[REAL ENV]`） |
| C43 | pi attach：注入 summary-v1、上报 `session.turn.*`、轮末 settle_hint；提示词不再承诺自动回复（`sends your final reply back` 等字样从 `src/hyprial` 消失） | wp5c.md §2；G5 | `send-links.json.retiredPromptPhrases`；`send-links.md` §8 | K10；E27-05 |
| C44 | codex interactive 挂 harness-bridge MCP；`capabilities.py` 中 interactive codex 的 `PROACTIVE_SEND`、`TOOL_INJECTION` 改 native | 裁定 57/65；WP5-B | `send-links.md` §8 | E27-06 |
| C45 | MCP 工具面：`harness_send` 的 `to` 可为数组且 `summary` 必填；`harness_reply` 必填 `summary`；新工具 `harness_read_body`；`harness_read` 只返回 summary 与 bodyBytes；`WORKER_HARNESS_TOOLS`、`_CLAUDE_HARNESS_TOOLS` 含 `harness_read_body`；pi bridge 对等 | WP6 §1.7 | `contract/mcp-tools/run.py`（WP6 已改，32/32）；`mcp-tools/README.md`、`pi-receipts.md` 文字 | 既有 `test_unreachable_surface_retirement`（32/32）；K12 |
| C46 | CLI：`send`/`reply` 的 `--summary` 与本地 `SUMMARY_REQUIRED`；新 `read`；`link` 组；`top` 三列 | WP6 | `contract/cli-blackbox/run.py`（WP6 已改）；`send-links.md` §9 | 既有 cli-blackbox；E26-03/04/12 |
| C47 | E2E 语料：约 25 个跑者经 `hyprial send` 缺 `--summary`，约 15 个读 pending 行 `message`；硬切后 agent 间发送需要 org 空间 | WP3 §6；本规格实测（§3.6） | `contract/e2e/` 逐文件（§3.6） | `./contract/run-all.sh --isolated-only`；`run-isolated-gate.py` 全行 PASS |
| C48 | 升级连续性：基线版本写的 inbox 库在新版本可读（旧行 `summary: null`，`bodyBytes` 正确，`read_body` 可读正文） | WP3 §6「WP8 另需」 | `contract/e2e/10-upgrade-continuity.md` 与 `_upgrade_continuity/inbox.py` 增断言 | E2E-010 保持 PASS 且含新断言 |
| C49 | interactive codex 以 `on-request` 审批策略启动（有人在场，审批给 TUI 用户）；`-a never` 只用于 headless（该策略下 codex 拒绝全部 MCP 调用）；native 声明以此为前提 | 裁定 77；WP5-B 探针 | `send-links.md` §8 载体表；hyprial-ops skill（§2.1 第 8 条）；`capabilities.py` 注释（WP5-B 已写，WP8 只核对） | S1（skill 含 `on-request`）；E27-06 |

不进契约、只进 PR 正文的条目：`link_ledger`、`link_task_bindings`、`outbox.link_hold`、`codex_turn_report` 是内部 schema（wp2.md §B14；wp5c）；authority 豁免 61→64（wp5a-acc-2）；`ConsumptionState` 的 Python 枚举名。

### 0.1 评审意见对照（v2）

| 评审项 | 并入的小节 | 钉住的测试或检查 |
| --- | --- | --- |
| B1 登记表形状（`session.turn.ended` 的 `reportIds`、`link.list`/`link.stats` 键、K13 `format`）与 `ipc` 条目无测试 | §0 C33；§3.2 json；§6.1 K4、K13 | K4（对 `ipc` 每条经真实 router 取键集）；K13 |
| B2 `hyprial log` 查不到 hook 事件 | §4.1 E26-02、E26-07；§6.2 P1 | P1（桩 publisher 断言发布）；E26-02/07 只验可见后果 |
| B3 isolated 门里 owner 通道拿不到 `ok:true` | §4.1 E26-12 | E26-12（隔离等价断言）；单元测试钉 W 语义（C14） |
| B4 §5.3 残留命中无 owner、§7.8 白名单矛盾 | §5.3；§7.8；§1.3；§3.6 autoreply 排查 | §5.3 grep 无输出；§7.8 |
| A1 t11 清单 18 个；引用 §5.2；C47 约 25 | 顶部版本行；§0 第 1 条；§0 C47；§5.2 | §5.2 的分类命令无输出 |
| A2 随手项出处与撞名 | §1.2；§5.8（改称随手 1–4）；§0 C39 | 无（文字） |
| A3 `hyprialOnly: true` | §3.3 | `contract/wire/golden/validate.py`；WG 适配器测试 |
| A4 §2.1 行号 | §2.1 | S1（skill 内容） |
| A5 `MESSAGE_REPLY_UNAVAILABLE` | §2.1 第 6 条；§6.2 S1 | S1 |
| A6 K4 子集与相等语义、K8 收集规则 | §6.1 K4、K8 | K4；K8 |
| A7 E26-02/E26-07 预算与重跑策略 | §4.1 Preconditions | E26-02；E26-07 |
| A8 `sessions.py:163` | §5.3（已由 WP5-C pi 在集成分支消除，实测） | §5.3 grep；K10 |
| 裁定 77 interactive codex 的 `on-request` | §2.1 第 8 条；§0 C49；§4.2 E27-06；§2.6 | S1；E27-06 |

## 1. 范围、非目标、文件与预算

### 1.1 范围

- 文档与契约：§0 表 49 条的 `contract/` 落点；skill 与 MCP 工具说明。
- 测试：t11 分类修复；`test_send_links_contract.py`；E2E 清单与 CI 接线测试的更新；skill 内容测试。
- 契约适配器：`src/hyprial/daemon/impl/ipc/wire.py` 新用例处理器与 `autoReply` 键改名；`ReceiveResult.auto_reply` 改名 `reply_owed`（2 个文件各 1 行）。这是契约跑者的适配层，不改产品行为。
- 两条 E2E 场景与跑者。
- 合 dev 就绪清单（§5）。

### 1.2 非目标

- 不改 link 引擎、控制器、准入、载体、CLI 行为。§5.8 列出的随手项（随手 1–4、F1–F3、drop 崩溃窗口、WP2-B 重放少计 W）不在 WP8 修，写进 PR 正文的「已知限制」并各开一张卡（§5.8）。例外：只有当它们让 §5 的门红时才在 WP8 内修，修法须先红后绿且单独提交。
- 不搬 `injection_golden.json`；不改 `ipc/wire.py` 的 summary 映射（裁定 40 归 WP3-B，WP8 只核对）。
- 不做 `hyprial outbox list` 的等待时长列（设计 §9 列了，WP6 未做，见 §8 冲突 X7）。
- 不合并 #1457（合并由 Allen 定）。

### 1.3 文件清单与预算（规格基线实测，实施时重测）

| 文件 | 现行数 | 动作 | 预算 |
| --- | --- | --- | --- |
| `tests/integration/native_harnesses/test_native_resident_tripwires.py` | 942 | `NATIVE_ORIGINS` 加约 18–26 行 | ≤ 975（超过 990 则把 send-links 条目移到同目录新模块 `_send_links_origins.py` 再合并进字典；该目录现 4 个直接文件） |
| `contract/wire/send-links.md` | 新 | 规范说明 14 节 | ≤ 450 |
| `contract/wire/send-links.json` | 新 | 机读登记表 | ≤ 400 |
| `contract/wire/golden/envelopes.json` | 26 | 夹具加 `summary`；新增 4 个夹具 | ≤ 40 |
| `contract/wire/golden/expectations.json` | 32 | 改名 4 处；新增 WG-030–WG-037 | ≤ 45 |
| `contract/wire/golden/transport-inbox.json` | 33 | 加 WG-030–WG-035、WG-037 | ≤ 45 |
| `contract/wire/golden/validate.py` | 117 | 新规则：agent 发出的 request/reply/event 夹具必须带 ≤512 字节的 summary，负例夹具以 `fixture_id` 白名单列出 | ≤ 150 |
| `contract/wire/golden/README.md` | 37 | 用例数改为实测数 | ≤ 40 |
| `src/hyprial/daemon/impl/ipc/wire.py` | 584 | `_WG_030`–`_WG_037`；`autoReply` → `replyOwed` | ≤ 800 |
| `src/hyprial/daemon/impl/inbox/contracts/api.py`、`inbox/service/delivery/receive.py` | 334、365 | `auto_reply` → `reply_owed` | 不变 |
| `contract/wire/message-sender-auth.md`、`trajectory.md` | 56、118 | summary 参数；两新方法身份规则；interactive turn 事件 | ≤ 80、≤ 140 |
| `contract/mcp-tools/README.md`、`pi-receipts.md` | 10、62 | `harness_read_body`、summary | ≤ 20、≤ 75 |
| `contract/inbox-watch/README.md` | 99 | pending 行为 summary 不含正文 | ≤ 110 |
| `contract/pac/context/README.md` | 86 | PAC 投递带 summary；跨节点不保证一致 | ≤ 100 |
| `contract/README.md` | — | 语料地图 `wire/` 行加 send-links | +1 行 |
| `contract/e2e/` 既有跑者与文档 | 见 §3.6 | 补 `--summary`、改读 summary、补 org 空间 | 每文件 ≤ 1000 |
| `contract/e2e/26-send-links.md`、`run-send-links.py` | 新 | E2E-026 | 文档 ≤ 200；跑者 ≤ 700 |
| `contract/e2e/_send_links/`（`__init__.py`、`phases.py`、`cli.py`） | 新 | 阶段实现与 CLI 包装 | 各 ≤ 600 |
| `contract/e2e/27-send-links-cross.md`、`run-send-links-cross.py` | 新 | E2E-027 | 文档 ≤ 220；跑者 ≤ 800 |
| `contract/e2e/scenarios.json`、`validate.py`、`run-isolated-gate.py` | 425、95、300 | 加 026/027 | ≤ 470、≤ 100、≤ 305 |
| `tests/tooling/ci/runner/test_ci_e2e_baseline.py` | 658 | `EXPECTED_IDS`、`ISOLATED_IDS` | ≤ 665 |
| `.forgejo/workflows/e2e-controlled.yml` | — | 加 E2E-027 步骤（照 E2E-025 写法，preflight 拒绝时 NOT-RUN） | — |
| `tests/tooling/contracts/test_send_links_contract.py` | 新 | K1–K15 | ≤ 450 |
| `tests/shell/skills/test_send_links_skill.py` | 新 | S1–S4 | ≤ 150 |
| `src/hyprial/skills/hyprial-ops/SKILL.md` | 324 | 发送章节重写 | ≤ 390 |
| `.agents/skills/hyprial-workflow/SKILL.md` | 308 | 三节改写 | ≤ 335 |
| `.agents/skills/hyprial-e2e/SKILL.md` | 359 | 场景表两行 | ≤ 365 |
| `src/hyprial/daemon/impl/squire/SKILL-template.md` 与 `docs/domains/squire/SKILL-template.md` | 275、275 | send 示例补 `--summary`；两份逐字相同 | ≤ 285 |
| `src/hyprial/daemon/impl/harnesses/capabilities.py`、`src/hyprial/daemon/impl/application/messaging/inbox_surface/send.py` | 现各有一处含 `auto-reply` 的注释（167、400 行） | 只删注释行（§5.3） | 各不增 |
| `src/hyprial/daemon/impl/mcp/server.py` | 611 | 只改 `harness_send`、`harness_reply`、`harness_read` 的 description 字符串 | ≤ 625 |
| `src/hyprial/daemon/impl/harnesses/pi/resources/pi_harness_bridge.ts` | 254 | 同名工具 description 对等 | ≤ 262 |
| `docs/evidence/org-structure-constrain-2026-10-14/wp8/` | 新 | 先红、门禁、E2E 输出、blocked.txt | WORM，只追加 |

7 文件门：`tests/tooling/contracts/` 4 → 5；`tests/shell/skills/` 2 → 3；`src/hyprial/skills/hyprial-ops/` 不变；`contract/` 只受 1000 行门，不计目录文件数（AGENTS.md）。`src/hyprial/daemon/impl/ipc/` 已满 7，只改不增。

## 2. skill 更新

所有文字以实施时的 `hyprial <cmd> --help` 与 E2E-026 实测的 `--json` 为准；与本节不一致时以实测为准并在证据里记一行。

### 2.1 `src/hyprial/skills/hyprial-ops/SKILL.md`

位置：说明里写的 `.agents/skills/hyprial-ops/` 不存在，权威文件是 `src/hyprial/skills/hyprial-ops/SKILL.md`（打包注入 agent）。在「agent / worker / 消息」一节内（2026-10-15 在本分支重核行号，文件 324 行）：第 268–271 行（agent create 条目尾部的 managed worker send/reply/ack、代发、ack 三句）改写：保留 daemon session fence、`--from user:<owner> --on-behalf-of <actor>` 代发与 ack 仅限有匹配 session 的 worker 三个事实，并补 `--summary`；第 272–275 行（`hyprial query`）不动；第 276–277 行（MCP 会话条）就地改写（见第 7 条）。新增条目组「- **发消息:link、summary、回复**」（下列 1–8 条为其子条目）插在第 277 行之后、第 278 行（发文件/图片到飞书）之前。内容按以下条目写（中文，每条一两行）：

1. 发送前提：agent 只能向持有 link 的对端发送。第一次发送时 daemon 自动铸造 link：写本方 `link/<uri>.json`，等对端写回；这期间消息留在本方 outbox，`send` 当场返回 `queued: true, holdReason: LINK_MINTING`。不需要也不能手工批准。owner（`user:<owner>`）、所有 `user:` 目标、`route:` 目标不受 link 约束。
2. 命令面：
   - `hyprial send --from <自身> --to <对端> [--to …] --summary "<≤512 字节>" "<正文>"`；`--summary` 必填，缺失、全空白、超长都在本地报 `SUMMARY_REQUIRED`（退出 1，`--json` 带 `data.summaryBytes/maxBytes`），不发出任何消息。
   - `hyprial reply <messageId> --from <自身> --summary "…" "<正文>"`：唯一能闭合请求的方式。
   - `hyprial read <messageId> --from <自身>`：取正文；是消费动作（记 fetched），不闭合，仍要 reply 或 ack。
   - `hyprial ack <messageId> --from <自身>`：不需回复时结算。
   - `hyprial link list|stats --from <自身>`：看 link、k、W、cwnd、在途；`hyprial link add|drop <peer> --from <自身>`：运维动作，`drop` 返回 `reclaiming`，回收异步完成，排干数见 `link.reclaimed` 事件。
   - `hyprial outbox list --from <自身>`：被 hold 的消息与 `holdReason`。
   - `hyprial top`：`W`（本节点容量）、`OUT`（出站在途）、`INJQ`（入站等待注入）三列，`—` 表示该 actor 没有这项数据。
3. 收到的消息：上下文里只有发送者、意图、messageId、summary、正文字节数；正文用 `hyprial read` 或 `harness_read_body` 取。
4. 回复规则：只有带 reply-to 的消息才是回复（`hyprial reply`、`harness_reply`）。对请求方再 `send` 一条新消息不是回复：它占你的 W，原请求仍未闭合、继续占对方的 W；`send` 返回 `openRequestsFrom` 时 CLI 会在 stderr 提示。载体（pi、codex、claude、headless）在轮末不会替你回复；轮末未回复的请求会收到一条 system notice「N 条请求未回复」。
5. hold 原因与该做什么（表格，四码八 detail）：

| holdReason / detail | 意思 | agent 该做什么 |
| --- | --- | --- |
| `LINK_MINTING` / `minting`、`awaiting-peer` | 已写本方引用，等对端写回 | 什么都不做；daemon 放行。⛔ 不要重发 |
| `LINK_PEER_FULL` / `awaiting-peer` | 对端 link 数已满（v1 为估计） | 等；确有急事走 owner 升级。⛔ 不要换个话题重发 |
| `LINK_SELF_FULL` / `k` | 本方 link 已满且都有在途请求 | 先收回复或闭合自己的请求；需要时请运维 `link drop` |
| `LINK_SELF_FULL` / `churn` | 短时间回收过多 | 等一个周期 |
| `LINK_SELF_FULL` / `org-unavailable` | 本节点不在 org 空间或 orgfs 不可用 | 只剩 owner 通道；报 owner |
| `LINK_SELF_FULL` / `gate-unavailable` | daemon 的 link 服务未就绪 | 报运维；不要重发 |
| `LINK_WINDOW_FULL` / `cwnd` | 对该对端在途已达窗口 | 等它的回复 |
| `LINK_WINDOW_FULL` / `W` | 本节点未闭合事项已达 W | 先 reply 或 ack 入站的 pending，再发 |

   hold 永远是 `ok: true`；被 hold 的消息到 TTL 未放行时写 `expired`。
6. 拒绝码（`ok: false`，退出 1）：`SUMMARY_REQUIRED`；`LINK_WINDOW_FULL`（向非 owner 的人发新请求而 W 已满，同步拒绝）；`LINK_UNKNOWN`（对方节点找不到你的 link 文件：本节点没有加入 org 空间）；`LINK_RECLAIMED`（link 已回收，那条请求已被排干，不能再回复）；`MESSAGE_BODY_UNAVAILABLE`（不可读或不存在）；`MESSAGE_REPLY_UNAVAILABLE`（reply 的原请求已闭合或不存在：ack 你这侧的行，若仍要对话另发新消息，它占 W）；保留名 `dispatch-coordinator`、`_turn-hooks` 不能注册为 agent（`INVALID_AGENT_NAME`）。
7. MCP：`harness_send(to, summary, message)`，`to` 可为数组；`harness_reply(messageId, summary, message)`；`harness_read` 返回 summary 与 bodyBytes；`harness_read_body(messageId)` 取正文。第 276–277 行就地改写：保留「回入站消息用 harness_reply(回复+消费一步),新话题才用 hyprial send」，并补「新话题会占你的 W」。
8. 载体审批（裁定 77）：interactive codex 以 `on-request` 审批策略启动（有人在场，工具审批交给 TUI 用户）；`-a never` 只用于 headless，该策略下 codex 拒绝全部 MCP 调用。agent 在 interactive codex 里调用 `harness_reply`/`harness_read_body` 时若出现审批提示，是正常流程，由 TUI 前的人批准。

同时修改：第 293 行（已核）「每条收到的消息都带 from 与 to…headless worker 收到的正文首行…pi 附着注入都一样」改为「注入只含 summary，首行格式不变；正文用 read 取」；删去任何「轮末自动回复」字样。

### 2.2 `.agents/skills/hyprial-workflow/SKILL.md`

- 「Dispatch through a PAC graph」中 `--output-text` 段后加一句：PAC 通知（graph 通知、routine 任务、远端 offer）都带 summary；完整任务文本用 `harness_read_body` 或 `hyprial read` 取。
- 「End conversations without acknowledgement loops」：补三句：只有 `harness_reply`/`hyprial reply` 闭合请求；给请求方发新 `send` 不是回复且占 W；载体不会在轮末替 worker 回复，未回复请求在下一 turn 收到提醒。
- 「Keep an interactive session's inbox drained」：`harness_read` 返回 summary，不返回正文；处理每条前按需 `harness_read_body`。
- 「Declare the communication contract」：一句「worker 第一次向非 owner 的 agent 发送会铸造 link 并短暂 hold，不是失败」。

### 2.3 `.agents/skills/hyprial-e2e/SKILL.md`

场景表加两行：E2E-026（隔离，单节点 send links，命令 `uv run python contract/e2e/run-send-links.py`）与 E2E-027（跨节点与真实载体，`[REAL ENV]` 行需 pi/codex/claude 与 attended TUI）。

### 2.4 squire `SKILL-template.md`（两份逐字相同）

第 26–30 行三条 `hyprial send` 示例加 `--summary "<一句话>"`；第 22–24 行「harness_reply」说明加「回复必须带 summary」。`test_squire_template_attachments` 的「两份相同」断言继续成立。

### 2.5 MCP 与 pi bridge 工具说明

`mcp/server.py` 的 description（只改字符串）：

- `harness_send` 末尾加：`A first send to an agent without a link is held (ok, queued, holdReason) while the daemon mints the link; do not resend. A send is never a reply: to answer a request use harness_reply, otherwise the request stays open and the new message counts against your window.`
- `harness_reply` 加：`Only a reply closes a request. LINK_RECLAIMED means the link was reclaimed and the request was drained; ack instead.`
- `harness_read` 已改（WP6），不动。
- `pi_harness_bridge.ts` 对应三个工具的 description 用同一英文。

### 2.6 Skill-Impact 声明

`Skill-Impact: updated` —— hyprial-ops 发送章节（interactive codex 的 `on-request` 启动、link 自动铸造、`--summary`、`read`、`link list/stats/add/drop`、`top` 三列、hold 原因与处置、只有 reply-to 是回复、载体从不自动回复、保留名）；hyprial-workflow 的 PAC 通知带 summary、回复规则与 interactive 读取；hyprial-e2e 的 E2E-026/027；squire SKILL-template 的 send 示例；MCP 与 pi bridge 工具说明。

## 3. `contract/` 同步

### 3.1 `contract/wire/send-links.md`（新）

规范说明，14 节：1 治理表（C27）；2 summary 与 body（C01、C07）；3 IPC 方法与入参（C02、C05、C06、C33）；4 send/reply 结果、hold、回复关联（C08、C09、C11、C13、C14、C17、C18、C20、C28）；5 错误码（C03、C04、C14、C15、C16）；6 link/outbox/top 视图（C19、C21、C22、C25）；7 事件（C23、C24、C34、C38）；8 载体契约（C35、C37、C39–C44：注入字段、不自行回复、turn 上报、轮末只问 daemon、各载体落点、启动结算扫描、提醒）；9 CLI 面（C46）；10 保留名（C26）；11 PAC（C31、C32）；12 orgfs link 文件格式（C30）；13 已退役名（`hyprial-agent-sdk-reply:`、`harness.reply.deferred`、`harness.reply.already_answered`、`worker.carrier.settlement.reply_lost`、`link.e2e_permissive_gate`、`autoReply`）；14 已知限制（C29、启动扫描 256 上限（随手 1）、headless 行在轮末盖戳、drop 崩溃窗口、本地/远端 summary 差异、多 org 空间待裁）。

每节写：字段名、类型、何时出现、错误时的形状、来源（裁定号或 wp 文件）。不写实现路径以外的推测。

### 3.2 `contract/wire/send-links.json`（新，机读）

```json
{
  "schema": "hyprial-send-links/v1",
  "summaryMaxBytes": 512,
  "summaryRequiredDataKeys": ["summaryBytes", "maxBytes"],
  "errorCodes": ["SUMMARY_REQUIRED", "MESSAGE_BODY_UNAVAILABLE", "LINK_MINTING", "LINK_PEER_FULL",
                 "LINK_SELF_FULL", "LINK_WINDOW_FULL", "LINK_UNKNOWN", "LINK_RECLAIMED"],
  "holdReasonKey": "holdReason",
  "holdReasons": ["LINK_MINTING", "LINK_PEER_FULL", "LINK_SELF_FULL", "LINK_WINDOW_FULL"],
  "holdDetails": {"minting": ["LINK_MINTING"], "awaiting-peer": ["LINK_MINTING", "LINK_PEER_FULL"],
                  "k": ["LINK_SELF_FULL"], "churn": ["LINK_SELF_FULL"], "org-unavailable": ["LINK_SELF_FULL"],
                  "gate-unavailable": ["LINK_SELF_FULL"], "cwnd": ["LINK_WINDOW_FULL"], "W": ["LINK_WINDOW_FULL"]},
  "ipc": {"message.read_body": {"params": ["messageId"], "result": ["ok", "messageId", "conversationId", "from", "to",
            "intent", "summary", "message", "bodyBytes", "origin", "pending", "fetched"]},
          "message.settle_hint": {"params": ["messageId"], "result": ["ok", "messageId", "hint", "pending"]},
          "link.list": {"params": ["from"], "result": ["ok", "actor", "orgAvailable", "k", "W", "inFlight", "owner", "links", "held"]},
          "link.stats": {"params": ["from"], "result": ["ok", "actor", "nActive", "k", "W", "wTarget", "tMinMs", "tMaxMs",
            "tBarMs", "muMax", "diff", "grad", "periodMs", "links"]},
          "link.add": {"params": ["from", "peer"], "result": ["ok", "actor", "peer", "state"]},
          "link.drop": {"params": ["from", "peer"], "result": ["ok", "actor", "peer", "state", "written"]},
          "session.turn.started": {"params": ["actor", "sessionRef", "reportId", "deliveryId?"], "result": ["ok", "startedAtMs"]},
          "session.turn.ended": {"params": ["actor", "sessionRef", "reportIds", "status?", "deliveryId?"],
            "result": ["ok", "endedAtMs", "turnCount", "turnCountSinceMs", "lastTurnEndedAtMs"]}},
  "pendingRowKeys": ["messageId", "deliveryId", "conversationId", "from", "to", "intent", "kind", "summary", "bodyBytes", "origin"],
  "sendResultFields": ["queued", "holdReason", "detail", "evicted", "openRequestsFrom"],
  "replyHoldFields": ["ok", "messageId", "replyMessageId", "replied", "acknowledged", "queued", "holdReason", "detail"],
  "outboxListFields": ["holdReason"],
  "topFields": ["capacityW", "outboundInFlight", "inboundAwaitingInjection"],
  "hookEvents": ["link.minted", "link.reclaimed", "turn.started", "turn.completed", "turn.failed"],
  "logEvents": ["…实施时从源码收集，见 K8…"],
  "serviceStages": ["…实施时收集，至少含 bind、callback、binder、describe…"],
  "workerChannelMethods": ["message.read_body", "message.settle_hint"],
  "reservedActorNames": ["dispatch-coordinator", "_turn-hooks"],
  "reservedNameText": "is reserved for a daemon internal actor",
  "mechanismReplyPrefixes": ["hyprial-turn-hook-reply:", "hyprial-mechanism-reply:"],
  "injectionFormat": {"version": "summary-v1", "golden": "tests/daemon/harnesses/carrier/injection_golden.json"},
  "linkFile": {"dir": "link", "name": "<canonical actor uri>.json"},
  "retired": ["hyprial-agent-sdk-reply:", "harness.reply.deferred", "harness.reply.already_answered",
              "worker.carrier.settlement.reply_lost", "link.e2e_permissive_gate"],
  "retiredPromptPhrases": ["sends your final reply back", "your final reply is sent back"]
}
```

上面的字段与值以实施时的源码为准；`link.list`/`link.stats` 的结果键以 WP2 `views.py` 实际返回为准（路由层包 `{"ok": True, **mapping}`；WP6 只容忍缺失）；每个 `ipc` 条目由 K4 对真实 router 钉住，不再只钉方法名。`logEvents` 与 `serviceStages` 必须是源码实测的完整集合（K8 双向钉住），不允许留占位串。

### 3.3 wire golden

- `envelopes.json`：`from.actorId` 以 `agent:` 开头且 `type != "control"` 的夹具全部加 `"summary"`（`ack-control` 是控制帧，不加）；channel 入站夹具 `history-reply`、`quote-external`、`quote-unresolved` 的 summary 由发送组件派生，按裁定 30 填派生值。新增夹具：`request-no-summary`、`request-summary-513`（summary 为 513 字节 ASCII）、`reply-with-replyto-body`、`request-from-unknown-sender`。
- `expectations.json`：
  - WG-001/002/004/005：`autoReply` 改名 `replyOwed`，值不变（WG-001 true：request 欠 agent 一个显式回复；其余 false）。`sources` 加 `docs/design/design-send-links-2026-10-14.md §4.5`；条目带 `hyprialOnly: true`（改名偏离冻结的 TS 参考实现，裁定 76 X6）。
  - 新增（WG-030–WG-037 全部带 `hyprialOnly: true`，`sources` 引设计文档；`validate.py` 对用例键是否有白名单，实施时复核，必要时放行该键）：WG-030 缺 summary 拒绝（`accepted:false, code:SUMMARY_REQUIRED`，无 outbox/inbox 行）；WG-031 513 字节拒绝（`data:{summaryBytes:513,maxBytes:512}`）；WG-032 pending 行键集合（C07）且不含正文；WG-033 read_body 返回正文、盖 fetched、仍 pending，对不可读 id 返回 `MESSAGE_BODY_UNAVAILABLE`；WG-034 settle_hint：pending request 为 keep、reply 后为 ack；WG-035 pending 的 reply/event 行为 ack（裁定 56）；WG-036 回复 body 带 `replyTo`，`replyTo:""` 为 `INVALID_ARGUMENT`；WG-037 发送方无 link 文件时 ingress 拒收，delivery state `refused`、code `LINK_UNKNOWN`。
- `transport-inbox.json`：加 WG-030–WG-035、WG-037（WG-036 属 daemon 消息面）。
- `ipc/wire.py`：每个新用例一个 `_WG_0xx`，照现有写法用真实 inbox 栈与 `PermissiveLinkGate`/脚本门（WG-037 用拒绝门），不读 `expect`；`tests/tooling/contracts/test_wire_contract_adapter.py::test_adapter_does_not_echo_declared_expectation` 继续成立。
- 跑法：`PYTHONPATH=src uv run python contract/wire/golden/run-python.py` 输出 `RESULT GREEN n/n`，n 为实施时的用例数（基线 27 + 8 = 35）。

### 3.4 其它说明文件

`message-sender-auth.md`（summary 参数、read_body/settle_hint 身份规则同 `message.ack`、保留名）；`trajectory.md`（interactive 的 `turn.*` 字段）；`mcp-tools/README.md`、`pi-receipts.md`；`inbox-watch/README.md`；`pac/context/README.md`（正向说明，WP3 §6 要删的那句不存在，wp5d-acc-1 已核）；`workers/python-worker`、`workers/user-proxy` 的 schema 与夹具：核对是否携带 pending 行正文——按裁定 60 python_worker 照传原文，属于机制载体，不改 schema，只在 `send-links.md` §8 写明例外。

### 3.5 `check_contract_impact.py` 要过

- WP8 自身 PR（目标集成分支）：`uv run --no-project --python 3.12 python scripts/verify/check_contract_impact.py --base <集成分支头> --head HEAD --pr-body-file <body>` 退出 0。预期候选只有 `autoReply`（wire.py 删除行），其命中的 `expectations.json` 在同一增量内改过，判 synced。
- #1457（目标 dev）：同命令 `--base o/dev`。整个分支删除行里的已发布名（如 `hyprial-agent-sdk-reply:`、`harness.reply.deferred`、`autoReply`、`reason` 相关键）凡 `contract/` 仍引用的，引用文件必须在本分支增量内改过；目标 `impacts=0`。PR 正文仍写 `Contract-Impact:` 行（AGENTS.md 要求语义变化必须声明），证据记录输出首行的分母（`src_files_scanned`、`candidates`、`impacts`）。
- 该门只看删除行，新增字段漏同步不会被它发现，由 K1–K15 补位。

### 3.6 E2E 语料同步（C47、C48）

规格基线实测（`grep '"send"'` 与 `--summary` 计数）：下列文件调用 `hyprial send` 而无 summary：`run-actor-registration-roundtrip.py`、`run-cc-tui-worker.py`、`run-daemon-restart-survival.py`、`run-codex-worker.py`、`run-dsh-worker.py`、`run-dual-instance-roundtrip.py`、`run-hosted-start.py`、`run-hosted-upgrade.py`、`run-outbox-deadletter.py`、`run-pac-graph-slice.py`、`run-orgfs-forwarding-redial.py`、`run-pi-interactive-attach-manual.py`、`run-pi-wake-registration.py`、`run-peer-restart-relearn.py`、`run-selfdrive-routine.py`、`run-resident-providers.py`、`run-tui-attach-manual.py`、`run-two-machine-roundtrip.py`、`run-zenoh-half-open-reconnect.py`、`_harness_lifecycle/processes.py`、`_harness_lifecycle/phases/queue.py`、`_harness_lifecycle/phases/lark.py`、`_two_machine/local.py`、`_two_machine/phases.py`、`_upgrade_continuity/scenario.py`。读 pending 行 `message` 的：`run-actor-registration-roundtrip.py`、`run-cc-tui-worker.py`、`run-dsh-worker.py`、`run-daemon-restart-survival.py`、`run-codex-worker.py`、`run-codex-interactive-attach-gate.py`、`run-pac-graph-slice.py`、`run-hosted-start.py`、`run-routine-template-pac-journal.py`、`run-selfdrive-routine.py`、`run-resident-providers.py`、`run-pac-public-events.py`、`_harness_lifecycle/processes.py`、`_harness_lifecycle/phases/lark.py`、`_harness_lifecycle/phases/queue.py`。

每个跑者三步：

1. 每次 `hyprial send`/`reply` 补 `--summary`；直接 IPC 的 `message.send`/`message.reply` 补 `"summary"`。
2. 读行改为匹配 `summary`；需要正文的改调 `message.read_body` 或 `hyprial read`。
3. 按发送方与目标分类：`user:`/owner/`route:` 不受影响；agent→agent 的，单节点在发送前 `hyprial fs create group-<场景>-<nonce> --json` 建 org 空间，跨节点按 E2E-025/E2E-021 的 invite/join 让双方加入同一空间；首发后等待 hold 放行（轮询 `outbox list` 直到行消失，上限写死在跑者里）。未加入 org 空间的 agent→agent 发送会永久 hold（`org-unavailable`），这不是跑者能用重试解决的。

`_upgrade_continuity/scenario.py`：`--summary` 只传给新版本二进制；旧版本（v0.5.1）没有该参数。升级后断言（C48）：升级前写入、升级后仍 pending 的行 `summary is None`、`bodyBytes == len(正文 UTF-8)`、`kind == "message"`，`message.read_body` 返回原文。

`run-resident-providers.py`：删除对 `hyprial-agent-sdk-reply:` 前缀的匹配；该场景依赖载体自动回复完成「correlated reply」，硬切后改为由 worker 显式 `harness_reply`（给 resident 的提示里写明要求调用该工具），或把该断言改为「无自动回复 + 提醒到达」。两种做法写进 `06-resident-providers.md` 并选前者；`[REAL ENV]`，实施期间未跑记 BLOCKED。同类依赖自动回复的断言在 E2E-001/002/012/013/014 与 `run-pi-interactive-attach-manual.py` 中逐个排查，清单写进证据 `wp8/e2e-autoreply-audit.txt`。`run-pi-interactive-attach-manual.py`（第 16、353 行：docstring 里的「carrier must auto-reply」与标签 `auto-reply containing {marker}`）的断言与标签改为「无自动回复 + 提醒到达」（同上对 `run-resident-providers.py` 的后一种写法），其 attended 文档（`contract/e2e` 对应说明）同步。

文档同步：`01`、`02`、`04`、`06`、`08`、`12`、`13`、`14` 号文档与 `contract/e2e/README.md` 的命令示例与断言文字。

## 4. E2E 场景

两个场景，按 `hyprial-e2e` 的约定：隔离 home（跑者自建，`test "$HYPRIAL_HOME" != "$HOME/.hyprial"`）、`HYPRIAL_NETWORK_ISOLATED=1` 且 `hyprial ps --json` 的 `zenoh.isolated == true`、socket 与端口随 home、唯一 actor 名带 nonce、只停自己起的进程、跑完核无泄漏；每个跑者最后恰好一行 `RESULT PASS|FAIL|BLOCKED`；BLOCKED 带原文错误；任何断言 FAIL 即整体 FAIL；skip 不是结论。跑者用 `_daemon_runner.IsolatedDaemon`，二进制按 `HYPRIAL_BIN` 解析。

### 4.1 E2E-026：单节点 send links（`26-send-links.md`、`run-send-links.py`，isolated，进 `run-isolated-gate.py`）

前置：`uv sync --extra test`；无真实凭据、无 provider；`real_environment_required: false`。

步骤与断言（E26-xx 为 `scenarios.json` 的 assertions，也是证据行号）。

Preconditions（写进 `26-send-links.md`）：时间预算 E26-02 为 10 s、E26-07 为 30 s，出处是 1 s tick 加 orgfs 本地写回的估计（§9 UNKNOWN，实施时按实测校准一次）。重跑策略：任一预算超时，整个场景用新 home、新 nonce 重跑一次，不逐断言放宽预算；两次都超时才判 FAIL，首次超时的原文进 `e2e-026.txt`。

- E26-00 preflight：隔离生效；`hyprial version --json` 记录 SHA；`hyprial fs create group-links-e2e-<nonce> --json` 成功并取得 space id。
- 准备：`agent create` 建 A、B、C、D（加 nonce），对每个 `session.register` 一个 interactive 拉取会话（照 `run-actor-registration-roundtrip.py` 写法），拿到 sessionRef；之后 A–D 的读、回、ack 都经 IPC 带 sessionRef 或经 CLI `--from`。
- E26-01 summary 必填：`hyprial send --from A --to B "x" --json` 不带 `--summary` → `ok:false`、`code == SUMMARY_REQUIRED`、退出 1、A 的 `outbox list` 无新行；513 字节 summary → `data == {summaryBytes: 513, maxBytes: 512}`。
- E26-02 首发自动铸造：`send --from A --to B --summary s1 b1 --json` → `ok:true`、`queued:true`、`holdReason == LINK_MINTING`。在 10 s 内（250 ms 轮询）：B 的 pending 出现该 id；`hyprial fs` 读 `link/<A uri>.json` 列出 B、`link/<B uri>.json` 列出 A（两份互引）；`link list --from A` 中 B 为 `established`。（`link.minted` 是 hook 总线事件，不写 `hyprial log` 的 JSONL，E2E 不断言它；发布由 §6.2 P1 钉住，铸造的可见后果是互引文件加 `established`。）超时按 Preconditions 的重跑策略处理，仍超时即 FAIL。
- E26-03 只注入 summary：B 的 `message.pending.list` 该行键集合等于 C07，`summary == s1`，`bodyBytes == len(b1.encode())`，无 `message` 键；`hyprial read <id> --from B --json` 返回 b1；再读 pending 该行仍在；`message.settle_hint` 答 `keep`。
- E26-04 top：`hyprial top --json` 中 A 行 `capacityW` 为 ≥2 的 int、`outboundInFlight` ≥1；B 行有 `inboundAwaitingInjection`（int）；人读 `hyprial top` 表头含 `W`、`OUT`、`INJQ`。
- E26-05 新消息不是回复：B `send --to A --summary n1 ...`（无 reply-to）→ 结果中 `openRequestsFrom` 含 A 的 id1；非 `--json` 运行时 stderr 有 `if this is a reply, use 'hyprial reply` 一行；B 原请求仍 pending。
- E26-06 reply-to 闭合：`hyprial reply id1 --from B --summary r1 rb --json` → ok；A 收到 intent reply，`read_body` 的 payload 有 `replyTo == id1`；B 的 pending 不再有 id1；A 的 `outboundInFlight` 比 E26-04 少 1；A 对该回复行 `settle_hint` 答 `ack`。
- E26-07 hold 到放行：读 `link stats --from B` 的 k；让 B 与 C（及更多 agent，直到 B 的 established 数等于 k）建 link，并让 C 对 B 有一条未回复请求 rC。新 agent D 首发 `send --to B` → `queued:true`，`holdReason ∈ {LINK_PEER_FULL, LINK_MINTING}`，`detail == awaiting-peer`；D 的 `outbox list --json` 行带同一 `holdReason`。然后 `hyprial link drop C --from B --json` → `state == reclaiming`；30 s 内 D 的消息出现在 B 的 pending、D 的 outbox 行消失，且 rC 被排干（E26-08 核）。（`link.reclaimed` 同为 hook 事件，不在 `hyprial log` 查；发布由 P1 钉住。）不放行按重跑策略处理，仍不放行即 FAIL。
- E26-08 回收残余：rC 已不在 B 的 pending（排干为 ack）；B 对 rC `reply` → `ok:false`、`code == LINK_RECLAIMED`。
- E26-09 载体不自动回复（单节点可测部分）：A→B 一条请求，B 的会话执行 `pending.list fetched:true` 后什么都不回；等 5 s，A 收到的消息里没有任何 `replyTo == 该 id` 的行。真实载体的同一断言在 E27-03。
- E26-10 保留名：`hyprial agent create dispatch-coordinator --json` → `ok:false`、`code == INVALID_AGENT_NAME`、文本含 `is reserved for a daemon internal actor`；`session.register` 以 actor 段 `_turn-hooks` 注册 → 同码；`agent create dispatch-coordinator2` 成功（对照）。
- E26-11 重启后的结算扫描：A→B 一条 event（或 B 已收到的 reply 行）E1 与一条 request Q1；B 以 `fetched:true` 读过两者但不 ack；对 daemon SIGKILL，再 `start`；就绪后 E1 不在 B 的 pending，Q1 仍在且 messageId 不变；`hyprial log` 无 `harness.stranded_sweep_deferred`。
- E26-12 owner 通道不受 link 约束（隔离等价断言）：A `send --to user:<owner> --summary …` 的结果无 `holdReason`、无 `detail`、不含任何 `LINK_*` 码；若 `ok:false`，其 `code` 属 user 投递拒绝集（`TARGET_SQUIRE_UNCONFIGURED`、`TARGET_SQUIRE_ADAPTER_UNAVAILABLE`、`USER_DELIVERY_TIMEOUT` 等，隔离 daemon 无 adapter 与 squire profile，投递本身不可达）；A 的 `outbox list` 无 `link_hold` 行。owner 计入 W、W 满也不挡的语义由单元测试钉（C14）。
- E26-13 清理：只停本场景的 daemon；home 删除；无残留进程。

### 4.2 E2E-027：跨节点与真实载体（`27-send-links-cross.md`、`run-send-links-cross.py`，受控）

`real_environment_required: true`；`[REAL ENV]` 行逐条标注。各阶段独立分类，整体 RESULT 取最严重的（FAIL > BLOCKED > PASS）。

- E27-01 两节点互引（wp2-composition G2）：复用 E2E-021/E2E-025 的闭合回环拓扑（两个隔离 daemon X、Y，同一 org 空间经 invite/join）。X 上的 agent 首发给 Y 上的 agent：双方 `link/` 文件互引，非 owner 成员在根下 `mkdir link` 与写文件通过对端准入；两边并发首发不产生重名 `link` 目录节点。前置二进制（`HYPRIAL_TAILCAT_BINARY` 等）缺失记 BLOCKED 原文。
- E27-02 副本延迟：Y 侧暂停 orgfs 同步后，X 的已成立 link 上的新消息在 Y 先落库不派发，恢复后自动派发（dispatch 互引等待）。
- E27-03 `[REAL ENV]` headless 载体不自动回复：用 `_fake_provider.py` 起本地 provider，`hyprial start pi --headless`（无 pi 时依次试 claude/codex，均无则 BLOCKED）；fake 模型只返回最终文本 `FINAL-<nonce>`、不调工具。断言：发送方收不到含该文本或 `replyTo == id` 的消息；该请求在 worker 侧仍 pending；worker inbox 出现提醒 system notice（C40）；daemon 重启后模型不因该行重跑（turn 计数不变）。
- E27-04 未入 org 的节点：第三个 daemon Z 不加入 org，Z 上 agent 向 Y 的 agent 发送：Z 侧 hold `org-unavailable`；若绕过发送方直接投递（跑者用原始 IPC 构造 `remote` 帧），Y 侧记 `refused`、`LINK_UNKNOWN`。
- E27-05 `[REAL ENV]` attended TUI：Claude Code TUI（channel 推送 turn 是否触发 `UserPromptSubmit`；harness_read 补报失败时的工具报错呈现；F1 场景是否误提醒）；pi interactive attach（summary-only 注入、轮末 settle、提示词无自动回复承诺）。只能 attended 执行，跑者打印手工步骤与观察项，未执行记 BLOCKED。
- E27-06 `[REAL ENV]` codex interactive：按裁定 77 以 `on-request` 启动，模型能否在 TUI 审批后调用 `harness_reply`/`harness_read_body`（WP5-B 探针结论为前置）；`-a never` 仅作对照，记录其 MCP 调用被拒的原文，不作为失败。

### 4.3 BLOCKED 的记录

- 证据文件 `docs/evidence/org-structure-constrain-2026-10-14/wp8/blocked.txt`：每行 `<断言号> BLOCKED <原文错误或缺失前置> <解除需要的 owner 动作>`。
- 预计实施期 BLOCKED：E27-03（若无 provider 二进制）、E27-05、E27-06（需 attended 与登录的 TUI）；E27-01/02/04 视 tailcat 二进制是否在场。
- E2E-026 不允许有 BLOCKED：它只依赖仓库内容。其任一断言 BLOCKED 视为跑者缺陷。
- PAC 投递带 summary（C31）不进 E2E-026（需 PAC 图与 worker，`run-pac-graph-slice.py` 在 §3.6 同步时加一条断言「通知行 summary 非空」即可）。

## 5. 合 dev 就绪清单

按顺序，每步留证据到 `docs/evidence/org-structure-constrain-2026-10-14/wp8/`。

1. 前置合并：`git log --merges refactor/org-structure-constrain` 可见 WP5-C pi、WP5-C codex、WP5-B、WP3-B 的合并提交；WP8 分支自其后的集成头派生。
2. t11 分类修复（§0 第 1 条）。规格基线 `54db6fda2` 的未分类文件（17 个；本分支 HEAD 实测 18 个，多 `harnesses/carrier/codex_carrier_support.py`，WP5-C codex 新增；实施时仍用下面的命令重测）：`application/actors/sessions/turns.py`、`application/messaging/inbox_surface/link_send.py`、`composition/links.py`、`harnesses/carrier/codex_carrier_support.py`、`harnesses/carrier/contract.py`、`inbox/contracts/settlement.py`、`inbox/service/link_ledger.py`、`links/controller.py`、`links/model.py`、`links/policy.py`、`links/store.py`、`links/service/{__init__,_base,core,gate,holds,reclaim,views}.py`（均在 `daemon/impl/` 下）。命令：
   `PYTHONPATH=src:. uv run --extra test python -c "from tests.integration.native_harnesses.test_native_resident_tripwires import resident_offenders; from tests.shell.login.device.support import _FORBIDDEN,_SRC_ROOT; [print(p.relative_to(_SRC_ROOT/'hyprial')) for p in sorted(resident_offenders(_SRC_ROOT,_FORBIDDEN))]"`
   修后该命令无输出；登记的说明串写来源包（例如 `new-module: send-links WP2 link engine`）。先红证据为修复前该命令与 t11 的失败输出。
3. 退役名检查（全部无输出）：
   `git grep -n "hyprial-agent-sdk-reply:\|harness.reply.deferred\|harness.reply.already_answered\|reply_lost\|E2E_PERMISSIVE\|E2EPermissiveLinkGate\|sends your final reply back\|your final reply is sent back\|auto-reply" -- src contract scripts`（`contract/wire/send-links.*` 的「已退役」节除外，用 `':!contract/wire/send-links.*'` 排除）。本分支 HEAD（2026-10-15）实测命中 9 处、6 个文件：`src/hyprial/daemon/impl/harnesses/pi/resources/pi_harness_attach.ts:233`、`src/hyprial/shell/impl/cli/commands/agent/runtime/sessions.py:163`（两处提示词短语）；`src/hyprial/daemon/impl/harnesses/capabilities.py:167`、`src/hyprial/daemon/impl/application/messaging/inbox_surface/send.py:400`（两处注释）；`contract/e2e/run-pi-interactive-attach-manual.py:16,353`（2 处）；`contract/e2e/run-resident-providers.py:353,363,365`（3 处）。分工：
   - 提示词两处由 WP5-C pi 消除（wp5c.md §2 G5）。已核：集成分支 `refactor/org-structure-constrain` 上 `sessions.py` 与 `pi_harness_attach.ts` 已无这两句（集成分支 `src` 内只剩 `capabilities.py:167`、`send.py:400` 两处命中），本分支命中是因为尚未并入集成分支最新；WP8 从合入后的集成头重拉分支后自然消失。若实施时仍命中，属前置不满足，报集成者，不由 WP8 改。
   - `capabilities.py:167` 与 `send.py:400` 的两处注释由 WP8 删除（仅注释行，先红为本条 grep 的对应行，随证据提交）；§7.8 的 `src/` 白名单相应加这两个文件，owner 为 WP8。
   - `contract/e2e` 两个跑者由 WP8 在 §3.6 改（前者的 auto-reply 断言与标签、后者的旧前缀匹配）。
   实施时在集成头上重跑，以实测数为准。
4. 结构与边界：`uv run --no-project --python 3.12 python scripts/ci/check_structural_limits.py`、`check_module_boundaries.py` 均 `violations: []`；`uv run ruff check .`；`git diff --check <base> HEAD -- ':!docs/evidence'` 为空。
5. 全量测试：Node 24 在 PATH；`npm --prefix src/gui ci --ignore-scripts`；`npm --prefix desktop ci --ignore-scripts`；`uv sync --extra test`；`uv run --with libcst==1.9.0 pytest tests/`。0 failed，或失败恰为第 6 条的 dev 既有红。
6. dev 既有红（已在各验收中点名，须在 `o/dev` 头上实测确认）：`tests/daemon/harnesses/pi` controlled_loader t04–t06；`test_ipc_cost_counters` 的 CPU 覆盖比；kernel POSIX listener fd；pi SDK pin（pi 0.85.1）。环境类（不算 dev 红，须给环境证据）：GUI lifecycle ×5（GUI 30 s 未就绪）、dsh doctor localhost ×2、`test_pr_hygiene_git::test_git_env_scopes_the_token_to_one_command`（本机 `GIT_CONFIG_GLOBAL`）、`test_daemon_zenoh_failloud` ×8（scratch 无 `.venv/bin/hyprial`）。判定方法按 hyprial-e2e「Apply the release gate」第 1 条：dev 与候选各跑两次、交错，只有在 dev 上两次都红的同一测试 ID 才算 dev 既有红；`test_whole_registered_command_inventory_stays_on_native_app` 已由 WP6 修，若仍红按候选回归处理。
7. 契约语料：`./contract/run-all.sh --isolated-only` 退出 0；`run-python.py` `RESULT GREEN 35/35`（以实施时用例数为准）；`contract/mcp-tools/run.py` 输出 `mcp-tools: PASS 32/32`；`contract/e2e/validate.py` `RESULT PASS`；`uv run python contract/e2e/run-send-links.py` `RESULT PASS`；`uv run python contract/e2e/run-isolated-gate.py` 全部行 PASS。
8. 已知限制与随手项写进 PR 正文并开卡（不阻断；此处「随手 1–4」与 §6.2 的 skill 测试名 S1–S4 无关）：随手 1 启动扫描 256 上限被 keep 行占满；随手 2 gate 的 `settle_hint` 抛非 inbox 异常会中止启动（当前实现为字典查找）；随手 3 G3 未钉 `_confirm_mechanism_answer` 的调用者；随手 4 换代并发重建旧记账；claude F1（Stop 脉冲晚转发致误提醒）、F2（清 open-turn 与删脉冲次序）、F3（kept 请求重问 settle_hint）；drop 在「排干 ack 后、写 link 文件前」崩溃会丢撤回（wp2b3-acc-2 P1）；WP2-B 重放路径的 correlation 重复与 out 行少计 W（wp2b-acc-3 跟进 1、2）；headless 行在轮末才盖戳与 WP2「已注入」定义的关系（wp5a-acc-2 §6.2）；`link.gate_unbound` 未限频；`application/__init__.py` 998 行、`dispatch/remote/__init__.py` 1000 行；证据目录的 module-boundaries JSON 膨胀；pi worker 对 429/503 无退避（README 运行记录 10:0x）。
9. CI：#1457 上 `tests`、`contract-impact`、`skill-declaration`、隔离 E2E 门全部绿；红的只能是第 6 条确认的 dev 既有红，逐条在 PR 评论里给出 dev 上同 ID 的红链接或运行号。
10. PR #1457 正文（覆盖原正文，保留历史链接）：
    - 动机：问题报告与设计文档链接，R1–R18 一句话。
    - 改动：按工作包 WP0–WP8 各一段，每段列主要文件与一句行为。
    - 验证：第 4、5、7 条的命令与实测数字（passed/failed/skipped、RESULT 行），第 6 条的 dev 红对照，E2E-026 输出，E2E-027 每阶段的 PASS/FAIL/BLOCKED。
    - 未测项：逐条 UNKNOWN/BLOCKED（§4.3 与 §9）。
    - `Skill-Impact: updated`（§2.6 原文）。
    - `Contract-Impact: <一句理由>`，下接 §0 表的 49 条摘要与 `contract/wire/send-links.md` 链接，以及第 3.5 节命令的输出首行。
    - 待 Allen 裁定：裁定 7（PAC 绑定 link 不计 k）、裁定 50（多 org 空间默认镜像）、wp2-composition G7（换 owner 后 link 失效需新 owner 重铸）、C32（本地/远端 summary 不一致是否统一）。
    - 结尾按仓库约定加生成说明行。
11. 去 WIP：按 `.agents/skills/forgejo-ops/SKILL.md` 改 #1457 标题去掉 `WIP` 前缀；改完核 `skill-declaration` 因 `edited` 事件重跑后仍绿。
12. 通知：A 层 1:1 消息给 hq-adjutant，一行：`#1457 可合 dev：head <sha>，CI 绿（dev 既有红 N 条已对照），E2E-026 PASS，E2E-027 <各阶段结论>，待 Allen 裁 4 项见 PR 正文`。由 hq-adjutant 汇总报 Allen（HQ 通信分层 2026-10-11 与 09-25 修订；README「收尾」）。其中任何失败或需裁决的问题直接私信 Allen，抄 hq-adjutant 一行。

## 6. 测试

### 6.1 `tests/tooling/contracts/test_send_links_contract.py`（新）

先红形状统一为：登记表文件不存在时 `FileNotFoundError`；登记表与源码不一致时 `AssertionError` 并打印差集。测试只读 `contract/wire/send-links.json` 与公开常量；扫描源码用 `ast`，不 import `links.service`。

| # | 测试名 | 断言 | 先红 |
| --- | --- | --- | --- |
| K1 | `test_registered_error_codes_are_frozen_kernel_codes` | `errorCodes ⊆ ipc_errors.ALL_CODES`；且等于 `{SUMMARY_REQUIRED, MESSAGE_BODY_UNAVAILABLE} ∪ HOLD_REASONS ∪ {LINK_UNKNOWN, LINK_RECLAIMED}` | FileNotFoundError |
| K2 | `test_hold_reasons_and_key_match_ports` | `set(holdReasons) == HOLD_REASONS`；`holdReasonKey == HOLD_REASON_KEY` | 同上 |
| K3 | `test_hold_details_are_closed_over_the_source` | AST 收集 `links/policy.py`、`links/service/*.py`、`links/controller.py`、`inbox_surface/link_send.py` 中 `Hold(` 第三参数与 `detail=` 关键字的字符串常量，集合等于 `holdDetails` 的键；每个 detail 对应的码 ⊆ `holdReasons` | 登记表缺 `gate-unavailable` 时 AssertionError |
| K4 | `test_registered_ipc_methods_are_routed_with_registered_shapes` | `ipc` 的方法名 ∈ router allow-list，且等于对应 ports 常量；`workerChannelMethods ⊆ worker_channel.METHOD_PARAMS`；`pendingRowKeys`、`outboxListFields`、`sendResultFields`、`replyHoldFields` 与源码中构造这些结果的字面键一致（各取一个已有的产品函数在内存 inbox 上调用一次取键集，照 `tests/daemon/application/transport/channel/summary` 的夹具）。对 `ipc` 每个条目，在内存 inbox 夹具上经真实 router 调用一次，断言 `result` 键集与实际返回一致、`params` 覆盖 handler 必读键。语义分两组：`pendingRowKeys` 是行键全集相等（WP3-B 后恰 10 键）；`outboxListFields`、`sendResultFields`、`replyHoldFields`、`ipc.*.result` 中按条件出现的键是「登记键 ⊆ 实际构造键，且条件出现的键在触发条件下必现」（send 结果另有 target/messageId/accepted/code/reason 等键，不要求相等） | 同上 |
| K5 | `test_top_fields_match_ports` | `topFields == [TOP_FIELD_*]` | 同上 |
| K6 | `test_hook_events_are_kernel_event_names` | `hookEvents ⊆ kernel EVENT_NAMES`；含 `EVENT_LINK_MINTED/RECLAIMED` | 同上 |
| K7 | `test_reserved_names_match_kernel_and_registry_text` | `set(reservedActorNames) == RESERVED_AGENT_ACTOR_NAMES`；对 `agent.create` 的登记路径以保留名调用得到的错误文本含 `reservedNameText` | 同上 |
| K8 | `test_link_log_events_are_closed_both_ways` | 收集规则：用 `ast` 只取 `src/hyprial/daemon/impl/{links,composition,application/messaging/inbox_surface}` 中 `_log`/logger 调用的事件名实参（字符串字面量），不扫全文件字面量，并排除 `hookEvents` 与 IPC 方法名（`ports.py` 的 hook 事件常量与 `link.*` IPC 名都不是日志事件）；所得 `link.*` 集合等于 `logEvents` 中 `link.*` 子集；`harness.settle.deferred`、`harness.mechanism_answer.deferred`、`harness.stranded_sweep_deferred`、`message.settle_hint.invalid` 在源码中存在 | 同上 |
| K9 | `test_summary_limit_matches_ports` | `summaryMaxBytes == SUMMARY_MAX_BYTES`；`summaryRequiredDataKeys == list(SUMMARY_REQUIRED_DATA_KEYS)` | 同上 |
| K10 | `test_retired_names_are_gone_from_src_and_contract` | `retired` 与 `retiredPromptPhrases` 的每个串在 `src/` 下 `git grep -F` 无命中；在 `contract/` 下只命中 `contract/wire/send-links.*` | 基线上 `pi_harness_attach.ts`、`sessions.py` 命中（若 WP5-C/B 已消除，则以 `contract/e2e/run-resident-providers.py` 的旧前缀命中为先红） |
| K11 | `test_mechanism_reply_prefixes_exist` | 两个前缀在源码中各有恰一处定义 | 同上 |
| K12 | `test_mcp_send_and_reply_descriptions_state_the_reply_rule` | 构造 MCP server（照 `contract/mcp-tools/run.py` 的 fake factory）列工具：`harness_send` 描述含 `holdReason` 与 `harness_reply`；`harness_reply` 描述含 `LINK_RECLAIMED`；`harness_read_body` 在列表中 | 描述未改时 AssertionError |
| K13 | `test_injection_format_registry_points_at_the_golden` | `injectionFormat.golden` 文件存在，其 `format` 字段为 `summary-v1` | FileNotFoundError |
| K14 | `test_golden_agent_envelopes_carry_a_summary` | `envelopes.json` 中 `from.actorId` 以 `agent:` 开头、`type != "control"`、不在负例白名单的夹具都有 ≤512 字节的 `summary` | 基线夹具无 summary |
| K15 | `test_link_file_location_matches_ports` | `linkFile.dir == LINK_DIR` | FileNotFoundError |

### 6.2 其它测试

| # | 文件 | 改动 | 先红 |
| --- | --- | --- | --- |
| T11 | `tests/shell/login/device/test_boundaries.py::test_t11_…`（不改代码） | 由 `NATIVE_ORIGINS` 登记转绿 | 基线已红（17 个 offenders），证据记原文 |
| WG | `tests/tooling/contracts/test_wire_contract_adapter.py`（不改） | 新用例经既有测试执行 | 先写 expectations 再写 `_WG_0xx`：`RESULT RED` |
| P1 | `tests/daemon/links/service/test_hold_release.py`（minted）与 `test_reclaim.py`（reclaimed），追加用例（该目录已 7 个直接文件，不新增文件） | 用桩 publisher 断言 service 的 `_emit` 以 `EVENT_LINK_MINTED`/`EVENT_LINK_RECLAIMED` 发布，payload 含 `peer`/`revision`/`side`；E2E 无法经 `hyprial log` 查这两个 hook 事件，故由本测试钉发布 | 把断言里的事件名改成不存在的名字：AssertionError |
| B1 | `tests/tooling/ci/runner/test_ci_e2e_baseline.py` | `EXPECTED_IDS` 加 026、027，`ISOLATED_IDS` 加 026 | 先改测试：`scenarios.json` 缺 ID 而红 |
| S1 | `tests/shell/skills/test_send_links_skill.py::test_ops_skill_documents_the_send_links_surface` | hyprial-ops SKILL.md 含 `--summary`、`hyprial read`、`hyprial link list`、`harness_read_body`、`openRequestsFrom`、四个 hold 码、`LINK_RECLAIMED`、`LINK_UNKNOWN`、`MESSAGE_REPLY_UNAVAILABLE`、`on-request`（裁定 77） | 基线缺 |
| S2 | 同文件 `test_ops_skill_never_promises_an_automatic_reply` | 不含 `自动回复` 出现在肯定句（用固定否定串白名单，如「不会在轮末替你回复」） | 基线若含则红；不含时以 S1 为先红 |
| S3 | 同文件 `test_workflow_skill_says_pac_notices_carry_a_summary` | hyprial-workflow SKILL.md 含 `summary` 与 `harness_read_body` | 基线缺 |
| S4 | 同文件 `test_squire_template_send_examples_carry_a_summary` | 两份模板中每个 `hyprial send` 行都含 `--summary` | 基线缺 |

焦点命令：`PYTHONPATH=$PWD/src uv run --extra test pytest tests/tooling/contracts tests/tooling/ci/runner tests/shell/skills tests/shell/login/device tests/integration/native_harnesses tests/daemon/squire/template -q`。

## 7. 验收（可机检）

1. §6 焦点命令 0 failed、0 error；K1–K15、S1–S4 名字都存在（`grep -c "def test_" tests/tooling/contracts/test_send_links_contract.py` ≥ 15）。
2. §5.2 的分类命令无输出；t11 绿。
3. §5.3 的 grep 无输出。
4. `PYTHONPATH=src uv run python contract/wire/golden/run-python.py` 最后一行 `RESULT GREEN n/n`，n ≥ 35；`uv run python contract/wire/golden/validate.py` 退出 0。
5. `./contract/run-all.sh --isolated-only` 退出 0；`uv run python contract/e2e/validate.py` 输出 `RESULT PASS`；`uv run python contract/e2e/run-send-links.py` 最后一行 `RESULT PASS`。
6. `uv run python contract/e2e/run-isolated-gate.py` 每行 PASS（含 E2E-026）。
7. `check_contract_impact.py`：对集成分支与对 `o/dev` 两次均退出 0，输出首行记入证据。
8. `git diff --name-only <base>..HEAD -- src/` 只含：`daemon/impl/ipc/wire.py`、`daemon/impl/harnesses/capabilities.py`（仅删注释）、`daemon/impl/application/messaging/inbox_surface/send.py`（仅删注释）、`daemon/impl/inbox/contracts/api.py`、`daemon/impl/inbox/service/delivery/receive.py`、`daemon/impl/mcp/server.py`、`daemon/impl/harnesses/pi/resources/pi_harness_bridge.ts`、`daemon/impl/squire/SKILL-template.md`、`skills/hyprial-ops/SKILL.md`。超出即越界（§1.2 例外须在 PR 正文逐条说明）。
9. 结构门、边界门 `violations: []`；`ruff check .` 清；`git diff --check` 清。
10. 全量 `uv run --with libcst==1.9.0 pytest tests/` 的失败集合 ⊆ §5.6 在 dev 上两次确认的红。
11. 证据目录含：`red-k.txt`（K 组先红）、`red-t11.txt`、`gates.txt`、`e2e-026.txt`、`e2e-027.txt`、`blocked.txt`、`e2e-autoreply-audit.txt`、`dev-reds.txt`、`contract-impact.txt`。

## 8. 依赖与冲突

### 8.1 依赖

- WP5-C pi（第一次验收 REJECTED，返工中）、WP5-C codex（验收中）、WP5-B（规格 wp5b.md；探针先行）、WP3-B（硬切；依赖 WP5-A/C/D、WP6，不依赖 WP5-B）。四者全部合入集成分支后 WP8 才实施。WP5-B 未合入时集成分支不合 dev（裁定 57、66）。
- WP8 的 §3.6 改的是 WP3-B 之后才会红的跑者；若 WP3-B 合入顺序变化，§3.6 仍然必要（CLI 侧 `--summary` 自 WP6 起已必填，现有 E2E 跑者在集成分支上已会在本地报 `SUMMARY_REQUIRED`）。

### 8.2 与裁定或前序规格的冲突（只列，不解）

- X1：`LINK_WINDOW_FULL` 既是 `HOLD_REASONS` 成员（agent 目标的 hold，detail `cwnd`/`W`），又是非 owner `user:` 目标的同步拒绝码（裁定 53）。设计 §9 写「LINK_*_FULL 是 hold 原因，不是失败」。契约必须两用并列写明；是否另立拒绝码未裁。
- X2：wp6 §2.2 的表用 `reason` 键承载 hold 码，裁定 15 与 wp6 评审改为 `holdReason`。WP8 以 `holdReason` 为准；CLI `_lift_hold` 是否仍提升 `reason` 需在 E26-02 实测核对。
- X3：设计 §9 与 wp6 §2.1 写 `link drop` 返回 `drained` 计数；WP2-A 改为 `{state: reclaiming, written: false}`、计数进 `link.reclaimed`（已接受偏差）。
- X4：WP3 §6 要求删除 `contract/pac/context/README.md` 中「PAC 投递 summary 可能为 null」，该句不存在；WP5-D 后 PAC 投递带 summary。
- X5：本地 workflow-request 的 summary 取事件文本，远端取重建后全文（WP5-D 规格 §6 明定透传）。WP8 只在契约里写「不保证跨节点一致」，是否统一待裁。
- X6：wire golden 声明「冻结 TS 参考实现行为」，`autoReply → replyOwed` 改名与新用例背离 TS 源（硬切 R14 的直接结果）。`sources` 字段补设计文档引用；是否另立「hyprial-only」标记未裁。
- X7：设计 §9 列出 `outbox list` 的等待时长，WP6 未做，产品只有 `holdReason`。契约只登记 `holdReason`，等待时长记 UNKNOWN/未交付。
- X8：WP3 §6 说 `ipc/wire.py` 的 summary 映射「WP8 一并做或单开」，裁定 40 归 WP3-B。以裁定为准，WP8 只核对。
- X9：硬切后 agent→agent 发送需要 org 空间，既有 E2E 场景（E2E-001 的 Docker 双机、E2E-013、E2E-014 等）的前置条件随之扩大；这超出「补 `--summary`」，属场景语义变更，WP8 照 §3.6 执行，但 Docker 双机里 org 建立的可行性未实测。
- X10：WP1 规格把 detail 词表固定为 5 个；实现增加 `minting`、`gate-unavailable`、`org-unavailable` 共 8 个（裁定 54 与 WP2-B3 引入）。契约以实现为准。
- X11：任务说明写 `.agents/skills/hyprial-ops/SKILL.md`，实际文件是 `src/hyprial/skills/hyprial-ops/SKILL.md`，`.agents/skills/` 下没有 hyprial-ops。
- X12：裁定 7（PAC 绑定 link 不计 k）、裁定 50（多 org 空间默认镜像）、wp2-composition G7（换 owner 后旧 link 失效）仍待 Allen；契约按现行缺省写，并标「待裁」。
- X13：`contract/e2e/validate.py` 的注释要求「每个新增 ID 同时带基线决定、跑者、文档、门接线」；E2E-027 的受控步骤在实施期大概率 NOT-RUN，与「门接线」的完成度要求如何对齐未定，按 E2E-025 的 NOT-RUN 写法处理。

## 9. UNKNOWN

- 单节点 mint 的放行时延（1 s tick 加 orgfs 本地写回）是否稳定落在 E26-02 的 10 s 内。
- E26-07 实际观察到的是 `LINK_PEER_FULL` 还是 `LINK_MINTING`（裁定 48 的估计依赖 k 与对端常规 link 数）；断言接受两者，证据记实际值。
- E2E-021 回环拓扑能否在 800 行预算内复用于 E27-01/02/04。
- CI 隔离 runner 上是否有 pi/claude/codex 二进制（决定 E27-03 是否必然 BLOCKED）。
- Claude Code 对 channel 推送 turn 是否触发 `UserPromptSubmit`；codex interactive 在 `on-request`（裁定 77 已定）下 TUI 审批提示的实际呈现。
- dev 头当前的红集合（§5.6 的名单来自各验收记录，未在 dev 上复测）。
- 旧版本写入、升级后 `summary: null` 的行在各载体上的显示（WP3 §2.5 规定显示固定提示，未经真实载体验证）。

### 8.3 集成者裁定（2026-10-15，README 裁定 76）

X1 两用保留并列写明；X2 只 `holdReason`；X3 已接受；X4 删除指令作废；X5 契约写「不保证一致」、统一待 Allen；X6 加 `hyprialOnly: true` 与 `sources` 引用，`autoReply → replyOwed` 接受；X7 记未交付；X8 归 WP3-B；X9 E2E-026 为 CI 门、E2E-027 允许 NOT-RUN/BLOCKED、既有场景前置扩成先建 org；X10 以实现为准；X11 路径更正；X12 标「待裁」；X13 按 E2E-025 写法。§3.6 的跑者修复是合 dev 前置，列入 §5。

## 评审（2026-10-15，wp8-review）

已并入正文 v2，见各小节

核对方式：在本 worktree（`wp8/send-links` @ e37fb7987，已并入集成分支最新，含 WP5-C codex）用 grep/实测逐条核 §0 的 48 条、§3.2 机读表、K1–K15 可写性、§2 skill 覆盖、§4 两场景可跑性、§5 清单、§8.2/§8.3 对齐。核对通过的部分：错误码（`kernel/impl/contracts/daemon/ipc_errors.py`）、IPC 方法名（`links/ports.py` IPC_LINK_*/IPC_MESSAGE_*）、hook 事件（`kernel/impl/hooks/events.py` 含 `link.minted`/`link.reclaimed`）、send/reply 返回字段（`holdReason`/`detail`/`evicted`/`openRequestsFrom`、reply hold 八键）、`top` 三字段、`outbox list` 的 `holdReason`、注入契约（`carrier/contract.py` `INJECTION_FORMAT="summary-v1"`、`injection_golden.json` 在场）、PAC summary（`InboxDeliveryIoAdapter.deliver` summary 必填）、保留名文本（`visibility.py:453`）、`worker_channel.METHOD_PARAMS`、`WORKER_HARNESS_TOOLS`/`_CLAUDE_HARNESS_TOOLS` 含 `harness_read_body`、机制应答双前缀、四条 harness.* 日志事件——与 §0 表逐条对上，无漏项（除 B1 的三处形状误差）。7 文件/1000 行门不卡：`tests/tooling/contracts/` 4→5、`tests/shell/skills/` 2→3、新测试文件预算 ≤450 ≤1000；`contract/e2e/validate.py` 的 ID 集合、`[REAL ENV]` 标记与 runner 可执行规则对 E2E-026/027 均可满足，E2E-027 的 NOT-RUN 写法与 `gate-verdict.py`（3=NOT-RUN）及 E2E-025 惯例一致；X1–X13 全部被 §8.3 裁到；§5 清单十二步覆盖 t11、跑者、全量测试（含 GUI/desktop `npm ci`）、PR 正文四段与 trailers、去 WIP、通知链。

### 阻断

**B1｜§3.2/C33：机读登记表三处形状与源码不符，且 `ipc` 的 params/result 无任何 K 测试钉住**

源码实测：`session.turn.ended` 的必填参数是 `reportIds`（1–1000 个非空、各 ≤128 字符的数组），不是 `reportId`；返回键为 `{ok, endedAtMs, turnCount, turnCountSinceMs, lastTurnEndedAtMs}`（`application/actors/sessions/turns.py`）。`link.list` 实际还返回 `orgAvailable`、`held`；`link.stats` 还返回 `tMaxMs`、`periodMs`（`links/service/views.py`，路由层包 `{"ok": True, **mapping}`）。K13 所指 golden 的键名是 `format`（`"format": "summary-v1"`），不是「版本字段」。同时 K4 只钉方法名与四组字段清单，`send-links.json.ipc` 里每个方法的 params/result 数组没有测试对源码——登记错值不会红，违背「双向钉住」。

修正后的规格文字：§3.2 的 json 对应条目改为——

```json
"link.list":  {"params": ["from"], "result": ["ok", "actor", "orgAvailable", "k", "W", "inFlight", "owner", "links", "held"]},
"link.stats": {"params": ["from"], "result": ["ok", "actor", "nActive", "k", "W", "wTarget", "tMinMs", "tMaxMs", "tBarMs", "muMax", "diff", "grad", "periodMs", "links"]},
"session.turn.started": {"params": ["actor", "sessionRef", "reportId", "deliveryId?"], "result": ["ok", "startedAtMs"]},
"session.turn.ended":   {"params": ["actor", "sessionRef", "reportIds", "status?", "deliveryId?"], "result": ["ok", "endedAtMs", "turnCount", "turnCountSinceMs", "lastTurnEndedAtMs"]}
```

C33 中「`session.turn.ended` …返回 `endedAtMs`」改为「入参 `reportIds`（1–1000 个非空、各 ≤128 字符）、可选 `status`（completed/failed）与 `deliveryId`，返回 `{ok, endedAtMs, turnCount, turnCountSinceMs, lastTurnEndedAtMs}`」。K4 追加一句：「对 `ipc` 每个条目，在内存 inbox 夹具上经真实 router 调用一次（照 `tests/daemon/application/transport/channel/summary` 的写法），断言 `result` 键集与实际返回一致、`params` 覆盖 handler 必读键」。K13 的「其版本字段为 `summary-v1`」改为「其 `format` 字段为 `summary-v1`」。

**B2｜E26-02/E26-07：`hyprial log` 有 `link.minted`/`link.reclaimed` 不可实现**

这两个名字是 hook 总线事件：`links/service/core.py:_emit` 经 `HookEventPublisher` 发 `HookBus`，不写 `state/logs/*.jsonl`；bus 只在 lane 溢出时记 `hooks.lane_overflow`，daemon 内也没有订阅这两个事件的 lane（`LINK_WAKE_EVENTS = ("graph.closed", "worker.cleanup.complete")`）。`hyprial log` 只投影持久化 JSONL（`shell/impl/cli/commands/log/history.py`），查不到这两个名字。E2E-026 不允许 BLOCKED/FAIL，这两条断言必红。

修正后的规格文字：E26-02 删去「`hyprial log` 有 `link.minted`」，事件本身改由单元测试钉——§6.2 增一条：在 links service 测试用桩 publisher 断言 `_emit` 以 `EVENT_LINK_MINTED`/`EVENT_LINK_RECLAIMED` 发布且 payload 含 `peer`/`revision`/`side`；E2E 层以「互引文件 + `link list` established」为铸造的可见后果（已断言）。E26-07 删去「`hyprial log` 有 `link.reclaimed`」，以「D 的消息放行 + rC 被排干为 ack」为回收的可见后果。

**B3｜E26-12：isolated 门里 `send --to user:<owner>` 拿不到 `ok:true`**

无 adapter、无 squire profile 的隔离 daemon 上：agent-bot 路径要求 Lark 网关（`agent_bot_attempts._agent_bot_adapter` 只认 lark pins），receiver 路径对无 profile 的 owner 直接拒（`squire/addressing.py:_deliver_unrecorded` → `TARGET_SQUIRE_UNCONFIGURED`）或无回执超时（`USER_DELIVERY_TIMEOUT`）；两条路都 `accepted:false`，send `ok:false`。「只断言 `ok:true` 且无 `holdReason`」在 E2E-026 里必红。

修正后的规格文字：E26-12 改为「owner 通道不受 link 约束（隔离等价断言）：A `send --to user:<owner> --summary …` 的结果无 `holdReason`、无 `detail`、不含任何 `LINK_*` 码；`ok:false` 时其 `code` 属 user 投递拒绝集（`TARGET_SQUIRE_UNCONFIGURED`、`TARGET_SQUIRE_ADAPTER_UNAVAILABLE`、`USER_DELIVERY_TIMEOUT` 等），A 的 `outbox list` 无 `link_hold` 行。owner 计入 W 而 W 满也不挡的语义仍由单元测试钉（C14 不变）」。

**B4｜§5.3 与 §7.8 自相矛盾：残留命中无人认领，门无法转绿**

在本 HEAD 实跑 §5.3 的 grep，命中 7 处 5 文件：§5.3 已列的 `pi_harness_attach.ts:233`、`sessions.py:163`、`capabilities.py:167`、`inbox_surface/send.py:400` 之外，还有 `contract/e2e/run-pi-interactive-attach-manual.py:16,353`（auto-reply 断言与标签，任何规格章节未列）与 `contract/e2e/run-resident-providers.py:353,363,365`（§3.6 覆盖，但 §5.3 基线枚举漏列）。§5.3 只对前两处指派 owner；`capabilities.py:167`、`send.py:400` 两处注释无 owner，而 §7.8 的 src/ 白名单不含这两个文件——按规格执行，§5.3「全部无输出」与 §7.8「超出即越界」互相矛盾。

修正后的规格文字：§5.3 基线枚举改为上列 5 文件 7 处，并追加一句「`daemon/impl/harnesses/capabilities.py:167` 与 `daemon/impl/application/messaging/inbox_surface/send.py:400` 的两处注释由 WP8 删除（仅注释行；先红为 §5.3 grep 的对应行，随证据提交），§7.8 的 src/ 白名单相应加这两个文件」。§3.6 的 autoreply 排查（`wp8/e2e-autoreply-audit.txt`）明确加入 `run-pi-interactive-attach-manual.py`：其 auto-reply 断言与标签改为「无自动回复 + 提醒到达」（同 §3.6 对 run-resident-providers 的后一种写法）。

### 建议

- **A1｜§0/§5.2**：t11 清单在本 HEAD 已过期——实测 18 个未分类文件，比规格基线多 `daemon/impl/harnesses/carrier/codex_carrier_support.py`（WP5-C codex 新增）；§0 第 1 条的「（§5.1）」应为「§5.2」。改法：§5.2 清单补该文件并把「17 个」改「18 个（实施时仍重测）」；§0 引用改 §5.2。同族：C47「约 20 个跑者」与 §3.6 列出的 25 个文件、README 裁定 76 的「约 25」不一致，统一为「约 25」。
- **A2｜§1.2**：「§8 列出的随手项」出处错——S1–S4、F1–F3 在 §5.8，§8 是冲突 X1–X13；且 §5.8 的 S1–S4 与 §6.2 的 skill 测试名 S1–S4 撞名。改法：§1.2 改为「§5.8 列出的随手项」；§5.8 的随手项改称「随手 1–4」或在 §6.2 加一句「§6.2 的 S1–S4 是测试名，与 §5.8 随手项无关」。
- **A3｜§3.3**：§8.3 X6 的 `hyprialOnly: true` 未落进正文——§3.3 只写了 `sources` 补引。补一句：「新增 WG-030–037 与改名 WG-001/002/004/005 的 expectations 条目带 `hyprialOnly: true`」。
- **A4｜§2.1**：行号漂移且自相矛盾——「把第 268–277 行（两条）替换」实际覆盖三个条目：268–271（agent create 条目内的 send/reply/ack 会话围栏三行）、272–275（`hyprial query`，必须保留）、276–277（MCP，第 7 条要求保留）。改法：范围改为「268–271 改写、276–277 就地改写，272–275 不动」。
- **A5｜§2.1**：拒绝码清单缺 `MESSAGE_REPLY_UNAVAILABLE`（C16 明确保留：对非排干、已结算或不存在的请求 reply 时返回）。改法：第 6 条补一行「`MESSAGE_REPLY_UNAVAILABLE`：reply 的原请求已闭合或不存在；ack 你这侧的行，若仍要对话另发新消息（占 W）」。
- **A6｜§6.1 K4/K8**：断言语义需写精确。K4 的 `pendingRowKeys` 是行键全集相等（WP3-B 后恰 10 键），而 `outboxListFields`/`sendResultFields`/`replyHoldFields` 是「关注字段 ⊆ 构造键、按条件出现」（send 结果还有 target/messageId/accepted/code/reason 等键）——两种语义混写为「一致」会让实现者写成全集相等而永红。K8 的「形如 `link.<snake>` 的日志事件字面量」若按全文件字面量收集，会把 `ports.py` 的 hook 事件常量（`link.minted`/`link.reclaimed`）与 IPC 方法名一并收进——须改为「只收集 `_log`/logger 调用的事件名实参，且排除 `hookEvents` 与 IPC 方法名」。
- **A7｜§4.1**：E26-02 的 10 s 与 E26-07 的 30 s 上界已在 §9 列为 UNKNOWN；建议在 26-send-links.md 的 Preconditions 写明预算出处与重跑策略（超时整体重跑一次，不逐断言放宽），避免 CI 慢机上出现 flaky 判 FAIL。
- **A8｜§5.3**：`sessions.py:163` 的 owner 表述含糊（「WP5-C pi 与 WP5-B」未逐文件对应）。按 wp5c.md §2 G5，两处提示词短语（`sessions.py:163`、`pi_harness_attach.ts:233`）的消除都属 WP5-C pi 范围；若其返工分支未覆盖 `sessions.py`，集成者应把它加进 WP5-C pi 返工清单，否则 K10 在 §7.8 白名单外无法转绿且无前置可报。

结论：阻断 4 条（B1–B4），均为规格文字修正，不动 §8.3 任何裁定；按上文修文更新本文件后即可进实施。
