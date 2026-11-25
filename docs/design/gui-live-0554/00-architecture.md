# GUI 统一接线：总架构与要求

规划日期：2026-11-04。适用目标：0.5.5.4 接线补齐。以下“新增”均为待实现能力。

## 1. 目标与事实基线

当前已有 MethodSpec、daemon request、FastAPI 和 generated client，但产品入口没有把四条线和首次登录装配到同一个调用通道。组织 live adapter 已存在却未注入普通 App；agent 的部分方法已用 generated client；settings 的登录/初始化走专用 worker，很多管理能力没有 HTTP 契约；chat 仍用独立业务 bridge，其中部分底层方法未登记 MethodSpec。各线已有实现仍需公共装配与完整首次旅程补齐。

本计划的目标是：从 GUI 登录/注册开始，所有后端业务动作都有 MethodSpec，走相同调用链；真实模式调用 daemon，缺能力返回明确错误；只有一个显式开关选择真实后端或 Contract Mock。前端 fixture 不能充当产品兜底。

规划采用的源码快照（2026-11-04）：

| ref | 精确 SHA | 结论 |
|---|---|---|
| release/v0.5.5.4 | `764cf168ecf39713da463af66557cda0044b1fee` | 本文实施事实基准 |
| dev | `db184708b45ad90efa9b557314f87e1c2d099fca` | 此快照尚未包含上述 release tip |
| intg/gui-v1 | `540ce1626685d0b88d0de2974da249bbc251c6ae` | 已有 GUI 集成工作的参考点，不等于产品入口已完整接线 |

release/dev 的共同祖先为 `1e59a8e94af245787a179268d3bc95134d3c38e9`；两者 VERSION 文件仍为 `0.5.5.3.1`。基线核验未查到 v0.5.5.4 tag，release API 查询返回 403，发布包状态 UNKNOWN。因此不能把“release 分支存在”写成“0.5.5.4 安装包已发布且 dev 已包含”。协调者提交前必须重新固定目标 SHA。

基线核验：上述 release 源码的 OpenAPI `--check` 与 generated client `--check` 均 PASS；纯源码 MethodRegistry 投影为 75 个 MethodSpec、68 个 HTTP 暴露方法。方法明细见 [catalog.json](../../notes/gui-live-wiring-20261104/catalog.json)。这两个生成物检查不证明真实服务、浏览器登录或完整功能通过。

## 2. 唯一架构与调用规则

### 2.1 产品终态

```text
GUI 页面/组件（包括首次登录、注册入口、初始化、设置）
  → 各功能的薄 adapter（只做 UI 输入/输出转换）
  → 同一个注入的 DaemonClient / generated client
  → 同一个 HttpTransport
  → 同源宿主转发 /daemon/request/<method>
  → HTTP FastAPI 的 MethodSpec 投影路由
  → daemon_request_once → 唯一 daemon IPC registry / MethodSpec 校验与授权
  → daemon 方法 → 所属 domain 的公共服务/权威状态
  ← 同一错误 envelope / code / requestId / dispatch / catalogDigest

显式 contract-mock 模式：
同一 GUI、同一 adapter、同一 generated client、同一浏览器 URL
  → 宿主选择 Contract Mock upstream
  → 由同一 OpenAPI/examples 产生结果或错误
```

宿主允许管理连接、同源防护、凭据注入与自有子进程生命周期；不得实现第二套 chat/settings/org 业务 API、durable inbox、会话/turn 权威或业务状态库。物理上可有 daemon、HTTP、GUI 服务多个进程，但应由一个入口统一启动、检查、清理自有进程，不能要求用户分别配置四条线。

MethodSpec 是唯一契约源：参数、结果、错误、身份绑定、读写属性、确认条件、重试/回执语义均由它声明。OpenAPI、TypeScript client、Contract Mock examples 和测试依次生成/验证。禁止先手写前端 DTO，再假装它就是 daemon 契约。

### 2.2 没有后端时怎样处理

