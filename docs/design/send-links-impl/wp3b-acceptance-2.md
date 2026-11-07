REJECTED

WP3-B 返工后二次独立验收

验收对象：wp3b/send-links，dddab588bb1e5e58d3bf32c1ea3b65bbf57e3056。
对照集成头：refactor/org-structure-constrain，c876bf91a3fdad64ec8d6d49f3b3dbe90d62856f。
验收日期：2026-10-15。
工作流：wf-4b2aa4e42527465980e71b62171a3e81 / wp3b-accept2。
请求：workflow-request:e7e46658fb324a219236f761f8191719。
deadline：1792066272973。

结论：第一次验收的 R1、R2、R3 已独立复核，不再作为拒绝理由。summary、pending、read_body、wire 和 loud 通知的焦点验证通过；未发现这些行为的新增回归。但是，完整 daemon/kernel/tooling 组仍有两条不在预许可清单中的失败，尚未得到同一失败的基线复现或充分因果归因。不能把它们直接算作环境红。因此，本次仍为 REJECTED。新增返工项见 R4、R5；这不是判定这两条一定由 WP3-B 产品代码引起。

1. 执行边界与复现环境

PASS：主 worktree 未编辑 src/、tests/；仅新增本报告。未 push、开 PR 或给 agent 发消息。Python 命令均经 uv；未修改产品依赖或根锁。测试使用仓库隔离 home/socket/端口 fixture，未操作生产 daemon、凭据或用户配置。

使用 Node v24.21.0，PATH 前缀为 /Users/bobadmin/.npm/_npx/387698761821791d/node_modules/node/bin。分别执行 npm --prefix src/gui ci --ignore-scripts 和 npm --prefix desktop ci --ignore-scripts，均成功。完整组删除大小写 HTTP_PROXY、HTTPS_PROXY、ALL_PROXY，设置 NO_PROXY/no_proxy=127.0.0.1,localhost。未用低版本 Node 或缺依赖作为 GUI 免责理由。

以下 pytest 命令均设置 PYTHONPATH=$PWD/src。默认并发数为 -n 6；单测归因明确使用 -n 0 或 -n 2。Python 为 3.12.13，LibCST 为 1.9.0。scratch 首次直接 pytest 入口曾因 LibCST 未进入该 venv 而收集失败；补齐 scratch venv 后重新执行完整命令，不把收集失败冒充行为结果。

scratch detached worktree 均位于指定 scratchpad：
/private/tmp/claude-501/-Users-bobadmin-Workspace-HyprialOS-hq-harness-bridge/87d11f16-52cb-42cd-8e0d-2101ff5c5d66/scratchpad/
使用 wp3b-accept2-r1、wp3b-accept2-red、wp3b-accept2-intg；交付前删除。

本地辅助日志位于 /private/tmp/wp3b-accept2-logs/。本报告记录关键命令、结果、失败节点和判据，不以临时日志作为唯一证据。

2. R1 正文精确守护：PASS

独立执行等字节数负控，而非仅阅读 7d3b47daf 或返工报告。目标为 tests/integration/actor_preservation/delivery/channel/test_registration.py::test_channel_session_registration_and_pending_read_are_daemon_authoritative。

在 detached 7d3b47daf^（45010a558），只把 support.py 的正文 durable body 改为 durable BODY，两者均为 12 个 UTF-8 字节，保持 summary、ID、长度等不变：1 passed。这证实旧测试确实漏检错误正文。
在 detached dddab588b 执行同一改动：1 failed，失败落在 assert body["message"] == "durable body"，实际为 durable BODY。
恢复正确正文后：1 passed。
命令形式：uv run --extra test pytest <上述节点> -q -n 0。
对应日志：r1-parent-wrong.txt、r1-head-wrong.txt、r1-head-correct.txt。

返工新增的 read_body 精确比较恢复了原守护；FakeInbox.fetch_pending 的 only= 参数适配保留精确筛选。返工没有用 summary 或 bodyBytes 代替正文等值断言。read_body 的 receipt、重复读取和 gate 守护另由焦点测试覆盖，不因该 registration fixture 隔离 publisher 而消失。

3. R2 完整组与 session persistence fd：PASS（本次未复现）

不是以单文件通过代替完整组。主 HEAD 独立执行：
uv run --with libcst==1.9.0 --extra test pytest tests/daemon tests/kernel tests/tooling -q -n 6
结果：6 failed, 8576 passed, 5 skipped, 7 xfailed, 1 xpassed，380.06 秒。

