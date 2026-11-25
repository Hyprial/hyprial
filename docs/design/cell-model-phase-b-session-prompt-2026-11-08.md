# Session prompt：Cell model phase B（PR #2551）

> 将本文件交给负责 #2551 的独立 session。先读当前仓库与规格，再按小批次交付；不要把整个 phase B 塞进一个实施 PR。

你负责 `intg/cell-model-phase-b` → `dev` 的剩余施工。WIP PR 是 #2551。第一个子 PR 只做 **`conversations.list/history` HTTP 暴露**；GUI、links、route、S1 和真实环境验收按依赖分别交付。

## 一、开工前必读

1. `AGENTS.md`、`GLOSSARY.md`、`ROADMAP.md`；
2. `docs/reference/known-problems.md`，特别是 KP-028、KP-029、KP-036；
3. `docs/design/cell-model-phase-b-handoff-2026-11-07.md`；
4. `docs/design/cell-thread-conversation-model-2026-11-03.md` §9、§10；
5. `docs/design/cell-model-phase-b-brief-2026-11-03.md` 的统一契约链约束；
6. `src/hyprial/daemon/impl/request_catalog/{threads,conversations}/`；
7. `tests/integration/request_http/test_http_entry_live.py` 中 `test_thread_reads_are_served_with_the_owner_user_selector`；
8. 修改 GUI 前另读 `gui/docs/ui-development-standard.zh-CN.md`、`gui/docs/shared-ui-entry.zh-CN.md` 和 UI 交接模板。

旧 brief 的 release 目标、分支名和“GUI 不在本阶段”是历史分工。当前 WIP 面向 `dev`，GUI 属于后续独立批次，但仍须先取得 GUI owner 窗口。

## 二、当前已完成，不要重做

- 集成分支已完成一次性 rebase 到 `dev@284dcf72942b1b1b2495c9bbddc1e646cfc386a3`；**你不要再 rebase 或 force-push**。
- cell-model 最终提交已在当前历史中；`cells/`、`threads/`、`delivery/`、`conversations/` 都存在。
- `squire/` 不存在，squire 已是普通 agent。不得恢复该目录或增加 squire 专用运行分支。
- S8 attach 已在树中；`threads.get/list` 已有 HTTP 身份绑定、examples 和真实 HTTP 测试。
- `conversations.list/history` 已有 IPC spec/handler，但仍是 `http_exposed=False`。
- 当前 conversations + HTTP entry 基线为 **98 passed**；这是交接基线，不代替你子 PR 的复跑。

## 三、第一个子 PR 的精确范围

从集成分支最新 head 建 `feat/conversations-http`（若已存在，先核实 owner），完成：

1. **HTTP 身份绑定**
   - 复用 `threads.*` 的 `local-owner-user-selector` 模式，由宿主注入 `from=user:<owner>`。
   - 按 `x-hyprial-http-forbidden` 规则省略宿主专用字段；浏览器提交的 `onBehalfOf`、`sessionRef` 必须在 dispatch 前被拒绝为 400 `INVALID_ARGUMENT`。
   - 不得接受浏览器自报的 session/operator/agent 身份，也不得把本地 owner 错映射成 synthetic agent。
2. **保持 IPC 与读语义不变**
   - 不改现有 IPC params/result/error 名称和含义，不改 conversation cell id、thread id、legacy id 的映射。
   - 保持 cursor/limit/mode、正文恢复和 OrgFS 不可用时可读的行为。
   - `agent.attach/detach` 仍不暴露 HTTP；本 PR 不扩大范围。
3. **examples 与 registry**
   - 为两个方法新增 success、empty、invalid-params examples，并按实际可达性说明 domain errors。
   - 在 `request_catalog/registry.py` 注册 example sets；contract mock 不得新增 skip。
4. **测试**
   - 先让新增 HTTP 正反向测试在旧代码上 RED，再实现到 GREEN。
   - 真实调用两个 HTTP 方法，覆盖成功/空结果、错误参数、host-only 字段省略、伪造 `from`、`onBehalfOf`、`sessionRef` 的 400 + `not-dispatched`。
   - 有意更新 `gui/tests/features/user-settings/contract-gap.test.ts` 等手写期望；不得通过重新生成独立期望集掩盖变化。
