ACCEPTED

# 常驻 Pi provider 二次验收（2026-10-16）

验收对象：fix/resident-pi-provider-credentials，02a4a7bd5。
首验判词：2c611a5ed；失败对照：62465dbec。
本次只裁定首验 R1、R2，并检查已通过项是否退化。
R1、R2 均已修复；未发现本轮新增回归。
全量测试仍为 FAIL，4 条失败均在对照提交重现。
本结论不表示全量门禁通过，也不表示生产环境验收通过。

## 1. R1：配置默认值与显式选择共用凭据检查

结论：PASS。

代码检查确认了同一条检查路径：
- pi_provider_environment 先调用 pi_effective_model_selection。
- 显式 provider 优先；未指定时读取配置投影的 settings.json。
- 可信项目的 .pi/settings.json 覆盖配置投影的默认值。
- 有效 provider 为 deepseek 时，共用 grant、native auth 和拒启分支。
- PiRpcProcess 与交互式 Pi 启动均传入 settings_root。

没有增加 operator 凭据回退，也没有另建 PAC 凭据来源。
这里的 PAC 指声明式派单与跟踪层。

亲自复跑首验 boundaries 脚本，Pi 1.2.0、0.85.1 均退出 1。
脚本在第 1 个无授权场景即抛出 AgentProviderCredentialMissing。
异常发生于 PiRpcProcess 构造阶段，早于 worker 就绪。
旧脚本未捕获拒启异常，因此该退出码符合本轮预期。
拒绝文字包含以下操作：

    hyprial agent secret grant <agent> ... --environment-name DEEPSEEK_API_KEY

另复跑返工 probe，两个版本的读数相同：

    fresh + no grant: refused=true, ready=false
    stale zai session + no grant: refused=true, ready=false
    stale zai session + grant: refused=false, ready=true, running=true
    builtin openai default: delta={}

显式选择、配置默认选择、grant 优先、native auth、内置厂商负例，
均由本次两个版本的焦点集覆盖。
测试使用仓内假 key；未读取真实凭据。

## 2. R2：存活期间可查询重绑告警

结论：PASS。

PiRpcClient 使用 PiStderrTail 按完整行识别 loader 告警。
已识别事件立即写入 worker 日志，不再等待进程退出。
转发只复制 sessionId、旧 provider/model、新 provider/model。
分片读取的告警测试在两个版本均通过。

返工 probe 在 carrier.running=true 时读取日志。
两个版本均为 liveLogWarning=true、logHasNoCredential=true。
实际日志中的命名字段如下：

    level=warn
    event=session.provider.rebound
    sessionId=rework2-live
    recordedProvider=zai-coding-cn
    recordedModel=glm-4.6
    configuredProvider=deepseek
    configuredModel=deepseek-flash

还单独调用了首验 boundaries 脚本的 run(True, True)。
这是授权后的旧会话场景；保留原来的 get_state 调用与清理。
Pi 1.2.0、0.85.1 均读到：

    ready=true
    modelProvider=deepseek
    modelId=deepseek-flash
    hasGrantedKey=true
    stderrWarning=true
    liveLogWarning=true
    logWarningAfterStop=true

启动探针只发 get_state，不发 prompt。
旧会话实际重绑、匹配会话不重复改写的测试均未退化。

## 3. 已通过项的回归检查

结论：PASS，限本次隔离组合与焦点测试范围。

亲自复跑首验 wire 脚本，两个 Pi 版本均退出 0：

    calls=1
    dispatches=1
    attempts=1
    terminal=true
    failureCode=PROVIDER_USAGE_LIMIT
    notifications=1
    processStopped=true

该脚本组合真实 Pi SDK、controlled loader、PiRpcClient、
PiRpcProcess、TurnRuntime 和 DaemonEventBridge。
HTTP 429 由仓内假 provider 在 loopback 返回。
时钟再推进 30 分钟、运行 10 次 timer 后，仍只有 1 次调用和通知。

组合边界与首验相同：InboxPort、transport 使用仓内测试实现。
调度接口实际调用 process.enqueue。
探针仅将 launch payload 中的 baseUrl 改为假 provider 地址。
错误分类、重试决策、settlement 与通知实现均未替换。
这不是完整 CLI/daemon 网络端到端验收。

