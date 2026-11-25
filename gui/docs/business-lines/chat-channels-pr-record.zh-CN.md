# chat-channels GUI v1 首批 PR 记录（只读与渠道状态切片）

- 分支：`feat/gui-chat-channels`；目标：`intg/gui-v1`。
- 基线：开始时 `origin/intg/gui-v1` 与工作树同为 `196bf209d9e7cefeeab03f6c7b4dc655e2759baa`。
- 本 PR 按用户拆分指令提交已完成切片；写方法、adapter 管理动作与飞书授权能力留给后续 PR。
- 本记录区分方法已声明、已注册、HTTP 已准入和真实可调用；生成 client 中有方法名不代表网页可调用。

## 固定提交复用

- C01 `message.status`：`5d7463268 → ed742bff2 → bd9b5ac83 → f5c440d91 → 2fbe0394b → 17298ee7b → 21e83b361 → 5d125833e → ce772ce75 → f6751c960 → 35f510110 → 46a906cd7 → da13404bb → ed2d1164f`。中间 merge `778c3ba4f` 的变更已由目标基线与冲突整合覆盖，没有单独摘 merge 提交。
- C02 `message.pending.batch`：`84e3fd609 → 47cb5c6c3 → 91a532034 → 9944395fd → 7b9a54e54 → 45e846265 → e32c56917`，按顺序复用完整链。
- 共享生成物按开工规则重新生成，未手工保留冲突块；重生成提交 `5e066856d`。目录文件数修正提交 `0a89b92d5`。

## 方法清单

| 方法 | MethodSpec/Registry | HTTP 准入 | GUI 消费 | 运行时 |
| --- | --- | --- | --- | --- |
| `message.status` | C01 已声明并绑定真实 handler | IPC-only | 待接 | UNKNOWN |
| `message.pending.batch` | C02 已声明并绑定真实 handler；`mutates=true`、`retry=never` | IPC-only | 待接 | UNKNOWN |
| `message.send.lookup` | 基线声明/真实 handler 复用；本线补兼容身份绑定 | 可信 owner 用户只读已准入，`inbox-service` 就绪门槛 | 生成 client → feature API；组件待接 | 真实 HTTP UNKNOWN |
| `message.history` | 本线声明并绑定原有参与者校验/游标 handler | 可信 owner 用户只读已准入，`inbox-service` 就绪门槛 | 生成 client → feature API → 聊天页历史消息区 | 真实 HTTP 空历史 PASS，非空 live UNKNOWN |
| `message.conversations` | 本线声明并绑定历史持久行查询及参与者身份校验 | 可信 owner 用户只读已准入，`inbox-service` 就绪门槛 | 生成 client → feature API → 聊天页历史会话目录 | 真实 HTTP 空目录 PASS，已结算持久行焦点 PASS |
| `message.query` | 本线声明并绑定原有非消费式 inbox/outbox handler | 可信 owner 用户只读已准入，`inbox-service` 就绪门槛 | 生成 client → feature API；不再用于历史会话发现 | 真实 HTTP PASS |
| `adapter.list` / `adapter.status` | 本线已声明并绑定原有生命周期 handler | local-operator 只读已准入，`lark-service` 就绪门槛 | 生成 client → feature API → 连接页状态 | 真实 daemon/HTTP UNKNOWN |
| `message.send/reply/read_body/ack` | 原 IPC handler 在，尚待逐个迁移 | 未准入 | 现有聊天参考切片仍走旧 live host；迁移待完成 | UNKNOWN |
| 飞书 user-auth / scopes | 缺公开 daemon 方法 | 未准入 | 未连接/未确认/暂不可用 | BLOCKED（后端能力） |

## 已运行检查

