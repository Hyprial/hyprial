# UUID 寻址：交接给 HQ（cloud-claude → HQ，2026-11-04）

- 交接时间：2026-11-04。应 Allen 要求停止，由 HQ 接手。
- 交接点：`intg/uuid-addressing-20261023` @ `d7f1e8d7b`（已推送），WIP PR #1886 → `release/v0.5.5.4`
- 状态：P2、P3、P4 已实现；`ID_FORM_MINTING = True`；已用 merge 并入 release 最新提交，不落后。
- 规格与全部决定：`docs/design/uuid-addressing-implementation-2026-11-03.md`。§2 是规格，§4 是 D1–D7，§6 是 D8–D15，§5 和 §7 是待裁问题 Q1–Q4。
- 合约：`contract/identity/agent-addressing.md`、`contract/identity/agent-naming.md`。

## 0. 2026-11-07 更新（交接之后的变化，以本节为准）

- 分支改名：`intg/uuid-addressing-20261023` 已改名为 `intg/uuid-addressing`，PR #1886 → `release/v0.5.5.4` 跟随新分支。下文提到旧分支名的地方，指的都是这条分支。
- 0.5.5.4 合入：`release/v0.5.5.4`（@`c32b005ff`）已用 merge 并入（merge 提交 `3738b5cb1`），随后补了 daemon specs、selectors、tests/structural、重新生成的 openapi/changelog 和评审修复（推送头 `2bc489339`）。
- GUI 拆分：CI 规定进 `release/*` 的 GUI 组件改动必须来自 `intg/gui*` 分支（`scripts/ci/ci-plan.mjs`）。所以 GUI 路径的改动（显示名、已声明的拒绝码、重新生成的 GUI client）移到叠在 #1886 之上的 `intg/gui-uuid-addressing`，单独开 PR → `release/v0.5.5.4`。#1886 只保留非 GUI 改动。
- **合入顺序（定死）**：只经 GUI PR（`intg/gui-uuid-addressing` → `release/v0.5.5.4`）合入。它已包含 #1886 的全部提交，合入后 #1886 变成 no-op，直接 close。若必须两者都合，只能在同一窗口内先后合入，且 #1886 自己那次 CI 的 `gui-unit` 红是预期的。**不要让 #1886 单独先合进 release**，原因有两个：
  - #1886 的 diff 含 `contract/*.json`、`scripts/ci/*` 这类 shared 路径，`ci-plan` 走 full validation（`gui=true`），`gui-unit` 会跑。`agent.rename` 变成 HTTP 暴露后，`gui/tests/features/user-settings/contract-gap.test.ts` 的计数应为 87 / 94，但这个计数更新只在 GUI 分支上，所以 #1886 单独跑时这个测试红 3 条。
  - #1886 上 `ID_FORM_MINTING = True`，`agent.create` 返回 `agent:<o>:<n>:a.<id>`；而 `gui/server/setup/checkpoint.mjs` 的 actorUri 校验仍要求 `:squire$`，GUI 首跑在 agent 建好之后写 checkpoint 会报 `SETUP_CHECKPOINT_UNSAFE`，之后每次 setup 都卡在 `SETUP_REVIEW_REQUIRED`。修复（checkpoint schemaVersion 2 / `actorAgentId`）只在 GUI 分支上。
