# GUI 深模块结构迁移计划

状态：WIP。目标分支为 `dev`，基线为 `e8d38384372e06a14601090fece5833beeb92b58`。

## 背景与目标

当前根 `gui/` 按 pages、features、state、adapters 等技术类型横向拆分，聊天等能力分散在多个目录；同时存在 legacy re-export、重复 controller、正式源码依赖 `dev-mode` 或 `tests/fixtures` 的情况。目录改名不能单独解决这些边界问题。

本批目标是：

1. 将 `gui/src/dev-mode/` 收敛为 `gui/src/dev/`，消除重复命名和一行旧路径转发。
2. 所有目标业务模块从结构建立之初就采用 `index.ts + impl/`，包括当前较简单的 `org`、`settings` 等模块，不等复杂后再补结构。
3. 跨模块只消费公共入口，正式产品不得依赖开发预览或测试 fixtures。
4. 先以聊天域验证深模块边界，再完成其他业务域的同类收拢；保持预览与正式共用页面。

## 结构决定

每个业务模块统一采用：

```text
features/<module>/
  index.ts             # 显式、受控的公共 API
  impl/                # 本模块组件、控制器、模型和内部适配实现
```

- `index.ts` 是有意保留的公共门面，不属于应删除的 legacy alias。
- Git 不跟踪空目录，因此不提交 `.gitkeep` 形式的空 `impl/`。每个模块第一批就迁入实际实现；简单模块同样执行。
- 公共入口使用显式导出，不通过 `export *` 自动扩大 API，不向正式消费者导出 mock 或 fixture。
- 不机械复制 Python 的五域，也不把公共门面膨胀为新的巨型聚合文件。借鉴的是公共面、隐藏实现和依赖门。

目标顶层职责：

```text
gui/src/
  main.tsx             # 正式/开发组合的显式入口
  app/                 # 应用组合根、路由、provider 和应用壳
  features/            # 按业务能力组织的深模块
  components/          # 无业务语义的共享组件和 primitives
  client/              # 类型化 client 与生成代码
  platform/            # 宿主能力与传输端口
  dev/                 # 预览、场景、mock、fixture 和开发 session
  styles/
```

`pages/` 逐步缩减为路由组合或由 feature 公共入口替代；`state/` 只保留跨页面 UI 状态，业务状态归入所属 feature。`server/` 继续保持 Node 宿主边界，不套 React feature 结构。

## 依赖规则

1. 跨 feature 只能引用对方 `index.ts`，不得穿透 `impl/`。
2. `dev/` 和测试可以消费正式模块公共 API；正式模块不得依赖 `dev/` 或 `tests/`。
3. `components/primitives` 不依赖 feature、page、dev 或业务状态。
4. `client/`、`platform/` 保持底层，不反向引用 feature。
5. `main.tsx` 可以显式动态加载开发组合；正式 `App` 不静态导入 mock/fixture。
6. 新增 GUI import-boundary 检查，历史例外使用只减不增清单，不用新豁免扩大债务。

## 分批实施与子智能体边界

### A. 可并行的小步清理

- A1 `dev` 目录收敛：更新代码、测试和现役文档中的路径；保留 `dev-01`～`dev-06` ID；不改写历史 `docs/notes/`。
- A2 legacy/duplicate 清理：两个 `pages/chat` alias、无消费者的 `AccountStep` 重导出、重复的 legacy `ChatController` 及其重复公共出口。

A1 与 A2 使用独立 worktree 和本地分支，不直接 push；集成人审查后合入 WIP 分支。

### B. 全模块结构落地

- B1 聊天域：收拢 `agent-chat`、`chat`、`chat-channels`、`quick-chat` 和相关页面/controller，建立 `chat/index.ts + impl/`。
- B2 组织与网络域：`org`、`network` 立即建立 `index.ts + impl/`，开发组合迁入根 `dev/`。
- B3 设置、账号、连接与 onboarding：`user-settings/settings`、`model`、`account`、`connections`、`onboarding` 立即建立相同结构。
- B4 Agent 域：`agents`、`agent-provider` 建立相同结构，并区分正式 API 与开发 mock。

各任务对根 `App.tsx`、`AppShell.tsx` 或全局 `state/` 只做本域搬迁必需的 import/路径更新，不改变组合逻辑；这些共享文件可能产生文本冲突，由集成人统一复核和解决。跨域功能调整仍由集成人完成。

### C. 边界与组合根收尾

- mock、fixture、开发 session 归入 `dev/`，消除 `src -> tests` 和正式公共入口导出 mock。
- 拆出 `createFixtureState`，业务状态归 feature；正式 `App` 不再依赖开发实现。
- 根 `App.tsx`、`AppShell.tsx` 只负责组合与路由，公共页面实现继续保持 preview/live 共用。
- 增加 GUI import-boundary 检查并更新 `AGENTS.md`、GUI 文档和相关 skill，使其描述最终结构。

## 验证与退出条件

每批先跑直接覆盖改动的焦点测试，再按改动范围执行：

```sh
npm --prefix gui run typecheck
npm --prefix gui test
npm --prefix gui run build
node gui/tests/linked-preview-browser.mjs
uv run --no-project --python 3.12 python scripts/ci/check_structural_limits.py
uv run --no-project --python 3.12 python scripts/ci/check_module_boundaries.py
git diff --check
```

浏览器命令所需的依赖与隔离环境必须先满足；未实际运行写 `UNKNOWN` 或 `BLOCKED`，不把 unit/build 当成真实 backend、浏览器或完整功能等价证据。

完成条件：所有目标业务模块均有显式公共入口和实际 `impl/`；无 legacy alias 与重复 controller；正式源码不引用 dev/test fixture；共享页面行为与生产隔离检查不回归；文档、skill 和边界门与代码一致。

## 非目标与约束

- 不改变已发布 JSON 字段、诊断字段、事件名或错误码语义。
- 不恢复旧 `src/gui/` 或 `app/`，不增加第二套 preview/live 页面。
- 不为追求目录统一引入 `.gitkeep`、无消费者接口或新的框架依赖。
- 不批量改写历史证据，不处理无关 GUI 功能缺陷或已知问题。
