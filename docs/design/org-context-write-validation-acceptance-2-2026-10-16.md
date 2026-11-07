org-context 写前校验二次验收，2026-10-16

结论：REJECTED。

R1 的 9 项状态探针、R3、R4 已通过。
R2 仍缺 org status 的正文信息回退。
人读模式还把同一警告写入 stdout 和 stderr。
R1 的读取改动又使合法 CRLF 文件的格式预检失败。
现有先红测试未覆盖这 3 个缺口。
本次只新增验收材料，不修改产品或测试。

1. 验收对象与范围

工作目录：/private/tmp/hf-orgctx-validate。
分支：feat/org-context-write-validation。
受验提交：6fd9ff7ed。
首验判词：bf5c47e5b，docs/design/org-context-write-validation-acceptance-2026-10-16.md。
失败对照：独立 detached worktree /private/tmp/oc2-base，固定在 62465dbec。
先红复查：独立 detached worktree /private/tmp/oc2-red。
只复核首验 R1–R4 和“不足”。其余通过项只检查是否退化。

解释器为 worktree .venv 的 CPython 3.12.13。
Node 为 v24.21.0。
所有 Python 命令均经 uv run 执行。
手工探针使用临时 home；两套全量使用各自的 home 和 state。
测试环境设置 HYPRIAL_NETWORK_ISOLATED=1。
未手工读取生产 org 槽、生产 daemon、凭据或用户配置。
未启用 extensions.worktreeConfig，未使用 git config --worktree。
未 push，未开 PR，未给其他 agent 发消息。
工作流 inspect/complete 仅用于本次任务的状态和回执。

2. 未完成的要求与回归

2.1 org status 没有恢复正文中的可展示字段。

位置：src/hyprial/daemon/impl/org/store.py:400-446。
位置：src/hyprial/shell/impl/cli/commands/org/commands.py:753-767。
正文可解析、但对应采纳记录缺失时，status 返回成功和警告。
不过结果没有正文的 version、publisher 或摘要。
警告声称 showing the parsed body only，与实际输出不符。

独立探针的正文包含 version: 3、publisher: alice。
org status --json 的退出码为 0，stdout 恰好 1 个对象：

{"ok":true,"slot":"accepted","pendingCount":0,"pending":[],"warnings":[{"code":"ORG_ACCEPTANCE_METADATA_UNAVAILABLE","message":"accepted org-context has no readable adoption record; showing the parsed body only"}]}

org status 的人读输出同样只有槽状态、pending 和警告。
首验 R2 要求“从可解析正文恢复可展示字段”。
当前结果只保留 slot: accepted，没有恢复这些正文信息。
这属于原 R2 未完成，不是新增验收范围。

现有测试将缺口固化成了通过结果：
tests/shell/cli/test_cli_org_context_state.py 的 status 测试只核槽和警告。
tests/daemon/organizations/store/test_org_store.py 的对应测试也未核正文字段。
两项测试都断言没有 accepted 记录，这能防止伪造采纳证明。
但这不能替代对正文视图字段的断言。

返工要求：展示可从正文确定的信息，并明确记录缺失。
正文信息可放入独立的视图字段，不要求伪造完整采纳记录。
继续省略 adoptedAt，不得把正文可解析等同于已有采纳证明。
先增加能核对 version、publisher 的失败测试，再修产品代码。

2.2 人读模式的警告同时进入 stdout。

位置：src/hyprial/shell/impl/cli/commands/org/commands.py:723-737、763-766。
org show、org show --full、org status 均调用 warn 输出 stderr。
三条命令又把 warnings 放入结果，由通用渲染器写入 stdout。
因此警告并非只走 stderr。

独立探针在这 3 条命令的 stdout 均发现：

warnings:
  code:    ORG_ACCEPTANCE_METADATA_UNAVAILABLE
  message: accepted org-context has no readable adoption record; showing the parsed body only

stderr 同时包含：
hyprial: accepted org-context has no readable adoption record; showing the parsed body only