- **两个 PR 的正文都要有 `Contract-Impact:` 行**。`check_daemon_contract_diff.py --base-ref origin/release/v0.5.5.4` 报 2 条 BREAKING：`agent.create` 的 `resumeSnapshot.required` 新增 `agentId` / `displayName`；`resumePreviousSnapshot.oneOf` 少了一个 variant。没有这一行 contract projection step 会红。
- 合入前提（Allen 2026-11-06）：HQ 评审和真实环境 E2E 不是 merge 前提，它们是 `release/v0.5.5.4` 上的动作。§1 待办 a、§4 和 §6 里相应的旧说法已一并改掉。
- squire 裁定（Allen 2026-11-06）：squire 降级为完全普通的 agent，后端不对它做任何特殊处理。#1886 只做最小改动：
  - `address_admission.SERVICE_ACTOR_NAMES` 去掉 `squire`（保留 `pac-expansion`）。squire 的名字地址和其他 agent 一样被 retire，destroy 之后名字冻结。
  - daemon 自己发的通知（autoupdate 重启通知、orgfs owner-notice）不再按名字以 squire 名义发送，改用 daemon 的服务身份 `dispatch-coordinator`。
  - `SquireSetup` 写进 users.json `profile.agents` 的是 management 返回的官方地址（`a.<id>`），不再是按名字拼出的 URI。
  - 前端自己保存哪个 agentId 是用户的 squire，不按名字，也不靠后端标签。user-delivery 那套术语（`squireChannel`、`TARGET_SQUIRE_*`、user-proxy、持久化名字）保持不变。
- 合入状态（2026-11-07）：#1886 与 GUI PR #2546 均已合入 `release/v0.5.5.4`（@`baf3f86b1`）。
- 降级裁定（Allen 2026-11-07）：P4 是单向迁移（U1，无兼容窗），P4 之后**唯一支持的降级是恢复升级前备份**（backup rollback）。不再支持保留迁移后的库直接运行旧 build（clean-row rollback）：旧 build 会带着它无法路由的 id 形态地址运行。此前 AT04 按临时方案 (a) 要求旧 build 原样读出 id 形态行，该预期已撤销，AT04（`contract/e2e/run-hosted-upgrade.py`）去掉了这条路径，只验证备份还原，并在还原后再升级一次，确认 P4 结果相同。
  - 产品在 P4 之前不自动备份：P4 在新 daemon 第一次启动、打开 registry 时执行（`registry/_core.py` 的 `AgentRegistry.__init__`），`hyprial daemon upgrade` 的安装、重启和 upgrade-step 注册表（只有 `mcp_entry`、`autoupdate_timer`）都不复制状态。备份必须由运维在升级前完成。
  - 需要备份并在降级时一起还原的是 P4 改写的全部存储：`state/agents.sqlite3`、`state/lifecycle-operations.sqlite3`（desired-state）、`state/adapters.sqlite3`、`state/routines.sqlite3`、`state/pac-graph.sqlite3`（后三者库内有 P4 迁移标记），`HYPRIAL_HOME/state/org/org-acl-grants.json` 及其迁移标记，以及 `HYPRIAL_HOME/agents/`。消息库 P4 不改写（裁定 X），可以保留。运维步骤见 `docs/notes/runbook-agent-transfer-mvp-2026-09-25.md`（路径二）和 `CHANGELOG.md` 0.5.5.4 的 Upgrade notes。
- 剩余的后续工作：
  1. 删除后端 squire 链，单独一个 PR：`management.squire.ensure`、`management.squire.prepare`、`setup.squire.prepare`、`SquireSetup`、CLI `hyprial squire`。CLI 改为自己保存 squire 的 agentId。
  2. operator synthetic principal 移除提案。
  3. GUI 会话 conversation-id 的连续性。
  4. Q1–Q4：除非 Allen 另行裁定，维持当前实现。
  5. `ID_FORM_MINTING` 开关删除和 §4 的低危遗留项，照旧。

## 1. 交接 prompt（直接转发给接手的 session）

