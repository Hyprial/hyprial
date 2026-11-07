# App Core host 地址与配置契约（D8 / D9②）

日期：2026-10-17（2026-10-18 修订端点的持有与传递方式）。状态：契约已定稿并开始接线；实现与验收证据在本任务记录中另行列出。
范围：桌面壳启动的包内 Python App Core host 的**非秘密**地址、配置与生命周期边界。
本文件不定义生产端口，不记录任何 token、私钥、设备行内容或环境变量快照。

## 1. 角色与权威

| 角色 | 职责 | 不承担 |
| --- | --- | --- |
| operator | 提供应用级非秘密配置（sender/sessions/地址开关）；生产端口由 operator 在其环境决定 | 不把凭据写进该配置；不由本节点代选生产端点 |
| 桌面壳（starting owner） | 在 daemon-ready 后启动 host；把本轮端点写入进程内受控 Tauri managed state，只由 `app_core_call` 读取；监督并只停止本实例 | 不扫描/杀死同名进程，不 adopt 未知 host，不改生产 daemon |
| 包内 Python host | 按显式配置监听；原子 bind；N-A2 仅在显式注入时启用；清理本实例 socket/暴露 | 不自选端口，不在冲突时换端口，不静默降级为无认证远端 |
| 消费方（Rust core bridge） | 只从 shell 的 managed endpoint 读取；页面只拿到五个操作端口，不拿到地址或 token | 不从浏览器请求读取地址、身份或凭据；不从全局环境变量读取端点 |
| daemon | tailnet 地址、`network.expose`、认证设备与业务权限的权威 | 不成为第二个 durable inbox / turn 权威 |

## 2. 两种端点

### 2.1 本地 IPC（默认）

- 传输：AF_UNIX，newline-delimited JSON，一次连接可承载多帧。
- 目录：应用私有 state 目录，创建时 `0700`；默认 `<app home>/app-core`。
- 文件：`<state dir>/app-core-host.sock`（`app-core-host.sock` 是固定名）。
- 路径长度：启动前检查平台 `sun_path` 上限（macOS 必须 `< 104` 字节）；超限以命名错误失败，不截断路径。
- 唯一性：一个 state 目录只属于一个 host。socket 已存在且仍有进程应答时，新 host **拒绝启动**；只有确认无人应答的 stale 文件才可被替换。
- 端点传递：由 starting owner 在内存中持有，并交给同一进程内的 Rust relay：
  - 端点枚举 `AppCoreEndpoint::{UnixSocket(path), LoopbackTcp{port, token}}` 只存在于壳进程的 Tauri managed state；
  - **不写入 GUI/Node 子进程环境，也不在调用时读取壳进程的全局 `std::env`**；浏览器/WebView 拿不到路径或 token；
  - 独立 Core 构建（无 payload runtime，仅开发组合）在启动时读取一次 operator 显式给出的端点变量，随后同样存入 managed state；PowerPack 发布路径不读取这些变量，也不把外部继承值当作可信地址。

### 2.1.1 Windows 本机 loopback TCP（无 AF_UNIX）

Windows 的 CPython 与 Rust `std` 都没有可用的 AF_UNIX 本机 IPC，PowerPack 因此用同一帧协议的
**本机 loopback TCP 档**，而不是把 host 相位禁用：

- host 以 `--tcp-port 0`（**不带** `--expose-port`）启动，只 bind `127.0.0.1`；端口由 OS 选择，
  readiness 回报实际 `tcpHost`/`tcpPort`；不调用 `network.expose`，不监听非 loopback 地址。
- 为区分本机其它进程，starting owner 生成本轮随机 request token，只放进 host 子进程环境
  （`HYPRIAL_APP_CORE_REQUEST_TOKEN`）与 managed endpoint；Rust relay 每帧附带 `token`，host 在
  分发前校验，缺失/不匹配返回 definitive `unauthorized`。页面永远拿不到 token。
