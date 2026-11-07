# GUI dev-01 首次使用设计基线（2026-10-28）

状态：任务 A 设计盘点与基线固定。本文只记录设计结论和实现边界，不修改产品代码，不代表真实账号、模型、Squire 或聊天已经接通。

## 1. 基线与依据

| 项 | 值 |
| --- | --- |
| 代码/设计冻结基线 | `e703c6690b3bbcd16f558cb7293503d3c4208bb2` |
| 基线 tree | `fead2c360f38c692db13aaae0def48b3ce229b6e` |
| 工作树 | `/home/chloemartin/HyprialOS/.worktrees/gui-dev01-onboarding-20261028` |
| 分支 | `feat/gui-dev01-onboarding-20261028` |
| 同头引用 | `feat/gui-dev-01`、`intg/gui-init`、`design/gui-user-journeys` |
| 集成目标 | `intg/gui-init`，唯一集成人 `work-developer` |

已读取的本地来源：

- 根 `<MEMBER>-GUI-WORKFLOW.md` 最新覆盖，包括 `GUI-DEV01-20261028`、`GUI-CONTRACT-CANCEL-20261028` 和 `PAUSE-SCOPE-CLARIFICATION-20261027`。
- 本工作树 `AGENTS.md`。
- `docs/design/gui-init-tracks.md`，尤其第 4 节 dev-01 登记。
- `gui-fix-records/2026-10-28-gui-dev01/PLAN.md` 及其任务 A/B/C 边界。
- 首次使用相关现有实现、测试、primitives、样式文件和操作手册，见第 2 节。

未读取、也不得声称已读取的材料：

- `module-boundaries-r3.md`
- `product-handoff-r3.md`，包括其第 9 节
- `shared-file-ownership-r1.md`

以上三份文件在冻结基线的 tree 中未找到。本文不补写或推测其内容，只能依据 track 登记、当前仓库实现和现有组件完成无歧义部分。缺失影响列于第 9、10 节。

产品视觉依据：本地 v17 HTML 不是当前最新设计，不纳入本基线，不作为实现或验收依据。`design/gui-user-journeys` 与基线同头，未提供额外用户旅程页面稿。

## 2. 当前实现盘点

### 2.1 当前首次使用页

`gui/src/pages/FirstRunPage.tsx` 当前是五步视觉：

| 顺序 | 当前步骤 | 当前行为 |
| --- | --- | --- |
| 1 | 账号登录 | 账号和访问令牌表单；调用占位 `account.login`；令牌提交后清空；没有成功状态 |
| 2 | 设备初始化 | 独立卡片；显示“未初始化 · 未接线”；调用占位 `device.initialize` |
| 3 | 模型服务 | 端点和模型密钥表单；调用占位 `model.configure`；密钥提交后清空；没有成功状态 |
| 4 | 创建并启动 Squire | 创建、启动两个按钮；显示“未知 · 未接线”；不创建进程 |
| 5 | 双向聊天 | 只有“查看聊天界面”，跳转到聊天页；不发送消息、不产生回复 |

实现事实：

- `STEPS` 固定为五步，`currentId` 固定为 `account`；后四步一直是 `pending`。
- 所有卡片同时可见，未来步骤没有锁定，当前也没有真实步骤推进。
- 页面顶部明确显示“首次设置暂不可用”，提交不推进步骤。
- 占位请求由 `useStubAction` 发起；占位后端返回不可用，开发者工具不会把调用写成真实成功。
- 当前没有“注册”入口。
- 当前模型配置只有 API Key 形状，没有账号授权形状，也没有额度、限制和费用说明。
- 当前聊天步骤不是首次对话流程，只是进入聊天页。

### 2.2 相关页面和共享边界

| 文件/组件 | 当前事实 | 对设计的影响 |
| --- | --- | --- |
| `gui/src/pages/SettingsPage.tsx` | 含账号、模型服务、Squire 创建/启动，以及“打开首次设置”按钮 | 与登记的设置页边界冲突；本次设计必须把它列为待迁移内容，不能把当前页面当作目标 |
| `gui/src/pages/ConnectionsPage.tsx` | 已有“模型服务”和“消息连接”两区，状态区明确未确认 | 模型服务的长期归属是连接页；模型配置私有组件应作为接入提案，不在本 track 直接改连接页 |
| `gui/src/pages/StartPage.tsx` | 仍是 fixture 风格开始页，没有首次使用槽位；创建 Agent 是占位动作 | 首次使用完成后的落点需要共享入口提案 |
| `gui/src/pages/start/StartPageView.tsx` | 已预留 `firstRun` 和 `sessionEntry` 两个容器托管槽位，但当前没有被页面消费 | 是未来外壳接入的可复用形状，不等于已接线，也不能在 dev-01 私有 track 直接接线 |
| `gui/src/state/gui-view-model.ts` | `GuiPage` 含 `first-run`；注释明确它是展示模型，不是 wire protocol 或正式契约 | 私有页面可以表达展示状态，不得据此新增 HTTP/daemon 协议 |
| `gui/src/state/types.ts`、`gui/src/state/appReducer.ts` | 没有首次使用步骤状态、完成状态或持久化状态；聊天发送只生成本地 `not-sent` 消息 | 步骤推进、完成落点、历史恢复需要共享状态提案；不能靠浏览器存储补齐 |
| `gui/src/adapters/stub.ts`、`gui/src/adapters/useStubAction.ts` | 只有占位操作入口和明确失败回执 | 当前 UI 操作名只是占位，不是产品接口契约 |
| `gui/src/components/primitives/Stepper.tsx` | 纯展示组件；支持 `pending/active/completed/failed/blocked`；调用方拥有步骤迁移 | 应由 dev-01 私有逻辑拥有步骤状态，不修改共享 primitive |
| `gui/src/components/primitives/Field.tsx` | 支持标签、提示、错误、必填和 aria 关联；不读取或存储值 | 可用于错误保持和重试；秘密生命周期仍在所属表单 |
| `gui/src/styles/workspace-pages.css` | 已含 `first-run-grid`、`first-run-card`、`settings-form`、`first-run-facts` 等响应式样式 | 属于全局样式，dev-01 不得修改；应优先复用现有 class |
| `gui/tests/settings-first-run.test.tsx` | 断言设置页存在模型表单，首次设置五步固定且失败不推进 | 现有共享测试与目标边界冲突，需集成人处理，不由本 track 改共享测试 |
| `gui/tests/app-shell.test.tsx` | 断言设置页有“模型服务”和“打开首次设置”，并进入五步首次设置 | 同上；这是共享测试更新项，不是 dev-01 私有测试可覆盖的 |