```
你接手 hyprial 的「稳定 id 寻址」（UUID addressing）收尾，向 HQ 研发负责人汇报（经 Allen 转）。

0. 仓库与身份
- 仓库 https://code.hyprial.com/HyprialOS/harness-bridge ，先读 AGENTS.md。
- 分支：intg/uuid-addressing-20261023，交接点为 d7f1e8d7b；WIP PR #1886 → release/v0.5.5.4。
- 提交身份：cloud-claude <cloud-claude@agents.hyprial.com>，末尾加 Co-Authored-By: Claude <noreply@anthropic.com>。
  禁止 force-push 和改写历史。同步 release 一律用 merge，不用 rebase。

1. 先读（按顺序）
- docs/design/uuid-addressing-handoff-2026-11-04.md（本文件）
- docs/design/uuid-addressing-implementation-2026-11-03.md（规格，以及决定 D1–D15、待裁问题 Q1–Q4）
- contract/identity/agent-addressing.md、contract/identity/agent-naming.md
- docs/design/uuid-addressing-migration-path.md 的 §9、§10（Allen 裁定 U1–U3、X）

2. 现状
- P2、P3、P4 已实现，id 寻址开关已打开（ID_FORM_MINTING = True）。
- 最终全量测试：18587 passed。相对基线 9fa507288 只有 1 项新增失败，单独连跑 3 次都通过（时序抖动）。
  基线另有 33 项环境类失败，名单见本文件 §5。
- 真实环境 E2E 未跑（UNKNOWN）。

3. 待办（按优先级）
a. 派独立评审，并按 .agents/skills/hyprial-e2e 跑 E2E。建议场景见本文件 §3。（2026-11-07 更新：Allen 2026-11-06 裁定，这两项是 release/v0.5.5.4 上的动作，不是 merge 前提，见 §0。）
b. 裁定 Q1–Q4（见规格 §5、§7）。若 Q2 被否决：删掉 address_admission.py 的 _refuse_unclaimed_local_name 及其调用和测试即可。
c. 删除 ID_FORM_MINTING 开关及全部 id_form_minting() 分支，switch-off 路径的测试一并清理。
d. 低危遗留项和阶段 A 存储迁移，见本文件 §4。

4. 已定规则（不要再问）
U1 无兼容窗，发到短名报 AGENT_ADDRESS_RETIRED 并给出 id 地址；
X 短名永久冻结，只作只读别名读历史；
用户在 CLI/GUI 输入裸名、在边界解析成 id，属于输入便利，可以保留；
设备和用户只新增输出字段（deviceUid、agentId），不改路由键；
与阶段 A（#2323）冲突时以阶段 A 为准，后合的一方解决冲突。

5. 门禁与纪律：同原任务说明。每个提交先写失败的测试；改 JSON 字段或错误码要同步 contract/；
结构门、模块边界门、ruff 都要绿；测试隔离 HOME/TMPDIR；坑点见本文件 §6。
```

## 2. 已完成的内容

| 阶段 | 内容 | 主要位置 |
|---|---|---|
| P2 | `agents.agent_id` 派生列和唯一索引；`get_by_agent_id`；共享解析函数 `resolve_local_actor`、`official_agent_uri`；`actor_key`；daemon 侧 `_agent_address`；铸造点改走这两个入口 | `identity/impl/agents/addressing.py`、`registry/_core.py`、`application/actors/agents_registry.py` |
| P3 | id 形态铸造（`mint_agent_uri`）；`display_name` 列；`hyprial agent rename` 和 `agent.rename`；`agentId`、`displayName`、`deviceUid` 输出；`agent list` 显示为 `display(a.xxx)`；presence 和路由以 id 宣告；冻结名台账 `agent_name_ledger` | `registry/naming/`、`request_catalog/agent/rename.py`、`shell/.../agent/` |
| fail-loud | 发送入口报 `AGENT_ADDRESS_RETIRED` 或 `TARGET_UNRESOLVED`；投递侧对无人认领的收件人给终态失败（修复 10-23 事故）；会话指向已死的 id 时报 `SESSION_SUPERSEDED`；sender 和 `onBehalfOf` 同样处理（D8） | `application/actors/address_admission.py`、inbox 可选钩子 `recipient_claimed` |
| D4 只读别名 | 翻转前、收件人为旧名 URI 的消息，所属 agent 在 fetch、pull、wake、history、回复提醒里仍能读到 | `_consumer_read_keys`、`_frozen_name_read_alias` |
| P4 | identity 侧：`agents.uri` 改写，fence 和 retired refs 迁移，`agent_id` 列回填，台账建立前已销毁的名字补冻；外部存储：adapters、routines、PAC，在 link service 启动前执行；desired-state 会话迁移（D6）；消息行不回填 | `registry/naming/id_form.py`、`application/actors/stored_agent_uris.py`、`*/agent_uris.py` |