再在 detached 当前集成头执行完全相同的组命令：
6 failed, 8573 passed, 5 skipped, 7 xfailed, 1 xpassed，332.34 秒。

主 HEAD 的补充全套 tests/ 也已完成。以上三次均没有 session persistence fd teardown ERROR，没有 test leaked file descriptor 诊断。session 相关测试、tests/conftest.py 与集成头的对照未发现差异。因此，本次没有需要按 R2 条件追加归因的 fd 泄漏现场；不把“本次未复现”写成“历史泄漏已经修复”。也不引用返工报告的文字代替本次完整执行。
对应日志：daemon-kernel-tooling.txt、intg-daemon-kernel-tooling.txt、all-tests.txt。

4. R3 contract 实际口径：PASS

git diff --quiet 60179f3e1 HEAD -- contract/ 返回 0；WP3-B 没有修改 contract/。
uv run --extra test python contract/wire/golden/run-python.py：RESULT GREEN 27/27。
uv run --extra test python contract/wire/golden/validate.py：RESULT PASS 21 envelope fixtures, 27 expectation cases, 30 coverage tags。
tests/tooling/contracts 实际收集 72 项，包含在本次 204 项通过的焦点组中。

因此，正确口径是 21 个 envelope fixture、27 个 golden expectation case、30 个 coverage tag、72 个 tooling 测试。不能把 72 称为 golden fixture 数，也不能声称 unchanged contract 已覆盖全部新语义。WP8 的新增契约义务仍然存在，见第 11 节。
对应日志：golden.txt、golden-validation.txt、contracts-collection.txt、focus.txt。

5. 先红与三切片审查：PASS

独立复现核心先红：detached 0b0de0072，git cherry-pick --no-commit ff5525de1，只叠加测试，不叠加实现。执行 summary/test_send_summary.py、test_pending_shape.py、test_call_paths.py：6 failed, 20 passed。
六条失败依次为：
- test_a_send_without_a_summary_is_refused_before_any_side_effect。
- test_a_reply_without_a_summary_is_refused_and_leaves_the_request_pending。
- test_the_row_carries_the_summary_and_body_size_without_the_body。
- test_a_pre_summary_row_reads_as_null_summary。
- test_the_two_loud_notices_carry_a_derived_summary。
- test_the_human_loud_path_delivers_the_notice_text_unchanged。
对应日志：red-reproduced.txt。与 red-tests-before-cut.txt 的六条一致。

已对照 red-after-cut.txt、slice-a.txt、slice-b.txt、slice-c.txt 与三个实际提交 e716cae6c、4996929f0、bf428e6e7 的 tests diff。red-after-cut 含 331 条 FAILED、57 个文件；按各提交改动文件交集重新统计，A 为 46 条，B 为 176 条，C 为 67 条。这个统计不等于各切片整个测试范围的先红总数：A 范围记录 51 条；B 的范围还含已有环境红；C 的 69 条还含不在修复文件交集中的 PAC events 两条。未把这些不同口径加总或包装成新增回归数。

审查了 A 全部 8 个改动文件、B 全部 22 个改动文件、C 全部 9 个改动文件，并额外审查返工 support.py。以下明确列出每片至少 8 个测试文件及所保留的守护。

A（共同前缀 tests/daemon/）：
application/messaging/test_open_requests_from.py：请求来源、数量、身份精确断言不变。
application/ports/test_daemon_capability_wiring.py：发送能力与拒绝路径不变。
application/ports/test_daemon_visibility_wiring.py：可见性隔离不变。
application/transport/channel/delivery/test_delivery.py：fetched/ack/receipt 断言不变。
application/transport/channel/routing/test_routing.py：notice 按新 summary 取展示文本。
application/transport/channel/sessions/test_sessions.py：正文移到 read_body 精确比较，session fence 不变。
dispatch/without/test_dispatch_without_pac.py：dispatch、权限和请求状态断言不变。
inbox/idempotency/test_agent_send_idempotency.py：幂等、冲突、提交次数断言不变。