| 实际情况 | 必须行为 |
|---|---|
| 有 handler + MethodSpec + HttpBinding | 使用 generated client 正常调用；保留后端业务错误 |
| 有 legacy IPC handler，没有 MethodSpec/HTTP | 原后端 owner 补契约与授权/负例，再接前端；补齐前明确 unavailable，不让 GUI 绕过 HTTP 调 IPC |
| 有方法名/界面，但没有可对应的后端语义 | 返回稳定的 capability unavailable；显示未实现和错误码，不能造成功、写本地 fixture 或调用“差不多”的方法 |
| 浏览器调用不存在的 HTTP 方法 | 现有 HTTP 返回 `METHOD_NOT_FOUND`（404）；前端统一呈现。catalog 缺项可在发送前生成带 `not-dispatched` 的 `GUI_CAPABILITY_UNAVAILABLE`（拟新增客户端错误），不能声称这是 daemon 已返回的错误 |
| daemon 未 ready / 身份不符 / catalog 漂移 | 请求 envelope 保留 `HTTP_NOT_READY`、`PRINCIPAL_MISMATCH`、`CATALOG_STALE`/`CATALOG_MISMATCH`。`PRINCIPAL_CONFIG_MISMATCH` 是启动 refusal，启动器单独报告，不能冒充请求返回码；禁止自动切 mock |
| 写请求发出后断线或超时 | 保留 `dispatch=unknown`；用声明的查询/receipt 方法确认，禁止自动重发或显示“未发送” |
| Contract Mock 无该场景 | `MOCK_SCENARIO_MISSING`（409）；不生成任意假数据补洞 |
| 明确拒绝的业务 | 例如 org.delete 的 `LAST_AUTHORITY_REFUSED`，真实/Mock 均保持拒绝；有接口不等于允许成功 |

本地纯 UI 行为（展开、选择标签、编辑尚未提交的草稿）不需要虚构 daemon 方法；凡读取/改变后端业务事实，必须遵守上述链路。

### 2.3 一个开关与一次装配

唯一模式配置沿用键名，扩展取值：`HYPRIAL_GUI_BACKEND_DRIVER=daemon|contract-mock`。这是拟议新行为，当前代码并不支持本文完整语义。

- 模式仅由启动宿主解析一次；非法/缺失值 fail closed。产品默认启动器可显式选择 daemon，测试选择 contract-mock。
- 四条线不得各读模式环境变量、query 参数或创建自己的 endpoint/token/principal。
- 同一份构建资源可用两种模式运行，模式必须来自宿主的安全运行时描述，不能依赖 Vite build 时布尔值。
- 宿主描述仅含 mode、ready phase、公共 catalog 元数据、连接 generation；无 bearer、refresh token、任意 socket/home 路径。
- 切换模式必须退出并以独立 home/runtime/端口重新启动；不在已有真实会话中无提示热切换。界面全局显示当前来源。
- `HYPRIAL_GUI_REFERENCE_CONFIG`、`HYPRIAL_GUI_AUTH`、`HYPRIAL_GUI_SETUP`、`HYPRIAL_BACKEND_ENDPOINT` 不再作为并行产品业务入口开关。旧测试 reference 保留时必须不能混入普通产品构建。
- 开发 fixture 仅保留在显式组件演示/测试入口；普通 DEV App 也不得自动使用 fixture。
- 登录 provider 的远程认证和业务 Contract Mock 是不同概念；mock 模式不得读取真实凭据、启动真实业务 daemon 或联系真实身份提供方。

## 3. 工作线与责任人

| 工作线 | 负责人 / 执行责任 | 文档 | 固定职责 |
|---|---|---|---|
| coordinator | linyilun / emergent | [coordinator](01-coordinator.md) | 维护容器、排依赖、统筹合并与最终 release 交付 |
| 公共线 | yaoshengyue / codex-daemon-request | [公共线](02-common.md) | 公共契约、冷启动、身份隔离、唯一 client/transport、宿主、生成与 Mock |
| agent | linyilun | [agent](03-agent.md) | agent 目录/详情/生命周期适配与嵌入 chat 的装配 |
| user-setting | huangjiajia | [user-setting](04-user-setting.md) | GUI 首次旅程、设置页面及本线缺失的业务契约 |
| org | yaoshengyue | [org](05-org.md) | 组织、文件、链接适配与产品装配 |
| chat | lizhenyu | [chat](06-chat.md) | chat/Lark 的业务契约、统一客户端迁移、发送回执与恢复 |

