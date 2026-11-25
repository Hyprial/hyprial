# 前端组件规范与 shadcn/ui 接入

状态：组件接入与迁移规范；本 PR 仅修改文档，不安装组件或依赖。
依据：初稿基于 2026-10-28 的 `dev` `d7175833a`，现已同步 `dev` `0567e1c99`。
Button/Card 与 Tailwind 已由 #2038 接入；当前实现见
[shadcn Button / Card 示例](../../gui/docs/shadcn.md)，本文阶段 0 描述保留为迁移起点。

现行前端是根目录下的 `gui/`，技术与启动边界见 [GUI README](../../gui/README.md) 和 [GUI 开发模式](gui-dev-mode.md)；
仓库规则见 [AGENTS.md](../../AGENTS.md)。旧 `src/gui/` 与 `app/` 已退场，
不作为 shadcn 接入或恢复目标。

本规范补充 [GUI 设计与研发规范](../../gui/docs/ui-development-standard.zh-CN.md)
中的组件接入与迁移规则；当前公共接口及页面覆盖见
[组件目录](../../gui/docs/component-catalog.zh-CN.md)。已接入状态以源码和该目录
为准，本文的阶段 0 记录不替代后续实现 PR 的结果。

## 1. 当前基础与接入方向

`gui/` 已使用 React 19.3.1、TypeScript、Vite、Vitest 和 Testing Library，已有
TSX 编译与组件测试。阶段 0 编写时 package 尚未声明 Tailwind 或 shadcn 组件依赖；
后续接入情况以当前 package、源码及组件目录为准。

| 现有位置 | 职责 | 接入原则 |
| --- | --- | --- |
| `gui/src/components/primitives` | Button、Card、Dialog、Field、Notice、PageHeader、Progress、States、Stepper 等 | 保持公共接口，按消费者逐项适配 |
| `gui/src/components` | Badge、AgentAvatar、Rail、AccountMenu 等共享组合 | 复用统一基础组件，避免复制同名实现 |
| `gui/src/styles/themes/hyprial-v5.css`、`gui/src/styles/tokens.css` | 主题变量（颜色/阴影/字体族字面量的唯一来源）与 `--ui-*` 语义别名及刻度 | 组件只消费 `--ui-*`；旧变量层已退役，见 `gui/docs/theming.zh-CN.md` |
| `gui/src/styles/primitives.css` | 现有 `.ui-*` 控件样式 | 逐组件交接样式归属，避免双重覆盖 |
| `gui/src/pages`、`app`、`state`、`adapters` | 页面、外壳、应用状态和占位接口 | 组件替换不改变业务状态与接口边界 |

采用“小范围适配现有 primitives，再迁移实际消费者”的路线。只共享配色可以先改善
一致性，但不算引入 shadcn 组件；全量替换页面会扩大行为和视觉回归范围，不作为
安装组件的附带工作。

shadcn/ui 的组件源码纳入项目后，由团队负责定制、依赖、可访问性和升级。新增组件
必须有消费者和使用场景，不一次导入整个组件目录。

## 2. 目录、公共接口与业务边界

推荐在实际接入时形成以下结构；`ui/` 与 `components.json` 是阶段 0 的新增目标：

```text
gui/
  components.json                # shadcn 生成配置
  src/
    components/
      ui/                        # 本地维护的 shadcn 源码，作为内部实现
      primitives/
        index.ts                 # 项目统一的基础组件公共入口
        Button.tsx               # 保持项目语义与兼容 props 的适配层
    styles/
      tokens.css                 # 共享语义 token 来源
      primitives.css             # 现有控件样式，按迁移进度收敛
```

- 页面优先从 `components/primitives` 导入基础控件。`components/ui` 由基础组件
  适配层消费，不让各页面自行选择不同来源的 Button、Dialog 或另一套主题。
- 保留现有 Button 的 `primary/secondary/ghost/text/danger`、`sm/md`、`loading`
  和默认 `type="button"` 等语义。上游 props 的差异集中转换；公共 API 变更须同步
  消费者及测试，不能把上游默认值直接传给页面。
- Badge、AccountMenu 等组合沿用既有入口。通用能力下沉到 primitives，业务对象与
  页面布局留在组合层，不以复制实现来解决颜色或间距差异。
- 基础组件只处理展示、焦点、输入及临时 UI 状态，不调用 HTTP、daemon、CLI 或原生
  桥接，不读取凭据，不决定会话、任务、登录或设备的业务状态。
- 页面动作继续经现有 adapter 和状态流转。组件接入不增加持久化、权限或后端 owner，
  不以组件局部 state 维护另一份业务真值。