`gui/docs/dev-mode-user-guide.zh-CN.md` 明确：开发预览使用独立 Node 占位后端，页面示例数据不代表真实业务状态，业务请求返回 not implemented 时不得伪装成功。该说明与本文的未连接/状态未知/暂不可用状态一致。

## 3. 五步与登记四步的差异

| 当前五步 | 登记四步目标 | 差异与固定结论 |
| --- | --- | --- |
| 1 账号登录 | 1 账号 | 保留；登录和注册都是该步骤的入口，不各算一步 |
| 2 设备初始化 | 无独立步骤 | 降为账号步骤内的本机设备子状态；不进入 Stepper，不新增路由，不形成第五步 |
| 3 模型服务 | 2 模型服务 | 前移；必须等账号步骤完成后才可交互；包含账号授权和 API Key 两种方式 |
| 4 创建并启动 Squire | 3 Squire | 同样归并为 Squire 步骤；创建和启动是步骤内子状态，Squire 不可跳过 |
| 5 双向聊天 | 4 首次对话 | 改为“首次对话”步骤；dev-01 只提供引导和跨 track 交接，不实现 dev03 聊天 |
| 无 | 完成后进入“开始” | 首次对话真实成功后进入开始页；不能由按钮点击或本地状态冒充 |

固定后的顺序：

```text
账号（含本机设备身份）
  → 模型服务
  → Squire
  → 首次对话
  → 开始页
```

编号步骤只有四个。设备初始化、Squire 创建/启动、首次对话内的发送/重试都是子状态，不增加编号，也不允许通过视觉上的子卡片绕开步骤顺序。

## 4. 步骤状态与全局规则

### 4.1 状态含义

沿用 `Stepper` 已有的展示状态，不扩展共享 primitive：

| 状态 | 语义 |
| --- | --- |
| `pending` | 尚未到达；不可提交、不可跳过 |
| `active` | 当前步骤；可以输入或等待受支持操作 |
| `completed` | 仅在该步骤的完成条件已由宿主/受支持结果确认后使用 |
| `failed` | 当前步骤的操作失败；停留原步骤，保留可重试状态 |
| `blocked` | 缺少后端、能力或权限，无法继续；停留原步骤并显示明确原因 |

`active` 和 `blocked` 都可以是当前步骤。页面上的“未连接”“状态未确认”“暂不可用”是业务状态文案，不等于 `completed`。没有真实回执时不得显示“已完成”、已登录、已连接、已启动或已收到回复。

### 4.2 全局交互规则

1. 同一时刻只有一个可交互的编号步骤。未来步骤可以显示为锁定摘要，但不能提交、不能通过 Stepper 或链接直接跳入。
2. 失败或不可用停留在当前步骤；不得自动推进，也不得把未来步骤预先标成完成。
3. 重试保留非敏感输入和已选配置方式；密码、访问令牌、API Key 等秘密在提交、取消、组件卸载时从表单状态清除。秘密不得写入 `localStorage`、`sessionStorage`、URL、日志或测试证据。
4. 请求进行中禁用重复提交；操作返回不明时显示状态未确认，不重复发送有副作用的请求。
5. 重新进入首次使用或刷新页面时，从宿主已确认状态重新计算；本 track 不新增浏览器存储，也不以页面内存状态作为完成证据。
6. 开发预览只能使用明确标识的隔离演示状态。演示状态不得发真实业务请求，不得被描述为正式登录、真实模型连接、真实 Squire 或真实聊天。
7. 正式构建无后端时停在账号步骤，显示未连接/状态未知/暂不可用；未来步骤保持不可操作。

## 5. 四步的入口、完成条件和失败重试

### 5.1 步骤 1：账号（含本机设备身份）

**入口**

- 正式构建的首次使用入口进入本步骤；没有后端或账号能力未确认时仍停在账号。
- 同一编号步骤内提供登录和注册两种入口；注册成功仍以宿主确认的账号会话为准，不伪造“注册后已登录”。
- 账号步骤内部的第二块是本机设备身份，不是第二个编号步骤。

**输入与展示**

- 账号标识、认证凭据或受支持的登录/注册入口。
- 当前设备状态：未初始化、初始化中、已就绪、失败、状态未知/暂不可用。
- 若宿主持久化首次使用状态，页面只读取宿主结果，不写浏览器存储；该持久化能力当前未提供，属于共享提案。

**完成条件**

- 账号会话由宿主确认已认证；并且
- 本机设备身份由宿主确认已初始化/可用，且属于本应用实例，不冒用外部 CLI 的 profile、凭据或设备身份；并且
- 两项状态都有可识别回执，页面不再显示“未接线”。

仅有账号表单提交、仅有按钮点击、仅有本地状态，不得完成步骤。设备未就绪时账号步骤不完成。

**失败、重试与不可用**

| 情况 | 行为 |
| --- | --- |
| 登录/注册失败 | 留在账号步骤；账号输入保留，秘密清空；展示具体失败原因；允许重试 |
| 设备初始化失败 | 留在账号步骤；账号已认证结果保留；设备子状态为失败；允许单独重试设备 |
| 账号成功、设备状态未知 | 留在账号步骤；显示“设备状态未确认”；不得推进模型步骤 |
| 正式构建无后端 | 账号步骤 `blocked`，显示未连接/暂不可用；后续步骤不可操作 |
| 重复进入 | 由宿主状态判断；不因页面重新挂载自动显示成功 |

### 5.2 步骤 2：模型服务

**入口**

- 仅当步骤 1 完成且账号/设备状态可确认时进入。
- 模型服务步骤只在首次使用流程内配置；长期管理入口归连接页模型区。
- 同一编号步骤支持两种配置方式：账号授权和 API Key。切换方式时保留非敏感选择，清空不必要的秘密。

**输入与展示**

- 配置方式选择、服务端点或账号授权入口、必要的非敏感配置。
- 校验状态：未校验、校验中、已连接/可用、失败、状态未知、不可用。
- 额度、限制和费用只能展示受支持接口返回的真实说明；缺失时显示“未知/待服务确认”，不得编造数值、货币、免费额度或限制。
- 不使用 mock 成功状态作为真实连接结果。

