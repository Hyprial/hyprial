Pi P2 启动信任三次验收（2026-10-16）

判词：REJECTED。

C2 的正向断言已恢复，先红证据成立。
基线“假成功”的根因说法也成立。
但 d47765c55 用自动直跑 RPC 替代受控加载，新增了 P2 配置边界回归。
普通转发脚本就能触发回归，不需要恶意节点。
按裁定 94 的“只防误操作，不防恶意节点”，本项仍阻断验收。

验收对象：fix/pi-p2-launch-trust，产品 HEAD d47765c55ff482a9505571ac133820ab41b100c6。
红提交：260c303f0515cf253660fb18e8c88f90e20adaa8。
对照基线：62465dbec549f72cc378c0d206504471e816b5e0。
二次判词：9f28bc1fe，docs/design/pi-p2-launch-trust-acceptance-2-2026-10-16.md。
本次只检查 C2 及其修法，没有修改产品或 tests/。

1. 阻断项：普通 Pi 包装脚本改变 P2 加载范围

状态：FAIL。

位置：
src/hyprial/daemon/impl/harnesses/pi/loader.py，find_pi_package_root_or_none。
src/hyprial/daemon/impl/harnesses/pi/rpc_client.py，PiRpcClient.__init__。

新增 helper 捕获 find_pi_package_root 的 ValueError，返回 None。
PiRpcClient 随即使用普通 CLI 的 RPC 模式。
此分支不调用 pi_sdk_launch_from_runtime_context。
此分支也不运行 pi_controlled_loader.mjs。

包查找只检查 command[0] 的真实路径及祖先目录。
独立 shell 包装脚本没有 SDK 包祖先，就会触发直跑。
默认 command=("pi",) 也受影响，不限于显式自定义 command。

独立反例只使用以下普通转发脚本：

#!/bin/sh
exec /opt/homebrew/bin/pi "$@"

脚本没有修改 HOME、PI_CODING_AGENT_DIR 或参数。
将脚本目录放到 PATH 首位，再使用默认的 pi 命令。
同一个真实 P22 context、同一个 cwd、同一个完整子环境，得到：

1) 原安装命令使用 SDK bridge；包装脚本使用直跑 RPC。
2) SDK bridge 读取显式配置 C，默认模型为 gpt-4o-mini。
3) 包装脚本忽略 C 的 AGENTS.md，改读 nativeRoot/settings.json。
4) 包装脚本的默认模型变成 gpt-4o。
5) SDK bridge 排除 HOME/.agents/skills 中的诱饵 skill。
6) 包装脚本把同一诱饵 skill 加入 get_commands。
7) SDK bridge 排除 Git 根目录以外的祖先 AGENTS.md。
8) 包装脚本把同一祖先文件写入系统提示词。

上述反例无需链接文件，也无需恶意包装脚本。
因此，即使不考虑凭据泄漏，也已违反 P2 的显式配置与项目边界。

另一个反例把 cwd/AGENTS.md 链接到临时合成用户目录的 auth.json。
合成文件只含 ACCEPT3_SYNTHETIC_GLOBAL_SYMLINK，不含真实凭据。
SDK bridge 拒绝，错误为：
context candidate is not a regular non-symlink file
直跑 RPC 启动成功，系统提示词包含合成凭据标记。
测试只输出是否包含标记，没有输出文件内容。

会话路径也发生变化：
SDK bridge：context.roots.session_root，即 agent-home/state/pi。
直跑 RPC：nativeRoot/sessions/<cwd 编码>。
本次实际 get_state 返回了这两种不同路径。
这与 P2 transfer locator 消费的 session_root 不一致。
本次未执行后续再次迁移，不把路径风险冒充迁移失败实测。

2. SDK bridge 与直跑 RPC 的逐项差异

状态：环境替换仍有效；受控资源加载不等价。

2.1 HOME 与完整环境

两条路径都使用 complete_launch.environment.for_exec()。
独立测试逐字比较完整环境，结果相等。
HOME 仍指向本 agent 的 tool_home。
PI_CODING_AGENT_DIR 仍指向本 agent 的 native_root。
没有恢复 os.environ 合并，没有自动改回操作员 HOME。