- 按职责组织源码，遵守当前适用的结构门禁；生成代码不能移入排除检查的目录。新 GUI
  沿用 TypeScript 公共入口，不复制历史 `src/gui` 的 `.mjs` 模块布局。

## 3. 依赖、生成与构建

1. 使用官方 `ui.shadcn.com` 文档和 registry。记录组件来源、获取日期、CLI 版本、
   基础实现与 preset、本地修改及依赖，保留适用许可证。第三方 registry 独立审查。
2. 在 `gui/` 中维护配置、依赖与 lockfile，沿用 Node 24+、npm 11+ 和现有 Vite。
   直接依赖和 CLI 固定版本，CI 使用锁定安装，不因初始化默认值顺带升级 React。
3. `components.json` 使用 `tsx: true`、`rsc: false`，启用 CSS variables。别名与
   TypeScript、Vite、Vitest 的实际解析一致；生成目录符合组件分层。
4. 同一组基础组件选择一致的底层实现，例如 Base UI 或 Radix；按现有 React 版本、
   控件行为和目标浏览器验证后固定选择，不把未测试的版本组合写成兼容结论。
5. Tailwind 复用现有 Vite 管线，明确样式入口、源码扫描范围、CSS 导入顺序及生产
   产物。不另建独立应用或第二个 React root 来绕过现有页面结构。
6. 先验证少量实际消费者，再继续添加组件。保留类型检查、交互测试和生产构建；修改
   来源后重建，不手改 `dist`，不运行时从 CDN 下载组件代码。

## 4. 主题与样式规则

主题文件 `themes/hyprial-v5.css` 以 shadcn 标准变量名定义颜色、阴影、字体族；`tokens.css`
把它们别名为组件消费的 `--ui-*`，`shadcn.css` 的 `@theme inline` 把 `--color-*` 映射到同一批变量。
初始化工具的默认配色不能覆盖全局主题。`gui/tests/theme-contract.test.ts` 强制主题文件之外零字面量。

| shadcn token | 项目语义（组件侧别名） |
| --- | --- |
| `background` / `foreground` | `--ui-ground` / `--ui-ink-1` |
| `card`、`popover` 及其 foreground | `--ui-surface` / `--ui-ink-1` |
| `primary` / `primary-foreground` | `--ui-accent` / `--ui-on-accent` |
| `muted` / `muted-foreground` | `--ui-ground-2` / `--ui-ink-3` |
| `border`、`input`、`ring` | `--ui-border`、`--ui-input-fill`、`--ui-focus` |
| `destructive` | `--ui-danger`，配对前景 `--ui-danger-ink` |

- `secondary`、`accent`、hover、disabled 和前景配对结合现有控件样式明确。不把
  `--muted` 或 `--ui-ink-3` 这类文字色直接作为 muted 背景。
- 页面使用语义样式；品牌色、字号、间距、圆角和状态颜色集中维护。沿用现有 4px
  间距基准、控件尺寸、字体及动效 token，不在业务页面散布覆盖值。
- 保留 info、positive、warning、danger 等含义，不用主色替代错误或
  成功状态。新增正常文本与实际背景的对比度至少 4.5；半透明背景按合成结果检查。
- 主题文件已声明深色变量，但没有切换入口，也未目检页面。引入组件不等于支持暗色；
  后续增加暗色模式时，应验收页面和弹层。
- Tailwind 样式限定到迁移区域，审查 Preflight 对原有页面的影响。仅给 utility
  加前缀不能隔离全局 reset；不能让未迁移页面的表单或布局意外变化。
- Dialog、Popover、Tooltip 的 portal 必须继承所属界面的变量、字体与层级，不能
  因默认挂到 `body` 而丢失局部主题、样式范围或焦点归属。
- 字体为随包本地的 Geist 与系统中文字体（声明在主题文件）；不新增远程字体请求，
  也不把字体回退造成的差异当作验收通过。

## 5. 首批组件与交互约定

以下顺序以真实消费者为准，没有使用场景的组件不安装：

| 顺序 | 候选组件 | 与当前实现的关系 |
| --- | --- | --- |
| 第一批 | Button、Card、Input、Textarea、Label | 通过现有 Button/Card/Field 接口适配，选择一个页面区域试点 |
| 后续展示 | Badge、Separator、Skeleton | 复用既有 Badge、LoadingState 语义，避免重复状态组件 |
| 后续交互 | Dialog、AlertDialog、Popover、Tooltip、DropdownMenu、Select | 先验证 Dialog/AccountMenu 消费者的键盘、焦点、关闭和 portal 行为 |
| 按需增加 | Tabs、Sheet、ScrollArea、Table | 验证窄屏、滚动归属、隐藏内容生命周期及页面状态保持 |