JSON 模式符合纪律：stdout 恰好 1 个对象，warnings 位于对象内。
人读模式不符合警告只走 stderr 的要求。
旧输出封装路径未发现退化；重复警告位于本轮新增的 R2 回退路径。
输出纪律的静态扫描只核调用方式，不能发现这种重复渲染。
现有回退测试也只断言 stderr 含警告，未断言 stdout 不含警告。

返工要求：JSON 保留结构化 warnings，人读结果不重复渲染警告。
先增加 stdout/stderr 分离的失败测试，再修产品代码。

2.3 R1 的原始字节读取改动使 CRLF 格式预检退化。

位置：src/hyprial/shell/impl/cli/commands/org/commands.py:106-131。
validate 从 read_text 改成 read_bytes 后直接 decode，再交给 parser。
原始字节用于摘要是正确的，但 parser 的围栏规则只接受 LF。
把同一合法样本的换行全部改成 CRLF 后，--path 预检现在失败。
这属于首验已通过的合法 --path 格式路径退化。

命令：uv run python -m hyprial.cli org context validate --path /private/tmp/oc2-crlf.md --json。
首验 bf5c47e5b：退出 0、ok:true、valid:true。
当前 6fd9ff7ed：退出 1、ok:false、valid:false，错误为：
org-context must contain exactly one fenced YAML block（line 1，column 1）。

又对 R1 修复提交做了成对复跑，每个提交各 2 次：
52d630a58：2 次均退出 0。
adf1270ff：2 次均退出 1，错误完全相同。
因此这项退化可以定位到 R1 修复，不是全量测试的间歇失败。
62465dbec 尚无 context validate 命令，不能作为该命令的对照。

返工要求：保留原始字节摘要，同时维持原有 CRLF 文件预检能力。
先增加合法全 CRLF 文件的失败测试，再修产品代码。
当前摘要测试只在标题放入 1 个 CRLF，没有覆盖围栏使用 CRLF。

3. 已完成的返工

R1 状态一致性：PASS；格式读取仍有第 2.3 节的回归。

亲自复跑首验的 9 个默认槽场景，均退出 1、ok:false：
- 缺当前原始字节摘要对应的采纳记录。
- version 不一致。
- publisher 不一致。
- documentSha256 不一致。
- adoptedAt 无效。
- 采纳记录不是合法 JSON。
- 手改正文后没有新摘要记录。
- publish-pending 指向其他摘要。
- publish-pending 不是合法 JSON。

正文合法且记录匹配时通过。
存在指向当前摘要的 publish-pending 时通过。
--path 在缺采纳记录时仍通过格式预检。
混合换行样本保留标题中的 CRLF，原始字节摘要与采纳记录一致。
该样本的 acceptance_record 和默认 validate 均通过。
摘要计算不再依赖 read_text 的换行归一化结果。

首验“不足”所述默认槽测试已修正。
合法正文的成功样本现在通过 adopt 创建采纳记录。
直接放正文但缺记录的样本已另立拒绝测试。

R2 的 show 正文回退与 JSON 封装：PASS。
这些通过项不抵消第 2 节的缺口。

org show 和 org show --full 都恢复了正文信息。
两条命令均退出 0，JSON 含明确 warnings，且不编造 adoptedAt。
show --full 还保留 document 和 documentSha256。
status 不再退出 1，也含明确 warnings。
所有已覆盖 JSON 探针均能完整解析为单个带 ok 的对象。

R3：PASS。

使用真实 OrgFsRuntime，亲自复跑新建和覆盖候选两分支。
损坏正文均返回 False，候选不新增或保留原字节。
两分支均不新建 publish-pending，并记录 warn。
校验发生在 queue_orgfs_publish 和 facade.write_text 之前。
补遗 docs/notes/org-context-write-paths-orgfs-publish-2026-10-16.md 已补盘点。
补遗列出 context/candidates/<user>.md 和 orgfs.create/context、orgfs.join 入口。

R4：PASS。

issued_at: 2026-99-99 不再从 validate_document 抛出 ValueError。
API 返回失败 issue，line: 8、column: 14，reason 含 month must be in 1..12。
CLI 退出 1，输出单个 ok:false 对象，并保留同一定位信息。
parse_document 的失败信息也有行列，焦点测试通过。

