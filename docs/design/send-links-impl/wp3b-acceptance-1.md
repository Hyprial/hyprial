# REJECTED

WP3-B 第一次独立验收。产品硬切通过焦点验证；测试切片 C 丢失正文断言，不能接受。完整测试仍有未完成基线归因的 teardown error，不能记为全绿。

## 1. 验收对象与依据

- 验收分支：`wp3b/send-links`，产品与测试固定在 `782854399`；本次只新增本报告。
- 核心测试提交：`ff5525de1`；核心实现：`e60f60fa6`。
- 三切片：A `e716cae6c`、B `4996929f0`、C `bf428e6e7`。
- 已读：`wp3.md` Stage B、§9 B1–B3；README 裁定 27、40、56、62、76；`docs/evidence/org-structure-constrain-2026-10-14/wp3b/` 的先红、硬切红集、三个切片证据。
- 裁定 66 的前置提交均已在祖先中：WP3-A、WP6、WP5-A、WP5-D、WP5-C 三载体。WP5-B 也已合入，但不是 WP3-B 的前置。
- 测试使用临时 home、socket、数据库及隔离网络。没有操作生产配置、凭据或服务；没有 push、PR 或向其他 agent 发消息。

## 2. 核心行为

### 2.1 PASS：send/reply 校验

独立探针直接构造隔离 `DaemonApplication`，复用 channel support 的 FakeInbox 与已登记会话。对 `message.send`、`message.reply` 分别执行以下 5 项，共 10 项，全部通过：

| summary 输入 | 错误码 | 精确 error.data |
| --- | --- | --- |
| 缺字段 | SUMMARY_REQUIRED | `{field: summary, reason: missing, maxBytes: 512}` |
| 空字符串 | SUMMARY_REQUIRED | `{field: summary, reason: empty, maxBytes: 512}` |
| 整数 123 | SUMMARY_REQUIRED | `{field: summary, reason: not_string, maxBytes: 512}` |
| 孤立代理项 `\ud800` | SUMMARY_REQUIRED | `{field: summary, reason: invalid_utf8, maxBytes: 512}` |
| `摘` 重复 171 次，513 字节 | SUMMARY_REQUIRED | `{summaryBytes: 513, maxBytes: 512}` |

每项同时断言：`submitted == []`、`acks == []`、dispatch gate conversation 集合不变。超长错误没有混入 `field`、`reason` 或 `bytes`。现有测试另覆盖 512 字节通过、认证错误优先、`replyTo`、重放 digest 及原请求保持 pending。

### 2.2 PASS：pending 与 read_body

独立执行 summary 测试及 wire adapter 测试：

```
PYTHONPATH=$PWD/src uv run --extra test pytest tests/tooling/contracts/test_wire_contract_adapter.py tests/daemon/application/transport/channel/summary tests/daemon/inbox/summary -q -n 4
85 passed, 10 warnings in 7.42s
```

已核对的断言：

- pending 行键恰为 `messageId/deliveryId/conversationId/from/to/intent/kind/summary/bodyBytes/origin`，普通及 fetched 读形状一致。
- 旧行 summary 为 null；正文只能经 read_body 读取。
- `BODY-SENTINEL` 不通过 message/payload 键泄漏；summary 可以合法地与正文同值。
- read_body 正文及 UTF-8 字节数正确；单条 fetched 盖章不影响同收件人的其他行，重复读取保持首次时间戳。
- read_body 的身份、会话 fence、保留行和未知 id 行为保持原断言。
- notice 的 summary 是全文、bodyBytes 为 0；notice 的 read_body 返回 MESSAGE_BODY_UNAVAILABLE。
- settle_hint 保持只读；pending 的非 request 行按裁定 56 返回 ack。

### 2.3 PASS：wire 加性映射；BLOCKED：所称 59 条 golden 的数量口径

独立调用 `wire._message`，分别传缺字段、null、整数、数组和字符串。前四项得到 `summary is None`；字符串原样映射。每项同时断言 payload 仍等于原信封的确定性 JSON 编码。没有把 summary 校验错误施加到旧 golden 信封。