5. **生成物**
   - 按规定顺序完整重建，不手工编辑 openapi、TS client 或 surface 快照。

不要在第一个子 PR 中修改 GUI 业务组件、退役 links/route、删除别名或修改 actor 引擎。

## 四、生成与验证命令

按仓库当前脚本参数执行；若脚本接口已变化，先读脚本帮助并报告，不猜测。

```bash
uv sync --python 3.12 --extra test
uv run --python 3.12 python scripts/verify/dump_daemon_openapi.py --write
npm --prefix gui ci
npm --prefix gui run client:reference
uv run --python 3.12 python scripts/verify/check_cli_help_inventory.py --write
uv run --python 3.12 python scripts/verify/update_cli_surface_snapshot.py
uv run --python 3.12 python scripts/verify/regenerate_post_install_surface.py
```

至少运行并报告：

```bash
uv run --python 3.12 --extra test pytest tests/daemon/conversations tests/integration/request_http/test_http_entry_live.py -q
uv run --python 3.12 python scripts/verify/dump_daemon_openapi.py --check
npm --prefix gui run client:reference:check
npm --prefix gui run typecheck
npm --prefix gui test -- tests/features/user-settings/contract-gap.test.ts
uv run --no-project --python 3.12 python scripts/ci/check_structural_limits.py
uv run --no-project --python 3.12 python scripts/ci/check_module_boundaries.py
uv run ruff check .
./contract/run-all.sh --isolated-only
uv run --with libcst==1.9.0 pytest tests/
```

- 检查生成物幂等和 diff 范围；两个目标方法之外的生成漂移必须归因，不能直接提交。
- KP-028/KP-029/KP-036 是已登记基线问题。2026-11-08 本地 merge-tree 另有 unbounded-waits（3 项）与 provider 术语 lint（1 failed / 13 passed）为红，纯 `dev@284dcf729` 结果相同，Node CI-policy 为绿色。保留真实 FAIL 并与同基线对照，不重生成 ratchet/router baseline、扩大豁免或在本子 PR 修无关门禁。
- 浏览器、真实 backend 或真实环境未执行时写 BLOCKED/UNKNOWN；unit/mock 不能代替。

## 五、分支与 PR 操作

1. 使用独立、干净的 worktree；先 `git fetch origin`。
2. 不再 rebase 集成分支。若需同步 `dev`，用 merge commit并说明原因。
3. 子 PR base 必须是 `intg/cell-model-phase-b`，不是 `dev`。
4. PR 写清动机、HTTP 身份规则、IPC 兼容性、examples、生成物、RED→GREEN、验证命令与结果、UNKNOWN/BLOCKED、Skill-Impact、Contract-Impact；按当前模板处理 Submitted-by，不伪造 agent URI。
5. 未获明确授权不要自行合并。前序子 PR 未合入时，不堆叠依赖它的 GUI/links/route 批次。
6. 每个后续批次仍独立：GUI 改读 → links 退役 → route 退役 → 别名计划 → S1 → 真实环境验收。可根据依赖调整，但不得扩大当前 PR。

## 六、并行与停止条件

你可以与 #2541 的 P2c 并行。不要合并两个 WIP。修改 dispatch、PAC、shutdown、`adapters/user_delivery/{actor,ledger_actor}.py`、`identity/profiles.py` 或对方正在修改的文件前，先通过 Allen 约定唯一 owner/冻结窗口；修改 `gui/` 前先取得 GUI 线窗口。

出现以下情况停止相关施工并报告：

- 必须改变现有 IPC 契约或冻结的 cell/thread 语义才能完成 HTTP 暴露；
- HTTP 身份规则无法与 `threads.*` 对齐，或需要接受浏览器自报可信身份；
- 需要恢复 `squire/`、增加无必要安全限制、手工拼接生成物或改写独立期望集求绿；
- 出现无法归因的新失败，或需要修改未协调的共享文件；
- 真实环境需要未批准的凭据、生产 daemon 或 Lark App。

每段结束报告：子 PR URL、base/head、身份与契约差异、生成物清单、RED→GREEN、全部验证结果、基线失败归因、未测项和下一批次解锁条件。
