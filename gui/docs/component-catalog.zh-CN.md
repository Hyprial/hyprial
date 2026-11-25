# Hyprial GUI 组件目录

文档版本：2026-10-28

对应源码：仓库根 `gui/src/`

配套规范：[GUI 设计与研发规范](ui-development-standard.zh-CN.md)

本目录只记录**当前源码里实际存在的组件和参数**。没有经过源码和测试确认的“计划组件”不列入。组件有实现不等于页面已全部使用；页面覆盖情况见第 5 节。

状态标记：

- **已接入**：当前 App 的用户路径使用该组件。
- **可用未接入**：源码和测试存在，但当前 App 尚未路由/组合。
- **共享外壳**：应用壳或页面使用，但仍是旧样式体系，不是 `.ui-*` primitive。
- **目标**：尚未实现，不能当作可复用组件。

## 1. 使用顺序

1. 先查本目录和 `gui/src/components/primitives/`。
2. 页面局部结构先留在页面；多个页面共享再提升为共享组件。
3. 通用交互/无障碍语义缺失时才新增 primitive。
4. 新增或修改组件后，同步本目录、token 说明和相关测试。

所有新 primitives 从统一入口导入：

```tsx
import {
  Button,
  Card,
  StatusCard,
  PageHeader,
  Notice,
  Field,
  Dialog,
  LoadingState,
  EmptyState,
  ErrorState,
  ProgressBar,
  Stepper,
  DiagnosticBox,
} from '../components/primitives';
```

路径按调用文件位置调整；primitive 入口是 [`src/components/primitives/index.ts`](../src/components/primitives/index.ts)。

## 2. 基础 token

完整定义见 [`src/styles/tokens.css`](../src/styles/tokens.css)，值来自 [主题文件](../src/styles/themes/hyprial-v5.css)（说明见 [主题系统](theming.zh-CN.md)）；契约测试见 [`tests/token-contract.test.ts`](../tests/token-contract.test.ts) 与 [`tests/theme-contract.test.ts`](../tests/theme-contract.test.ts)。新增 primitive 只能消费已声明的 `--ui-*` token，不得硬编码颜色、阴影、字体族、远程字体、`@import` 或外部 `url(...)`。

| 类别 | 当前 token | 说明 |
| --- | --- | --- |
| 文本 | `--ui-ink-1`、`--ui-ink-2`、`--ui-ink-3`、`--ui-ink-4` | 从主文本到弱提示 |
| 背景/表面 | `--ui-ground`、`--ui-ground-2`、`--ui-surface`、`--ui-surface-muted` | 页面底、弱底、卡片 |
| 边框 | `--ui-border`、`--ui-border-strong` | 普通/强调边框 |
| 动作 | `--ui-accent`、`--ui-accent-hover`、`--ui-accent-wash`、`--ui-on-accent` | 主操作/悬停/浅底/反白 |
| 状态 | `--ui-info`、`--ui-positive`、`--ui-warning`、`--ui-danger` 及各自 wash | 信息/成功/警示/危险 |
| 焦点 | `--ui-focus`、`--ui-focus-width` | 键盘焦点环 |
| 字体 | `--ui-font-ui`、`--ui-font-cn`、`--ui-font-mono` | 本地 Geist 配随包思源黑体（Noto Sans SC）；mono 为回退栈 |
| 字号/行高 | `--ui-text-xs` … `--ui-text-3xl`、`--ui-leading-*`、`--ui-tracking-wide`、`--ui-tracking-tight` | 12–30px；1.2/1.35/1.5/1.65 |
| 间距 | `--ui-space-1`、`--ui-space-2`、`--ui-space-3`、`--ui-space-4`、`--ui-space-5`、`--ui-space-6`、`--ui-space-8`、`--ui-space-10`、`--ui-space-12`、`--ui-space-16`、`--ui-space-20`、`--ui-space-24`、`--ui-space-32` | 4px 基准，至 128px |
| 圆角 | `--ui-radius-xs` … `--ui-radius-xl`、`--ui-radius-pill` | 8/12/20/28/40/999px |
| 阴影 | `--ui-shadow-xs/sm/card`（`none`）、`--ui-shadow-float-sm`、`--ui-shadow-popover`、`--ui-shadow-dialog` | 静止表面无阴影；只有浮层有 |
| 层级 | `--ui-z-raised` … `--ui-z-dialog-popover` | 具名 z-index；不写数字字面量 |
| 宽度上限 | `--ui-measure-content`、`--ui-measure-form`、`--ui-width-prose`、`--ui-width-form`、`--ui-width-dialog` | 内容栈 720px、表单 480/560px、正文 68ch |
| 步骤条 | `--ui-step-current`、`--ui-step-track` | 首次设置步骤条当前/轨道色 |
| 动效 | `--ui-motion-fast`、`--ui-motion-base`、`--ui-motion-slow`、`--ui-ease-standard` | 120/180/260ms |
| 控件 | `--ui-control-height-sm`、`--ui-control-height-md`、`--ui-control-height-lg` | 40/48/56px |