**完成条件**

- 宿主/受支持服务确认所选模型服务可用于本实例；并且
- 返回可识别的已连接/可用状态；并且
- 若服务返回额度、限制或费用信息，状态和说明可展示；若未返回，明确标为未知但不伪造。

仅有端点/密钥填写、仅有“保存”按钮、仅收到 `accepted` 或 queued 语义，不得完成步骤。账号授权跳转本身也不等于已授权。

**失败、重试与不可用**

| 情况 | 行为 |
| --- | --- |
| 认证或校验失败 | 留在模型步骤；保留配置方式和端点等非敏感输入；清空密钥；显示失败；可重试 |
| 账号授权未完成/被取消 | 留在模型步骤；显示未授权；返回后允许重新发起或切换 API Key |
| 保存成功但服务未确认 | 显示状态未确认；不得完成步骤，不得显示“已连接” |
| 额度/费用信息缺失 | 单独显示未知，不阻断其他已确认的连接状态，但不得填充虚构值 |
| 正式构建无后端 | 模型步骤 `blocked`；显示暂不可用；不发起真实业务请求 |
| 开发演示 | 只能使用明确标记的隔离夹具；不得调用真实服务；演示完成不能作为正式验收 |

模型连接状态需要输出给共享选择器，供 dev-02 显示 Agent 的模型依赖；具体共享接口由集成人定义，本 track 不新增 wire/HTTP 契约。

### 5.3 步骤 3：Squire

**入口**

- 仅当步骤 2 完成且模型服务状态可确认时进入。
- Squire 不可跳过；不得提供“稍后创建”或直接进入首次对话的按钮。
- 步骤内可能同时有“创建”和“启动/确保运行”两个子状态；它们不拆成新步骤。

**输入与展示**

- 当前状态：未创建、创建中、已创建未启动、启动中、运行就绪、失败、状态未知/不可用。
- 已存在 Squire 时避免重复创建；只允许检查并启动/恢复。
- 不展示虚构 Agent、进程或回复；不假定页面按钮能启动真实进程。

**完成条件**

- 宿主确认本实例的 Squire 已存在；并且
- 宿主确认 Squire 已启动/运行就绪，可用于首次会话；并且
- 结果不是 accepted/queued 或仅本地按钮状态。

创建的接受回执不是完成条件；创建后未启动仍是步骤 3。

**失败、重试与不可用**

| 情况 | 行为 |
| --- | --- |
| 创建失败 | 留在步骤 3；不显示已创建；保留可重试入口 |
| 创建成功、启动失败 | 留在步骤 3；显示“已创建、未启动”；只重试启动，不重复创建 |
| 启动回执未确认 | 显示状态未确认；不得进入首次对话 |
| 已有 Squire 但状态未知 | 显示状态未确认；允许受支持的检查/启动，不伪造运行状态 |
| 正式构建无后端 | 步骤 3 `blocked`；停留在已完成模型步骤之后；显示暂不可用 |
| 不可跳过 | 不渲染“跳过”“稍后设置”以提高完成率；只能退出流程后重新进入 |

### 5.4 步骤 4：首次对话

**入口**

- 仅当步骤 3 完成且 Squire 运行就绪时进入。
- dev-01 只负责首次对话的引导、入口和完成去向的私有展示；不实现 dev03 聊天页、消息发送、回复、飞书或历史。

**输入与展示**

- 说明首次对话的对象和目的；提供进入授权会话的入口。
- 在聊天能力未接入时显示“暂不可用/等待聊天能力接入”，入口不可伪装成功。
- 可由容器注入聊天槽位或状态；私有页面不新增共享聊天协议。
- 不展示示例回复、模拟 Agent 或伪造的双向消息。

**完成条件**

- 用户在共享聊天页面发送首条真实消息，且收到 Squire 的真实回复；并且
- 完成信号来自共享聊天状态/宿主回执，不是“已查看聊天界面”或“已点击进入”；并且
- 首次使用状态被宿主标记完成，随后进入开始页。

在 dev-03 和共享接线未就绪时，本步骤只能到达引导状态，完成条件和“进入开始页”均为 BLOCKED/UNKNOWN，不能由 dev-01 私有页面自行模拟完成。

**失败、重试与不可用**

| 情况 | 行为 |
| --- | --- |
| 进入聊天失败 | 留在步骤 4；显示具体失败；允许重试进入 |
| 发送失败 | 由 dev03 聊天组件保留草稿并给出失败/重试；首次使用不得标记完成 |
| 发送结果不明 | 显示状态未确认；先核对原请求，不盲目重发 |
| 未收到真实回复 | 仍在步骤 4；不得用“等待中”冒充成功 |
| 聊天能力缺失/无后端 | 步骤 4 `blocked`；显示暂不可用；完成去向不可达 |
| 成功后进入开始页 | 只在真实完成信号后导航；开始页入口由共享外壳提案，不在 dev-01 直接修改共享状态 |

**交接边界**

- 聊天发送、回复、草稿保留、失败重试和历史：dev-03。
- 首次使用完成信号、导航到开始页、共享状态：`work-developer` 集成。
- dev-01 只消费/展示上述结果；不新增 dev03 页面，不伪造回复。

## 6. 设备初始化如何附属而不新增一步

设备初始化固定为账号步骤内的子状态：

```text
步骤 1 账号
├── 1a 账号登录 / 注册
└── 1b 本机设备身份
       未初始化 → 初始化中 → 已就绪
                     └── 失败 / 状态未知 / 暂不可用
```

规则：

1. Stepper 仍只有四项；“本机设备”只在步骤 1 卡片内显示，不出现在编号步骤列表。
2. 不新增 `device` 路由或独立页面；不通过“下一步”单独推进设备。
3. 设备子状态有自己的状态文案和重试按钮，但步骤 1 只有在账号和设备都确认后才完成。
4. 设备已就绪时重新进入不重复初始化；状态未知时只显示未知，不自动当作完成。
5. 设备失败不回退已确认的账号输入/结果；不得把账号失败与设备失败混成一个错误。
6. 设置页只显示“当前设备”状态，不承担首次使用初始化流程；设置页边界仍按第 7 节。
7. 设备身份必须属于本应用实例，遵守 ADR-GUI-003/006 的独立节点边界，不读取外部 CLI 的 profile、凭据或私钥。
8. 无后端时不生成假设备 ID；正式构建停在账号步骤。
9. 设备就绪状态需要输出给 dev-04 的组织加入流程和设置页当前设备区；具体共享状态接口由集成人定义。

