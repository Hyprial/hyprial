# H-01 设计：真实 HTTP 请求入口与 daemon 权威可用目录（R1/R2 修订）

状态：**R2 收尾稿；设计已通过，H-01 实现中（不改已审技术契约）**。
日期：2026-10-30。
容器：`/Users/amyws/projects/request-http-h01`，分支 `feat/http-request-entry`。
PR：**#2146（Draft）**，base `intg/gui-request-http`。
看板：`c08692e3-8b75-4c42-93ad-a442869b167c`（只引用，不改板）。
R1 审查输入：`/Users/amyws/projects/daemon-request-http/docs/notes/http-h01-design-review-r1-2026-10-30.md`
（审查提交 `ef4d2e1223fe402d7d9653f1d0a54f27e7747ad6`；审查工作树 head
`17cfd4d469064123c8d9016651803239eca85835`，2026-10-30）。
R2 审查输入：`/Users/amyws/projects/daemon-request-http/docs/notes/http-h01-design-review-r2-2026-10-30.md`
（审查提交 `f56089d2df76d8871b23fae79f33299effeb566e`，2026-10-30）。
上游计划：[HTTP 消费面集成：H-01 / H-02](http-integration-2026-10-30.md)、
[仓库规则](../../../AGENTS.md)。

本文件只回答 H-01 的**方案、契约与测试设计**。它不实现产品代码，不迁移
`service/host/user/log/config` 等 CLI 命令族，不做 C 的 operationId 通用业务协议，
不接 GUI 页面；H-01 也不自行合入 A（`intg-A` / #2015）到 dev 或修改共享分支。
用户 2026-10-30 的并行开发澄清（`docs/notes/http-parallel-development-correction-2026-10-30.md`）
覆盖了本文件早期“A 已合入 dev 才开始”的开工时序：H-01 在自己分支先 merge-forward
容器，再合并固定 A `4ad16740b38b374c1608bae974db1482e204ae57` 作为开发依赖，
完成基线兼容后立即实现；A 实际合入 dev 后仍须同步真实 dev、独立核对并完成最终
验收。该澄清只改开工时序，不改变下文技术契约与测试面。

本轮 R2 收尾只改四项：audience 必要过滤与摘要、无连接错误的重启/epoch 一致性、
dispatch 按来源/阶段而非 code、CORS/日志/摘要包络/restore 分发位置的文档一致性。
下文保留 R1 主体，R2 改动以“R2”标注。

**2026-10-30 实施框架修订（用户硬要求）**：生产入口使用真实 FastAPI + Uvicorn，
替换原 stdlib `BaseHTTPRequestHandler`/`ThreadingHTTPServer` 监听。路由、认证依赖、
异常转换、CORS、lifespan 和 OpenAPI 使用框架机制；业务方法名单与 JSON Schema 仍
只来自 daemon `daemon.catalog`，不复制。为满足原始 request-target 校验，Uvicorn
使用 httptools protocol 子类捕获原始 target；HTTP parser 层面的畸形 framing
（如重复 `Content-Length`）由 Uvicorn 直接以 400 拒绝，不再进入应用包络。
新增受 Bearer 保护的 `GET /openapi.json` 元数据端点；HTTP 头名大小写不敏感，
Uvicorn 实际发出小写头名。OpenAPI 声明 HTTP Bearer security scheme，并对每个受保护
operation 声明 security requirement；`/healthz` 和预检保持公开。未完成 header 的
连接由 Uvicorn protocol 的 header deadline timer 关闭，晚到 header 不得再得到 200；
ASGI body 读超时仍返回 408。除此之外 §7–§13 的 wire/status/validation-order、身份、
预算和 dispatch 语义不变。

R1 修订的核心变化：

1. **目录来源**从“HTTP 进程本地 `daemon_catalog()` + ping.version”改为
   **运行 daemon 的权威只读 `daemon.catalog`**；本地投影只做对照。
2. **身份绑定**从“按 required `from`/`actor` 猜”改为 **显式声明的 HTTP binding
   元数据**；无身份参数的方法可声明 `none`，未知/不支持模式 fail closed。
3. **本片调用面**固定为 **safe unary read**；写、preview/execute、Idempotency-Key、
   session 绑定都不进入 H-01/H-02。
4. **结果语义**用 `dispatch` 区分“未派发 / 已返回明确结果 / 派发后未知”，
   不再用 HTTP 状态或错误包络断言“未执行”。
5. **契约摘要**覆盖实际公开 HTTP 契约（路径、身份绑定、调用/重试/确认语义、
   schema、错误、包络版本），排除状态/epoch/principal 等动态字段。
6. **状态/HTTP/CORS/测试矩阵**按 R5 自洽，补齐 400/403 顺序、framing 冲突拒绝、
   标准 CORS 头、恢复期 ready 与重启 epoch 行为。

## 1. 结论摘要

H-01 交付一个**独立的本地 HTTP 传输进程**：

- 进程通过 A 的公共 IPC client（`daemon_request_once`）调用真实 daemon，不运行 CLI
  子进程，不复制业务 handler。
- 启动时向运行 daemon 查询 **`daemon.catalog`**，以 daemon 注册与当前可用状态为准；
  HTTP 进程的本地 `daemon_catalog()` 只用于**声明对照**，不是运行事实。
- 只路由 `httpExposed=true`、有显式或默认 no-selector binding、且满足
  `audience` 含 `local-operator`、`mode=unary, mutates=false, retry=safe,
  confirmation=none` 的方法；当前 A 的 `link.*` 只有 `link.list`、`link.stats`
  满足。`link.add`/`link.drop` 即使未来声明 HTTP 也不因 `httpExposed=true`
  自动进入本片，写 profile 留给 H-03。
- 可信身份来自**显式配置**（operator owner + 固定本节点 canonical selector actor）
  与**每进程 capability token**；body 中的 selector 不是身份凭证，未知 binding 不猜。
- 目录状态区分 `available` / `not-ready` / `unsupported` / `disabled`；描述文件存在
  只说明传输入口已启动，`/readyz` 才表示业务可调用；恢复期明确 not-ready。
- 后端重启后按 epoch 重握手：目录声明不变则恢复，声明变化则 `CATALOG_STALE`，
  不热切换已发布契约。
- 验收使用**真实隔离 daemon + 真实 HTTP 进程**，不以 fixture、CLI、GUI stub 或
  已删除的临时日志冒充证据。

## 2. 基线与证据

| 项 | 值 |
| --- | --- |
| 当前分支 | `feat/http-request-entry` |
| 本轮返工起点 | `948921a8279348abac76723c79af24cf8f6c5c9e` |
| R1 审查提交 | `ef4d2e1223fe402d7d9653f1d0a54f27e7747ad6` |
| 本容器计划基线 | `105f4558520098e10e7a65e4d6509f3c4fb4b15d`（#2095 已入 dev） |
| A 只读对象 | `4ad16740b38b374c1608bae974db1482e204ae57`（#2015，**尚未落 dev**） |
| 本分支与 A 的 merge-base | `4965314a9cd134d4cfdad0259748d507e8b53bbe` |
| 产品版本 | `0.5.5.2`（`VERSION`） |
| Python | 3.12.14，经 `uv run` 使用仓库环境 |

A 的事实只从上述 commit 读取。A 合入后，协调者必须把实际 dev 合入本容器、重新固定
SHA/tree，再释放实现节点；本文中的 A 接口、schema 与摘要在实现前要按新 head 复核。

### 2.1 旧准备探针的边界

准备阶段曾在隔离 tmp home/state 上跑过一次性探针：

- 启动 `uv run python -m hyprial.cli daemon run`（pid 86225）；
- `ping` 返回 `version=0.5.5.2`、`nodeId=h01-node`、`owner=h01-owner`、
  `phase=reconciled`；
- 裸 IPC `link.list`/`link.stats` 成功，`link.add` 被 `SENDER_UNRESOLVED` 拒绝；
- 探针按 PID 结束，tmp 目录已删除。

这条证据只证明**基线 daemon + link 查询**可运行，**不**证明 A client、HTTP、
`daemon.catalog`、TS 或本设计已验收；临时日志已删除，不能作为 H-01 HTTP 证据。

## 3. R1–R5 处置索引

| 审查项 | 本轮处置 | 主要位置 |
| --- | --- | --- |
| R1 安装目录冒充运行目录 | 新增 daemon 权威 `daemon.catalog`；本地投影仅对照；状态区分 declared/http-enabled/not-ready；恢复期 ready 与重启 epoch 规则 | §6、§8 |
| R2 参数名推断身份 / audience 当授权 | 显式 `HttpBinding`/`IdentityBinding`；`none` 合法；未知模式拒绝；selector 不是认证 agent；`local-operator in audience` 加回暴露必要过滤 | §4.1、§6.1、§9、§12 |
| R3 写范围与重试结果不一致 | 本片仅 safe unary read；H-02 不做 preview/execute/session/业务幂等；`dispatch` 按来源/阶段三态 | §4、§7.4、§11 |
| R4 摘要未覆盖 HTTP 契约 | 摘要覆盖实际公开 HTTP 契约；动态字段排除；路径/绑定变化测试 | §12 |
| R5 HTTP/测试矩阵不自洽 | 400/403 顺序、拒绝 chunked/冲突 framing、标准 CORS、受保护 GET 预检、claimedIdentity 不回显、uv 示例、全量记录纪律 | §9、§10、§11、§15 |

R2 复审四项收尾（不改 R1 主体，不扩范围）：

| R2 项 | 处置 | 位置 |
| --- | --- | --- |
| audience 必要过滤 | `local-operator in audience` 加回 §4.1；摘要加 `requiredAudience`；internal-only/bound-agent-only 反例 | §4.1、§8.5、§12、§15 |
| 无连接错误的 restart/epoch | 启动 ping/catalog epoch 必须一致；任何新 catalog epoch 变化都走同一重握手；无混合快照；不宣称请求级 fence | §8.1–§8.4、§15 |
| dispatch 按来源/阶段 | 不用 code 反推；本地/connect/restore gate 为 not-dispatched；明确 daemon 响应为 dispatched；timeout/无效/DAEMON_ERROR 为 unknown | §7.4、§11.1–§11.2、§13.2 |
| CORS/日志/摘要/restore meta | 受保护 GET 的 OPTIONS；origin-form 全端点；ready/notReady 一致；无 body/params/token 日志；digest 头权威/目录 body 对拍；daemon.catalog 在业务断言前分发 | §6.2、§7.3–§7.4、§8.3、§10.2–§10.3、§15 |

## 4. 范围：本片只做 safe unary read

### 4.1 H-01 HTTP 暴露条件（全部满足）

一个方法进入 H-01 的 HTTP route，必须同时满足：

1. 在运行 daemon 的 `daemon.catalog` 中 `declared=true`、`httpExposed=true`；
2. 有显式 `httpBinding` 且 `identity.mode` 是 `none` 或
   `local-operator-selector`，或没有显式 binding 而使用默认 no-selector binding
   （`identity.mode=none`）；默认 binding 不推断 caller，也不改写业务 target；
