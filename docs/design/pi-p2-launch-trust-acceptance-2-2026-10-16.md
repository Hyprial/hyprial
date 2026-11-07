Pi P2 启动信任二次验收（2026-10-16）

判词：REJECTED。

A、B 的首验反例已修复。C1 与 C3 已修复。
C2 的同步错误码与消息断言已适配，但删除了 Pi 正向启动断言。
因此，首验新增的 transfer 失败不能判为已经解决。

验收对象：fix/pi-p2-launch-trust，产品 HEAD 4bb5111e3。
首验判词：docs/design/pi-p2-launch-trust-acceptance-2026-10-16.md，73d1a19c9。
对照基线：detached 62465dbec549f72cc378c0d206504471e816b5e0。
验收范围仅为首验 A/B 与 3 项新增全量失败。
其余首验通过项只检查是否发生回归。
验收者未修改产品源码、仓库测试或既有证据。

1. 阻断项 C2：Pi 正向断言被删除

状态：FAIL。

位置：tests/integration/transfer/p2/e2e/test_two_daemon_chain.py:475-497。
提交：7eea54b28。

负向适配正确：ipc_raw 读取同步拒绝。
测试同时断言 PI_P2_LAUNCH_UNTRUSTED 与 explicit --approve trust。
这两项断言均保留了明确的错误原因。

但第 7 步新增 if harness != "pi"。
Pi 不再执行下列原有操作和断言：

1) 带 --approve 调用目标 daemon 的 lifecycle.start。
2) 通过 ps 等待目标 worker running。
3) 在目标启动后再次确认源端 worker 不运行。

原测试现在通过，只能证明 Pi 同步拒绝。
Claude 的通过不能替代 Pi 的正向启动。
transfer.receive 返回 active 也不能替代目标进程的 ps 见证。
这违反了本次“测试改动未放松断言”的验收要求。

独立复测保留当前负向断言，只恢复 Pi 的第 7 步。
复测使用仓库原 fixture、真实双 daemon 与原协议 double。
复测未调用真实模型，也未注入真实凭据。

原测试：1 passed，6.73 秒。
恢复原正向断言：1 failed，24.52 秒。
失败位置为目标 lifecycle.start，IPC recv 抛 TimeoutError。

补充诊断只将原始 IPC 读取超时改为 90 秒。
正向断言仍保留，结果仍为 FAIL。
两次诊断分别耗时 92.50 秒和 92.28 秒。
第二次读取到如下错误；解释器绝对路径已脱敏：

code: LIFECYCLE_OPERATION_UNSETTLED
lastError: pi harness failed to start: Pi SDK package root not found beside <CPython 3.12.13>/bin/python3.12

因此，源码注释将省略原因归为 SDK 1.2.0 与 0.85.1 不符，缺少这次实测支持。
本次实际先遇到协议 double 的 Python 命令无法定位 Pi SDK 包。
不能用删除正向断言来处理 fixture 的启动条件。

修复要求：保留同步 code/message 断言，并恢复 Pi 正向启动与双端 ps 断言。
若需要适配 SDK/协议 double，应修正隔离 fixture。
无法执行时，明确报告 UNKNOWN/BLOCKED，不得把缩减后的通过当作完整通过。

复现方法：复制当前测试到隔离目录。
将相对 .conftest 导入改为 tests.integration.transfer.p2.e2e.conftest。
通过 pytest_plugins 加载同一 conftest。
只把第 7 步 if harness != "pi" 改为 if True。
执行该副本的 test_the_chain_lands_on_the_target_and_retires_the_source[pi]。
命令使用 PYTHONPATH="$PWD:$PWD/src"，并加载 -p tests.conftest。
执行器为 uv run --with pytest-timeout pytest，超时为 --timeout 120。
这次副本与原始输出保存在 /private/tmp/pi-p2-accept2-logs。

2. A：选项组与别名合并

状态：PASS。

位置：src/hyprial/kernel/impl/facts/launch_spec.py。
修复提交：4d50bb183。