## 7. 设置页与连接页边界

登记目标与当前实现的差异必须先固定，不能在 dev-01 实现时继续沿用当前页面：

| 页面 | 目标 | 当前冲突 | 处置 |
| --- | --- | --- | --- |
| 设置 | 账号、当前设备、普通偏好 | 还有模型服务表单、Squire 创建/启动、“打开首次设置” | 迁移模型/Squire/首次设置入口；设置页属于 dev-01 私有页面，但是否保留入口/路由跳转需共享提案 |
| 连接 | 模型服务和消息连接 | 已有模型/消息两区，符合长期归属 | 不在 dev-01 直接修改；模型私有组件由任务 B 输出接入提案给集成人 |
| 首次使用 | 账号→模型→Squire→首次对话 | 当前五步且由设置页进入 | dev-01 私有页面改为四步；入口和完成导航由共享外壳接入 |

“设置页只保留账号、当前设备和普通偏好”不等于设置页实现全部偏好功能；本批不新增无设计的偏好项。模型服务表单的私有组件可以复用，但连接页的最终接入由集成人完成。

## 8. 视觉与组件来源

### 8.1 可用来源

1. `docs/design/gui-init-tracks.md`：步骤、完成条件、失败/重试、设置页排除项和验收口径的功能依据。
2. `gui/src/pages/FirstRunPage.tsx`：现有布局骨架、表单密度、错误提示和未接线文案的参考；不是最终设计。
3. `gui/src/components/primitives/`：
   - `PageHeader`、`Stepper`、`Card`、`StatusCard` 用于标题、步骤和状态块。
   - `Field`、`Button`、`Notice` 用于表单、错误、提交和不可用提示。
   - `EmptyState`、`ErrorState`、`UnsupportedState`、`DiagnosticBox`、`ProgressBar` 用于空、错误、缺失能力和等待状态。
   - `Stepper` 是展示组件，步骤迁移必须由 dev-01 私有逻辑管理。
4. `gui/src/pages/ConnectionsPage.tsx`：模型服务状态卡、连接行和边界说明的既有模式；可参考，不在 dev-01 修改。
5. `gui/src/pages/start/StartPageView.tsx`：`firstRun` 和 `sessionEntry` 槽位是未来外壳接入形状；当前未消费，不能当成已接线。
6. `gui/src/styles/tokens.css`、`gui/src/styles/primitives.css`、`gui/src/styles/workspace-pages.css`：现有 tokens、primitive 样式和 `first-run-*`/`settings-*` 响应式样式。均为全局文件，dev-01 不得修改。
7. `gui/docs/dev-mode-user-guide.zh-CN.md`：开发预览、占位后端和 not implemented 的用户说明。

### 8.2 视觉实现规则

- 只使用现有 primitives 和 tokens；优先复用 `page-first-run`、`first-run-grid`、`first-run-card`、`first-run-facts`、`settings-form` 等现有 class。
- 不新增依赖，不修改 `gui/package.json`，不修改全局 CSS，不把页面直接包成 HTML。
- 新业务组件放在 dev-01 私有目录；如需私有样式，只能新增并仅在私有组件导入的局部样式，提交时列出；需要共享 token/全局样式变更时先向 `work-developer` 提案。
- 375、760、1440 三档必须无根横向溢出；窄屏使用现有单列布局；按钮、输入、错误提示和状态卡要可键盘操作，失败重试后焦点留在当前步骤。
- 不把“未连接”“状态未确认”“暂不可用”替换成绿色成功徽标；`completed` 只在宿主持久化/真实回执确认后出现。

## 9. 私有文件清单与共享文件边界

### 9.1 本任务 A

只新增/修改：

- `docs/design/gui-dev01-onboarding-plan-2026-10-28.md`（本文）

不改产品代码、测试、共享文件、全局样式或任何后端/协议文件。

### 9.2 任务 B（local-linux-worker 持有，dev-01 私有）

- `gui/src/pages/SettingsPage.tsx`
- `gui/src/features/dev01/forms/`（新增）
- `gui/src/features/dev01/model/`（新增）
- `gui/tests/dev01-settings*`（专属测试）
- `gui/tests/dev01-model*`（专属测试）
- `docs/design/gui-dev01-settings-2026-10-28.md`

### 9.3 任务 C（本工作树后续，dev-01 私有）

- `gui/src/pages/FirstRunPage.tsx`
- `gui/src/features/dev01/onboarding/`（新增）
- `gui/tests/dev01-onboarding*`（专属测试）
- `docs/design/gui-dev01-onboarding-plan-2026-10-28.md`（本文的实现记录更新）
- 任务 B 已提交的表单组件只读复用；不修改 B 的分支或测试。

### 9.4 禁止直接修改的共享/越权文件

- 共享冲突热点：`gui/src/App.tsx`、`gui/src/app/AppShell.tsx`、`gui/package.json`、`gui/src/dev-mode/dev-module-index.css`
- 共享入口、外壳、全局状态、适配器、运行时、共享测试、构建配置、全局样式（`gui/src/styles/*`）
- `src/hyprial/`、`contract/`、`.forgejo/`、根 `scripts/`
- 现有共享测试：`gui/tests/settings-first-run.test.tsx`、`gui/tests/app-shell.test.tsx` 等；本 track 不直接修改

### 9.5 需要集成人处理的共享提案

| 提案 | 原因 | 接收方 |
| --- | --- | --- |
| 首次使用完成状态与持久化 | 当前 `AppState` 无首次使用状态；不能写浏览器存储 | `work-developer` |
| 首次使用入口门控 | 当前只能从设置页进入；设置页又要移除“打开首次设置” | `work-developer` |
| 完成后导航到开始页 | 需要共享路由/外壳；首次对话完成信号来自 dev03 | `work-developer` + dev03 |
| 模型连接状态选择器 | dev-02 需要读取；不能由 dev-01 私有写死 | `work-developer` |
| 设备状态选择器 | 设置当前设备、dev-04 加入流程需要 | `work-developer` |
| 现有共享测试更新 | 现有测试断言旧设置页和五步首次设置 | `work-developer` |
| 共享样式/token 缺口 | 现有全局样式不得由 dev-01 修改 | `work-developer` |

