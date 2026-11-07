# pi P2 启动信任修复验收（2026-10-16）

判词：REJECTED。

A 引入了显式参数合并回归。B 的信任函数接受跨 agent 的 workspace 符号链接。
C 的同步拒绝与错误输出通过独立复核。
全量另有 3 项新增失败。修复后需要重新验收。

验收对象：fix/pi-p2-launch-trust，HEAD 527a1ca70。
分支起点：6de5cdc00。指定对照：detached 62465dbec。
验收者未修改产品源码、测试或既有根因 note。

## 阻断项 A：显式短选项留下旧值

位置：src/hyprial/kernel/impl/facts/launch_spec.py:49-89。

merge_harness_launch_args 仅把 -- 开头的参数识别为选项。
Codex 的 -a 因此被当成位置参数。
显式 -a never 覆盖已存 -a on-request 后，旧值 on-request 仍留在参数中。
该函数进入所有 harness 的 lifecycle.start，不只影响 Pi。

独立启动回归测试使用仓库的 application fixture。
进程执行沿用该 fixture 的 double；参数构造与 desired state 是真实代码。

```python
from tests.identity.agents.registry.entity.support import application


def test_explicit_codex_approval_option_still_wins(tmp_path):
    app = application(tmp_path)
    try:
        app.agents.create(
            "foo",
            harness_args={"codex": ("-a", "on-request")},
            preferred_harness="codex",
        )
        app.handle("lifecycle.start", {
            "provider": "codex", "name": "foo", "headless": True,
            "cwd": "/work", "args": ["-a", "never"],
        })
        spec = next(row for row in app.desired_state.load().harnesses
                    if row.name == "foo")
        assert spec.args == ("-a", "never")
    finally:
        app._close()
        app._close_home_lease()
```

将测试保存到隔离临时目录后，在两个 worktree 分别执行：

```sh
PYTHONPATH="$PWD:$PWD/src" uv run --with pytest-timeout pytest \
  -p tests.conftest /private/tmp/pi-p2-accept-logs/test_short_start.py \
  -q --timeout 60
```

实测：

- 527a1ca70：1 failed。实际参数为 (-a, never, on-request)。
- 62465dbec：1 passed。实际参数为 (-a, never)。

另有长选项误去重。不同选项的值相同，不代表选项重复：

```sh
uv run python - <<'PY'
from hyprial.kernel import merge_harness_launch_args
actual = merge_harness_launch_args(
    ("--provider", "deepseek"), ("--model", "deepseek")
)
print(actual)
assert actual == ("--model", "deepseek", "--provider", "deepseek")
PY
```

实测输出只有 (--model, deepseek)，已存 --provider 整组丢失。
原因是 any(token in seen for token in tokens) 按单个 token 去重。
该反例只测试参数合并，不声称名为 deepseek 的模型可用。

修复要求：按选项及参数组处理覆盖关系。
短选项不能留下失去所属选项的旧值。
不同选项不能因共享值而互相删除。
需要覆盖 start、restore、routine 使用的同一合并函数。

## 阻断项 B：先 resolve，后检查符号链接

位置：src/hyprial/daemon/impl/harnesses/pi/loader.py:435-449。

own_workspace 先经 resolve 转成目标路径。
随后传给 PiProjectTrustReceipt 的已是目标路径。
receipt 的 is_symlink 检查无法识别原 workspace 符号链接。

以下复现只创建临时目录，不读取真实 agent home：

```sh
uv run python - <<'PY'
from pathlib import Path
from tempfile import TemporaryDirectory
from hyprial.daemon.impl.harnesses.pi.loader import (
    PiProjectTrustError, resolve_approved_pi_project,
)

with TemporaryDirectory(prefix="pi-trust-review-") as raw:
    root = Path(raw)
    own = root / "agents/a/workspace"
    other = root / "agents/b/workspace"
    own.parent.mkdir(parents=True)
    other.mkdir(parents=True, mode=0o700)
    own.symlink_to(other, target_is_directory=True)
    try:
        receipt = resolve_approved_pi_project(
            cwd=str(other), runtime_args=("--approve",), own_workspace=own,
        )
    except PiProjectTrustError:
        print("REFUSED")
    else:
        print(receipt.scope, receipt.repository_root == other.resolve())
        raise AssertionError("another agent workspace was trusted")
PY
```