合并按选项组处理，不再按单个 token 去重。
短选项的旧值随旧选项一起删除。
不同选项即使同值，也不会互相删除。
Codex 的 -a/--ask-for-approval、-m/--model、-s/--sandbox 使用同一选项键。
独立测试覆盖了短到长、长到短及 --name=value。

start 与 restore 都传入 harness 选择别名表。
routine 继续调用同一 lifecycle.start，没有第二套合并逻辑。
独立测试逐一走过 start、restore、routine。
三条路径得到的 launch spec 均只保留本次显式值。
三条路径均未修改已存默认参数。

首验共享值反例现返回：
(--model, deepseek, --provider, deepseek)

首验 Codex start 反例现返回：
(-a, never)
带未覆盖 sandbox 的函数反例保留 (-s, workspace-write)，没有旧 on-request。

首验独立 test_review.py 与 test_short_start.py 均已复跑。
复测沿用首验的 application fixture；执行进程仍为 fixture double。
这不声称已调用真实 Codex 模型。

3. B：workspace 符号链接拒绝

状态：PASS，限定为首验要求的静态路径检查。

位置：src/hyprial/daemon/impl/harnesses/pi/loader.py。
修复提交：3d94b6d53。

workspace receipt 在 canonical 化之前保留并检查原路径。
检查使用 lstat，要求 agents 目录、agent-home 与 workspace 为真实目录。
workspace scope 的直接 PiProjectTrustReceipt 构造也执行同一检查。
resolve_approved_pi_project 未再把 canonical 路径直接作为 workspace receipt 输入。
函数中仍有前置 resolve 用于 cwd/Git 发现与存在性检查。
这些调用未丢弃随后校验所需的原 workspace 路径。

首验 4 种反例均为 REFUSED：

1) workspace 链接指向另一 agent 的 workspace。
2) workspace 链接指向任意私有目录。
3) workspace 经多级链接指向目标目录。
4) cwd 直接使用 workspace 链接路径。

补充独立测试覆盖 workspace、agent-home、agents 目录三处链接。
每种情况分别使用链接 cwd 与 canonical cwd。
直接构造 workspace receipt 也全部拒绝。
真实 0700、非 Git 的本 agent workspace 正例通过。
无关私有目录负例拒绝。

上述检查未声称解决并发替换路径的竞态。
完整生产启动中的越权可利用性仍为 UNKNOWN。
本次未读取凭据，也未测试生产 agent。

4. C1：默认配置与上次启动参数分离

状态：PASS。

修复提交：b6726d9dd。
位置：src/hyprial/daemon/impl/application/actors/agents_registry.py。

已存 Agent 的 _ensure_agent 不再回写 harness_args。
本次启动参数继续保存在 desired state。
agent create 设置的默认参数仍参与合并。
显式参数只覆盖本次启动，不污染下一次默认配置。
新建记录的既有首次参数初始化分支未删除。

未修改的 test_explicit_lifecycle_start_retires_same_entity_restore_aba 已复跑。
修复前实际为 (--episode-a, --episode-b)，断言失败。
当前断言通过，desired state 恢复为仅 (--episode-a)。
独立 start/restore/routine 用例同时检查默认参数记录不变。
补测显式覆盖后停止，再不带参数启动。
新启动恢复默认值，没有带回 --episode-b。
恢复路径仍保留上一次 desired state 的显式参数。

5. C3：门禁登记与冻结摘要

状态：PASS。

登记提交：833601f6c。
门禁记账提交：4bb5111e3。

supervision_exemptions.json 只增加 1 条精确记录：
path: src/hyprial/daemon/impl/harnesses/pi/rpc_process.py
symbol: PI_RPC_STARTUP_TIMEOUT_SECONDS_DEFAULT
kind: module constant
owner: hyprial-developer
card: pi-p2-launch-trust

该常量是 launcher 的 30 秒启动就绪预算。
登记说明了复用 actor_runtime 请求级 ready 原语不可行的原因。
实际 launcher 引用该常量，没有通过改名隐藏门禁发现。
这是启动握手预算，不是新增常驻业务轮询循环。

