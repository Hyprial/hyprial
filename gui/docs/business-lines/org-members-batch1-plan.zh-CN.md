# org-members 第一批：org.list + org.fetch 接入（实现预算约 3 小时）

- 目标分支：`intg/gui-v1` @ `196bf209d9e7cefeeab03f6c7b4dc655e2759baa`
- 实现分支：`feat/gui-org-members`（本文件所在分支）；PR 目标 `intg/gui-v1`
- 状态：实现完成；方法层规范复评 PASS（`8ea9e6e19`）；GUI 评审 P3 已修复（`1164f8cd8`）；
  PR #2277 已创建（`feat/gui-org-members` → `intg/gui-v1`）；最终门禁、Swagger 自查与
  E2E-025 mesh 两轮 PASS 见 `docs/notes/org-members-batch1-final-2026-11-02/`。本文件是
  恢复点与实现边界，不是验收结论；跨真实机器/tailnet 的 mesh 仍为 UNKNOWN。
- 实现预算：约 3 小时（按姚升悦 2026-11-02 的评估；不含评审等待、真实 daemon/mesh 环境故障排查）。

## 1. 目标与可演示流程

让组织页主读路径用真实 daemon request，而不是 fixture：

1. 打开组织页：从 `org.list` 读取本机真实组织，切换器与概览显示组织名、角色、设备数、状态；
   来源未提供的字段显示「状态未确认」，不补零、不猜。
2. 组织设置里的「同步组织配置」：调用 `org.fetch`，向 mesh（默认 `requestMode=neighbors`，
   2 s 超时）请求组织上下文候选；显示请求数、应答数、接收数、拒绝数、候选版本与来源。
3. 候选「采信」仍不可用：读取候选正文/状态的 `org.context.show/status/import` 后端尚无，
   页面明确显示暂不可用，不假装已接通。

可选演示（真实环境）：单机只能验证 0 应答与未就绪路径；看到候选需要至少两台 daemon
组成组织 mesh，单机验收记为 BLOCKED/UNKNOWN，不用 mock 冒充结论。

## 2. 方法盘点与分类

| 能力 | 现状（`intg/gui-v1` @ 196bf209） | 本批 | HTTP 准入 |
| --- | --- | --- | --- |
| `org.list` | 已实现可经 IPC 调用：`src/hyprial/daemon/impl/org/network/ipc.py:314` → `OrgNetworkService.list()`（`org/network/service.py:422`）。无 MethodSpec，生成 client 无 `org.*`，未准入 HTTP | 新增 MethodSpec + 真实 handler 绑定 | H-01 读画像：`mutates=false`、`retry=safe`、identity `none` |
| `org.fetch` | 已实现可经 IPC 调用：`app._ipc_org_fetch(params)`（`application/messaging/status/views.py:664`）。无 MethodSpec，未准入 HTTP | 新增 MethodSpec + 真实 handler 绑定 | H-03 写画像：`mutates=true`、`retry=never`、identity `none`、confirmation `none`、unary |
| `org.network` / `org.remove` / `org.invite` 等 | 仅 IPC | 不做 | — |
| `link.list` / `link.stats` | 已有 MethodSpec，`http_exposed=True`（`request_catalog/link.py:203/222`） | 不做（需要宿主可信 actor 身份，另批） | 已准入 |
| 成员详情、邀请、机器、拓扑、文件、统计 | 无已准入的 daemon 方法 | 不做；页面保持未接通/状态未确认 | — |

判定口径：生成 client 里出现方法名不等于可调用；本批只把上面两个方法从
「缺 MethodSpec / 未准入 HTTP」推进到「已声明、已注册、已准入、真实可调用」。

## 3. 共享 vs 本线独占

共享文件（改动需在 PR 中单列，知会集成人）：

- `contract/daemon-request/openapi.json`、`gui/src/client/generated/*`：只能重新生成，不手改。
- `gui/src/app/capabilities.tsx`：新增可选字段 `orgSource?: OrgDomainSource`；不改变现有能力，
  默认不提供时界面与现在一致。属于组合入口接口，按开工包第 2 节先给集成人提案。
- `gui/src/pages/OrgPage.tsx`：从 capability 取 `orgSource`，仅在存在时包 `OrgDomainProvider`。
  沿用 `#2172` 的单视图规则：预览与正式共用同一页面实现。

本线独占：

- `src/hyprial/daemon/impl/request_catalog/org.py`（新文件）、`request_catalog/__init__.py`、
  `registry.py` 的绑定组合。
