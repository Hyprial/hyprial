ACCEPTED

# 常驻 Pi provider 三次验收（2026-10-17）

429 只终止当前消息，不再永久阻塞 agent。
本次未发现新增回归；全量仍为 FAIL。
4 条全量失败均在 62465dbec 重现 2 次。
本判词不表示全量门禁或生产环境验收通过。

验收对象：fix/resident-pi-provider-credentials，635eb6e59。
增量基点：1e64d5bc7；修复提交：dcb24aee6。
前次判词：docs/design/resident-pi-provider-credentials-acceptance-2-2026-10-16.md。
本轮只裁定 429 非阻塞语义，并复核前次已通过项。

## 1. 429 当前消息终态与后续投递

结论：PASS，Pi 1.2.0、0.85.1 的组合探针读数相同。

亲自检查并运行遗留 wire 探针。
组合使用真实 Pi SDK、controlled loader、PiRpcClient、PiRpcProcess、
TurnRuntime、DaemonEventBridge，以及真实 SQLite inbox 和 agent registry。
假 provider 仅在 loopback 返回第 1 次 HTTP 429；第 2 次返回成功响应。
凭据仅为仓内假 key。

第 1 条消息读数为：

    failureCode=PROVIDER_RATE_LIMITED
    calls=1
    dispatches=1
    attempts=1
    attemptRows=1
    notifications=1
    terminal=true
    permanent=true
    agentBlocks=[]
    isBlocked=false
    ownerNotices=0

时钟推进 30 分钟，再运行 10 次 timer 后，以上读数不变。
失败通知包含新码。
随后同一 agent 的第 2 条消息得到投递：

    nextDelivered=true
    sameAgent=true
    totalCalls=2
    dispatches=2
    agentBlocks=[]
    processStopped=true

通知计数指发给等待方的 provider-unavailable 通知。
429 没有产生 agent blocked 的 owner 通知。
第 2 条消息证明可继续投递并调用假 provider；未将该读数宣称为完整业务回执验收。

组合边界：transport 和 harness farm 使用仓内测试实现。
farm 的 dispatch 实际调用 process.enqueue。
探针只把 launch payload 的 baseUrl 指向假 provider。
错误分类、重试、settlement 和通知实现均未替换。
本组合不等于完整 CLI/daemon 网络端到端验收。

## 2. quota 词与阻塞行为

结论：PASS，dev 既有 quota 分类及阻塞语义未变。

亲自读取 62465dbec 的分类器，并复核当前分类器。
以下 6 个既有词仍判为 PROVIDER_USAGE_LIMIT：

    usage limit
    usage_limit
    quota exceeded
    quota exhausted
    insufficient_quota
    exceeded your current quota

每个词与 HTTP 429 混合后，仍优先判为 PROVIDER_USAGE_LIMIT。
以下 5 个限流词单独出现时，均判为 PROVIDER_RATE_LIMITED：

    429
    too many requests
    rate limit
    rate_limit
    resource_exhausted

消费者探针同时验证了大小写归一化。
另增加 quota 正例探针，使用真实 registry、inbox 与测试 harness。
输入为 HTTP 429: insufficient_quota; quota exhausted。
当前树和 detached 62465dbec 均得到：

    failureCode=PROVIDER_USAGE_LIMIT
    attempts=1
    terminal=true
    quotaBlockPersisted=true
    ownerNotices=1
    nextHeld=true
    explicitUnblockReleases=true

探针直接检查 agent_blocks 中的 provider-quota 行。
另开 registry 连接后，阻塞状态仍存在。
第 2 条消息在阻塞时未投递，显式 unblock 后才投递。

## 3. 消费者与文档复核

结论：PASS。已亲自 grep 源码并逐处读取，未仅引用返工 notes。

- daemon/impl/api.py：新码在 permanent 集合内；quota 分支先于限流分支。
- daemon/impl/runtime/results.py：结算前与 legacy observer 两处共用闭集。
  _BLOCKING_FAILURE_CODES 仅含 usage 和 authentication 两码。
  新码正常结算，但不调用阻塞 observer。
- identity/impl/agents/state/worker_state.py：_reason 闭集不含新码。
  submit 对新码直接接受，不进入 block 或 notify。
  消费者探针用会抛错的 block/notify 回调验证了此路径。
- daemon/impl/application/messaging/inbox_surface/queries.py：
  同步 legacy 映射也不含新码，访问 agent 依赖前即返回。
- biz/impl/hooks/quota/consumer.py：只接受 PROVIDER_USAGE_LIMIT。
  新码不会触发额度耗尽告警；探针保留 usage 正例。