- Node `24.20.0`；`npm --prefix gui run typecheck`：PASS。
- `uv run --no-sync python scripts/verify/dump_daemon_openapi.py --write` 与 `npm --prefix gui run client:reference`、`client:reference:check`：PASS（加入渠道和消息查询方法后重生成）。
- 渠道 Python 契约测试：4/4 PASS；HTTP 路由目录测试：17/17 PASS。由 `/home/lenovo` 运行单文件、关闭自动插件加载和字节码写入，规避 `/mnt/d` 上 pytest 收尾阻塞。
- MethodSpec 总目录：27/27 PASS；C01 `message.status` AF_UNIX/身份/参数/错误回归：18/18 PASS；C02 `message.pending.batch` 游标/身份/正文/去重回归：18/18 PASS。
- 消息查询 HTTP 身份适配按 TDD 先见失败，再实现；可选选择器、owner 用户身份注入和防伪造、lookup Registry/准入焦点用例合并回归：61/61 PASS（约 86 秒，期间受 `/mnt/d` I/O 影响）。
- `message.history` TDD：缺失绑定测试先失败；绑定真实 handler 后焦点 1/1 PASS，原 IPC 历史 v1/v2 分页与身份回归 28/28 PASS。真实 HTTP 仍 UNKNOWN。
- 历史方法加入后 MethodSpec、历史绑定与 HTTP 目录组合回归：47/47 PASS；OpenAPI 和生成 client 重生成/一致性检查、Node 24 类型检查、`git diff --check` PASS。
- 提交后补查历史兼容字段：原 handler 接受显式 null 的 `from`、`onBehalfOf`、`limit`、`mode`；新增失败测试复现预拒，修正后 2/2 PASS，OpenAPI/client 重新生成与 `git diff --check` PASS。
- `message.query` 缺失绑定用例先失败；绑定原非消费式读取后，MethodSpec/HTTP 目录与旧 IPC 发件人身份焦点组合回归 48/48 PASS；OpenAPI/client 生成与检查、Node 24 类型检查、`git diff --check` PASS。真实 HTTP 入口仍 UNKNOWN。
- GUI 消息读取 API：Vitest 先因模块缺失失败，最小实现后 3/3 PASS；覆盖生成客户端调用、unknown 保持未知、accepted 原请求一致性、历史会话 ID 与查询视图一致性。Node 24 类型检查 PASS；history/query 已接聊天页，lookup 待接组件。
- 历史会话组件：TypeScript 缺模块先失败；收件箱独有会话测试先失败，再接收/发合并。ext4 同源验证副本 Vitest 6/6 PASS（空/失败/历史/游标/宿主能力注入/入站独有会话）。`/mnt/d` 原位 Vitest 两次因 worker 启动 60s 超时、另一次执行 3 项中 2 项通过且 1 项为测试文本匹配误差；修正后代码结果仅由 ext4 副本证明。生产宿主若未注入可信 request 能力，聊天页显示历史会话目录尚未确认；reference 模式由可信 host 提供 request token。
- ext4 同源验证副本完整 `npm run verify`：PASS（exit 0）；85 个 Vitest 文件、904/904 用例、浏览器检查 5/5、构建/生产拷贝检查及 first-run、safe-r1、j2/j3/j4/j5/j6/j7/j10、linked browser 均通过。初次副本缺根目录 `contract_mock` 时单测 903/904，通过补齐副本后完整重跑为 904/904。本结果不替代 `/mnt/d` 原位结果。
- 聊天控制器 Vitest：11/11 PASS；聊天 Contract Mock Vitest：16/16 PASS。渠道状态组件 Vitest：4/4 PASS（包括未配置/旧在线标志边界）。
- dev-03 浏览器旅程：1440/760/375px，3/3 PASS（草稿、焦点、飞书不可用、渠道状态）。`git diff --check`：PASS。
- 真实隔离 daemon/HTTP/IPC 入口测试：daemon 启动后 180s 无摘要，超时 BLOCKED；不能以路由目录单测替代。外部飞书授权：BLOCKED（后端缺公开能力）。GUI 全量 `verify`：类型检查和生成客户端检查 PASS；Vitest fork worker 在 `/mnt/d` I/O 上五分钟无摘要，终止本次运行（exit 143）；后续阶段未运行，完整验收 BLOCKED。
- 后补隔离 Swagger 启动：`daemon.catalog` 的新 `local-owner-user-selector` 未列入结果 schema，HTTP OpenAPI 投影也未接纳该模式；两处先写失败测试再修正。目录测试 8/8、HTTP 投影测试 29/29 PASS。使用本线源码、隔离 home/socket 和同版本 Python 环境启动 daemon 与 request-http；`/docs` 及本地 Swagger UI 5.17.14 资源 200，受保护的 `/readyz`、`/openapi.json` 200。Chromium 实际输入临时 token 后加载 schema，并显示 `message.history` 和 `adapter.status`：PASS。实时 OpenAPI 含 16 个路径，其中 `adapter.list/status`、`message.history/query/send.lookup` 已列出；C01/C02 仍为 IPC-only，不生成 HTTP 路径。逐方法真实调用、跨节点与外部授权仍 UNKNOWN/BLOCKED。
- Swagger 示例补全：先以失败测试确认 HTTP 投影未消费 MethodSpec 同源示例，且 `link.list/stats`、`message.history/query/send.lookup` 缺开发示例；再加入经 schema 校验的合成示例并投影到命名 request/200 response Example Value。`adapter.list` 等无参数读取的 `{}` 是合法请求，空目录/历史的 `[]` 是真实空态，不伪造运行数据。隔离服务重启后在线 `/openapi.json` 含 16 个路径、12 个业务 POST 方法；这 12 个方法均有请求和 200 响应命名示例，22 对成功请求/响应在线示例通过 JSON Schema 校验。Chromium 直接展开 `message.history`，请求与 200 响应 Example Value 均显示具体合成消息：PASS。通用 `/daemon/request` 与 `/openapi.json` 等动态响应保留通用 schema。离线导出一致性、reference client 生成/检查、Ruff 检查、Node 24 类型检查 PASS；真实聊天写入与外部授权仍 UNKNOWN/BLOCKED。
- OpenAPI 相关 Python 回归首次原位运行 56/59 PASS；3 个失败来自运行中重生成快照，以及旧测试仍只预期 5 条 HTTP 路径并把合法 schema 属性 `pid` 当成运行时值。修正测试后，ext4 同源副本投影与契约合并 37/37 PASS；`/mnt/d` 原位快照/示例焦点 2/2 与离线导出检查 PASS。HTTP 服务端契约焦点 1/1 PASS；原位完整组合未重跑。
- 交审前自检（2026-11-02）：抓取最新 `origin/intg/gui-v1` 后分支落后提交数为 0。结构门先查出 `tests/daemon/ipc/contracts` 有 10 个直接文件；将本线三个消息 HTTP 测试原样移入 `message/` 后，ext4 同源提交快照结构门 PASS（3853 文件、1011 模块、最大直接文件数 7）。模块边界 PASS（1189 文件、0 违规），Ruff 全仓 PASS，OpenAPI 导出 `--check`、Node 24 参考 client 检查 PASS。隔离的 Python 3.13 依赖环境加载同源快照运行 HTTP 投影/请求服务、消息/渠道 MethodSpec、C01/C02 和 OpenAPI 契约组合：182/182 PASS。ext4 同源 GUI 副本完整 `npm run verify`：85 个 Vitest 文件、904/904 用例、浏览器 5/5、构建/生产拷贝、first-run、safe-r1、j2/j3/j4/j5/j6/j7/j10 与 linked browser 全部 PASS（exit 0）；生成示例相关 agent-provider 浏览器用例另跑 1/1 PASS。`/mnt/d` 原位完整验证及全仓 Python `tests/` 仍未完成。
- 隔离真实 daemon/HTTP 自检：`/healthz`、`/readyz`、`/daemon/request`、`/openapi.json` 均 200；`adapter.list`、`message.query`、`message.history`、`message.send.lookup`（未发送记录返回 `unknown`）与 `link.list` 的只读调用均 200。伪造 owner/agent 身份均 403 `PRINCIPAL_MISMATCH` 且 `not-dispatched`；缺必填参数均 400 `INVALID_ARGUMENT` 且 `not-dispatched`；C01/C02 通过 HTTP 请求均 404 `METHOD_NOT_FOUND` 且 `not-dispatched`。未配置的 `adapter.status` 返回 `ADAPTER_NOT_FOUND`、`dispatch=dispatched`，共享 HTTP 状态映射给出 500；独立评审需确认此状态码口径。真实有消息历史、发送写链、跨节点与外部飞书授权未实测。
- 独立方法评审请求：以 `agent:member-a@example.com:member-a-dev:doer` 发给 `agent:member-b@example.com:member-b-dev:codex-daemon-request`，Hyprial messageId `5c19cea7-1342-5e74-841b-fd3440b6d7fa`；发送后查询为 `fetched` / `ACK_RECEIVED`，持有节点 `member-b-dev`。请求覆盖身份、参数/游标、错误码、HTTP 准入、C01/C02 边界及 `ADAPTER_NOT_FOUND` 的 HTTP 状态；评审结论待回。
- 组合 Vitest（chat-controller + chat-contract-mock）在 `/mnt/d` 100s 超时；分别运行两文件均 PASS。渠道状态组件的 fork worker 曾两次启动超时，改用线程池后 4/4 PASS。结构限制脚本在 `/mnt/d` 曾遭遇 I/O 阻塞，后续 ext4 同源快照结果见上。

