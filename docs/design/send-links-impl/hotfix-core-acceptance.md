REJECTED

send-links 生产热修合并验收，2026-10-16。

1. 结论

本合并树不得作为本轮生产热修放行。
H2 历史图扫描超过 200 ms，并同步阻塞链接 tick 线程。
H2 的 5 秒所有权缓存仍会让新建临时 worker 发布空文件。
隔离 E2E-026 新增 E26-08 正文读取回归。
H1 的 enforce 准入判定保持等价，但 stats 返回并非逐字节一致。

威胁模型按任务中的 Allen 裁定 94，仅检查误操作。
正文读取问题发生在原收件 actor 内，不宣称跨身份攻击。
本次未修改产品代码，未实施 WP9 重构。

2. 合并树与范围

基线：ca8d54bb4。
本地分支：hotfix-core/send-links。
工作目录：/private/tmp/hf-wp8-send-links。
依次执行 git merge --no-ff，3 次合并均无冲突：
H1 d1d20f348，经 b79cef5ea 合入。
H2 f8a0f7981，经 b5181250b 合入。
H3 43cdadee6，经 429fe82cc 合入。
验收产品头：429fe82cc0fd547236658f06a6ae95c6e5230301。
产品树：905c0dfcee50ceb8a59816c855e76a418d9359ca。

相对基线共改动 21 个文件，新增 1016 行，删除 34 行。
其中产品文件 10 个、契约文件 2 个、测试及 helper 文件 9 个。
H1 只改窗口开关、准入与观测计数，并同步契约。
H2 只改临时 worker 的空文件发布判断与 PAC 所有权查询。
H3 只补充无 ledger 行的收件请求排干事务。
未发现越过三片目标的产品改动。
范围限定通过；目标行为和性能未全部通过。

3. 返工项

3.1 H2 历史图扫描：新增性能回归

位置：src/hyprial/daemon/impl/composition/links.py:409–443。
workflow_owned_actors 每 SPACE_REFRESH_MS=5000 ms 扫描 store.graph_ids()。
查询没有关闭状态过滤，也没有本地候选 actor 限定。
每图调用 workflow_roster，校验 roster、actor 节点与 receipts。
随后 _owned_workers_in 再逐 worker 查询 receipt。
调用路径为 LinkService._loop → tick → _ensure_local_files → workflow_owned_actors。
扫描直接在 link-service 线程内执行。

探针创建真实 PAC SQLite 库，含 3000 个已关闭图。
workflow 状态为 completed，receipt 状态为 down。
每图分别放置 1 个、4 个 workflow-owned worker。
所有图均通过真实 workflow_roster 完整性校验。
OrgFS 使用隔离 fake，避免把网络或磁盘发布耗时计入扫描。
因此结果不代表生产上限。

最终实测，每图 1 个 worker：
5 次刷新分别为 86.158、84.583、83.889、85.694、84.768 ms。
真实 link-service 线程内扫描为 86.205 ms。

最终实测，每图 4 个 worker：
5 次刷新分别为 211.918、219.914、214.588、216.813、212.697 ms。
真实 link-service 线程内扫描为 216.388 ms。
从 kick 到线程排干退出为 216.707 ms。
缓存命中为 0.148 ms，但不消除每 5 秒的同步扫描。
另两轮扫描同样超过 200 ms，范围为 204.798–218.686 ms。

ca8d54bb4 detached worktree 的同形库不触发所有权扫描。
两轮基线 tick 均未接近 200 ms，详见证据记录。
本项为新增扫描成本，不归因于代理、生产状态或 HYPRIAL_NODE_ID。

返工要求：只查询活跃候选或建立可更新索引。
不得在链接 tick 线程遍历全部历史图。
返工后重新实测 3000 个图，并覆盖每图多个 worker。

3.2 H2 新图缓存窗口：目标仍未修复