过程：每条并行线都走「实现 → 独立对抗评审 → 修复 → 复核」。翻转普查共五轮，相对基线的新增失败依次为 994 → 25 → 15 → 6 → 3。
复核查出的测试削弱（例如改用新名字导致断言恒真）已全部恢复：同名后继一律用 `rotate_secret_authority` 或补偿性 destroy 构造。

## 3. 建议的 E2E 场景

1. 存量 home 升级：`agents.uri` 改成 id 形态；adapters、routines、PAC 中存的地址改成 id；翻转前收件人为旧名 URI 的 pending 消息仍被对应 agent 消费（D4）。
2. 发往旧短名，收到 `AGENT_ADDRESS_RETIRED` 并带 id 地址；发往本节点无人认领的名字，报 `TARGET_UNRESOLVED`，不再「ACK 后停放」。
3. `hyprial agent rename` 之后：通过 id 地址收发正常，旧显示名被拒；destroy 之后再 create 同名，报 `AGENT_NAME_FROZEN`。
4. 跨节点收发与 presence：两个 daemon 之间以 id 宣告、投递和唤醒。
5. transfer（P2 链路）和 host-invite 落地后的地址与会话。
6. Lark 入站、出站：pin 指向 id 地址；转述行的显示（Q4）。
7. interactive 会话（Claude、Codex、Pi carrier）首次注册、心跳、owner 迁移后的定位。

## 4. 遗留项（都不阻塞合入）

- 删除 `ID_FORM_MINTING` 开关：规格要求翻转后另提交删除。
- 阶段 A 存储：inbox `link_ledger`、`link_task_bindings`、orgfs `link/*.json` 里持久化的名字形态 agent URI，阶段 A 定型后需要经 `stored_agent_uri_mapping` 做键值迁移。本线没有动这些存储。
- 已知低危项：
  - `shell/.../agent/runtime/claude.py` 的 `_channel_registration_confirmed` 仍用 CLI 拼出的名字 URI 做比对（Pi、Codex 已改为官方地址）。
  - `runtime/results.py` 的 `_mechanism_is_python_worker` 取 `parse_agent_uri(...)[2]` 作 harness 名；对 id 地址的行能否判对，未核实。
  - 回复提醒按 `created_at_ms` 合并，发送方时钟漂移时与批处理顺序可能不一致。
  - D11 提前建记录时，如果之后 start 失败，记录会保留。这与 CLI 先 `agent.create` 再 start 的行为一致，但未经裁定。
  - 首次注册在 coordinator 拒绝时，会留下一个没有会话的记录。
  - 存量 agent 的 `AGENT_EXISTS` 文案在 host-invite 路径上未逐一核对。
- PAC wf-* worker（Q3）：receipt 中的 `actor_uri` 仍是计划阶段的名字 URI，比较前先经 registry 解析；彻底解决要等 PAC 图 schema 升级。

## 5. 基线失败（`9fa507288`，接手时就存在，环境类为主）

