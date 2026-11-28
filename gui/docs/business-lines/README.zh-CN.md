# GUI 业务线开工包（`intg/gui-v1` 初始内容）

本目录是新 GUI 集成分支 `intg/gui-v1` 的初始内容：只含任务分工和规范，不含业务代码。
四条业务线从 release head 之后的 `intg/gui-v1` 拉出自己的分支开工；最终由 Allen 统一处理
冲突并合出 gui 第一版。

- 集成分支：`intg/gui-v1`（由协调者从 release head 创建）
- 业务分支：见第 1 节；fixtures 目录：`gui/tests/fixtures/<slug>/`
- 本包取代 `docs/design/gui-init-tracks.md` 的 track 命名；该文档的共享文件、PR 与门禁规则继续有效。

Allen 2026-11-01（原文）：「gui-init 最好合入 release 分支，明早开工我们会在新的集成分支上进行，
集成分支初始只有任务分工和规范。业务线你找到的四条线是对的，但后续请用可以一眼看懂的命名，
另外，补全这四条线上的内容使其完整覆盖整个业务，例如当前用户配置就没有，似乎应该把 dev-01
改为用户配置与 daemon 管理」

## 1. 四条业务线

| slug | 中文名 | owner | 工作分支 | fixtures 目录 | 替代旧 track |
| --- | --- | --- | --- | --- | --- |
| `user-settings` | 用户配置与 daemon 管理 | 黄佳佳 | `feat/gui-user-settings` | `gui/tests/fixtures/user-settings/` | dev-01 |
| `agents` | Agent 创建与管理 | 林懿伦 | `feat/gui-agents` | `gui/tests/fixtures/agents/` | dev-02 |
| `chat-channels` | 聊天、消息与渠道 | 李震宇 | `feat/gui-chat-channels` | `gui/src/dev/fixtures/chat-channels/` | dev-03 |
| `org-members` | 组织、邀请与成员 | 姚升悦 | `feat/gui-org-members` | `gui/tests/fixtures/org-members/` | dev-04 |

slug 用在：文档、分支名、fixture 目录。各线详情：`user-settings.zh-CN.md`、`agents.zh-CN.md`、
`chat-channels.zh-CN.md`、`org-members.zh-CN.md`。

## 2. 开工流程（Allen 2026-11-01，原文）

「按照计划，明天早上我们要组织开展 gui 组件的开发与完善…1. 每个业务线负责人（原 gui 集成分支
里面定义的三条业务线），首先阅读当前组件设计规范和后端对接原则 2. 各负责人首先梳理完成所有业务
组件，分层次实现组件。然后编写用于 openapi 测试的 mock 数据 3. 检查组件与后端是否接线，如果是
已存在功能，首先判断 daemon request 分层改造是否完成，没有完成的先进行改造。改造已经完成的
接入组件，并测试输入返回数据与 mock 数据是否一致 4. 按照瑞华给出的业务流程进行测试，测试通过
的合入 gui 中 5. 最后我统一进行合并的冲突处理，给出完整的 gui 第一版」

### 每步执行清单

1. **读规范**：按第 5 节阅读清单读完；确认本线的能力归属（第 6 节矩阵）与后端判定（各线文档状态表）。
2. **梳理组件 + mock 数据**：先列页面/组件清单（复用 `components/primitives`，不复制 preview/live 页面），
   分层实现；fixtures 放 `gui/tests/fixtures/<slug>/`，按第 3 节规则命名。mock 数据从 Contract Mock
   （MethodSpec examples）来，不自己发明第二份 DTO。
3. **接线检查**：对照本线状态表。已有功能先判定 daemon request 改造是否完成：有 MethodSpec 且
   HTTP 暴露 → 接生成 client 测试；只有 IPC 没有 MethodSpec → 先做改造（改 MethodSpec，跑
   `uv run python scripts/verify/dump_daemon_openapi.py --write` 再 `npm --prefix gui run client:reference`）；
   后端没有的能力 → 页面停在未连接/状态未确认/暂不可用，不显示成功。