3. `audience` 包含 `local-operator`；这是**暴露必要条件**，不是授权充分条件，
   也不能写成“audience 与 HTTP 无关”；
4. `mode="unary"`；
5. `mutates=false`；
6. `retry="safe"`；
7. `confirmation="none"`；
8. 在 `daemon.catalog` 中 `state` 为 `available` 或 `not-ready`。`not-ready` 只用于
   目录可见性（`callable=false`），保留 route 但调用返回 503，见 §8.2；它不是
   “当前可调用”。

未来方法即使标了 `httpExposed=true`，只要不满足 3–7 或身份模式不受支持，就由 H-01
标为 `unsupported`，不进入目录/路由；不得自动进入名单。必须有合成反例测试。

### 4.2 H-01/H-02 明确不做

- 不实现 `preview`/`execute`、operationId 配对、业务确认流程。
- 不实现 `bound-agent-session`、session fence、agent 身份代理。
- 不实现业务幂等（`Idempotency-Key` 映射、idempotency-key 重试、重放去重）。
- 不迁移 CLI 命令族，不新增 CLI 业务面，不接 GUI 页面，不做 C 的通用业务协议。
- 不把 A 合入本分支，不发布公网服务，不读取生产凭据，不触碰生产 daemon。

`Idempotency-Key` 在本片没有合法用途：HTTP 入口一律拒绝该 header（400
`HTTP_IDEMPOTENCY_KEY_UNSUPPORTED`），H-02 也不发送它。未来写协议另行立项。

## 5. 现状核对与缺口

### 5.1 A 公共接口（H-01 只能消费）

| 公共符号 | 归属 | H-01 用途 |
| --- | --- | --- |
| `kernel.daemon_request_once` | kernel | **唯一调用方式**：一次连接、一帧、一响应，不重试 |
| `kernel.daemon_request` | kernel | 不使用（它的 `DAEMON_RESTORING` 内建等待不适合 HTTP 预算） |
| `kernel.daemon_socket_path(state_dir, env=...)` | kernel | 启动器计算 endpoint；HTTP 进程只接受显式 socket |
| `kernel.RequestPortError` / `kernel.ipc_errors` | kernel | 传输/域错误码 |
| `kernel.parse_agent_uri` / `canonical_user_uri` | kernel | principal 校验 |
| `kernel.MethodSpec` / `export_catalog` / `export_openapi` | kernel | 声明与投影类型 |
| `daemon.daemon_catalog()` | daemon | **本地声明对照**（不是运行事实） |
| `daemon.resolve_node_owner(...)` | daemon | 启动器/测试可选校验 owner；进程以 `ping.owner` 为准 |

禁止 import：`daemon.impl.request_catalog`、`daemon.impl.ipc.*`、`daemon.impl.links.*`、
任何 `daemon.impl.*`。

### 5.2 A 当前缺口（本轮要补）

- `daemon_catalog()`（`registry.py:254`）只是对**本进程** `METHOD_REGISTRY.specs`
  的纯投影；不查询配置的 daemon，不证明同版本同构建、同绑定或当前服务可用。
- `MethodSpec` 没有显式 HTTP 身份/参数绑定元数据；从 `from`/`actor` 名称推断语义
  会把 selector 当成身份，也会把所有方法错误地要求一个身份参数。
- 没有 daemon 权威目录 IPC 方法；HTTP 进程无法知道声明、HTTP-enabled、not-ready
  的区别。
- `link` handler 的 `_service()` 在服务未绑定时抛 `DAEMON_NOT_READY`；仅凭
  `ping.version` 无法提前发现。
- `link.list`/`link.stats` 的 `from` 是 **selector**，不是认证身份；真正写操作才进入
  `_authenticated_message_caller`。HTTP 入口必须把两者分开。

### 5.3 基线事实（探针）

- 隔离 daemon 可在无 org、无网络的情况下启动并服务 `link.list`/`link.stats`
  （`orgAvailable=false`，`links=[]`）。
- 未注册的 actor 可作为查询 selector：探针传 bare `h01-owner`，daemon
  canonicalize 为 `agent:h01-owner:h01-node:h01-owner` 并返回 200；canonical URI
  也直接接受。这不证明它是已认证 agent，也不要求 registry 注册。
- `link.add` 需要注册 sender/session，基线探针得到 `SENDER_UNRESOLVED`；它不在本片
  HTTP 暴露。

## 6. A 侧最小契约扩展（H-01 范围）

### 6.1 显式 HTTP binding 元数据

在 A 的 `kernel/impl/contracts/daemon/method_spec.py` 增加纯数据声明：

```python
HttpIdentityMode = Literal["none", "local-operator-selector"]

@dataclass(frozen=True, slots=True)
class IdentityBinding:
    mode: HttpIdentityMode
    param: str | None = None

@dataclass(frozen=True, slots=True)
class HttpBinding:
    identity: IdentityBinding
    readiness: str | None = None
```

`MethodSpec` 增加字段：

```python
http_binding: HttpBinding | None = None
```

语义：

- `mode="none"`：该方法没有 selector/身份参数；HTTP 层不注入任何字段。
- `mode="local-operator-selector"`：`param` 指向 params schema 中一个 **required
  string** 属性；HTTP 层把它当“受限 selector”：
  - body 缺省 → 注入配置的 `principal.actor`；
  - body 提供字符串且等于 `principal.actor` → 通过；
  - body 提供合法字符串但不等于 → 403 `PRINCIPAL_MISMATCH`；
  - body 提供非字符串 → 400 `INVALID_ARGUMENT`（见 §9 顺序）。
- `mode` 不在上述集合（包括未来的 `bound-agent-session` 在实现前）→
  `MethodSpecError`，不得启动/导出。
- `readiness` 是可选的服务就绪探针键；`None` 表示“绑定即可用”。键必须在 daemon
  装配期 `READINESS_PROBES` 中存在，否则 registry freeze 失败（fail closed）。
- `http_exposed=True` 只声明“有资格进入 HTTP 投影”，不要求同时有
  `http_binding`；`http_exposed=False` 必须为 `None`。无显式 binding 时，通用投影
  使用**默认 no-selector binding**：`identity.mode=none`、`readiness=None`，不注入、
  不猜测任何 caller/actor 参数。该默认绑定同时进入声明 catalog/OpenAPI、声明 digest、
  运行 catalog 和 route，保证同源；只有同时满足 §4.1 的 audience/unary/safe/public
  profile 才形成 route，写方法仍由 H-03 的写 profile 接纳。这样 M 线可先声明资格，
  H 线不需要 M 引入 H 私有 `HttpBinding`，也不会因一个未绑定声明让 registry 装配失败。
- 显式 selector binding 仍严格校验；默认 no-selector binding 不把 `from`/`actor`
  等业务字段解释成 caller，也不改写业务 target。caller 的可信来源仍是服务端配置的
  local-operator principal + capability token / 本地 IPC，daemon 授权继续执行。
- 不得按具体业务方法名（如 `agent.*`）特判白名单；eligibility 与 binding 分离是
  通用 MethodSpec 语义。
- 不把 `from`/`actor` 名字本身当语义；参数名可以是任意合法字段，只要声明了
  binding。参数也可能是查询对象，不能被自动解释为身份。

A 的校验还要保证：

- selector 的 `param` 在 `params_schema.required` 中，且属性 schema 接受 string；
- `http_binding.readiness` 非空字符串（若提供）；
- 现有 `validate_method_specs` 的 preview/execute 校验不受影响；
- `export_catalog` 的每个条目增加 `httpBinding`：HTTP-enabled 方法（显式 binding
  或默认 no-selector binding）为对象；未暴露方法为 `null`（形状固定，便于
  本地/daemon 逐字段对照）：
  ```json
  "httpBinding": {
    "identity": {"mode": "local-operator-selector", "param": "from"},
    "readiness": "link-service"
  }
  ```
- `export_openapi` 的每个 `POST /daemon/request/<method>` 增加
  `x-hyprial-http-binding`（同上 JSON），并继续只在 `http_exposed=true` 时生成 path；
  未暴露方法只在 `x-hyprial-methods` 中列出，不生成 path。
- `contract/daemon-request/openapi.json` 重新生成；`contract/daemon-request/README.md`
  说明 `httpBinding` 是 HTTP 传输绑定，不是授权声明。
- `link.list`/`link.stats` 增加
  `http_binding=HttpBinding(IdentityBinding("local-operator-selector", "from"),
  readiness="link-service")`；`link.add/drop` 不增加 binding（继续不暴露）。

### 6.2 daemon 权威只读目录：`daemon.catalog`

新增一个**只读、内存、无业务副作用**的 IPC 方法：

```text
method: daemon.catalog
owner:  daemon.catalog
mode:   unary
mutates: false
retry:  safe
confirmation: none
audience: ("internal",)
http_exposed: false
errors: (DAEMON_ERROR,)
params: {"type":"object","properties":{},"additionalProperties":false}
```

handler 读取：

- 运行 daemon 的 `METHOD_REGISTRY.specs`（声明与绑定）；
- `app.epoch`、`hyprial.__version__`、restore gate 状态；
- `READINESS_PROBES`（见下）；
- **不**调用任何业务方法，不读取 inbox/links/PAC 等业务状态，不试调用方法。

静态字段直接来自 `export_catalog(METHOD_REGISTRY.specs)`（与本地
`daemon_catalog()` 同一个函数），只追加 `declared=true`、`state`、`callable`、
`reason`、`epoch`、`packageVersion`；不手写第二份 field 投影。这样本地/daemon 的
静态声明天然逐字段一致，动态状态单独计算。

结果（`daemon.catalog` 自己的 result schema 用 `$defs` 固定）：

```json
{
  "schemaVersion": 1,
  "catalogVersion": "1",
  "epoch": "320d65216ad94f27b3db2a3707c4d96d",
  "packageVersion": "0.5.5.2",
  "methods": [
    {
      "name": "link.list",
      "declared": true,
      "httpExposed": true,
      "httpBinding": {
        "identity": {"mode": "local-operator-selector", "param": "from"},
        "readiness": "link-service"
      },
      "state": "available",
      "callable": true,
      "reason": null,
      "mode": "unary",
      "mutates": false,
      "retry": "safe",
      "confirmation": "none",
      "audience": ["local-operator", "bound-agent"],
      "paramsSchema": { "...": "..." },
      "resultSchema": { "...": "..." },
      "errors": [ { "code": "INVALID_ARGUMENT", "description": "", "dataSchema": null } ]
    }
  ]
}
```

`state` 只表达运行可用性，不表达业务授权。计算顺序固定：

1. `httpExposed=false` → `disabled`；
2. restore gate 未开（`app._restore_done` 未 set）→ `not-ready`，
   `reason=RESTORE_IN_PROGRESS`；