缺 codex sandbox、Pi SDK、`ssh-add`、docker、GitHub token，以及 CI 工具链和负载时序相关：

  - tests/daemon/channels/adapters/test_review_e1c.py::test_pi_unsupported_cache_expires_on_new_daemon_epoch
  - tests/daemon/channels/mcp/lifecycle/test_mcp_channel_owner_migration_e2e.py::test_channel_survives_owner_migration_without_restart[asyncio]
  - tests/daemon/harnesses/codex/test_codex_carrier_settlement.py::test_transient_daemon_errors_never_settle_terminally
  - tests/daemon/ipc/test_agent_read_ipc.py::test_agent_list_cli_prints_the_registry_result_unchanged
  - tests/daemon/ipc/test_agent_read_ipc.py::test_agent_secret_ls_cli_keeps_the_grant_inventory_and_error_semantics
  - tests/daemon/links/bypass/test_bypass_ingress.py::test_ingress_non_member_is_refused_either_way[bypass-off]
  - tests/daemon/maintenance/health/test_database_health.py::test_healthy_database_checked_without_replacing_it
  - tests/daemon/network/test_peer_reachability.py::test_tcp_probe_reports_timeout_for_test_net_blackhole
  - tests/daemon/orgfs/join/test_supplier_presence.py::test_sync_fails_immediately_when_supplier_disappears_mid_page[callbacks-incomplete-3]
  - tests/daemon/orgfs/join/test_supplier_presence.py::test_sync_fails_immediately_when_supplier_disappears_mid_page[supplier-offline-1]
  - tests/daemon/orgfs/join/test_supplier_presence.py::test_sync_fails_immediately_when_supplier_disappears_mid_page[supplier-offline-3]
  - tests/identity/agents/capabilities/test_agent_git_ssh_capability_e2e.py::test_t16_home_staging_fidelity_e2e[asyncio]
  - tests/identity/agents/capabilities/test_agent_git_ssh_capability_e2e.py::test_t17_git_ssh_capability_commit_push_e2e[asyncio]
  - tests/identity/agents/capabilities/test_agent_git_ssh_capability_e2e.py::test_t17_partial_git_triple_dropped_and_unusable_sock_refused_e2e[asyncio]
  - tests/integration/request_http/test_gui_reference.py::test_shared_app_against_live_and_contract_mock
  - tests/integration/transfer/bundle/test_transfer_bundle.py::test_cli_export_passes_allow_live
  - tests/integration/transfer/bundle/test_transfer_bundle.py::test_export_refuses_a_live_source_home
  - tests/integration/transfer/commands/test_transfer_cli_structure.py::test_transfer_command_graph_matches_frozen_baseline
  - tests/integration/transfer/p2/e2e/test_codex_two_daemon_chain.py::test_codex_chain_lands_on_the_target_and_retires_the_source
  - tests/integration/transfer/p2/e2e/test_codex_two_daemon_chain.py::test_codex_tampered_bundle_refuses_and_leaves_the_source_held
  - tests/integration/transfer/p2/e2e/test_codex_two_daemon_chain.py::test_codex_w2_w3_the_export_boundary_re_enters_after_the_first_step
  - tests/integration/transfer/p2/e2e/test_codex_two_daemon_chain.py::test_codex_w5_after_the_target_answered_both_daemons_restart_and_the_report_holds
  - tests/integration/transfer/p2/e2e/test_codex_two_daemon_chain.py::test_codex_w6_before_the_complete_the_chain_closes_on_the_report
  - tests/support/ci/attach/test_wheel_assets.py::test_missing_github_token_is_not_evaluable_not_failure
  - tests/tooling/ci/hygiene/test_pr_hygiene_git.py::test_git_env_scopes_the_token_to_one_hardened_command
  - tests/tooling/ci/runner/test_ci_e2e_baseline.py::test_gate_precompiles_the_tree_and_the_hyprial_venv_before_any_scenario
  - tests/tooling/isolation/test_exit_independence.py::test_the_stall_dump_is_armed_before_the_proactive_exit_can_be_reached
  - tests/tooling/release/homepage/test_public_changelog_sync.py::test_the_candidate_token_travels_only_in_the_hardened_child_git_environment
  - tests/tooling/release/publish/github/test_publication.py::test_publish_names_ignored_parent_git_variables_without_values
  - tests/tooling/release/publish/github/test_publication.py::test_publish_token_never_reaches_the_log
  - tests/tooling/release/publish/github/test_snapshot.py::test_whitelist_constant_has_a_single_home
  - tests/tooling/verification/test_verification_instruments.py::test_devcheck_container_missing_is_loud
  - tests/tooling/verification/test_verification_instruments.py::test_devcheck_docker_missing_is_loud

