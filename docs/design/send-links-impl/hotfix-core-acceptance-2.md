REJECTED

send-links 生产热修二次验收，2026-10-16。

1. 结论

上一轮 3.1—3.4 的目标行为已按新裁定通过。
本合并树仍不得放行：全量新增 4 个测试失败。
H2 新解析器触发 2 个既有守卫。
H3 新测试在全量执行前已经超过自身 TTL。
这些问题不能归为 ca8d54bb4 的既有失败。
本次只验收返工，没有修改产品代码或实施 WP9 重构。
威胁模型按裁定 94 限定误操作。

2. 合并范围

工作目录：/private/tmp/hf-wp8-send-links。
分支：hotfix-core/send-links。
上一轮判词提交：8cefbaae5。
依次执行 git merge --no-ff，均无冲突：
H1 b3f9e9968，经 9a72c3f8e 合入。
H2 40d547da9，经 8e3d9c320 合入。
H3 205cc63ed，经 9fd0877b0 合入。
验收头：9fd0877b0f47c3580bf1b627fb6e06139310afeb。
验收树：395bfedae0f5705b045fc6359fec25992fef0a63。
相对上一轮共改动 16 个文件，新增 713 行，删除 166 行。
产品文件共 6 个，改动限于本轮返工范围。
未发现其他产品范围扩张。

3. 上一轮返工项复核

3.1 历史图扫描：PASS

真实 PAC SQLite 中建立 3000 个已关闭图。
每图分别含 1 个、4 个 workflow-owned worker。
全部图均通过 workflow_roster 校验。
每图 4 个 worker 时，5 次 tick 耗时为：
0.189、0.091、0.073、0.070、0.068 ms。
真实 link-service 线程的 tick 为 0.189 ms。
每图 1 个 worker 时，线程 tick 为 0.180 ms。
两组均没有打开 PAC store。
每次同步 tick 均推进 5000 ms，排除缓存命中假象。
源码已删除 workflow_owned_actors、历史图扫描和所有权缓存。
本结果使用隔离 fake OrgFS，不代表真实发布耗时。

3.2 新图识别与五段 marker：PASS

使用真实 GraphWorkflowService 新建图。
由真实 ActorCoordinator._prepare_up 产生 activation marker。
再经 DaemonActorRuntime._launch_spec 写入真实 desired state。
同一时钟值下加入 worker 并立即 tick，未发布 worker 空文件。
常驻 resident 与名为 wf-resident 的常驻 agent 均发布空文件。
所有权查询没有打开 PAC store。
这次复核覆盖 marker 已持久化后的立即识别，不依赖 5 秒刷新。

唯一解析器为 pac/actors/marker.py 的 pac_worker_marker。
DaemonActorRuntime.observe 与 PacLifecyclePort 共用该解析器。
接受的文法为 pac:<graph>:<node>:<incarnation>:<effect>。
4 个字段必须非空，incarnation 必须是正 ASCII 十进制整数。
另查 14 个畸形输入，均返回非 worker。
将同一 worker 的 marker 改成四段后，下一次 tick 发布空文件。
缺行、读取失败或畸形 marker 均按常驻 agent 处理。
该保守处理符合本轮 desired state 所有权裁定。
常驻可达性、worker 惰性 mint、owner/PAC 通信的焦点测试通过。

3.3 持久排干与正文读取：PASS

官方隔离 E2E-026 为 13/13 PASS，耗时 32 秒。
保留全部原断言的库内复核为 13/13 PASS，耗时 31 秒。
E26-08 的真实 SQLite 状态为：
consumed=1；acknowledged_at_ms 非空；fetched_at_ms 为空。
link_ledger.direction=in；state=drained；sample_pending=0。
同一收件 actor 读取正文返回 MESSAGE_BODY_UNAVAILABLE。
settle_hint=ack；晚到 reply 返回 LINK_RECLAIMED。
已正常处理的 acked 行仍可只读读回。
没有删除或放宽 E26-08 的正文断言。

3.4 H1 等价范围与行为红：PASS

