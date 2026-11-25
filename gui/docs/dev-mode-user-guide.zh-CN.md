# Hyprial GUI 开发模式操作手册

文档更新日期：2026-11-14。适用于在源码 checkout 内直接使用其自身 `gui/` 的 `hyprial gui dev`。具体参数和功能以所使用版本的命令帮助及发行说明为准。

> 2026-11-16：`hyprial gui dev` 由 Python 会话 host 在公开端口前承载（Vite dev server 在其后，
> 只经 loopback 随机端口出资源与 HMR，不持有 backend 变量、grant 或 bearer），一次性 launch grant、
> 可信 principal 与会话都由该 host 提供；已不再有 Node host，也没有 `npm start`。
> 操作与进程契约见 [`docs/guides/gui-dev-mode.md`](../../docs/guides/gui-dev-mode.md)。
> 仅需组件视觉预览时，可显式使用 `HYPRIAL_GUI_DEV_PREVIEW=1 npm --prefix gui run dev`。

面向产品设计人员、GUI 研发人员及协助修改界面的 AI agent。

## 1. 开发模式可以做什么

完整流程：**启动预览 → 交给 agent 修改 → 浏览器查看效果 → 验证 → 提交 PR → 合入后更新使用版本**。

在源码 checkout 内，`hyprial gui dev` 直接编辑并服务该 checkout 自身的 `gui/`，不创建自动工作树；它启动支持热更新的网页，并默认打开浏览器。修改源码并保存后，一般无需重启服务或重新打安装包。

显式 fixture 预览中的示例数据不代表真实业务状态；登录、模型配置、Squire 聊天等功能是否
可用，需以对应版本的后端接线和验收结果为准。安装包里的 `hyprial gui` 经 admission 与
`request_http` 接入真实 daemon（默认 driver 为 `daemon`），不再返回
`not implemented` 占位；daemon 未运行时会给出明确的启动错误。普通页面不会回退到占位后端，
也不会把 fixture 当作成功。

2026-11-09 起，开发预览与正式入口使用同一产品组合：预览只在组合根选择 mock/real
输入（数据源、能力适配器、控制器），页面、路由与交互一致。开发期附加 UI 只有右下角的
独立浮动开发工具：默认收起为浮动球，点击展开场景选择、数据来源说明、会话工具与模块索引，
Escape 或点击外部关闭。它位于产品 `#root` 之外，不改变产品布局，也不进入正式构建；
产品页面内不再保留常驻开发工具栏、底部模块索引或 `devLabels` 导航标记。该形态由
dev/product 等价改造确立（集成 PR #2612），落地状态以集成验收为准。

2026-11-14 起，浮动工具层（`src/dev/tools`）与 fixture 组合专用工具（`src/dev/fixture-tools`）在源码上分开，
为之后在真实后端组合上按运行时开关加载工具层做准备。本版行为不变：浮动工具仍只在
`HYPRIAL_GUI_DEV_PREVIEW=1` 的开发预览构建中挂载，并与 fixture 数据一起出现。

2026-11-15 起（PR 4b-G，GUI 部分）：同一份正式构建把浮动工具层作为按需加载的 chunk 放在 `dist/dev-tools/`，
只有宿主返回的会话响应带 `devTools` 能力时，入口才在连接成功后动态加载并挂载它；浏览器无法自行开启
（没有 URL 参数、存储键、全局变量或构建开关）。没有该能力时不请求、不渲染任何工具。真实后端下面板只含
模块索引；`devTools.apiDocs` 为真时多一个“API 文档”链接。仅作用于 fixture 的工具不在此渲染。
2026-11-17 起（PR 4b-H、4b-C）：`hyprial gui --with-dev-tools` 让会话 host 返回 `devTools`，工具层随之挂载；
`hyprial gui dev` 自带该开关。不带参数的 `hyprial gui` 不返回该能力，工具层保持未挂载，host 对
`/dev-tools/*` 回 404。“API 文档”链接只在 daemon 驱动下出现，打开 host 的 `/docs` 页面（沿用当前会话，
无需 bearer）。同日起（PR 5）`hyprial gui --live-source` 单独提供源码热更新而不带工具层；
`hyprial gui dev` 等价于 `--with-dev-tools --live-source`，API 文档文件取自所选 `gui/node_modules/swagger-ui-dist`。