4. **业务流程测试**：按瑞华的用户旅程测试正常、失败、空态和不可用；未接通的流程不得显示成功。
5. **合入**：PR 目标 `intg/gui-v1`；最终提交前按第 3 节做跨线 fixtures 比对与 cherry-pick；冲突由
   Allen 统一处理。共享文件（`App.tsx`、`AppShell.tsx`、`package.json`、`dev-module-index.css` 等）
   只由集成人修改，需要改先提接口提案。

## 3. fixtures 规则

Allen 2026-11-01（原文）：「注意组件实施时的 fixtures 应该这样进行：在第一次时，各方根据自己的
理解补全上下游 fixtures，并在最终提交集成分支的时候，与相关的其他线进行比对，cherry-pick 他们的
实施进行完善。例如，用户配置业务线的开发者，先按照自己的理解添加 org 相关的 fixture，在开发的
时候，不需要接入真实的 hyprial org 状态，而是使用自己生产的 org-fixture，但在最终提交时，如果
org 线已经完成，需要 cherry-pick 对方的 org 相关 fixture 替换掉自己生产的 org数据」

执行细则：

- **位置**：单元/开发 fixtures 统一放 `gui/tests/fixtures/<slug>/`（参考切片先例：unit fixtures 在
  `gui/tests/`，不进产品 API）。已有的 `gui/src/fixtures/chat-directory.ts`、`gui/src/dev/org/`
  开发数据在各自线动到时迁入本线目录。
- **命名**：文件名带本线 slug 前缀（如 `agents/agent-list.ts`、`org-members/org-snapshot.ts`），
  并导出稳定的类型名。这样最终提交时 cherry-pick 另一线的同名实现可以整文件替换，不留缝合痕迹。
- **上下游**：开工时按自己的理解补全上下游 fixture；在最终提交 `intg/gui-v1` 前，与本线消费的上游
  线、消费本线下游的线比对，用对方已完成的实现替换自己生产的版本。各线文档列了「必须提供/消费」清单。
- **边界**：fixtures 只进 `gui/tests/` 与开发场景；产品构建不含 fixture 数据（沿用
  `npm run verify:production-copy` 门禁）。fixtures 不含凭据明文。

## 4. 后端对接原则（源自参考切片）

1. 唯一分层：组件 → feature `api/`（把生成类型映射成视图模型）→ 生成 client → H-01 HTTP（读；
   写只经 H-03 write admission）→ daemon MethodSpec → 既有 handler。组件不 fetch、不选 driver、
   不解析 HTTP envelope、不持有第二份业务 DTO。
2. 改后端先改 MethodSpec：`uv run python scripts/verify/dump_daemon_openapi.py --write`，再
   `npm --prefix gui run client:reference`。绝不手写 DTO；缺方法不是手写 DTO 的理由。
3. 开发模式用 Contract Mock（读 MethodSpec examples 服务），没有本地成功 fallback；缺配置即显式
   报错（fail closed）。mock 只用于显式选择的隔离开发/测试。
4. 读操作走 H-01（`mutates=false`、`retry=safe`）；写操作只有 H-03 write admission
   （`mutates=true`、`retry=never`）后才允许进 HTTP 面。判定看 MethodSpec 的声明而不是这次调用是不是“读”：
   声明为 `mutates=true` 的方法（例如 `agent.provider.default`，`{}` 只读调用也一样）在 HTTP 上整个只经 H-03。
5. `dispatch` 如实上报：发送前 `not-dispatched`，服务端确认 `dispatched`，发送后中断 `unknown`。
   `unknown` 显示「结果未知」，不显示成功，也不自动重试。
6. 未接线/不可用只能显示 未连接/状态未确认/暂不可用，绝不显示成功；未接后端的业务请求数为 0，
   `localStorage`/`sessionStorage` 写入为 0。
7. token/Bearer 只放在宿主进程的 0600 配置里，绝不进浏览器
   代码、URL、日志或提交；浏览器请求只携带非密 marker。
8. principal 由可信宿主显式提供，浏览器不能自报身份；宿主特有能力不是
   daemon request 方法，不得写成 daemon 方法，也不得用 mock 假装已接线，缺口要如实登记。
9. HTTP 只有一根线：`POST /daemon/request/<method>`；不为能力开第二套 REST 路由；catalog digest
   不一致即报错（`CatalogDriftError`），浏览器不得自报目录版本。
