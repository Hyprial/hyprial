# 交接：wheel 安装态 `hyprial gui` 验收脚本与验收环境

日期：2026-11-15。发起：GUI 单一启动线。状态：待接手。

## 目标

GUI 单一启动线当前的达标标准是：**从 wheel 安装的 `hyprial gui` 能正常运行**。本任务交付一个可重复执行的验收脚本，
以及一台能跑它的机器上的实测结果。它与正在进行的 PR 2b（Python session host）、PR 2c（wheel 改走 Python host、
删除 501 占位服务）没有代码交叉，可以并行。

背景与决策见 [单一启动设计](../gui-single-launch-2026-11-14.md) 与 [host 计划](host-2-3.md) 的 D.3 节
（“Wheel acceptance”一段就是本任务要自动化的内容）。

## 为什么需要外部机器

GUI 线的开发环境是受限沙箱，可能构建不出带原生 actor 引擎的 wheel（见 `docs/reference/known-problems.md` 的 KP-027）。
需要一台能完成 `scripts/build/build-wheel.sh` 的 Linux x64 或 macOS arm64 机器。Windows 不在本任务范围。

## 交付物

1. **验收脚本**，放在 `scripts/verify/`（文件 ≤ 1000 行；仓库规则见 `AGENTS.md`）。一条命令完成：
   1. 在干净 checkout 上构建 wheel（`scripts/build/build-wheel.sh <outdir>`；不得用 `HYPRIAL_BUILD_SKIP_NATIVE=1` 或伪造标签）。
   2. 新建一次性 venv（Python 3.12），只安装该 wheel。之后的步骤不得依赖源码 checkout、Node、npm 或 Git。
   3. 使用全新的隔离 `HYPRIAL_HOME`（临时目录）。准备一个“已登录且 daemon 在运行”的 home；
      做法参照 `tests/integration/request_http/onboarding/test_gui_admission.py` 及其 fixture 建立隔离 home 与 daemon 的方式。
   4. 运行 `hyprial gui --no-open --json --port <空闲端口>`，解析 stdout 的 JSON（必须恰好一个对象且含 `ok`），
      读取启动地址（运行目录下的 `launch.url`，位置以 `src/hyprial/shell/impl/gui/backend/source.py` 为准）。
   5. 用 Chromium（Playwright）打开该地址，断言：Start 页出现；浏览器发出的 `/daemon/request/*` 至少有一次返回 200；
      没有任何请求命中 `not implemented: hyprial backend not wired`。
   6. 结束 GUI 进程，确认 GUI 子进程退出；**daemon 不应因 GUI 关闭而停止**（设计要求），随后由脚本显式停止它并清理临时目录。
   7. 输出一行机器可读结论：`PASS` / `FAIL <步骤> <原因>` / `BLOCKED <步骤> <原因>`，退出码与之对应。
2. **脚本自身的测试**（放在 `tests/tooling/` 下合适的目录，注意每目录 ≤ 7 个直接文件）：
   覆盖结论判定逻辑（给定各步骤结果得到 PASS/FAIL/BLOCKED），不需要真的构建 wheel。
3. **两次实测记录**，写进 PR 正文：
   - 在当前 `dev` 上运行：预期在第 5 步 **FAIL**（wheel 目前由 501 占位静态服务提供页面）。请记录真实输出。
   - PR 2c 合入 `dev` 后再运行一次：预期 **PASS**。2c 合入时 GUI 线会在本 PR 评论通知。
4. **KP-027 现场**：如果 wheel 在你的机器上构建或安装失败，把完整命令、错误输出、平台信息写进 PR 正文，
   结论记 BLOCKED，不要绕过。能定位到原因的话一并写明；修复 KP-027 本身不属于本任务。

## 红线

- 绝不读取、改写或清理真实 `~/.hyprial`、生产 daemon、Lark App、凭据和用户级配置；home、socket、端口、缓存全部隔离。
- 一律用 `uv` 管理 Python，不裸跑 `python`。
- 不用 skip/xfail 冒充结论；BLOCKED 不是 PASS。
- 不新增行内注释（决策写文件头决策块，docstring 保留）；不提交凭据、内网主机名或邮箱。
- 不修改 `src/hyprial/shell/impl/gui/**`、`gui/**`、`contract/gui-session/**`（2b/2c 正在改）。
  发现产品缺陷时在 PR 评论里报告，由 GUI 线处理。
- 不接入 CI workflow：本脚本先作为手动验收入口；是否进 CI 由 GUI 线在 2c 之后决定。

## 完成时请这样交回

1. 提交并推送到本 PR 的分支 `test/gui-wheel-acceptance`（普通提交，不 force-push）。
2. 把 PR 标题里的 `WIP:` 去掉。
3. 在本 PR 发一条评论，写明“已完成”，并列出：脚本路径、运行命令、机器平台、当前 `dev` 上的实测结论与输出摘要、
   未完成或未测项（标 UNKNOWN / BLOCKED）。

GUI 线以“标题去掉 WIP + 完成评论”作为接手审查与合入的信号，不依赖其他渠道转告。合入由 GUI 线执行。