开发模式不会自动启动 AI agent，不会自动提交、推送或合并代码，也不会自动更新其他客户端。

### 当前旅程与归档场景（2026-11-13）

纯前端预览中，点击右下角“开发”，默认在“当前旅程”选择未登录或已登录的初始化起点。
当前 Agent 开发使用 `?hyprialScenario=logged-in-init&hyprialFirstRun=false`：模拟账号已登录、设备就绪，Agent、凭据与平台接入为空。

需要参考旧数据时，切到“归档场景”，选择 J1/J2/J3/J5/J6/J7 或组织数据，再点击“打开归档场景”。
新标签页仍运行共享界面，原标签页的数据不变；归档选择编码在 URL 中，刷新恢复所选起点。
它保存的是模拟数据与能力输入，历史术语和状态仅供参考，不是后端验收结论，也不恢复旧版页面。
关闭归档标签页即可回到当前开发旅程；纯前端内存中的操作不跨刷新保存。

## 2. 使用前准备

需要研发环境已经具备：

- 支持上述 `gui dev` 功能的 Hyprial CLI，以及含 `gui/` 的 Git 源码工作树。
- Git、Node.js 24 或以上版本、npm。
- 首次安装依赖所需的网络或缓存。
- 能打开预览地址的浏览器。

这是源码开发入口，不保证任意旧安装包或普通 wheel 安装都具备开发源码。环境首次准备由研发人员完成。

可检查：

```bash
hyprial version
hyprial gui --help
git --version
node --version
npm --version
```

macOS、Linux 用户在相应终端执行命令；Windows + WSL 用户可在 WSL 执行命令，在 Windows 浏览器查看页面。浏览器自动打开依赖运行环境的默认浏览器打开程序；未弹出时可以直接访问启动输出的 URL。本文路径均为示例，执行前应替换为实际路径，包含空格的路径需用引号包围。

## 3. 启动开发预览

在已准备好的环境中执行：

```bash
hyprial gui dev
```

运行会启动网页与占位后端（不创建工作树、不自动安装依赖；`gui/node_modules` 缺失时先在该目录运行 `npm ci --ignore-scripts`），并调用浏览器打开预览。默认地址：

```text
http://localhost:4173/
```

以命令实际打印的地址为准。保留启动命令所在终端，服务在前台运行。若该端口已有你的预览，直接打开页面，不必重复启动。

常用启动方式：

```bash
# 使用另一个端口
hyprial gui dev --port 4174

# 不自动打开浏览器，只输出预览信息
hyprial gui dev --no-open

# 输出结构化启动信息，适合 agent 收集路径和状态
hyprial gui dev --json
```

`--json` 启动成功后仍保持前台运行，不是执行完立即退出的查询命令。

## 4. 找到实际编辑目录

使用启动输出的 `HYPRIAL_GUI_HOME`，或 JSON 的 `gui_home`。它指向含 `package.json` 的 GUI 项目目录。

源码 checkout 下的目录结构：

```text
<源码 checkout>/
└── gui/                    ← GUI_HOME，编辑这里
```

`hyprial gui dev` 直接使用该 checkout 自身的 `gui/`，包括其中未提交的修改。该路径没有工作树隔离：CLI 直接读取并运行该目录，未提交的修改立即生效，也没有文件系统沙箱。旧版本留下的 `.worktrees/gui-dev-auto` 与分支 `work/gui-dev-auto` 不再使用，CLI 也不会删除；确认其中没有未提交工作后，可用 `git worktree remove .worktrees/gui-dev-auto` 与 `git branch -D work/gui-dev-auto` 清理。

启动命令不会修改父终端的环境变量，所以另开终端后不能假设 `$HYPRIAL_GUI_HOME` 已存在。复制输出路径进入目录，例如：