2.2 nativeRoot 与 projectionRoot

SDK bridge 分开使用两根目录。
设置、AGENTS.md 与 skill 从 projectionRoot 读取。
凭据与模型存储从 nativeRoot 读取。

直跑只得到 PI_CODING_AGENT_DIR=nativeRoot。
普通 Pi CLI 把 nativeRoot 同时当作用户配置与资源目录。
projectionRoot 没有作为 CLI 加载参数传入。
这会忽略 C，并允许读取 nativeRoot 中的配置残留。
SDK launch 对两根目录必须不同的检查也不再执行。
身份层预先生成的目录和完整环境没有因此消失。

2.3 auth.json 与 models.json

对本次使用的未改造 Pi 1.2.0，两条路径仍指向 nativeRoot 下的文件。
SDK bridge 显式传入 authPath、modelsPath、modelsStorePath。
普通 CLI 经 PI_CODING_AGENT_DIR 选择同一个 nativeRoot。
SDK bridge 还注册 hyprial 的 pi_model_definitions；直跑不注册。
本次没有发模型请求，不声称验证了真实认证或全部模型等价性。

普通转发脚本本身不会自动读取操作员 ~/.pi/agent/auth.json。
本次没有发现完整环境替换失效，也没有读取真实用户凭据。
但直跑不再执行受控 context 文件的链接拒绝。
合成用户 auth.json 经项目链接进入系统提示词，已实测。

若脚本主动重设 HOME 或忽略 PI_CODING_AGENT_DIR，也能读取其他路径。
这属于受信本机程序主动越界，不是本次阻断依据。
同 OS 用户权限本来就不防恶意进程。

2.4 项目信任收据与 --approve

两条路径都先经过 resolve_approved_pi_project。
缺少 --approve 时，两条路径均同步抛 PiProjectTrustError。
原 npm 安装与包装脚本的负例均通过。

SDK bridge 将 Python 收据中的 cwd、repositoryRoot、trustScope 传给 JS。
JS 限制 context 文件与 skill 的读取范围，并写出 loader receipt。
直跑仍计算 Python 收据，但不将收据交给资源加载器。
直跑把 --approve 原样交给 Pi CLI，由上游执行常规项目发现。
直跑不生成受控 loader receipt。
不能把“先算过收据”当作“随后按收据加载”的证明。

2.5 B 的 lstat 与链接防御

Python 的 workspace、agent-home、agents 目录检查仍在分支选择之前。
本次分别用原安装命令和包装脚本复测：
真实 0700、非 Git 的本 agent workspace 可创建 client。
将 workspace 替换为链接后，两条路径都拒绝。
Pi 焦点测试也覆盖了既有 workspace 链接矩阵。
因此，首验 B 的静态目录防御没有被这次修改删除。

但 JS 的 context 文件 strictRead、O_NOFOLLOW 与 skill 链接检查没有运行。
直跑也没有执行 assertStaticResourceSettings 的动态资源拒绝。
B 的 Python 路径检查通过，不等于整套受控加载仍然有效。
本次未验证并发替换路径的竞态。

2.6 全局 npm 安装是否误入直跑

原安装路径、真实 CLI 文件路径、指向 CLI 的符号链接均走 SDK bridge。
find_pi_package_root 会先 resolve，能找到：
/opt/homebrew/lib/node_modules/@earendil-works/pi-coding-agent
实际包版本为 1.2.0。
这 3 种命令形式均已断言 argv 使用 pi_controlled_loader.mjs run-rpc。
普通 shell 包装脚本不具备上述祖先关系，即使最终执行的仍是同一安装。
因此，“真实安装可正确识别”不能覆盖“PATH 包装脚本安全”等价性。

3. 裁定 94 与修复要求

状态：阻断。

本次不要求阻止恶意节点或恶意同 UID 程序。
阻断依据是普通转发脚本就会静默改变 P2 的配置与项目发现范围。
包装脚本保留完整环境和所有参数，仍会丢失 C、读取祖先 context。
这属于需要防止的误操作，不在恶意节点豁免范围内。