- `gui/src/features/org/api/*`、`gui/src/features/org/domain/live-source.ts`、org 域名类型与
  页面/Settings 的接线。
- `gui/docs/business-lines/org-members.zh-CN.md` 状态表与证据。

公共方法负责人：`org.list`、`org.fetch` 由 org-members 线登记为「改造：本线」。其他线需要时
写「复用：org-members」，不并行改同一 MethodSpec。

## 4. 交付文件

### 4.1 daemon request

1. `src/hyprial/daemon/impl/request_catalog/org.py`（新）
   - `ORG_LIST_SPEC`：handler 直接调用 `ipc_org_list(app, params)`；空参数对象
     （`additionalProperties=false`）；结果按真实返回：
     `{orgs: [{org, spaceId: string|null, role: string|null, devices: int, state: string}], leftOrgs: [string]}`。
   - `ORG_FETCH_SPEC`：handler 调用 `app._ipc_org_fetch(params)`；参数：
     `timeoutSeconds`（number，`0 < t <= 30`，默认 2，布尔/字符串/0/负值/31 均拒绝）、
     `from`（可选非空字符串）、`requestMode`（可选，枚举 `neighbors`；其他值按参数错误拒绝，
     不再静默走批处理）；结果按 mesh 与 orgfs 两种真实返回的并集声明
     （`requestedSources`、`responseCount`、`receivedCount`、`rejectedCount`、`candidates[]`，
     以及 orgfs 模式可选的 `source`、`refreshed`、`spaceId`、`deprecatedSourceTarget`、
     `sourceTargetHint`）。
   - 错误声明必须覆盖 handler 真实可能抛出的 code（注册表会把未声明 code 改写成 `DAEMON_ERROR`，
     丢掉语义）。至少核对：`INVALID_ARGUMENT`、`DAEMON_NOT_READY`、`ORG_SOURCE_UNREACHABLE`
     （data `{availableSources: [string]}`）、`ORG_SPACE_NOT_JOINED`（data `{hint}`）、
     `ORG_NETWORK_ERROR` 以及 `org.list` 路径上真实可达的 orgfs code；用故障注入测试逐项确认。
2. `src/hyprial/daemon/impl/request_catalog/__init__.py`、`registry.py`：导出 `ORG_BINDINGS` 并纳入
   `_build_registry()`。**不把 `org` 加入 `MIGRATED_FAMILIES`**：org 族还有大量未迁移方法，
   加入封闭族会让它们全部变成 `METHOD_NOT_FOUND`。只登记这两个名字。
3. `src/hyprial/daemon/impl/request_catalog/org_examples.py`（或现有 examples 模块）：
   `org.list` 成功/空/结构非法参数；`org.fetch` 成功（0 候选与有候选）、orgfs 可选字段、
   超时参数非法；错误用例覆盖 `ORG_SOURCE_UNREACHABLE` 的 data。示例必须被各自 schema 校验，
   供 Contract Mock 使用；不手写第二份 DTO。

### 4.2 生成物

```sh
uv run python scripts/verify/dump_daemon_openapi.py --write
npm --prefix gui run client:reference
npm --prefix gui run client:reference:check
```

`contract/daemon-request/openapi.json` 与 `gui/src/client/generated/*` 一起提交；合并/rebase 后
重新生成，不手工解冲突。

### 4.3 GUI

1. `gui/src/features/org/api/org-request-api.ts`（新）：生成 client → 视图模型。
   - `listOrgs()` 校验 `orgs`/`leftOrgs` 形状；`spaceId`、`role` 允许 null。
   - `fetchContext({timeoutSeconds, requestMode, from?})` 返回计数与候选摘要。
   - 不新增 DTO、不解析 HTTP envelope、不 fetch。
2. `gui/src/features/org/domain/types.ts`：`OrgSummary` 缺失来源字段改为可为 null
   （`role`、`ownerLabel`、`authorityDevice`，`lifecycle` 增加 `unknown`），与文件头
   「null 表示来源未提供」一致；同步修改引用处显示「状态未确认」：
   `crosstab/ChatRefusalHint.tsx`、`domain/selectors.ts`、`sections/OverviewSection.tsx`、
   `sections/SettingsSection.tsx`、`workspace/OrgWorkspace.tsx`。
   设备数用 `OrgSummary.deviceCount: number | null` 带入，只显示 `org.list` 的真实值。