3. `httpBinding` 缺失/readiness 键不存在 → `unsupported`；
4. readiness 探针存在且为假 → `not-ready`，`reason=SERVICE_NOT_READY`；
5. 否则 `available`。

| state | 含义 | HTTP 路由 |
| --- | --- | --- |
| `available` | 已声明、已绑定、HTTP-enabled、restore 已开、readiness 探针为真/无需探针 | 可路由；`callable=true`；实际调用仍由 daemon 授权 |
| `not-ready` | 已声明、已绑定、HTTP-enabled，但 restore 未完成或 readiness 探针为假 | 保留 route，`callable=false`，调用由 HTTP 层返回 503 `HTTP_NOT_READY`；不调用 handler |
| `unsupported` | HTTP-enabled 但缺 binding 或 readiness 键不存在 | 不进入目录/路由；`callable=false`；记录原因 |
| `disabled` | `httpExposed=false` | 不进入目录/路由；`callable=false` |

`daemon.catalog` 的 `reason` 只用 `RESTORE_IN_PROGRESS`、`SERVICE_NOT_READY`、
`HTTP_BINDING_MISSING`、`READINESS_PROBE_MISSING`；H-01 的公开目录另外用
`HTTP_CALL_PROFILE_UNSUPPORTED` 表示“HTTP-enabled 但非本片 safe unary read”，
这类方法不出现在公开目录/route。

`daemon.catalog` 必须加入 router 的 restore light methods（与 `ping`/`shutdown` 同级），
并且必须在 restore gate 之后、`_inbox`/`_harnesses`/`_presence` 这些业务断言**之前**
有一个只读 meta 分发分支，只命中 `daemon.catalog`。不能只加名字后仍落到未恢复的
业务断言，也不能把其它业务方法移出 restore gate。该分支只读内存，不触发恢复或业务。
测试：恢复期 `daemon.catalog` 可用；`link.list` 仍 `DAEMON_RESTORING`；不访问
`_inbox`/`_harnesses`/`_presence`。

`READINESS_PROBES` 放在 `daemon/impl/request_catalog/readiness.py`，至少：

```python
READINESS_PROBES = {
    "link-service": lambda app: getattr(app, "_link_service", None) is not None,
}
```

`MethodRegistry.freeze()`/request_catalog 装配必须校验每个
`http_binding.readiness` 键在 `READINESS_PROBES` 中存在；缺失即 `MethodSpecError`，
不得默认 available。

`METHOD_REGISTRY.freeze()` 已经保证“声明集合 == handler 绑定集合”；
`daemon.catalog` 的 `available` 来自运行 daemon 的实际绑定。HTTP 进程不维护/验证
第二份 handler 映射，也不声称能区分“声明相同但二进制构建不同”的 daemon：
- 声明不同 → 启动 `declarationDigest` 对照失败；
- 声明相同但实现不同 → HTTP 以 daemon 实际 handler 的结果为准，这是 transport
  不越权验证业务实现的边界；
- 运行状态不同 → `daemon.catalog` 的 `state`/readiness 与 `/readyz` 负责。
若未来需要构建身份，另加签名/buildId，不在 H-01 伪装成已有能力。

注册与导出：

- `daemon.catalog` 进入 `request_catalog` 的 meta binding（与 `LINK_BINDINGS` 一起
  freeze），不进入 `MIGRATED_FAMILIES` 的旧 router 回退。
- `_IPC_STATS_METHODS`/inventory/contract 测试同步；`daemon.catalog` 在
  `openapi.json` 的 `x-hyprial-methods` 可见，但 `httpExposed=false`，没有 path。
- `daemon.catalog` 本身不读取 `daemon_catalog()` 的 JSON 结果；它从同一个
  `METHOD_REGISTRY` 生成，避免两套声明。

### 6.3 本地投影的角色

`hyprial.daemon.daemon_catalog()` 继续保留，但只用于：

- HTTP 进程启动时计算**声明对照摘要**；
- 类型/契约测试的本地 fixture；
- 发现“同版本不同声明”（不是构建身份；同声明的不同构建由 daemon 实际 handler
  负责，见上）。

它不是运行目录，不能用来判断当前 daemon 可用性、不能替代 `daemon.catalog`、
不能用来生成 HTTP route。HTTP 进程的 route 表只从 `daemon.catalog` 的响应构建。

## 7. HTTP 进程与公共接口

### 7.1 入口与生命周期

```sh
uv run python -m hyprial.shell.impl.request_http serve --config /abs/path/request-http.json
```

- 前台进程，不 daemonize、不 fork、不启动/停止 daemon。
- 只读 `--config` 的 JSON；不读 `HYPRIAL_HOME`、`HARNESS_STATE_DIR`、`HYPRIAL_OWNER`
  来决定后端/身份（启动器可解析，但必须写进配置）。
- stdout 为空；stderr 为 JSON lines；无 token、无 body、无 params。
- 退出码：
  - `0`：SIGTERM/SIGINT 完成 drain；
  - `2`：配置/权限/runtime 校验失败（`CONFIG_INVALID`）；
  - `3`：backend 不可达、版本/身份/目录不一致（`BACKEND_UNAVAILABLE`、
    `VERSION_MISMATCH`、`PRINCIPAL_CONFIG_MISMATCH`、`CATALOG_MISMATCH`、
    `UNSUPPORTED_PRINCIPAL`）；
  - `4`：runtime 目录/锁/端口失败（`RUNTIME_UNAVAILABLE`、`BIND_FAILED`）；
  - `1`：未预期异常。
- 就绪不靠 stdout：`server.json` 是传输就绪契约；`/readyz` 是业务就绪契约。

H-01 不新增 `hyprial http ...` CLI 命令族；模块入口是唯一启动面。若审核要求 CLI
命令，那是新增业务族的独立决定（D12）。

### 7.2 运行时描述文件

`runtime.dir` 必填、绝对路径；目录必须 0700、非 symlink、属主当前 uid。一个 runtime
dir 只允许一个 server：`server.lock` 用 `flock(LOCK_EX|LOCK_NB)` 独占；第二个进程
退出 4，绝不抢占或 unlink 活 listener。

启动完成后原子写 `server.json`（同目录临时文件 → `fsync` → `rename`，0600）：

```json
{
  "schemaVersion": 1,
  "pid": 12345,
  "endpoint": "http://127.0.0.1:54321",
  "token": "<43-char base64url>",
  "httpContractVersion": 1,
  "catalogDigest": "sha256:<64 hex>",
  "declarationDigest": "sha256:<64 hex>",
  "packageVersion": "0.5.5.2",
  "backend": {
    "kind": "local-daemon",
    "socket": "/abs/state/daemon.sock",
    "version": "0.5.5.2",
    "nodeId": "node-1",
    "owner": "owner",
    "epoch": "<daemon epoch>"
  },
  "principal": {
    "kind": "local-operator",
    "owner": "owner",
    "actor": "agent:owner:node-1:actor"
  },
  "startedAt": "2026-10-30T04:00:00Z"
}
```

- `catalogDigest` = 实际公开 HTTP 契约摘要（§12）；`declarationDigest` = 全声明对照
  摘要（§12）。
- `backend.epoch` 是启动握手时的 daemon epoch；重启后可能变化，见 §8.4。
- token 由 `secrets.token_urlsafe(32)` 生成，只写 `server.json`；不写日志、不进 URL、
  不进 catalog、不回显错误。每次 restart 换新。
- 描述文件是能力文件：同 uid 进程本来就能访问 daemon socket，token 不制造更强用户
  隔离；它阻止浏览器跨源页面、其他应用误用和跨 server 串用。
- 清理：SIGTERM/SIGINT 的 finally 删除 `server.json`；崩溃留下的 stale 文件只有在
  确认旧 pid 已死且锁可拿之后才替换；不盲信 stale endpoint。
- `server.json` 写入失败 → 关闭 listener、释放锁、退出 4。

### 7.3 端点

| 方法 | 路径 | 认证 | 语义 |
| --- | --- | --- | --- |
| `GET` | `/healthz` | 无 | 进程存活；只返回 `{"ok":true}` |
| `GET` | `/readyz` | Bearer | 传输 + daemon + 目录 + 方法就绪；503 返回原因/状态 |
| `GET` | `/daemon/request` | Bearer | 当前 HTTP-enabled 方法目录（含 state） |
| `GET` | `/openapi.json` | Bearer | 实际传输路由的 OpenAPI；方法 request/result schema 与目录一致 |
| `POST` | `/daemon/request/<method>` | Bearer | 调用 safe unary read |
| `OPTIONS` | `/readyz` | Origin/Host | 受保护端点的 CORS 预检；不要求 Bearer，不调用 daemon |
| `OPTIONS` | `/daemon/request` | Origin/Host | 带 Authorization 的目录 GET 预检；不调用 daemon |
| `OPTIONS` | `/openapi.json` | Origin/Host | 受保护 OpenAPI GET 预检；不调用 daemon |
| `OPTIONS` | `/daemon/request/<method>` | Origin/Host | 方法 POST 预检；不调用 daemon |
| 其他 | 其他 | — | 404/405 |

- 绑定固定 `127.0.0.1`；不接受 `0.0.0.0`、`::`、局域网地址或 hostname。
- method 必须匹配 A 的 `^[a-z][A-Za-z0-9]*(?:[._-][A-Za-z0-9]+)*$`，且在 route 表；
  未知/未暴露统一 404 `METHOD_NOT_FOUND`。
- 一个连接一个请求，响应 `Connection: close`；不做 keep-alive/pipelining/流式。
- 不做重定向、静态文件、压缩。

### 7.4 响应包络与 `dispatch`

成功（200）：

```json
{
  "schemaVersion": 1,
  "ok": true,
  "method": "link.list",
  "requestId": "http-<uuid4>",
  "dispatch": "dispatched",
  "result": { "ok": true, "actor": "agent:...", "links": [] }
}
```

错误：

```json
{
  "schemaVersion": 1,
  "ok": false,
  "method": "link.list",
  "requestId": "http-<uuid4>",
  "code": "INVALID_ARGUMENT",
  "message": "from must be a string",
  "dispatch": "not-dispatched",
  "data": { "field": "from" }
}
```

`dispatch` 是 H-01 对 R2/R3 的**来源/阶段**分类，不是 error code 分类：

| 值 | 判定来源 | `outcomeUnknown` |
| --- | --- | --- |
| `not-dispatched` | HTTP 层在调用 daemon 前拒绝（认证/路由/预算/schema/framing/目录 stale/busy）、connect 失败、或 restore gate 的契约保证 pre-handler 拒绝 | false |
| `dispatched` | 收到 daemon 的**有效、明确**响应：成功结果或声明过的错误（含 handler 内/参数校验/域拒绝；该标签不声称 handler 执行轨迹） | false |
| `unknown` | 请求已发送但没有有效明确响应：timeout/disconnect/无效响应/协议或结果校验失败/未登记 `DAEMON_ERROR` | true |