```
PYTHONPATH=$PWD/src uv run --extra test python contract/wire/golden/run-python.py
RESULT GREEN 27/27
uv run --extra test python contract/wire/golden/validate.py
RESULT PASS 21 envelope fixtures, 27 expectation cases, 30 coverage tags
```

`expectations.json` 当前只有 27 条期望；完整枚举后也是 27 条。不能把它写成“59 条 golden 已通过”。若 59 指另一组测试，须给出路径或 ID 清单。现有 corpus 全部通过；指定数量的验收仍 BLOCKED。

另跑 `tests/tooling/contracts -q -n 6`：72 passed，39.44s。该数量是 pytest 测试数，不是 golden case 数。`git diff --exit-code 0b0de0072 HEAD -- contract` 退出 0。

### 2.4 PASS：loud 通知

`test_the_two_loud_notices_carry_a_derived_summary` 和 `test_the_human_loud_path_delivers_the_notice_text_unchanged` 均通过。核对 unavailable/no-progress 的提交对象：summary 等于 `summary_from_text(text)`，非空且不超过 512 字节；nonce 留在 payload，派生文本不受影响。人达路径仍传原 notice 对象及原文本。

B3 的实现把派生放在 `availability_loud._message` 内，因此 `runtime/forwards.py` 的三个既有调用点不需要新增参数。行为与 B3 目标一致，接受此实现偏差。

## 3. PASS：独立复现先红

指定 scratchpad 为 `/private/tmp/claude-501/-Users-bobadmin-Workspace-HyprialOS-hq-harness-bridge/87d11f16-52cb-42cd-8e0d-2101ff5c5d66/scratchpad`。在该目录中建立 detached worktree：

```
git worktree add --detach <scratchpad>/wp3b-first-accept-red 0b0de0072
git -C <scratchpad>/wp3b-first-accept-red cherry-pick --no-commit ff5525de1
# 在该 worktree 内：
PYTHONPATH=$PWD/src uv run --extra test pytest tests/daemon/application/transport/channel/summary/test_send_summary.py tests/daemon/application/transport/channel/summary/test_pending_shape.py tests/daemon/application/transport/channel/summary/test_call_paths.py -q -n 4
```

结果：6 failed、20 passed，10.05s；失败 ID 与仓库先红记录完全一致：

1. `test_a_send_without_a_summary_is_refused_before_any_side_effect`：DID NOT RAISE。
2. `test_a_reply_without_a_summary_is_refused_and_leaves_the_request_pending`：DID NOT RAISE。
3. `test_the_row_carries_the_summary_and_body_size_without_the_body`：多出 message 键。
4. `test_a_pre_summary_row_reads_as_null_summary`：多出 message 键。
5. `test_the_two_loud_notices_carry_a_derived_summary`：summary 为 None。
6. `test_the_human_loud_path_delivers_the_notice_text_unchanged`：summary 为 None。

该轮 uv 选择 Python 3.14.2；后续主套件为 Python 3.12.13。未以解释器差异掩盖失败。用后已强制删除 worktree；没有将测试补丁应用到当前工作树。

## 4. 三切片审计

已逐个读取三个提交的全部测试 diff：A 8 文件、B 22 文件、C 9 文件，均满足每切片至少 8 文件的要求。审计范围限定到切片提交，未把合入的 WP5-B 测试误算成 WP3-B 修复。

### 4.1 PASS：切片 A，8 文件

审查文件（前缀 `tests/daemon/`）：

- `application/messaging/test_open_requests_from.py`
- `application/ports/test_daemon_capability_wiring.py`
- `application/ports/test_daemon_visibility_wiring.py`
- `application/transport/channel/delivery/test_delivery.py`
- `application/transport/channel/routing/test_routing.py`
- `application/transport/channel/sessions/test_sessions.py`
- `dispatch/without/test_dispatch_without_pac.py`
- `inbox/idempotency/test_agent_send_idempotency.py`

先红记录：9 个红文件 51 failed、58 passed；同组修复后 109 passed。第 9 个文件 dispatch gate 复用发送 helper，不需要修改本身。

断言审计：notice 从 message 改读 summary，并增加 kind/bodyBytes；sessions 的字典仍精确相等，正文断言迁到 read_body。没有把精确相等改成包含，没有放宽错误码、ack 顺序、重放或副作用断言。

