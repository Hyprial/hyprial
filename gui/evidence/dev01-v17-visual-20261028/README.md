# dev01 首次设置 v17 视觉对齐证据

日期：2026-10-28
PAC：`wf-f46d28966d1f4848b4e7c232a1cf9602` / node `visual`
请求：`workflow-request:6ee22d1e5e1946c08b23a2bde40a4666`

## 范围与基线

- worktree：`/home/huangjiajia/HyprialOS/.worktrees/gui-dev01-v17-visual-20261028`
- branch：`feat/gui-dev01-v17-visual-20261028`
- base：`c0cae2b71159f8369d40c250b5346959c812537a`
- 未修改正在运行的预览 worktree `gui-dev01-onboarding-20261028`；未 push、未合 dev/intg、未发布或重启服务。
- 本次只改 dev01 onboarding、其专属测试/证据与局部作用域样式；未改共享 App/AppShell/全局 CSS/state/adapters/runtime/共享测试/package、`src/hyprial`、CI、native/packaging。

## 原型与资产来源

- 原型：`/mnt/d/Users/nity/Downloads/Hyprial-mvp-desktop-v17.html`，只读视觉参考，**不声明为团队全站最新设计**。
- 经批准的 vendor 副本：`/home/huangjiajia/HyprialOS/gui-fix-records/2026-10-25-prototype-v17/vendor/Hyprial-mvp-desktop-v17.html`。
- 原型 SHA-256：`c7e5ae80067c29dfe773f394bb95fa3fb9f2883c11504c012c28201013017348`。
  - 源文件直接 `sha256sum` 被工具策略阻止；已用 `cmp` 验证 vendor 副本与源文件逐字节一致，SHA 取自该批准副本，不声称直接对源文件取哈希。
- 品牌资产：`../../src/features/dev01/onboarding/assets/hyprial-lock-full.png`
  - 760×146 px，74,947 bytes，SHA-256 `c6b81b1cbf72f32a385eecaa9b7dcd612d33b0b12026d204c2e1d9d3d3f23089`。
  - 从原型 `V5_LOCK_FULL` 提取；来源与提取说明见同目录 `assets/README.md`。
- 未从远程加载资产，未整体导入原型脚本，未直接打包 HTML。

## 实现摘要

- 团队四步：账号、模型服务、创建 Squire、首次对话；设备是账号步骤的子状态。
- 单活动步骤卡片；顶部四段进度条（当前/已完成蓝色 6px，失败/阻塞使用 danger/warning 语义色）。
- 品牌 Logo 居中；标题在卡片外，正文与操作在白色圆角卡片内；v17 的 36px/900 标题、28px 圆角、32px 内边距、10px 步骤间距、44px 胶囊按钮。
- 隔离 demo 带明确标识；正式未接线时显示“真实服务尚未接入”，不伪造登录/模型/Squire/聊天成功。
- 诊断、场景说明与“返回设置”退出放在卡片后的专属调试区域；不堆叠工程说明到产品卡片内。
- 未新增 localStorage/sessionStorage 写入、网络请求、聊天输入或发送按钮。

## 375 / 760 / 1440 对照

截图（组件范围）：

- 实现：`visual/375-implementation.png`、`visual/760-implementation.png`、`visual/1440-implementation.png`
- 原型：`visual/375-prototype.png`、`visual/760-prototype.png`、`visual/1440-prototype.png`
- 机器指标：`visual/report.json`

| 指标 | 375 实现 | 375 原型 | 760 实现 | 760 原型 | 1440 实现 | 1440 原型 |
| --- | --- | --- | --- | --- | --- | --- |
| 根/内容宽度 | 345 | 248 | 720 | 633 | 720 | 720 |
| Logo 宽度 | 260 | 248 | 360 | 360 | 360 | 360 |
| 步骤间距 | 10px | 10px | 10px | 10px | 10px | 10px |
| 当前进度条 | 6px `rgb(0,72,168)` | 6px `rgb(0,72,168)` | 同左 | 同左 | 同左 | 同左 |
| 步骤字号 | 13px | 13px | 13px | 13px | 13px | 13px |
| 卡片宽度 | 345 | 248 | 720 | 633 | 720 | 720 |
| 卡片圆角/内边距 | 28px / 32px | 28px / 32px | 同左 | 同左 | 同左 | 同左 |
| 标题字号/字重 | 36px / 900 | 36px / 900 | 同左 | 同左 | 同左 | 同左 |
| 主按钮背景 | `rgb(0,72,168)` | `rgb(0,72,168)` | 同左 | 同左 | 同左 | 同左 |
| 次按钮背景 | `rgb(236,236,239)` | `rgb(236,236,239)` | 同左 | 同左 | 同左 | 同左 |
| 按钮圆角/内边距/最小高 | 999px / 0 20px / 44px | 同左 | 同左 | 同左 | 同左 | 同左 |

差异表（有意保留）：