原有 66 条 exemption 的字段与值全部保持不变。
当前共 67 条。
按测试的 path 归一化算法重新计算 SHA-256：
d68c75c789e76741da31bb433bd64ae313de12f97f1f0fa98eb86a30cb60096c
该摘要与 test_authority_migration.py 的冻结值一致。

provider-term lint 只增加 1 条规则。
规则限定 launch_spec.py 的 --provider 选项表字面量。
该选项确实表示模型厂商，不是把 harness 改称 provider。
规则未放行整文件，也未放宽其他路径。

supervision、provider-term lint 与 authority migration 焦点测试均通过。

6. 先红证据

A 与 B 的测试提交均是修复提交的祖先。
验收者新建 detached worktree，未改写历史提交。
两个 worktree 均使用 CPython 3.12.13。

A：780abdb0a → 4d50bb183。
780abdb0a 定向结果为 4 failed、20 deselected。
其中 3 项执行了行为断言：短选项 start、短选项函数、共享值函数。
第 4 项因旧函数不接受 harness 关键字而 TypeError。
该项不能单独充当别名行为的先红证据。
另用兼容旧 API 的独立测试补验 3 组别名的双向覆盖。
旧函数未传新增 harness 关键字，避免 TypeError。
780abdb0a 为 6 failed，均为参数值断言失败。
4bb5111e3 为 6 passed，0.26 秒。
当前独立别名矩阵还覆盖了 --name=value。

B：5b52849d2 → 3d94b6d53。
5b52849d2 定向结果为 4 failed、7 deselected。
全部失败均为 DID NOT RAISE PiProjectTrustError。
没有 ImportError 或接口 TypeError 代替行为断言。

C1/C2/C3 使用首验已经存在的失败测试。
本次在修复前提交重新执行，得到：

C1：780abdb0a，1 failed。desired state 多出 --episode-b。
C2：5b52849d2，1 failed。旧测试收到同步 PI_P2_LAUNCH_UNTRUSTED。
C3：5b52849d2，1 failed。启动预算常量未登记。

C2 的原始负向失败得到解释，但其正向覆盖被删除。
因此 C2 仍不能判为修复完成。

7. 门禁与焦点测试

结构门原始结果为 FAIL，只有指定排除的 3 项既有违规：

src/hyprial/daemon/impl/application/actors/agents_admin/registry_ops.py：1005 行。
tests/daemon/outbox：8 个直接文件。
contract/e2e/_daemon_runner.py：1004 行。

本次未增加结构违规。
边界门：PASS，exit 0。
ruff：PASS，All checks passed。

亲自执行的命令：

uv run --no-project --python 3.12 python scripts/ci/check_structural_limits.py
uv run --no-project --python 3.12 python scripts/ci/check_module_boundaries.py
uv run ruff check .

指定焦点：
uv run --with pytest-timeout pytest tests/daemon/harnesses/pi tests/shell/cli/agents tests/daemon/desired_state -q --timeout 600

结果：1 failed、396 passed，259.82 秒。
唯一失败为首验已有的 Pi SDK 固定版本断言。
实际 1.2.0，期望 0.85.1。
这次仍执行了真实 SDK probe，没有用 skip 代替结果。

补充焦点包含 identity registry、composition ABA、restore、lifecycle、routine、PAC 和 3 类门禁。
结果：1275 passed，125.83 秒。
首验独立反例、同步 CLI 拒绝与本次补充矩阵合计：39 passed，2.76 秒。
补充矩阵曾误用 owner，并曾未停止旧 connector 就再次启动。
这两次失败均为验收 fixture 错误。
改用真实 agent.uri，并在重启前 down，39 项全部通过。

8. 全量与失败对照

全量结果：FAIL，exit 1。
耗时 2433.85 秒，执行窗口为 15:06:21–15:47:04 UTC。
统计为 4 failed、12983 passed、14 skipped、5 xfailed、1 xpassed。
skip、xfail 与 xpass 均未用作本次要求的通过证据。

全量命令在上述环境清理包装器内执行：