- 该 token 是传输层的本机能力，不是 N-A2 身份信封，也不改变五操作或发送者权威；把 loopback
  TCP 当远端暴露仍被 `--expose-port` 的远端档与 N-A2 gate 约束，本 head 继续 fail-closed。
- Windows 真机运行验收属于 §7/§9 的 NOT_RUN 项，不在 Linux 上以 cfg 推断“已通过”。

### 2.2 远端 expose（默认关闭）

两档语义：**默认本机档**（AF_UNIX / loopback，远端关闭）与 **显式远端档**（operator 显式
`remoteExpose=on` 且给出 `exposePort`）。两档中 host 自身都只绑定 `127.0.0.1:<tcpPort>`
（`tcpPort` 默认 `0`，由 OS 选择 ephemeral port），**不直接绑定 tailnet 或任何外部地址，
也不接受外部地址输入**；外部可达性只由 daemon 的 `network.expose` 建立，因此不存在
`0.0.0.0`/公网直绑路径，也不另造绕过 daemon 的 tailnet listener。未显式开启时不得调用
`network.expose`，不得监听非 loopback 地址。

显式远端档：
- 通过 daemon 已存在的 `network.expose` 暴露 `exposePort`，请求只含 `port`、
  `target=tcp:127.0.0.1:<effective>`、`proxyProtocol=none`；回答必须回显这三项。
- `exposePort` 必须显式给出（`1..65535`），无默认值，不做“下一个可用端口”搜索。
- **外部地址/节点身份由 daemon/sidecar 权威确认**；本配置只给端口，不自报或另选 tailnet 地址。
  当前 `network.expose` 接口只确认 exposure（port/target/proxy），不返回外部地址，也不允许
  host 选择/校验节点地址——这是明确的**接口缺口**，如实记录，不臆造确认能力。客户端侧必须
  使用显式配置的已确认端点，并在可用时通过 `network.peer-key` 一类解析校验目标节点身份；
  错误节点/地址不匹配必须拒绝。客户端侧验收在本 head 为 NOT_RUN（无移动端实现）。
- expose 成功前不得宣告 ready；expose 失败必须停止 loopback listener，且不留下半开暴露。
- 停止时 `network.unexpose` 必须是清理路径的一部分；cleanup 失败单独命名，不伪装成成功。
- 认证门：N-A2 gate 是进程内显式注入对象，不是序列化配置。**壳路径**在没有显式认证组合时
  对 `remoteExpose=true` 直接 fail-closed（`APP_CORE_REMOTE_UNSUPPORTED`），不提供匿名降级、
  关闭证书校验或把长期 bearer 放进 URL 的开关。Python 远端 facade 仍保留旧路径
  （`serve_host(auth_gate=None)`），只允许在隔离测试或显式注入 gate 时使用远端；生产启用前
  必须注入 N-A2，并先补齐 §8.3 的缺口。若传输无法保护认证数据，该项保持关闭并记录所需
  加密传输，而不是降低认证。

### 2.3 移动/远端消费方

- 移动端/远程 Core 必须使用 operator 显式配置的 `host:port` 端点；没有 `localhost` 隐式默认，也不做 mDNS/文件系统发现。
- 端点只进入原生网络桥/transport 驱动，不进入 URL 查询串、页面存储或日志。
- `tailnet` 连接必须经过 N-A2 认证门；网络成员资格不是业务授权。未认证远端状态为 unavailable，不是降级连接。

## 3. 配置来源与优先级

优先级固定为：

```
显式环境变量 > 应用级持久配置 > 安全默认
```

### 3.1 环境变量（operator 可设，均非秘密）