### 4.2 PASS：切片 B，22 文件

审查全部 22 文件；下列 10 个是明确复核点（前缀 `tests/daemon/`）：

- `channels/mcp/contracts/test_mcp_daemon_alignment.py`
- `channels/mcp/lifecycle/test_mcp_channel_owner_migration_e2e.py`
- `delivery/identity/test_sender_identity.py`
- `delivery/message/test_authenticated_message_sender.py`
- `delivery/origin/test_inbound_origin.py`
- `delivery/sender/test_visibility_sender_canonical.py`
- `harnesses/carrier/turns/test_turn_reports.py`
- `harnesses/pi/headless/test_pi_headless_worker_fetch_e2e.py`
- `identity/aliases/test_alias_input_surfaces.py`
- `links/service/test_wiring.py`

其余 12 文件涵盖 path_authz、routing、squire、local-first 与 worker relay，均只补发送参数。

先红记录：180 failed、5749 passed；修复后全切片 4 failed、5925 passed，测试总数不变。记录中的剩余项明确列为 pi SDK pin 及 3 条时序失败，没有伪报全绿。

断言审计：origin 的 `hi`、`legacy` 正文精确断言迁到 read_body；调用方同步解包 `(row, body)`。保留 TARGET_IS_NODE、AMBIGUOUS_TARGET、INVALID_ARGUMENT、LINK_RECLAIMED 等原码与精确 data；没有把错误码断言改成集合放行。

### 4.3 FAIL：切片 C，9 文件

审查文件：

- `tests/integration/actor_preservation/delivery/channel/test_registration.py`
- `tests/integration/actor_preservation/delivery/channel/test_reply_settlement.py`
- `tests/integration/identity/contracts/test_identity_contract.py`
- `tests/kernel/actors/identity/support.py`
- `tests/kernel/actors/identity/test_mailboxes.py`
- `tests/kernel/actors/identity/test_resolution.py`
- `tests/kernel/actors/identity/test_wire_delivery.py`
- `tests/shell/compatibility/test_target_kind_honesty.py`
- `tests/tooling/architecture/identity/test_sender_identity_gate.py`

先红记录：73 failed、2418 passed，含 69 条 cut 类和 4 条环境/时序类；改动文件焦点 152 passed。两条 PAC contract 黑盒红明确保留为 WP8 BLOCKED，符合裁定 76。

阻断：`test_registration.py::test_channel_session_registration_and_pending_read_are_daemon_authoritative` 删除期望字典中的 `"message": "durable body"`，但没有新增 read_body 正文断言。summary=None 和 bodyBytes=12 不能证明正文仍为 `durable body`，同字节长度的错误正文也能通过。切片证据所称“既有断言本就不读正文”不成立。A 中同名测试已正确补 read_body，不能替代此 integration 测试的断言。

其他 8 文件未见断言削弱；notice 改读 summary 符合全文通知规则。

## 5. PASS：产品发送方与 pending 读者

已扫描全部 `src/hyprial` 的发送调用及 pending.list/wait 调用，结合 AST 枚举和逐点读码核对：

- CLI send/reply 在 IPC 前用 `_require_summary` 校验，参数带 summary。
- CLI workflow 通知透传规划方 summary；orgfs 邀请用 helper；Lark 授权交接用固定摘要。
- MCP server、channel adapter、pi bridge 的 send/reply 工具参数均带 summary。
- Lark daemon_link 从人写原文派生；user-proxy 转递在拼好归属行后派生；forward 优先保留原摘要，无摘要才派生。
- PAC/routine/remote notification 经 InboxIo.deliver，必填 summary 并在写入前校验。
- hook 与 python_worker 的机制应答带派生 summary；没有恢复模型自动回复。hook 请求原文豁免按裁定 58，python_worker 原文按裁定 59/60，不误判为缺摘要的模型发送。
- 两条 loud durable 通知已修复。system notice 的摘要在 pending 投影中取全文；progress 走独立进度面，不误算为 agent 新请求。
- codex 经 `injection_from_row`，pi attach 仅取 kind/summary/bodyBytes 等投影字段；claude/MCP 读工具转发新行形状。
- CLI read 走 read_body；message.query 是操作员视图。squire 私有 ledger 的 message、本地 carrier journal 的 message 均不是 pending 行正文。