B（共同前缀 tests/daemon/，以下 8 项之外的同提交文件也已逐段审查）：
channels/mcp/contracts/test_mcp_daemon_alignment.py：工具参数及 daemon 调用对齐。
channels/mcp/lifecycle/test_mcp_channel_owner_migration_e2e.py：实际 owner/session 迁移与权限结果。
delivery/identity/test_sender_identity.py：发送者身份。
delivery/message/test_authenticated_message_sender.py：认证与伪造拒绝。
delivery/origin/test_inbound_origin.py：正文 hi、legacy 改由 read_body 精确比较。
delivery/sender/test_visibility_sender_canonical.py：canonical sender、可见性与请求状态。
identity/aliases/test_alias_input_surfaces.py：别名解析后的精确身份。
links/service/test_wiring.py：link gate、拒绝与提交结果。
其余 routing、squire、workers、transport 改动主要为输入增加合法 summary，未删其 gate、ack、提交次数或身份断言。

C（以下恰为 8 个测试文件，另审 helper tests/kernel/actors/identity/support.py）：
tests/integration/actor_preservation/delivery/channel/test_registration.py：pending 精确形状与 read_body 正文。
tests/integration/actor_preservation/delivery/channel/test_reply_settlement.py：reply/ack/原请求状态。
tests/integration/identity/contracts/test_identity_contract.py：跨入口身份一致。
tests/kernel/actors/identity/test_mailboxes.py：actor mailbox 及拒绝行为。
tests/kernel/actors/identity/test_resolution.py：解析结果及错误码。
tests/kernel/actors/identity/test_wire_delivery.py：wire 提交与发送者。
tests/shell/compatibility/test_target_kind_honesty.py：CLI 目标类型与错误结果。
tests/tooling/architecture/identity/test_sender_identity_gate.py：身份架构守护。

没有在三片迁移中发现新增 skip/xfail、把严格相等改成宽松包含、删除权限/幂等/提交次数守护，或用假 gate 代替本应执行的产品判断。集成带入的 harness capabilities 从 UNSUPPORTED/None 改成 NATIVE/"app_server"，属于 WP5-B 新能力的精确期望替换，不是 WP3-B 放宽断言。

6. 删除 message 行的穷举复核：PASS

独立读取 git diff 60179f3e1 HEAD -- tests 的完整输出，不采信“4 处”概括。共有 69 条删除行含带引号的 message 键，分布在 23 个文件。逐行区分输入字典与输出守护后，63 条是发送输入的同一行补 summary；真正涉及输出正文或 notice 展示守护的共 6 条。

这 6 条逐项处理如下：
1）daemon/application/transport/channel/routing/test_routing.py：notice 的 original-1 包含断言从 message 迁到 summary，比较强度未变。
2）daemon/application/transport/channel/sessions/test_sessions.py：期望 pending 字典删除 durable body 后，同一测试通过 read_body 精确断言 durable body。
3）daemon/delivery/origin/test_inbound_origin.py：hi 改为 read_body["message"] == "hi"。
4）同文件：legacy 改为 read_body["message"] == "legacy"。
5）integration/actor_preservation/delivery/channel/test_registration.py：期望 pending 字典中的 durable body 已由返工恢复为 read_body 精确断言，且通过第 2 节负控。
6）同 registration 文件：notice 的 original-1 包含断言迁到 summary，比较强度未变。

完整删除行计数如下；以下路径均以 tests/ 为前缀，合计 69，不能只数显式 assert 而漏掉期望字典：
1 daemon/application/ports/test_daemon_capability_wiring.py
1 daemon/application/ports/test_daemon_visibility_wiring.py
1 daemon/application/transport/channel/routing/test_routing.py
1 daemon/application/transport/channel/sessions/test_sessions.py
2 daemon/channels/mcp/lifecycle/test_mcp_channel_owner_migration_e2e.py
2 daemon/delivery/origin/test_inbound_origin.py
1 daemon/delivery/sender/test_visibility_sender_canonical.py
1 daemon/harnesses/pi/headless/test_pi_headless_worker_fetch_e2e.py
5 daemon/identity/aliases/test_alias_input_surfaces.py
4 daemon/links/service/test_wiring.py
4 daemon/routing/addressing/test_agent_address_normalization.py
1 daemon/routing/test_route_attribution_targets.py
4 daemon/transport/first/test_local_first_self_node_delivery.py
4 daemon/workers/test_worker_relay.py
2 integration/actor_preservation/delivery/channel/test_registration.py
13 integration/actor_preservation/delivery/channel/test_reply_settlement.py
1 integration/identity/contracts/test_identity_contract.py
1 kernel/actors/identity/support.py
10 kernel/actors/identity/test_mailboxes.py
5 kernel/actors/identity/test_resolution.py
2 kernel/actors/identity/test_wire_delivery.py
2 shell/compatibility/test_target_kind_honesty.py
1 tooling/architecture/identity/test_sender_identity_gate.py