```bash
cd /实际源码路径/gui
```

dev 使用发现到的源码 checkout，不会自动 fetch 或选择最新远程分支。如果从其他仓库目录启动，发现结果可能不同，应核对打印的源码目录和基线。

## 5. 让 agent 修改页面

可以使用 Codex、Claude Code，或具备目录访问权限的研发 agent。dev mode 本身不决定由哪个 agent 操作。

将以下模板中的目录和任务替换后交给 agent：

```text
请修改当前 Hyprial GUI 开发预览。

GUI_HOME：<启动输出的 gui_home 完整路径>
预览地址：<启动输出的 URL>
任务：<需要调整的页面、样式或交互，以及期望效果>

要求：
1. 先读取适用的 AGENTS.md 和 GUI 开发规范。
2. 修改指定 GUI_HOME 内的源码，复用现有组件和样式。
3. 修改前检查 Git 状态，保留其他已有修改。
4. 保存后检查当前预览效果。
5. 核对后端接线状态，不将示例数据或占位操作伪装成真实成功。
6. 报告修改文件、验证结果和未验证项。
7. 本次先不提交、不推送；待我确认效果后再进入交付流程。
```

例如：将“Hyprial OS”标题调大、调整导航间距、完善窄屏布局。多人或多个 agent 不要同时修改同一工作树中的相同文件；并行任务交由协调者安排独立工作树。

## 6. 查看效果和撤销

组件、样式保存后，浏览器通常会热更新。你可以继续提出调整意见，反复修改直至满意。

如果没有变化，先检查修改目录是否等于本次启动输出的 `gui_home`，以及浏览器是否打开了正确端口，再查看终端中的编译错误。

撤销某次调整时，可明确告诉 agent：

```text
撤销刚才的标题字号调整，保留其他改动。
```

agent 应只撤销指定差异，不使用整库 reset/clean 覆盖其他成果。依赖、启动脚本或服务配置的变更可能需要重装依赖或重启预览，不能一概视为热更新。

## 7. 验证并交付修改

先人工检查修改涉及的页面、交互、不同窗口宽度和错误状态。准备交付时，在 GUI_HOME 中执行适用验证：

```bash
git status --short
git diff
git diff --check
npm run verify
```

`npm run verify` 包含类型检查、GUI 测试和构建。单次小样式调整无需反复运行完整验证；正式提交前按变更范围记录命令、结果和精确提交。自动测试通过不能代替浏览器效果检查。

确认后可交给 agent：

```text
请整理本次 GUI 修改，完成适用验证，提交到独立远程功能分支并创建 PR。
目标使用当前确认的 GUI 集成分支，不沿用过期目标。
报告提交 SHA、PR 链接、验证结果和未验证项。
```

执行者在自己的分支上提交，并推送到独立命名的远程功能分支。PR 审核、合入权限及 CI 要求遵循当前工作流，不由 dev mode 自动处理。

## 8. “预览生效”和“正式应用”的区别

| 操作 | 生效范围 |
| --- | --- |
| 保存源码 | 当前 dev 预览热更新 |
| Git 提交 | 保存本次修改记录 |
| PR 合入 | 修改进入目标分支，可供团队同步 |
| 更新运行基线或发布新版本 | 对应实例或安装包获得修改 |

当前没有一键将 dev 修改应用到所有客户端的操作。

想检查同一份修改构建后的普通网页效果，先在 dev 终端按 Ctrl+C，然后进入该工作树运行普通入口：

```bash
cd <工作树> && hyprial gui
```

该命令先构建再启动普通网页，不提供开发热更新，也不生成桌面安装包。普通 `hyprial gui` 不接受 `--gui-home`（返回 `INVALID_ARGUMENT`），也不接受调用环境中设置的 `HYPRIAL_GUI_HOME`（返回 `GUI_CONFIG_CONFLICT`）。

## 9. 停止和下次继续

在运行预览的终端按 **Ctrl+C**，停止本次网页服务及命令自行创建的占位后端。它不会删除代码、未提交修改或停止 Hyprial daemon。