未发现仍读取旧 pending 行 message 的产品消费者；遗留 contract/e2e 读者归 WP8。

## 6. 完整测试：FAIL / BLOCKED，不能记为全绿

安装 GUI/desktop 依赖时显式使用 Node 24.21.0，两个 `npm ci --ignore-scripts` 成功。首轮并行测试受代理及负载影响，未用其结果声称产品回归。随后取消大小写 HTTP_PROXY/HTTPS_PROXY/ALL_PROXY，设置 NO_PROXY/no_proxy=127.0.0.1,localhost，保留 Node 24；两组串行执行，各组内部使用 xdist：

```
PYTHONPATH=$PWD/src uv run --with libcst==1.9.0 --extra test pytest tests/daemon tests/kernel tests/tooling -q -n 6
3 failed, 8579 passed, 5 skipped, 7 xfailed, 1 xpassed, 1 error in 328.89s

PYTHONPATH=$PWD/src uv run --with libcst==1.9.0 --extra test pytest tests/shell tests/integration -q -n 6
27 failed, 3662 passed, 7 skipped in 225.76s
```

### 6.1 已知红或已接受窗口

- daemon：pi controlled_loader 的版本 pin，1 条。
- kernel：POSIX listener 的 AF_UNIX 路径过长，1 条。
- shell：login/device t11，1 条；squire/setup 的 machine/node 不匹配，20 条。
- integration：native_runtime_entries 的 turn_pulse import，1 条；provider_failure_settlement 的 transient-restart-1 未 ack，1 条。两者在切片 C 已有基线证据。
- integration：pac_events_blackbox 两条，分别为 public consumer snapshot/follow、first-wave B1/B2 runner；仍为 BLOCKED，归 WP8 的 contract 修复窗口。
- 本轮 GUI lifecycle、CLI inventory、pr_hygiene_git 没有失败。5/7 个 skipped 不当作已验证结论；保留 1 xpassed 原样报告。

### 6.2 独立补做的基线归因

在同一 scratchpad 再建干净 `0b0de0072` detached worktree，用 uv 选择的 Python 3.12.12 及相同去代理环境执行以下精确测试；主完整组为 3.12.13，未隐去补丁版本差异。用后已删除：

- `tests/daemon/ipc/cost/test_ipc_cost_counters.py::test_four_sides_cover_a_pure_heartbeat_load`
- `tests/integration/hosted/readiness/test_hosted_start_probe.py::test_probe_wrapper_times_a_slow_underlying_read`
- `tests/integration/pac/expansion/test_effect_races.py::test_preflight_git_budget_finishes_before_caller_timeout`
- `tests/daemon/sessions/actors/test_session_persistence_overload.py::test_deferred_completion_survives_generation_and_cannot_restore_old_route`（两个参数）

命令为 `uv run --python 3.12 --with libcst==1.9.0 --extra test pytest <上述节点> -q -n 0`。结果 3 failed、2 passed，6.87s。

前三项与验收头失败一致：CPU 比值基线 8.673/14.169，小于 0.70；slow read 为 1.350592 秒，超过 1.2±0.15；PAC preflight 抛 PacGraphTimeout，未得到预期 bounded git failure。归为同机既有环境/时序红，不修改阈值、不删断言。这个结论来自本次独立基线运行。

### 6.3 BLOCKED：session persistence teardown error

完整 daemon 组在 `[False]` 参数 teardown 泄漏 3 个 fd；缩小到 89 项串行重跑时，变为 `[True]` 参数泄漏同样 3 个 fd。该次缩组结果为 4 failed、84 passed、1 error，49.05s；4 个失败是 CPU、slow read、PAC preflight 和已知 provider settlement。

再仅跑两个 persistence 参数：验收头 2 passed，0.66s；干净基线也 2 passed。`session_actor/`、该测试文件、`tests/conftest.py` 相对基线无差异，但这些事实不足以证明完整组中的 fd error 是既有红。完整组的失败差集尚未闭合，保持 BLOCKED，不接受“孤立重跑绿即全量绿”的替代。

## 7. PASS：结构、边界、lint 与合并树