7. 行为与调用方验证：PASS

焦点命令：
uv run --with libcst==1.9.0 --extra test pytest tests/daemon/inbox/summary tests/daemon/application/transport/channel/summary tests/tooling/contracts tests/integration/actor_preservation/delivery/channel -q -n 4
结果：204 passed。

另写临时独立探针，不改仓库测试：send/reply 分别缺失 summary、空串、整数、孤立代理项 U+D800、171 个“摘”（513 UTF-8 字节），共 10 项。全部精确检查 SUMMARY_REQUIRED、error.data、未提交、未 ack、dispatch gate 状态不变。missing/empty/not_string/invalid_utf8 的 data 分别含 field=summary、对应 reason、maxBytes=512；超限 data 恰为 summaryBytes=513、maxBytes=512。
wire 另测缺失、null、整数、列表、合法字符串五种 summary，仅字符串映射到 InboxMessage.summary；原 envelope 和排序紧凑 JSON payload 字节不变。合计 15 passed。日志：probes.txt；探针：test_independent_probes.py。

pending：精确 key 白名单排除 message/payload；legacy summary 为 null；普通消息只有 summary/bodyBytes 等元数据；fetched=true 形状相同。notice 的 summary 为完整 notice 文本、bodyBytes=0，read_body 返回 MESSAGE_BODY_UNAVAILABLE。
read_body：原正文精确返回，只首次 fetch 指定行；不改其他行；保留首次 stamp；重复读取不重入 gate、不重复 receipt；已消费行只读；外人/未知行不可读；gate deny/unbound/异常、未获准而 ack/过期均不泄露正文；IPC 认证、fabricated/superseded fence 及 onBehalfOf 隔离均通过。
loud：unavailable_notice 与 no_progress_notice 的 summary 精确等于 summary_from_text(原文)，非空且不超过 512 字节；面向人的通知仍发送原 owner-facing 文本，不用摘要替换。FakeInbox.submit 只是记录对象，不替产品补摘要。

发送端审查：CLI send/reply、workflow control、fs invite、Lark auth、Lark worker IPC、用户代理与转发、dispatch、routine、PAC notification、mechanism answer、MCP server/channel、Pi bridge/attach 均有传入或确定性派生 summary。InboxNotificationIo.deliver 对必填 summary 继续执行校验。转发优先保留原 summary。
读取端审查：Codex carrier 经 injection_from_row 只取 summary/bodyBytes；Pi parsePending 和展示使用摘要并指引 read_body；MCP/channel 与 CLI read 已接入新读正文入口。未发现仍把普通 pending.message 当正文的产品读者。遗留耐久行的 null summary 不由读端偷读正文补齐。

8. 完整运行结果与归因：FAIL

shell/integration 命令：
uv run --with libcst==1.9.0 --extra test pytest tests/shell tests/integration -q -n 6
结果：27 failed, 3662 passed, 7 skipped，223.36 秒。
其中 20 条 squire/setup 和 login/device t11 属预许可既有红；PAC events 两条按裁定 76 交 WP8。GUI lifecycle 五条和 CLI inventory 本次没有失败，不能据此反称本次跳过 GUI。
另外四条已在 detached 当前集成头独立复现：
- native_harnesses/test_native_runtime_entries.py::test_generated_stop_hook_runs_without_cli_or_mcp_imports：两个 turn_pulse import，而期望一个。
- pac/expansion/test_effect_races.py::test_preflight_git_budget_finishes_before_caller_timeout：bounded budget/调度时限失败。
- transfer/p2/e2e/test_two_daemon_chain.py::test_the_chain_lands_on_the_target_and_retires_the_source：隔离 harness 的 Pi SDK package root 找不到；未去读取或修复所指私有安装。
- actor_preservation/delivery/test_actor_preserved_provider_failure_settlement.py::test_transient_backoff_and_budget_survive_actor_and_daemon_rebuild：同一 transient-restart-1 尚未 acknowledged。
对照日志：intg-stop-settlement.txt（2 failed）、intg-transfer-preflight.txt（2 failed）。因此这四条不新增 WP3-B 产品返工。