| 变量 | 取值 | 说明 |
| --- | --- | --- |
| `HYPRIAL_APP_CORE_CONFIG` | 绝对路径 | 应用级配置文件位置；默认 `<app home>/app-core.json` |
| `HYPRIAL_APP_CORE_STATE_DIR` | 绝对路径 | 本地 IPC state 目录；默认 `<app home>/app-core` |
| `HYPRIAL_APP_CORE_REMOTE` | `on` / `off` | 远端 expose 开关；缺省 `off`，其他值报错 |
| `HYPRIAL_APP_CORE_TCP_PORT` | `0..65535` 整数 | 远端 loopback 端口；缺省 `0`；Windows 本机档固定用 `0`，不读该值 |
| `HYPRIAL_APP_CORE_EXPOSE_PORT` | `1..65535` 整数 | 远端 tailnet 暴露端口；远端开启时必填 |

不接受带空白的布尔值、`true/1/yes` 等别名或越界端口；非法值必须报出**变量名与约束**，不回退默认值。

独立 Core 构建（无 PowerPack runtime 的开发组合）额外只读一次：

| 变量 | 取值 | 说明 |
| --- | --- | --- |
| `HYPRIAL_APP_CORE_SOCKET` | 绝对路径 | 该构建要连接的 AF_UNIX host 端点；不传给任何子进程 |
| `HYPRIAL_APP_CORE_LOCAL_TCP_PORT` | `1..65535` 整数 | 该构建在无 AF_UNIX 平台连接的本机 loopback 端口 |
| `HYPRIAL_APP_CORE_REQUEST_TOKEN` | 非空字符串 | 与主机启动时使用的本机 request token 配对 |

它们不是 PowerPack 发布的 operator 输入；PowerPack 的端点只来自自己启动并握手的 host。

### 3.2 应用级持久配置（非秘密 JSON）

默认路径 `<app home>/app-core.json`。正常首启时壳按 §3.2.1 安全生成最小文档；
需要真实目标、会话绑定或地址开关时由 operator/组合根事先提供（既有文件永不重写）：

```json
{
  "sender": "user:<owner>",
  "sessions": {
    "<app session id>": {
      "targets": ["<agent uri>"],
      "conversationId": "<daemon conversation id>"
    }
  },
  "address": {
    "stateDir": "/absolute/app-private/state",
    "remoteExpose": false,
    "tcpPort": 0,
    "exposePort": null
  }
}
```

- `sender`/`sessions` 是 adapter 组合根契约，由 Python host 校验；本文件的地址解析不重写它们。
- `sender` 的两种合法形状（由 daemon 的身份边界决定，host 不代选）：
  - `user:<owner>`：本地 host 使用 daemon 私有 socket 的 operator 边界（`owner` 必须是本节点
    owner）。发送、历史读取与 worker 回复的参与者都是该 operator。桌面壳不持有 harness
    session，这是默认形状。
  - `agent:<owner>:<machine>:<actor>` **加**顶层 `sessionRef`：仅当 host 实际持有 daemon
    签发的 worker/交互 sessionRef 时才成立。
  - **只写 `agent:` 而不带 `sessionRef` 的 claim 会被 `message.history` 以
    `SENDER_NOT_AUTHENTICATED` 拒绝**；`message.send` 在 observe 兼容窗口内可能被接受，
    但桌面聊天随后读不到历史，因此组合根不得提供该形状。
- `address` 只接受上述四个键；未知键报错，避免拼写错误被静默忽略。
- `stateDir` 必须是绝对路径；相对路径报错。
- `remoteExpose` 必须是 JSON bool；`tcpPort`/`exposePort` 必须是整数（bool 不算整数）。
- 配置文件缺失时，地址默认仍为“本地、远端关闭”。默认路径由壳在正常首启安全生成，
  显式路径与身份未知时 fail-closed；不得用伪造 sender/session/目标让 GUI 假装可用。

#### 3.2.1 正常首启的安全生成

正常交互启动先经过账户门：只有账户 worker 的 `ready` 投影（已认证凭据、设备就绪、
无 sidecar 错误）成立时，壳才把该非秘密 owner 交给 App Core 启动阶段。

- 仅当**默认路径**缺失时生成；内容严格为 `{"sender":"user:<owner>","sessions":{}}`。
  不带 `address`（本地、远端关闭），不带任何 target、conversationId 或 sessionRef。