旧 token（`--ink`、`--paper`、`--line`、`--green`、`--btn-*` 等）已退役。2026-11-11 起主题文件之外不再写颜色、阴影或字体族字面量，`tokens.css` 之外不写 z-index 字面量；前者由 `theme-contract` 测试强制，体系外的值由 [提示性检查](refactoring-ui-advisory.zh-CN.md) 提醒。

## 3. Primitives

Primitive 样式主要在 [`src/styles/primitives.css`](../src/styles/primitives.css)。Button/Card 的基础样式由 [`src/styles/shadcn.css`](../src/styles/shadcn.css) 生成，复用现有 `--ui-*` token；图标、spinner、StatusCard 及其他组件样式仍在 primitives.css。组件行为测试在 [`tests/primitives.test.tsx`](../tests/primitives.test.tsx)，shadcn 适配回归见 [`tests/shadcn-example.test.tsx`](../tests/shadcn-example.test.tsx)。

Button/Card/Select 保持下列公共 props（Select 见 3.2a），内部使用本地维护的 [`ui/button.tsx`](../src/components/ui/button.tsx)、[`ui/card.tsx`](../src/components/ui/card.tsx) 与 [`ui/select.tsx`](../src/components/ui/select.tsx)。页面继续通过 primitives 导入；来源、依赖、样式边界和运行方式见 [shadcn 示例说明](shadcn.md)。

### 3.1 Button

源码：[`primitives/Button.tsx`](../src/components/primitives/Button.tsx)

状态：**已接入**（设置、首次设置、fixture 开始页推荐区域等）

| Prop | 类型/默认 | 说明 |
| --- | --- | --- |
| `variant` | `primary \| secondary \| ghost \| text \| danger`，默认 `secondary` | primary 近黑；secondary 为 neutral 平面底无描边；ghost/text 为 ghost；danger 浅红底深红字 |
| `size` | `sm \| md`，默认 `md` | 40/48px 高度；胶囊，按下 `scale(.98)` |
| `loading` | `boolean`，默认 `false` | 显示 spinner、禁用并设置 `aria-busy` |
| `type` | `button \| submit \| reset`，默认 `button` | 默认不会误提交表单 |
| `leadingIcon` | `ReactNode` | 加载时被 spinner 替代 |
| `fullWidth` | `boolean` | 占满容器 |
| `onClick` | `() => void` | 加载/禁用时不触发 |
| `children` | `ReactNode` | 按钮文字，应保持可读 |

```tsx
<Button variant="primary" loading={busy} onClick={save}>
  保存模型服务
</Button>
```

约束：危险操作优先 `danger`，不要用 `primary` 加红色自绘；按钮文字不要只用一个符号；忙碌状态不能重复提交。

### 3.2 Card / StatusCard

源码：[`primitives/Card.tsx`](../src/components/primitives/Card.tsx)

状态：**已接入**（设置、首次设置）；Card 还用于 fixture 开始页推荐区域，开始页实时视图 scaffold 也使用

`Card` 是纯表面，不做业务判断。

| Prop | 类型/默认 | 说明 |
| --- | --- | --- |
| `as` | `div \| section \| article \| aside`，默认 `div` | 语义容器 |
| `tone` | `neutral \| info \| positive \| warning \| danger`，默认 `neutral` | 语义色 |
| `padding` | `sm \| md \| lg`，默认 `md` | 12/16/24px |
| `interactive` | `boolean` | 悬停/可点击提示；仍需真实交互元素 |
| `selected` | `boolean` | 选中底色/描边和 `data-selected` |
| 其他 | `HTMLAttributes` | 透传 aria/id/className 等 |

`StatusCard` 专门展示单个状态：

| Prop | 类型 | 说明 |
| --- | --- | --- |
| `label` | `string` | 状态名称 |
| `value` | `string \| null` | `null` 渲染“未提供”，绝不变成 0 |
| `hint` | `string?` | 补充说明 |
| `tone` | `UiTone?` | 语义色 |
| `className` | `string?` | 附加类名 |

```tsx
<StatusCard
  label="本机设备"
  value={deviceId ?? null}
  hint="设备标识由受支持接口提供"
  tone="neutral"
/>
```

约束：`Card` 的 `interactive` 只是视觉提示；真正可点击时必须在内部放 button/link，或让调用方提供完整键盘行为。不要用 `Card` 的 `tone` 表达真实成功，除非状态源已确认成功。

### 3.2a Select

源码：[`primitives/Select.tsx`](../src/components/primitives/Select.tsx)

状态：**已接入**（Agent 创建/编辑表单的所属组织与默认 harness、连接页登录服务商流程）

单选下拉，内部使用本地维护的 [`ui/select.tsx`](../src/components/ui/select.tsx)（Radix Select）。页面继续通过 primitives 导入；来源、依赖与本地修改见 [shadcn 示例说明](shadcn.md)。

