# dev-03 Agent 聊天参考样例交接

| 字段 | 内容 |
| --- | --- |
| Change ID | GUI-REFERENCE-SLICE-DEV03-AGENT-CHAT |
| 状态 | 自测、独立 review、真实隔离验收完成；待 Allen 集成 |
| 产品 owner | Allen / 李震宇 |
| 研发 owner | agent:jo.carter@example.com:usrb-dev:developer |
| 平台 | shared GUI |
| 目标分支 | intg/gui-init |
| 基线 | PR #2175 中任务 A 完成后的 `5d95d48c8` |

## 目标与命令映射

本项记录 PR 中的补充 dev-03 Agent 聊天参考切片，不改变正式任务 B（AccountLoginSection）的归属。字段 `AgentTargetField` 投影宿主 `targets.list`（daemon `hosts` + `targets`）；字段 `AgentHistoryField` 投影宿主 `sessions.history`（daemon `message.history`）；组合区块 `AgentChatSection` 将目标选择、历史与 `ChatComposer` 组合，发送经宿主 `sessions.send` 到 daemon `message.send`，对应现有 CLI `hyprial message send`。没有新增 daemon 方法，也没有在浏览器传入 `from`、principal 或自造 conversation id。

正式页与 J3 预览共用该区块。已有 `ChatController` 移至 feature 的 `api/chat-controller.ts`，由它集中转换宿主返回，并继续持有选中对象、草稿、历史与写操作状态；旧导入路径仅重导出同一组件和控制器，没有第二套状态。历史读取成功可显示消息；发送受理只表示服务接收，Agent 回复以权威历史为准。未知结果保留原请求并走 lookup/confirm，不自动重发。

## 状态与缺口

字段覆盖加载、空、错误和禁用状态；J3 已有基线、需要连接、发送失败、无消息、对话完成场景。`message.history` 没有独立 CLI 子命令，GUI 通过已有宿主 `sessions.history` 消费同一 daemon 方法。真实 Agent 模型回复、真实飞书授权和 H-02 通用 HTTP client 均不由本样例新增。

## 验证与交接

### 自测

- `npm run typecheck`、`npm run build` 通过。
- `npm run test -- chat-controller.test.ts chat-workspace.test.tsx chat-composer.test.tsx agent-chat-reference.test.tsx live-pages.test.tsx upstream-integration.test.tsx`：86/86 通过。新字段测试覆盖目录加载、空、失败、受控选择、禁用，以及历史加载、失败、陈旧和空状态。
- `npm run verify:j3-browser`：开发/生产两种模式、4 种视口，8/8 通过。
- 用 `hyprial gui dev --gui-home /home/lenovo/worktrees/gui-examples-agent-provider/gui --no-open --port 4189 --json` 启动本分支预览，启动记录确认 `mode=dev`、`dataMode=fixture`。在 J1 选择“首次设置已完成”、J3 选择“M-23 对话完成”，再点击“聊天”，共享 `AgentChatSection` 显示 Squire、用户消息及回复；输入 `CLI_DEV_AGENT_CHAT_CHECK` 后，消息与模拟回复均出现，消息数从 2 增至 4，浏览器无 page error。默认 J1“成功流程”仍有首次设置门禁，直接点“聊天”会回到首次设置；改变 J3 场景会重建模拟会话并返回“开始”页，需再点“聊天”。
- 全量 `npm run verify` 在 `npm run test` 阶段停下：761/762 通过，唯一失败为 dev-01 首次设置浏览器用例 `gui/tests/dev01-onboarding-browser.mjs:461`（旧 `.message` 断言期望 1，实际 0）。该失败已在本任务基线 `4959eda19` 独立复现，与聊天改动无关；由于早停，后续浏览器门禁须单独执行。
- 独立运行旧 `gui/tests/live/browser.mjs` 时两种模式均在 Vite 启动阶段失败：它设置 `configFile: false`，未带入已有 `@` 路径别名，因此 `@/lib/utils` 无法解析、聊天按钮未渲染。此测试脚本及相关导入在任务 B 基线已有；它使用 HTTP stand-in，不作为真实 daemon 验收依据。真实隔离 daemon 验收见下文。