- `owner` 来自上述可信登录态，不来自页面、CLI 参数或 `HYPRIAL_OWNER`；必须是
  非空、无首尾空白、不含 `:` 的规范用户 owner。
- 使用 `create_new` 排他创建并 `flush`/`sync`；Unix 权限 `0600`。已存在字节永不重写、
  永不重新归属；并发创建失败时保留胜者内容。
- 已有但损坏的配置报 `APP_CORE_CONFIG` 并原样保留；未登录/身份未知报
  `APP_CORE_IDENTITY_UNKNOWN`；显式 `HYPRIAL_APP_CORE_CONFIG` 指向的缺失路径报
  `APP_CORE_CONFIG_MISSING`，壳从不代为创建。
- 空 `sessions` 表示“已连接、暂无绑定”，不是聊天完成，也不触发任何发现。

### 3.3 安全默认

| 项 | 默认 |
| --- | --- |
| 配置文件 | `<app home>/app-core.json` |
| state 目录 | `<app home>/app-core`（`0700`） |
| 本地 socket | `<app home>/app-core/app-core-host.sock` |
| 远端 expose | `false` |
| loopback TCP | `0`（仅远端开启时使用） |
| expose 端口 | 无（远端开启时必须显式提供） |

## 4. 冲突与失败语义

### 4.1 本地 socket

- 已有进程在 socket 上应答：新 host 报 `already serving`（命名错误），**不 unlink、不接管、不杀进程**。
- 文件存在但无人应答：允许替换 stale 文件；替换与 bind 由 OS 原子完成。
- 启动失败/正常停止：只删除本实例创建的 socket；不按进程名或全局查找清理。

### 4.2 TCP / expose

- `bind(127.0.0.1:tcpPort)` 返回 `EADDRINUSE` 时：立即 fail-closed，错误包含地址与端口；**不杀占用者、不静默换端口、不改用其他地址**。
- `tcpPort=0` 由 OS 选择；实际端口由 `on_bound` 回报给 expose，绝不在客户端假设为某个固定值。
- `network.expose` 拒绝/超时/回答与请求不一致：命名 `expose` 阶段错误，关闭已绑定 listener，不宣告 ready。
- `network.unexpose` 失败：命名 `cleanup` 阶段错误；进程退出状态仍为失败（或按现有契约报告 cleanup 失败），不伪装成功。
- 已有同端口暴露由 daemon 决定；本壳不先 `unexpose` 别人的暴露来“腾位置”。

## 5. 生命周期（与 A 的接线点）

1. daemon-ready 后，桌面壳才进入独立的 `starting-app-core` 相位。
2. 壳解析地址配置、创建私有 state 目录、以受监督子进程启动包内 Python host；子进程环境包含 daemon socket 路径；端点路径/端口与（Windows 本机档的）request token 只进入子进程环境，不进入任何 GUI 子进程。
3. host bind 成功后通过 loopback readiness IPC 回报 token/端点/自检结果；Unix 回报 `socket`，Windows 本机档回报 `tcpHost`/`tcpPort`；壳同时核对子进程仍存活且回报的端点就是本轮拥有的端点。
4. 只有握手成功才发出 `host-ready`：壳把端点写入 Tauri managed state，主窗口直接加载静态 v11 Core 页（`ui/core/index.html`）与注入的五操作端口；**不再启动旧 Node GUI product 子进程，也不把 socket 注入它的环境**。握手失败/超时（有界）则清理本轮 host，启动 fail-closed，不展示“可用”的 GUI。
5. 停止顺序：App Core host → daemon（只停止本 runtime 拥有的子进程）；host 正常退出时清理自己的 socket，异常退出留下 stale 文件由下一次安全替换。
6. shell 异常退出：host 监视启动 owner PID（非进程名），检测到 owner 消失后走 SIGTERM 清理路径；不依赖 `killall`/`pkill`。Windows 没有 POSIX 的 owner watchdog，由壳的 `stop`/子进程句柄兜底，真机行为见 §9。
7. 不 adopt 已存在的 host；不包含第二个 durable inbox/业务权威；不重启共享生产 daemon。