## 剩余缺口与合入条件

1. 后续 PR 完成 `message.send/reply/read_body/ack` 的 MethodSpec → Registry/真实 handler → OpenAPI → generated client → feature API → 组件迁移，写方法走 H-03；现有发送仍走旧 live host，须保留发送未知结果与草稿。`message.send.lookup` 的生成 API 尚待聊天控制器接入。
2. 按真实能力完成 adapter 启停/装载；飞书授权方法未接通前维持未连接、未确认或暂不可用。
3. 消费 agents 线权威 fixture（目前尚未见该线 fixture），补齐消息目标和 read/ack 等 mock 契约与浏览器旅程。
4. `/mnt/d` 原位全量 `npm --prefix gui run verify` 仍受 I/O 阻塞；ext4 同源副本完整 `verify` 已通过。隔离 HTTP 只读/明确失败场景已实测；真实消息写链、live 数据、跨节点和外部授权仍 UNKNOWN/BLOCKED，副本验收不能替代这些验证。
5. `codex-daemon-request` 对固定 head `36d12dda6` 的复评提出两项 blocking；本 PR 后续提交修正 `message.query` 的 queued-outbox 布尔字段和四处旧分派。member-a 的 doer 在固定 head `3d3bd2a5d` 的评审指出已结算会话无法由 `message.query` 发现；本 PR 追加 `message.conversations` 持久目录能力，仍需评审方复核。`ADAPTER_NOT_FOUND` 的 HTTP 404 修正位于叠加 PR #2285。