| Prop | 类型/默认 | 说明 |
| --- | --- | --- |
| `id` | `string` | 落在 trigger 上，配合页面 `<label htmlFor>` |
| `value` | `string` | 受控值；`''` 表示选中"空选项"（如 未指定 / 不加入组织），经内部哨兵映射（Radix 禁止空字符串 item value） |
| `options` | `readonly { value, label, disabled? }[]` | 扁平选项表；`disabled: true` 保留展示但禁止鼠标/键盘选中；空选项以 `value: ''` 显式声明 |
| `onValueChange` | `(value: string) => void` | 回调收到的空选项仍是 `''` |
| `disabled` | `boolean`，默认 `false` | 禁用 trigger |
| `className` | `string` | 附加在 trigger 上；默认 `ui-select` 钩子 + 全宽 |
| `ref` | `Ref<HTMLButtonElement>` | 透传到 trigger（如对话框打开时聚焦） |

```tsx
<Select
  id="agent-harness"
  value={harness}
  options={[
    { value: '', label: '未指定' },
    ...HARNESS_OPTIONS.map((h) => ({ value: h, label: h })),
  ]}
  onValueChange={setHarness}
/>
```

约束：键盘、焦点、Esc 关闭与 portal 行为来自 Radix（jsdom 测试需在测试文件内补 pointer capture / `scrollIntoView` 存根）；选项列表保持扁平，分组等复杂形态未接入。

### 3.3 PageHeader

源码：[`primitives/PageHeader.tsx`](../src/components/primitives/PageHeader.tsx)

状态：**已接入**（设置、首次设置）；实时开始页 scaffold 使用

| Prop | 类型/默认 | 说明 |
| --- | --- | --- |
| `title` | `string` | 页面标题 |
| `eyebrow` | `string?` | 上层分组/领域 |
| `description` | `string?` | 说明，最大约 68ch |
| `actions` | `ReactNode?` | 页头操作 |
| `stepper` | `ReactNode?` | 步骤条等 |
| `headingLevel` | `1 \| 2 \| 3`，默认 `1` | 保持标题层级 |
| `className` | `string?` | 附加类名 |

```tsx
<PageHeader
  eyebrow="系统"
  title="设置"
  description="账号、设备、模型与 Squire 的常驻管理入口。"
  actions={<Button variant="secondary">打开首次设置</Button>}
/>
```

约束：一个页面通常只有一个一级标题；嵌套页面/管理区块用 `headingLevel={2}`，不要复制多个 `h1`。

### 3.4 Field

源码：[`primitives/Field.tsx`](../src/components/primitives/Field.tsx)

状态：**已接入**（设置、首次设置）

| Prop | 类型 | 说明 |
| --- | --- | --- |
| `label` | `string` | 可见 label |
| `htmlFor` | `string` | 与控件 id 一致 |
| `hint` | `string?` | 生成 `-hint` 关联 |
| `error` | `string?` | 生成 `-error`、`aria-invalid`、`role="alert"` |
| `required` | `boolean` | 设置 required 和视觉星号 |
| `children` | `ReactNode` | 单个原生控件时自动克隆 aria 属性 |
| `className` | `string?` | 附加类名 |

```tsx
<Field
  label="模型密钥"
  htmlFor="model-key"
  hint="提交后立即清除，不写入本地存储。"
  error={keyError}
>
  <input
    id="model-key"
    type="password"
    autoComplete="off"
    value={key}
    onChange={(event) => setKey(event.target.value)}
  />
</Field>
```

约束：必须提供稳定 `htmlFor`/`id`；secret 生命周期由表单 owner 负责，`Field` 不读取、不保存值。错误文案不能回显 secret。

### 3.5 Notice

源码：[`primitives/Notice.tsx`](../src/components/primitives/Notice.tsx)

状态：**已接入**（首次设置）

| Prop | 类型/默认 | 说明 |
| --- | --- | --- |
| `tone` | `UiTone`，默认 `info` | 语义色 |
| `title` | `string?` | 可读标题 |
| `children` | `ReactNode` | 正文 |
| `action` | `{label,onClick,disabled?}?` | 明确恢复动作 |
| `onDismiss` | `() => void?` | 关闭按钮 |
| `dismissLabel` | `string`，默认“关闭提示” | 可访问名称 |
| `role` | `status \| alert?` | 默认 danger=alert，其余=status |

```tsx
<Notice
  tone="warning"
  title="占位模式不会推进步骤"
  action={{ label: '重新检查', onClick: refresh }}
>
  当前提交只到达本机占位服务，没有真实登录或业务结果。
</Notice>
```

约束：不要用 Notice 冒充成功；危险错误才用 assertive；同一错误不要在一个页面重复播报多次。

### 3.6 States：Loading / Empty / Unsupported / Error