## 6. 非秘密 operator 清单

operator 只需提供：

- 需要真实目标/绑定时，应用级 `app-core.json` 的 `sender`、`sessions` 与（按需）`address`；
  正常首启无需预先提供，壳只为已登录本机 owner 生成最小的本地 `sender` + 空 `sessions`；
- 需要远端时显式设置 `HYPRIAL_APP_CORE_REMOTE=on` 与 `HYPRIAL_APP_CORE_EXPOSE_PORT`；
- 实际生产端口在其环境中决定，本节点不写入、不修改任何生产端点。

禁止进入该配置/日志/PR 的内容：token、私钥、完整认证配置、设备密钥、浏览器可读凭据、生产凭据。

## 7. 验收矩阵（本契约）

| 场景 | 期望 |
| --- | --- |
| 无任何配置，已登录正常首启 | 默认路径排他生成 `sender=user:<owner>`、空 `sessions`、远端 off；无 target；已有字节不重写 |
| 无任何配置，但身份未知（smoke/probe 未登录） | `APP_CORE_IDENTITY_UNKNOWN`；不生成文件；无匿名远端 |
| 默认配置损坏 | `APP_CORE_CONFIG`；原字节保留；host 不启动 |
| 显式 `HYPRIAL_APP_CORE_CONFIG` 缺失 | `APP_CORE_CONFIG_MISSING`；壳从不创建显式路径 |
| 配置文件 + 默认地址 | 远端 off；socket 在 app 私有 state；端点只进入 managed state；不再有 GUI 子进程，页面拿不到 socket/token |
| env 覆盖 config | env 值生效；非法 env 报出变量名，不回退 |
| Windows 本机档（模拟） | `--tcp-port 0` 无 expose；只 bind `127.0.0.1`；readiness 回报实际端口；缺/错 request token definitive 拒绝 |
| Windows 真机启动 | 实机执行本机 loopback TCP + 五操作 + UI 连接；本 head 为 NOT_RUN |
| remote off + expose 端口 | 不调用 `network.expose`，不监听非 loopback |
| remote on 缺 exposePort | 配置错误，host 不启动 |
| remote on 无显式认证 | fail-closed，远端保持关闭 |
| 占用 AF_UNIX | 第二实例拒绝，不 steal；原实例继续服务 |
| stale AF_UNIX | 安全替换；正常停止只删本实例 socket |
| 占用 TCP 端口 | 命名冲突失败，不杀占用者、不换端口 |
| expose 失败 | 关闭 listener；不宣告 ready；无半开暴露 |
| 正常停止 | unexpose（如开启）、关连接、移除 socket、只停本实例 |
| host 崩溃/握手超时 | 有界失败；GUI 不进入可用；daemon 不被误停 |
| 旧 Node GUI product | 发布路径不再启动；主窗口加载 v11 Core 页；smoke 不再声明 mock driver |
| shell 异常退出 | host 通过 owner PID 监视退出并清理 |
| 移动/远端 | 显式端点 + N-A2；无隐式 localhost/mDNS；认证数据不进入 URL |

## 8. N-A2 当前 head 语义与验收

### 8.1 定义（与冻结 v0.1 对齐）

- 信封恰好 `{deviceId, ts, nonce, mac}`；`mac = HMAC-SHA256(key, ts|nonce|spaceId|operation|digest)`，
  `spaceId := params.sessionId`（sessionless 操作为空串），`digest` 是去掉 `auth` 后整帧
  canonical JSON（sort_keys、紧凑分隔符、UTF-8）的 SHA-256 hex。
- 请求密钥只来自 sidecar 的 `derive(role="server", peer=clientPublic)`（X25519 + HKDF-SHA256）；
  本模块从不重做 DH，也不持久化派生密钥之外的东西。