按集成者新裁定，只比较 enforce 准入判定和错误字段。
接受 stats 的 windowObserved 加性字段，不再比较整个 stats。
随机种子 94、10000 个固定状态的规范 JSON 完全相同。
基线与候选均为 1261198 字节，SHA-256 为：
edf875e05adc18af21e18d7329aae17054fe71d55df3282cb116be59d1165b24。
同一批 observe 状态均未出现 LINK_WINDOW_FULL Hold。

亲自将 b3f9e9968 的新测试函数附到 ca8d54bb4 的测试文件。
只补充基线已有 Hold、ipc_errors 导入，没有引入新 API。
结果为 1 failed，失败断言明确收到：
Hold(reason='LINK_WINDOW_FULL', peer='agent:o:n1:b', detail='cwnd')。
不是收集失败或参数错误。
候选该测试通过。

H3 的 8ee44688c 红提交实跑为 1 failed、1 passed。
失败是正文读取没有抛错；205cc63ed 后通过。
H2 的 02d0f3773 测试提交先于 265b33540 修复提交。
H1 补红按本轮授权在基线复核，不改写原先提交顺序。

4. 新增返工项

4.1 H2 未接入既有守卫：FAIL

测试 tests/kernel/addressing/test_address_parsing_guard.py 的
 test_no_bare_address_parsing_outside_the_uri_module 失败。
新增命中位置：daemon/impl/pac/actors/marker.py:38。
原文：parts = nickname.split(":")。
这是 PAC marker 自有文法，不是 agent URI。
需要按文法所有者登记精确守卫例外，保留唯一解析器。
不得仅更换字符串引号或写法来绕过守卫。

测试 tests/shell/login/device/test_boundaries.py 的
 test_t11_daemon_tree_never_imports_login_or_names_the_credential 失败。
新增 marker.py 返回 [1]。
原因是 NATIVE_ORIGINS 尚未登记这个新模块。
这是未分类新增文件的拒绝，不是发现读取生产凭据。
需要按现有 native owner 规则登记模块，不能删除守卫。

按基线 → 候选 → 基线 → 候选复测上述同一断言。
基线 2 次均通过，候选 2 次均失败。
这 2 个失败为本轮新增，不归入既有红项。

4.2 H3 新测试的收集时刻 TTL：FAIL

位置：tests/daemon/links/composition/test_drained_read_back.py:51—52。
NOW 在模块导入时固定，TTL_MS=600000。
全量收集完成后，执行到这 2 个测试已超过 10 分钟。
请求因此在调用 message.pending.list 时已经过期。
2 个测试均在前置列表断言失败：assert [] == ['rC']。
焦点和立即重跑均通过，不能据此将全量失败忽略。

需要在每次测试的设置阶段取得当前时间。
同一测试的请求、链接和账本查询继续使用同一时钟。
不得拉长 TTL 或删除正文、持久终态断言来掩盖失败。
本问题属于新测试的全量稳定性缺口。
第 3.3 节的真实 daemon 证据未复现正文功能回归。

5. 验证结果

环境：macOS arm64；CPython 3.12.13；Node v24.21.0。
候选与基线各用自己的 .venv/bin/hyprial。
GUI 的 node_modules 已就绪。
测试均隔离 home、state、socket 和 loopback 端口。
生产 daemon、凭据、provider 和 Lark 均未触碰。

结构门：PASS。
uv run --no-project --python 3.12 python scripts/ci/check_structural_limits.py

边界门：PASS。
uv run --no-project --python 3.12 python scripts/ci/check_module_boundaries.py

ruff：PASS。
uv run ruff check .

焦点：PASS，1297 passed，2 warnings，190.61 秒。
uv run pytest tests/daemon/links/ tests/daemon/inbox/ tests/daemon/pac/ tests/tooling/contracts/ -q

交付前精确焦点：PASS，597 passed，2 warnings，140.61 秒。
uv run pytest tests/daemon/links/ tests/daemon/inbox/ tests/tooling/contracts/ -q