3. `gui/src/features/org/domain/live-source.ts`（新）：`createOrgLiveSource(client)`，返回
   `OrgDomainSource`；`client` 是公共注入的 `GeneratedClient` 最小依赖面。
   未实现的方法统一委托 `createUnwiredGateway()`；实现：
   - `refresh()`：`org.list`，更新 `orgs`、`currentOrgId`、`details` 仍为 null、状态 ready；
     失败按 `OrgResult.failed`，未就绪/不可达不显示成功。
   - `syncContext()`（`OrgGateway` 新增，本线类型）：`org.fetch`，返回计数与候选摘要；
     失败保留 code/message；超时或传输中断显示「结果未知」，不自动重试。
   - `OrgPage` 在 capability 存在且外层未提供 domain 时挂载它；普通产品没有
     `orgSource` 时显示 `UNWIRED_SOURCE`，开发 fixture 只在显式组件演示/测试入口使用。
4. `gui/src/features/org/sections/SettingsSection.tsx`（或概览）：「同步组织配置」按钮，
   显式动作才调用 `syncContext`，禁止页面加载时自动调用（`org.fetch` 是写：会在本机
   org-context store 暂存候选）。显示请求/应答/接收/拒绝、候选版本与来源；无候选也明确
   「没有收到候选」。候选采信入口保持暂不可用，并注明后端缺口。

## 5. 估时（实现预算 3.0 h）

| 项 | 估时 |
| --- | --- |
| daemon：两个 MethodSpec、真实绑定、错误清单（含 org.py/registry） | 0.9 |
| examples + openapi/client 重新生成与检查 | 0.3 |
| Python 定向测试 + IPC/Swagger 自查 | 0.5 |
| GUI：api、live source、OrgSummary 空值、OrgPage/Settings 接线 | 0.8 |
| GUI 单测 + Contract Mock 浏览器契约 + dev04 既有 36 条回归 | 0.4 |
| 状态表/PR 正文/证据整理 | 0.1 |

评审等待、真实 mesh 环境搭建不计入；如遇 org 子系统不稳定（见第 7 节）另记时间与三态结论。

## 6. 依赖与阻塞

- `org.fetch` 的真实候选需要两台及以上 daemon 在同一组织 mesh；单机只能验证 0 应答、
  未就绪、参数与错误路径。真实候选结果记 UNKNOWN/BLOCKED。
- 组织子系统近期有 `org list` 15 s 超时与 anti_entropy 自循环的报告；monitor 要求不重启
  daemon。真实 daemon 验收可能 BLOCKED，按三态记录，不用 mock 冒充。
- `gui/src/app/capabilities.tsx` 与 `gui/src/pages/OrgPage.tsx` 是共享组合面，需集成人知会；
  未获接受时 capability 默认不提供，页面回退为未接通。
- 方法评审：`codex-daemon-request` 对固定 SHA 做独立方法评审；评审未完成前 PR 正文写
  「口径评审待补」，不据此声称通过。
- CI：`gui-unit` 在旧 `src/gui` 路径上的共享失败若仍存在，与本批无关也不能当作通过；
  PR 正文写 BLOCKED 与证据。

## 7. 验证方式

Python（定向，不跑全仓）：

- MethodSpec 校验：参数负例（`timeoutSeconds` 为 0/-1/31/true/"2"、`from` 为空、
  `requestMode` 非枚举）、结果 schema 接受 mesh/orgfs 两种真实形状。
- 绑定：`METHOD_REGISTRY` 中存在且 handler 调到真实 `ipc_org_list` / `app._ipc_org_fetch`；
  未声明 code 不被静默改写。
- HTTP 准入：`request_http/catalog.py` 的 `build_view` 中两个方法都在 routes 里；`org.fetch`
  的 `mutates/retry/confirmation/identity` 符合支持画像；`org.list` 命中读画像。
- `dump_daemon_openapi.py --check`、`check_agent_contract_examples.py`（若纳入本批示例）。

真实 IPC / Swagger 自查（隔离 HOME/XDG/socket/端口，不用日常 daemon）：

- 通过真实 IPC 调用 `org.list`、`org.fetch`，与 HTTP 结果逐字段比对；确认 `org.fetch`
  的暂存副作用和 `dispatch` 三态。
- 启动本地 Swagger UI，逐项核对正常、缺参/null/边界、错误 code/data、未就绪、来源字段；
  token 只在页面内存，不写 URL/存储/截图。仅 IPC 的方法不为 Swagger 开放。

GUI：

