# 常驻 Pi provider 分支验收（2026-10-16）

REJECTED

验收对象：fix/resident-pi-provider-credentials，fc932e2d1。
开发基线：origin/dev 038763c03；失败对照：62465dbec。
范围：d7a396d42/f66736537，以及 §8.1、§8.2 续作。

拒绝原因有 2 项。默认配置选 deepseek 时，无授权拒启仍有遗漏。
headless worker 的重绑告警在进程运行期间不可见。
这 2 项是本次目标未完成，不能用既有测试通过替代。

## 1. 必须修复的验收缺口

### R1：默认配置选 deepseek 时，无授权仍能就绪

等级：P1；对应“无授权拒启”和 §8.1 的当前配置权威。

位置：
- src/hyprial/daemon/impl/harnesses/model_provider.py:271。
- src/hyprial/daemon/impl/harnesses/pi/resources/pi_controlled_loader.mjs:447。

pi_provider_environment 在 spec.model_provider 为 None 时直接返回空映射。
但 controlled loader 随后会从当前配置选择 deepseek。
两处使用的有效选择不同，凭据检查漏掉了配置默认值。

隔离实测条件：
1. 从 C 源准备 defaultProvider=deepseek、defaultModel=deepseek-flash。
2. launch 不带 modelProvider/model。
3. 不授予 DEEPSEEK_API_KEY，nativeRoot/auth.json 不存在。
4. 仅完成 RPC 就绪探测和 get_state，不发送 prompt。

实测结果：
- 新会话：ready=true，model.provider=unknown。
- 恢复 zai 会话：ready=true，model.provider=deepseek。
- 两次均没有授权 key；启动前不存在 nativeRoot/auth.json。
- SDK 就绪后会创建空的 auth.json，内容仍为 {}。
- 探针字段 nativeCredentialPresent 仅表示文件存在，不表示已授权。
- Pi 1.2.0 和 CI 钉定的 0.85.1 结果相同。

期望：当前有效 provider 需要凭据时，在就绪前拒绝启动。
拒绝必须给出授权操作，不得改用其它厂商或 operator 凭据。
修复必须覆盖显式选择和配置默认选择，保留内置厂商负例。

复现文件：docs/notes/resident-pi-provider-acceptance-boundaries-2026-10-16.py。
这是启动探测，不需要真实 provider 请求。
同一探针换用 62465dbec 产品代码，也会无授权就绪。
因此 R1 是既有缺口未封住，不是本分支新增的回归。

### R2：headless 重绑告警只进入内存缓冲

等级：P2；对应 §8.1“跟随当前配置且有告警”。

位置：
- src/hyprial/daemon/impl/harnesses/pi/resources/pi_controlled_loader.mjs:397。
- src/hyprial/daemon/impl/harnesses/pi/rpc_client.py:865、878。

loader 向 stderr 写出 PI_CONTROLLED_LOADER_WARNING。
PiRpcClient._drain_stderr 只把字节保存在 _stderr_tail。
_watch_process_exit 等进程结束后才把 stderrTail 写入 worker 日志。
因此常驻 headless worker 仍在运行时，操作员看不到重绑告警。

隔离实测：agent 配置 deepseek，会话记录 zai-coding-cn/glm-4.6。
agent 获得假 key，launch 不带 provider。

实测结果：
- get_state.model.provider=deepseek。
- stderrWarning=true。
- liveLogWarning=false。
- logWarningAfterStop=true。
- Pi 1.2.0 和 0.85.1 结果相同。

原有 test_pi_session_provider_rebind 捕获的是退出后的 stderr。
该测试不能证明常驻 worker 运行期间存在可读告警。

期望：重绑时立即写入可查询的 worker 告警日志。
日志保留 sessionId、旧选择和新选择，不记录凭据。
另补 headless 存活期间的日志断言，不能只断言子进程 stderr。
复现使用 R1 的同一份 boundaries 脚本。

## 2. 已通过的行为检查

2.1 显式 deepseek 启动的凭据边界：PASS。

焦点测试验证了 grant 环境、agent native credential 和 legacy 来源。
P2 不使用 operator HOME 回退。缺 key 的拒绝文字包含 grant 操作。
错误文字没有拼接 key；凭据仍不进入 argv 或 desired state。
授权后需要重启才能更新子环境，credential-invalid 需显式 unblock。

非 deepseek 内置厂商负例：
tests/identity/providers/authentication/test_provider_auth_hook.py::test_pi_stop_reason_error_triggers_observer。
该用例在 Pi 1.2.0 和 0.85.1 均通过，没有再次误拒内置厂商。

2.2 §8.1 的实际模型重绑：PASS；运行中告警：FAIL（R2）。

真实 SDK 的 zai-coding-cn/glm-4.6 → deepseek/deepseek-flash 反例已复现。
匹配配置的会话不重复改写，漂移会话新增 model_change。
告警内容不含测试 key。