源码：[`primitives/States.tsx`](../src/components/primitives/States.tsx)

状态：**已接入**（实时开始页 scaffold；当前 fixture 主路径尚未统一使用）

| 组件 | 必填 | 可用 props | 语义 |
| --- | --- | --- | --- |
| `LoadingState` | 无 | `title`（默认“正在加载”）、`description`、`className` | `role=status`、`aria-busy` |
| `EmptyState` | 无 | `title`（默认“暂无内容”）、`description`、`action`、`className` | neutral |
| `ErrorState` | `message` | `title`、`retryable`、`code`、`retryLabel`、`onRetry`、`className` | `role=alert` |

`AsyncView` 的状态映射为：loading / ready / empty / error。没有“不支持”状态组件：没有能力端口的控件或页面不渲染，被拒绝的调用在使用时发通知（见标准 §4.7）。`statusToTone()` 统一映射 tone，页面不要各自写一套。

```tsx
if (view.status === 'error') {
  return (
    <ErrorState
      message={view.message}
      code={view.code}
      retryable={view.retryable}
      onRetry={retry}
    />
  );
}
```

约束：只有 `retryable && onRetry` 才显示重试；empty 不是 error。

### 3.7 Dialog

源码：[`primitives/Dialog.tsx`](../src/components/primitives/Dialog.tsx)

状态：**可用未接入**（当前 App 使用旧的 `LogoutDialog`）

| Prop | 类型/默认 | 说明 |
| --- | --- | --- |
| `open` | `boolean` | 是否渲染 |
| `title` | `string` | 必填，用于 `aria-labelledby` |
| `description` | `string?` | 用于 `aria-describedby` |
| `children` | `ReactNode?` | 正文 |
| `actions` | `DialogAction[]`，默认 `[]` | 按钮配置：label/onClick/variant/disabled/loading |
| `onClose` | `() => void` | Escape/关闭/遮罩的关闭回调 |
| `closeOnEscape` | `boolean`，默认 `true` | Escape 是否关闭 |
| `closeOnBackdrop` | `boolean`，默认 `false` | 点击遮罩是否关闭 |
| `initialFocusRef` | `RefObject<HTMLElement \| null>` | 初始焦点 |
| `closeLabel` | `string`，默认“关闭对话框” | 关闭按钮名称 |
| `className` | `string?` | 附加类名 |

```tsx
<Dialog
  open={open}
  title="删除连接？"
  description="此操作需要真实服务确认。"
  actions={[
    { label: '取消', onClick: close },
    { label: '确认删除', onClick: confirm, variant: 'danger' },
  ]}
  onClose={close}
>
  <p>当前占位模式不会执行真实删除。</p>
</Dialog>
```

约束：焦点陷阱、Escape 和关闭后恢复焦点由组件负责；确认动作本身由调用方执行，组件不代替业务。需要二次确认的危险操作优先使用 Dialog。

### 3.8 ProgressBar

源码：[`primitives/Progress.tsx`](../src/components/primitives/Progress.tsx)

状态：**可用未接入**

| Prop | 类型/默认 | 说明 |
| --- | --- | --- |
| `value` | `number \| null` | `null`=未知，不画完成 |
| `max` | `number`，默认 100 | 非有限或 ≤0 时回退 100 |
| `label` | `string` | progressbar 可访问名称 |
| `showValue` | `boolean`，默认 `true` | 显示百分比/未知 |
| `className` | `string?` | 附加类名 |

```tsx
<ProgressBar label="同步进度" value={loaded ? 42 : null} />
```

约束：值会被裁剪到 0..max；`null` 的 `aria-valuenow` 不存在，`aria-valuetext` 为“未知”。业务不要用 `value={100}` 表示“未知”。

### 3.9 Stepper

源码：[`primitives/Stepper.tsx`](../src/components/primitives/Stepper.tsx)

状态：**已接入**（首次设置，固定展示五步）

| Prop | 类型/默认 | 说明 |
| --- | --- | --- |
| `steps` | `{id,label,status}[]` | status：pending/active/completed/failed/blocked |
| `currentId` | `string` | 当前步骤 id |
| `ariaLabel` | `string`，默认“步骤” | nav 名称 |
| `className` | `string?` | 附加类名 |

```tsx
<Stepper
  currentId="account"
  ariaLabel="首次设置步骤"
  steps={[
    { id: 'account', label: '账号登录', status: 'active' },
    { id: 'device', label: '设备初始化', status: 'pending' },
  ]}
/>
```

约束：Stepper 只展示；步骤推进必须由真实成功状态驱动。占位模式下不要因为点击按钮把步骤改成 completed。

### 3.10 DiagnosticBox

源码：[`primitives/DiagnosticBox.tsx`](../src/components/primitives/DiagnosticBox.tsx)

状态：**可用未接入**

