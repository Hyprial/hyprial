# WP5-B 第三次验收（窄验收）

结论：ACCEPTED

- 分支 `wp5b/send-links`，worktree `/private/tmp/hf-wp5b-send-links`，HEAD `08018c1d3`，工作树干净。
- 范围：只验第二次验收（`wp5b-acceptance-2.md`，`d93cc4677`）退回的两项，加回归门。其余项沿用第二次验收结论。
- `git log --oneline d93cc4677..HEAD`：`ce8fb3f1e` 红测、`4ff6078f2` 修复、`08018c1d3` 文档。
  改动 5 个文件，+12/-5：`sessions.py`、`meta/mcp.py`、`test_codex_interactive_mcp.py`、`wp5b.md`、`wp5b-acceptance-1.md`。

## 逐项

1. `--session-ref-file` 补 help —— PASS。
   `meta/mcp.py:121-123` 加了 `help=`，仍是 `hidden=True`。
   `test_cli_help_coverage.py::test_undocumented_parameters_do_not_grow` 通过。
   `_UNDOCUMENTED_BUDGET = 54` 没有上调：`git diff d93cc4677 HEAD -- tests/shell/cli/presentation` 为空，与 merge-base `93a13b4ba` 相比也没有改 BUDGET。

2. `--yolo`、`-anever` 拒绝 —— PASS。
   - 修复：`_CODEX_BYPASS_FLAGS = {--dangerously-bypass-approvals-and-sandbox, --yolo}`；
     单横线 `-a<值>` 粘连形式按 `arg[2:]` 收进 approvals。
   - 先红后绿：在 scratchpad 下建临时 worktree，检出 `ce8fb3f1e`（只改测试，src 等于 `d93cc4677`）。
     `test_codex_interactive_mcp.py` 得到 4 failed / 21 passed，
     失败项是 `test_interactive_rejects_policies_that_disable_harness_tools[None|thread × args5(--yolo)|args6(-anever)]`。
     该 worktree 已移除。HEAD 上 25 个全绿。
   - 探针（临时测试文件，经真实 `_launch`，跑完已删除，工作树干净），start 与 `--resume` 两种情形都测：
     - `-a=never`、`--ask-for-approval=never`、`--dangerously-bypass-approvals-and-sandbox`、`--yolo`、`-anever`
       都被拒绝，错误码 `INVALID_ARGUMENT`。
     - `-a untrusted`、`--ask-for-approval=untrusted`、`--approve-for-me` 原样透传，不前置 on-request。
       argv 示例：`codex [resume <thread>] --no-alt-screen -a untrusted --remote unix://…`。
     - 共 16 个，全部通过。

3. 行尾空格与预算记录 —— PASS。
   - `git diff --check d93cc4677 HEAD` 无输出：`wp5b-acceptance-1.md:34` 的行尾空格已去掉。
   - `wp5b.md` §2 预算表的 `sessions.py` 行记为「实测 756，超 26，裁定 75 记录不返工」。
     `wc -l` 实测 756，与记录一致。

4. 回归门 —— PASS。
   - 焦点集（`tests/daemon/harnesses/codex`、`tests/daemon/channels/mcp`、`tests/shell/cli/agents/codex`、
     `tests/shell/cli/presentation`、`tests/tooling/contracts`，`-n 4`）：476 passed。
   - `tests/shell -n 4`：1600 passed，6 failed，1 skipped。6 个失败都在已知红名单内：
     - GUI 生命周期 5 个：`test_gui_profile_recovery` ×2、`test_gui_runtime` ×2、`test_gui_apps` ×1，原因是 GUI 30s 内未就绪。
     - `login/device/test_boundaries.py::test_t11_…`。
     已知红名单里的 shell CLI command inventory 本次没有失败。
   - `check_structural_limits.py`：退出 0，`violations: []`。
   - `check_module_boundaries.py`：退出 0，`violations` 为空列表。上一轮 worker 跳过了这一项，本轮已补跑。
   - `uv run ruff check .`：All checks passed。

5. 合并就绪 —— PASS。见下节。

## merge-tree 结果

- 集成分支 `refactor/org-structure-constrain` 当前在 `39d240d7c`，已包含 `cf52d2ee8`。
- `git merge-tree --write-tree refactor/org-structure-constrain HEAD` 退出 0，tree 为 `711bf3b4617958e4dd5fca094ed8bd1e9f1d3fc3`，无冲突文件。
- 合并树中 `sessions.py` 为 756 行，低于 1000 行硬门。

## 集成者合入时要做

- PR 正文写 Skill-Impact：hyprial-ops SKILL 新增 interactive codex 审批条。
  附注：`src/hyprial/skills/hyprial-ops/SKILL.md:256` 列了 `-a never` 与 `--dangerously-bypass-approvals-and-sandbox`，
  没有提 `--yolo`（它是后者的别名）。建议合入时顺手补上，不返工。
- PR 正文写 Contract-Impact，沿用 `wp5b-acceptance-2.md` 第 8 项，并在拒绝列表中加 `--yolo` 与 `-a<值>` 粘连形式（如 `-anever`）。
- `docs/evidence` 下的 WORM 原字节（`probe.py.txt:27` 行尾空格）在 `--check` 时排除。
- 合入后在合并树上复跑：本包焦点集、`tests/daemon/harnesses/contracts`、`tests/shell/cli/presentation`。