### 消息方法 HTTP 身份适配

`message.send.lookup` 及其他消息方法的旧 IPC 参数允许 `from` 或 `actor`；原 handler 的 `_actor()` 优先读取 `actor`。原 HTTP 配置的操作者是 `agent:` URI，而现有 GUI 发送使用 `user:<owner>`；两者不能混用。为保留旧 IPC 格式，本线让 MethodSpec 允许可选的声明选择器，并增加 `local-owner-user-selector`：HTTP 入口由受信任的配置 owner 推导 `user:<owner>`，在 schema 校验前注入缺省 `actor`，并在派发前精确比较任何已给的 `actor`（包括 null）。Lookup 绑定优先的 `actor`，因此伪造 `from` 不影响真实调用身份；伪造 `actor` 被 403 拒绝。隔离真实 HTTP 入口的只读及伪造身份回归已通过，完整 live 消息写链仍待验收。

## 固定 head 复评阻断修正（2026-11-02）

- `message.query` 的 `undeliverableScheme` 由真实 `_outbox_entry_json` 生产为布尔值；MethodSpec 改为 boolean，并重生成 OpenAPI 与 GUI reference client。新增持久队列中两条非空 outbox 的真实处理器回归，分别覆盖 `false`/`true` 且查询不消费队列。
- 删除 `ipc/router.py` 中 `message.query`、`message.history`、`adapter.list`、`adapter.status` 四处 Registry 分派后的旧 shadow 分支；新增重复分派回归。
- 两项先通过失败测试复现，再做最小修正。复评确认尚待 `codex-daemon-request` 回复。
- 修正后原工作树两条 query 测试与 shadow 回归打印 3/3 PASS；`/mnt/d` 的 multiprocessing 收尾等待仍不稳定。相同源文件的 ext4 快盘副本扩展消息/C01/C02/MethodSpec/HTTP 回归 94/94 PASS。OpenAPI `--check`、reference client `--check`、Node typecheck、Ruff 焦点、模块边界门、`git diff --check` PASS；原位结构门因 `/mnt/d` I/O 阻塞中止，快盘副本结构门 PASS。全仓 Python 和新增 live 验收未运行。

