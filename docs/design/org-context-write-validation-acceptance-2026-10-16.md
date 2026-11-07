org-context 写前校验验收，2026-10-16

结论：REJECTED。

479ae9262 已实现本机 adopt/stage 写前校验和 validate CLI。
规格追加的采纳状态校验、缺记录读取回退均未实现。
这 2 项缺失已足以判定 REJECTED。
orgfs 发布还存在未校验的候选写入路径。
本次只提交验收文档，不修改产品或测试。

1. 验收对象与边界

工作目录：/private/tmp/hf-orgctx-validate。
分支：feat/org-context-write-validation。
实现基线：038763c03。
盘点提交：ddd66e549。
先红测试提交：ba4c16e3f。
实现提交：479ae92624dd2dd976b9b4f77779a1a0a871fddf。
失败对照：独立 detached worktree，固定在 62465dbec。
不使用验收期间继续移动的 origin/dev 指针代替固定对照。

解释器为 CPython 3.12.13，Node 为 v24.21.0。
所有 Python 命令都经 uv run 执行。
未启用 extensions.worktreeConfig，未使用 git config --worktree。

手工探针使用 /private/tmp 下的独立 home 和 state。
测试使用仓内隔离 fixture，并设置 HYPRIAL_NETWORK_ISOLATED=1。
未人工读取生产损坏备份、凭据或用户配置。
未执行针对 ~/.hyprial 的文件写操作。
工作流 inspect/complete 仅用于验收任务的领取与回执。
未 push，未开 PR，未给其他 agent 发消息。

2. 必须返工的项目

R1，采纳状态校验缺失。

位置：src/hyprial/shell/impl/cli/commands/org/commands.py:65-127。
validate 只读取正文并调用 validate_document。
成功分支直接返回 ok:true、valid:true。
代码未读取采纳记录或 publish-pending。

独立探针逐项得到退出码 0、ok:true、valid:true：
- 当前正文没有对应 org/adoptions/<sha256>.json。
- 采纳记录的 version 与正文不一致。
- 采纳记录的 publisher 与正文不一致。
- 采纳记录的 documentSha256 与正文不一致。
- 采纳记录的 adoptedAt 无效。
- 采纳记录不是合法 JSON。
- 手改正文后，当前 sha256 没有采纳记录。
- publish-pending 指向 64 个 0，而非当前正文。
- publish-pending 不是合法 JSON。

以上场景均使用默认已采纳槽，未使用 --path 绕过状态检查。
返工要求：区分格式校验与已采纳槽一致性校验。
默认槽必须核对原始字节摘要、采纳记录和发布标记。
摘要不得用换行归一化后的文本代替原始文件字节。
状态不一致时返回 ok:false，并退出 1。
--path 预检的状态适用范围必须在帮助中说明。

R2，org show/status 缺采纳记录时没有回退。

位置：src/hyprial/daemon/impl/org/store.py:198-221、331-340。
位置：src/hyprial/shell/impl/cli/commands/org/commands.py:615-691。
acceptance_record 在缺记录时抛出 OrgStoreError。
show/status 没有处理该错误并保留正文输出。

实测：正文合法，采纳记录缺失。
org show --full --json 和 org status --json 均退出 1。
stdout 均为单个 ok:false 对象。
错误为 cannot read acceptance metadata: [Errno 2] No such file or directory。
两条命令均未输出正文信息，也未输出回退警告。
show/status 的实现与 62465dbec 相同，属于追加规格未交付。

返工要求：缺记录时从可解析正文恢复可展示字段。
同时输出明确警告，不编造 adoptedAt 或采纳证明。
JSON 模式仍须维持单对象输出。

R3，orgfs 候选发布漏过写前校验，盘点也未列出该写路径。

位置：src/hyprial/daemon/impl/org/orgfs_migration.py:172-222。
publish_accepted 直接读取已采纳槽的原始字节。
随后调用 facade.write_text 写入 context/candidates/<user>.md。
新建和覆盖两个分支均未调用唯一校验入口。
该方法还会写 publish-pending 并在成功后清除标记。