焦点集还覆盖凭据失效、显式 unblock、瞬态错误预算、交互式启动，
以及 PAC（workflow、routine）测试；两个版本均未出现新增失败。

## 4. 先红早于修复

结论：满足。未用修复后的断言充当先红证据。

提交顺序为：

    2c611a5ed  首验拒绝
    5ba1ed317  R1/R2 红测试
    afcc60cce  R1/R2 修复
    e0b7a7954  门禁分类
    02a4a7bd5  返工读数

git merge-base --is-ancestor 5ba1ed317 afcc60cce 返回 0。
红提交只新增测试；本轮未在修复提交中更新旧断言。
分片读取测试随修复新增，仅作为补充，不算先红证据。
首验已指出的 4cb02bcf5 顺序偏差仍按首验记录处理。

在 detached 5ba1ed317 上独立复跑：
- 两个 environment 用例因 settings_root 参数不存在而失败。
  这两条只证明接口缺失，不单独证明拒启行为。
- R1 的 test_pi_rpc_process_refuses_a_config_default_launch_without_a_credential
  报 DID NOT RAISE AgentProviderCredentialMissing，是有效行为反例。
  同次运行有 fd teardown error：1 failed、1 error。
  pytest 已输出结果，但进程未退出，外层 120 秒超时终止了该次运行。
- R2 的 test_a_live_worker_logs_the_rebind_warning_while_it_runs
  在两个 SDK 均因存活日志缺少 session.provider.rebound 而失败。
  连同上述两个 environment 用例，每个版本为 3 failed。
- 原 boundaries 脚本在红提交、两个 SDK 均完成 3 个场景并退出 0。
  无 grant 的 fresh 场景仍 ready=true、modelProvider=unknown。
  无 grant 的 stale 场景仍 ready=true、modelProvider=deepseek。
  有 grant 的 stale 场景为 stderrWarning=true、liveLogWarning=false。

修复后，同一批测试包含在两个版本的焦点集中，均通过。
红提交上的清理错误没有在当前焦点集或全量重现。

最初对照使用 /tmp 路径别名，loader 提前退出，不能证明行为。
改用 /private/tmp 规范路径后，才得到上述有效红读数。
原因是 Node 的 import.meta.url 与 argv 中路径的字符串比较。
未改产品代码，也未把路径别名导致的失败算作 R1/R2 证据。

## 5. 独立门禁与全量

环境：macOS arm64、uv 管理的 CPython 3.12.13、Node 24.21.0。
主运行使用 Pi 1.2.0；另用 /tmp/pi-accept-sdk085 的隔离 Pi 0.85.1。
未修改全局 Pi 安装。
GUI 前置安装为 12 packages、0 vulnerabilities。

亲自运行的结果如下：
- 结构门：exit 1，仅 2 项既有违规；按任务约定不计入拒绝项。
  registry_ops.py 为 1005 行；tests/daemon/outbox 为 8 个直接文件。
  62465dbec 的结构门也只有这 2 项，没有新增违规。
- 模块边界门：PASS，violations=[]。
- ruff：PASS，All checks passed。
- Pi 1.2.0 焦点集：1 failed、1079 passed，131.25 秒。
  唯一失败为断言 SDK 0.85.1，实际为 1.2.0。
- Pi 0.85.1 的同一焦点集：1080 passed，120.23 秒。
- contract/run-all.sh --isolated-only：PASS，exit 0。

全量结果：FAIL，exit 1。
开始：2026-10-16T19:34:03Z；结束：2026-10-16T20:13:30Z。
全量期间没有并发运行其他测试或探针。

    4 failed, 12954 passed, 14 skipped, 5 xfailed, 1 xpassed
    2358.21s (0:39:18)

4 条失败均在 detached 62465dbec 上独立重现 2 次。
两次均为 4 failed，分别耗时 5.58 秒、5.65 秒：

1. tests/daemon/harnesses/pi/configuration/test_pi_controlled_loader.py::test_t04_t05_t06_real_sdk_loads_c_and_trusted_project_with_git_boundary
   原文：assert '1.2.0' == '0.85.1'。
   当前版本差异已由隔离 0.85.1 的焦点集排除。
