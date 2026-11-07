Pi P2 启动信任四次验收（2026-10-17）

判词：ACCEPTED，限本轮返工与三次判词阻断项。

P2 无相邻 SDK 时已取消直跑 RPC 回退。
普通 PATH 转发脚本与无 SDK 命令均拒绝启动。
真实 SDK 0.85.1 的 transfer Pi 正例为 1 passed。
A、B、C1、C3 的抽检通过。
本判词不表示全量、真实模型或发版验收通过。

验收对象：fix/pi-p2-launch-trust。
产品 HEAD：93f3f238a8efa56b4fcb47c2bf6333c832dbb656。
三次判词：44e5a3b48，docs/design/pi-p2-launch-trust-acceptance-3-2026-10-16.md。
本轮提交：d5860a27a → faf4940f6 → 93f3f238a。
验收者只新增判词与证据，没有修改产品或 tests/。

1. 取消 P2 直跑回退

状态：PASS。

faf4940f6 删除 find_pi_package_root_or_none。
PiRpcClient 的 P2 分支改为直接调用 find_pi_package_root。
找到 SDK 后，P2 分支始终构造受控 SDK bridge。
找不到 SDK 时，构造 client 抛 ValueError，不构造直跑命令。
非 P2 的既有 RPC 分支没有删除。

对普通转发脚本的独立测试使用以下内容：

#!/bin/sh
exec /opt/homebrew/bin/pi "$@"

将脚本目录放到 PATH 首位，使用默认 command=("pi",)。
结果为明确拒绝；显式传入脚本路径也拒绝。
使用 CPython 命令且相邻目录没有 Pi SDK 时，同样拒绝。
PiRpcProcess 的 ready gate 返回 false，last_error 保留缺 SDK 原因。
ready gate 使用测试给定的 10 秒等待预算，不声称立即返回。

错误包含解析后的命令路径与以下补救要求：
a pi installation with the @earendil-works/pi-coding-agent SDK is required for P2 launches

错误没有输出合成 auth.json 中的凭据标记。
补救动作是使用相邻目录包含上述 SDK 的 Pi 安装。
本次没有恢复完整环境合并，也没有恢复操作员 HOME。

2. 三次判词的 8 项独立复现

状态：PASS，8 passed，12.21 秒。

新复现文件：
docs/notes/pi-p2-launch-trust-acceptance-4-2026-10-17/reproduce.py。

本轮保留原来的 8 个检查场景。
原来验证“直跑成功且越界”的预期改为“拒绝”。
逐项结果如下：

1) PATH 包装器拒绝；原安装仍读取 C 的 gpt-4o-mini。
   nativeRoot 的 gpt-4o 残留不生效。
   HOME/.agents/skills 中的诱饵 skill 不加载。
   完整子环境逐字相等，HOME 与 PI_CODING_AGENT_DIR 保持 P2 根。
   get_state 的 sessionFile 位于显式 session_root。
   无 SDK 的 CPython 命令与包装器 ready gate 均拒绝。
2) 项目 AGENTS.md 链接到合成用户 auth.json 时，包装器先拒绝。
   原安装的 SDK bridge 仍拒绝非普通文件，未输出合成内容。
3) 原安装缺少 --approve 时，仍抛 PiProjectTrustError。
4) 包装器缺少 --approve 时，仍先抛同一信任错误。
5) 原安装的系统提示词包含 C，排除 Git 根以外的祖先 AGENTS.md。
   包装器拒绝，未执行用于观察提示词的项目扩展。
6) 原 npm 命令、真实 CLI 文件路径与 CLI 符号链接均走 SDK bridge。
   argv 仍为 pi_controlled_loader.mjs run-rpc。
7) 原安装接受真实 0700 的本 agent 非 Git workspace。
   workspace 改为链接后，信任检查拒绝。
8) 包装器在真实 workspace 中因缺 SDK 拒绝。
   workspace 改为链接后，仍先由 lstat 信任检查拒绝。