全量：FAIL，7 failed、13502 passed、14 skipped。
另有 7 xfailed、1 xpassed、14 warnings。
pytest 耗时 2494.86 秒，命令墙钟耗时 2509 秒。
skip、xfail 和 xpass 均不转算功能 PASS。
4 个新失败见第 4 节。
另 3 个既有失败经同解释器交错复测 2 轮确认：
Pi SDK 1.2.0 与预期 0.85.1 不符。
心跳 CPU 计数覆盖率未达 70%。
Unix socket 测试路径过长：OSError: AF_UNIX path too long。

全量的环境清理与命令如下；另设置隔离 HYPRIAL_HOME、
HARNESS_STATE_DIR、HYPRIAL_NETWORK_ISOLATED=1 和本树 HYPRIAL_BIN。
env -u HYPRIAL_NODE_ID -u HYPRIAL_MANAGED_WORKER -u HYPRIAL_WORKER_ACTOR -u HYPRIAL_WORKER_SESSION_REF -u HYPRIAL_TOOL_PROFILE_ID -u HTTP_PROXY -u HTTPS_PROXY -u ALL_PROXY -u http_proxy -u https_proxy -u all_proxy NO_PROXY=127.0.0.1,localhost no_proxy=127.0.0.1,localhost PATH=/Users/bobadmin/.npm/_npx/387698761821791d/node_modules/node/bin:$PATH uv run --with libcst==1.9.0 --with pytest-timeout pytest tests/ -q --timeout 600

6. 追加隔离场景

E2E-018：PASS，13/13，64 秒。
uv run python contract/e2e/run-pac-graph-slice.py
覆盖显式 complete/reset、旧 request fencing、回边和只读投影。

E2E-021：FAIL，候选 2 次分别为 150、124 秒。
基线 2 次分别为 150、125 秒。
执行顺序为候选 → 基线 → 候选 → 基线。
使用未改断言的 run-orgfs-forwarding-redial.py。
外部探针只记录 fs join 耗时和只读查询提交数。
uv run python docs/notes/send-links-hotfix-core-acceptance2-20261016/probes/e21.py

fs join 单列，单位为秒：
候选第 1 次：共享空间 0.251；org 空间 5.005、4.116、5.005、5.006、5.003。
基线第 1 次：共享空间 0.189；org 空间 5.005、5.005、5.005、5.004。
候选第 2 次：共享空间 0.194；org 空间 5.004、5.005、5.005、5.005。
基线第 2 次：共享空间 0.189；org 空间 5.005、5.006、5.009、5.008。
4 次均未在 20 秒预算内加入 org 空间。
错误原文前缀相同：
RESULT FAIL E2E-021 orgfs-forwarding-redial org directory member join did not complete within 20s
末次 fs join 均为 timed out after 5 seconds。

org 空间提交数单列：
4 次 A 侧均为 969 次提交、323 个文档、324 次 meta 提交。
共享空间均只有 2 次提交。
E2E-021 的 320 个 liveliness 会话没有 desired state PAC marker。
按本轮裁定，这些 actor 仍须视为常驻，不能按名称跳过。
这与 REF-741 登记的 969 次提交及同步 hydrate 前沿相符。
登记依据为 0a6acdd1e 的 docs/design/send-links-impl/README.md。
两轮同断言对照确认这是既有 REF-741，未发现本轮新增前沿。
E2E-021 保留 FAIL，后续 redial 阶段未到达，不能写成通过。
失败现场内的 owned daemon/sidecar PID 均已退出。

7. 证据与交付范围

本轮证据目录：docs/notes/send-links-hotfix-core-acceptance2-20261016/。
probes/ 保存历史图、立即识别、真实排干、E2E-021 和延时复现探针。
records/ 保存准入摘要、红绿结果、场景原文及全量对照。
本机完整日志：/private/tmp/hotfix-accept2/。
旧判词和旧 WORM 证据保持原字节。
新增返工 diff 的 git diff --check 通过。

真实生产 PAC 库、跨机网络、真实 provider 和 Lark：UNKNOWN。
未 push，未开 PR，未向其他 agent 发消息。
未启用 extensions.worktreeConfig，未执行 git config --worktree。

Contract-Impact: 本验收仅新增判词和复现证据，不修改产品或已发布契约。
Skill-Impact: 本验收不改变 agent 操作方式，无 skill 改动。
