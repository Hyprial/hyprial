# Refactoring UI 提示性检查

`gui/scripts/check-refactoring-ui.mjs` 按 Refactoring UI 的原则扫描共享 GUI 的样式与组件源码，
输出按层分组的待改清单。它是**提示，不是门禁**：默认始终退出 0；`--strict` 是唯一的升级开关。
本检查只读源码，不改变任何运行时行为、后端接口或发布字段。

## 分层与施工顺序

| 层 | 范围 | 顺序 |
| --- | --- | --- |
| base | `styles/themes/`、`styles/tokens.css`、`styles/primitives.css`、`styles/shadcn.css`、`components/primitives/`、`components/ui/` | 1 |
| composite | 其余 `components/`、`features/` | 2 |
| page | `pages/`、其余 `styles/`、`app/`、`App.tsx` | 3 |

`src/dev/` 与生成的客户端不在范围内。视觉调整先收敛 base，再收敛 composite，最后调整 page 布局。

## 规则

| 规则 | Refactoring UI 原则 | 判定 |
| --- | --- | --- |
| `color-literal` | 预先定义色阶，不临时调色 | `styles/themes/` 之外出现十六进制或 `rgb()/hsl()` 字面量 |
| `font-literal` | 字体族集中声明 | `styles/themes/` 之外的 `font-family` 不是 `var(--ui-font-*)` / `inherit` |
| `theme-bypass` | 组件复用主题层 | `themes/`、`tokens.css`、`shadcn.css` 之外直接引用主题变量（如 `var(--card)`），或声明 `--ui-*` |
| `spacing-scale` | 约束的间距体系 | `margin/padding/gap` 的 px 值不在 0/1/2/4/8/12/16/20/24/32/40/48/64/80/96/128 |
| `type-scale` | 约束的字号体系 | `font-size` 不在 12/13/14/16/20/24/30，或使用 em/rem/% |
| `font-weight` | 少量字重，避免细字重 | 字重不在 400/500/600 |
| `radius-scale` | 统一的形状语言 | 圆角 px 值不在 0/2/4/6/8/12/20/28/40/999 |
| `shadow-literal` | 固定的层级体系 | `styles/themes/` 之外的 `box-shadow` 不是 `--ui-shadow-*` token（`0 0 0 …` / `inset 0 0 0 …` 纯环形描边除外） |
| `line-height` | 行高随字号收紧 | 行高不是 `--ui-leading-*` 或 1/1.2/1.5/1.65 |
| `z-index-literal` | 具名层级 | 大于 1 的 z-index 数字 |
| `border-density` | 少用边框 | 单文件边框声明超过 12 处 |
| `fixed-size` | 自适应 | ≥320px 的固定宽高且无 `max-width/min()/clamp()` 保护，或使用 `100vw` |
| `breakpoint` | 统一断点 | `@media` 宽度不在 430/640/820/979/1359 |
| `uppercase-tracking` | 全大写需加字距 | `text-transform: uppercase` 缺 `letter-spacing` |
| `inline-style` | 样式进样式表 | TSX `style={{…}}` 含数值或颜色字面量 |
| `primary-action` | 层级：一个主操作 | 同一 TSX 出现多个 `variant="primary"` |
| `copy-jargon` | 文案面向用户 | 界面文字含 fixture/mock/后端/接线/占位等实现术语 |
| `copy-length` | 去除无关文案 | 单段文案超过 40 个汉字 |
| `inline-feedback` | 统一通知：一次性结果走 toast | composite/page 层 TSX：小写 HTML 标签带 `role="status\|alert"` 或 `aria-live`；或 `setMessage/setResult/setNotice/setFeedback/setStatus/setStatusText` 传入「已保存/已复制/…/成功」类结果字符串 |

豁免路径：颜色、字体族、阴影字面量只在 `src/styles/themes/` 豁免（`tokens.css` 不再豁免）。

2026-11-11 主题系统落地后的基线：base 1 / composite 9 / page 9 处，余下为 `calc(100vw …)` 宽度、既有断点、互斥分支中的主按钮，以及必须保留的凭据安全提示。硬门见 [主题系统](theming.zh-CN.md) 与 `tests/theme-contract.test.ts`。