daemon/kernel/tooling 的 6 条失败逐项为：
- pi/configuration/test_pi_controlled_loader.py::test_t04_t05_t06_real_sdk_loads_c_and_trusted_project_with_git_boundary：SDK 1.2.0 与 0.85.1 期望不同，预许可既有红。
- kernel/platform/test_platform_actor_preservation.py::test_posix_listener_keeps_request_owner_and_reserve_fd：AF_UNIX path too long，预许可既有红。
- tooling/refactor/review/test_review_r3.py::CrossStageTests::test_py04_generated_module_reachable_in_next_stage：confirm-mismatch。已在集成头将该测试与 engine/test_equivalence_failpoints_edges.py::PlanApplyEquivalenceTests::test_token_binds_tool_code 用 -n 2 联跑三次，三次均 1 failed, 1 passed，且均为同一 confirm-mismatch。后者临时修改共享 imports.py，导致前者 plan/apply 指纹不同；不是 WP3-B 改动。日志 intg-token-race-correct-1/2/3.txt。
- lifecycle/process/manager/coordinator/test_wait_without_polling.py::test_a_waiting_caller_reads_the_journal_rarely_and_wakes_on_completion：集成头焦点也失败，日志 intg-failure-probes-module.txt；不是仅凭未改代码判既有。
- home/test_home_lease_actor.py::test_stalled_renewal_fences_lease_at_existing_stale_threshold：尚未充分归因，R4。
- lifecycle/process/manager/coordinator/test_coordinator.py::test_slow_reservation_does_not_hold_manager_condition_or_escape_drain：尚未充分归因，R5。

补充遵循仓库全套纪律：
uv run --with libcst==1.9.0 --extra test pytest tests/ -q -n 6
结果：33 failed, 13403 passed, 15 skipped, 7 xfailed, 1 xpassed，470.83 秒。没有 fd teardown ERROR。skip/xfail/xpass 原样列出，不计为 PASS 证据。
除上述重复项，全套额外出现三条已在当前集成头逐条复现的失败：
- identity/agents/registry/entity/test_capabilities.py::test_capmx_codex_interactive_has_no_injected_tools：新 Codex 能力的旧期望。
- identity/providers/lifecycle/availability/test_attempts.py::test_pc2_a_completed_slow_turn_is_not_reported_or_killed：LoudInbox 缺 fetch_pending。
- daemon/harnesses/pi/extension/test_pi_harness_attach_extension.py::test_wait_names_every_id_from_the_last_observation：解包观察结果数量不符。
基线焦点结果 3 failed, 1 passed，日志 intg-extra-failures.txt；通过项为 asserted-venv-fence mutation。
另两条为测试并发共享资源问题：IdempotencyTests::test_tampered_output_refused_not_already_done 的 confirm-mismatch，以及 test_key_mutations_are_killed[asserted-venv-fence] 的 copytree 源 tests/fd-guard-probe-* 目录在复制中消失。后者在基线完整组的 regular-file-fence 参数也出现同一 ENOENT；两处均指向 isolation/test_fd_hygiene_collection.py 创建后删除的临时目录。主 HEAD 串行重跑这两项均通过。不能将 copytree 尚未到达产品断言的失败写成 wire golden 失败。

基线完整组的六条为 SDK 版本、Pi wait、IPC cost heartbeat、coordinator independent effects、POSIX socket 长度、resolver regular-file-fence copytree。基线没有重现 R4/R5 的两个具体节点；不能拿“同目录另一条也红”代替同一失败归因。

9. 结构、边界、格式和合并：PASS，另有一项接受的既有 FAIL

uv run --no-project --python 3.12 python scripts/ci/check_structural_limits.py：exit 0，violations=[]。
uv run --no-project --python 3.12 python scripts/ci/check_module_boundaries.py：exit 0，violations=[]。
uv run ruff check .：All checks passed。
git diff --check（工作区）及 src/tests 的验收范围检查均通过。
git diff --check $(git merge-base HEAD refactor/org-structure-constrain) HEAD：通过。
git diff --check 782854399 HEAD：通过。

git diff --check 60179f3e1 HEAD：FAIL，exit 2。唯一位置为 docs/evidence/org-structure-constrain-2026-10-14/wp5b/probe-scripts/probe.py.txt:27 的 trailing whitespace。主 HEAD 和当前集成头该文件 blob 均为 abc848aead5b812ed20dcf1480b49324937203ec，是集成带入的 WP5-B 证据，不是 WP3-B 新增；且位于证据目录，未擅改。此 FAIL 被接受为既有偏差，不伪报全范围 diff --check 干净。