10. 结论三态：unit/mock/browser 通过不等于真实 backend 等价；未实测的项如实报 UNKNOWN/BLOCKED，
    不用 skip 冒充结论。

## 5. 阅读清单（按序）

1. `gui/docs/ui-development-standard.zh-CN.md` — UI 规范唯一事实源：布局与 token、状态用词、
   无障碍、组件复用顺序与新增 primitive 的门槛。
2. `gui/docs/single-view-composition.zh-CN.md` — 预览与正式必须共用同一页面实现；预览不得另建
   产品表单或独立页面。
3. `gui/docs/component-catalog.zh-CN.md` — 现有 primitives、共享外壳、页面覆盖与新增组件配方；
   动手前先查有没有可复用的。
4. `gui/docs/http-request-client.md` — H-02 传输行为：单一 POST 路由、envelope 校验、dispatch
   三态、错误分类与生成器规则。
5. `docs/design/gui-init-tracks.md` — 上一轮 track 分工、共享文件热点、demo request 流程与集成
   门禁；本包沿用其规则，仅把 track 更名为业务线。
6. `gui/docs/modules/dev-01.md` … `dev-04.md` — 四条线现有组件、状态机、开发场景与测试（旧 track
   名）；本线开工前必读自己那篇与相邻线依赖。
7. 用户旅程：`design/gui-user-journeys` 的页面与流程已并入各模块的 J1–J10 场景（见 `gui/docs/modules/`
   与 dev-mode 场景文档），没有独立 journey 目录；各线以模块文档中的旅程章节与瑞华的验收为准。
8. `.agents/skills/gui-workflow/SKILL.md` — GUI 工作方式与「业务线工作」一节（本包入口、fixtures
    规则摘要）。

## 6. 能力覆盖矩阵

来源：`docs/reference/cli-command-inventory.json`（实际注册树，25 源码族）、daemon request 目录
（`src/hyprial/daemon/impl/request_catalog/`）与用户旅程（各模块 J1–J10）。规则：每个用户可见能力
归属恰好一条业务线；故意不进 GUI v1 的能力逐项给一行理由；不允许未分配的能力。

判定列含义见各线文档：**可直接接入** / **需先做 daemon request 改造** / **后端尚无此能力**。

### 6.1 user-settings（用户配置与 daemon 管理）

| 能力 | CLI 入口（`docs/reference/cli-command-inventory.json`） | v1 |
| --- | --- | --- |
| 账号登录/登出 | `login`、`logout` | v1 |
| 首次使用/本机初始化 | `init`（组）（首启使用普通 agent 运行配置，已无 `squire setup`） | v1 |
| 偏好设置 | `config set` | v1 |
| 身份 profile | `profile list`、`profile show`、`profile use`、`profile create` | v1 |
| 用户与设备绑定 | `user list`、`user bind`、`user unbind`、`user whois`、`user add`、`user show`、`user bindings audit`、`user bindings withdraw`、`user bindings publish` | v1 |
| 模型服务凭据 | `agent provider login`、`agent provider default`、`agent provider logout`、`agent provider grant-key` | v1 |
| daemon 生命周期 | `daemon status`、`daemon start`、`daemon run`、`daemon stop`、`daemon restart`、`daemon service status`、`daemon service install`、`daemon service uninstall` | v1（安装/卸载可只给本机引导） |
| daemon 升级 | `daemon upgrade`（组）、`daemon upgrade status`、`daemon upgrade apply` | v1 |
| 诊断与日志 | `doctor`、`log` | v1 |
| 主机 | `host list`、`host status`、`host create` | v1（列表）/后续（创建） |
| 本机运行监控 | `agent top` | 后续：实时进程采样属运维视图，v1 用 `agent.list` 的 runState 呈现状态 |

### 6.2 agents（Agent 创建与管理）