规则是启发式的：无法可靠判定的值不报。命中不等于必须修改；保留时在 PR 说明理由即可。
`copy-jargon` 与 `copy-length` 只列候选，文案真实性仍以 [设计与研发规范 §4.4](ui-development-standard.zh-CN.md) 为准，
不得为缩短文案而把“未知”“未接通”改写成成功。

## 通知与界面文字

`inline-feedback` 提醒：一次性的操作结果不要写成页面上的一行字，改用 `dispatch({ type: 'push-notice', tone, text })`
走统一通知（toast；info/success 自动消失，warning/error 停留并进入通知中心）。决策表见
[设计与研发规范 §4.6](ui-development-standard.zh-CN.md)。

它报告两种形状：A 手写 live region（小写标签 `p/span/div/small/strong/em/li/output/section` 带 `role="status|alert"` 或 `aria-live`）；
B 本地结果消息（上述 setter 的同一行参数含结果词）。大写组件（`Notice`、`EmptyState`、`Field` 等）永远不报；
失败文案不在 B 的词表内，因为许多失败本就应留在出错处。base 层、`ToastHost.tsx`、`NotificationCenter.tsx`、
`/impl/api/`、`/daemon-api/`、`/read-adapters/`、`src/adapters/` 不检查。

不报告的情形：字段校验（`id` 以 `-error`/`-hint` 结尾、`aria-errormessage`、`components/forms/`）、进行中（`aria-busy`，
或文字含「正在/读取中/处理中/加载中/提交中/保存中」、以 `…` 结尾）、读屏专用（`ui-visually-hidden`/`sr-only`）、
setter 同行已转发 `push-notice`。

确属应留在页面的，在标签上声明 `data-inline-feedback="<reason>"`，reason 只能取：

| reason | 含义 |
| --- | --- |
| `validation` | 字段校验，贴着出错的输入 |
| `progress` | 进行中（正在保存、正在读取） |
| `state` | 持续状态（需要连接模型服务、离线、结果未知），不是某次操作的结果 |
| `blocking` | 阻断性错误：对话框内必须先解决，或整页加载失败 |
| `secret` | 一次性凭据与需抄录内容（邀请链接、恢复码、诊断码） |
| `message` | 消息流里归属于某条消息的状态 |
| `flow` | 多步流程的阶段提示 |

不在列表内的 reason 本身会被报告。

基线（2026-11-11，未改动产品源码）：共 98 处，其中 A 手写 live region 91、B 本地结果消息 7；
composite 80、page 18。按 feature：chat-channels 28（A 28）、org 18（A 16 / B 2）、agents 16（A 13 / B 3）、
connections 7、settings 3（A 1 / B 2）、agent-chat 2、chat 2、onboarding 2、agent-provider 1、network 1；
页面：pages/channels 5、pages/live 5、ConnectedChatPage 4、pages/org 2、ConnectionsPage 1、ContactsPage 1。
抽样 15 处中真正的「一次性结果写成页面文字」只有少数（如 AgentDetail 的「已保存修改」）；
多数是「尚未确认/暂不可用」这类持续状态，应补 `data-inline-feedback="state"` 或改用 `Notice`/状态卡。

## 使用

```bash
npm --prefix gui run check:refactoring-ui
npm --prefix gui run check:refactoring-ui -- --verbose --layer base
npm --prefix gui run check:refactoring-ui -- --json /tmp/rui.json
npm --prefix gui run check:refactoring-ui -- --base origin/dev --annotations
```

- 无参数：输出三层的规则计数与命中最多的文件。
- `--verbose`：逐条列出 `文件:行 [规则] 声明`。
- `--layer base|composite|page`：只看一层。
- `--base <ref>`：只报告相对 `<ref>` 新增行上的命中；CI 用它给 PR 发 `::warning` 注解。
- `--strict`：有命中即退出 1。CI 当前不传此参数。

CI 位置：`.forgejo/workflows/gate-gui.yml` 的 “Remind on Refactoring UI findings” 步骤，`continue-on-error: true`。
升级为门禁时在该步骤加 `--strict` 并去掉 `continue-on-error`，其余不变。
规则测试见 `gui/tests/tooling/refactoring-ui-check.test.mjs`。