## 已结算会话目录修正（2026-11-03）

- 评审在 `3d3bd2a5d` 发现 `message.query` 仅覆盖 pending inbox/outbox，已结算但仍可由 `message.history` 读取的会话不进入 UI 目录。先新增真实持久行测试，在旧实现中得到 `METHOD_NOT_FOUND`，再补 `message.conversations` 的 MethodSpec、Registry、真实身份校验 handler、OpenAPI、生成客户端、feature API 与组件接线。
- 新目录从 inbox、outbox、sender_copies 的会话消息发现参与者可见会话；按最近消息排序和游标分页，已消费或远端交付后的发送方副本仍可发现。游标绑定参与者，跨身份复用返回 `INVALID_ARGUMENT`；读取不消费、不 ACK。`message.query` 仍保持非消费队列查询语义。
- ext4 同源副本的历史/方法目录/HTTP 契约焦点 50/50 PASS，新增远端交付 sender copy 目录测试 1/1 PASS；隔离真实 daemon/HTTP/AF_UNIX 空目录请求 PASS；OpenAPI 导出 `--check`、结构门、模块边界门、Ruff PASS。组件与 feature API 焦点 10/10 PASS。Node 24 的完整 `npm run verify` 在 ext4 同源 GUI 副本 exit 0（85 文件、905/905 Vitest、浏览器 5/5、构建及后续浏览器门）；`/mnt/d` 原位运行在 Vitest 无摘要约四分钟后中止，挂载性能未确认。真实非空 live 历史及跨节点仍 UNKNOWN。
- 扩展回归发现旧 `test_send_lookup.py` 仍断言 `message.send.lookup` 为 HTTP 隐藏方法，与本 PR 已公开的可信 owner 只读 MethodSpec 不符；测试已改为检查真实 HTTP 身份绑定。该失败在修正前复现，组合回归结果另行记录。
- 更广的历史 v2/send-lookup/query 组合 34/34 PASS。全仓 Python `pytest tests/ -q -x` 在 ext4 副本启动，257 项通过、进度约 1% 后因预估运行时间过长主动中止；完整结论 UNKNOWN，不能据此宣称全仓通过。

## 最新 GUI 集成基线同步（2026-11-03）

