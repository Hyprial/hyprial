# shadcn Button / Card 示例

打开 GUI 的“开始”页，“给下一件事，找一位帮手”卡片和“创建 Agent”按钮使用本地
shadcn 源码。点击继续进入本集成分支的本地 Agent 创建表单；打开表单不调用
真实后端，也不表示已创建真实 Agent。

```bash
cd gui
npm ci --ignore-scripts
npm run build
HYPRIAL_GUI_DEV_PREVIEW=1 npm run dev -- --port 4173
```

需要 Node 24+ / npm 11+。上述端口须空闲；也可以使用 `--port 0` 并读取输出中的实际端口。

## 分层与样式

- 页面继续从 `src/components/primitives` 导入 Button、Card。`components/ui` 是内部
  shadcn 实现；新增页面不绕过适配层。
- Button 将 primary → default、secondary → outline、text → link、danger → destructive，
  ghost 不变；md → default，sm 不变。保留默认 type=button、loading/disabled、图标和全宽。
- Card 通过 Radix Slot 保留 div/section/article/aside，不增加 DOM 包裹。padding、tone、
  selected 与 interactive 仍由项目适配层管理，StatusCard 的未知值保持“未提供”。
- 基础 Button/Card 的样式转移到 Tailwind utilities；`primitives.css` 仅保留图标、
  spinner、label、StatusCard 及其他组件样式。`.ui-*` 类保留为现有页面的布局钩子。
- `shadcn.css` 在现有页面 CSS 后载入，不导入 Preflight。使用 `tw:` 前缀和显式
  `@source`，只扫描这两个组件及其适配层。utilities 不放入 cascade layer，避免现有
  未分层的全局 button font/color 覆盖组件样式；theme 定义仍放 theme layer。
- `@theme inline` 的 `--color-*` 直接映射主题文件（`styles/themes/hyprial-v5.css`）里的
  shadcn 标准变量；圆角与间距映射 `--ui-*`。主题结构与替换方式见 [主题系统](theming.zh-CN.md)。
  深色变量已齐全但无切换入口、未目检。未引入远程字体；中文字体为随包依赖 `@fontsource-variable/noto-sans-sc@5.3.1`（思源黑体，OFL），由主题文件引入，与 shadcn 组件无关。
- 设置、首次设置、Dialog/Notice/States 等也消费这些公共组件，需要
  同步回归。只有开发预览开始页的推荐区域从旧 HTML 控件切换了入口。

## 来源、依赖与升级

2026-10-28 手动从官方 registry 获取 **new-york-v4 / Radix** 源码：

- [Button](https://ui.shadcn.com/r/styles/new-york-v4/button.json)，原始 JSON SHA-256：
  `4b8d7d848742727973c1b088af8d15f0b628272a1316f2d2fa344e73ddff9e9c`。
- [Card](https://ui.shadcn.com/r/styles/new-york-v4/card.json)，原始 JSON SHA-256：
  `c7ebaa49ba2a0613838558f3892c5007523c2dc26d6b36a244145d454a796106`。
- [Select](https://ui.shadcn.com/r/styles/new-york-v4/select.json)，2026-10-29 获取，原始 JSON SHA-256：
  `ca3189ea8101a04f144b6d0f0e7330841639bfa5f4d57e7cca0894d39b30817b`。
- [MIT 许可证](licenses/shadcn-MIT.txt)随源码保留。

本地修改：使用 `@radix-ui/react-slot` / `@radix-ui/react-select` 子包，避免引入整个 Radix
聚合包；使用本地 `cn`（clsx + 配置 `tw` 前缀的 tailwind-merge）；按现有 token 调整
utility、尺寸、换行、焦点与 reduced-motion；Button 保留本次需要的两种尺寸；Card 仅保留有
消费者的 root，并支持 Slot 和既有 block 布局，不导入未使用的 Header/Footer 等部件。

Select（2026-10-29，dev-02 批次）在上述基础上还做了：`tw:` 前缀置于整个 utility 之前
（变体随前缀，如 `tw:disabled:opacity-55`，与 Button 一致）；图标改为内联 SVG，不引入
lucide-react；高度映射 `--ui-control-height-*`；移除动画 utility
（tw-animate-css 不是依赖）；裁剪为 Root/Trigger/Value/Content/Item/滚动按钮与
item-aligned 定位，不导入 Group/Label/Separator。`@theme inline` 增加
`--color-popover`/`--color-popover-foreground` 映射（主题变量 `--popover`/`--popover-foreground`）。
公共适配层 `primitives/Select.tsx` 维持项目语义：`value: ''` 的空选项经内部哨兵往返
（Radix 禁止空字符串 item value）、ref 透传到 trigger、`ui-select` 布局钩子与默认全宽；
jsdom 测试在各测试文件内补 pointer capture / `scrollIntoView` 存根（真实浏览器原生提供）。

依赖固定为 Tailwind / Vite 插件 4.3.3、Radix Slot 1.5.0、Radix Select 2.4.1、CVA 0.7.1、
clsx 2.1.1、tailwind-merge 3.7.0。React 保持 19.3.1。组件源码已入库，不存在运行时下载组件的流程。

本次采用手动安装，未执行 CLI 初始化，也未将 shadcn CLI 加入运行时依赖。
配置文件为 `gui/components.json`；后续生成候选代码时固定 **shadcn CLI 4.23.0**，
例如 `npm exec --package=shadcn@4.23.0 -- shadcn add button --dry-run`。
先阅读 CLI 帮助和 diff，不能直接覆盖本地适配。registry 可变，CLI 固定版本不代表
远端组件源码固定；升级时重新核验来源、许可证、版本与改动。

## 验证

```bash
npm run typecheck
npm test
npm run build
```

真实后端、暗色主题和原生平台均不属于这个示例的完成范围。