位置：同文件 workflow_owned_actors 的 5 秒缓存。
探针使用真实 GraphWorkflowService、真实 PacLifecyclePort 和隔离 OrgFS。
先在空图集上 tick，缓存空集合。
1000 ms 后创建 1 个具有有效 roster/receipt 的 workflow worker。
将 worker 加入本地 actor 集合，再次 tick。
此时缓存仍为空，worker 的空 link 文件已经提交。
5000 ms 后刷新所有权，OrgFS 中的空文件仍然存在。
连续两次合并树探针均失败。

基线也发布该文件，属于本热修未修复的目标，不标为新增回归。
静态 worker 集合的 R12/R12b 测试不足以覆盖正常的新建图过程。
返工要求：所有权集合发生变化时可立即识别新 worker。
不得用 actor 名称前缀推断所有权，也不得漏发常驻 agent。

3.3 E26-08 正文读取：新增行为回归

命令：uv run python contract/e2e/run-send-links.py。
执行时固定 HYPRIAL_BIN 为对应 worktree 的 .venv/bin/hyprial。
隔离 home、loopback 网络，未连接生产 daemon。

首次交错复测为合并树 → 基线 → 合并树 → 基线。
随后发现初建基线环境选用了 CPython 3.14，未据此定案。
已将基线与 H3-only 环境重建为 CPython 3.12.13。
最终复测顺序为基线 → 合并树 → 基线 → H3-only。
最终基线 2 次 PASS，合并树与 H3-only 均在 E26-08 失败。
合并树累计 3 次复现，错误前缀完全一致：
RESULT FAIL E2E-026 send-links the drained request rC still exposes a body:
返回包含 ok=True、message=rC-body、pending=False、fetched=False。
另在仅含 H3 的 43cdadee6 detached worktree 复现同一失败。
因此该正文读取回归可归因于 H3，无需 H1/H2。
基线两次均通过该断言，最终均为：
RESULT PASS E2E-026 send-links isolated pass=13 fail=0 blocked=0

基线 runner 同时记录 E26-08-drain-uneven：consumed=0、ack 时间为空。
这仍是旧版的持久排干缺口，runner 的 PASS 不代表该缺口已修复。
合并树两次失败现场的真实 SQLite 均为：
consumed=1，acknowledged_at_ms 非空，fetched_at_ms 为空。
link_ledger.state=drained，direction=in，sample_pending=0。
所以 H3 确实修复了持久终态，但新增了从未读取正文的读回入口。

原因：新 drained 行进入 LinkService 的 settled 集合。
admit_dispatch 对 settled id 直接返回 True。
message.read_body 的 retained-row fallback 因此返回正文。
调用位置：links/service/gate.py:165–171；
application/messaging/inbox_surface/queries.py:530–560。
以上判断不依赖恶意调用者模型。
返工要求：保留持久 drained 终态，同时拒绝从未准入的 drained 正文读回。
保持 E26-08 既有断言，不得删断言或用 skip 规避。

3.4 H1 enforce 返回：不满足字面上的逐字节要求

相同 fake 时钟、相同空链接状态下，序列化实际 LinkService.stats 返回。
基线与合并树各复测 2 次，每个版本内部字节稳定。
合并树 enforce 返回比基线多一个字段：windowObserved=0。
该字段在 service/views.py:113 无条件增加。
契约已同步，但 enforce 返回仍不等于旧版返回。

返工要求：enforce 保持原返回，或由发布负责人显式放宽逐字节要求。
本验收没有获得后一项授权，按原要求判 FAIL。
这不等于发现了 enforce 准入判定差异，见下一节。

4. 已通过的行为检查

H1 准入差分：随机种子 94，10000 个固定状态。
覆盖 W/cwnd、链接状态、owner/user/route、reply、drained 和 dispatch。
基线与合并树 enforce 的原有判定及错误字段规范 JSON 逐字节相等。
比较不含新增的内部 Allow.window_observed 标签，也不含 stats 返回。
规范 JSON 共 1261198 字节，两个版本的 SHA-256 均为：
edf875e05adc18af21e18d7329aae17054fe71d55df3282cb116be59d1165b24。
observe 同一批 10000 个状态没有 Hold(LINK_WINDOW_FULL)。
基线执行 observe 目标的负对照会在 Hold(LINK_WINDOW_FULL) 断言处失败。
这份负对照证明旧窗口拦截确实存在。