独立复现使用本机原安装 Pi 1.2.0，只执行离线资源加载与 RPC 查询。
没有发送模型请求。
原三次 reproduce.py 引用已删除的 helper，描述的是旧直跑行为。
按 WORM（只追加、不改旧字节）规则，原文件保持不变。
原文件 Git blob 仍为 2f79f00ba32bc8f42971b74ae3f9629d531a10f4。
本次没有把旧文件的 ImportError 当作行为验证结果。

3. 先红与修复顺序

状态：PASS。

d5860a27a 是 faf4940f6 的直接父提交。
红提交只新增 test_pi_sdk_required.py，共 2 项断言。
红提交作者时间为 01:11:08，提交时间为 01:13:02。
修复提交时间为 01:13:06，均为 2026-10-17 UTC+8。

本次在独立 detached worktree 重跑红提交：
2 failed，10.82 秒。
已核对 import 路径指向该 worktree，解释器为 CPython 3.12.13。

失败原因对应本次修复：
1) ready gate 测试读到 Python 的 unknown option --approve。
   错误没有“pi installation with the”，断言失败。
2) 包装器 client 构造未抛 ValueError，报告 DID NOT RAISE。

两项均执行到了目标行为断言，没有用导入失败代替先红。
当前分支的 2 项测试随 Pi 焦点全部通过。

4. transfer 第 7 步与环境差异

状态：PASS，限定为真实 SDK 启动与双端状态读取。

93f3f238a 将 Pi 正例的 command 改为已安装 Pi 命令。
Pi 正例不再依靠 Python 协议 double 启动 P2 worker。
本次在临时目录安装 SDK，没有修改全局 npm 安装：

npm --prefix /private/tmp/pi-p2-fourth-independent/pi85 install @earendil-works/pi-coding-agent@0.85.1 --no-audit --no-fund

PATH 首位使用该目录的 node_modules/.bin，Node 为 v24.21.0。
已读取安装清单并确认 SDK 版本为 0.85.1。
测试使用真实双 daemon、隔离 home/socket 与回环网络。
源端协议 double 与 Claude 协议 double 保持原设计。
目标 Pi 的第 7 步使用真实受控 SDK bridge，不调用模型。

单项命令：
bash docs/notes/pi-p2-launch-trust-acceptance-4-2026-10-17/run-clean.sh env HYPRIAL_TEST_TMP_ROOT=/private/tmp HYPRIAL_REQUIRE_PI_SDK=1 PATH=/private/tmp/pi-p2-fourth-independent/pi85/node_modules/.bin:/Users/bobadmin/.npm/_npx/387698761821791d/node_modules/node/bin:$PATH uv run --with pytest-timeout pytest 'tests/integration/transfer/p2/e2e/test_two_daemon_chain.py::test_the_chain_lands_on_the_target_and_retires_the_source[pi]' -q --timeout 120 --tb=short

结果：1 passed，9.61 秒，无 skip。
保留了 launch → target running → source not running 的全部断言。
同步负例仍检查 PI_P2_LAUNCH_UNTRUSTED 与 explicit --approve trust。
Claude 正例仍使用原协议 double，启动与双端状态断言未变。

本机 1.2.0 的失败形态也已复现，但不能仅归因于版本。
本次发现 macOS 临时目录拼写会影响复制后的 JS 入口：

1) SDK 1.2.0，默认 /tmp：1 failed，23.89 秒。
2) SDK 0.85.1，默认 /tmp：1 failed，23.75 秒。
3) SDK 1.2.0，显式 /private/tmp：1 passed，9.58 秒。
4) SDK 0.85.1，显式 /private/tmp：1 passed，9.61 秒。

两个默认目录失败都发生在第 7 步的 lifecycle.start。
IsolatedHarness.ipc 在 recv 处抛 TimeoutError。
补充诊断中，目标 worker 以 exit 0 退出，未完成 RPC ready。
20 秒后 ps 仍为 running=false，并保留启动失败原因。

