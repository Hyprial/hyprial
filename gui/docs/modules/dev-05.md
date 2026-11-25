# dev-05 机器与通信网络（J6／J7 切片）

本文件描述 J6 机器列表切片与 J7 拓扑、Inspect、Monitor 切片。J6 章节在前，J7 章节从“J7 拓扑、Inspect 与 Monitor”开始。

## 组件与模型

- `src/features/network/impl/machine-view-model.ts`：产品类型、连接文案、`buildHiddenMachineSectionModel()`。**不含开发种子**。
- `src/dev/machine-scenarios.ts`：M-14 合成机器、`buildMachineScenario(baseline|m14|m15)`，只由开发选择器与测试引用。
- `src/features/network/impl/OrgMachines.tsx`：纯展示组件，只读模型。
- `src/features/network/impl/machines.css`：局部样式，根类 `.org-machines`。

## 场景

开发预览默认渲染连续会话；本文件的机器/拓扑场景下拉只作定向回归注入。会话场景 `linked` 表示由会话统一提供的机器区。

| 场景 | 显示 | 禁止 |
| --- | --- | --- |
| `hidden` | 不渲染 | 第二条加入失败状态 |
| `m14` | 2 台合成机器；当前行标“这台电脑”；连接状态“在线” | 当前行移除按钮、真实端点 |
| `m15` | “还没有机器记录” | 空拓扑图、示例机器 |

## 文案

- 字段固定四个：主机名、系统、加入时间、连接状态。
- 连接状态只用“在线”“离线”。流程结果沿用 J5 的“失败”，被拒绝的动作只发通知。
- “这台电脑”是可见文本，不只靠颜色表达。
- 不提供任何机器移除操作。

## 集成接口

组织页按位置挂载机器区：成员区之后、拓扑预留区之前。J5 加入失败（M-13）时集成人传 `hidden`，机器区不渲染，由 J5 提供原因与重试。

集成人传入一个 `MachineSectionModel`：

- `scene`：`unavailable | hidden | m14 | m15`
- `machines`：`MachineRecord[]`，字段 `id`、`hostname`、`system`、`joinedAt`、`connection`、`current`
- `title`、`emptyText`、`unavailableNote`、`currentLabel`

J5 加入失败（M-13）时传 `hidden`，机器区不渲染，由 J5 提供原因与重试。

## 边界

- 无请求、无存储写入、不读系统信息、不生成设备身份。
- 正式默认停在 `unavailable`。
- J6 部分不做 J7 拓扑、Inspect、Monitor。J7 见下方章节。

### 集成硬门（已关闭）

M-14 合成种子 `studio-mac`、`travel-mac` 与系统名 `macOS 合成系统` 原本与产品 builder 同文件，接线后会被打进正式产物。集成时已按审查建议把它们移入 `src/dev/machine-scenarios.ts`，产品模块只留类型、文案与两个产品默认模型。

生产门禁 `scripts/verify-production-copy.mjs` 已加入 `studio-mac`、`travel-mac`、`合成系统` 三个禁止词，`tests/safe-r1-build-contract.mjs` 也逐词断言正式产物命中 0。

## 测试

`tests/modules/dev05/j6-machines.test.tsx`：模型 4 项、渲染 6 项，覆盖 M-14 两行、当前机无移除按钮、离线态、M-15 空态、hidden 不渲染、unavailable 无列表、无请求与无存储。

## J7 拓扑、Inspect 与 Monitor

### 组件与模型

- `src/features/network/impl/topology-view-model.ts`：类型、固定产品文案、`unavailable`、`hidden`、`unconfirmed` 三种产品态。不含合成成员、机器、边、Inspect 或 Monitor 样本。
- `src/features/network/impl/OrgTopology.tsx`：区块组件，持有当前视图、选中节点、选中边与面板状态。
- `src/features/network/impl/TopologyDiagram.tsx`：节点与通信边按钮、图例。节点与边都是原生 `button`。
- `src/features/network/impl/TopologyPanels.tsx`：Inspect 与 Monitor 面板。
- `src/features/network/impl/topology.css`：局部样式。根类 `.org-topology`，全部选择器限定在该根之下。

### 数据模式

| `dataMode` | 界面 | 禁止 |
| --- | --- | --- |
| `hidden` | 不渲染 | 第二条加入失败状态 |
| `structure-only` | 显示节点与图例；不渲染通信视图切换与 Monitor | 通信线、Inspect 入口 |
| `full` | 结构视图显示节点；通信视图显示 1 条可键盘选中的边；Monitor 显示静态演示 | 实时刷新、轮询、真实端点 |
| `empty` | 按传入节点显示，并显示“邀请成员后这里会出现更多节点” | 额外节点或假边 |
| `unconfirmed` | “状态未确认”和“重试”，不绘图 | 成功态、“刷新”“变化”字样、请求 |

### 交互与焦点

- 默认结构视图；视图按钮为 `data-topology-view="structure|communication"`，带 `aria-pressed`。
- 节点为 `button`，选择态用 `aria-pressed`；切换视图或开关 Monitor 后选中节点保留。
- 通信边为 `button`，Enter 与 Space 均可打开 Inspect；Inspect 只显示发起方、接收方、状态、时间。
- 面板打开后焦点进入面板标题；关闭按钮或 Escape 关闭，焦点回到通信边或 Monitor 按钮。
- Monitor 面板标“本地静态演示”“不是实时数据”，无计时器、轮询、动画与请求。
- 选择器固定：`data-topology-view`、`data-topology-node`、`data-topology-edge`、`data-topology-panel`。

### 布局

- 区块根类为 `.org-topology`，J7 的样式选择器都写在 `.org-topology` 之下。图示放在 `.org-topology .topology-diagram-scroll` 内，模块内横向滚动；页面根不横向滚动。
- 面板 `max-height` 与内部滚动，375×320 下关闭按钮仍可达。
- 760 与 1440 下完整可见，不依赖横向滚动。

### 边界与限制

- 无网络请求、无浏览器存储、无系统探测、无 canvas、无新增依赖。
- 合成节点、边、Inspect 样本与 Monitor 样本由原 Developer 的 `dev/topology-scenarios.ts` 提供，不进入产品模块。
- 组件级测试覆盖模型、渲染与交互；真实视口与遮挡由集成后的浏览器合同验证。

### J7 测试

`tests/modules/dev05/j7-topology.test.tsx`：模型 3 项、渲染 6 项、交互与样式 8 项，共 17 项，覆盖六种 `dataMode`、M-16 无通信线、M-17 键盘打开 Inspect、Monitor 静态标识、选中保留、焦点返回、失败态重试零请求与零存储、CSS 根类作用域。