| 能力 | CLI 入口 | v1 |
| --- | --- | --- |
| Agent 创建/列表/详情 | `agent create`、`agent list` | v1 |
| Agent 运行管理 | `agent start`、`agent stop`、`agent unblock`、`agent restore`、`agent readiness` | v1（状态可判，动作按接线进度） |
| Agent 删除 | `agent destroy` | 未接：无 MethodSpec/HTTP，按钮禁用并显示原因 |
| 能力授权 | `agent grant`、`agent grants` | CLI 管理：无 MethodSpec/HTTP，GUI 不展示 |
| Agent 服务秘钥 | `agent secret set`、`agent secret rotate`、`agent secret remove`、`agent secret history`、`agent secret provider-write`、`agent secret grant`、`agent secret ls`、`agent secret list`、`agent secret revoke` | v1（`ls`/`list` 只读优先） |
| Agent 主机与迁移 | `agent ensure-home`、`agent home census`、`agent home migrate preflight`、`agent home migrate execute`、`agent home migrate rollback` | 后续：home/migrate 属多机运维，v1 只显示 owner · host 事实 |
| harness 能力 | `agent tiers`（组）、`agent tiers probe` | 后续：创建表单先固定已验证 harness |
| 高级配置 | `agent config rebind` | 后续：CLI 运维入口，GUI v1 无页面 |

### 6.3 chat-channels（聊天、消息与渠道）

| 能力 | CLI 入口 | v1 |
| --- | --- | --- |
| 消息收发与状态 | `message send`、`message ack`、`message reply`、`message read`、`message batch`、`message inbox`、`message status`、`message trajectory` | v1（会话列表+收发+失败/未知状态） |
| 发送目标 | `message targets` | v1（聊天对象目录） |
| 发件与投递 | `message outbox list`、`message outbox prune`、`message delivery status` | 后续：outbox 管理属运维视图，v1 用消息状态呈现 |
| 渠道（adapter）管理 | `adapter add`、`adapter remove`、`adapter start`、`adapter stop`、`adapter status`、`adapter reload`、`adapter list`、`adapter pin`、`adapter unpin`、`adapter downgrade-state` | v1（列表/状态/启停优先） |
| 渠道路由 | `adapter route list`、`adapter route candidates`、`adapter route add`、`adapter route remove` | 后续：路由配置属管理面，v1 只显示渠道连接状态 |
| 渠道身份与媒体 | `adapter identities sync`、`adapter identities list`、`adapter identities find`、`adapter media get` | 后续：v1 无媒体与身份检索页面 |
| 渠道授权 | `adapter scopes check`、`adapter scopes authorize`、`adapter user-auth check`、`adapter user-auth complete` | v1（飞书授权四步所需） |

### 6.4 org-members（组织、邀请与成员）

| 能力 | CLI 入口 | v1 |
| --- | --- | --- |
| 组织生命周期 | `org create`、`org repair`、`org leave`、`org delete`、`org list`、`org repin`、`org pin-authority`、`org migration-status` | v1（创建/列表/离开优先） |
| 邀请与加入 | `org invite`、`org join`、`org pending`、`org group invite` | v1 |
| 成员管理 | `org remove` | v1（移除预览+确认） |
| 组织网络 | `org network` | v1（通信视图状态） |
| 通信 link | 已退役（原 `agent link *` 与 daemon `link.list`/`link.stats`/`link.add`/`link.drop` 已移除） | 已退役：send-link 与 `link.*` 方法已随 T2a/T2b 下线 |
| ACL | `org acl grant`、`org acl revoke`、`org acl ls` | 后续：v1 只显示通信权限状态，不做编辑 |
| 组织文档 | `org context validate`、`org context show`、`org context status`、`org context fetch`、`org context import` | 后续：配置发布流程 v1 只做交互占位 |
| Agent 迁移 | `agent teleport` | 后续：迁移向导交互已有，真实接线等 teleport 接入 |
| 组织服务暴露 | `org service connect`、`org service disconnect`、`org service list`、`org service guide`、`org service expose`、`org service unexpose`、`org service exposures`、`org service whois` | 后续：服务暴露属组织网络管理面，v1 不做页面 |
| 组织文件 OrgFS | `org fs ls`、`org fs resolve`、`org fs read`、`org fs write`、`org fs mkdir`、`org fs mv`、`org fs rm`、`org fs watch`、`org fs import`、`org fs export`、`org fs space list`、`org fs space create`、`org fs space join`、`org fs space web`、`org fs space status`、`org fs space checkout`、`org fs member invite`、`org fs member remove`、`org fs member list`、`org fs trash list`、`org fs trash restore`、`org fs purge run` | 后续：组织页「文件」子页签保留交互，数据接线按 orgfs 改造进度 |