| 差异 | 实现 | 原型 | 原因 |
| --- | --- | --- | --- |
| 步骤数量与命名 | 4 步：账号、模型服务、创建 Squire、首次对话 | 5 步：登录、设备、模型、Squire、聊天 | 团队四步账号/模型/Squire/首次对话；设备是账号子状态 |
| 375 内容宽度 | 345（视口减 30px） | 248 | 原型为桌面参考，375 下标题/文案明显挤压截断；实现保留可读移动布局，不使用 980px 桌面最小宽度 |
| 760 内容宽度 | 720（20px 边距） | 633（约 56px 边距） | 实现沿用 720px 内容列；1440 下两者均为 720 |
| 卡片内结构 | 标题在卡片外，说明/表单在卡片内 | 标题与说明在卡片外，卡片内仅操作 | 保持团队步骤说明与状态可读，同时复刻标题/卡片分离关系 |
| 额外状态区域 | 隔离 demo 提示、真实服务未接线提示、底部诊断区域 | 无 | 团队隔离与真实能力边界要求；原型无后端状态语义 |
| 设备状态 | 账号步骤内子状态 | 独立步骤 | 团队四步语义；不新增跳过或伪造设备成功 |

## 命令与结果

在 `gui/` 目录执行：

| 命令 | 结果 |
| --- | --- |
| `npm run typecheck` | PASS |
| `npx vitest run tests/dev01-onboarding.test.tsx --maxWorkers=1` | 10/10 PASS |
| `npx vitest run tests/dev01-onboarding-browser.test.mjs --maxWorkers=1` | 1/1 PASS（375/760/1440；无横向溢出；零 fetch/XHR/storage 写入；键盘顺序） |
| `node tests/dev01-v17-visual-compare.mjs` | PASS；Chromium 1243，browser `153.0.8010.12`；生成 6 张对照图与 `report.json` |
| `npm run build` | PASS；`dist/assets/main-BoTfjRem.js` 288.88 kB，CSS 58.87 kB，logo 74.94 kB |
| `npx vitest run --maxWorkers=2` | 28 files / 170 tests：163 pass，7 fail（见下） |
| `npm run dev -- --port 0` + `/src/features/dev01/onboarding/preview.html` | HTTP 200，开发服务器正常返回私有预览入口；浏览器验收已真实渲染 |
| `npm run dev`（默认 4173） | 4173 被现有预览服务占用，未停止该服务；改用临时端口验证 |

### 全量失败分类（7 个已知共享断言）

`full-vitest.json`（本目录机器报告）显示失败集合与基线已知项一致，均为共享 settings/app-shell 在进入 onboarding **之前**的断言，本次未修改这些共享测试或页面：

| 文件 | 数量 | 失败位置 | 性质 |
| --- | --- | --- | --- |
| `tests/settings-first-run.test.tsx` | 5 | 36、64、27+94、27+119、27+144 | 设置页 label/“打开首次设置”入口缺失的已知共享断言 |
| `tests/app-shell.test.tsx` | 1 | 139 | 设置页 `账号与设备` 标题的已知共享断言 |
| `tests/safe-r1-ui.acceptance.test.tsx` | 1 | 127 | 设置页“打开首次设置”按钮的已知共享断言 |

未发现由本次 dev01 视觉层引入的新失败；没有把失败隐藏为通过。

## 已测 / 未测

已测：

- dev01 四步状态机、单活动卡片、失败/未知/重试、不可跳过 Squire、账号/设备子状态。
- 375/760/1440 布局、居中、无横向溢出、Logo 解码、v17 视觉 token、键盘顺序、诊断区域位置。
- 隔离 demo 无 fetch/XHR/storage 写入，无聊天输入/发送；页面无错误。
- 生产构建通过。

未测 / 不在本次范围：

- 真实后端登录、模型校验、Squire 创建/启动、双向聊天与历史恢复（当前明确未接线，禁止伪造）。
- 原生壳、安装包、签名、发布、服务重启。
- 其他浏览器/操作系统、真实读屏与像素级视觉回归（本次为组件截图与计算样式对照）。
- 共享入口接线：若需要把视觉层接入正式 AppShell/路由，由共享 owner（work-developer）评估，不在本分支修改。

## 工具与验收环境说明

- 浏览器：复用 WSL 已有 `~/.cache/ms-playwright/chromium-1243/chrome-linux64/chrome`（1228/headless shell 作为回退候选），未重新安装浏览器。
- CJK：按进程使用已有 `NotoSansSC-VF.ttf` 生成隔离 fontconfig（`FONTCONFIG_FILE`），未安装系统字体、未改权限；截图中文正常，无 tofu。
- 共享脚本硬编码 macOS 浏览器入口属于验收入口适配问题：本任务的浏览器脚本显式使用 `executablePath` 并探测缓存；未修改共享脚本。建议共享 owner 后续提供跨平台 executablePath/env 解析，单独立项。
- 直接以环境变量前缀运行 `DEV01_SCREENSHOT_DIR=... node tests/dev01-onboarding-browser.mjs` 曾要求额外批准，未绕过；改用同仓库 Vitest browser test（同一验收函数）执行。
- 源原型直接哈希被阻止；按上文用批准 vendor 副本 + `cmp` 记录来源。

## 私有预览

```sh
cd gui
npm run dev -- --port 0
# 打开 ready 行给出的 URL，并访问：
# <ready-url>/src/features/dev01/onboarding/preview.html
```

默认端口 4173 可能被现有预览 worktree 占用；不要停止或修改该服务，使用 `--port 0` 或空闲端口。私有预览只使用隔离回调，不访问网络、不写存储、不创建真实账号或 Squire。