- 单测：adapter 映射（null role/spaceId → 状态未确认）、`fetchContext` 计数与错误、
  live source 的 `refresh`/`syncContext`、capability 缺失时无请求。
- Contract Mock 浏览器契约（examples 来自 MethodSpec）：组织页显示 mock 组织；点击
  「同步组织配置」只发 `POST /daemon/request/org.fetch`；无第二套 REST；写入
  `localStorage`/`sessionStorage` 为 0；生产构建无 capability 时不显示成功。
- 原 `gui/tests/modules/dev04/org-acceptance-browser.mjs` 36 条回归已随 PR 1e 第一段删除（基线即失败且不在任何工作流）；回归以 `org-request-browser.mjs` 与 vitest 的 `tests/modules/dev04/` 为准（`org-lifecycle-browser.mjs` 已随 PR 1e-4 删除，场景见 `docs/design/gui-single-launch-plans-2026-11-15/1e-4-stage-f-acceptance.md`）。
- `npm --prefix gui run verify`（本线 PR 门禁）、`git diff --check`；Python 侧 `uv run ruff check .`、
  结构门、模块边界门。

固定 SHA 独立方法评审：PR 正文给方法清单、使用方、精确 head、生成检查、测试结果与未测项，
由 `codex-daemon-request` 出结论；本线不自行宣称方法评审通过。

全量 Python 与最终回归：姚升悦 2026-11-02 决定，本批不跑全量；全量检查与修复由 coordinator
在 intg 最终提交内容确定后统一执行。本批不以全量结果作为结论。

## 8. 明确不做

- 成员列表/详情、邀请、加入、移除、创建/修复/离开/删除组织、机器、拓扑、统计、文件、ACL、
  OrgFS、迁移向导、服务暴露。
- 候选配置的读取与采信（`org.context.show/status/import` 后端尚无）；只做同步与候选摘要。
- `link.list`/`link.stats` 的 GUI 接线（需要可信 actor 身份，另批）。
- 不改 `MIGRATED_FAMILIES`；不注册空 handler；不为通过检查削弱声明；不让浏览器自报身份；
  不新增业务 REST；不读写 daemon 数据库；不把 fixture/mock 说成真实后端。
- 不迁移 `gui/src/features/org/dev/` 到 `gui/tests/fixtures/org-members/`（fixtures 迁移另批，
  本批只新增 MethodSpec examples）。
- Agent move 真实方法由 `codex-sw` 维护，本线不平行实现。

## 9. fixtures 状态与使用方

本批可提供的权威材料（org 线）：

- `org.list` 示例：个人无组织（空列表）、工作组织所有者、成员视角、`partial`/`legacy-active` 状态、
  `role=null`、`spaceId=null`、leftOrgs 非空。
- `org.fetch` 示例：0 候选、mesh 有候选（版本/发布者/来源）、orgfs 模式（`source=orgfs`、
  `refreshed`、`spaceId`、deprecated 提示）、`ORG_SOURCE_UNREACHABLE` 的 availableSources。
- 现有开发场景仍由 `gui/src/features/org/dev/fixture-data.ts` 提供（个人组织/所有者/成员/邀请/
  迁移/历史异常）；本批不移动它们。

使用方：user-settings（个人组织显示）、agents（组织徽标与筛选）、chat-channels（被拒提示）、
org 页面自身与后续批次。邀请链接等凭据一律占位值，不写真实链接。

## 10. PAC 执行方式（coordinator：cc-gui-orgs）

- 实现节点：PAC format 2，`launch: {harness: pi, provider: deepseek, model: deepseek-flash,
  args: ["--thinking","max"], cwd: /Users/member-b/projects/hyprial-gui-org-members-20261102}`，
  即「deepseek flash max」；显式 cwd 使用本分支工作树，不让 PAC 另建隔离 worktree。
- 每个实现节点完成后 PAC 通知 coordinator；coordinator 检查固定 SHA 与证据，再把该 SHA
  交 `codex-gui-orgs-review` 独立评审。评审不通过则用新图派修复节点（同一工作树），
  直到评审通过；每轮保留自己的固定 SHA 与失败/通过证据。
- 评审通过后 coordinator 做节点验收（门禁、生成检查、三态记录），更新 kanban 卡，
  再准备 PR（正文含 `Submitted-by`、方法清单、固定 SHA、未测项）。
- 所有问题经 coordinator 汇总后报姚升悦决策；worker/reviewer 不直接改共享文件或合并分支。