C2 需要保留正向断言，但不能用取消受控加载来使协议 double 通过。
修复必须保证 P2 的加载边界与 SDK bridge 等价。
无法提供这种保证的命令必须明确拒绝，不能自动降级。
测试 double 可适配受控启动接口，但不得成为产品的隐式绕过条件。
standalone/custom Pi 的等价加载与会话定位需要独立证据。

4. C2 断言与先红证据

状态：PASS。

260c303f0 是 d47765c55 的直接父提交。
时间分别为 2026-10-16 23:54:52 与 2026-10-17 00:05:44（UTC+8）。
前者只恢复第 7 步，后者只修改 loader.py 与 rpc_client.py。

当前测试保留以下断言：
1) 未带 --approve 的 lifecycle.start 同步返回 PI_P2_LAUNCH_UNTRUSTED。
2) 错误消息包含 explicit --approve trust。
3) 带 --approve 调用目标 daemon 的 lifecycle.start。
4) 通过 ps 等待目标 worker running。
5) 目标启动后，源端 worker 不运行。

当前测试与 62465dbec 的差异只涉及同步负向拒绝适配及旧轮询 helper 删除。
第 7 步的正向启动与双端 ps 断言没有放松。

本次重新执行红提交的完整 Pi 用例：
1 failed，23.87 秒。
失败发生在 test_two_daemon_chain.py:477 的目标 lifecycle.start。
IsolatedHarness.ipc 的 recv 抛 TimeoutError。
这正是恢复的正向步骤，没有用导入失败代替先红。

当前分支的完整 transfer 文件随焦点套件执行，全部通过。
独立诊断副本保留全部断言，额外等待 3 秒并读取 ps 与操作表。
当前副本为 1 passed，10.31 秒。
目标此时仍 running=true，并有真实协议 double 子进程 PID。
负向操作为 compensated，错误码正确；正向操作为 completed。
这些测试使用真实双 daemon 与协议 double，不调用真实模型。

5. 基线假成功的独立复核

状态：根因说法成立。

在 detached 62465dbec 上执行原断言的诊断副本：
1 passed，9.92 秒。
lifecycle.start 返回 ok=true，操作表为 completed。
原 wait_running 断言也通过。
但额外等待 3 秒后，目标 ps 为：

running: false
pid: null
state: retrying
error: Pi SDK package root not found beside <CPython 3.12.13>/bin/python3.12

因此，基线正例通过不能证明 worker 就绪。
基线 StreamingTurnProcess.running 在 client 尚未建立、last_error 尚空时返回 true。
懒加载随后失败，却没有撤销已经 completed 的 lifecycle 操作。

字段需要区分：进程内部属性名为 last_error，ps 中输出为 error。
该映射可见 harnesses/actor/contracts.py 的 status()。
本次成功响应本身没有 data.lastError，不能声称成功响应带了该字段。
已证实的事实是：最后启动错误存在，但生命周期仍结算成功。

同步 P2 启动使“Python 协议 double 旁没有 Pi SDK”的失败变得可见。
这解释了 C2 红提交，不能据此证明直跑修法保留了 P2 的加载保证。

6. 门禁与焦点结果

结构门：原始结果 FAIL，仅有允许排除的 3 项既有违规。
1) agents_admin/registry_ops.py：1005 行。
2) tests/daemon/outbox：8 个直接文件。
3) contract/e2e/_daemon_runner.py：1004 行。
本次没有新增结构违规。
rpc_client.py 为 981 行，loader.py 为 723 行。

边界门：PASS，exit 0。
ruff：PASS，All checks passed。

执行命令：
uv run --no-project --python 3.12 python scripts/ci/check_structural_limits.py
uv run --no-project --python 3.12 python scripts/ci/check_module_boundaries.py
uv run ruff check .

焦点命令：
uv run --with pytest-timeout pytest tests/daemon/harnesses/pi tests/integration/transfer/p2/e2e/test_two_daemon_chain.py -q --timeout 120 --tb=short

结果：1 failed、206 passed，105.09 秒。
唯一失败为真实 SDK 固定版本断言：实际 1.2.0，期望 0.85.1。
没有 skip；transfer 文件的 Pi 和 Claude 两组用例均执行。

