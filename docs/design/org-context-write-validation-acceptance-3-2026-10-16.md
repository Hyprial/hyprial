org-context 写前校验三次验收，2026-10-16

结论：ACCEPTED。

二次判词剩余 3 项均已完成。
原二次验收脚本的 33 项探针全部通过。
补充探针的最终复跑为 36 项全部通过。
已通过的 R1 状态校验、R3、R4 未发现回归。
本结论只覆盖本次返工，不等于完整发版验收。

1. 验收对象

工作目录：/private/tmp/hf-orgctx-validate。
分支：feat/org-context-write-validation。
二次判词：8387fcfd6，docs/design/org-context-write-validation-acceptance-2-2026-10-16.md。
受验提交：c675a2262。
本次只新增判词、复现脚本和读数，不修改产品或测试。

解释器为 worktree .venv 的 CPython 3.12.13。
验收环境前置 Node v24.21.0。
所有 shell 中的 Python 命令均经 uv run 执行。
HOME、HYPRIAL_HOME、state 和 uv 缓存均指向本轮临时目录。
设置 HYPRIAL_NETWORK_ISOLATED=1。
最终焦点及红绿复跑均显式指定本轮独立 --basetemp。
未读取生产 org 槽、生产 daemon、凭据或用户级配置。
未启用 extensions.worktreeConfig，未使用 git config --worktree。
未 push，未开 PR，未给任何 agent 发消息。
工作流 inspect/complete 仅用于本任务状态和回执。

2. 剩余 3 项的裁定

2.1 org status 恢复正文信息：PASS。

位置：src/hyprial/daemon/impl/org/store.py 的 status。
正文可解析、对应采纳记录缺失时，status 仍成功返回。
返回的 meta 为正文的 version、issuedAt、publisher。
status 和 show 均取自 OrgMeta.to_json，不另造字段映射。

独立探针的结果包含：
{"ok":true,"slot":"accepted","meta":{"version":3,"issuedAt":"2026-10-16","publisher":"alice"},"pendingCount":0,"pending":[],"warnings":[{"code":"ORG_ACCEPTANCE_METADATA_UNAVAILABLE","message":"accepted org-context has no readable adoption record; showing the parsed body only"}]}

结果未包含 accepted 采纳记录，也未编造 adoptedAt。
补充探针还覆盖记录 JSON 损坏和 publisher 不匹配。
这 2 种状态同样保留正文信息，并明确警告记录不可用。
有有效采纳记录时，原 adoptedAt 展示保持正常。

2.2 人读模式的警告只走 stderr：PASS。

位置：src/hyprial/shell/impl/cli/commands/org/commands.py。
show、show --full、status 均返回带 _org_human_view 的 CliResult。
人读渲染去掉 warnings；warn 通道仍负责 stderr。
--json 的数据保留结构化 warnings，不调用人读渲染。

亲自复跑三条命令的两种模式：
- 人读 stdout 包含正文信息，不含 warnings、警告码或警告文本。
- 每条人读 stderr 恰好 1 行警告。
- JSON stdout 恰好 1 个带 ok:true 的对象，含 warnings。
- JSON stderr 为空。

缺记录、记录 JSON 损坏、记录字段不匹配均满足上述分离。
有记录和空槽场景均不产生多余警告。
show --full 的 document、documentSha256 仍存在。
通用输出纪律焦点测试也已通过。

2.3 全 CRLF 合法文件的格式预检：PASS。

位置：同文件 org_context_validate。
格式校验使用换行归一化文本。
accepted_slot_issue 仍接收 read_bytes 得到的原始 payload。
格式校验不再把 CRLF 围栏误报为缺失。
摘要也未改用归一化文本。

补充探针写入含 19 个 CRLF 的完整合法文件。
原始字节 SHA-256：f70b95a1ea5fc0cb0c925d7cd491517f4b57510cbcbb7d1afca064ed985c29e7。
LF 文本 SHA-256：a2c81bbc1f95ad6c9e02e40540fc85681388bdb28f1ce6712778223126e5cd6f。
两者不同；采纳记录匹配原始字节时，acceptance_record 通过。
--path 和默认槽在两种输出模式下都退出 0。
JSON 为 ok:true、valid:true；人读为 org-context OK。
校验后文件原始字节不变。

仅留下 LF 摘要的采纳记录时，默认槽退出 1。
结果为 ok:false、valid:true，原因为缺少当前字节的采纳记录。
这确认 CRLF 正例没有绕过默认槽摘要核对。
原脚本的混合换行摘要正例也继续通过。

3. 先红后绿复查

红测试确实先于修复提交，且失败来自行为断言。
本次在 2 个独立 detached worktree 亲自复跑。
导入路径读数确认使用对应 worktree 的产品实现。
依赖和解释器使用受验 worktree 的 uv 环境。

第 1 组：status 正文信息与 stdout/stderr 分离。
红提交 39dd8ec40，修复提交 52d21f9d4。
52d21f9d4 的直接父提交就是 39dd8ec40。
红提交：6 failed，10 passed，退出 1。
失败为 store/status 缺 meta、CLI/status 缺 meta、4 项 stdout 警告断言。
修复提交运行相同集合：16 passed，退出 0。