同一个 code 可因来源不同而不同：HTTP 本地 schema 的 `INVALID_ARGUMENT` 是
`not-dispatched`；daemon 返回的已声明 `INVALID_ARGUMENT` 是 `dispatched`；
`DAEMON_ERROR`/未知码/结果校验失败是 `unknown`。客户端只看 `dispatch`，不根据
HTTP status 或 code 猜“未执行”；服务端按来源/阶段填值，不按 code 反推。

- `message` 最多 512 字符；传输类错误使用固定通用文案。日志只记 `requestId`、
  method、错误分类和阶段，**不记 body/params/token**；socket/home 等敏感路径不直接
  抄进错误或日志。
- `data` 默认只透传 daemon 声明过的域数据，并按 catalog 的 `errors[].dataSchema`
  校验；`PRINCIPAL_MISMATCH` 只带 `{"field": "<param>"}`，不带 body 值；未声明/
  超限 data 丢弃。
- 所有响应（成功/错误）带 `X-Hyprial-Request-Id` 和
  `X-Hyprial-Catalog-Digest`。`X-Hyprial-Catalog-Digest` 是摘要的权威位置；
  `GET /daemon/request` body 的 `catalogDigest` 必须等于该头；POST 成功/错误 body
  不再重复 digest 字段，客户端以响应头为准。

## 8. daemon 权威目录、状态、ready 与重启

### 8.1 启动握手

1. 解析/校验配置；校验 runtime dir、锁。
2. `daemon_request_once("ping", params={}, socket_path=<config>, timeout=5)` 取
   `version`、`nodeId`、`owner`、`epoch`、`phase`。
3. `daemon_request_once("daemon.catalog", params={}, timeout=5)` 取运行 daemon 的目录；
   校验响应 `schemaVersion=1`、`catalogVersion`、`epoch`、`methods` 结构。
   **`catalog.epoch` 必须等于 `ping.epoch`**。不一致时在 2 秒预算内重取 ping+catalog
   最多 3 次；仍不一致 → 退出 3 `CATALOG_MISMATCH`（日志 reason=`EPOCH_INCONSISTENT`）。
   绝不把不同 epoch 的 ping 与 catalog 拼成已验证结果。
4. 用本地 `daemon_catalog()` 计算**声明对照**：对全部声明方法（不含动态 state）
   计算 `declarationDigest`；再从 daemon 响应的 `methods` 计算同一摘要并比较；
   不一致 → 退出 3 `CATALOG_MISMATCH`（同版本不同声明）。
5. 从 **daemon 响应**构建 HTTP 方法集与 route：
   - 过滤 §4.1 的 8 个条件（含 `local-operator in audience`）；
   - 对每个方法计算 `httpContractDigest`（§12）；
   - `available`/`not-ready` 进入 route；`unsupported`/`disabled` 不进入；
   - 记录 `unsupported` 原因到 stderr（不把写方法自动暴露）。
6. 校验 `ping.version == hyprial.__version__`、`nodeId/owner` 与配置 principal 一致；
   失败 → 退出 3 `VERSION_MISMATCH`/`PRINCIPAL_CONFIG_MISMATCH`。
7. bind `127.0.0.1:port`，生成 token，原子写 `server.json`，开始 accept。

ping/catalog 是两次独立 IPC 往返，**不是原子快照**。启动只验证“这两个快照同 epoch 且
声明一致”；快照之后 daemon 再重启，由 §8.4 的下一次 catalog/readyz 快照发现并
重握手。H-01 不宣称请求级 epoch fence。

### 8.2 目录状态与路由

`state` 来自 daemon 的实时探针：

- `available`：route 存在，调用可进入 handler。
- `not-ready`：route 存在，但调用在派发 daemon 之前由 HTTP 层直接 503
  `HTTP_NOT_READY`（`dispatch=not-dispatched`，带 `reason`），不调用 handler；
  `/readyz` false。若缓存状态仍为 available、实际 handler 抛出 `DAEMON_NOT_READY`，
  则映射为 503 `DAEMON_NOT_READY`（`dispatch=dispatched`），两种来源不混淆。
- `unsupported`/`disabled`：无 route；POST 404。`unsupported` 包含
  HTTP-enabled 但 call profile 不满足本片（写/preview/never-retry 等）或 binding
  缺失/未知；这是 R3 的“不自动进入名单”。
- 目录不是逐请求授权承诺：`available` 只表示入口/服务可用；每次调用仍由 daemon
  校验 schema、session/域权限与业务状态。`audience` 含 `local-operator` 是暴露
  必要条件，不是授权充分条件。

`GET /daemon/request` 每次重新调用 `daemon.catalog`（短 timeout），**先比较 epoch**：
- epoch 与已记录 `backend.epoch` 不同 → 进入 §8.4 的统一重握手路径；
- epoch 相同但声明摘要与启动摘要不一致 → 503 `CATALOG_STALE`，不返回半新目录；
- epoch 相同且摘要一致 → 返回当前 state。

POST 的 state 快照最多缓存 1 秒（`stateRefreshMs=1000`）：过期就先重取
`daemon.catalog`；取不到 → 503 `DAEMON_UNAVAILABLE`（`dispatch=not-dispatched`）；
新快照 epoch 变化 → 先走 §8.4 重握手，再决定是否调用；快照为 `not-ready` → 503
`HTTP_NOT_READY`（`dispatch=not-dispatched`）；`available` → 进入 handler。若 handler
抛 `DAEMON_RESTORING`/`DAEMON_NOT_READY`，按 §11.1 的来源/阶段映射。HTTP 进程不缓存
业务授权，只缓存启动契约摘要与短 TTL 的 state 快照。

### 8.3 `/healthz` 与 `/readyz`

- `/healthz`：进程活着即 200；不访问 daemon，不暴露版本/路径。
- `/readyz`：调用 `ping` + `daemon.catalog`（短 timeout），要求两者 epoch 一致；
  不一致时在 2 秒预算内重取最多 3 次，仍不一致 → 503 `CATALOG_STALE`（false，
  reason=`EPOCH_INCONSISTENT`），不使用混合快照。正常样例：

```json
{
  "ok": true,
  "ready": true,
  "transport": "ready",
  "backend": {
    "available": true,
    "version": "0.5.5.2",
    "nodeId": "node-1",
    "owner": "owner",
    "epoch": "..."
  },
  "catalog": {
    "fresh": true,
    "catalogDigest": "sha256:...",
    "declarationDigest": "sha256:..."
  },
  "methods": [
    {"name": "link.list", "state": "available"},
    {"name": "link.stats", "state": "available"}
  ],
  "notReady": []
}
```

若有一个方法 not-ready，必须返回 503 / `ready=false` / `notReady=["link.stats"]`，
不能出现 `ready=true` 与 `notReady` 同时存在。

`ready` 的全部条件：

1. listener/描述文件就绪（transport=ready）；
2. `ping` 成功；
3. `phase in {"serving","reconciled"}`（`restoring` → false）；
4. `catalog.epoch == ping.epoch`，且声明/HTTP 摘要与启动一致（fresh）；
5. 所有 HTTP-enabled 且本片支持的方法 `state=available`（有 not-ready → false）。

恢复期 `ping` 成功但 `phase=restoring`，所以 `/readyz` 必须 false；不能把“ping 活着”
当业务 ready。描述文件存在只说明传输入口已启动，不代表 ready。

### 8.4 后端重启/epoch 重握手

触发条件有两条，缺一不可：

1. 请求遇到 `DAEMON_UNAVAILABLE`/`DAEMON_DISCONNECTED`/`IPC_TIMEOUT`/无效响应后
   标记 `backendUnknown=true`；
2. **任何新的 `daemon.catalog` 快照发现 `epoch != backend.epoch`**，即使两次成功
   请求之间没有观测到连接错误。

统一重握手（控制面，不重放业务请求）：

1. `ping`（timeout 2s）取 `version/nodeId/owner/epoch`；
2. `daemon.catalog`（timeout 2s）取目录与 `catalog.epoch`；
3. 若 `ping.epoch != catalog.epoch`：在 2 秒预算内重取两者最多 3 次；仍不一致 →
   `catalogStale=true`，所有 POST 返回 503 `CATALOG_STALE`（`dispatch=not-dispatched`，
   reason=`EPOCH_INCONSISTENT`），`/readyz` false；不使用任一混合快照；
4. 若 `ping.version/nodeId/owner` 与配置不符 → 503 `CATALOG_MISMATCH` /
   `PRINCIPAL_CONFIG_MISMATCH`（`dispatch=not-dispatched`），不恢复；
5. 比较 `declarationDigest` 与 `httpContractDigest`：
   - 两者都与启动一致 → 更新 `backend.epoch`/`server.json`，清除 stale/backendUnknown，
     恢复；
   - 任一不同 → `catalogStale=true`，所有 POST 返回 503 `CATALOG_STALE`
     （`dispatch=not-dispatched`），`/readyz` false；不热切换契约，要求重启进程。

- 如果 daemon 在两次成功请求之间重启且没有连接错误，下一次 `GET /daemon/request`、
  `/readyz` 或 1 秒 TTL 的 POST 前置刷新会发现新 epoch，走同一路径。
- 如果重启发生在 ping 与 catalog 之间，两个快照 epoch 不同；重握手有界重试，仍不一致
  就拒绝，不把旧 ping 与新 catalog 拼成“已验证”。
- 快照之后 daemon 再次变化，由 daemon 的真实 schema/域校验和错误决定；H-01 不宣称
  请求级 epoch fence，也不因快照一致就跳过 daemon 校验。
- `ping`/`catalog` 不可达 → `/readyz` 503 `DAEMON_UNAVAILABLE`；server 不退出，
  等待恢复。
- 重启后同一契约恢复、不同契约 stale/拒绝、握手中途重启、无连接错误重启都必须测试。

### 8.5 目录 payload

`GET /daemon/request`：

```json
{
  "schemaVersion": 1,
  "ok": true,
  "httpContractVersion": 1,
  "catalogVersion": "1",
  "catalogDigest": "sha256:<http contract>",
  "declarationDigest": "sha256:<all declarations>",
  "generatedFrom": "daemon.catalog",
  "backend": {
    "kind": "local-daemon",
    "version": "0.5.5.2",
    "nodeId": "node-1",
    "owner": "owner",
    "epoch": "..."
  },
  "principal": {
    "kind": "local-operator",
    "owner": "owner",
    "actor": "agent:owner:node-1:actor"
  },
  "methods": [
    {
      "name": "link.list",
      "path": "/daemon/request/link.list",
      "state": "available",
      "callable": true,
      "reason": null,
      "httpBinding": {
        "identity": {"mode": "local-operator-selector", "param": "from"},
        "readiness": "link-service"
      },
      "mode": "unary",
      "mutates": false,
      "retry": "safe",
      "confirmation": "none",
      "audience": ["local-operator", "bound-agent"],
      "paramsSchema": { "...": "..." },
      "resultSchema": { "...": "..." },
      "errors": [ { "code": "INVALID_ARGUMENT", "description": "", "dataSchema": null } ]
    }
  ]
}
```