2. tests/daemon/ipc/cost/test_ipc_cost_counters.py::test_four_sides_cover_a_pure_heartbeat_load
   当前全量：8.11/14.281 < 0.70。
   对照两次：5.658/8.615、5.802/8.706，均低于 0.70。
3. tests/kernel/platform/test_platform_actor_preservation.py::test_posix_listener_keeps_request_owner_and_reserve_fd
   原文：OSError: AF_UNIX path too long。
4. tests/tooling/architecture/authority/test_inbox_authority_gate.py::test_current_raw_bypasses_are_exactly_in_the_shrinking_manifest
   原文：unmanifested inbox bypass: src/hyprial/daemon/impl/runtime/forwards.py:_BridgeForwardsMixin._log_missing_failure_route:alarm_emitter。

当前树另单跑 3 条非版本失败，仍为 3 failed。
没有据此把全量结果改写成 PASS。
14 个 skip、5 个 xfail、1 个 xpass 不计作真实环境验收通过。

## 6. 复跑命令与证据

所有测试和探针均先清理 worker 身份及代理环境。
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

在同一清理环境下运行：

    uv run --no-project --python 3.12 python scripts/ci/check_structural_limits.py
    uv run --no-project --python 3.12 python scripts/ci/check_module_boundaries.py
    uv run ruff check .
    npm --prefix src/gui ci --ignore-scripts
    uv run --with pytest-timeout pytest tests/daemon/harnesses/pi tests/daemon/pac tests/daemon/harnesses/turn_delivery tests/daemon/delivery/providers tests/identity/providers tests/shell/cli/agents/test_cli_pi_interactive.py -q --timeout 600
    uv run sh contract/run-all.sh --isolated-only

3 个原有探针，均设置 PYTHONPATH="$PWD:$PWD/src"：

    uv run python docs/notes/resident-pi-provider-acceptance-boundaries-2026-10-16.py
    uv run python docs/notes/resident-pi-provider-acceptance-wire-2026-10-16.py
    uv run python docs/notes/resident-pi-provider-credentials-rework2-probe-2026-10-16.py

0.85.1 的重复运行仅在上述环境的 PATH 前加入：

    /tmp/pi-accept-sdk085/node_modules/.bin

单独复跑原 boundaries 授权场景时，只加载原文件的导入与函数定义，
不执行底部的 3 次场景调用，再执行 asyncio.run(run(True, True))。
原函数的 get_state、日志读取和 stop 均保持不变。
另断言实际 modelProvider/modelId 与 liveLogWarning 的读数。

对照使用 /private/tmp/pi-accept2-red 与 /private/tmp/pi-accept2-base。
PYTHONPATH 指向各自的 src 与仓库根；uv run --project 指向当前树。
测试文件与产品源码均来自各自对照提交，只复用当前树的锁定依赖。

原始读数归档：

    docs/notes/resident-pi-provider-credentials-acceptance-2-readings-2026-10-16.txt

归档包含完整全量输出、有效红失败、两个版本的探针读数、
对照失败、结构门与 contract 输出。单份输入均小于 1 MB。
6 MB 的完整模块边界 JSON 未入库；本报告保留 PASS 与 violations=[]。
旧 notes 与原复现脚本未修改。
原始输出保留 pytest 的行尾空白。
git diff --cached --check 仅对该 txt 报空白问题，未改写原始证据。

## 7. 范围、清理与未测项

隔离测试状态均在临时目录；假 provider 仅监听 loopback。
两个验收 detached worktree 已删除，测试进程已退出。
未读取、修改或清理生产 daemon、真实凭据及用户级配置。
未 push、未开 PR、未向其他 agent 发消息。

Pi 0.85.1 的完整 tests/ 未重跑，结果为 UNKNOWN。
生产 daemon、真实 Lark、真实 provider、真实配额恢复均为 UNKNOWN。
本次 ACCEPTED 仅表示首验 R1/R2 已修复且已通过项未见退化。

Contract-Impact：本提交只增加验收报告和读数，不改产品契约。
分支 429 分类的契约说明仍按首验要求由集成方处理。
Skill-Impact：本提交不改变 agent 操作方式，不修改 skill。