- `origin/intg/gui-v1` 固定为 `f74436363536bd7ae03f4db7c23b0ce5c35443bd`，包含 Org #2305 与 Agent #2270。先由 merge 提交 `d283487048f83443cb9f666c54394c3b4b04b4f3` 同步到本分支；后续聊天线提交均在该基线上。
- 隔离真实 HTTP 测试发现约 2 秒的 `daemon.catalog` 握手会触发原 2 秒快照超时，调整快照预算为最多 5 秒。HTTP fixture 的 `/readyz` 轮询超时原来发生在 `finally` 之前，会泄漏子进程日志句柄；现把就绪轮询纳入清理作用域并允许短暂超时后重试。
- 最新基线增加了尚无开发示例的 Org/Agent 等 HTTP 方法，因此原“所有 HTTP 方法有示例”断言不再符合跨线现状；本线断言改为逐一检查聊天与渠道六个已公开方法。其他方法的示例覆盖由对应业务线继续补充。
- 基线同步后 `test_daemon_openapi_contract.py` 8/8 PASS；方法/投影/目录组合前 81 项 PASS，随后由上述跨线示例断言失败触发范围修正。隔离真实 daemon/HTTP 的 agent-provider 写、目录/只读和 orgfs 拒绝三个焦点 3/3 PASS；OpenAPI `--check`、reference client `--check`、Ruff 焦点、结构门、模块边界门、PR diff check 均 PASS。
- Node 24 完整 GUI 验证与全仓 Python 结果分别独立记录；非空 live 消息、跨节点仍 UNKNOWN，外部飞书授权 BLOCKED。焦点通过不替代这些验收。
- 最新基线同一工作树的 Node `24.20.0` 完整 `npm --prefix gui run verify` 退出 0：96 文件/1081 Vitest PASS、基础浏览器 5/5 PASS、构建与生产拷贝、first-run、safe-r1、j2/j3/j4/j5/j6/j7/j10、Org lifecycle/rework 和 linked browser 均 PASS。全仓 Python `pytest tests/` 未完成，状态 UNKNOWN。

## 范围拆分与重新送审（2026-11-03）

- 本节是 #2279 的当前范围；上文记录的是此前固定 head 的历史验证。按 daemon-request 与 GUI 集成线的范围判定，本 PR 只保留 GUI 和已有 daemon 方法的 MethodSpec、Registry、HTTP、OpenAPI、generated client 接线。`message.conversations` 的新持久会话服务、查询 SQL、身份游标、声明和测试已移出本 PR；现有 `message.history` 的 int64 缺陷修复也已移至独立 PR #2338（目标 `release/v0.5.5.4`）。
- 会话目录尚无后端能力时，feature API 明确抛出“历史会话目录暂不可用”，组件显示不可用状态；不返回假空目录、不在 GUI 复制数据库查询、不注册空 handler。新增服务需待 `release/v0.5.5.5` 目标分支建立后独立成 PR 并经核心 owner 复核；届时 GUI 还需单独接通该能力。
- 已把当前 `intg/gui-v1@1642919998f70228e22200b91237f4ee55c4ca1a` 合入并解决 4 个冲突：Registry 导入和 session 绑定、daemon catalog 测试导入、MethodSpec 精确方法族、HTTP server 测试导入。生成 OpenAPI/client 重新校验。**传输行为单列：**`server.py` 的 daemon catalog 启动快照等待预算仍为 5/4/3 秒；真实隔离 daemon 首次目录握手在旧 2/1.5/1 秒预算下报 `BACKEND_UNAVAILABLE`，恢复原分支预算后重跑。授权 OpenAPI 测试因新增方法与示例使首次验证超过客户端 5 秒，仅该测试调用改用 10 秒预算，超时仍关闭连接，生产 HTTP 请求超时未改。
- 当前树的焦点 MethodSpec/HTTP catalog/Contract Mock 68/68 PASS，`test_server.py` 22/22 PASS，OpenAPI 导出和 generated client `--check`、Ruff、`git diff --check` PASS。Node 24 完整 `npm --prefix gui run verify` 第二轮退出 0：97 文件/1137 Vitest、基础浏览器 5/5、构建和后续浏览器门均 PASS。首轮单个 agents 用例在全量并发中 5 秒超时，单文件重跑 38/38 PASS；第二轮全量通过。全仓 Python、外部飞书授权和跨节点 live 仍未由本轮验证放行。
- 启动预算恢复后真实隔离 daemon/AF_UNIX/HTTP 的消息身份与非空私密历史 2/2 PASS，已配置 adapter 生命周期真实 HTTP 3/3 PASS；C01/C02 焦点回归 36/36 PASS。完整 live 消息投递与跨节点仍 UNKNOWN。