实测：workspace True，随后 AssertionError。
把 cwd 改为 own，结果仍相同。
原有 another_agents_workspace 负例没有符号链接，不能覆盖此情况。

另以 ValueError 及 Git/符号链接原因断言重跑同一负例：
527a1ca70 为 1 failed，62465dbec 为 1 passed。
旧 API 没有 own_workspace，基线调用不传该参数。
基线实际触发非 Git cwd 拒绝，未用 TypeError 充当通过。

影响范围需要区分：

- 信任函数违反“本 agent workspace、非符号链接”的限定：FAIL。
- identity 的初次 home 校验会拒绝既存 workspace 符号链接。
- 完整生产启动中的越权可利用性：UNKNOWN。本次未尝试读取凭据。

修复要求：在丢弃原路径信息前检查 workspace 归属与符号链接。
需要增加指向另一 agent workspace 的符号链接负例。
加载时也需要维持已批准边界，不能只依赖 canonical 路径相等。

## C：同步拒绝与明确原因

同步拒绝：PASS。P2 就绪门的新增焦点用例：PASS。

独立复核使用真实 HarnessLauncher、HarnessRuntimeActor 和生命周期 manager。
测试为 channel 注入临时 runtime roots。
CliRunner 的请求 seam 调用真实 application.handle。
请求 seam 把 DaemonRequestError 转成 CliError，未走真实 socket。

两种拒绝各执行一次生命周期调用和一次 CLI 调用。
两项测试通过。CLI 退出码均为 1，stdout 均是单个 JSON 对象：

```json
{"ok":false,"code":"PI_P2_LAUNCH_UNTRUSTED","error":"harness.ensure failed: PI_P2_LAUNCH_UNTRUSTED: Pi P2 headless launch requires explicit --approve trust"}
{"ok":false,"code":"PI_P2_LAUNCH_UNTRUSTED","error":"harness.ensure failed: PI_P2_LAUNCH_UNTRUSTED: Pi P2 cwd is not inside an approved Git repository"}
```

独立测试命令：

```sh
PYTHONPATH="$PWD:$PWD/src" uv run --with pytest-timeout pytest \
  -p tests.conftest /private/tmp/pi-p2-accept-logs/test_review.py \
  -k real_lifecycle -q -s --timeout 60
```

全量的隔离双 daemon 测试也收到该同步拒绝码。
该测试仍期待 start 先成功，再从 connector 读取后台错误，因此失败。
这项失败符合 C 的行为变化，但测试尚未适配。

完整 CLI→socket→daemon→Pi 子进程成功路径：UNKNOWN。
双 daemon 场景未越过负例，未验完后续成功路径。
真实模型、生产常驻 agent 与真实凭据均不在本次验证范围。

## 提交顺序与先红测试

提交顺序符合先测试、后修复：

- A：6a91d2989 → 4cd13cd92。
- B：62ee93a9d → a010b19c3。
- C：01e2170c3 → 19fcc591a。

验收者在三个独立 detached worktree 重跑红测试。
解释器统一为 CPython 3.12，未改写历史提交。

- A：4 failed。start、显式参数、routine 与 restore 断言均失败。
- B：workspace 文件在收集期 ImportError。
  loader 用例因 trust_scope 参数不存在而 TypeError。
  这两类红灯没有执行信任行为断言，先红证据不足。
- C：2 failed、1 teardown error。
  就绪门用例未抛 HarnessStartError。
  同步拒绝用例未抛 PiProjectTrustError，并遗留后台执行造成 fd 检查失败。

C 的 outside_its_own_workspace 用例没有传 --approve。
因此该用例首先触发缺少批准，不能单独证明 cwd 拒绝。
本次独立复核补测了带 --approve 的无关非 Git cwd。