新建、覆盖候选两个分支均使用真实 OrgFsRuntime 实测。
DaemonApplication 使用隔离 home 和 state：
- 将 escaped-brace fixture 放入临时已采纳槽。
- 调用 OrgContextOrgFsBridge.publish_accepted。
- 返回 True。
- candidates/owner.md 中的字节与损坏 fixture 相同。
- validate_document 对发布后的内容返回 ok:false。

该路径还可由 orgfs.create/context 与 orgfs.join 调用。
入口见 application/messaging/orgfs_bridge.py:336、552。
盘点文档只列了本机 adopt/stage，未列出此发布写路径。
本机已采纳槽仍只有 adopt 一个写入者；此发现不改变该判断。

返工要求：补盘点 orgfs 发布写入路径。
发布前校验即将写入的原始正文，拒绝损坏内容。
拒写测试必须同时覆盖新建和覆盖候选的分支。
该路径在 62465dbec 已存在，属于覆盖缺口，不称为新增回归。

R4，无效 YAML 日期会穿透非抛出式校验接口。

位置：src/hyprial/daemon/impl/org/document.py:314-317。
yaml.load 只捕获 yaml.YAMLError。
将合法正文中的 issued_at 改为 2026-99-99：
validate_document 抛出 ValueError: month must be in 1..12。
CLI 虽退出 1，但仅返回：
{"ok":false,"error":"month must be in 1..12"}
该结果缺少承诺的 line、column 和 reason。

返工要求：将无效 YAML 日期转换为统一的失败结果。
必须增加先红测试，验证非抛出式接口和 CLI 定位信息。
旧 parser 已有同类日期抛出问题。
本项针对新接口“坏输入不抛出”的承诺，不称为新增解析回归。

3. 已通过的检查与限制

读写盘点：本机已采纳槽的读者与 adopt/stage 已核对。
mesh、orgfs 拉取、init/login、Lark 摘要和存在性检查均有记录。
新增 validate 是新增读者。
orgfs 发布读原始字节并写候选的路径缺失，见 R3。

唯一格式校验入口：PASS。
parse_document 委托 validate_document。
原有 schema 检查与唯一键 YAML loader 由该入口复用。
R4 所述输入仍会抛出异常，不能据此声称所有坏输入均已覆盖。

当日损坏模式定位：PASS，仅指仓内脱敏 fixture。
escaped-brace 样本返回第 20 行、第 33 列。
multiline-flow 样本返回第 19 行、第 69 列。
未读取生产备份，不声称已逐字节比对生产原件。

本机 adopt/stage 拒写：PASS。
两个入口都在 ensure_layout 和 _atomic_write 前调用校验。
独立探针分别验证两个入口均拒绝损坏 source。
错误均包含 line 20, column 33，目录内容保持不变。
orgfs 发布不能归入该通过结论，见 R3。

CLI 输出纪律：PASS，限已覆盖的格式错误和正常路径。
新命令通过 CliResult 与 services._execute 输出。
未发现直接 print、typer.echo 或 stdout 写入。
焦点测试验证 --json 恰好一个对象。
坏 fixture 为 ok:false、退出 1；合法 --path 为退出 0。
缺文件为 ORG_CONTEXT_ABSENT、退出 1。
R1 是错误判为有效的问题，不是输出封装问题。

CLI 动态清单：PASS。
org --help 已显示 context，context --help 已显示 validate。
check_cli_help_inventory.py 返回：
PASS Typer/help top-level inventory match: 62 commands (56 visible, 6 hidden)
该门只核一级清单，不能代替二、三级 help 检查。
2026-10-04 的历史盘点仍只列 show/status/fetch/import。
返工时须补当前 validate 使用指引和采纳状态语义。
不要求改写 WORM 历史证据。

先红测试：有意义，但不足以证明全部规格。
已在独立 ba4c16e3f detached worktree 亲自复跑。
格式校验测试因缺少 validate_document 而收集失败。
CLI 的 4 个测试均失败，原因是 No such command 'context'。
这不是依赖安装失败。
另用旧 store 分别执行 adopt/stage，均能写出损坏 fixture。
当前提交的对应焦点测试通过，独立拒写探针也通过。

