# shadcn Button / Card 示例实施计划

**目标：** 在根 `gui/` 接入可构建的 shadcn 源码，让开始页的推荐卡片和创建按钮成为实际消费者，并单独提交 PR。

**架构：** 官方 new-york-v4 / Radix 组件源码放在 `components/ui`，现有 `components/primitives` 保持公共接口。Tailwind 4 通过 Vite 构建，使用 `tw:` 前缀、显式源码扫描和现有 `--ui-*` token，不导入 Preflight。业务动作继续调用现有占位 adapter。

**技术栈：** 现有 React 19.3.1、TypeScript、Vite、Vitest；Node 24+ / npm 11+；新增依赖固定版本并提交 npm lockfile。

## 约束与验收

- 延续 PR #2032 的规范；本 PR 从 `dev` 的 `63ab7e7ac` 开始，不包含 #2032 的提交。
- 保持 Button 的 primary/secondary/ghost/text/danger、sm/md、loading、disabled、默认 type=button、图标和 fullWidth 行为。
- 保持 Card 的语义标签、tone、padding、selected 和 interactive 行为；StatusCard 的未知值仍为“未提供”。
- 保留 fixture 标记、HTTP 501、BACKEND_NOT_WIRED 和当前占位文案；不接真实后端，不添加新的业务状态。
- 不增加展示组件技术细节的产品页面；真实开始页就是示例，开发者说明放文档。
- 不覆盖旧 --card / --muted 等变量。只迁移 Button/Card 的样式归属，其余 primitives 与页面保持原样。
- 不升级 React，不安装无消费者的组件；保留源码来源、许可证及本地改动说明。

## 执行步骤

- [x] 安装基线依赖，运行 `npm --prefix gui run verify`，记录已有失败。
- [x] 扩展组件和开始页测试：语义标签、键盘提交、禁用/忙碌阻止重复请求、真实占位错误；确认新增接入断言先失败。
- [x] 新增 `gui/components.json`、`src/lib/utils.ts`、`src/components/ui/button.tsx` / `card.tsx` 和 `src/styles/shadcn.css`；配置 TS/Vite/Vitest 别名。
- [x] 在 primitives 适配上游 variant/size 与 Card 语义标签，移除重复的 Button/Card 基础 CSS；开始页推荐区域改用公共入口。
- [x] 执行 `npm --prefix gui run verify`，涵盖所有既有消费者；执行干净锁定安装后复验。
- [x] 使用隔离的本机 Node 占位服务与浏览器，在桌面及 360/390/430px 检查布局、键盘、长中文、忙碌、501 错误和未迁移页面样式。
- [x] 独立审阅 diff，修复发现，记录验证结果。

交付方式：通过内部 SSH/Tailscale 推送独立分支，创建目标为 dev 的新 PR。

## 验证边界

本次不验证真实 daemon、登录、原生壳或暗色主题。Python 结构门不覆盖根 `gui/`，稀疏工作树缺失 Python 根目录时不能将其结果记为 PASS；完整仓库门由 CI 复核。

## 实测结果（2026-10-28）

- PASS：Node 24.20.0 下干净 `npm ci --ignore-scripts`，直接依赖和 React 版本锁定。
- PASS：typecheck；`npm test -- --maxWorkers=2`，18 个文件、127 项测试；生产构建。
- PASS：Chromium 生产产物验收，真实占位服务 HTTP 501、键盘/焦点、忙碌防重、长中文、1280/360/390/430px、设置与首次设置消费者、reduced-motion。
- PASS：与接入前快照比较标题、统计卡、侧栏、旧按钮和看板的 computed style，一致。
- PASS：独立审阅发现的 selected Card 阴影与 sm Button 行高问题已修复；回归测试先失败后通过，审阅者再次用 Chromium 核验。
- 环境说明：WSL 原 IPv4 回环路由拒绝连接，挂载盘默认并发曾导致 worker/交互超时。最终在与工作树源码一致的 Linux 临时副本中干净安装，并使用 `unshare --user --map-root-user --net` 的隔离回环网络和 2 个测试 worker 通过全部 GUI 测试；没有修改系统路由或测试断言来规避失败。
- BLOCKED：稀疏工作树中的 Python 结构门缺少 `src/hyprial` / `tests`，完整 Python 测试未运行；本次没有修改 Python 或协议代码。远端 CI 结果须独立复核。