- `methods[]` 只含本片支持的 HTTP-enabled 方法（available/not-ready）；`unsupported`/
  `disabled` 不出现。客户端只调用 `callable=true`；`not-ready` 只用于状态展示。
  `callable=false` 的入口即使被直接 POST，也只返回 503 `HTTP_NOT_READY` 或 404，
  不进入 handler。
- `catalogDigest` 在目录 body 与 `X-Hyprial-Catalog-Digest` 响应头中必须相等；
  POST 成功/错误 body 不重复该字段，以响应头为准。
- `state`/`callable`/`reason`/`backend`/`principal` 是动态字段，不进入摘要。
- `audience` 含 `local-operator` 是本片暴露的**必要条件**（route 过滤）；它不是授权
  充分条件，daemon handler 仍做实际 session/域授权。`audience` 缺 `local-operator`
  的 internal-only/bound-agent-only 方法即使 safe read 也不进入 operator 入口。

## 9. 可信身份路径

### 9.1 三个概念分开

1. **operator**：`principal.owner`，必须等于 daemon `ping.owner`；它是配置里的本地
   operator 身份。
2. **selector**：`principal.actor`，必须是本节点 canonical
   `agent:<owner>:<nodeId>:<actor>`；它是 HTTP 绑定 `local-operator-selector` 的固定
   值，是“被选中的本地 actor”，**不是**认证 agent 证明。
3. **agent/session**：`bound-agent-session` 需要 session fence 证明；H-01/H-02 都
   不支持，遇到该 binding 模式 fail closed。

`principal.kind` 只支持 `"local-operator"`。`"agent"`/`"bound-agent"` 启动时拒绝
`UNSUPPORTED_PRINCIPAL`，不静默降级。

### 9.2 显式绑定，不猜参数名

- 只有 `httpBinding.identity.mode="local-operator-selector"` 的方法才注入/校验
  selector；参数名来自 `identity.param`，绝不按 `from`/`actor` 名字推断。
- `mode="none"` 的方法不注入任何字段；这是合法只读方法的长期形态。
- 未知 mode、缺 binding、binding 与 params schema 不一致 → 启动/目录失败，不路由。
- selector 不要求已注册；`link.list`/`link.stats` 的 handler 只 canonicalize 本节点
  URI。配置的 actor 未注册也可能返回 200 + 空 links；这不表示它是已认证 agent。
- 写方法/session 方法不进入本片；`link.add/drop` 的 session fence 没有被 HTTP 绕过。

### 9.3 请求校验顺序（R5 固定）

1. `Host`、`Origin`、`Authorization`、`Content-Type`、大小/framing 检查；
2. body 必须是 JSON object；否则 400；
3. 若 method 有 `local-operator-selector`：
   - body 缺 `param` → 注入 `principal.actor`；
   - body 有 `param` → 保留原值，供 schema 校验；
4. 用方法的 `paramsSchema` 校验（daemon catalog 的同一 schema）：
   - 任何类型/required/额外字段错误 → 400 `INVALID_ARGUMENT`；
   - 错误 message 只含 schema path/字段名，不引用原值；
   - 特别地，`from: 7` 在 schema 阶段是 400；
5. selector 值与 `principal.actor` 比较（仅当 body 提供了合法字符串）：
   - 不等 → 403 `PRINCIPAL_MISMATCH`，**不回显** body 值；
   - `data` 最多 `{"field": "<param>"}`；
6. 调用 daemon；daemon 再次校验 schema 与业务授权。

这样固定了 R5 的“类型非法 400 / 合法字符串冒用 403”，并使 `from:7` 与
`from:"agent:other:..."` 成为两条独立断言。

## 10. 配置、来源与大小预算

### 10.1 配置文件

`--config` 必须是绝对路径常规文件（非 symlink、非 group/world-writable）；未知键、
缺键、类型/越界值一律拒绝。示例：

```json
{
  "schemaVersion": 1,
  "backend": {
    "kind": "local-daemon",
    "socket": "/abs/state/daemon.sock"
  },
  "principal": {
    "kind": "local-operator",
    "owner": "owner",
    "actor": "agent:owner:node-1:actor"
  },
  "listen": { "host": "127.0.0.1", "port": 0 },
  "security": {
    "allowedOrigins": ["http://127.0.0.1:4173"],
    "allowedHosts": ["127.0.0.1", "localhost"]
  },
  "budgets": {
    "requestTimeoutSeconds": 15,
    "headerTimeoutSeconds": 5,
    "idleTimeoutSeconds": 30,
    "maxRequestBytes": 262144,
    "maxResponseBytes": 8388608,
    "maxConcurrentRequests": 8,
    "shutdownDrainSeconds": 5
  },
  "runtime": { "dir": "/abs/state/request-http" }
}
```

必填：`backend.socket`、`principal.owner`、`principal.actor`、`runtime.dir`。

- `backend.kind` 只接受 `"local-daemon"`；socket 绝对路径，不读环境变量猜。
- `principal.actor` 必须 `parse_agent_uri` 可解析的 canonical 四段 URI；bare name、
  `user:`、adapter/peer URI 拒绝。
- `listen.host` 只能是 `127.0.0.1`；port 0..65535，0 为 ephemeral。
- `allowedOrigins` 默认 `[]`；每项必须是 `http(s)://host[:port]` origin，无路径/
  查询/片段/凭据，不能 `*`/`null`。
- `allowedHosts` 默认 `["127.0.0.1","localhost"]`；忽略端口；不支持通配/后缀。
- 配置不含 token/secret/password 键；出现即拒绝。

### 10.2 来源、大小、framing 预算

| 预算 | 值 | 执行点 | 超限行为 |
| --- | --- | --- | --- |
| 绑定地址 | `127.0.0.1` | bind 前 | 配置拒绝，退出 2 |
| Host | allowlist，忽略端口 | 请求头 | 403 `HTTP_HOST_DENIED` |
| Origin | 精确 origin allowlist，默认空 | 请求头 | 403 `HTTP_ORIGIN_DENIED` |
| 认证 | Bearer token，常量时间 | 请求头 | 401 `HTTP_UNAUTHORIZED` |
| request line | 512 B | 解析 | 414 `HTTP_URI_TOO_LONG` |
| headers | 8 KiB 总量、50 个 | 解析后 | 431 `HTTP_HEADERS_TOO_LARGE` |
| body | 256 KiB | `Content-Length` | 413 `HTTP_BODY_TOO_LARGE` |
| content-type | `application/json` | 请求头 | 415 |
| content-encoding | 仅 identity | 请求头 | 415 |
| transfer-encoding | **一律拒绝** | 请求头 | 400 `INVALID_REQUEST` |
| 请求总时长 | 15 s（1..120） | socket + daemon | 408/504 |
| header/body 读超时 | 5 s | socket | 408 |
| 并发 | 8（1..64） | semaphore | 429 `HTTP_BUSY` + `Retry-After: 1` |
| state 快照 | 1 s | POST 前 | 重新取 `daemon.catalog`；失败 503 |
| 响应 | 8 MiB | 序列化后 | 502 `INVALID_RESPONSE` |

framing 规则（R5）：

- POST 必须有且只有一个 `Content-Length`；缺失 → 411 `HTTP_LENGTH_REQUIRED`；
  非数字/负数/超过 256 KiB → 400/413。
- `Transfer-Encoding` 任意值 → 400；`TE`+`CL` 同时出现 → 400；重复/冲突的
  `Content-Length`（如 `1, 2`）→ 400；重复的 `Authorization`/`Origin`/`Host`/
  `Content-Type`/`X-Hyprial-Catalog-Digest` → 400。
- header 读取超时由 Uvicorn httptools protocol 的 timer 在 header 未完成时关闭连接；
  body 读取超时由 ASGI 层返回 408。`idleTimeoutSeconds` 接到 Uvicorn keep-alive
  timeout；响应仍声明 `Connection: close`。
- request-target 必须是 origin-form，且只允许 `/healthz`、`/readyz`、
  `/daemon/request`、`/openapi.json`、`/daemon/request/<method>`；不接受
  absolute URI、`//`、`..`、控制字符、query/fragment；否则 400。不能把
  `/healthz`/`/readyz` 自己也拒绝掉。
- 不手写通用 HTTP parser：使用 FastAPI/Uvicorn；httptools protocol 子类保留原始
  target 供上面的有界校验。重复/冲突 `Content-Length`、`TE`+`CL` 等 parser 级
  framing 错误由 Uvicorn 以 400 拒绝，不伪装成应用 JSON 包络；应用层仍拒绝
  `Transfer-Encoding`、重复/冲突应用可见头和超限大小。
- POST 不接受空 body 的隐式语义：必须显式 `Content-Length: 0`（等价 `{}`）。
  body 必须是 JSON object；`null`/数组/标量 → 400。

### 10.3 CORS（标准头名）

- `allowedOrigins` 为空 → 不下发任何 `Access-Control-*`。
- Origin 精确匹配时：
  - 实际响应（含错误）：`Access-Control-Allow-Origin: <origin>`、`Vary: Origin`；
  - 预检：`Access-Control-Allow-Methods: GET, POST, OPTIONS`、
    `Access-Control-Allow-Headers: Authorization, Content-Type,
    X-Hyprial-Catalog-Digest`、`Access-Control-Max-Age: 600`；
  - `Access-Control-Expose-Headers: X-Hyprial-Request-Id,
    X-Hyprial-Catalog-Digest`，让浏览器读取关联/摘要头；
  - 永不设置 `Access-Control-Allow-Credentials`。
- 预检覆盖 `/readyz`、`/daemon/request`、`/openapi.json`、
  `/daemon/request/<method>`；只校验 Origin/Host，不要求 Bearer，不调用 daemon。
  `/healthz` 是无自定义头的简单 GET，不要求预检。
- 所有响应（含错误）带 `X-Hyprial-Request-Id` 和 `X-Hyprial-Catalog-Digest`；
  `X-Hyprial-Catalog-Digest` 是摘要的权威位置。`GET /daemon/request` body 的
  `catalogDigest` 必须等于该头；POST 成功/错误 body 不带 digest 字段，客户端以响应头
  为准。测试对拍目录 body/header、POST header/目录 digest。
- H-02 的独立 browser fetch 最小验证使用真实浏览器和真实 CORS；没有可用浏览器时
  记 BLOCKED，不用 Node fetch 冒充 CORS 通过。不涉及产品页面接线。

## 11. 错误、dispatch、重试与取消

### 11.1 dispatch 来源与状态映射

`dispatch` 由**来源/阶段**决定，不由 code 决定：