- `uv run --no-project --python 3.12 python scripts/ci/check_structural_limits.py`：退出 0，violations=[]。
- `uv run --no-project --python 3.12 python scripts/ci/check_module_boundaries.py`：退出 0，violations=[]。
- `uv run ruff check .`：All checks passed。
- 焦点组（inbox summary、channel summary、links、ipc、authority、migration、contracts）：775 passed，50.51s。
- inbox authority exemptions 相对 `0b0de0072` 无差异。
- `git diff --check`、`git diff --check $(git merge-base HEAD refactor/org-structure-constrain) HEAD`、`git diff --check 0b0de0072 HEAD -- src tests`：均退出 0。
- 扩大到 `60179f3e1..HEAD` 的全部文件时，发现 WP5-B 的历史 `probe.py.txt:27` trailing whitespace。该文件已在共同祖先中，不属于 WP3-B 分支差异；未改写历史证据，也不把它误列为本包新增问题。
- `git merge-tree --write-tree HEAD refactor/org-structure-constrain`：退出 0，无冲突；集成头为 `3b88bd81b7ac8096162c9001b7d52a2c13ac17ca`，结果树 `deda9b0183356c65f3e13ba3997c85d8c4c4eaac`。这是静态合并检查，不替代合并后完整测试。

## 8. 编号返工项与合并待办

1. R1，FAIL，必须修复：在切片 C 的 registration integration 测试中保留新 pending 精确形状，并经 message.read_body 精确断言正文 `durable body`。不得仅保留字节数。用修改正文但保持字节数的负控证明断言有效；补充更正证据，不改写原 slice-c.txt。
2. R2，BLOCKED，必须补证：对 session persistence 的 3-fd teardown error 做同机同完整组的基线比较或定位修复。记录失败 ID、运行环境、顺序及通过结果；不能扩大环境红白名单，也不能删除 fd hygiene 门。
3. R3，BLOCKED，必须澄清：提供“59 条 golden”的路径/ID 或纠正验收数量。现仓 wire corpus 的 27 条已全部通过，不能虚报另 32 条。

修复后再独立验收；本次不合并。集成者合入前须重跑合并树门禁与两组完整测试。WP8 contract 窗口可保留到其批次，但合 dev 前必须关闭。基线实测新增确认的 CPU/slow-read/preflight 红只按上述证据归因，不据此豁免其他失败。

## 9. WP8 Contract-Impact 与 Skill-Impact

Contract-Impact: 本次报告不改变产品契约。WP3-B 改变 send/reply 缺 summary 的拒绝行为及 pending 行去正文；contract/ 按裁定 40/76 延后到 WP8，wire 的 src 映射已由 WP3-B 完成，不再延后。

WP8 合 dev 前清单：

- golden 新增/更新 summary、缺摘要、513 字节精确错误 data、pending 无正文、read_body fetched、settle_hint；hyprialOnly 标记按裁定 76 X6。
- 同步 CLI/MCP/worker 的必填 summary、正文读取工具、MESSAGE_BODY_UNAVAILABLE 与 SUMMARY_REQUIRED 形状。
- 修复 wp3.md §9 A2 的 24 个发送侧、13 个读侧跑者/helper；关闭上述两条 PAC 黑盒红。
- 落实 E2E-026 isolated CI 门；E2E-027 允许明确 NOT-RUN/BLOCKED，不冒充实测。
- 文档说明旧行 null summary、notice 全文摘要、PAC 必有摘要及机制应答豁免。

Skill-Impact: 本次报告无操作方式变化；WP8 更新 `src/hyprial/skills/hyprial-ops/SKILL.md` 的发送/回复摘要、read_body、轮末不代答等说明。

## 10. 证据留存说明

本报告已记录决定结论的命令、提交、断言差异、错误形状及数量。既有先红与切片原始证据仍在仓库中。独立运行的临时原始日志位于 `/private/tmp/wp3b-accept-logs/`，仅作辅助：`red-reproduced.txt`、`probes.txt`、`summary-wire.txt`、`golden-runner.txt`、`contracts72.txt`、`focus.txt`、两组 `*-isolated.txt`、`flake-recheck.txt`、`baseline-recheck.txt`、`fd-isolated.txt`。结论不依赖这些临时日志继续存在。