## 10. 跨 track 交接

| 对象 | 交接内容 | 当前状态/边界 |
| --- | --- | --- |
| `work-developer`（集成人） | 共享入口、完成状态、路由、共享测试、模型/设备状态选择器、样式提案 | 设计基线已明确；未实现共享接线 |
| dev-02（Agent 管理） | 模型服务连接状态、模型选择依赖；不能写死或推断 | dev-01 只输出设计/状态语义；无真实状态 |
| dev-03（聊天、消息、飞书） | 首次对话发送、真实回复、失败重试、草稿保留、历史；完成信号 | 本任务不实现聊天；dev03 未提供共享完成信号 |
| dev-04（组织、邀请、成员） | 设备身份/当前设备状态；无组织不阻塞首次本地使用 | 只交接语义；真实设备状态待宿主能力 |
| 用户旅程（陈瑞华） | 首次使用入口、四步、失败/重试、完成去向的页面稿 | 基线同头，未发现额外页面稿；本地 v17 不算 |
| 任务 B（local-linux-worker） | 设置页、账号/设备表单、模型私有组件、提交 SHA | 尚未派单；本任务只列边界 |
| 独立验收（Mini/mini-ci-worker） | 组合 head 复验、375/760/1440、键盘焦点、0 业务请求、0 浏览器存储 | 尚未开始，不把本文当验收 |
| operator | 缺失交接文档、登记旧口径修正、集成人路由 | 本文记录，未自行联系 HQ/其他团队 |

## 11. 缺失原型与缺失材料清单

以下材料在冻结基线或本工作树中未找到，不得虚构：

1. `module-boundaries-r3.md`
2. `product-handoff-r3.md` 及第 9 节
3. `shared-file-ownership-r1.md`
4. 账号/注册入口的最终页面稿和交互稿
5. 模型服务“账号授权”和“API Key”两种方式的最终视觉稿
6. 模型额度、限制和费用的展示稿与真实数据来源
7. 本机设备身份子卡片的最终视觉稿和状态稿
8. Squire 创建/启动/运行状态的设计稿
9. 首次对话引导和成功进入开始页的流程稿
10. 失败、重试、状态未知、暂不可用、无后端的逐页状态稿
11. 375/760/1440 的移动/窄屏视觉稿
12. “前两封邮件”对应的用户旅程页面材料

无歧义部分按现有 primitives、当前首次使用页结构和 track 登记先实现；上述缺失只阻塞视觉确认或缺失接口，不阻塞本设计基线和现有的四步状态定义。开发预览和正式无后端仍必须使用明确的非成功状态。

## 12. 登记中的旧口径，必须修正

`docs/design/gui-init-tracks.md` 仍保留“后端标准由黄佳佳提供”一类的过期措辞：

- 第 63 行：“前后端标准由黄佳佳确认并在可用后同步。”
- 第 205 行：“可按黄佳佳提供的前后端标准接线；标准未就绪时显示暂不可用。”
- 第 253 行：“任一步未通过时，等待黄佳佳提供正在验证的前后端标准。”

该口径与当前 `GUI-CONTRACT-CANCEL-20261028` 冲突：后端契约任务已取消，#2043 不是团队权威，本侧不新增 HTTP/daemon 协议，前端不应等待该“标准”。

修正建议（由 operator 交集成人 `work-developer` 处理，本任务不直接改登记文件）：

- 删除“等待黄佳佳提供/确认前后端标准”的依赖描述。
- 改为：前端按已确认的产品/组件和现有受支持能力实现；无真实后端时显示未连接、状态未知或暂不可用；接口缺口由对应 owner 和集成人按当前仓库能力处理。
- 不在本 track 生成新的后端契约、候选 HTTP 协议或 #2043 依赖。

## 13. 本设计基线的验收与未测项

本任务可验证：

- 文档固定四步顺序和设备附属方式。
- 四步均有入口、完成条件、失败/重试和不可用行为。
- 首次对话明确为引导/交接，不含 dev03 实现。
- 设置页/连接页边界和现有共享测试冲突已列出。
- 缺失材料、共享提案和旧口径修正已列出。
- 无产品代码、构建产物、存储写入或业务请求变更。

未测项（必须如实保留）：

- 真实账号登录/注册：无后端，NOT_RUN/BLOCKED。
- 真实模型授权/API Key 校验/额度费用：无后端，NOT_RUN/BLOCKED。
- 真实 Squire 创建/启动：无后端，NOT_RUN/BLOCKED。
- 真实首次对话和进入开始页：依赖 dev03/共享接线，NOT_RUN/BLOCKED。
- 375/760/1440 浏览器视觉、键盘焦点和 0 业务请求：本任务无产品代码，NOT_RUN；由任务 C 验收。
- 现有共享测试的更新：不在本 track 修改，交集成人。

正式构建不能因本文存在而主张首次使用已完成；开发演示也不能替代上述真实验收。

## 14. 任务 C 实现与验收记录（2026-10-28）

Applicable-Decision: ADR-GUI-003/004/005/006；版本 `gui-architecture-20261022-v3`。
本记录只覆盖 dev-01 私有首次使用组件和私有预览，不代表共享入口、真实登录、
真实聊天或五端验收已经可用。

### 14.1 基线与提交

| 项 | 值 |
| --- | --- |
| 工作树 | `/home/chloemartin/HyprialOS/.worktrees/gui-dev01-onboarding-20261028` |
| 分支 | `feat/gui-dev01-onboarding-20261028` |
| 任务 C 前 head | `bf4f2b3cacee212234bdc97c21b9ad8461b1b9cf` |
| 任务 A | `5a34b2bb5` |
| 复用 B 实现 | cherry-pick `e7b4e4130`（B `dd5f8fa71`） |
| 复用 B 文档 | cherry-pick `bf4f2b3ca`（B `6380ceae5`） |
| 任务 C 代码/测试 | `41be6b418` |
| 组合 head | 本记录提交后的 head；以 PAC 回报中的精确 SHA 为准 |

B 分支和 B 文件未修改。B 的 `AccountCredentialForm`、`ApiKeyForm`、
`SecretField`、`ModelServicePanel` 由 dev-01 私有组件只读复用；没有修改 B
的测试、分支或公开接入方式。

### 14.2 实现文件与职责