- `author` 在请求体内：缺省取设备行 owner；存在但与 owner 不同则 `author-device-mismatch`。
- `ts` 是整数秒，接受 ±120 s（含边界）；越界 `stale-request`。nonce 只在 MAC 验证通过后记入，
  重放 `replayed-request`；容量上限是本地加固，不是协议语义。
- 授权矩阵：设备行 owner 必须是该 session 的绑定 target；sessionless 操作不得夹带 `sessionId`；
  无矩阵行或无绑定均 `auth-not-authorized`。网络成员资格不等于业务授权。
- 设备行由 host 侧权威来源解析；行不存在 → `unknown-device`。**v0.1 没有独立 revoked 状态或
  reason**：撤销的当前表示是“来源不再返回该行”，同样落到 `unknown-device`；本地缓存行不得继续服务。
- 默认关闭、仅显式开启：`AuthGate` 是进程内注入对象，不是序列化开关。CLI `main` 永不注入；
  桌面 AF_UNIX 拒绝 gate；Rust 壳在没有显式认证组合时拒绝远端（`APP_CORE_REMOTE_UNSUPPORTED`）。
  gate 注入后，缺信封、派生不可用、未知/撤销设备、过期与重放全部拒绝，不存在匿名降级。

### 8.2 验收结果（本 head）

| 用例 | 期望 | 结果 |
| --- | --- | --- |
| 缺失信封 | `auth-envelope-missing` / `device-authentication-required` | PASS（含 TCP facade 端到端） |
| 过期（两侧越界 ±121 s） | `auth-timestamp-skew` / `stale-request` | PASS |
| 撤销（来源不再返回设备行） | `device-unknown` / `unknown-device` | PASS（行删除即撤销） |
| 未知设备 / MAC 篡改 / 重放 / author 不匹配 | 对应冻结 reason；definitive | PASS |
| 越权 session / sessionless 夹带 / 无矩阵行 | `auth-not-authorized` | PASS |
| 无 gate 旧路径；显式 gate 才启用；桌面拒绝 gate；CLI 默认 None | 无隐式开启、无匿名降级 | PASS |
| 生产 derive/设备行来源与真实远端启用 | 可审计注入 + 认证数据受保护 | BLOCKED / NOT_RUN（见 §8.3） |

### 8.3 未决冲突与选项（保持远端关闭）

1. **目标 host 身份**：v0.1 信封不含目标 host 字段，`DeviceRow` 只消费 owner/clientPublic；
   当前 verifier 没有校验“该设备行属于本 host”（例如 serverPublic 与本机一致）。选项：
   (a) 由身份 owner 提供 host 绑定的权威行来源，并在注入 seam 校验；(b) 下一版契约增加目标 host
   字段。两者都未冻结，不得自造。
2. **撤销语义**：没有独立 reason，也没有生产撤销来源。当前只以“行删除”表达；若身份 owner 需要
   显式撤销谓词，需先落契约与 reason，再接线。
3. **传输**：远端只经 daemon 的 tailnet `network.expose`；host 只 bind `127.0.0.1`，认证数据不进
   URL/日志/页面存储。生产 tailnet 端到端未在本 head 验证；在确认加密传输与来源前保持 off。
4. **外部地址确认**：`network.expose` 只接受/回显 port、loopback target 与 proxyProtocol，
   不返回也不校验外部 tailnet 地址或节点身份；host 无法独自满足“对外地址必须是本节点已确认
   tailnet 地址”的验收。选项：(a) daemon 增加返回已确认地址/节点身份的权威字段；
   (b) 客户端用 `network.peer-key` 一类解析自行确认并在不匹配时拒绝。均未在本 head 接通，
   记为接口缺口/NOT_RUN，不以猜测替代。
5. **配置面**：operator 非秘密输入仍只有 sender/sessions/address；N-A2 不得变成可序列化开关，
   也不得新增“关闭认证/关闭证书校验”的键。

