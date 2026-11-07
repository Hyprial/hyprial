结构门基线豁免验收（2026-10-16）

判词：ACCEPTED。

受验变更满足结构门基线豁免的验收要求。
全量测试有 4 条失败，均在指定对照提交复现。
此判词不表示全量测试全绿，也不表示发版端到端验收通过。

1. 验收对象

分支为 ci/structural-baseline-exemptions，受验提交为 68f5a06c3。
分支基线为 6de5cdc00；失败对照树固定为 62465dbec。
本次验收只新增本报告，不修改实现、基线或测试。

验收环境使用 CPython 3.12.13 和 Node 24.21.0。
所有 Python 验证均由 uv run 启动。
GUI 依赖安装命令 npm --prefix src/gui ci --ignore-scripts 返回 0。

测试启动器删除全部 HYPRIAL_*、GIT_CONFIG_* 和含 WORKER 的环境变量。
启动器也删除 HTTP_PROXY、HTTPS_PROXY、ALL_PROXY 及小写变量。
NO_PROXY 和 no_proxy 均设为 127.0.0.1,localhost。
测试依赖 tests/conftest.py 的隔离 home、state 和 loopback-only daemon fixture。
验收未操作生产 daemon、凭据或用户配置。

2. 结构门实测

以下命令在受验树返回 1，符合预期：
uv run --no-project --python 3.12 python scripts/ci/check_structural_limits.py

新增失败项：
contract/e2e/_daemon_runner.py：1004 行，限制为 1000 行。

既有欠债恰好 2 条，均归属修复 PR #1502：
- src/hyprial/daemon/impl/application/actors/agents_admin/registry_ops.py：1005 行，限制为 1000 行。
- tests/daemon/outbox：8 个直接文件，限制为 7 个。

JSON 的 summary 实测为：
{"failing":1,"grandfathered":2,"staleBaseline":0,"baselineErrors":0}

violations 只含新增失败项。
grandfathered 只含上述 2 条欠债。
基线没有 _daemon_runner.py 条目。
两条基线的到期日均为 2026-10-30。

任务中的“dev 当前树”须固定提交解释。
分支基线 6de5cdc00 含 #1510 引入的 1004 行 runner。
验收时 origin/dev 已推进到 f5bb4edc7，并合入 #1512。
该提交把 runner 拆为 763 行。
在该 detached 树上复用受验门脚本和同一基线，实测返回 0。
其 grandfathered 仍为 2，failing 为 0。
不得把最新 dev 的结果描述为 runner 仍超限。
隔离 fixture 保留 1004 行反例，不依赖后续 dev 的行数。

3. 只出不进的测试核查

基线按 kind、path 精确匹配，不自动写回。
只豁免未过期且 actual 未超过记录值的计数违规。
不豁免缺失源码根、源码软链接等结构缺陷。
新增违规、超出记录值和过期条目均使状态失败。
修复违规后若未删除条目，stale_baseline 使状态失败。

test_structural_baseline.py 的 16 条测试覆盖：
- 基线内放行：test_violation_at_or_below_the_recorded_actual_is_grandfathered。
- 变差失败：test_actual_worse_than_recorded_is_a_failing_violation。
- 新增失败：test_new_violation_is_not_covered_by_the_baseline。
- 过期失败：test_expired_entry_stops_grandfathering_and_is_red。
- 已修未删失败：test_repaired_entry_must_be_deleted。
- 格式错误失败：test_malformed_baseline_fails_loudly_and_fails_closed、test_baseline_entry_missing_required_fields_fails。
- 默认 14 天、退出码、真实基线集合及 JSON 分类输出。
- #1510 runner 失败且不进入基线，扫描前后基线字节相同。
- #1502 两条欠债修复后同时成为 stale，并提示删除。

隔离补充探针通过：
- registry_ops 从记录的 1005 行降至 1003 行，仍只按既有预算放行。
- registry_ops 增至 1006 行，转为失败项。
- 只有 stale、没有计数违规时，状态仍失败。
- 只有格式错误、没有计数违规时，状态仍失败。

基线文件属于受审查的配置源，不是自动学习的快照。
“不得新增条目、不得抬高 actual”的变更纪律仍需代码审查。
真实基线集合测试另将当前允许的条目固定为 #1502 的 2 条。

4. 消费者与其它门

check_pr_hygiene.py 的 merge_tree_gate_errors 消费子进程退出码。
premerge-check.sh 同样消费退出码，并区分退出 1 与未运行。
两者均不依赖旧的违规条目总数，也不解析固定 JSON 键集合。
保留 status、violations 和原扫描统计键可兼容现有消费者。
新增 grandfathered、stale_baseline、baselineErrors 和 summary 分开报告分类。
main() 的 JSON 回归测试同时断言分类、计数和非零退出码。

对 6de5cdc00 的原脚本做 AST 对照：
原 scan 与当前 _scan_tree 的函数体完全相同。
源码根、后缀、排除目录、排除路径、历史路径及 7/1000 限制均未改变。
没有借此次豁免扩大扫描排除范围或放宽其它门。