- biz/impl/hooks/quota/policy.py：接收 consumer 已筛选的 actor。
  policy 不把其他失败码重新解释为额度耗尽。
- daemon/impl/correlation/availability_loud.py：通知原样携带 failure_code。
  wire 探针已验证新码通知与单次计数。
- runtime/forwards.py、turn_delivery/turn/turn_runtime.py、
  harnesses/streaming/process.py、harnesses/python_worker/__init__.py：
  分类结果仅用于转交、结算或日志，没有 usage 专用分支遗漏新码。
- turn_runtime 的 terminal marker 集合未变，限流仍在 turn 内首错终止。
  harness_failure_is_permanent 的实际消费点仍为 results.py 的结算。

另查 GUI、desktop 与 contract，未发现这两个失败码的专用消费者。
docs/reference/turn-hooks.md 已同步两码区别：
限流只终止当前消息；usage 和 authentication 才阻塞 agent。

## 4. 先红早于修复

结论：满足。旧断言更新也在修复前单独提交。

提交顺序为：

    0d3f8f743  新增 429 非阻塞行为测试
    5bf18583a  更新结算与通知的旧断言
    142bafe92  更新 turn 失败交接的旧断言
    dcb24aee6  产品修复及文档
    635eb6e59  返工读数

已检查 3 个红提交都只有测试变更。
对 3 个红提交分别执行 merge-base --is-ancestor，均返回 0。

另在 detached 142bafe92 上独立复跑新行为与旧断言：

    6 failed, 25 passed, 0.94s

6 条失败均为实际得到 PROVIDER_USAGE_LIMIT、期望新码。
其中包含真实 registry/inbox 的新行为测试。
未把导入错误或清理失败作为有效反例。
同一选择在当前树为 31 passed，0.72 秒。

## 5. R1、R2 与已通过项

结论：PASS，限本次隔离测试组合。
R1 指有效配置默认 provider 共用凭据拒启检查。
R2 指 worker 存活期间可查询重绑告警。

两个 Pi 版本均亲自复跑 rework2 探针：

    fresh + no grant: refused=true, ready=false
    stale zai session + no grant: refused=true, ready=false
    stale zai session + grant: ready=true, running=true
    liveLogWarning=true
    logHasNoCredential=true
    builtin openai default: delta={}

拒启发生于 PiRpcProcess 构造阶段。
授权后的旧会话在进程存活时记录 session.provider.rebound。
日志字段为 zai-coding-cn/glm-4.6 到 deepseek/deepseek-flash。
探针只查询启动状态，没有调用真实模型。

两个版本的焦点集还覆盖显式选择、配置默认值、grant、native auth、
会话实际重绑、匹配会话不改写、分片告警、凭据失效及显式 unblock。
交互式启动与 PAC 测试也包含在焦点集中。
PAC 指声明式派单与跟踪层。

## 6. 独立门禁与全量

环境：macOS 26.5.2 arm64、CPython 3.12.13、Node 24.21.0。
主运行使用 Pi 1.2.0；另用隔离安装的 Pi 0.85.1。
未修改全局 Pi 安装。

亲自运行的门禁结果：

- GUI 前置 npm ci：PASS，12 packages，0 vulnerabilities。
- 结构门：FAIL，exit 1，仅 2 项约定不计的既有违规。
  registry_ops.py 为 1005 行；tests/daemon/outbox 为 8 个直接文件。
  detached 62465dbec 也仅有这 2 项，没有新增违规。
- 模块边界门：PASS，violations=[]。
- ruff：PASS，All checks passed；新增探针后已复跑。
- Pi 1.2.0 焦点集：1 failed、1910 passed、1 skipped，377.42 秒。
  唯一失败是 SDK 版本断言 0.85.1，实装 1.2.0。
- Pi 0.85.1 同一焦点集：1911 passed、1 skipped，375.96 秒。
- 隔离 contract corpus：PASS，exit 0。

全量：FAIL，exit 1。
开始：2026-10-17T02:19:37Z；结束：2026-10-17T03:03:17Z。
全量期间没有并发运行其他测试或探针。

    4 failed, 12955 passed, 14 skipped, 5 xfailed, 1 xpassed
    2607.86s (0:43:27)

4 条失败的确切测试如下：

1. tests/daemon/harnesses/pi/configuration/test_pi_controlled_loader.py::test_t04_t05_t06_real_sdk_loads_c_and_trusted_project_with_git_boundary
   版本断言：assert '1.2.0' == '0.85.1'。
2. tests/daemon/ipc/cost/test_ipc_cost_counters.py::test_four_sides_cover_a_pure_heartbeat_load
   当前全量为 11.772/20.643，低于 0.70。