- 忙碌按钮阻止重复操作，并提供可感知状态。保留原有 `loading`、`disabled` 和提交
  语义；中文输入法合成期间不误触发送。
- 输入控件和图标按钮有可访问名称，字段错误与输入关联。Tooltip 不能是触屏用户
  获得必要信息的唯一方式；Dialog 验证焦点进入、约束、Esc 关闭及关闭后恢复。
- 保留 EmptyState、LoadingState、ErrorState 的区别。Skeleton
  仅代表加载中，组件不能把失败、未接入或未知状态渲染为成功。
- 普通源码入口是 M1 同源宿主：同一组件经 `/daemon/*`、同一 generated client 和
  已注册 adapter 消费所选 daemon 或 Contract Mock。fixture 只在
  `HYPRIAL_GUI_DEV_PREVIEW=1` 或显式测试组合中保留 `FIXTURE · 演示数据`；它不能
  成为普通入口的回退。配置 driver/endpoint 不等于 login、online 或 backend ready。
- 未注册方法显示明确的 unavailable/unknown；登录、首次初始化和 chat send 在 M2
  与对应契约完成前不可用。组件替换不得伪造登录/设备状态、请求成功或新的 Agent
  回复。旧 `/api/gui/v1/not-wired` 501 的独立 Node 占位服务已删除。
- 凭据输入继续在提交后清空，占位层继续脱敏；UI 改造不把 token 存入浏览器持久化
  或日志。未来真实后端接线属于独立接口与授权工作。
- 官方 blocks 的样例数据和行为不能直接当作业务实现；项目明确标注的隔离 fixture
  可以保留，但必须与未实现操作的真实返回一致。

## 6. 迁移顺序与退出条件

### 阶段 0：规范

本次只增加规范与发现入口，不安装组件、依赖或改造页面。旧 GUI 资料仅作为历史，
不恢复旧目录、Studio schema、Core 端口或原生壳来承载新组件。

### 阶段 1：基础设施与一个消费者

选择一个现有页面区域，例如 Start 页的一张卡片及其操作按钮。补齐 Tailwind、生成
配置和语义映射，在现有 primitives 接口后面接入组件，明确该控件的新旧样式归属。

退出条件：干净安装与生产构建可复现，类型与针对性组件测试通过，真实浏览器中键盘、
点击、占位错误、中文长文及窄屏行为正确，未迁移页面无全局样式回归。

### 阶段 2：按组件和页面迁移

逐项迁移控件及消费者，先列明 props、variant、状态和事件映射；复杂控件另验收
焦点与 portal。保持 adapter 和页面状态流转，保留可回退到旧组件实现的改动边界。
全部消费者迁移并验收后，再删除重复实现与遗留样式。

### 阶段 3：扩大平台或业务能力

真实 backend、原生客户端和移动认证按现行 ADR 与对应 owner 的范围另行接入。
网页组件通过测试不等于这些能力完成；WebView、CSP、安全区域和软键盘在实际承载
环境中单独验证，不以恢复旧 `app/` 作为默认方案。

## 7. 验证与升级

- 沿用 Vitest 和 Testing Library，覆盖 props、键盘、焦点、表单、禁用/忙碌、错误
  恢复和适配层语义。扩展已有 primitives、token、responsive 契约测试，避免逐行复制样式断言。
- 在 Node 24+ 环境执行 `npm --prefix gui ci --ignore-scripts` 和
  `npm --prefix gui run verify`（typecheck、test、build），并满足改动适用的仓库门禁。
- 浏览器检查当前 light 主题、长中文、空/加载/失败/未接入状态及 360/390/430px
  窄屏。实现暗色后再增加对应验收，不能把未测能力写成已支持。
- 新增或升级组件均审阅源码、依赖、lockfile、样式和许可证变化。不无条件覆盖本地
  适配，升级后验证实际消费者，记录通过、失败和未测范围。

## 8. 官方参考

以下页面在 2026-10-28 核验；实施时复核版本兼容性并固定具体选择：

- [shadcn/ui 介绍与源码分发方式](https://ui.shadcn.com/docs)
- [手动安装与 Tailwind 前置条件](https://ui.shadcn.com/docs/installation/manual)
- [components.json：语言、别名与生成配置](https://ui.shadcn.com/docs/components-json)
- [主题与语义 CSS variables](https://ui.shadcn.com/docs/theming)
- [Button 的基础实现与组件用法](https://ui.shadcn.com/docs/components/button)