独立入口探针复制同一个 pi_controlled_loader.mjs。
用 /tmp 别名调用时，脚本不执行 main，直接 exit 0。
用 /private/tmp 调用时，main 执行并报告预期的空参数错误。
原因是 import.meta.url 使用真实路径，入口守卫比较的 argv 路径未消除别名。
入口守卫位于文件第 488 行，git blame 指向既有提交 6228a018fe。
本轮 3 个返工提交没有修改该守卫。

因此，本次使用仓库支持的 HYPRIAL_TEST_TMP_ROOT 显式选择真实短路径。
默认 /tmp 的入口别名问题仍是已复现的环境限制。
不能声称 transfer 的 1.2.0 失败证明 SDK 版本不兼容。
固定版本 probe 的 1.2.0 != 0.85.1 断言失败则另有独立证据。

5. 缺 SDK 的 skip/raise 约定

状态：PASS，检查的是约定，没有把 skip 计作正例通过。

transfer 与 controlled-loader 的 SDK 定位和强制开关逻辑一致。
没有 Node 或相邻 Pi SDK 时：
1) 未设 HYPRIAL_REQUIRE_PI_SDK：transfer Pi 正例显式 skip。
2) 设了 HYPRIAL_REQUIRE_PI_SDK：模块收集阶段抛 RuntimeError。

实际执行的隔离 PATH 矩阵：
1) 有 Node、无 Pi SDK：transfer 为 1 skipped，说明缺真实 Node/SDK。
2) 有 Pi SDK、无 Node：transfer 为 1 skipped，说明相同。
3) 无 Pi SDK：controlled-loader 单项为 1 skipped，说明相同。
4) 强制开关且无 Pi SDK：两个模块各抛 RuntimeError，共 2 个收集错误。
5) 强制开关且无 Node：同样是两个模块各抛 RuntimeError。

强制开关的错误原文为：
HYPRIAL_REQUIRE_PI_SDK is set but node or the pi SDK is missing

真实 SDK 正例与整组焦点均设置强制开关，均没有 skip。

6. A、B、C1、C3 与门禁

状态：抽检 PASS；结构门按任务指定排除 3 项既有违规。

A 的参数合并、短长别名与共享值测试通过。
B 的 workspace 信任与链接矩阵随 Pi 焦点通过。
C1 的 same-entity restore ABA 用例通过。
C3 的 supervision、provider-term lint 与 authority migration 测试通过。

抽检命令：
uv run --with pytest-timeout pytest tests/identity/agents/registry/entity/test_lifecycle.py tests/daemon/application/composition/composition/test_final_composition_lifecycle.py tests/tooling/architecture/migration/test_authority_migration.py tests/tooling/architecture/boundary/test_supervision_boundary_gate.py tests/tooling/architecture/lint/test_provider_term_lint.py -q --timeout 120 --tb=short

结果：81 passed，24.90 秒。

真实 SDK 焦点使用第 4 节的 PATH、临时目录根与强制开关：
uv run --with pytest-timeout pytest tests/daemon/harnesses/pi tests/integration/transfer/p2/e2e/test_two_daemon_chain.py -q --timeout 120 --tb=short

结果：209 passed，115.32 秒，无 skip。
包含完整 transfer 文件的 Pi 臂与 Claude 臂。
默认本机环境的同组结果另记为 2 failed、207 passed。
两个失败分别是固定版本断言与上述临时目录入口别名问题。

结构门原始结果：FAIL，仅有指定排除的 3 项：
1) agents_admin/registry_ops.py：1005 行。
2) tests/daemon/outbox：8 个直接文件。
3) contract/e2e/_daemon_runner.py：1004 行。
没有新增结构违规；rpc_client.py 为 974 行，loader.py 为 710 行。
边界门：PASS，violations=[]。
ruff：PASS，All checks passed。

门禁命令：
uv run --no-project --python 3.12 python scripts/ci/check_structural_limits.py
uv run --no-project --python 3.12 python scripts/ci/check_module_boundaries.py
uv run ruff check .