| Prop | 类型/默认 | 说明 |
| --- | --- | --- |
| `code` | `string` | 安全、已脱敏诊断标识 |
| `copyText` | `string?` | 复制内容；调用方必须先脱敏，默认复制 code |
| `title` | `string`，默认“诊断信息” | 区域名称 |
| `description` | `string?` | 说明 |
| `className` | `string?` | 附加类名 |

```tsx
<DiagnosticBox
  code="GUI-DIAG-001"
  title="诊断信息"
  description="只包含可公开的安全编号。"
/>
```

约束：绝不能传原始异常栈、请求头、token、完整环境变量；复制失败要有可读状态。

### 3.10a Icon

用途：Phosphor 线性图标（默认 bold，选中/激活用 fill）。名字来自 `primitives/Icon.tsx` 内显式映射表，新增图标先加进映射表。

| Prop | 类型/默认 | 说明 |
| --- | --- | --- |
| `name` | `IconName` | 映射表中的图标名 |
| `size` | `sm \| md \| lg`，默认 `md` | 18/22/26px |
| `active` | `boolean` | 使用 fill 字重 |
| `label` | `string` | 有则 `role="img"` 加 `aria-label`，无则 `aria-hidden` |

### 3.10b Tooltip / InfoTip

用途：承接可选的补充解释。`<Tooltip content>{trigger}</Tooltip>` 包裹任意触发元素；`<InfoTip content label />` 渲染 22px 信息按钮。hover 与 focus 显示，Escape、blur、mouseleave 隐藏，触屏点击 InfoTip 切换；内容常驻 DOM（`hidden` 切换），触发元素以 `aria-describedby` 关联。

何时使用：只放可选的补充解释。错误、状态、操作结果以及完成操作所必需的信息必须直接显示在页面上，不放入 Tooltip。

### 3.11 类型与工具

源码：[`primitives/types.ts`](../src/components/primitives/types.ts)


| 导出 | 用途 |
| --- | --- |
| `UiTone` | `neutral \| info \| positive \| warning \| danger` |
| `AsyncView<T>` | loading / ready / empty / error 的可辨识联合 |
| `UiAction` | `{ label, onClick, disabled? }` |
| `statusToTone` | 统一状态到 tone 映射 |
| `cx` | 组合类名的小工具 |

`AsyncView` 的 `ready` 必须有数据；不要用 `ready + 空数组` 代替 empty 的业务语义。

## 4. 共享外壳与辅助组件

凭据业务共享组件：`features/agent-provider` 公开 `CredentialList` / `CredentialListItem`，
用于全局及 Agent 配置页的 Key、Auth 清单。统一列表边框、标题、摘要、展开按钮和详情网格；
条目接受 `title`、`summary`、详情 children，以及可选受控 `expanded` / `onExpandedChange` 或
`defaultExpanded`。交互复用 primitive Button；样式仅消费 `--ui-*` token。
`NativeAuthList` 负责读取 Auth 元数据和可选移除动作，不在列表布局组件内派发业务请求。
全局和 Agent 配置页的 Key/Auth 空清单统一使用 primitive `EmptyState`，不混用普通段落；
缺少读取能力或读取失败不作为空清单展示。

这些组件当前服务 App 或页面，但多数仍是旧样式体系，不属于 `.ui-*` primitives。新页面可复用其已稳定的行为，但视觉迁移需按任务处理。

### 4.1 Rail / RailFrame

- [`Rail.tsx`](../src/components/Rail.tsx)：把页面枚举映射到导航并渲染账号菜单。
- [`RailFrame.tsx`](../src/components/RailFrame.tsx)：`aside.rail`、HYP 图形标（`.brand` 带 `role="img"` 与 `aria-label="Hyprial OS"`，内含深色底与浅色底两个 `img`，由 CSS 择一显示）、分组导航、footer；`active`、`onSelect`、`footer`、`showOrg`。不再有文字字标。

当前导航分组：开始；工作（聊天、任务）；查找（Agent、组织、设备）。设置与连接在账号菜单里。`showOrg=false` 可隐藏组织项。≥641px 是 72px 黑色图标栏（IM 式）：顶部图形标，中间 56px 方块导航项（选中为青柠底），底部通知铃铛与账号头像，账号菜单与通知面板从侧栏右侧弹出；≤640px 是顶栏（图形标 + 通知 + 账号）加底部标签栏。样式类为 `.rail`、`.brand-mark`、`.nav-item`、`.rail-foot`，颜色只用 `--ui-rail*`。

使用约束：`RailFrame` 是导航外壳，不负责权限；页面是否真的可访问由宿主/适配器决定。新增导航项应同时检查键盘、`aria-current`、56px 宽度下的文字长度和路由映射。

### 4.2 AccountMenu

源码：[`AccountMenu.tsx`](../src/components/AccountMenu.tsx)

状态：**已接入**

Props：`open`、`onOpenChange`、`onNavigateConnections`、`onLogout`。