| 来源/阶段 | dispatch | 典型 |
| --- | --- | --- |
| HTTP 本地拒绝 | `not-dispatched` | 认证/Host/Origin/路由/framing/schema/body 超限/目录 stale/busy |
| daemon connect 失败 | `not-dispatched` | `DAEMON_UNAVAILABLE` |
| restore gate pre-handler | `not-dispatched` | `DAEMON_RESTORING` |
| daemon 有效明确响应 | `dispatched` | 成功 result；声明过的 `INVALID_ARGUMENT`/403/`DAEMON_NOT_READY` |
| 已发送但无有效明确响应 | `unknown` | `IPC_TIMEOUT`、`DAEMON_DISCONNECTED`、`INVALID_RESPONSE`、`IPC_RESPONSE_TOO_LARGE`、响应 `VERSION_MISMATCH`、`DAEMON_ERROR`/未知码/结果校验失败 |

同码反例（必须测试）：

- `INVALID_ARGUMENT`：HTTP schema 400 → `not-dispatched`；daemon 返回已声明
  `INVALID_ARGUMENT` → `dispatched`。不能按 code 统一标成一种。
- `DAEMON_NOT_READY`：HTTP 目录快照预拒绝 `HTTP_NOT_READY` → `not-dispatched`；
  daemon 返回 `DAEMON_NOT_READY` → `dispatched`。
- `VERSION_MISMATCH`：启动 ping → `not-dispatched`；响应版本不符 → `unknown`。
- `DAEMON_ERROR`/未知码：daemon 结果校验失败或未声明异常 → `unknown`，不标
  `dispatched`。

状态/可重试映射（dispatch 由上行来源表判定，不在此表重复）：

| code | HTTP | 可重试（仅 safe read + 瞬态许可 + 预算） |
| --- | --- | --- |
| `INVALID_ARGUMENT`、`INVALID_REQUEST`、`INVALID_PARAMS` | 400 | 否 |
| `HTTP_HOST_DENIED`、`HTTP_ORIGIN_DENIED`、`PRINCIPAL_MISMATCH` | 403 | 否 |
| `HTTP_UNAUTHORIZED` | 401 | 否 |
| `METHOD_NOT_FOUND` | 404 | 否 |
| `HTTP_BODY_TOO_LARGE`、`HTTP_URI_TOO_LONG`、`HTTP_HEADERS_TOO_LARGE` | 413/414/431 | 否 |
| `HTTP_UNSUPPORTED_MEDIA_TYPE` | 415 | 否 |
| `HTTP_METHOD_NOT_ALLOWED` | 405 | 否 |
| `HTTP_BUSY` | 429 | 是（退避后） |
| `HTTP_LENGTH_REQUIRED` | 411 | 否 |
| `HTTP_TIMEOUT` | 408 | 否（读 body 超时） |
| `HTTP_NOT_READY` | 503 | 是（safe read；等恢复） |
| `CATALOG_MISMATCH`、`CATALOG_STALE` | 409/503 | 否（需重启/重新握手） |
| `DAEMON_UNAVAILABLE` | 503 | 是（safe read；connect 失败） |
| `DAEMON_RESTORING` | 503 | 是（safe read；等恢复） |
| `DAEMON_NOT_READY` | 503 | 是（safe read；明确的未就绪响应） |
| `VERSION_MISMATCH` | 409 | 否 |
| `CALLER_NOT_AUTHORIZED`、`SENDER_NOT_AUTHENTICATED`、`SENDER_UNRESOLVED`、`TARGET_NOT_AUTHORIZED`、`CHANNEL_NOT_AUTHORIZED`、`ORG_CONTEXT_NOT_AUTHORIZED`、`STALE_SESSION`、`SESSION_SUPERSEDED`、`INVALID_SESSION_SOURCE` | 403 | 否 |
| `DAEMON_ERROR`、未登记 daemon code | 500 | 否 |
| `IPC_TIMEOUT` | 504 | 是（safe read；不证明未执行） |
| `DAEMON_DISCONNECTED` | 503 | 是（safe read；不证明未执行） |
| `INVALID_RESPONSE`、`IPC_RESPONSE_TOO_LARGE` | 502 | 是（safe read） |
| `HTTP_IDEMPOTENCY_KEY_UNSUPPORTED` | 400 | 否 |
| `HTTP_INTERNAL` | 500 | 否 |

- `DAEMON_RESTORING`/`DAEMON_UNAVAILABLE` 是 `not-dispatched`；`DAEMON_NOT_READY`
  是 daemon 的明确响应，`dispatched`。
- `IPC_TIMEOUT`/`DAEMON_DISCONNECTED` 可能发生在 sendall 之后，必须 `unknown`；
  HTTP 504/503 不能证明 daemon 没执行。
- A 的 `MethodRegistry.invoke` 先执行 handler、再 `_refuse_invalid(result)`；因此
  `DAEMON_ERROR`（含 result 校验失败/未声明异常）没有明确结果保证，保守 `unknown`，
  不得因为 HTTP 500 或 code 名称标成 `not-dispatched`。
- HTTP 本地拒绝的 `INVALID_ARGUMENT` 与 daemon 返回的同码必须走不同来源分支；
  不能按 code 归类。
- `METHOD_NOT_FOUND` 若由 HTTP 路由表返回，是 `not-dispatched`；若 daemon 在已有
  route 后返回它，表示目录/版本不一致，按 `CATALOG_STALE` 处理，不重试。
- `HTTP_INTERNAL` 按发生点：daemon 派发前的 server bug 是 `not-dispatched`；
  派发后/响应序列化失败是 `unknown`。
- 所有 `HTTP_*`/`CATALOG_*` code 进入 `contract/http-request/` 冻结清单；代码中
  不允许表外 code。

### 11.2 重试

- server 永不重试；只调用 `daemon_request_once`。
- H-02 默认不重试。可重试必须同时满足：
  1. safe read（`mutates=false && retry="safe"`）；
  2. status/code 在瞬态许可内：`HTTP_BUSY`、`HTTP_NOT_READY`、
     `DAEMON_NOT_READY`、`DAEMON_RESTORING`、`DAEMON_UNAVAILABLE`、
     `DAEMON_DISCONNECTED`、`IPC_TIMEOUT`、`INVALID_RESPONSE`、
     `IPC_RESPONSE_TOO_LARGE`；
  3. 未取消、在总预算内；最多 2 次、指数退避 + jitter、尊重 AbortSignal。
- `dispatch=unknown` 或 `not-dispatched` **本身不是重试许可**；必须同时满足
  safe-read、瞬态许可和预算。`CATALOG_MISMATCH`/`CATALOG_STALE`/`VERSION_MISMATCH`
  不在瞬态许可内，不能重试。
- `dispatch=dispatched` 的明确 4xx 业务拒绝（400/403/404 等）不重试；
  `DAEMON_NOT_READY` 是瞬态例外，允许 safe-read 有界重试。
- `dispatch=unknown` 的 safe read 可重试，但不得声称服务端未执行。
- 写/preview/idempotency/session 不在本片；未来写方法的默认策略必须是 never，
  直到单独协议定义。
- H-01 拒绝 `Idempotency-Key`；H-02 不发送、不实现业务幂等。

### 11.3 取消

- 客户端取消 = abort fetch / 关闭连接；server 无法取消已进入 daemon handler 的调用。
- 写响应失败时记录 `client_disconnected`，丢弃结果，**不回滚**。
- 读方法取消安全，可重试；写方法（未来）取消/断线后 `dispatch=unknown`，
  `outcomeUnknown=true`，不得自动重放。
- `shutdownDrainSeconds` 只等待正在处理的请求；超时关闭 listener，不等待 daemon
  取消（没有取消协议）。

## 12. HTTP 契约摘要

### 12.1 `httpContractDigest`

覆盖**实际公开的 HTTP 契约**：

```text
canonical_json({
  "httpContractVersion": 1,
  "catalogVersion": "1",
  "methods": [
    {
      "name": "...",
      "path": "/daemon/request/<name>",
      "httpBinding": {
        "identity": {"mode": "...", "param": "..."}
      },
      "mode": "unary",
      "mutates": false,
      "retry": "safe",
      "confirmation": "none",
      "requiredAudience": ["local-operator"],
      "paramsSchema": {...},
      "resultSchema": {...},
      "errors": [{"code": "...", "description": "...", "dataSchema": ...}]
    }
  ]
})
```

- methods 按 `name` 排序；只含本片支持的 HTTP-enabled 方法（available/not-ready 都
  进入契约；state 不进入），并且必须 `local-operator in audience`。摘要中的
  `requiredAudience: ["local-operator"]` 是有效暴露要求，不是完整 audience 副本。
- `httpBinding` 只取 `identity`；`readiness` 是运行探针键，不进入公开 HTTP 摘要。
- 排除：`state`、`callable`、`reason`、`readiness`、`epoch`、`packageVersion`、
  `backend`、`principal`、`generatedAt`、`requestId`、token。
- canonical：UTF-8、`sort_keys=True`、`separators=(",",":")`、`ensure_ascii=False`、
  `allow_nan=False`；SHA-256 hex 前缀 `sha256:`。
- `declarationDigest` 覆盖全部声明方法的静态元数据：`name`、`owner`、`httpExposed`、
  `httpBinding`、`mode`、`mutates`、`retry`、`confirmation`、`audience`、
  `paramsSchema`、`resultSchema`、`errors`；排除 `declared`、`state`、`callable`、
  `reason`、`epoch`、`packageVersion`。它用于本地/daemon 对照，不是公开 HTTP
  契约摘要。
- backend epoch/principal 作为运行绑定单独校验（§8.3/§8.4），不进入摘要。

### 12.2 摘要测试

- 改变公开 `path` → `httpContractDigest` 改变。
- 改变 identity mode/param → 改变。
- 改变 readiness 键 → `httpContractDigest` 不变，`declarationDigest` 改变。
- 移除 `local-operator` audience（或改成 internal-only/bound-agent-only）→ 方法
  不进入公开契约，`httpContractDigest` 改变；`declarationDigest` 也改变。
- 改变 mode/mutates/retry/confirmation → 改变。
- 改变 params/result/error schema → 改变。
- 新增未暴露方法 → `httpContractDigest` 不变，`declarationDigest` 改变。
- 新增 HTTP-enabled 写/preview 方法（不在本片）→ 不进入公开契约，摘要不变；
  route 不出现（`unsupported`）。
- 方法顺序变化 → 摘要不变（排序）。
- 改变 state/epoch/packageVersion/principal → 摘要不变。
- 本地投影与 daemon 声明不一致 → `declarationDigest` 不等，启动失败。

## 13. TS 消费契约（H-02 输入，不在本节点实现）

### 13.1 包与边界

- 推荐位置：`gui/src/request-http/`（纯 TS，无 React/DOM 状态依赖，Node 24 和浏览器
  均可）。实现时核对根 `gui/` 的实际 CI；已有接线不重复添加。
- 备选 `clients/request-http/` 只在根 `gui/` CI 无法接通时采用，不能为绕 CI 改名。
- 只依赖 `fetch`、`AbortSignal`、`URL`；不 import React、不写 localStorage、不做
  全局单例。Node 适配器 `node.ts` 读 `server.json`（fs）；浏览器调用方自备
  endpoint/token。