| 文件 | 职责 |
| --- | --- |
| `gui/src/pages/FirstRunPage.tsx` | 旧五步页面的薄包装；默认注入无后端动作，提供返回设置回调 |
| `gui/src/features/dev01/onboarding/FirstRunOnboarding.tsx` | 四步顺序、状态机、失败停留、重试、焦点和退出行为 |
| `gui/src/features/dev01/onboarding/AccountStep.tsx` | 登录/注册模式、B 账号表单、设备初始化子状态 |
| `gui/src/features/dev01/onboarding/ModelStep.tsx` | 复用 B `ModelServicePanel`，只在确认已连接后解锁 |
| `gui/src/features/dev01/onboarding/SquireStep.tsx` | 创建/启动两阶段；失败和未知停留本步；无跳过入口 |
| `gui/src/features/dev01/onboarding/ConversationStep.tsx` | 首次对话引导和跨 track 交接；不实现消息输入或发送 |
| `gui/src/features/dev01/onboarding/StepCard.tsx` | 既有 `Card`/token/样式类的步骤卡片；活动标题可聚焦 |
| `gui/src/features/dev01/onboarding/actions.ts` | 默认本机不可用回调与显式隔离预览回调；均不访问网络/存储 |
| `gui/src/features/dev01/onboarding/presentation.ts` | 把尝试结果映射到既有 `StatusCard`/`Notice` 语义，不推断成功 |
| `gui/src/features/dev01/onboarding/types.ts` / `index.ts` | 私有回调、步骤和聊天状态契约 |
| `gui/src/features/dev01/onboarding/preview.tsx` / `preview.html` | 非产品独立预览入口，明确标识隔离场景 |
| `gui/tests/dev01-onboarding.test.tsx` | 10 个聚焦测试 |
| `gui/tests/dev01-onboarding-browser.mjs` | 隔离浏览器验收脚本：显式 `executablePath`、375/760/1440、键盘焦点、四步隔离预览路径、0 网络/0 存储探针、真实截图 |
| `gui/tests/dev01-onboarding-browser.test.mjs` | 现有 `npm test`/Vitest 的 node-environment 入口；无 Chromium 时跳过，不修改共享浏览器脚本 |

没有修改 `App`、`AppShell`、共享路由、共享状态、适配器、运行时、共享测试、
`gui/src/styles/*`、`src/hyprial`、`contract`、`.forgejo` 或根脚本。未新增 HTTP、
daemon 或聊天协议，不依赖 #2043。

### 14.3 四步语义

1. **账号与设备**：账号确认和当前设备初始化都成功才可继续。设备初始化是
   步骤 1 的子状态，不新增第五步。登录/注册共用 B 的账号表单；正式无后端时
   回调返回本机不可用，页面显示未登录/不可用，不推进，不显示成功。
2. **模型服务**：复用 B 的账号授权/API Key 表单。只有校验回调明确返回
   `connected`，才启用“继续到 Squire”；失败、未知或不可用停留在本步。
3. **创建与启动 Squire**：先创建，再启动。Squire 是必填步骤，页面没有任何
   跳过入口。创建失败、启动失败、未知或不可用都停留本步，并给出当前阶段重试。
4. **首次对话**：只显示引导、当前聊天可用状态和交接到聊天的入口；不渲染
   消息输入框、发送按钮或任何聊天逻辑。只有外部 `chatStatus === 'completed'`
   且调用 `onComplete` 时，才表示可以进入开始页。

全局行为：

- 未到达的步骤显示锁定状态，不渲染其操作控件；活动步骤标题在推进后获得焦点。
- 失败或未知不推进；非敏感账号文本和当前步骤选择保留。
- 账号密钥只保存在组件内存中的最近一次请求里，用于当前步骤重试；成功、
  取消、离开、卸载时清除。不使用 `localStorage`/`sessionStorage`、URL 或日志。
- 返回设置只调用共享回调；未完成状态不持久化，重新进入从账号步骤开始。
- 默认 `live` 未注入回调时使用本机不可用动作；`scenario="demo"` 仅在显式私有
  预览中启用，显示隔离横幅并带 `data-scenario="demo"`，不使用共享 runtime 或
  共享 state 伪造成功。

### 14.4 研发补齐 UI 台账（交瑞华）

| 编号 | UI 项 | 原因 | 实现/组件 | 截图状态 | 待产品/瑞华设计项 |
| --- | --- | --- | --- | --- | --- |
| DEV-UI-ONB-01 | 登录/注册模式切换与注册说明 | 账号路径需要区分已有账号和注册；正式注册字段/流程未定 | `AccountStep.tsx`，B `AccountCredentialForm` | 见 14.6（375/760/1440 截图） | 注册页字段、校验、服务失败文案和入口位置 |
| DEV-UI-ONB-02 | 当前设备初始化子状态和按钮 | 设备身份是步骤 1 的前置，不新增步骤 | `AccountStep.tsx`、`presentation.ts` | 见 14.6（375/760/1440 截图） | 设备身份、就绪/未知/失败的视觉和文案 |
| DEV-UI-ONB-03 | 锁定步骤卡片、步骤编号和 Stepper 状态 | 让顺序、不可跳过和失败停留可见 | `FirstRunOnboarding.tsx`、`StepCard.tsx` | 见 14.6（375/760/1440 截图） | 完整四步视觉稿、移动端折叠/滚动方式 |
| DEV-UI-ONB-04 | Squire 创建/启动两阶段状态与重试 | 创建和启动是不同真实生命周期，不能用单一成功文案 | `SquireStep.tsx`、`presentation.ts` | 见 14.6（375/760/1440 截图） | 创建、启动、未确认、失败、已就绪的视觉稿 |
| DEV-UI-ONB-05 | 首次对话交接卡和状态入口 | 聊天由 dev03 负责；首次使用只做引导和完成信号 | `ConversationStep.tsx` | 见 14.6（375/760/1440 截图） | 引导文案、打开聊天/进入开始页的流程与按钮层级 |
| DEV-UI-ONB-06 | 隔离预览横幅与私有预览页 | 可在不接真实服务时检查正常界面路径，同时避免把预览当产品成功 | `FirstRunOnboarding.tsx`、`preview.tsx` | 见 14.6（375/760/1440 截图） | 是否保留研发预览、是否进入正式设计工具链 |
| DEV-UI-ONB-07 | 浅色 token/primitives 条件下的四步布局 | 复用既有 `first-run-grid`/Card/Notice/Button，避免新增全局 CSS | `StepCard.tsx`、既有 `workspace-pages.css` | 见 14.6（375/760/1440 截图） | 375/760/1440 视觉确认和最终响应式规范 |