已知会抖动（负载下偶发失败，单独跑能过）：`test_agent_migration_actor::test_timed_out_caller_keeps_accepted_effect_and_can_join`、secret_storage 迁移类、codex carrier 时序、orgfs supplier presence、fd hygiene 收集。

## 6. 坑点（本轮踩过的）

- **固定快照**：每次 merge 后都可能需要重新生成。
  - post-install 模块清单：`uv run python scripts/verify/regenerate_post_install_surface.py`
  - `legacy_user_uri` 白名单行号：`tests/tooling/architecture/identity/test_identity_construction_gate.py`
  - import ratchet：`scripts/ci/check_import_ratchet.py --write-baseline`
  - `NATIVE_ORIGINS`：`tests/integration/native_harnesses/test_native_resident_tripwires.py`，新增 daemon 文件都要登记
  - openapi：`scripts/verify/dump_daemon_openapi.py --write`
  - CLI surface：`scripts/verify/update_cli_surface_snapshot.py`
  - 启动事件对账：`_FORWARDING_SEAMS`
  - provider 术语 lint：新代码里不要出现 "provider" 字样
- **不要设置 `GIT_DIR`、`GIT_WORK_TREE`、`GIT_INDEX_FILE` 去跑测试**。tooling 测试的 git fixture 会继承这些变量，改写共享仓库。本轮发生过一次：本地分支被移走、身份被改成 `gate`、`core.worktree` 被指向别处，已恢复。
- **全量测试**：容器只有 4 核，`-n 3` 大约 45 分钟。和 lane agent 并行跑时，超时类测试会抖动；凡是新增失败，都要单独重跑确认。
- **`TMPDIR` 路径不能太长**，否则会触发 `AF_UNIX path too long`。
- **stop hook** 会要求把提交身份改成 noreply 并 amend。原任务说明指定了 cloud-claude 身份，并且禁止改写历史，本轮都没有照做。
- **forge token**：PR 正文可以改，review comment 可以发；评论要用新 token，带 issue scope。
- **Gitea 显示 `mergeable: false`**：交接时标题带 `WIP:`。评审和 E2E 已不是 merge 前提（§0），能否合入只看 CI。但 CI 有两个已知前提（§0）：#1886 单独跑时 `gui-unit` 红（contract-gap 计数和 checkpoint 的 `a.<id>` 支持只在 GUI 分支），所以只经 GUI PR 合入，或两者同一窗口合入；两个 PR 正文都要有 `Contract-Impact:` 行，否则 `check_daemon_contract_diff` 报 breaking=2 变红。

## 7. 分支清单

- 远端：
  - `intg/uuid-addressing`（原 `intg/uuid-addressing-20261023`）：交接点所在分支。`release/v0.5.5.4`（`c32b005ff`）已 merge 进本分支（`3738b5cb1`）；本分支经 PR #1886 → `release/v0.5.5.4`，**尚未合入**，按 §0 的合入顺序走 GUI PR。
  - `intg/gui-uuid-addressing`：GUI 部分，叠在 `intg/uuid-addressing` 之上，PR → `release/v0.5.5.4`，是两者进 release 的唯一入口（见 §0 合入顺序）。
  - `intg/uuid-flip`：开关打开的翻转分支，已全部 merge 回交接点，可以删除。
- 只在本地、已合入交接点的 lane 分支，没有推送，没有未合入的工作：`intg/uuid-lane-*`、`intg/uuid-r3-*`、`intg/uuid-flip-*`、`intg/uuid-flip2-*`、`intg/uuid-flip3-*`、`intg/uuid-flip4-*`。

## 8. 与 HQ 的沟通记录（#1886 上的 review）

- 1397：公共面变更声明。
- 1400：Q1–Q3。
- 翻转进展和 Q4。
- P2–P4 完成，请 HQ 派评审和 E2E。

截至交接时，HQ 没有回复。