开发与合入流程固定为：**各线自行开发 → codex-daemon-request 审核固定 SHA → emergent 统筹合入 intg → 整候选验收 → release/v0.5.5.4 → dev**。codex-daemon-request 承接公共线，不接管其他线的实现。公共线作者自己的改动须有非作者独立代码审查，再由 emergent 接收；linyilun 的 agent 工作包也须经过同一审核链，不能凭 coordinator 身份自行验收。

各线提交的是代码/证据/PR，不是合并授权。审核发现问题仍由原线修复；新 head 必须增量复核，旧 PASS 不外推。CI、独立审查与真实浏览器证据各自保留，任何一种不能替代另外两种。

## 4. 并行开发协议

“可并行开发”不等于“无依赖即可合入”。各线可同时建立工作分支、梳理本线契约、迁移薄 adapter、编写测试；公共接口未稳定时不各自发明网络层。

1. coordinator 从固定 release 建立 `intg/gui-live-0554`，所有实现 PR 的 base 均为该分支。每条工作线在自己的短分支开发，不直接向容器推实现。
2. 公共线先提交最小接口快照：generated client 的注入类型、公共连接/错误/phase 约定、新方法 schema 草案及文件归属；经审核后成为跨线引用坐标。快照不是后端已经实现的声明。schema 变化必须版本化告知依赖线。
3. 已有 HTTP 方法的 agent/org/settings adapter 可立即按 `Pick<GeneratedClient, ...>` 重构；新增方法由相应业务线在自己的 request_catalog 子包实现并测试。公共线维护 MethodSpec 核心、registry 公共装配与生成器，按文件登记协调接入。
4. 依赖尚未运行时，只在测试中使用符合冻结契约的替身/Contract Mock；替身不得进入普通产品入口，不算 live PASS。功能线实现可以先完成，合入/最终验收仍等待所依赖的公共能力。
5. 共享文件由公共线维护：`gui/src/app/capabilities.tsx`、`main.tsx`、`App.tsx`、`app/backend/`、通用 client/transport、宿主、`method_spec.py`、`request_catalog/registry.py` 与生成器。各线给出 adapter 导出/注册补丁与测试，公共线统一装配；不得复制一份共享文件另走通道。
6. agent 线负责 `features/agents/`；user-setting 负责 `features/onboarding/`、`features/user-settings/`、`features/agent-provider/` 及相关 platform auth/setup；org 负责 `features/org/`、OrgPage；chat 负责 `features/chat-channels/`、`features/agent-chat/api/`、`adapters/live/` 与 pages/live。AppShell 的共同改动也归公共线统筹，org 提供所需行为及断言。
7. 业务 Spec 子包由对应业务线开发：user-setting 的 setup/identity/maintenance；chat 的 message/send、message/directory 及 adapter 缺口。接口设计/安全审核归 codex-daemon-request，核心 registry/生成器由公共线维护。分支内生成物用生成器产生，合并冲突由公共线重生成，不能手工拼 JSON。
8. base 每次推进后，待合工作包由原作者 merge-forward 最新容器，记录冲突处理、重新验证，交审核后由 emergent 排队合并。禁止 force push、删除有效测试或改 CI 过门。

依赖最短路径是“公共身份/初始化 + 公共 client”；user-setting 的升级、日志等非关键页面不是其他三线开始开发的前置。org/agent/chat 可用已初始化的隔离 home 提前验证，最终仍须通过空 home 的完整旅程。

## 5. 共同验收门与验证命令

用户已授权优先集成上线；以下是完整目标门。工作包、M1/M2 的适用阻断门与可延期项
按 [阶段放行调整](integration-gates.md) 执行，不要求每个小包先完成全部最终验收。