3. tests/kernel/platform/test_platform_actor_preservation.py::test_posix_listener_keeps_request_owner_and_reserve_fd
   原文：OSError: AF_UNIX path too long。
4. tests/tooling/architecture/authority/test_inbox_authority_gate.py::test_current_raw_bypasses_are_exactly_in_the_shrinking_manifest
   原文：unmanifested inbox bypass: src/hyprial/daemon/impl/runtime/forwards.py:_BridgeForwardsMixin._log_missing_failure_route:alarm_emitter。

已按对照、当前、对照、当前的顺序独立复跑以上 4 条。
4 次均为同样的 4 failed，耗时依次为 7.86、7.68、7.44、7.99 秒。
没有将已重现的基线失败改记为门禁通过。
skip、xfail、xpass 均不算真实环境验收通过。

## 7. 复跑与证据

全量命令与任务要求一致：

    env -u HYPRIAL_NODE_ID -u HYPRIAL_MANAGED_WORKER \
      -u HYPRIAL_WORKER_ACTOR -u HYPRIAL_WORKER_SESSION_REF \
      -u HYPRIAL_TOOL_PROFILE_ID \
      -u HTTP_PROXY -u HTTPS_PROXY -u ALL_PROXY \
      -u http_proxy -u https_proxy -u all_proxy \
      -u GIT_CONFIG_GLOBAL -u GIT_CONFIG_NOSYSTEM \
      NO_PROXY=127.0.0.1,localhost no_proxy=127.0.0.1,localhost \
      PATH=/Users/bobadmin/.npm/_npx/387698761821791d/node_modules/node/bin:$PATH \
      uv run --with libcst==1.9.0 --with pytest-timeout \
      pytest tests/ -q --timeout 600

以下命令也使用相同的身份及代理清理环境：

    uv run --no-project --python 3.12 python scripts/ci/check_structural_limits.py
    uv run --no-project --python 3.12 python scripts/ci/check_module_boundaries.py
    uv run ruff check .
    npm --prefix src/gui ci --ignore-scripts
    uv run --with pytest-timeout pytest tests/daemon/harnesses tests/daemon/delivery tests/identity/providers tests/integration/actor_preservation/delivery tests/daemon/pac tests/shell/cli/agents/test_cli_pi_interactive.py -q --timeout 600
    uv run sh contract/run-all.sh --isolated-only

探针设置 PYTHONPATH 为仓库根与 src，通过 uv run python 执行：

    docs/notes/resident-pi-provider-credentials-acceptance-3-wire-2026-10-17.py
    docs/notes/resident-pi-provider-credentials-acceptance-3-consumers-2026-10-17.py
    docs/notes/resident-pi-provider-credentials-acceptance-3-quota-2026-10-17.py
    docs/notes/resident-pi-provider-credentials-rework2-probe-2026-10-16.py

0.85.1 的重复运行仅在清理后的 PATH 前加入：

    /tmp/pi-accept-sdk085/node_modules/.bin

红提交与基线分别使用本轮新建的 detached worktree。
运行时 PYTHONPATH 指向各自 src 与仓库根。
uv run --project 指向当前树，仅复用锁定依赖。
基线 quota 探针输出的模块路径也已留档。

原始输出与执行脚本归档：

    docs/notes/resident-pi-provider-credentials-acceptance-3-readings-2026-10-17.txt

归档包含全量、焦点、红绿、4 次对照、探针、结构门和 contract 输出。
每份原始输出均小于 1 MB。
6 MB 的模块边界原始 JSON 未入库；归档保留结果、字节数与摘要。
旧 notes 保持原字节；2 个遗留未跟踪探针均经亲自检查和运行后提交。
原始 pytest 输出中的行尾空白保留不改。

## 8. 范围与清理

本轮仅新增验收报告、探针与读数，没有修改产品代码。
两个验收 detached worktree 已删除，执行脚本已退出。
wire 探针均断言 worker 停止，authority 与数据库均已关闭。
测试状态位于隔离临时目录，provider 仅监听 loopback。
未读取或改写真实凭据、用户级配置及生产 daemon 生命周期状态。
未调用真实 provider；未 push、未开 PR、未向 agent 发消息。

Pi 0.85.1 的完整 tests/、生产 daemon、真实 Lark、真实配额恢复均为 UNKNOWN。
本次 ACCEPTED 仅表示 429 非阻塞目标已满足，前次通过项未见退化。

Contract-Impact：本提交只增加验收材料，不改变产品契约。
分支新增失败码的跨实现语料或 PR 放行理由仍由集成方按 AGENTS.md 处理。
隔离 contract corpus 通过不表示其他实现已支持新码。
Skill-Impact：本提交不改变 agent 操作方式，不修改 skill。