4. 先红测试与帮助指引

已按提交顺序检查红测试先于修复。
还在独立 worktree 逐个 checkout 红提交并亲自复跑。
失败来自行为断言，不是依赖或收集错误。

R1：52d630a58（含前序 43391bebe），修复 adf1270ff。
运行 CLI validate 与 store 测试：11 failed，13 passed。
失败包括 9 场景、--path 帮助和原始字节摘要。

R2：c22184285，修复 5443fb2c0。
运行 CLI org 与 store 测试：5 failed，40 passed。
失败覆盖 show、show --full、stderr 警告、status 警告和 store 回退。
缺正文字段和 stdout 警告的断言仍遗漏，见第 2 节。

R3：811a178a7，修复 31ef8bd5b。
运行 orgfs migration 的 corrupt 两项：2 failed，15 deselected。
两项都因 publish_accepted 返回 True 而失败。

R4：aaa31aa35，修复 0310a4496。
运行 document validation 与 CLI validate：3 failed，22 passed。
失败为无效日期穿透 API、parse_document 缺定位、CLI 缺 valid 字段。

当前同一焦点集合全部通过，见第 5 节。
提交 273d16c6b 只拆分测试文件，未删除上述断言。

org --help、org context --help 和 validate --help 均亲自复查。
帮助已写明默认槽核采纳状态，--path 只做格式预检。
docs/guides/onboarding-checklist.md 已补 validate 用法和采纳状态语义。
docs/domains/org-context.example.md 已补预检命令。
未改写历史 WORM 文档。
指引关于 status“仍展示正文”的说明尚不符合实际，见第 2.1 节。

5. 门、焦点与全量

通用环境前缀记录在证据目录的 env.sh。
前缀清除派单指定的 worker、代理和 GIT_CONFIG_* 变量。
另清除 HYPRIAL_AGENT_DIR、HARNESS_SOCKET_PATH、HARNESS_CONFIG。
设置独立 HYPRIAL_HOME、HARNESS_STATE_DIR 和网络隔离。
NO_PROXY/no_proxy 均为 127.0.0.1,localhost。
PATH 前置派单指定的 Node 24 目录。

uv run --no-project --python 3.12 python scripts/ci/check_structural_limits.py
结果：FAIL，退出 1；只有 2 项既有违规，不计为本次拒绝原因。
registry_ops.py 为 1005 行；tests/daemon/outbox 为 8 个直接文件。
62465dbec 实测结果完全相同，两个路径没有本次差异。
无新增违规不能写成结构门 PASS。

uv run --no-project --python 3.12 python scripts/ci/check_module_boundaries.py
结果：PASS，退出 0。

uv run ruff check .
结果：PASS，退出 0。

uv run python scripts/verify/check_cli_help_inventory.py
结果：PASS，退出 0；62 commands（56 visible，6 hidden）。

npm --prefix src/gui ci --ignore-scripts
npm --prefix desktop ci --ignore-scripts
结果：受验与对照均 PASS，均在对应全量之前安装，均使用 Node 24。

uv run --with pytest-timeout pytest tests/daemon/organizations tests/shell/cli/test_cli_org.py tests/shell/cli/test_cli_org_context_state.py tests/shell/cli/test_cli_org_context_validate.py tests/daemon/application/ports/test_daemon_org_context.py tests/shell/cli/output/test_output_discipline.py -q --timeout 600
结果：PASS，152 passed，2 warnings，12.44 秒。

全量命令：
uv run --with libcst==1.9.0 --with pytest-timeout pytest tests/ -q --timeout 600
受验结果：FAIL，退出 1。
5 failed，12953 passed，14 skipped，5 xfailed，1 xpassed。
14 warnings；2453.60 秒（40 分 53 秒）。
固定对照结果：FAIL，退出 1。
5 failed，12908 passed，14 skipped，5 xfailed，1 xpassed。
14 warnings；2456.05 秒（40 分 56 秒）。
两套全量均完整结束，没有用中断或抽样代替全量。