不足：原默认槽测试直接写合法正文，却不创建采纳记录。
该测试仍断言 validate 成功，与追加规格冲突。
R1-R4 的返工必须先增加失败测试，再修实现。

4. 验证命令与结果

统一环境清理方式如下。
先清除全部 GIT_CONFIG_* 环境变量。
再清除 HYPRIAL_AGENT_DIR，并设置临时 HYPRIAL_HOME、HARNESS_STATE_DIR。
同时设置 HYPRIAL_NETWORK_ISOLATED=1。
清除 HARNESS_SOCKET_PATH 和 HARNESS_CONFIG。
以下命令也清理派单指定的 worker 和代理变量：

env -u HYPRIAL_NODE_ID -u HYPRIAL_MANAGED_WORKER -u HYPRIAL_WORKER_ACTOR -u HYPRIAL_WORKER_SESSION_REF -u HYPRIAL_TOOL_PROFILE_ID -u HTTP_PROXY -u HTTPS_PROXY -u ALL_PROXY -u http_proxy -u https_proxy -u all_proxy NO_PROXY=127.0.0.1,localhost no_proxy=127.0.0.1,localhost PATH=/Users/bobadmin/.npm/_npx/387698761821791d/node_modules/node/bin:$PATH <下列命令>

uv run --no-project --python 3.12 python scripts/ci/check_structural_limits.py
结果：FAIL，2 项既有违规。
registry_ops.py 为 1005 行，上限 1000 行。
tests/daemon/outbox 有 8 个直接文件，上限 7 个。
62465dbec 对照结果完全相同。
这两个路径相对实现基线也没有变化。
本次变更未新增结构违规，但结构门不能记为 PASS。

uv run --no-project --python 3.12 python scripts/ci/check_module_boundaries.py
结果：PASS，退出 0。

uv run ruff check .
结果：PASS，退出 0。

npm --prefix src/gui ci --ignore-scripts
npm --prefix desktop ci --ignore-scripts
结果：均为 PASS，退出 0；使用 Node 24。
两项安装在全量测试前完成。

uv run --with pytest-timeout pytest tests/daemon/organizations tests/shell/cli/test_cli_org.py tests/shell/cli/test_cli_org_context_validate.py tests/daemon/application/ports/test_daemon_org_context.py tests/shell/cli/output/test_output_discipline.py -q --timeout 600
结果：PASS，129 passed，2 warnings，10.11 秒。
这是最终提交前的焦点复跑；此前同一焦点也通过。

uv run --with libcst==1.9.0 --with pytest-timeout pytest tests/ -q --timeout 600
结果：FAIL，退出 1。
6 failed，12929 passed，14 skipped，5 xfailed，1 xpassed。
另有 14 warnings；耗时 2670.17 秒（44 分 30 秒）。
全部 12955 项均有结果，没有用中断运行代替全量结论。

其中 5 个失败，已在固定对照 62465dbec 复跑。
对照运行 2 次均为 5 failed；受验提交焦点复跑也为 5 failed。
下列失败均有基线复现证据，不归为本次新增回归：

- tests/daemon/harnesses/pi/configuration/test_pi_controlled_loader.py::test_t04_t05_t06_real_sdk_loads_c_and_trusted_project_with_git_boundary
  实测 Pi SDK 版本为 1.2.0，测试预期为 0.85.1。
- tests/daemon/ipc/cost/test_ipc_cost_counters.py::test_four_sides_cover_a_pure_heartbeat_load
  instrumentedCpuMs 与进程 CPU 毫秒数的增量比低于 0.70。
  这是本机基线可复现的计时阈值失败，不推断其他环境必现。
- tests/kernel/platform/test_platform_actor_preservation.py::test_posix_listener_keeps_request_owner_and_reserve_fd
  错误为 OSError: AF_UNIX path too long。
  改用短 --basetemp 后，基线和受验提交均为 1 passed。