以上“研发补齐，待产品规范设计”的内容没有被宣称为最终产品设计；瑞华确认后
需要回归并收敛临时补丁。没有新增全局样式。

截图证据位于工作区（不提交）：
`/home/chloemartin/.hyprial/agents/local-windows-worker/workspace/evidence/gui-dev01-onboarding-20261028-browser/`。
375/760/1440 各有初始账号页和完成对话引导页截图，均由隔离脚本实际渲染生成。
该 Linux/WSL Chromium 宿主缺少可用 CJK 字形回退，截图中中文字符显示为豆腐块；
这不改变 DOM/语义断言的通过结果，也不冒充可读视觉验收。宿主字体安装或提供
受控 CJK 字体属于验收入口环境问题，单独记录，不在本任务修改产品样式。

### 14.5 共享接线提案（不实现）

以下片段只供集成人 `work-developer` / dev03 评估，不在本私有分支修改共享文件。

**入口与动作注入**：共享层负责把真实的账号、设备、模型和 Squire 能力包装成
`FirstRunActions`；私有组件不创建 HTTP 客户端，也不把失败转成成功。

```tsx
// 共享集成层提案；不是本分支的共享实现
const onboardingActions: FirstRunActions = {
  authenticateAccount: (values, mode) => accountPort.authenticate(values, mode),
  initializeDevice: () => devicePort.initialize(),
  validateModel: (request) => modelPort.validate(request),
  createSquire: () => squirePort.create(),
  startSquire: () => squirePort.start(),
};

<FirstRunOnboarding
  actions={onboardingActions}
  chatStatus={chatState.status} // 'unavailable' | 'ready' | 'completed'
  onOpenChat={() => navigate({ page: 'chat' })}
  onComplete={() => completeOnboardingAndNavigateStart()}
  onExit={() => navigate({ page: 'settings' })}
/>
```

未拿到回调时的默认行为保持不可用/未知；任何 `catch` 只能落到未知，不能自动
推进。共享层如暂未提供适配器，应保留默认无后端状态，而不是注入模拟成功。

**完成到开始页**：`onComplete` 必须由 dev03 的真实聊天完成事件触发，不能由
首次对话卡上的按钮自身制造完成。共享层需要提供首次使用完成状态的持久化和
`first-run -> start` 路由；完成状态不得存到浏览器存储，也不得把密钥写入状态。
未完成时重新进入从账号步骤开始；完成后进入开始页。若宿主还没有持久化能力，
只能显示“待接入”，不能把本次点击当作永久完成。

**跨 track 聊天**：dev03 需要向首次使用提供以下最小语义，而不是让 dev01 直接
调用聊天内部：

```ts
type ChatStatus = 'unavailable' | 'ready' | 'completed';
// ready: 可以打开共享聊天入口
// completed: 已有一次可确认的真实对话完成事件
// unavailable: 聊天尚未接入；不得显示成功或代发消息
```

`onOpenChat` 只做共享路由切换；消息发送、真实回复、历史、失败重试由 dev03
负责。首次使用不渲染 textbox、不发送业务请求、不写本地草稿。

**入口与共享测试**：当前 B 设置页移除了“打开首次设置”入口，共享测试仍断言旧
五步页面，因此需要 `work-developer` 增加正式首次使用入口/门控并更新
`settings-first-run.test.tsx`、`app-shell.test.tsx`、`safe-r1-ui.acceptance.test.tsx`。
这些共享文件本任务不修改。

### 14.6 验证结果

| 检查 | 命令/范围 | 结果 |
| --- | --- | --- |
| 聚焦测试 | `tests/dev01-onboarding.test.tsx` | PASS，10/10 |
| B 复用回归 | `tests/dev01-settings.test.tsx` + `tests/dev01-model.test.tsx` | PASS，13/13 |
| dev-01 合计 | 上述三个测试文件 | PASS，23/23 |
| 类型检查 | `npm run typecheck` | PASS |
| 生产构建 | `npm run build` | PASS |
| 生产文案门禁 | `npm run verify:production-copy` | PASS，0 forbidden markers |
| safe-R1 构建门禁 | `npm run verify:safe-r1-build` | PASS |
| 隔离浏览器验收 | `npm test -- tests/dev01-onboarding-browser.test.mjs` | PASS，1/1；Chromium 1243，153.0.8010.12 |
| 全量 Vitest | `npm run test` | FAIL，7 个现存共享测试断言（见下）；隔离浏览器测试通过 |
| 私有预览可服务性 | 本地 Vite development server + Playwright 打开 `preview.html` | HTTP 200，标题含“私有预览”；四步隔离路径已在浏览器执行 |

全量 Vitest 的 7 个失败均位于共享旧断言：

- `tests/settings-first-run.test.tsx`：5 个失败，仍按旧设置页/五步首次设置路径。
- `tests/app-shell.test.tsx` 1 个失败：设置页不再有旧 `账号与设备` 标题断言。
- `tests/safe-r1-ui.acceptance.test.tsx` 1 个失败：设置页不再有
  “打开首次设置”按钮。

这些文件属于共享测试，按任务边界未修改；B 的边界变更和本任务 C 的新页面都需要
共享集成人更新断言后才可能恢复全绿。报告中不能把 7 个失败写成通过。

焦点与 0 副作用证据：

- 聚焦测试断言活动步骤的键盘 Tab 顺序（登录模式按钮、注册按钮、账号输入），
  并断言推进后焦点落到下一活动步骤标题。
- 正式路径、失败重试、隔离预览路径和返回设置的测试都断言 `fetch` 未被调用，
  且 `localStorage.length === 0`、`sessionStorage.length === 0`。
- 隔离预览和正式无后端路径都没有发送业务请求；私有预览页只使用本地回调。

浏览器 375/760/1440：**PASS（隔离浏览器测试）**。使用本机缓存的
`/home/chloemartin/.cache/ms-playwright/chromium-1243/chrome-linux64/chrome`
（Chromium 153.0.8010.12），由隔离脚本显式传入 `executablePath`，headless 启动；
候选列表覆盖 1243、1228 及对应 headless shell，本次实际启动 1243，其余作为后备。
未修改共享 `tests/dev-overlay-interaction.mjs`、`tests/safe-r1-browser-contract.mjs`
或任何共享浏览器入口。