- 类型/方法名从固定版本契约生成，不手写方法白名单；生成脚本 + `--check` 进 CI，
  与 `contract/http-request/` 摘要绑定。

### 13.2 API 形状

```ts
export type CatalogDigest = `sha256:${string}`;

export interface RequestHttpClientOptions {
  readonly endpoint: string;
  readonly token: string;
  readonly catalogDigest: CatalogDigest;
  readonly fetch?: typeof globalThis.fetch;
  readonly requestTimeoutMs?: number;
  readonly signal?: AbortSignal;
}

export interface CallOptions {
  readonly signal?: AbortSignal;
  readonly timeoutMs?: number;
}

export interface RequestHttpClient {
  catalog(): Promise<Readonly<HttpCatalog>>;
  call<M extends HttpMethodName>(
    method: M,
    params: HttpParams<M>,
    options?: CallOptions,
  ): Promise<HttpResult<M>>;
}

export type RequestHttpFailureKind =
  | 'transport' | 'timeout' | 'cancelled' | 'http' | 'domain'
  | 'catalog-mismatch' | 'protocol';

export class RequestHttpError extends Error {
  readonly kind: RequestHttpFailureKind;
  readonly code: string;
  readonly status: number | null;
  readonly requestId: string | null;
  readonly retryable: boolean;
  readonly dispatch: 'not-dispatched' | 'dispatched' | 'unknown';
  readonly outcomeUnknown: boolean;
  readonly data?: unknown;
}
```

- 初始化时 `GET /daemon/request`，用 body 的 `catalogDigest` 与
  `X-Hyprial-Catalog-Digest` 对拍；之后每个 POST 都以响应头 digest 为准。不一致抛
  `catalog-mismatch`，拒绝调用。
- 每个 POST 带 `X-Hyprial-Catalog-Digest`；409 `CATALOG_MISMATCH` 不自动重试。
- `outcomeUnknown` 只由 `dispatch="unknown"` 决定，不由 HTTP status 或 code 推断。
- `retryable` 必须同时满足：safe read（`mutates=false && retry=safe`）、status/code
  在瞬态许可（§11.2）、未取消且在总预算内。`dispatch=unknown`/`not-dispatched`
  本身不是重试许可；默认不重试。
- 不提供 `Idempotency-Key`、preview/execute、operationId、session、业务幂等。
- 不按 error code 猜 `dispatch`；只消费服务端来源/阶段标签。
- 不实现 React Query/SWR 等状态容器；不做页面控制器；GUI 逐功能接线由产品负责。

### 13.3 独立消费者（H-02 验收）

Node 24 脚本读真实 `server.json`，对真实隔离 daemon 调用 `link.list`/`link.stats`，
覆盖：成功、400、403、404、409、503（restore/daemon 停/not-ready）、504（timeout）、
abort/超时、dispatch=unknown。浏览器 CORS 用真实浏览器最小 fetch；无浏览器记
BLOCKED。不以 mock/fixture 代替真实 daemon。

## 14. 模块边界与 CI

### 14.1 Python 布局（≤7 文件/目录，≤1000 行/文件）

```text
src/hyprial/shell/
  impl/
    request_http/
      __init__.py      # serve(), RequestHttpConfig, 错误类型
      config.py        # 配置、校验、server.json、锁
      catalog.py       # daemon.catalog 消费、状态/route、摘要
      errors.py        # HTTP_* 闭集、status/dispatch 映射、脱敏
      app.py           # FastAPI 路由/依赖/异常/CORS/OpenAPI/Uvicorn protocol
      server.py        # daemon 派发、预算/身份/目录、Uvicorn 生命周期
      __main__.py      # argparse serve --config、信号、退出码
```

7 个直接文件；依赖 `shell(4) → daemon(2)/kernel(0)`，只经公共面
`hyprial.daemon`/`hyprial.kernel`；禁止 daemon.impl、shell.impl.cli；不 import
typer/rich；不新增根入口/console script。产品依赖新增并锁定
`fastapi==0.162.0`、`uvicorn==0.54.0`、`httptools==0.9.0`；HTTP 入口模块导入时不
急切加载 FastAPI/Uvicorn（有测试）。

A 侧新增/修改文件（实现节点）：

- `kernel/impl/contracts/daemon/method_spec.py`：HttpBinding/IdentityBinding/校验/导出。
- `daemon/impl/request_catalog/`：meta binding、readiness probes、`daemon.catalog`。
- `daemon/impl/ipc/router.py`：restore light methods 增加 `daemon.catalog`，并在
  业务断言前加入只读 meta 分发分支（只命中 `daemon.catalog`）。
- `contract/daemon-request/openapi.json`/README：重新生成/说明。
- 相关 `tests/daemon/ipc/contracts/`、`tests/tooling/contracts/`。

### 14.2 测试布局

```text
tests/shell/request_http/
  test_config.py
  test_catalog.py
  test_identity.py
  test_errors.py

tests/integration/request_http/
  conftest.py
  test_http_entry_live.py
  test_lifecycle.py
```

每个目录 ≤7 个直接文件；测试 helper 同受限。

### 14.3 CI 与全量记录

- 焦点：
  `uv run --with libcst==1.9.0 pytest tests/shell/request_http tests/integration/request_http -q`
- 结构/边界：
  `uv run --no-project --python 3.12 python scripts/ci/check_structural_limits.py`
  `uv run --no-project --python 3.12 python scripts/ci/check_module_boundaries.py`
- 全量：`uv run --with libcst==1.9.0 pytest tests/`；`uv run ruff check .`。
- 契约：`contract/http-request/`（README、`http-contract.v1.json`、`validate.py`）；
  接入 `contract/run-all.sh --isolated-only`；H-02 TS 消费同一 JSON fixture。
- D5：单一 corpus，Python/TS 都消费，不复制两份。
- 全量纪律：记录命令、退出码、nodeid、平台；**继承的 FAIL、环境 BLOCKED 与本轮
  引入失败分开记录**；不把“本片未修历史失败”写成全量 PASS。未测项写 UNKNOWN/
  BLOCKED，不用 skip 冒充结论。
- D6：当前只承诺 POSIX/macOS 首片；Windows 实测/未测分别报告，不新建平台任务。
- D3：实现时核对根 `gui/` 现有 CI；已有接线不重复，缺口补正反例。

## 15. 真实测试矩阵

### 15.1 单元（无 daemon）

- 配置：缺必填、非 loopback host、相对/共享 runtime dir、通配/`null` origin、
  token/secret 键、0600/0700 失败 → `CONFIG_INVALID`，不启动。
- binding：`none` 合法；selector 参数必须 required string；未知 mode/
  readiness 键缺失 → fail closed；不按 `from`/`actor` 名字推断。合成无业务名
  `http_exposed=true, http_binding=None` 声明必须通过 MethodSpec/registry 装配，
  导出默认 no-selector `httpBinding`（identity none），运行 catalog 为 available，
  且真实隔离 daemon + Uvicorn 的 IPC/HTTP 正例可调用；受限 audience、未暴露、
  写 profile、伪造 caller 字段和显式 selector 越权必须分别被拒绝。绑定但没有
  `http_exposed` 仍须拒绝。
- audience：合成 internal-only、bound-agent-only 的 safe read → 不进入 operator
  route/目录；`local-operator` 是必要条件，移除后 method 消失。
- 目录状态：合成 `available`/`not-ready`/`unsupported`/`disabled`；`unsupported`
  的写/preview 方法不进入 route；`not-ready` 保留 route 但 `callable=false` + 503。
- 摘要：§12.2 全部用例。
- 身份顺序：`from: 7` → 400；`from:"agent:other:..."` → 403；`from` 缺省注入；
  `onBehalfOf`/`sessionRef` → 400；`mode=none` 不注入。
- 错误/dispatch：按来源/阶段测，不按 code 一列到底。至少覆盖：
  本地 schema `INVALID_ARGUMENT` → not-dispatched；daemon 同码 → dispatched；
  本地 `HTTP_NOT_READY` → not-dispatched；daemon `DAEMON_NOT_READY` → dispatched；
  `DAEMON_ERROR`/未知码/结果校验失败 → unknown；`IPC_TIMEOUT`/`DAEMON_DISCONNECTED`
  → unknown，不因 504/503 断言未执行；`claimedIdentity` 不回显。
- framing：TE、TE+CL、重复/冲突 CL、重复安全头、absolute URI、`..`、query/fragment
  → 400；允许的 origin-form 仅 `/healthz`、`/readyz`、`/daemon/request`、
  `/openapi.json`、`/daemon/request/<method>`；parser 级重复 CL 由 Uvicorn 400；
  超限 → 413/414/431。配置 `maxResponseBytes=1024` 时目录序列化后超限 → 502
  `INVALID_RESPONSE`，不得发出 200 大包。
- CORS：错误响应含 allow origin；`/readyz`、`/daemon/request`、`/openapi.json`、
  方法 POST 的预检；
  `Expose-Headers`；`Allow-Credentials` 不出现；目录 body/header digest 对拍，
  POST 以响应头为准。
- 生命周期：0600/0700、原子写、第二实例退出 4、stale 替换、SIGTERM 清理。

### 15.2 真实隔离 daemon + 真实 HTTP

fixture：tmp `home`（settings owner）、`state`（daemon.sock）、`request-http`
（server.json/lock）；env 固定 `HYPRIAL_HOME`/`HARNESS_STATE_DIR`/`HYPRIAL_NODE_ID`，
移除 `HYPRIAL_OWNER`，`ZENOH_*=""`/`PEER_DISCOVERY=0`。

步骤：

1. `uv run python -m hyprial.cli daemon run`，等待 `ping`（记录 pid/version/node/owner/
   epoch/phase）。
2. 显式 config 启动 `uv run python -m hyprial.shell.impl.request_http serve --config`；
   等 `server.json`；校验 endpoint 127.0.0.1、0600、digest、backend.epoch。
3. `GET /healthz` 200；`GET /readyz` 200（phase serving）且 methods available。
4. `GET /daemon/request`：只含 `link.list`/`link.stats`；state、digest、identity
   binding 与 daemon.catalog 一致；`link.add/drop` 不出现。
4a. `GET /openapi.json`：无 Bearer 401；有 Bearer 200；paths 与实际 route 一致；
   每个 POST 的 request schema 等于目录 `paramsSchema`，成功 envelope 的 result
   schema 等于 `resultSchema`；无默认 422、无 `/docs`、无 trailing-slash redirect。
4b. **合成无业务名声明资格**：用 PYTHONPATH `sitecustomize` 只向真实 daemon/HTTP
   子进程注入 `synthetic.echo`（`http_exposed=true, http_binding=None`）及受限
   audience、未暴露、写 profile 变体；`synthetic.echo` 必须经真实 IPC/HTTP 调用成功，
   目录/OpenAPI/route 均为默认 no-selector binding；其余变体和伪造 caller 字段不得
   产生 route，显式 selector 越权仍 403。产品 registry 不被注入。