第 2 组：全 CRLF 预检与默认槽摘要。
红提交 f961fd7e4，修复提交 2c217cb2d。
2c217cb2d 的直接父提交就是 f961fd7e4。
红提交：2 failed，27 passed，退出 1。
失败为 --path 与默认槽的全 CRLF 正例。
store 的原始字节摘要测试在红提交已通过，不冒充先红项。
修复提交运行相同集合：29 passed，退出 0。

当前受验提交的完整焦点集合为 158 passed。
提交作者与提交者均为派单指定的部门身份。
本次验收没有补写产品修复或倒置红绿提交。

4. 原通过项的回归复查

R1 的 9 个拒绝场景全部保持退出 1、ok:false：
- 缺当前原始字节摘要对应的采纳记录。
- version 不一致。
- publisher 不一致。
- documentSha256 不一致。
- adoptedAt 无效。
- 采纳记录不是合法 JSON。
- 手改正文后没有新摘要记录。
- publish-pending 指向其他摘要。
- publish-pending 不是合法 JSON。

匹配的正文和记录通过，匹配的 publish-pending 通过。
--path 仍只做格式预检，不要求采纳记录。
帮助仍区分默认槽状态校验和 --path 格式预检。

R3：PASS。
原脚本使用真实 OrgFsRuntime，复跑候选新建和覆盖两分支。
损坏正文均返回 False，并记录 warn。
新建分支不新增候选；覆盖分支保留原字节。
两分支均不新增 publish-pending。

R4：PASS。
issued_at: 2026-99-99 的 API 返回失败 issue，不抛出 ValueError。
定位仍为 line:8、column:14，原因含 month must be in 1..12。
CLI 退出 1，stdout 为单个 ok:false、valid:false 对象。
CLI 保留同一定位，stderr 为空。

旧格式拒绝行为保持正常。
escaped-brace 定位 20:33，multiline-flow 定位 19:69。
adopt/stage 仍在建立目录前拒绝损坏 source，保留 20:33 定位。

5. 门与焦点

以下命令均通过证据目录 env.sh 执行。

uv run --no-project --python 3.12 python scripts/ci/check_structural_limits.py
结果：FAIL，退出 1。仅 2 项既有违规，不计为本次拒绝原因。
registry_ops.py 为 1005 行；tests/daemon/outbox 为 8 个直接文件。
两个路径相对 8387fcfd6 无差异，本次无新增违规。
结构门没有记为 PASS。

uv run --no-project --python 3.12 python scripts/ci/check_module_boundaries.py
结果：PASS，退出 0，无 violations 或 unknownDynamicCalls。

uv run ruff check .
结果：PASS，退出 0。

uv run python scripts/verify/check_cli_help_inventory.py
结果：PASS，退出 0；62 commands，56 visible、6 hidden。

uv run --with pytest-timeout pytest tests/daemon/organizations tests/shell/cli/test_cli_org.py tests/shell/cli/test_cli_org_context_state.py tests/shell/cli/test_cli_org_context_validate.py tests/daemon/application/ports/test_daemon_org_context.py tests/shell/cli/output/test_output_discipline.py -q --timeout 600 --basetemp /private/tmp/oc3-review/pytest-focus
结果：PASS，退出 0；158 passed，2 warnings，13.63 秒。
警告均为 lark_oapi 依赖的弃用提示，无 skip。

未重跑全量 tests/。
相对二次判词，本轮产品与测试差异仅涉及 org 文件。
其余差异均为 org-context 证据文档，符合本次免跑全量条件。
二次验收的 5 项全量既有失败未在本轮重新裁定。
未把本轮焦点通过写成全量通过。

6. 证据与未测范围

读数目录：docs/notes/org-context-write-validation-acceptance-3-2026-10-16/。
命令、退出码、红绿读数与脚本说明见该目录 INDEX.md。
原二次验收复现目录仅有 probes.py，本次原字节未改动。
已在仓库根复跑全部 33 项，failures 为空。
补充脚本见 docs/notes/repro/org-context-write-validation-acceptance-3-2026-10-16/。
最终 supplemental-final.py 复跑 36 项，failures 为空。
早期补充脚本的夹具及断言错误保留在证据中，不计为产品通过或失败。

边界门原始输出为 6107580 字节，超过 1 MB，不入库。
gates-summary.json 记录状态、退出码、SHA-256、字节数和行数。
旧 notes/ 证据未改写或删除。

真实生产 org 槽、生产 daemon 和需专用材料的场景未测，均为 UNKNOWN。
GUI、desktop、真实网络和真实模型验收不在本轮范围。
本次未通过 skip 代替任何能力结论。

Contract-Impact: 本提交仅新增验收文档、脚本和证据，不改产品契约。
受验产品新增只读 meta 展示字段，不改变既有字段语义或错误码。
Skill-Impact: 无。本提交不改变 agent 操作方式。