已实现：`aria-haspopup="menu"`、`aria-expanded`、菜单 `role`、方向键、Home/End、Escape、外部点击关闭、打开后聚焦第一项。菜单项为设置、连接、登出（没有帮助能力，不渲染帮助项）；登出只打开演示登出对话框，不执行真实登出。

### 4.3 Badge / AvailabilityBadge / ConnectionStatusBadge

源码：[`Badge.tsx`](../src/components/Badge.tsx)

状态：**已接入**（聊天、联系人、连接页、实时开始 scaffold）

- `Badge`：`children: string`，`tone: positive | warning | muted | neutral`。
- `AvailabilityBadge`：`availability: available | needs-connection | disabled`，自动映射文字和 tone。
- `ConnectionStatusBadge`：`status: connected | unconfigured | disabled`，自动映射文字和 tone。

约束：Badge 是文本状态，不是可点击控件；不要用颜色代替文字。连接“已配置”不等于“在线”。

### 4.4 AgentAvatar

源码：[`AgentAvatar.tsx`](../src/components/AgentAvatar.tsx)

状态：**已接入**

Props：`name`、`tone: green | lilac | peach | grey`、`size?: sm | md | lg`。

当前为首字符头像，`aria-hidden="true"`；名称由相邻文本承载。若头像成为唯一标识，必须提供可读名称。

### 4.5 NoticeBanner

源码：[`NoticeBanner.tsx`](../src/components/NoticeBanner.tsx)

状态：**已接入**（AppShell）

Props：`text`、`onDismiss`。当前是旧样式 `.notice`，`role="status"`、`aria-live="polite"`，有关闭按钮。

新页面如需带 tone/动作/标题的通知，优先使用 `Notice` primitive；旧应用壳迁移时再统一。

### 4.6 LogoutDialog

源码：[`LogoutDialog.tsx`](../src/components/LogoutDialog.tsx)

状态：**已接入**（AppShell）

持有 `dispatch`，渲染模态登出确认；当前只清空演示会话并显示“真实登出未执行”。

已实现：Escape、Tab 焦点陷阱、初始焦点、关闭后恢复焦点、遮罩点击关闭。新代码优先使用 `Dialog` primitive 以获得统一 token 和可配置 actions；迁移时保持相同键盘行为。

### 4.7 AccountIcon

源码：[`Icon.tsx`](../src/components/Icon.tsx)


Props：`name: connection | help | logout`。SVG 为装饰性，`aria-hidden`、`focusable=false`；文字标签由父级提供。

### 4.8 ToastHost / NotificationCenter（统一通知）

源码：[`ToastHost.tsx`](../src/components/ToastHost.tsx)、[`NotificationCenter.tsx`](../src/components/NotificationCenter.tsx)

状态：**已接入**（AppShell）

生产者统一派发 `dispatch({ type: 'push-notice', tone, text, source? })`，`tone` 为 `info | success | warning | error`。
`ToastHost` 最多叠放最近 4 条（同 `text` + `source` 去重）；`info` / `success` 4 秒后自动消失，`warning` / `error`
停留到用户关闭，并由 reducer 同步写入通知中心历史（最多 20 条，计未读数）。`NotificationCenter` 目前只镜像本地结果，事件流尚未接入。

一次性操作结果走这里，不要写成页面文字；哪些反馈应留在页面见
[设计与研发规范 §4.6](ui-development-standard.zh-CN.md)。