5 个失败的完整 ID 在两套全量完全相同：
- tests/daemon/harnesses/pi/configuration/test_pi_controlled_loader.py::test_t04_t05_t06_real_sdk_loads_c_and_trusted_project_with_git_boundary
  两边均为 Pi SDK 实测 1.2.0，测试预期 0.85.1。
- tests/daemon/ipc/cost/test_ipc_cost_counters.py::test_four_sides_cover_a_pure_heartbeat_load
  两边均为计时比例低于 0.70；受验 9.449/15.919，对照 9.457/16.339。
- tests/kernel/platform/test_platform_actor_preservation.py::test_posix_listener_keeps_request_owner_and_reserve_fd
  两边均为 OSError: AF_UNIX path too long。
- tests/tooling/architecture/authority/test_inbox_authority_gate.py::test_current_raw_bypasses_are_exactly_in_the_shrinking_manifest
  两边均遗漏 _BridgeForwardsMixin._log_missing_failure_route:alarm_emitter。
- tests/tooling/architecture/lint/test_provider_term_lint.py::test_remaining_provider_hits_are_all_whitelisted
  两边均为 envelope.py 注释中的 user-provider 未获词表放行。

又按 base → head → base → head 的顺序复跑这 5 项。
4 次均为 5 failed，错误类型和上述失败一致。
对照耗时分别为 7.04、6.32 秒；受验为 6.59、6.58 秒。
AF_UNIX 单项再改用短 --basetemp，两边均为 1 passed。
这 5 项均有本轮基线复现，不归为本次新增回归。
详细命令与读数见 failure-compare.sh、compare-*.log、short-*.log。

首验记录的 Pi 迁移链间歇失败在本轮两套全量均未重现。
本轮通过不能证明间歇问题已修复。
全量失败集合中未发现新增项；第 2 节仍有独立确认的缺口和回归。
未用总用例数差异推断归因，也未把全量记为 PASS。

另以 -rs 复查首验的 14 个 skip，得到 2 passed、14 skipped。
耗时 4.64 秒，2 warnings；完整原因见 skip-recheck.log。
这些 skip 不计为能力通过：
- macOS 不使用 Linux 的 backlog EAGAIN 行为。
- 未开启真实 Agent SDK 下载测试。
- 文件系统把大小写变体视为同一目录。
- 本机 FD 观察器看不到 open holder。
- 未开启 3 项真实 Claude 配置/TUI 测试。
- 未开启真实、计费的 Claude plugin 会话。
- 6 项真实 VM/channel/mapping 场景缺材料、授权或机器锁。

6. 独立探针与证据

独立探针共 33 项，29 项通过、4 项失败。
失败为 status 缺正文信息，以及 3 条人读命令重复输出警告。
脚本未因预期不符提前退出；全部场景均有读数。
首验通过项的复查也包括 2 个损坏 fixture 和 adopt/stage 拒写。
escaped-brace 仍定位到 20:33；multiline-flow 仍定位到 19:69。
adopt/stage 都在建立目录前拒绝损坏 source，并保留 20:33 定位。

另有 CRLF 成对复跑，结果见第 2.3 节。
最初一轮探针把全 CRLF 内容直接交给 parse_document，提前停止。
最终脚本改用标题含 CRLF 的既有摘要测试形态，再跑完全部 33 项。
全 CRLF 文件的 CLI 行为另行对照，未将未完成探针记为通过。

环境前缀、原始读数和摘要存于：
docs/notes/org-context-write-validation-acceptance-2-2026-10-16/。
原始复现脚本存于：
docs/notes/repro/org-context-write-validation-acceptance-2-2026-10-16/probes.py。
超过 1 MB 的单个原始输出不入库。
对应摘要记录命令、退出码、sha256、行数、首尾各 40 行。

真实生产原件、生产 daemon 和需专用材料/机器锁的 VM 未测。
这些真实环境能力为 UNKNOWN，不用 unit 或 skip 代替。
本次结论不等于完整发版验收。

Contract-Impact: 仅新增验收文档和证据，不改变产品契约。
Skill-Impact: 仅记录二次验收，不改变 agent 操作方式。