以下验证通过：
- uv run --no-project --python 3.12 python scripts/ci/check_module_boundaries.py：退出 0。
- uv run ruff check .：All checks passed。
- uv run --with libcst==1.9.0 pytest tests/tooling/architecture/limits/ tests/tooling/ci/hygiene/ -q：99 passed。

tests/tooling/ 全部执行：1 failed, 1422 passed，耗时 359.09 秒。
唯一失败为：
tests/tooling/architecture/authority/test_inbox_authority_gate.py::test_current_raw_bypasses_are_exactly_in_the_shrinking_manifest

失败信息为 forwards.py 的 _log_missing_failure_route:alarm_emitter 未登记。
在 62465dbec 的 detached worktree、CPython 3.12.13 中单独重跑，同样失败。
该测试与产品 forwards.py 在对照提交和受验提交之间均无差异。
判定为既有 authority manifest 漂移，不记为本变更引入。

5. 先红后绿证据

独立重放 f0e55fb53 的测试文件：9 条测试均因缺少基线实现而报错。
该测试提交先于实现提交 c21fc67bf。

独立重放 15eb7f4b2 的测试文件：2 failed, 1 error。
失败明确指出旧基线错误包含 runner；另有缺少 summary 的错误。
该测试提交先于修复提交 e56d84cd7。
受验提交的对应测试已全部通过。
已有提交未改写；旧提交信息中的“3 条”不代表当前树。

6. 全量测试与失败对照

完整执行结果为：
4 failed, 12976 passed, 14 skipped, 5 xfailed, 1 xpassed。
耗时 2642.92 秒，退出码为 1。
14 条 skipped 不计为功能验证通过。

4 条失败均在 62465dbec 的 detached worktree 独立复现。
对照使用同样的环境清理、CPython 3.12.13 和 Node 24.21.0。
这 4 个测试文件在对照提交和受验提交之间均无差异。
受验分支相对 6de5cdc00 也未修改产品代码。

失败 1：
tests/daemon/harnesses/pi/configuration/test_pi_controlled_loader.py::test_t04_t05_t06_real_sdk_loads_c_and_trusted_project_with_git_boundary
受验树和对照树均实际获得 Pi SDK 1.2.0，断言期望 0.85.1。
判定：既有环境版本不匹配。

失败 2：
tests/daemon/ipc/cost/test_ipc_cost_counters.py::test_four_sides_cover_a_pure_heartbeat_load
受验树的覆盖比例为 11.19 / 21.062，低于 0.7。
对照树的比例为 9.94 / 14.33，同样低于 0.7。
判定：既有计时或负载敏感失败。

失败 3：
tests/kernel/platform/test_platform_actor_preservation.py::test_posix_listener_keeps_request_owner_and_reserve_fd
受验树和对照树均报 OSError: AF_UNIX path too long。
判定：既有 Unix socket 路径长度环境失败。

失败 4：
tests/tooling/architecture/authority/test_inbox_authority_gate.py::test_current_raw_bypasses_are_exactly_in_the_shrinking_manifest
受验树和对照树均报告同一 alarm_emitter 漂移。
判定：既有 manifest 漂移。

对照命令包含：
uv run --with libcst==1.9.0 --with pytest-timeout pytest tests/daemon/harnesses/pi/configuration/test_pi_controlled_loader.py tests/daemon/ipc/cost/test_ipc_cost_counters.py tests/integration/transfer/p2/e2e/test_two_daemon_chain.py tests/tooling/isolation/test_fd_hygiene_collection.py -q --timeout 600
uv run --with libcst==1.9.0 --with pytest-timeout pytest tests/kernel/platform/test_platform_actor_preservation.py::test_posix_listener_keeps_request_owner_and_reserve_fd -q --timeout 600
uv run --with libcst==1.9.0 --with pytest-timeout pytest tests/tooling/architecture/authority/test_inbox_authority_gate.py::test_current_raw_bypasses_are_exactly_in_the_shrinking_manifest -q --timeout 600

本次未复现上游交接记录中的 fd_hygiene 超时。
不得把上一次运行的 7 条失败套用为本次结果。

执行命令由上述清理环境的启动器运行：
uv run --with libcst==1.9.0 --with pytest-timeout pytest tests/ -q --timeout 600

Node PATH 前缀为：
/Users/bobadmin/.npm/_npx/387698761821791d/node_modules/node/bin

完整本地日志位于 /tmp/hf-struct-accept-f10/。
本报告保留判据、提交、命令和结果，不把临时日志作为唯一证据。

7. 影响声明

Contract-Impact: 本次仅增加验收报告。受验实现修改 CI 内部结构门诊断，不修改已发布产品协议；现有消费者的退出码及基础 JSON 字段保持可用。
Skill-Impact: 不改变 agent 操作方式，无须修改 skill。
本次未 push、未开 PR、未向任何 agent 发消息。
未启用 extensions.worktreeConfig，也未使用 git config --worktree。