5. POST `link.list` body `{}` → 200；`result.actor == principal.actor`；
   `orgAvailable=false`（隔离预期）；`link.stats` 同理。
6. 拒绝矩阵：无/错 token 401；伪造 selector 403；`from:7` 400；未知/unexposed 404；
   `Content-Type` 415；超 body 413；digest 头不匹配 409；TE/冲突 framing 400；
   `Idempotency-Key` 400。
7. **权威目录**：用受控 scratch tree（真实 daemon 进程，测试副本只改一个声明）
   制造同版本不同目录；HTTP 进程用基线安装代码运行 → 启动 `CATALOG_MISMATCH`，
   不服务；本地投影不能覆盖该拒绝。
8. **服务未就绪**：用受控 scratch tree（真实 daemon 进程，测试副本只让 link 装配
   返回未绑定）→ `daemon.catalog` state=not-ready，`/readyz` false，POST 由 HTTP 层
   返回 503 `HTTP_NOT_READY`（`dispatch=not-dispatched`）；恢复后 200。再补一例
   handler 在 available 缓存下抛 `DAEMON_NOT_READY` → 503 `dispatch=dispatched`。
9. **恢复期与 meta 分发**：重启 daemon 进入 restoring → `ping` 200 但 `/readyz`
   503/false；恢复期 `daemon.catalog` 可调用并返回 not-ready；`link.list` 仍
   `DAEMON_RESTORING`；确认 `daemon.catalog` 在 `_inbox/_harnesses/_presence` 业务
   断言之前分发，其它方法没有被放宽；恢复后 ready；描述文件仍存在（只说明 transport）。
10. **epoch/重启（含无连接错误）**：HTTP 空闲时重启同契约 daemon，使两次成功请求之间
    没有观测到连接错误；下一次 `GET /daemon/request` 或 `/readyz` 发现新 epoch，
    重握手后 epoch 更新、恢复 200。重启不同契约 → `CATALOG_STALE` 503，不热切换；
    在 ping 与 catalog 之间重启 → 两个快照 epoch 不一致，有界重试或拒绝，不使用混合
    快照；`/readyz` false。
11. **dispatch 来源/阶段**：本地 schema 400 → not-dispatched；daemon 同码 400 →
    dispatched；`DAEMON_ERROR`/未知码/结果校验失败 → unknown；派发后延迟超过 HTTP
    timeout（受控）→ 504 + unknown；客户端不得断言未执行；safe read 可重试但受瞬态
    许可/预算约束。
12. **CORS**：真实浏览器（H-02）从 allowlist origin fetch；`/readyz`、`/daemon/request`
    与方法 POST 的预检；错误响应/暴露头实测；不允许 origin 403；无浏览器 BLOCKED。
13. 关闭/单实例/崩溃：同 §15.1 + daemon 仍可 ping。
14. 隔离断言：socket 在 tmp，不读 `~/.hyprial`，不触碰 Lark/凭据/生产 daemon；
    按 PID 清理。

### 15.3 全量与未测

- 实现节点执行 §14.3 全量；记录继承 FAIL、环境 BLOCKED、本轮引入失败。
- 本设计节点未运行产品测试；以下 NOT RUN/UNKNOWN：所有 H-01 实现用例、A 合入后
  真实 head 复核、contract corpus、`daemon.catalog` 实现、Windows、GUI 页面。
- 旧准备探针不是 HTTP 证据；临时日志已删除，不复用为 PASS。

## 16. D1–D12 处置（R1/R2 裁决）

| # | 处置 | 残留/实现时验证 |
| --- | --- | --- |
| D1 | 显式 `HttpBinding`/`IdentityBinding`；未知不推导；同步 A 导出/校验/openapi | 实现时跑 schema/contract 测试；selector 参数 required string |
| D2 | `daemon.catalog` 为运行权威；`daemon_catalog()` 仅本地对照；重启/epoch 不只靠连接错误触发 | 同版本不同目录、服务未就绪、无连接错误重启、握手中途重启测试 |
| D3 | `gui/src/request-http/` 纯 TS、无 React；核对实际 CI，已有接线不重复 | H-02 时确认根 `gui/` job 与 `ci-plan` 正反例 |
| D4 | 仅 local-operator；H-02 不新增 bound-agent/session | 未支持模式 fail closed 测试 |
| D5 | `contract/http-request/` 单一 corpus，Python/TS 消费，接现有契约入口 | H-02 消费同一 fixture；`run-all.sh` 接入 |
| D6 | POSIX/macOS 首片；Windows 未测，不新建平台任务 | 报告实测/未测平台 |
| D7 | 保留 `HTTP_*`、`CATALOG_MISMATCH`；按语义映射；`dispatch` 按来源/阶段判定，不按 code | 同码不同来源反例 + 状态/瞬态矩阵对拍 |
| D8 | 摘要覆盖有效公开 HTTP 契约（含 `local-operator` 必要 audience）；排除非公开/动态字段 | §12.2 摘要测试 |
| D9 | 本片仅 safe unary read；H-02 不做 preview/execute/幂等/session | 合成写/preview 反例不进入 route |
| D10 | 0600 能力文件 + 0700 目录；同 uid 信任边界；token 不进日志/仓库 | 权限/泄露测试 |
| D11 | 配置 canonical 本地 actor 是受限 selector；不要求注册；不冒称认证 agent | 未注册 selector 200 + 空 links 的明确断言 |
| D12 | 模块入口；命令经 uv；不新增 CLI 业务族 | 不新增 console script/命令族 |

**仍未解决的实现风险（不是方案未决）**：A 实际合入后的 `MethodSpec`/router 代码可能
与只读对象有差异，需在实现前复核并更新本文的精确路径/字段；根 `gui/` CI 的实际
接线需 H-02 时核对；Windows 未测；真实浏览器 CORS 依赖可用浏览器（否则 BLOCKED）。
R2 新增的 audience 过滤、无连接错误重启/epoch、来源/阶段 dispatch、restore meta
分发位置都需要实现测试，不能只靠文档声明。

## 17. 实施顺序与退出条件

1. **本节点（revise-design）**：只改本文档和准备证据；不写产品代码；不改主图旗标。
2. **R1/R2 复审（补充图 `done` 节点；主图 `h01-design-review` 仍由协调者在其当前
   有效请求上单独处理，本节点不置位/不改主图旗标）**：按 R1–R5 与 R2 四项逐项核对；
   通过后由协调者决定主图；不通过则记录剩余问题并保持未置位。
3. **h01-build 开工**：在自己的分支 merge-forward 容器，再合并固定 A
   `4ad16740b...` 作为开发依赖；基线兼容单独成提交并记录双方 source/base/tree、
   冲突处置和焦点测试。A 的实际合入仍由协调者按发布/验收流程负责，不是本节点的
   完成条件，也不得把开发同步声称为 A 已落 dev。
4. **h01-build 实现**：先写失败测试，再实现 A 扩展（`httpBinding`、
   `daemon.catalog`、readiness）与 `shell/impl/request_http/`；按 §14.3/§15 跑焦点、
   live、结构、边界、Ruff、全量、契约；记录继承 FAIL/BLOCKED。
5. **A 实际合入后**：协调者把实际 dev 合入本容器，固定新 SHA/tree；复核 A 的
   `daemon.catalog`、`http_binding`、错误码、restore gate 和 `openapi.json`，
   再做最终独立验收。
6. **h01-review**：独立验收者按 §3/§15 看证据；未测项写 UNKNOWN/BLOCKED。
7. **PR**：同一 #2146、同一分支；保持 Draft 直到实现验收；正文含 Submitted-by、
   Contract-Impact、Skill-Impact、验证/未测；不自行合并/绕保护。

H-01 退出条件（全部满足才 PASS）：

- `daemon.catalog` 权威目录 + 同版本不同目录/服务未就绪/epoch 测试；
- audience `local-operator` 必要过滤 + internal-only/bound-agent-only 反例；
- 真实隔离 daemon + HTTP 的 `link.list`/`link.stats` 200；
- 400/401/403/404/409/413/415/429/503/504/`dispatch` 矩阵；同码不同来源反例；
- 摘要/排序/动态字段/audience 测试；route 与目录一致；
- 恢复期 ready/重启一致性（含无连接错误、握手中途重启）；单实例/关闭/崩溃；
- 结构/边界/Ruff/焦点/全量/契约记录；
- 无 CLI/GUI/C/A 范围混入。

## 18. PR 正文草稿（R2 收尾）

```text
docs(http): close H-01 R2 design corrections

Submitted-by: agent:amy.williams@example.com:devusr-dev:wf-474597cc922e4a0e83aaebf557fa9ae4-971eaed090b4
Contract-Impact: design-only; proposes daemon.catalog + MethodSpec httpBinding and a new contract/http-request corpus for the implementation PR.
Skill-Impact: not-needed: preparation-only design document; no agent operation changes.

R2-1: restore local-operator audience as a necessary exposure filter and include the effective requirement in httpContractDigest.
R2-2: epoch changes trigger the same re-handshake even without an observed connection error; startup/handshake ping+catalog epoch must agree; no mixed snapshots; no request-level epoch fence claim.
R2-3: dispatch is determined by source/phase, not error code; same-code local-vs-daemon examples; DAEMON_ERROR/unknown/result-validation are unknown.
R2-4: CORS preflight for protected GET /readyz and /daemon/request; origin-form allowlist includes all endpoints; ready/notReady sample consistent; no body/params/token logs; digest header authoritative, catalog body matches; daemon.catalog dispatches before _inbox/_harnesses/_presence assertions.

验证: doc-only diff check; R1/R2 review files read; no product tests run.
未测: daemon.catalog/httpBinding implementation, A post-merge recheck, live HTTP, TS, browser CORS, Windows, full suite — NOT RUN/UNKNOWN.
```

## 19. 证据与记录

- 本文档：`docs/design/daemon-request/http-entry-design-2026-10-30.md`。
- R1 判词：`/Users/amyws/projects/daemon-request-http/docs/notes/http-h01-design-review-r1-2026-10-30.md`
  @ `ef4d2e1223fe402d7d9653f1d0a54f27e7747ad6`。
- R2 判词：`/Users/amyws/projects/daemon-request-http/docs/notes/http-h01-design-review-r2-2026-10-30.md`
  @ `f56089d2df76d8871b23fae79f33299effeb566e`。
- A 只读：`4ad16740b38b374c1608bae974db1482e204ae57`；
  关键：`kernel/impl/ipc/client.py`、`kernel/impl/contracts/daemon/method_spec.py`、
  `daemon/impl/request_catalog/registry.py`、`daemon/impl/request_catalog/link.py`、
  `contract/daemon-request/openapi.json`。
- 旧准备探针：§2.1；仅基线 IPC，不是 HTTP 证据；临时目录/日志已删除。
- 本节点实际执行：读取 R1/R2 判词及其 commit；读取设计全文；只读 A 代码；更新本文档；
  `git diff --check`。未运行产品测试、未合 A、未改生产配置、未改主图旗标。