H1 的常驻服务测试覆盖 observe 计数、user: W 路径及旧持久 hold 释放。
H2 的 R12/R12b/R12c 通过。
常驻 agent 发布空文件并保留 sender_known。
静态已识别 worker 不发空文件；主动 mint 时仍惰性发布。
临时 worker 对 owner 的发送，以及与 dispatch-coordinator 的通信仍允许。
这组通过结果不覆盖第 3.2 节的新图缓存窗口。

H3 的 R22 ledger 与组合重启测试通过。
无 ledger 行的请求排干后 consumed=True，ack 时间非空，ledger=drained。
重复排干幂等；关闭后重开同一库仍为终态。
重启后 live inbound 为空，settle_hint=ack，晚到 reply 返回 LINK_RECLAIMED。
单元重启通过不能替代第 3.3 节的真实 daemon 正文读取验收。

5. 先红提交检查

H1：2ec5e990b → 5b067e78d → d1d20f348。
H2：a2e3c5284 → 7407ff7bc → f8a0f7981。
H3：caf006947 → 43cdadee6。
3 片均先提交测试，再提交修复，提交顺序通过。

亲自创建 3 个 detached 红测试 worktree 并复现：
H1 组合测试因缺少 WINDOW_MODES 导出而收集失败。
单跑 policy/service 的 H1 选择集为 10 failed、7 passed。
失败原因为新 window_mode 参数尚不存在。
这些 API 红不能单独作为旧窗口死锁的行为证据。
第 4 节的基线负对照补充了实际 Hold 行为证据。
H2 的 R12/R12b 为 2 failed，断言为多发 worker 空文件。
H3 的 R22 为 2 failed，断言为未持久排干及 drained 结果为空。
H2/H3 的红均为目标行为失败，不是收集错误。
H1 返工时补充不依赖新 API 的行为红测试。
验收时补跑的负对照不能改写原提交的红测试质量。

6. 验证命令与结果

环境：macOS 26.5.2 arm64，CPython 3.12.13，uv 0.11.7。
Node 为 v24.21.0，路径固定为：
/Users/bobadmin/.npm/_npx/387698761821791d/node_modules/node/bin。
候选 src/gui/node_modules 已就绪；基线也执行 npm ci --ignore-scripts。

结构门：PASS。
uv run --no-project --python 3.12 python scripts/ci/check_structural_limits.py

边界门：PASS。
uv run --no-project --python 3.12 python scripts/ci/check_module_boundaries.py

ruff：PASS。
uv run ruff check .

产品合并树的 git diff --check ca8d54bb4 429fe82cc：PASS。
新增原始 pytest 证据含行尾空格，暂存区 diff --check 会报告空白告警。
这些空格随原输出按 WORM 保留，不格式化历史证据。

焦点：PASS，1281 passed，2 warnings，177.99 s。
uv run pytest tests/daemon/links/ tests/daemon/inbox/ tests/daemon/pac/ tests/tooling/contracts/ -q

全量：FAIL，退出 1，2370.62 s。
3 failed、13490 passed、14 skipped、7 xfailed、1 xpassed、14 warnings。
14 个 skip、7 个 xfail、1 个 xpass 均不转算生产功能 PASS。

3 个失败项均在 ca8d54bb4 detached worktree 对照。
两边均固定 CPython 3.12.13，并按基线 → 合并树交错复测 2 轮。
每轮两个版本均为同样的 3 failed，归类为既有失败：

Pi SDK 版本不符：
tests/daemon/harnesses/pi/configuration/test_pi_controlled_loader.py::test_t04_t05_t06_real_sdk_loads_c_and_trusted_project_with_git_boundary
原文：AssertionError: assert '1.2.0' == '0.85.1'。

心跳 CPU 计数覆盖率未达 70%：
tests/daemon/ipc/cost/test_ipc_cost_counters.py::test_four_sides_cover_a_pure_heartbeat_load
全量原文：AssertionError: (8.389, 14.760000000009654)。
对照两边同一比例断言持续失败，具体计数随运行变化。