独立隔离比较：8 passed，3.42 秒。
提交的复现文件再次执行：8 passed，2.67 秒。
这 8 项测试通过表示已确认分支差异，不表示隔离验收通过。

7. 全量失败 ID 核对

本次不重复全量。
已核对 be-dev-2 的 /private/tmp/hf-pitrust-full3.log：
4 failed、12983 passed、14 skipped、5 xfailed、1 xpassed。
耗时 2438.10 秒。

本次把全部 4 个失败 ID 在当前分支与基线各复跑 1 次。
结果分别为 4 failed（6.00 秒）与 4 failed（5.66 秒）。
两棵树的失败 ID 与原因一致：

1) tests/daemon/harnesses/pi/configuration/test_pi_controlled_loader.py::test_t04_t05_t06_real_sdk_loads_c_and_trusted_project_with_git_boundary
实际 SDK 1.2.0，固定断言要求 0.85.1。

2) tests/daemon/ipc/cost/test_ipc_cost_counters.py::test_four_sides_cover_a_pure_heartbeat_load
CPU 计数覆盖率没有达到 0.70。
当前为 6.963/10.133，基线为 6.340/9.543。

3) tests/kernel/platform/test_platform_actor_preservation.py::test_posix_listener_keeps_request_owner_and_reserve_fd
OSError: AF_UNIX path too long。

4) tests/tooling/architecture/authority/test_inbox_authority_gate.py::test_current_raw_bypasses_are_exactly_in_the_shrinking_manifest
forwards.py:_BridgeForwardsMixin._log_missing_failure_route:alarm_emitter 未登记。

复跑命令为 uv run --with libcst==1.9.0 --with pytest-timeout pytest，
参数是上述 4 个完整 ID，以及 -q --timeout 120 --tb=short。
本次结果支持这 4 项不是本轮新增失败。
这不消除独立反例暴露的受控加载回归。

8. 可复现证据与隔离边界

提交的证据目录：
docs/notes/pi-p2-launch-trust-acceptance-3-2026-10-16/

reproduce.py 保存 8 项模型无关的分支比较。
命令：
PYTHONPATH="$PWD:$PWD/src" uv run --with pytest-timeout pytest docs/notes/pi-p2-launch-trust-acceptance-3-2026-10-16/reproduce.py -q -s --timeout 120 --tb=short

执行前清除继承的 HYPRIAL_*、HARNESS_*、PI_* 与模型凭据变量。
使用隔离 HOME，并设置 HYPRIAL_NETWORK_ISOLATED=1。
本次包装器为 /private/tmp/pi-p2-third-independent/run-clean.sh。
模型比较均使用 --offline；没有发送模型请求。

make_diagnostic.py 只生成测试副本，不改被测 worktree。
用 uv run python make_diagnostic.py ROOT OUTPUT 生成副本。
在 ROOT 中设置 PYTHONPATH="$PWD:$PWD/src"，再执行：
uv run --with pytest-timeout pytest -p tests.conftest 'OUTPUT::test_the_chain_lands_on_the_target_and_retires_the_source[pi]' -q -s --timeout 120 --tb=short
基线和候选副本均保留各自版本的负向及正向断言。

本次原始日志：/private/tmp/pi-p2-third-independent。
解释器：CPython 3.12.13；Pi：npm 全局安装 1.2.0。
Node 使用 PATH 首位的 v24.21.0。
基线和红提交均通过 PYTHONPATH 导入各自源码与 fixture。
uv 使用本 worktree 的锁定依赖，没有切回系统 Python。

所有 daemon 使用 fixture 独立 home、socket 与回环端口。
独立资源比较只启动离线 Pi 子进程，全部等待退出。
测试自有 daemon 已由 fixture 清理，未遗留本次测试进程。
没有访问生产 daemon、真实凭据或用户配置内容。
没有修改既有 WORM 证据，没有提交大于 1 MB 的原始输出。
完整生产 CLI 正向运行、真实模型认证、custom build 等价性仍为 UNKNOWN。

Contract-Impact: 仅新增验收文档与独立复现证据，不改变字段或错误码。
Skill-Impact: 未改变 agent 操作方式，无需修改 skill。
未 push，未开 PR，未向任何 agent 发消息。