7. 全量失败 ID 核对

全量状态仍为 FAIL；本次按任务范围不重跑全量。
已核对 be-dev-2 的 /private/tmp/hf-pitrust-full4.log：
5 failed、12984 passed、14 skipped、5 xfailed、1 xpassed。
耗时 2416.16 秒。

4 个既有失败 ID 与三次判词一致：
1) tests/daemon/harnesses/pi/configuration/test_pi_controlled_loader.py::test_t04_t05_t06_real_sdk_loads_c_and_trusted_project_with_git_boundary
   实际 1.2.0，固定断言要求 0.85.1。
2) tests/daemon/ipc/cost/test_ipc_cost_counters.py::test_four_sides_cover_a_pure_heartbeat_load
   CPU 覆盖率低于 0.70。
3) tests/kernel/platform/test_platform_actor_preservation.py::test_posix_listener_keeps_request_owner_and_reserve_fd
   OSError: AF_UNIX path too long。
4) tests/tooling/architecture/authority/test_inbox_authority_gate.py::test_current_raw_bypasses_are_exactly_in_the_shrinking_manifest
   forwards.py:_BridgeForwardsMixin._log_missing_failure_route:alarm_emitter 未登记。

本次将这 4 项在当前分支一起复跑：4 failed，5.76 秒。
原因与上列一致，CPU 计数为 6.423/10.209。
本次没有重跑基线，沿用三次判词对这 4 项的双树对照。

第 5 项为：
tests/integration/transfer/p2/e2e/test_two_daemon_chain.py::test_the_chain_lands_on_the_target_and_retires_the_source[pi]

全量中的失败同样是第 7 步 IPC recv 超时。
本次以第 4 节的双版本、双目录实测解释环境差异。
固定 SDK 与真实短路径下，完整 Pi 正例和焦点均已通过。

8. 证据与验收边界

仓库证据目录：
docs/notes/pi-p2-launch-trust-acceptance-4-2026-10-17/。
包含新 8 项复现、入口别名探针、原始输出选集与结果摘要。
临时原始输出位于 /private/tmp/pi-p2-fourth-independent。
边界门原始输出超过 1 MB，没有写入仓库。
pytest 原始输出保留了 4 行尾随空格。
暂存区 diff --check 因此报告 4 项，按 WORM 原样保留。
delivery.log 的 diff --check 在暂存前执行。
该次检查未覆盖当时未跟踪的证据，不作为原始输出格式通过依据。

复现 8 项检查：
bash docs/notes/pi-p2-launch-trust-acceptance-4-2026-10-17/run-clean.sh env PYTHONPATH="$PWD:$PWD/src" uv run --with pytest-timeout pytest docs/notes/pi-p2-launch-trust-acceptance-4-2026-10-17/reproduce.py -q -s --timeout 120 --tb=short

上述命令使用同目录 run-clean.sh 清除继承环境。
包装器隔离 HOME、HYPRIAL_HOME、状态目录与 npm cache。
包装器清除模型凭据、worker 会话变量、代理与 Git 配置注入。
所有测试 daemon 使用 fixture 的独立 home、socket 和回环端口。
诊断 ps 也确认 zenoh.isolated=true。
测试进程由各自 fixture 清理，没有操作生产 daemon。
没有读取真实凭据或用户配置，没有修改全局 npm 安装。
没有修改旧 WORM 证据，也没有提交大于 1 MB 的原始输出。

真实模型认证、完整生产 CLI 正例与 custom build 等价性仍为 UNKNOWN。
无相邻 SDK 的 P2 custom command 现在属于明确拒绝范围。
本轮不以真实模型或恶意同 UID 程序作为验收目标。

Contract-Impact: 仅新增验收文档与证据，不改变字段或错误码。
Skill-Impact: 未改变 agent 操作方式，无需修改 skill。
未 push，未开 PR，未向任何 agent 发消息。