env -u HYPRIAL_NODE_ID -u HYPRIAL_MANAGED_WORKER -u HYPRIAL_WORKER_ACTOR -u HYPRIAL_WORKER_SESSION_REF -u HYPRIAL_TOOL_PROFILE_ID -u HTTP_PROXY -u HTTPS_PROXY -u ALL_PROXY -u http_proxy -u https_proxy -u all_proxy -u GIT_CONFIG_GLOBAL -u GIT_CONFIG_NOSYSTEM NO_PROXY=127.0.0.1,localhost no_proxy=127.0.0.1,localhost PATH=/Users/bobadmin/.npm/_npx/387698761821791d/node_modules/node/bin:$PATH uv run --with libcst==1.9.0 --with pytest-timeout pytest tests/ -q --timeout 600

全部 4 个失败项都按完整 node id 在首验基线和当前分支复跑。
顺序为基线 → 当前 → 基线 → 当前。
4 轮均为相同 4 项失败，分别耗时 6.49、6.22、5.88、5.97 秒。
基线的实际 import 路径指向 baseline/src/hyprial。
基线解释器同为 CPython 3.12.13，未误用当前分支的包。
基线未执行第二次全量；对照限定为全部失败项。

失败项与归因：

1) tests/daemon/harnesses/pi/configuration/test_pi_controlled_loader.py::test_t04_t05_t06_real_sdk_loads_c_and_trusted_project_with_git_boundary
两棵树均为 1.2.0 != 0.85.1。属于首验既有环境版本不符。

2) tests/daemon/ipc/cost/test_ipc_cost_counters.py::test_four_sides_cover_a_pure_heartbeat_load
两棵树均未达到 CPU 计数覆盖率 0.70。
全量为 10.478/18.360。
基线两次为 10.492/15.832、5.960/9.092。
当前定向两次为 8.905/13.515、6.697/10.115。
属于首验已重现的既有失败，本次未证明新增退化。

3) tests/kernel/platform/test_platform_actor_preservation.py::test_posix_listener_keeps_request_owner_and_reserve_fd
两棵树均为 OSError: AF_UNIX path too long。
属于首验既有失败。

4) tests/tooling/architecture/authority/test_inbox_authority_gate.py::test_current_raw_bypasses_are_exactly_in_the_shrinking_manifest
两棵树均为 forwards.py:_BridgeForwardsMixin._log_missing_failure_route:alarm_emitter 未登记。
属于首验既有失败。

对照命令：
uv run --with libcst==1.9.0 --with pytest-timeout pytest <上述 4 个完整 node id> -q --timeout 600 --tb=short

首验新增的 ABA 与 supervision 失败，本次全量均通过。
transfer 的修改版测试也通过，但其 Pi 正向断言已被绕过。
因此不能凭“只剩 4 项既有失败”判为 ACCEPTED。
恢复正向断言的独立反例仍失败，见第 1 节。

9. 证据与交付边界

所有测试使用隔离 fixture home、socket 与回环端口。
GUI 依赖已用 Node v24.21.0 执行 npm ci --ignore-scripts。
本 worktree 的解释器为 CPython 3.12.13。

运行包装器清除了继承的 HYPRIAL_*、HARNESS_*、harness worker、Git 配置与代理变量。
NO_PROXY/no_proxy 均设为 127.0.0.1,localhost。
Node 24 目录位于 PATH 首位。
HYPRIAL_BIN 显式指向被测 worktree 的 .venv/bin/hyprial。

原始输出：/private/tmp/pi-p2-accept2-logs。
未把大于 1 MB 的原始输出写入仓库。
本文件保存判据、重现方法与结果，不以临时日志替代判词。
既有 docs/notes 字节未修改。

完整 Pi CLI→socket→daemon→SDK 正向成功路径仍为 UNKNOWN。
被删减的双 daemon 正向步骤实测为 FAIL。
真实模型、生产常驻 agent、生产凭据不在本次验收范围。
本判词不代替发版级真实环境验收。

Contract-Impact: 本提交仅新增验收文档，不改变字段或错误码。
Skill-Impact: 本提交不改变 agent 操作方式，无需修改 skill。
未 push，未开 PR，未向任何 agent 发消息。