## 9. 当前 head 的未决项（诚实记录）

- 裁决文件实存于 coordinator 工作区
  `/Volumes/HyprialDev/HyprialOS/agents/gui-coordinator/worker-records/GD-MVP-UNBLOCK-20261017.md`；
  本 worker 工具域不可读属可达性缺口，不是文件不存在。D8/D9 裁决要点已由 coordinator 消息
  `f3557831`/`ab298c08` 摘要提供，本契约按该摘要复核：默认本机档 + 显式远端档、loopback
  listener + daemon `network.expose`、operator 显式端口、地址/节点身份 daemon 权威、
  不默认 `0.0.0.0`/公网、无认证 fail-closed；接口无法确认地址时如实记缺口/NOT_RUN。
- N-A2 的生产 derive/设备行来源在本 head 仍未接线；远端保持默认关闭并拒绝无 gate 的显式开启，
  直到身份 owner 给出可审计的注入源与验收证据。
- Windows 本机端点已从“禁用 host 相位”改为显式的 loopback TCP 本机档（§2.1.1），并在 Linux 上以
  `platform=win32` 参数覆盖传输选择与握手校验；Windows 真机（含 Python host、Rust relay、WebView
  五操作与 UI 呈现）仍为 NOT_RUN，须在 Windows runner/实机执行后才能宣称完成。
- smoke 结果语义（2026-10-18）：`guiDriver` 固定为 `app-core`，`guiTransport` 必须为
  `local-ipc`（mobile 构建为 `tailnet`，不属于桌面 smoke）；`guiQueries` 是页面自身经
  `app_core_call` 执行的 `capabilities.get` / `sessions.list` / `sessions.history`，三项缺一
  或任一失败都不算通过。`guiMounted` 仍要求 `data-connection="connected"`、`data-demo!="true"`
  且至少一个会话。`ShutdownReport` 的旧 `host` 键已更名为 `app_core`，不再输出 `host`。
- 真实聊天验收语义（2026-10-18）：`--hyprial-smoke-chat <session> --hyprial-smoke-marker <token>`
  （必须同时给出、且要求 `--hyprial-smoke-test`）让同一次 smoke 再用页面自身的会话行、输入框、
  发送与刷新控件完成一轮聊天；结果写入 `guiChat`，`ok` 要求发送被页面接受 **且** 刷新后出现
  含 marker 的 `assistant` 行。marker 只能来自该会话真实绑定的 agent，发出的用户文本不算回复。
  未给出这两个 flag 时 `guiChat` 为 `null`，默认 smoke 保持只读；半组或非法 token 以命名错误
  使 smoke 失败，不静默退回只读。

## 10. 修订

- 2026-10-17：初版；对应集成分支 `intg/gui-app-core-20261008` 精确 head `8892674da`。
- 2026-10-17：补 N-A2 当前 head 语义、验收结果与未决冲突；撤销按“行删除”验收，远端继续关闭。
- 2026-10-17：按 coordinator 的 D8/D9 裁决摘要复核远端两档语义（loopback listener +
  daemon expose、显式端口、地址/节点身份 daemon 权威），记录外部地址确认与客户端节点身份
  校验的接口缺口；订正裁决文件可达性记录。
- 2026-10-18：端点持有改为 Tauri managed state，退役“注入 GUI 子进程环境/从全局 env 读取”；
  新增 Windows 本机 loopback TCP 档（无 expose、ephemeral port、readiness 回报实际端口、
  每轮 request token）；发布主窗口改载 v11 Core 页并退役旧 Node GUI product 启动；
  Windows 真机验收仍为 NOT_RUN。
- 2026-10-18：登记可选 smoke 聊天验收 flag 与 `guiChat` 结果字段（默认 smoke 仍只读）。
- 2026-10-18：登记 smoke 的 `guiDriver`/`guiTransport`/`guiQueries` 新语义与
  `shutdown.app_core` 字段更名；契约影响见 PR 正文 `Contract-Impact`。