### 6.5 整族不进 GUI v1（一行理由）

| CLI 入口 | 理由 |
| --- | --- |
| `org work ls`、`org work show <相对文件路径>` | 仅为组织 `work/` 下普通 OrgFS 文档的读取快捷入口，分别调用 `orgfs.ls/read`，不另设任务业务页面。编辑、冲突与恢复使用 6.4 的通用 OrgFS 能力。 |
| `workflow plan`、`workflow run`、`workflow list`、`workflow inspect`、`workflow cancel`、`workflow events`、`workflow reset`、`workflow complete`、`workflow fail`、`workflow gc`、`workflow overview show`、`workflow overview publish`、`workflow worker stop`、`workflow worker restart`、`workflow notify resend` | PAC workflow 管理面向协调者/worker，GUI v1 无专用管理页面；其执行与批准节点独立于普通任务文档。 |
| `routine plan`、`routine add`、`routine list`、`routine status`、`routine remove`、`routine set`、`routine pause`、`routine resume`、`routine templates`（组）、`routine templates show` | 周期任务属 dev-06 范围，gui-init-tracks §13 明确本阶段不含。 |
| `gui` | GUI 启动器本身，不在 GUI 内提供。 |
| `help`、`version` | CLI 元命令；GUI 版本信息由构建展示，不接 daemon 方法。 |
| `socialware install` | COMING_SOON 占位，没有可接的 GUI 能力。 |
| `agent runtime *`（2 个隐藏入口）、`daemon upgrade run`（隐藏）、`workflow history *`（隐藏）、`workflow migration status`（隐藏） | 隐藏运维入口，不进 GUI；列此处仅为完整对账。 |

工作文档的当前契约见 [Work documents in ordinary OrgFS](../../../contract/work-items/README.md)。
专用 work 写命令、collect/finish 与 `work.*` IPC 已退役。文本编辑使用普通读取返回的
`version` 配合 `orgfs.write.expectVersion`，冲突使用通用 resolve/move/trash。
任务 schema 只用于只读投影，状态、owner 或旧审批字段不构成授权证明；不以本地账本或
完整 journal 作为当前文本可读条件。已有文件、普通 OrgFS 历史与旧账本不迁移、不删除。

### 6.6 覆盖对账

矩阵把 25 个源码族的全部可见入口分派完毕：6.1–6.4 分派到业务线，6.5 整族排除。
复核方法（reviewer 用）：对 `docs/reference/cli-command-inventory.json` 中每个
`hidden=false` 的叶子路径，必须能在 6.1–6.5 各表中找到。

## 7. 跨线 fixtures 提供与消费（提交前比对用）

| 线 | 必须提供（权威方） | 需要消费（开工可自产，最终 cherry-pick 权威方实现） |
| --- | --- | --- |
| user-settings | 账号/模型凭据/provider 场景 fixtures | org（个人组织显示）、agent（普通 Agent 运行配置）、chat（首次对话草稿跳转） |
| agents | agent 列表/详情/管理面板 fixtures | org（组织标签与筛选）、模型凭据状态（user-settings）、chat（开始对话目标目录） |
| chat-channels | 聊天目录与历史/渠道场景 fixtures（现 `gui/src/fixtures/chat-directory.ts` 迁入） | agent（聊天对象）、org（被拒提示修复入口）、渠道状态 |
| org-members | org 快照/成员/邀请/迁移 fixtures（Allen 例中的「org 线」权威方） | agent（组织内 Agent）、机器与 link 状态 |

## 8. 门禁

各线 PR：本线焦点测试 + `npm --prefix gui run verify`（全链，含 `verify:production-copy` 与各
browser 合同）+ `git diff --check`；Python 侧改动另跑 `uv run ruff check .` 与结构/边界门
（`scripts/ci/check_structural_limits.py`、`scripts/ci/check_module_boundaries.py`）。
只改 markdown 的本包按「结构门 + 模块边界门 + ruff 全仓 + `git diff --check`」执行，并在 PR 正文
写明未测项。集成分支统一回归由集成人执行（gui-init-tracks §12）。