## 门禁与焦点测试

全部测试使用临时 home、fixture socket 与回环端口。
测试清除了继承的 HYPRIAL_*、GIT_CONFIG_*、代理及 harness worker 环境变量。
NO_PROXY 与 no_proxy 均设为 127.0.0.1,localhost。
Node 路径显式优先选择 v24.21.0。

```sh
uv run --no-project --python 3.12 python scripts/ci/check_structural_limits.py
uv run --no-project --python 3.12 python scripts/ci/check_module_boundaries.py
uv run ruff check .
```

实测：

- 结构门：FAIL，3 项既有违规。本次没有新增违规。
- 边界门：PASS，exit 0。
- ruff：PASS，All checks passed。

结构门对照：

- registry_ops.py：1005 行；62465dbec 同样为 1005 行。
- tests/daemon/outbox：8 个直接文件；62465dbec 同样为 8 个。
- contract/e2e/_daemon_runner.py：1004 行。
  62465dbec 为 998 行，但分支起点 6de5cdc00 已为 1004 行。
  本分支没有修改该文件。

主焦点命令：

```sh
uv run --with pytest-timeout pytest \
  tests/daemon/harnesses/pi tests/identity/agents/registry/entity \
  tests/daemon/restore tests/daemon/application/restore \
  tests/daemon/lifecycle tests/shell/cli/lifecycle \
  tests/biz/routines tests/daemon/pac -q --timeout 600
```

实测：1 failed、1407 passed，耗时 183.24 秒。
唯一失败为 test_t04_t05_t06_real_sdk_loads_c_and_trusted_project_with_git_boundary。
测试要求 Pi SDK 0.85.1，本机安装版本为 1.2.0。
在 62465dbec 重跑同一用例也失败，差异同为 1.2.0 != 0.85.1。
新 workspace loader 用例执行了真实 SDK probe，未跳过。
该结果不替代对 Pi SDK 0.85.1 的验证。

新增 A/B 绿测试定向复跑：12 passed、16 deselected。
独立反例集合：3 failed、2 passed。
3 个失败分别为共享选项值、Codex 短选项、跨 agent 符号链接。

补充焦点命令：

```sh
uv run --with pytest-timeout pytest \
  tests/shell/cli/agents tests/daemon/desired_state -q --timeout 600
```

实测：199 passed，耗时 190.91 秒。

## 全量测试

结果：FAIL，exit 1，耗时 2882.70 秒。

- 7 failed。
- 12972 passed。
- 14 skipped。
- 5 xfailed、1 xpassed。

skip 不代表对应场景通过。

命令通过隔离环境包装器执行：

```sh
for key in $(printenv | cut -d= -f1); do
  case "$key" in
    HYPRIAL_*|GIT_CONFIG_*|*PROXY|*proxy|HARNESS_*|CLAUDECODE|CLAUDE_CODE_*|CODEX_*)
      unset "$key" ;;
  esac
done
export NO_PROXY=127.0.0.1,localhost no_proxy=127.0.0.1,localhost
export PATH=/Users/bobadmin/.npm/_npx/387698761821791d/node_modules/node/bin:$PATH
uv run --with libcst==1.9.0 --with pytest-timeout pytest tests/ -q --timeout 600
```

全部 7 个失败项都在 detached 62465dbec 对照重跑。
对照结果：4 failed、3 passed，耗时 18.08 秒。
3 个基线通过项又在本分支定向重跑，均失败，耗时 14.62 秒。

新增失败：

1. tests/daemon/application/composition/composition/test_final_composition_lifecycle.py::test_explicit_lifecycle_start_retires_same_entity_restore_aba
   显式 B→A 后仍保留 B 的 --episode-b。
   实际 args 为 (--episode-a, --episode-b)，期望仅为 (--episode-a)。
   上次 launch 回写的参数又作为已存默认值参与合并。
   需要明确默认配置与上次 launch 参数的区别，不能只删除断言。