git merge-tree --write-tree refactor/org-structure-constrain HEAD：exit 0，无冲突，tree=77baa9e2e083f4c4c4a26f82a53b1c4acb74edf3。集成头固定为本报告开头的 c876bf91a；不代表未来集成头无需重验。

10. 新增返工项与合并待办

R4：完整组 home lease 失败归因未闭合。主 HEAD 原完整组失败在 entered.wait(1)，主 HEAD 串行同节点连续三次失败；新 pycache 也失败。当前集成头完整组不失败，该节点串行三次均通过。主 HEAD 的整套 tests/ 又没有此失败，说明有执行条件差异。被动观察只记录到 heartbeat-deadline、age 约 0.128 秒、keepalive=0.03 秒、renewing=False、renewalTiming=None；这支持调度时限方向，但还不是完整因果证明。
补充交叉检查：同 HEAD detached worktree 使用主 venv 的 python 入口通过；主 worktree 以 uv run --no-project --python <主 venv>/bin/python 通过，以 python3 入口失败；基线以主 venv/python3 仍通过。源码 configuration、kernel actor_runtime 与测试文件均无差异。不能因为这些文件未改，就跳过 checkout/入口条件差异而直接豁免。
返工要求：记录实际解释器、模块来源、timer/actor/effect 时序与隔离环境，给出同条件基线复现或其他可审计的因果解释，再重跑完整组；不得延长断言时限、删除断言或加 skip 冒充修复。
关键日志：head-home-1/2/3.txt、head-home-fresh-pycache.txt、intg-home-1/2/3.txt、head-home-observation.txt、head-detached-home.txt、head-no-project-home.txt、head-no-project-python3-home.txt、intg-home-head-python3.txt。

R5：test_slow_reservation_does_not_hold_manager_condition_or_escape_drain 在主完整组出现 TimeoutError: lifecycle operation still running: slow-reserve。当前集成头焦点通过，主 HEAD 串行复跑也通过，整套 tests/ 未再次失败。基线另一个 coordinator 节点失败不能替代此节点归因。返工要求：提供该超时的实际调度/锁等待证据及基线对照，或在同命令完整组复验并明确解释差异；保持原 manager condition、drain 与 bounded wait 断言，不以“timing”标签直接放行。
关键日志：daemon-kernel-tooling.txt、intg-failure-probes-module.txt、head-flakes-serial.txt、intg-daemon-kernel-tooling.txt。

接受的偏差：第 8 节已复现的基线失败、已证明的工具并发竞态、裁定 76 的两条 PAC events、WP5-B 证据空白问题。它们仍为 FAIL，不是 PASS；不要求本轮越界修改。R4/R5 不在接受偏差内。
合并待办：先关闭 R4/R5，再对新集成头重跑 merge-tree 与两大完整组；保留 R1 负控及本报告列出的精确正文守护。未来 CI 绿灯不能代替这里缺失的归因材料。

11. WP8 Contract-Impact 与边界

Contract-Impact：有已发布语义变化，按既定分工由 WP8 统一补 contract/，本报告不宣称 contract unchanged 等于无影响。至少需要：
1）message.send/reply 的 summary 必填、UTF-8 512 字节限制、SUMMARY_REQUIRED 及精确 error.data；拒绝前无提交/ack/gate 副作用。
2）普通 pending 行不再含正文，summary/bodyBytes/kind 的形状与 legacy null；notice 无 body；read_body 的正文、fence、权限、fetch 幂等和不可读错误。
3）wire 的加性 summary 映射，缺失或非字符串兼容、原 payload 字节与 summary-less digest 不变、带 summary 的幂等身份。
4）unavailable/no-progress loud 通知携带派生 summary，面向人的原文本不变。
PAC events 两条依裁定 76 纳入 WP8，不在 WP3-B 偷改契约 runner 以求绿。

Skill-Impact：本次只有验收报告，没有改变 agent 操作接口；产品端 send/reply 必填 summary 与按需读正文的技能同步仍应由对应实现/契约工作负责。
BLOCKED：无被隐去的必跑命令。R4/R5 是验收 FAIL，不是用 BLOCKED 掩盖失败。真实 Lark、外网跨主机和生产环境未实测，本报告不授予这些场景 PASS。