### 独立 review

独立代码 review 未发现 Critical 或 Important 问题。唯一 Minor 是共享入口文档仍指向旧聊天组件路径，已更新 `gui/docs/shared-ui-entry.zh-CN.md`，指向 feature 中的实际实现。

### 真实隔离验收

`contract/e2e/gui_live/run.py` 在隔离 network namespace 与临时 HOME 中启动真实 daemon、GUI 宿主和 Chromium，结果 `PASS`，耗时 20.3 秒。受控测试 Agent 不使用模型或外部服务。浏览器通过 `targets.list` 选中该 Agent，完成 `sessions.create`、`sessions.history`、`sessions.send` 和服务端确认，历史中出现用户消息和 Agent 回复；模拟成功回执丢失后，重启宿主并以同一幂等键执行 lookup/confirm，历史最终出现一次用户消息及其回复。归档的 [验收摘要](images/agent-chat/isolated-acceptance.json) 状态为 `PASS`、`pageErrors` 为空，[桌面截图](images/agent-chat/accepted-history-1440.png)与[窄屏截图](images/agent-chat/accepted-history-375.png)展示最终历史。完整本机临时证据位于 `/tmp/dev03-gui-live-_g7j9qqz/`。

隔离验收脚本现显式选择唯一受控测试 Agent，因为它不是用户的个人 Squire，`targets.list.preferredTargetUri` 正确返回 `null`。该测试脚本调整不改变产品默认选择逻辑。验收证明真实本地 daemon 路径，不代表已验证真实模型回复或飞书消息往返。

## 2026-10-31 契约场景补齐（PR #2175 补充切片）

开发工具栏既有 `J3 场景` 新增 `contract-success / contract-empty / contract-error / contract-unconfigured / contract-unknown / contract-failure`，保留默认 `baseline`、连续会话及原 J3 回归样例。显式选择契约选项后，单独注入 chat runtime，仍使用 `LiveChatPage` → `AgentChatSection` → 三个共享字段；页面固定标记 `FIXTURE · Agent chat host contract · 不连接真实服务`。场景切换销毁旧 controller/mock，J3 恢复 baseline 且其他 J 场景均为默认时返回原连续会话；契约场景不启动 Lark、不自动产生 Agent 回复、不启用旧聊天 simulation。

`features/agent-chat/api/mock.ts` 只实现已有 host LivePort 形状；`ChatController` 继续是唯一 host→view model 映射位置。`targets.list` 对应 daemon hosts + targets(kind=agent)，`sessions.create` 是 host 派生的 session binding，history/send/lookup 分别映射 message.history / message.send / message.send.lookup，pending/confirm 归 host journal。mock 的临时 Map 仅模拟这份契约，不构成 durable authority，不访问网络、凭据或浏览器持久存储。H-02 通用/生成 client 仍是缺口，本次不新增 raw daemon API。

unknown 首次发送丢失响应，保留原 idempotency key 与草稿，点击“查询原消息结果”后 lookup 返回 accepted，再 confirm 清理模拟 pending，历史只出现一次用户消息。failure 是明确拒绝，与目录读取错误分开，保留草稿且不显示虚构回复。当前新增自动化覆盖 mock 经真实 ChatController、并发 reconcile 去重、无自动重发、明确失败、空/错误/未配置无回退、显式 fixture 注入及场景隔离。CLI 浏览器验收由集成人员补充，本节不把 mock 结论当成真实 Agent 或 daemon 验收。

补齐自测：`npm --prefix gui run typecheck` PASS；`npm --prefix gui test -- tests/live/chat-contract-injection.test.tsx tests/live/chat-contract-mock.test.ts tests/live/chat-controller.test.ts tests/live/live-pages.test.tsx` 28/28 PASS；结构/模块边界检查 PASS；`git diff --check` PASS。新增原请求校验：缺少 key/text、复用 key 却更改 text 的 lookup/confirm 均拒绝。全量 GUI verify、生产构建以及本次 CLI 浏览器检查由集成方继续执行，当前 UNKNOWN。