2.3 §8.2 的 provider 调用次数及通知条数：PASS。

除独立运行新增测试外，另把以下实际组件接在一起：
- P2 的 grant 与完整子环境组合。
- PiRpcClient、PiRpcProcess 和 TurnRuntime。
- 真实 controlled loader 和真实 Pi SDK。
- contract/e2e/_fake_provider.py，持续返回 HTTP 429。
- DaemonEventBridge 的 settlement 与 availability_loud 通知路径。

边界说明：InboxPort 和 transport 使用仓内 LoudInbox、_NullTransport。
进程调度适配复用 FakeHarnessFarm 接口，dispatch 实际调用 process.enqueue。
P2 不接受 ambient endpoint override，所以探针只把 launch payload 中
modelDefinitions.deepseek.baseUrl 改为假 provider 的 loopback 地址。
探针没有替换错误、结果、重试决策或通知实现。
这证明组合组件的行为，不冒充完整 CLI/daemon 网络端到端验收。

Pi 1.2.0 和 0.85.1 的读数均为：

    calls=1
    dispatches=1
    attempts=1
    terminal=true
    failureCode=PROVIDER_USAGE_LIMIT
    notifications=1
    processStopped=true

时钟再推进 30 分钟、再跑 10 次 daemon timer，仍无第二次调用或通知。
复现文件：docs/notes/resident-pi-provider-acceptance-wire-2026-10-16.py。

identity/provider_auth 对 429 保留 OTHER 分类。
认证观察器不重新登录、不另发账号告警。
loader 的两层重试均关闭，瞬态错误改由 hyprial 重试。
该重试权威决定已显式记录，原有瞬态错误预算负例仍通过。

2.4 PAC 启动路径：PASS（代码检查及隔离测试范围）。

PAC、workflow、routine 和完整环境组合器的产品代码未变。
PAC 与常驻 headless 启动仍共用 HarnessLauncher._make_pi_process。
没有新增 PAC 特例，也没有第二条凭据来源。
tests/daemon/pac 全部通过。
未向生产 PAC 派发测试请求。

## 3. 先红后绿审计

独立 detached worktree 重跑了修复前提交，未改当前产品代码。

- d7a396d42：7 failed、26 passed。
  其中 6 条因新 resolver/参数不存在而失败，只证明接口缺失。
  CLI 拒启用例则因 exit_code=0 而失败，是有效的行为反例。
  f66736537 随后修复该边界。
- edf7b67a9：1 failed、1 passed。
  失败断言是实际 provider 为 zai-coding-cn，期望 deepseek。
  30c69b4e6 随后修复重绑。
- d8ced8f09（含 bcb17c2c0）：6 failed、40 passed。
  假 provider 实收 4 次 completion，期望 1 次。
  其余失败覆盖 terminal、permanent、首投 abandon 和通知 settlement。
  371909504 随后修复。

三项功能均有早于修复提交的有效行为红测试。
4cb02bcf5 的 2 条旧断言更新发生在修复之后。
其提交标题中的“red”不符合实际顺序，不能充作先红证据。
已有更早的有效红测试，因此此偏差不单独决定拒绝。

R1、R2 未由原有红测试覆盖，须补新反例后修复。

## 4. 独立运行的门禁

环境：macOS arm64，uv 管理的 CPython 3.12.13，Node 24.21.0。
主运行使用本机 Pi 1.2.0；另在 /tmp 独立安装 Pi 0.85.1。
没有修改全局 Pi 安装、用户级配置、真实凭据或生产 daemon。

GUI 前置安装已完成：npm --prefix src/gui ci --ignore-scripts。
62465dbec 对照 worktree 也安装了同一锁文件的 GUI 依赖。
两次安装均为 12 packages、0 vulnerabilities。
没有沿用续作报告中“缺 node_modules”的环境缺口。

门禁结果：
- check_structural_limits.py：exit 1，仅 2 项已知豁免。
  registry_ops.py 为 1005 行；tests/daemon/outbox 为 8 个直接文件。
  62465dbec 对照也存在这 2 项，未出现新增结构违规。
- check_module_boundaries.py：PASS，violations=[]。
- uv run ruff check .：PASS。
- tests/daemon/harnesses/pi tests/daemon/pac：885 passed、1 failed。
  唯一失败是断言 SDK 0.85.1，实际安装为 1.2.0。
- 同一焦点集改用隔离 Pi 0.85.1：886 passed。
- turn_delivery、delivery/providers、identity/providers、交互 Pi CLI：188 passed。
- Pi 0.85.1 的 configuration 重绑、429、controlled loader：16 passed。
- contract/run-all.sh --isolated-only：PASS，exit 0。