关闭浏览器标签页不会停止预览服务。若采用了外部指定的后端，本次命令也不会替你停止那个外部服务。

下次从同一源环境执行：

```bash
hyprial gui dev
```

继续使用同一 checkout 的 `gui/`，保留已修改文件和未跟踪文件。不会自动拉取更新、rebase、清理改动或安装、更新依赖。

需要跟随团队新基线时，让执行 agent 先检查并保存当前修改，按工作流同步，再重新验证。不要为了“更新”直接删除工作树或丢弃改动。

## 10. 常见问题

| 现象 | 处理方法 |
| --- | --- |
| 浏览器没自动打开 | 查看终端是否已就绪，手动打开实际 URL；检查默认浏览器打开程序 |
| `GUI_PORT_IN_USE` | 使用已有预览，或通过 `--port` 选择空闲端口；不要按进程名批量杀服务 |
| `GUI_NODE_MISSING` / `GUI_NPM_MISSING` | 检查启动终端 PATH 和 Node/npm 安装 |
| `GUI_HOME_INVALID`（提示缺少 `node_modules`） | 按提示运行 `npm --prefix <GUI 目录> ci --ignore-scripts` 后重试；CLI 不会自动安装 |
| `GUI_AUTO_WORKTREE_SOURCE` | 确认源 Git 工作树包含 `gui/package.json`；普通安装环境可能缺源码 |
| `GUI_AUTO_WORKTREE_INSTALL_FAILED` | 仅安装态受管工作树：检查 npm 错误、网络和缓存；若留下不完整 node_modules，由研发核查后处理并重装 |
| 保存后页面未变化 | 核对实际 GUI_HOME、预览端口、终端编译错误 |
| 找不到原来的开发工具栏或模块索引 | 开发工具已改为右下角默认收起的浮动球：点击展开场景选择、会话工具与模块索引，Escape 或点击外部关闭；正式构建不含任何开发工具 |
| 业务请求返回 501/not wired | 该请求使用了未接线的占位后端，不代表 GUI 开发服务器启动失败 |
| 重启后仍是旧基线 | CLI 不会 fetch/rebase，需按流程显式同步源码 |

## 11. 参数速查

| 参数 | 用途 |
| --- | --- |
| `--port 4174` | 设置网页端口；冲突时失败，不静默换端口 |
| `--ip 127.0.0.1` | 设置绑定地址，默认仅本机 |
| `--no-open` | 不自动打开浏览器 |
| `--json` | 输出结构化启动记录 |
| `--gui-home /路径/gui` | 仅 dev：使用指定 Git 工作树中的 GUI 项目；普通 `hyprial gui` 返回 `INVALID_ARGUMENT` |

`--gui-home` 优先于 `HYPRIAL_DEV_GUI_HOME`，两者都未指定时使用当前源码 checkout 的 `gui/`。源码 checkout 与显式指定的开发目录的依赖由使用者管理：首次使用前运行 `npm --prefix <GUI 目录> ci --ignore-scripts`；缺失时 CLI 在启动任何子进程前以 `GUI_HOME_INVALID` 报错并给出该命令，不会自动安装。仅安装态 wheel 的受管工作树会自行安装依赖。

本版只公开 `hyprial gui` 和 `hyprial gui dev`；不要使用旧的 `gui start/status/stop/upgrade` 命令。

## 12. 参考文档

- [GUI 设计与研发规范](ui-development-standard.zh-CN.md)：当前实现与目标标准、布局/状态/无障碍/凭据/dev 流程和验收边界。
- [组件目录](component-catalog.zh-CN.md)：真实 primitives、参数、页面覆盖与源码/测试链接。
- [UI 变更交接模板](ui-change-handoff-template.zh-CN.md)：产品、研发和 agent 共用的状态矩阵与交付记录。
- 技术说明位于完整源码仓库的 `docs/guides/gui-dev-mode.md`；仅 GUI 的稀疏工作树可能不物化该目录。