```text
npm test -- tests/dev01-onboarding-browser.test.mjs
Test Files  1 passed (1)
Tests       1 passed (1)
Duration    5.89s
```

实际浏览器断言：

- 375px：`.first-run-grid` 1 列（345px）；760px：1 列（730px）；1440px：3 列
  （341.328px / 341.328px / 341.344px），第 4 步独占宽卡；四张步骤卡均在视口内，
  `document.documentElement.scrollWidth <= viewport + 1`，无横向溢出。
- 初始键盘顺序：返回设置 → 登录已有账号 → 注册新账号 → 账号输入 → 访问令牌 →
  显示访问令牌 → 取消；锁定步骤的创建、设备初始化和继续按钮不获得焦点。
- 隔离预览四步路径在浏览器中完成：账号确认 → 设备就绪 → 模型已连接 →
  Squire 创建/启动 → 首次对话引导；每次推进后焦点落到下一活动步骤标题。
  首次对话没有 textbox、发送按钮或跳过入口；点击“打开聊天”只调用预览回调。
- 浏览器探针：`fetch=0`、`xhr=0`、`storageWrites=0`、`localStorage.length=0`、
  `sessionStorage.length=0`，无 page error。
- 截图（工作区证据，不提交）：
  `evidence/gui-dev01-onboarding-20261028-browser/{375,760,1440}-account.png`
  和 `{375,760,1440}-conversation.png`，另有 `report.json`。

宿主依赖缺口：该 Linux/WSL Chromium 没有可用的 CJK 字形回退，截图中中文字符
显示为豆腐块；这是浏览器宿主字体问题，不是 DOM 内容或状态伪造。布局、焦点、
DOM 语义和 0 副作用断言均通过；不把截图中的豆腐块文字冒充为可读视觉验收。
直接运行独立脚本的首次尝试在工具层被拒：

```text
DEV01_SCREENSHOT_DIR=/home/chloemartin/.hyprial/agents/local-windows-worker/workspace/evidence/gui-dev01-onboarding-20261028-browser node /home/chloemartin/HyprialOS/.worktrees/gui-dev01-onboarding-20261028/gui/tests/dev01-onboarding-browser.mjs
This command requires approval
```

未修改权限配置、未使用 `dangerouslyDisableSandbox` 或替代包装绕过工具拒绝；随后
使用现有普通 GUI 测试入口 `npm test -- tests/dev01-onboarding-browser.test.mjs`
执行同一隔离浏览器测试，脚本仍自行指定 `executablePath`。共享浏览器测试仍因
硬编码 macOS Chrome 失败，单独记录在 14.9，不在本任务修改。

### 14.7 未测项与边界

- 真实账号登录/注册：BLOCKED；无正式后端，页面只显示不可用。
- 真实设备初始化/设备身份：BLOCKED；没有共享设备适配器。
- 真实模型授权/API Key 校验/额度费用：BLOCKED；本任务只复用 B 的校验回调。
- 真实 Squire 创建/启动/运行状态：BLOCKED；默认回调不创建进程或记录。
- 真实首次对话、回复、历史、完成到开始页：BLOCKED；依赖 dev03 和共享路由。
- 共享入口：当前 `FirstRunPage` 仍是无后端默认；没有修改 AppShell/共享路由。
- 持久化：没有实现；重新进入从账号步骤开始，未完成输入不保存。
- 375/760/1440 浏览器布局/键盘/隔离路径：PASS；上述截图为实际渲染但宿主 CJK
  字体缺失，中文为豆腐块，不能当作可读视觉签核。可读视觉验收仍需具备 CJK 字体
  的验收环境。
- 全量 GUI 门禁：共享旧测试 7 个失败；共享浏览器测试仍因硬编码 macOS Chrome
  失败；未宣称 `npm run verify` 全绿。isolated dev01 浏览器测试本身通过。
- 独立 review：尚未进行；本记录和代码提交不等同于批准或合入。

### 14.8 独立 review 交接

请独立 reviewer 基于组合 head 复验：四步顺序和设备子状态、Squire 不可跳过、
失败/未知不推进、内存密钥清除、默认无后端不伪造成功、隔离场景标识、0 网络/
0 存储断言、共享接线提案边界；并复跑隔离浏览器测试，核对 375/760/1440 布局、
键盘焦点和实际截图。截图中文因宿主 CJK 字体缺失为豆腐块，review 需同时判断
该环境限制是否影响视觉结论。review 结论应绑定精确 head，不由本实现方自批。

### 14.9 共享浏览器验收入口适配提案（不修改共享文件）

本任务使用的隔离脚本已显式指定本机 Chromium 1243 入口并实际通过；共享浏览器
测试仍把 macOS Chrome 写死，因此在本 Linux/WSL 环境不可执行：

```text
gui/tests/dev-overlay-interaction.mjs
const CHROME_PATH =
  '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome';
browserType.launch: Failed to launch chromium because executable doesn't exist at
/Applications/Google Chrome.app/Contents/MacOS/Google Chrome

gui/tests/safe-r1-browser-contract.mjs
Error: Google Chrome is required at
/Applications/Google Chrome.app/Contents/MacOS/Google Chrome
```

这是验收入口适配缺口，不是 dev01 组件失败，也不是“本机没有 Chromium”的结论。
建议由共享脚本 owner 另开任务处理，方案边界如下：

- 两个脚本共用一个浏览器入口解析 helper：优先读取显式环境变量
  （例如 `HYPRIAL_CHROME_EXECUTABLE` 或 `CHROME_PATH`），其次按平台尝试已安装
  Chrome/Chromium 和 Playwright 缓存中的 Chromium；找不到时输出检查过的路径，
  不静默改成其他浏览器。
- 校验候选路径存在且可执行；不把 URL、token、用户目录或凭据放进 argv/日志。
- CI/无浏览器环境明确 skip 或报告 `BROWSER_MISSING`，不能把 skip 写成 PASS；
  有浏览器时仍保留真实 375/760/1440、键盘和截图断言。
- 共享脚本的截图/字体环境也应显式记录；本机 CJK 字体缺失属于同类宿主依赖，
  不应通过伪造文案或替换页面内容解决。
- 本任务未修改上述共享脚本、未安装/替换浏览器、未修改权限配置；提案只记录，
  不据此改变共享门禁或宣称 `npm run verify` 通过。