Unix socket 测试路径过长：
tests/kernel/platform/test_platform_actor_preservation.py::test_posix_listener_keeps_request_owner_and_reserve_fd
原文：OSError: AF_UNIX path too long。

这些既有失败不归因于热修，也不能把全量写成 PASS。
实际命令如下；另显式设置隔离 HYPRIAL_HOME、HARNESS_STATE_DIR、
HYPRIAL_NETWORK_ISOLATED=1 及该树的 HYPRIAL_BIN。
env -u HYPRIAL_NODE_ID -u HYPRIAL_MANAGED_WORKER -u HYPRIAL_WORKER_ACTOR -u HYPRIAL_WORKER_SESSION_REF -u HYPRIAL_TOOL_PROFILE_ID -u HTTP_PROXY -u HTTPS_PROXY -u ALL_PROXY -u http_proxy -u https_proxy -u all_proxy NO_PROXY=127.0.0.1,localhost no_proxy=127.0.0.1,localhost PATH=/Users/bobadmin/.npm/_npx/387698761821791d/node_modules/node/bin:$PATH uv run --with libcst==1.9.0 --with pytest-timeout pytest tests/ -q --timeout 600

交付前结构门、边界门、ruff 均复跑 PASS。
指定的精确焦点命令也复跑 PASS：
uv run pytest tests/daemon/links/ tests/daemon/inbox/ tests/tooling/contracts/ -q
591 passed，2 warnings，142.77 s。

7. 证据与未测范围

可复现探针及结果目录：
docs/notes/send-links-hotfix-core-acceptance-20261016/。
probes/ 保存 4 个探针；records/ 保存复测结果与 E2E 原文。
原始 baseline 记录含初建 CPython 3.14 环境的运行，不作最终对照依据。
最终对照以 records/baseline-cpython312.json 为准。
该文件追加了 3.12.13 的 E2E、性能、策略、stats 与缓存复测。
全量失败项的同解释器对照见 records/gate-summary.txt。
在待测树根设置 PYTHONPATH=$PWD:$PWD/src 后运行 uv run python。
benchmark.py 实测真实 PAC SQLite 与 link-service 线程。
policy-diff.py enforce 输出准入摘要；observe 检查不出现窗口 Hold。
h2-cache.py 预期退出 1，失败断言即第 3.2 节的未修复行为。
stats-diff.py 输出未经字段归一化的 stats JSON。
基线运行时用同一探针的绝对路径，并切换到 detached 基线目录。

红测试命令，在对应红提交树内执行：
uv run pytest tests/daemon/links/composition/test_compose.py tests/daemon/links/test_policy.py tests/daemon/links/service/test_hold_release.py -q -k h1
uv run pytest tests/daemon/links/test_policy.py tests/daemon/links/service/test_hold_release.py -q -k h1
uv run pytest tests/daemon/links/service/test_reclaim.py -q -k r12
uv run pytest tests/daemon/links/composition/test_restart.py tests/daemon/links/ledger/test_ledger.py -q -k r22
实际红测试复用了候选依赖环境，以 PYTHONPATH 指向红树源码。
通过 uv run --project /private/tmp/hf-wp8-send-links --no-sync python -m pytest 执行。
产品源码来自对应红提交树。
本机临时完整日志：/private/tmp/hotfix-core-acceptance-logs/。
临时日志不是唯一证据；判词、探针和结果摘要随本次提交入库。
基线 detached worktree：/private/tmp/hf-core-baseline，HEAD=ca8d54bb4。

真实生产 PAC 库、跨机网络、真实 provider 和 Lark：UNKNOWN，未触碰。
本次隔离 E2E-026 为 FAIL，不宣称生产功能通过。
未 push，未开 PR，未向其他 agent 发消息。
未启用 extensions.worktreeConfig，也未执行 git config --worktree。

Contract-Impact: 本验收只新增文档和复现证据；H1 的契约变更已随热修合入。
Skill-Impact: 本验收不改变 agent 操作方式，无 skill 改动。