生产边界补验：首轮生产 copy gate 捕获契约 hook 和共享页面残留的 fixture 文案，已在 hook 的 memo factory 与共享页面的契约文案分支加 `import.meta.env.DEV` 编译期门；保持 hooks 调用顺序，生产 bundler 可删除 mock 及契约文案。修复后 `npm --prefix gui run build` PASS、`node gui/scripts/verify-production-copy.mjs gui/dist` PASS（0 forbidden markers）、上述 28 个焦点测试再次 PASS。构建日志 `/tmp/pr2175-taskb-prod-fix.log`。

### 最终补齐验收（代码 `bfe127b6d`）

- B 焦点测试 28/28、typecheck/build、生产文案门禁 PASS（零禁用标记）；任务 A/B、完整 PR 和生产隔离补丁独立 review 均 PASS。
- 实际 `hyprial gui dev --gui-home <本工作树>/gui --ip 127.0.0.1 --port 4189 --no-open --json`，J1 选“首次设置已完成”，再选“Agent chat 契约场景”并点击聊天。success/empty/error/unconfigured/unknown/failure 六场景 PASS；unknown 阻止重发，查询确认后用户消息恰好一条，pageErrors 为空。
- `npm run verify:j3-browser`：开发/生产 × 四视口，8/8 PASS，原 J3 入口保留。
- 完整 `npm run verify` 在 `ce252f41a` 为 786/787 PASS；唯一失败仍为基线已有的 `dev01-onboarding-browser.mjs:461` 旧 `.message` 断言。最后生产隔离补丁另跑上述焦点测试与构建门禁。全量链后续其他浏览器门禁未整体运行。
- 最终代码重新运行真实隔离 daemon + 官方 GUI 宿主 + Chromium 验收 PASS，19.1 秒，证据 `/tmp/dev03-gui-live-vt4hkh6e/`；[摘要](images/agent-chat/isolated-acceptance.json)及桌面/窄屏截图已更新。回执丢失与重启恢复后，原浏览器 send 次数和 daemon 消息数均为 1。仍使用无模型受控 Agent，不证明真实模型或飞书往返。

复现真实验收：在仓库根执行 `unshare --user --map-root-user --net sh -c 'ip link set lo up && PATH=/home/lenovo/.nvm/versions/node/v24.20.0/bin:$PATH uv run --offline --no-sync --python 3.12 python contract/e2e/gui_live/run.py --node /home/lenovo/.nvm/versions/node/v24.20.0/bin/node --browser /home/lenovo/.cache/ms-playwright/chromium-1243/chrome-linux64/chrome'`。网络 namespace 仅 loopback，临时 HOME，与日常 daemon/凭据隔离。

### 场景入口纠正（当前操作）

契约场景使用上文 J3 选项，复用 J1–J7 的相同工具栏样式、hash 初始化与 keyed reset。组织数据开启时仍传入相同契约 controller；J3 契约选项优先于 seed，直接完成首次设置以便进入聊天。上方 CLI 实测记录保留当时入口，不能作为此轮新选择器已实测的证据。此轮 CLI 浏览器复验由集成人员单独记录。

实际 CLI 新入口验收（代码 `8bc73f970`）：在 `http://127.0.0.1:4189/` 的 J3 逐个选择六种 `contract-*` 场景并进入聊天，均 PASS；unknown 时发送按钮禁用，查询原结果后用户消息恰好一条。切回原 M-23 后契约控制器退场、原对话显示正常，pageErrors 为空。1440px/375px 下所有 J 选择器计算样式一致；独立 delta review PASS，生产文案门禁再次 PASS。此轮改动仅开发场景组合，未重跑无变更的真实 daemon 链路。