| 门 | 必须提供的证据 |
|---|---|
| G1 静态调用边界 | 产品组件/feature 不直接 fetch、启动 Python、读 env token；网络实现仅公共 transport。确有非业务浏览器资源/授权导航要明确区分。没有 /api/gui/v1/live、auth/setup 业务旁路 |
| G2 空 home | 普通产品页面启动→login/register intent→提供方授权或明确注册不可用→身份/设备→初始化→模型/管理 agent。未登录调用 org/agent/chat 写操作同时被 HTTP/IPC 拒绝 |
| G3 身份/T11 | daemon 不读取 refresh credential、不反向 import shell；bootstrap token 不具正常 owner 权限；旧 v1 parser 不执行 v2 请求、握手不符不监听；HTTP PID/birth/controller/view/receipt 全绑定；跨 home、过期、旧 generation、伪造 principal、CSRF、HYPRIAL_OWNER override 均拒绝 |
| G4 四线真接线 | 每线至少真实读、真实写或真实拒绝；浏览器请求与 daemon receipt/requestId 对应。数据库由 daemon 管理，不能只看 HTTP 200 或 UI toast |
| G5 Mock 唯一开关 | 相同 build、页面、adapter、URL，只有宿主 driver 不同；Contract Mock 来自同版本 Spec/examples。缺场景报 409，绝不 fallback fixture |
| G6 写入不确定性 | 人为断开写请求后的响应，显示 unknown；刷新后查询同 receipt/business key，无重复发送/创建；拒绝臆测回滚 |
| G7 组合/安装态 | 源码 npm 与 wheel `hyprial gui` 协议一致；wheel 无源码/Node/npm/Git 可启动并完成同旅程；退出只清自有子进程 |
| G8 结构/契约/CI | 结构门、模块依赖门、Spec/OpenAPI/client 一致、错误/权限负例、GUI typecheck/build/焦点与全量适用套件；平台失败与产品断言失败分开 |

实施者先跑直接焦点测试，再按改动运行以下已有命令；本文件不是这些命令已经执行的声明：

```bash
uv run --no-project --python 3.12 python scripts/ci/check_structural_limits.py
uv run --no-project --python 3.12 python scripts/ci/check_module_boundaries.py
uv run python scripts/verify/dump_daemon_openapi.py --check
node gui/scripts/generate-reference-client.mjs --check
npm --prefix gui run typecheck
npm --prefix gui test
npm --prefix gui run build
uv run --with libcst==1.9.0 pytest tests/
uv run ruff check .
./contract/run-all.sh --isolated-only
```

具体补测试位置：现有 `tests/shell/request_http/`、`tests/integration/request_http/`、`tests/shell/login/device/`、`tests/shell/login/orchestration/`、`tests/integration/desktop/test_desktop_network.py`；GUI 新增独立子目录 `gui/tests/unified-backend/`（建议新建）覆盖公共协议、冷启动与四线产品入口。目录/文件预算必须执行工具确认；不能直接在已有 7 文件目录加第八个。

现有 first-run/reference/fixture 测试可以保留作组件回归，但不能替代 G2/G4。测试 home/socket/端口/缓存全部隔离，外部 IdP 或真实 provider 不可访问时报告 BLOCKED，不能把 synthetic 测试标为真实登录 PASS。

## 6. 内容分布与使用顺序

先读本文件，再读本线任务文件；公共线的协议细节是所有线的共同约束，不允许各线复制出不同实现。

| 规划内容 | 维护位置 |
|---|---|
| 源码基线、唯一链路、缺方法行为、唯一模式开关、通用验收 | 本文件 |
| 空 home、B0–B6、锁/身份/T11、HTTP 重启、回执、IPC v2、进程树、Mock journey、公共文件与结构预算 | [公共线](02-common.md) |
| 各线完整能力/缺口/文件矩阵与专属 prompt | 各线任务文件 |
| 并行任务、依赖、分支维护、审核交接、合入/发布规则 | [coordinator](01-coordinator.md) |

这套计划完整承载原统一接线方案的实施内容。当前/新增能力均以源码事实和明确标注区分；设计存在不代表代码、CI、真实 IdP、浏览器或 wheel 已完成验收。