全量实测：FAIL，exit 1。
开始时间为 2026-10-16 16:30:55 UTC，结束时间为 17:10:43 UTC。

    4 failed, 12948 passed, 14 skipped, 5 xfailed, 1 xpassed
    2378.70s (0:39:38)

4 条失败均在 62465dbec 的独立 detached worktree 重现了 2 次：

1. tests/daemon/harnesses/pi/configuration/test_pi_controlled_loader.py::test_t04_t05_t06_real_sdk_loads_c_and_trusted_project_with_git_boundary
   原文：assert '1.2.0' == '0.85.1'。
   判定：既有 SDK 版本环境差异。隔离 0.85.1 的同一焦点集已通过。
2. tests/daemon/ipc/cost/test_ipc_cost_counters.py::test_four_sides_cover_a_pure_heartbeat_load
   本次比值为 9.607/16.629，低于 0.70。
   判定：既有成本覆盖断言失败。
3. tests/kernel/platform/test_platform_actor_preservation.py::test_posix_listener_keeps_request_owner_and_reserve_fd
   原文：OSError: AF_UNIX path too long。
   判定：既有临时 socket 路径环境失败。
4. tests/tooling/architecture/authority/test_inbox_authority_gate.py::test_current_raw_bypasses_are_exactly_in_the_shrinking_manifest
   原文：unmanifested inbox bypass: src/hyprial/daemon/impl/runtime/forwards.py:_BridgeForwardsMixin._log_missing_failure_route:alarm_emitter。
   判定：既有 authority manifest 漂移。

对照首轮为 4 failed、57 passed；第二轮只跑这 4 条，仍为 4 failed。
当前树的 3 条非版本失败另单跑一次，仍失败；GUI 生命周期为 57 passed。
两棵树都已安装 GUI 依赖，本次全量没有续作报告中的 5 条 GUI 缺依赖失败。

全量没有发现新增的套件断言失败，但 R1、R2 仍未获测试覆盖。
14 个 skip、5 个 xfail 和 1 个 xpass 不计作真实环境验收通过。
原始 -q 命令未逐项输出 skip 原因，本报告不推定这些场景已验证。
未在 0.85.1 上再跑完整 tests/，该组合的全量结果仍为 UNKNOWN。

## 5. 复现命令和证据边界

全量与焦点测试使用下列环境清理。
该环境不继承当前 worker 身份及代理。

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

其它门禁与焦点测试命令：

    uv run --no-project --python 3.12 python scripts/ci/check_structural_limits.py
    uv run --no-project --python 3.12 python scripts/ci/check_module_boundaries.py
    uv run ruff check .
    uv run --with pytest-timeout pytest tests/daemon/harnesses/pi tests/daemon/pac -q --timeout 600
    uv run --with pytest-timeout pytest tests/daemon/harnesses/turn_delivery tests/daemon/delivery/providers tests/identity/providers tests/shell/cli/agents/test_cli_pi_interactive.py -q --timeout 600
    uv run sh contract/run-all.sh --isolated-only

测试命令也使用上述身份及代理清理环境。
单独复现两个验收探针时，在上述环境下运行：

    PYTHONPATH="$PWD:$PWD/src" uv run python \
      docs/notes/resident-pi-provider-acceptance-boundaries-2026-10-16.py
    PYTHONPATH="$PWD:$PWD/src" uv run python \
      docs/notes/resident-pi-provider-acceptance-wire-2026-10-16.py

0.85.1 使用下列独立安装，不修改全局 Pi：

    npm install --prefix /tmp/pi-accept-sdk085 --ignore-scripts @earendil-works/pi-coding-agent@0.85.1

0.85.1 对照另将 /tmp/pi-accept-sdk085/node_modules/.bin 加入 PATH。
基线测试在 /tmp/pi-accept-base-62465（detached 62465dbec）运行。
对照使用 PYTHONPATH=<基线>/src，uv run --project 指向本工作树以复用锁定依赖。
对照收集基线自己的测试文件；仅额外的 boundaries 探针复用当前树的测试 helper。
脚本只创建 /tmp 下的隔离状态，wire 探针会关闭真实子进程。
boundaries 探针只发 get_state；wire 探针只访问 loopback 假 provider。
验收自建的 2 个 detached worktree 已删除。
两类探针均已关闭测试子进程，没有遗留探针进程。
旧 notes 未修改；本次只新增 2 个复现文件。

未测项：生产 daemon、真实 Lark、真实 provider、真实配额恢复均为 UNKNOWN。
本次不以 skip 证明这些场景通过。

Contract-Impact：本验收提交只增加报告和复现证据，不改产品契约。
被验收分支将 429 从 transient 改为 permanent，属于已发布分类语义变化。
分支未同步 contract/；集成 PR 必须给出明确的 Contract-Impact 理由或补契约。
Skill-Impact：本验收不改变 agent 操作方式，不修改 skill。