- tests/tooling/architecture/authority/test_inbox_authority_gate.py::test_current_raw_bypasses_are_exactly_in_the_shrinking_manifest
  清单遗漏 _BridgeForwardsMixin._log_missing_failure_route:alarm_emitter。
- tests/tooling/architecture/lint/test_provider_term_lint.py::test_remaining_provider_hits_are_all_whitelisted
  envelope.py 注释中的 user-provider 未在词表白名单中。

第 6 个失败为 Pi 迁移链测试：
tests/integration/transfer/p2/e2e/test_two_daemon_chain.py::test_the_chain_lands_on_the_target_and_retires_the_source[pi]
错误包含 Pi SDK package root not found beside，失败在目标 worker 启动断言。
受验提交单项复跑 1 次通过、1 次失败。
对照 worktree 的默认解释器下，单项复跑 2 次均通过。
对照改用受验 .venv 对应的同一基础解释器后，1 次失败、1 次通过。
两边失败均报告相同 SDK package root 问题。
受验提交改用对照基础解释器后，单项复跑通过。
两个基础解释器均为 CPython 3.12.13，但安装路径不同。
因此记为基线也可复现的间歇失败，不判新增稳定回归。
间歇失败的根因仍为 UNKNOWN，单项通过不抵消全量失败。
同解释器对照使用独立 UV_PROJECT_ENVIRONMENT，没有改动生产安装。

对照命令：在 /private/tmp/orgctx-control-62465 执行
uv run --with pytest-timeout pytest <上述完整测试 ID，空格分隔> -q --timeout 600。
使用与全量相同的环境清理方式。
短路径复查仅追加 --basetemp=/private/tmp/oc-base-short 或 oc-head-short。
同解释器对照先执行：
UV_PROJECT_ENVIRONMENT=/private/tmp/orgctx-control-same-python uv sync --directory /private/tmp/orgctx-control-62465 --python /private/tmp/hf-orgctx-validate/.venv/bin/python --extra test
再使用同一 UV_PROJECT_ENVIRONMENT 执行 Pi 迁移链测试。

固定对照 62465dbec 的额外全量运行也已完成。
命令与受验全量相同，通过 uv run --directory 指向对照 worktree。
对照使用独立 control-home/control-state，运行前也安装 GUI/desktop 依赖。
结果：FAIL，退出 1。
5 failed，12908 passed，14 skipped，5 xfailed，1 xpassed。
另有 14 warnings；耗时 2617.75 秒（43 分 37 秒）。
5 个失败 ID 与上列前 5 项一致；Pi 迁移链在本次对照全量通过。
仅按相同测试 ID 和错误归因，不用总用例数差异推断回归。
未发现可确认的新增稳定回归，但不能据此放行未交付的规格。

14 个 skip 已逐项以 -rs 复查，仍为 14 skipped。
以下场景均不记为 PASS；真实环境能力为 UNKNOWN：
- test_requests.py 的 real_full_kernel_backlog：macOS 不使用 Linux 的 EAGAIN 行为。
- test_agent_sdk_dependency.py 的 pinned_sdk_release：未启用 HYPRIAL_TEST_REAL_AGENT_SDK。
- test_send_gate.py 的 case_variant：本机大小写变体是同一目录。
- test_state_db_connect_race.py 的 connect_failure：本机 FD 观察器看不到 open holder。
- test_worker_channel_smolvm.py 的 real_smolvm_channel：未跑受机器锁保护的真实 VM。
- test_agent_home_p23_cc_native.py 的 real_sdk_and_claude 两项参数：未启用 AGENT_HOME_P23_CC_NATIVE。
- 同文件的 real_tmux_claude_tui：未启用 AGENT_HOME_P23_CC_NATIVE。
- test_smolvm_daemon_vm.py 的 ungranted_provider：缺专用材料、窗口和机器锁。
- 同文件的 real_managed_smolvm_daemon_crash：缺专用材料、监控窗口和机器锁。
- test_smolvm_model_vm.py 的 authorized_model：缺操作员授权、私钥、窗口和机器锁。
- test_smolvm_runtime_vm.py 的 real_persisted_smolvm_runtime：缺材料、监控窗口和机器锁。
- test_transfer_smolvm.py 的 real_smolvm_mapping：缺显式运行材料和机器锁。
- test_plugin_load.py 的 generated_plugin：未启用付费真实 Claude 会话测试。