2. tests/integration/transfer/p2/e2e/test_two_daemon_chain.py::test_the_chain_lands_on_the_target_and_retires_the_source[pi]
   负例的真实 IPC 已同步返回 PI_P2_LAUNCH_UNTRUSTED。
   旧测试仍期待异步拒绝；需要适配并继续验后续就绪成功路径。
   此项不单独判为 transfer 产品行为回归。
3. tests/tooling/architecture/boundary/test_supervision_boundary_gate.py::test_the_manifest_matches_the_repository_exactly
   新增 PI_RPC_STARTUP_TIMEOUT_SECONDS_DEFAULT 触发 supervision manifest drift。
   位置为 src/hyprial/daemon/impl/harnesses/pi/rpc_process.py。
   需要按现有运行时预算权威处理，不能靠改名隐藏该门禁发现。

既有失败在基线重现：

1. tests/daemon/harnesses/pi/configuration/test_pi_controlled_loader.py::test_t04_t05_t06_real_sdk_loads_c_and_trusted_project_with_git_boundary
   同为 SDK 1.2.0 与固定期望 0.85.1 不符。
2. tests/daemon/ipc/cost/test_ipc_cost_counters.py::test_four_sides_cover_a_pure_heartbeat_load
   同为 CPU 计数覆盖率低于 0.70。
   本分支读数为 18.655/33.855，基线为 8.126/12.425。
3. tests/kernel/platform/test_platform_actor_preservation.py::test_posix_listener_keeps_request_owner_and_reserve_fd
   同为 AF_UNIX path too long。
4. tests/tooling/architecture/authority/test_inbox_authority_gate.py::test_current_raw_bypasses_are_exactly_in_the_shrinking_manifest
   同为 forwards.py:_BridgeForwardsMixin._log_missing_failure_route:alarm_emitter 未登记。

对照命令是在基线执行上述 7 个完整 node id：

```sh
uv run --with libcst==1.9.0 --with pytest-timeout pytest \
  tests/daemon/application/composition/composition/test_final_composition_lifecycle.py::test_explicit_lifecycle_start_retires_same_entity_restore_aba \
  tests/daemon/harnesses/pi/configuration/test_pi_controlled_loader.py::test_t04_t05_t06_real_sdk_loads_c_and_trusted_project_with_git_boundary \
  tests/daemon/ipc/cost/test_ipc_cost_counters.py::test_four_sides_cover_a_pure_heartbeat_load \
  'tests/integration/transfer/p2/e2e/test_two_daemon_chain.py::test_the_chain_lands_on_the_target_and_retires_the_source[pi]' \
  tests/kernel/platform/test_platform_actor_preservation.py::test_posix_listener_keeps_request_owner_and_reserve_fd \
  tests/tooling/architecture/authority/test_inbox_authority_gate.py::test_current_raw_bypasses_are_exactly_in_the_shrinking_manifest \
  tests/tooling/architecture/boundary/test_supervision_boundary_gate.py::test_the_manifest_matches_the_repository_exactly \
  -q --timeout 600 --tb=short
```

对照使用相同的环境清理包装器。
基线已用 Node 24 执行 npm --prefix src/gui ci --ignore-scripts，exit 0。
基线没有执行第二次全量；这里只对照全部失败项。

原始验收日志保留在 /private/tmp/pi-p2-accept-logs。
本文件保存判据、复现代码、命令和结果，不以临时日志替代结论。

## 根因 note 与交付边界

docs/notes/pi-p2-launch-trust-2026-10-16.md 的 A/C 根因能复现。
B 的普通 workspace 正例能复现，但“不得是 symlink”的结论不成立。
结构门对照中的“与基线一致或为其超集”不能据此判定全部为既有。
本次另核对 6de5cdc00，确认第三项在分支起点已存在。

Contract-Impact: 本提交只增加验收判词，不改变字段或错误码。
本分支新增 PI_P2_LAUNCH_UNTRUSTED，contract/ 尚无对应记录。
Skill-Impact: 本提交不改变操作方式；A/B 修复需要重新验收。

未 push，未开 PR，未向其他 agent 发消息。