功能端：[`notifications.tsx`](../src/components/notifications.tsx) 提供 `NotificationsProvider`（由共享
`AppShell` 挂载，首次设置框架与常规框架都渲染 `ToastHost`）、`useNotify()`（`{ tone, text, source?, autoDismiss? }`）和
`useNotifyUnavailable()`（`(subject, { code?, reason?, source? })`）。后者发 warning toast（带 `autoDismiss`，同其他 toast 时长后离开，条目留在通知中心），文案固定为
`${subject}暂不可用。`；`reason` 不进文案，`source · code` 作为通知中心条目的来源。没有 provider 时调用会抛错。
toast 层级 `--ui-z-toast`（1100）高于对话框，对话框内触发的 toast 可见；开发工具层（仅 dev）在其上，球不会被 toast 盖住。规则见
[设计与研发规范 §4.7](ui-development-standard.zh-CN.md#unavailable)；测试用
`tests/support/notify-harness.tsx`。

## 5. 页面覆盖与当前状态

预览和正式入口共用页面，差异在数据/能力适配层。完整规则见 [共享 GUI 入口](shared-ui-entry.zh-CN.md)。

| 页面 | 共享展示入口 | 数据与行为边界 |
| --- | --- | --- |
| 开始 | [`pages/StartPage.tsx`](../src/pages/StartPage.tsx) | 保持现有共享页面；未知状态不显示虚构计数 |
| 聊天 | [`features/agent-chat/impl/AgentChatSection.tsx`](../src/features/agent-chat/impl/AgentChatSection.tsx) | `ChatPage`/`LiveChatPage` 分别适配模拟会话与真实控制器；展示层一致，草稿、结果未知、失败重试分开。正式组合只在目录可调用 `targets` 与至少一个消息读取方法时下发聊天端口，否则没有聊天导航项、路由回到开始页；`message.send`、历史读取被拒时控件保持可用，只发通知，不留页面文字；启动期读取被拒不写成“暂无”，目录被拒的对象不声称有无会话（`unread`） |
| Agent | [`pages/ContactsPage.tsx`](../src/pages/ContactsPage.tsx) | 列表、搜索、管理共用；正式能力不可用时禁用创建，数量未知 |
| 组织 | [`features/org/impl/workspace/OrgWorkspace.tsx`](../src/features/org/impl/workspace/OrgWorkspace.tsx) | 通过数据源适配，统一八个子页签；不再切换旧组织页面 |
| Agent 设置 | [`pages/ConnectionsPage.tsx`](../src/pages/ConnectionsPage.tsx) | 凭据与平台接入两个模块；没有对应能力端口的模块不渲染；两个端口都没有时本页、账号菜单的「Agent 设置」项和开始页的「查看 Agent 设置」都不渲染 |
| 设置 | [`pages/SettingsPage.tsx`](../src/pages/SettingsPage.tsx) | 账号、模型、本机能力由能力提供者决定，不能以渲染表单代替接线验收 |
| 首次设置 | [`pages/FirstRunPage.tsx`](../src/pages/FirstRunPage.tsx) | 账号与设备、模型服务、Squire、首次对话四步；预览与正式使用同一组件 |

应用壳：[`app/AppShell.tsx`](../src/app/AppShell.tsx) 统一路由、导航和通知。`live` 容器提供真实状态与操作，不构成第二套页面。

### 5.1 当前边界

- 仍有旧 token/类名，不宣称全站完成样式迁移。
- 已有真实账号和聊天控制器不等于所有后端能力已接通；安装、真实凭据、真实消息收发分别验收。
- 桌面打包、移动正式认证、更新器属于其他任务；网页检查不替代原生 WebView/真机验收。

## 6. 非视觉模块索引

| 模块 | 源码 | 职责 |
| --- | --- | --- |
| 展示状态 | [`state/types.ts`](../src/state/types.ts) | AppState、AppAction、聊天焦点意图 |
| Reducer | [`state/appReducer.ts`](../src/state/appReducer.ts) | fixture 初始化、导航、草稿、发送、通知、登出演示 |
| 选择器 | [`state/selectors.ts`](../src/state/selectors.ts) | 活跃目标、线程、搜索过滤、状态文案 |
| 视图模型 | [`state/gui-view-model.ts`](../src/state/gui-view-model.ts) | 页面、目标、消息、连接展示类型 |
| 消息合并 | [`state/messages.ts`](../src/state/messages.ts) | 按权威 id 去重、重连快照合并、local 占位替换 |
| 操作生命周期 | [`state/operation-lifecycle.ts`](../src/state/operation-lifecycle.ts) | not-sent/in-flight/success/definitive-failure/unknown；unknown 不能盲目重发 |

这些模块是展示/适配层，不是正式协议。正式 HTTP、认证、事件和错误码由契约 owner 定义。

## 7. 新增组件配方

### 7.1 新增页面局部组件

1. 在对应 `pages/` 目录定义 props 和展示。
2. 复用现有 primitive 处理按钮、字段、状态、对话框。
3. 不直接请求网络；把动作作为回调向上传递。
4. 为关键状态和键盘路径加测试。
5. 若只服务一个页面，不放进 `components/`。

### 7.2 新增共享组件

1. 确认至少两个页面有相同语义，或组件承载统一的无障碍/状态行为。
2. 在 `components/` 下定义稳定、受控的 props。
3. 使用现有 token；如新增视觉值，先扩展 token。
4. 增加测试并更新本目录。
5. PR 说明旧调用方迁移方式和视觉差异。

### 7.3 新增 primitive

1. 先证明现有 primitive 不足，而不是单纯“想要一个新样式”。
2. 设计 props、状态矩阵、键盘/ARIA 行为、错误和 unknown 边界。
3. 使用 `.ui-*` 和 `--ui-*`，不覆盖旧选择器。
4. 在 `primitives/index.ts` 导出；补 `primitives.test.tsx` 和必要的 token 契约断言。
5. 更新主规范、组件目录和受影响页面；不要删除既有契约测试来迁就实现。

### 7.4 修改响应式断点

1. 先确认目标产品行为和受影响页面。
2. 同时更新 CSS、`tests/responsive-contract.test.ts`（若属于现行契约）和文档。
3. 用真实浏览器检查桌面、窄桌面、手机宽度、长文本和错误态。
4. 保持 `min-width: 0`、`100dvh`、安全区和 reduced-motion 保护；不得重新引入 980px 最小宽度。

## 8. 源码与测试入口

- Primitive 入口：[`src/components/primitives/index.ts`](../src/components/primitives/index.ts)
- Token：[`src/styles/tokens.css`](../src/styles/tokens.css)
- Primitive 样式：[`src/styles/primitives.css`](../src/styles/primitives.css)
- 组件测试：[`tests/primitives.test.tsx`](../tests/primitives.test.tsx)
- Token 契约：[`tests/token-contract.test.ts`](../tests/token-contract.test.ts)
- 响应式契约：[`tests/responsive-contract.test.ts`](../tests/responsive-contract.test.ts)
- 页面测试：[`tests/app-shell.test.tsx`](../tests/app-shell.test.tsx)、[`tests/chat.test.tsx`](../tests/chat.test.tsx)、[`tests/settings-first-run.test.tsx`](../tests/settings-first-run.test.tsx)、[`tests/start-page-view.test.tsx`](../tests/start-page-view.test.tsx)
- 开发流程：[开发模式操作手册](dev-mode-user-guide.zh-CN.md)
- 交接格式：[UI 变更交接模板](ui-change-handoff-template.zh-CN.md)

## 9. 业务参考样例

Agent 设置的凭据与平台接入共用 `features/connections` 的 `CredentialsSection`、
`PlatformIntegrationsSection`、`PlatformIntegrationDetail`，使用现有 `ModuleSection`、`Dialog`、
`Field`、`Select`、`Button`。布局样式归 `features/connections/impl/styles/agent-settings.css`。
`SecretField layout="inline"` 的布局/显隐按钮样式归共享 `components/forms/forms.css`，由组件导入，
新手引导与设置页均消费同一实现，不再依赖新手引导的父级 CSS。
凭据模块不创建自定义服务地址，已保存记录的服务地址与协议只读展示。没有对应能力端口的模块、
测试按钮、删除接入等控件不渲染；正式组合只在方法目录准入 `user.provider.list`、`adapter.list`
时下发对应端口；被拒绝的操作在用户操作时通知，主题为被操作控件的可见文案，页面不写说明。
正式能力与操作旅程见统一[方法对账](../../docs/design/gui-v0.1-method-plan-2026-11-13.md)。

开发工具的 `src/dev/archive/ArchivedScenarios.tsx` 复用历史 selector，位于浮动面板的“归档场景”页签。
它仅维护选择草稿，并通过新标签页的既有场景 URL 注入同一个 `App`；不复制页面、不修改当前预览 store、不进入正式产物。
页签支持方向键、Home/End，保留 Escape 关闭面板与焦点返回。

| 组件 | 位置 | 方法与状态 |
| --- | --- | --- |
| AccountStatusField / AccountLoginSection | `src/features/account/impl/AccountLoginSection.tsx` | 现有 account status/login/cancel 能力；复用首次设置 action port |
| AgentTargetField | `src/features/agent-chat/impl/AgentTargetField.tsx` | 宿主 `targets.list`（daemon `hosts` / `targets`）；受控选择与目录状态 |
| AgentHistoryField | `src/features/agent-chat/impl/AgentHistoryField.tsx` | 宿主 `sessions.history`（daemon `message.history`）；只读历史与陈旧状态 |
| AgentChatSection | `src/features/agent-chat/impl/AgentChatSection.tsx` | 组合目标、历史、`ChatComposer`；宿主 `sessions.send` → daemon `message.send` |

The API now consumes the MethodSpec-generated client. Explicit host configuration enables live H-01 reads; missing configuration remains unavailable. The four provider scenarios call the Contract Mock service, using its documented synthetic sentinel rather than empty JSON. The toolbar still has six selectors; existing first-run options and account scenarios are unchanged. Product live login still needs trusted host credential input, and H-01 refuses login/logout pending H-03. Grant inventory remains independent of native credential writes. Provider/model discovery and first-run model-service integration are not supplied by this slice.

dev-03 Agent 聊天沿用已有 `ChatController`，其宿主返回转换现集中于 `src/features/agent-chat/impl/api/chat-controller.ts`。区块只消费受控 props；选中目标、草稿、历史和未知发送的对账仍由该控制器持有，不创建另一份会话权威。`message.send` 的受理不等于 Agent 回复，真实回复由 `message.history` 读取确认。J3 原有场景是预览数据；正式宿主由显式 daemon driver 和可信 principal 启动。

Agent chat 契约样例（dev-03）：开发工具栏既有 `J3 场景` 保留原选项和默认 `baseline`，新增 `contract-success/empty/error/unconfigured/unknown/failure`；显式选择契约选项后通过 `src/dev/mock/agent-chat.ts` 和同一 `ChatController` 注入 `LiveChatPage` / `AgentChatSection`；覆盖成功、空、读取错误、未配置、未知结果查询及明确发送失败。J3 原有展示回归选项保持不变。见 [交接说明](agent-chat-reference-handoff-2026-10-31.md)。