5. 独立探针的复现方法

以下代码在临时 home 内复现 R1、R2 和 R4。
在受验提交的仓库根执行 uv run python，并传入这些代码。
源码中的 VALID 与本次 CLI 焦点测试使用同一合法样本。

from pathlib import Path
from tempfile import TemporaryDirectory
import json, os, subprocess, sys
from hyprial.daemon import OrgContextStore, parse_document, validate_document

valid = Path('tests/shell/cli/test_cli_org_context_validate.py').read_text().split('VALID = """', 1)[1].split('"""', 1)[0]
with TemporaryDirectory(prefix='orgctx-review-', dir='/private/tmp') as raw:
    home = Path(raw)
    slot = home / 'org-context.md'
    slot.write_text(valid)
    environment = {**os.environ, 'HYPRIAL_HOME': raw, 'HARNESS_STATE_DIR': str(home / 'state'), 'HYPRIAL_NETWORK_ISOLATED': '1'}
    def run(*args):
        result = subprocess.run([sys.executable, '-m', 'hyprial.cli', 'org', *args, '--json'], env=environment, capture_output=True, text=True)
        print(result.returncode, result.stdout.strip(), result.stderr.strip())
    run('context', 'validate')
    run('show', '--full')
    run('status')
    slot.unlink()
    store = OrgContextStore(home)
    record = store.adopt(parse_document(valid))
    adoption = store.adoptions_dir / (record.document_sha256 + '.json')
    original = adoption.read_text()
    for field, wrong in [('version', 999), ('publisher', 'mallory'), ('documentSha256', '0' * 64), ('adoptedAt', 'invalid')]:
        data = json.loads(original)
        data[field] = wrong
        adoption.write_text(json.dumps(data))
        run('context', 'validate')
    adoption.write_text(original)
    store.queue_orgfs_publish('0' * 64)
    run('context', 'validate')
try:
    validate_document(valid.replace('2026-10-16', '2026-99-99'))
except Exception as error:
    print(type(error).__name__, str(error))

以下代码使用真实 orgfs 存储复现 R3 的新建分支。
测试 helper 只替换传输与 mesh endpoint，不替换 orgfs 写入。
在仓库根通过 uv run python 的标准输入执行。
若保存为仓外脚本，须设 PYTHONPATH="$PWD" 才能导入 tests。

from pathlib import Path
from tempfile import TemporaryDirectory
from tests.daemon.organizations.context.test_org_context_orgfs_migration import _application, _space, _close
from hyprial.daemon import validate_document

bad = Path('tests/daemon/organizations/document/fixtures/org-context-corrupt-escaped-brace.md').read_text()
with TemporaryDirectory(prefix='orgctx-orgfs-', dir='/private/tmp') as raw:
    app = _application(Path(raw))
    try:
        space = _space(app)
        (app.hyprial_home / 'org-context.md').write_text(bad)
        bridge = app._require_org_context_bridge()
        print('published', bridge.publish_accepted())
        path = 'candidates/' + bridge.runtime.author.removeprefix('user:') + '.md'
        content, _ = bridge.runtime.facade.read_text(space, path)
        print('corrupt_bytes_written', content == bad)
        print('validator_ok', validate_document(content).ok)
    finally:
        _close(app)

6. 交付与再验收条件

修复 R1-R4，并先提交各项失败测试。
再次执行结构门、边界门、ruff、焦点与全量测试。
结构门的既有违规必须单独保留，不以“无新增”替代 PASS。
真实生产原件与生产 daemon 不在本次验收授权内。
未运行的真实环境场景为 UNKNOWN，不用 unit 或 skip 代替。

Contract-Impact: 本次仅新增验收文档，不改变产品字段或契约。
Skill-Impact: 本次仅记录验收；实现返工须补 validate 使用说明。
