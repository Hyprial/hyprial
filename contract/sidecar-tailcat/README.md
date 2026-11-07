# sidecar-tailcat — 协议 v5 契约样例

`hyprial-tailcat` 边车的 stdio JSON 协议 v5 的 golden 样例。子命令与 key 文件格式见
[tailnet-cutover-architecture-2026-10-03.md §3.1–§3.2](../../docs/design/tailnet-cutover-architecture-2026-10-03.md)，
forward 基线见同文档 [§3.3](../../docs/design/tailnet-cutover-architecture-2026-10-03.md)，
service-connect-v1 扩展以 [service-connect baseline-C contract](../service-connect/README.md)
及其 `fixtures.json` 为准。

## HTTP proxy underlay (0.5.5.4)

The daemon passes an immutable launch snapshot of `HTTP_PROXY`, `HTTPS_PROXY`,
`http_proxy`, `https_proxy`, `NO_PROXY`, and `no_proxy`. Uppercase wins over
lowercase; HTTPS falls back to HTTP proxy when neither HTTPS variable is set.
`ALL_PROXY`/`all_proxy` and ambient `TS_*` remain excluded. Restart with a new
launch snapshot to change proxy settings. No URL or credential is logged.

DERP map HTTPS and DERP TCP use this policy. DERP CONNECT targets use the map's
hostname, never its IPv4/IPv6 hint; the port defaults to 443. NO_PROXY applies
before proxy selection. A proxy rejection does not fall back to direct TCP.
STUN is optional: unavailable UDP does not invalidate a working DERP route.
Invite redemption and registration use encrypted Tailcat streams over this
underlay, not a separate HTTP registration endpoint.

Diagnostic observations never cancel a control dial. Single-node and optional
netcheck failures are non-terminal while another path can work. Embedded regions
are tried deterministically, closing the preceding transport before reusing the
same derived client key; registration success pins that transport for its lifetime.
An engine-local healthy-region hint survives final lease release, not the
transport. The key combines authority server public key and canonical DERP-map
digest. The hint expires after ten minutes, is never persisted, and is bounded
to 128 entries. A new client tries the hint first, then the remaining regions in
sorted order. Ping or TCP dial failure clears the matching hint; engine startup
resets the cache and prevents an old generation from repopulating it.
Initial regional handshakes share the remaining thirty-second startup/caller
budget across untried regions, reserving up to ten seconds (one third of the
initial available budget) for healthy handshake/control work. The last region
retains the remaining context; the upstream ten-second Ping cap also applies.
Unused time is available to the next region when Ping returns early. Tailcat itself
retries meow delivery after send errors. On the checked-in unpatched
`tailscale.com` dependency there is no typed exhaustion notification: a rejected
or hanging region consumes its whole share before the next region is tried, so
three rejected regions cost about fifteen seconds per exchange. Each proxied node of a
multi-node region becomes a single-node candidate, so a hanging first proxied node
consumes only its own share and a healthy later node in the same region is still
reached. Direct nodes (no effective proxy, `NO_PROXY` honoured) keep the original
grouped candidate, so behaviour without a proxy is unchanged.
Faster per-node failure needs the candidate patch (see
[`patches/README.md`](../../sidecar/hyprial-tailcat/patches/README.md) and KP-001 in
`docs/reference/known-problems.md`).
No new wire field represents any internal notification. No diagnostic drives cancellation.
Four-region fallback is the validated scope. Larger maps or a healthy cold start
slower than its remaining share can still exhaust the request.

The candidate patch additionally bounds each proxied DERP node TCP/proxy-TLS/CONNECT
attempt at the same 1500 ms as a direct node dial and adds the typed
region-exhaustion notification. **It is not activated** by the checked-in module;
the nofork checkout ships neither the bound nor the notification. The candidate
remains the input for the cloud-PC fork decision.

Observations cover only the current control dial on its authority-client
instance, including optional netcheck activity, not stale prior requests. After
actual control failure, missing node causes may be supplemented by singleton TLS
probes within the remaining exchange budget (at most four concurrently). These
probes do not register a DERP identity or control fallback. They are not proof of
the original dial's cause. Unattempted nodes are not fabricated as failures.
A longer exchange or queued request can still reach `MEMBERSHIP_SIDECAR_TIMEOUT`;
its data follows the locally attributed `operation`/`leg`/`budgetSeconds` rule
below and is not evidence of a specific network cause.

## Membership transport diagnostic byte counts (0.5.5.3)

A correlated v6 membership `error` may carry a `data` diagnostic object.
The Python consumer validates the complete object before copying any of it
into IPC error data. Required closed fields:

- `stage`: `control`, `derp`, or `sidecar`.
- `cause`: `proxy-rejected`, `connection-refused`, `dns-failed`, `tls-failed`,
  `deadline-exceeded`, `transport-failed`, or `response-timeout`.
- `proxy`: `configured` or `not-configured`; configuration is not proof of use.

Optional `attempts` contains objects with `node` (DNS/IP host plus TCP port),
`cause` from the same vocabulary, and optional `source:"diagnostic-probe"`.
IPv6 hosts are bracketed. URLs, credentials and arbitrary prose are not valid
node values. Only these keys and the optional `bytesReceived` key are allowed.

`bytesReceived` is an integer count of bytes read before an incomplete control
response fails, never the bytes themselves. Go emits it with `stage:"control"`
and `cause:"deadline-exceeded"` or `"transport-failed"`; the Python consumer
requires that context whenever the key is present. Zero is meaningful;
absence means the producer did not supply a count. The accepted range is
**0 through 1,048,577 inclusive**: the 1 MiB control frame limit plus one
bounded overflow-detection byte, matching Go's `Detail.Valid` and bounded
control-frame reader. Negative values, larger values, booleans, strings,
floats, null and other non-integers invalidate the entire diagnostic object.
The Python consumer discards invalid diagnostics without changing the error
code or copying remote message text.

Valid counts survive wire → org error → IPC error `data` for
`AUTHORITY_UNREACHABLE` and `MEMBERSHIP_SIDECAR_TIMEOUT`. The latter also includes
locally attributed `operation`, `leg`, and `budgetSeconds`; these cannot be
supplied or overwritten by the remote diagnostic. A locally expired wire wait
has no observed byte count and does not fabricate one. No response payload,
invite credential, proxy URL or remote prose is included in the diagnostic.

## 布局

- `golden/commands.jsonl`：合法命令（daemon → 边车，每行一个 JSON 对象，`{"v":5,"op":…}`）。
- `golden/events.jsonl`：合法事件（边车 → daemon，`{"v":5,"event":…}`）。
- `golden/invalid-commands.jsonl`：必须被解析器拒绝（`BAD_REQUEST`）的命令行。

## PROXY v2 target admission (2026-10-30)

For `expose` with `proxyProtocol: "v2"`, the sidecar requires a `unix:` target
with a clean absolute path. Its immediate parent must exist, must not be a
symlink, must have mode 0700, and must belong to the current user. The stdio
parser and service-host configuration validator enforce the same check.
TCP loopback targets remain valid only with an empty `proxyProtocol`.
Non-v2 target validation is unchanged. Rejected stdio commands return
`BAD_REQUEST`; Windows named-pipe support is a separate work item.

The positive Unix golden target `/run/hyprial/app.sock` is a fixture path:
the Go golden runner replaces it with a socket path inside a test-owned 0700
directory before parsing. Invalid TCP v2 golden commands need no filesystem
fixture. Tests for mode, owner, symlink, and missing-directory rejection use
isolated directories in both validator packages.

This is an admission-time check. It does not pin directory descriptors or
prevent a later filesystem replacement between validation and backend dialing.

## 校验

`sidecar/hyprial-tailcat/internal/contracttest` 的 Go 测试逐行校验 golden：
正例行必须被 `proto.ParseCommand` / `proto.ParseEvent` 接受，`invalid-` 前缀文件的
每一行必须被拒绝。改动协议字段语义时须同步本目录并在 PR 正文说明 `Contract-Impact`。

## v5：按目标服务端派生客户端身份

v5（2026-10-21）增加 `client-key-derivation-v1` capability、
`derive-client-key` 命令与 `client-key-derived` 事件。命令只接收目标
`serverPublic`；边车读取现有 master `ClientPrivate`，按
`HKDF-SHA256(master, salt=empty,
info="hyprial-tailcat/client-per-server/v1\\x00" || serverPublic.raw)` 派生 X25519 私钥，仅返回其
`nodekey:` 公钥。私钥不落盘、不进入事件。每个 Tailcat client 仍绑定一个服务端地址，
但 pool 以地址内的 server public key（也是派生输入）为键；同 master、同 server 的结果
跨重启稳定，不同 server 的结果不同。相同 server public key 的地址或 PSK 变化触发单
代际交接：先同步关闭旧 client，再把全部存活 mesh/service lease 迁到新 client，之后才
允许新地址拨号；同一派生 key 不得有两条同时在线的 DERP client 连接。单次 stream 或
remote-port dial 失败不触发换代。

组织目录发布 `clientPublicByServer` 映射；服务端只准入以自身 `serverPublic` 为键的值。
任何映射值等于 master `clientPublic`、映射值之间重复、其他服务端的派生 key 或缺少映射
的旧记录均不得进入 allow 集。
v4 与 v5 是 fail-closed flag-day 边界：v5 daemon 不接受 v4 sidecar。

## v4：expose 按服务准入（admission）

v4（2026-10-08，orgfs requester-authentication 设计 §3.3）在 `expose` 命令上新增可选
`admission` 字段：`"allow"`（默认，仅放行 allow 集内的 key）或 `"any"`（放行任意已认证
key，用于加入端点）。`exposed` 事件总是回显 `admission`。封闭线格式下该字段为新增，
故协议版本由 3 升为 4：`v` 不为 4 的命令一律拒绝（见 invalid golden）。配套语义变化：
节点级握手放行所有 key，allow 集改在每条入站连接上执行（zenoh 入站与 allow 策略的
exposure）；撤销 key 只关闭该 key 的受限连接，any 服务上的连接保留。

## 秘密纪律提醒

`map-peer` 的 `address`（Tailcat 地址，内含 PSK）只经 stdin 传入，绝不出现在事件、
日志或 argv 中；`up` 的地址只写入 `addressFile`（0600）。golden 中的 `address`、
`nodekey:` 均为占位符，不是真实密钥。

## forwarding-request-id-v1

新边车在 `hello.capabilities` 宣告 `forwarding-request-id-v1`。daemon 随后为
`status`、peer、allow 与 exposure 操作发送唯一 `requestId`，成功事件和 `error`
回显同一 ID。未宣告该 capability 的兼容边车继续使用无 ID 形状；daemon 将无 ID
错误只归给当前串行 forwarding 操作，绝不归给并发 service 请求。

## derp-map-v1

新边车在 `hello.capabilities` 宣告 `derp-map-v1`。`up` 可带 `derpMapURL`
（必须是 HTTPS）和 `derpMapCacheDir`（绝对路径）；两者都设置时，Tailcat 以
region auto 从该 map 选路，并把最后一次成功 map 缓存在 sidecar 状态目录
（owner-only 0600/0700）。map 暂时不可用时使用缓存；没有缓存则 `up` 失败并
在 status 中可见。`status` 的 Running 事件带 `derpMapURL` 与
`derpMapSource`（`configured`/`override`/`default`）。这些字段可选：Running
事件不带 `derpMapURL` 时，daemon 按默认 map 处理。这不是旧版本兼容路径：v5 之前
的边车（含 v3、v4）在上文 flag-day 边界被整体拒绝（daemon
`forwarding/controller.py` 与 `internal/proto/message.go` 都只接受 v5）。
`hyprial-tailcat-labrelay` 输出的 `{"v": 3, "derp": …}` 是它自己的 relay 配置信封，
版本号与边车协议无关。

## connection-telemetry-v1

新边车在 `hello.capabilities` 宣告 `connection-telemetry-v1`。`status` 的 Running
`state` 事件可附加 `connectionTelemetry`：每个 `map-peer` 固定槽位保留最近 16 条
连接关闭记录和累计错误类别计数。每个槽位报告 `dialAttempts`、`dialFailures`、
`dialFailureReasons`、`splicesEstablished`、`closedCount` 与 `closeReasons`；
`dialFailureReasons` 的键闭集固定为 `timeout/refused/neterr/other`。`lastDialAtMs`
记录最近拨号结果的 Unix 毫秒，`lastDialError` 只在有失败时报告最近类别。
兼容字段 `dialFailureCount` 等于 `dialFailures`。daemon 继续接受旧边车仅含
`dialFailureCount`/`lastDialError` 的形状。所有错误分类均不含原始错误文本、地址或密钥。
v4 槽位与记录包含权威 `slot`；关闭记录还包含 peer、
`mapPort`、双向字节数、时长、
`closeSide`，以及 peer→local (`errors.in`) 和 local→peer (`errors.out`) 的归一化
`io.Copy` 结果。错误闭集为 `eof/reset/timeout/canceled/other:<type>`；`other` 不包含
原始错误文本、地址或密钥。TCP 槽位的 `mapPort` 是槽位端口；Unix socket 槽位没有
数值端口，固定报告 `null`。daemon 兼容旧 telemetry 的纯数值 `mapPort` 身份；但
`mapPort:null` 只有与非空字符串 `slot` 同时出现才合法，其他类型与空 slot 都 fail closed。
它是有界状态快照，不产生逐包或逐连接日志。未宣告该 capability 的边车可省略该字段，
daemon 将其视为 telemetry 不可用，而非空历史：daemon status 的
`forwarding.connectionTelemetry` 固定为 `{"state":"unavailable","reason":"capability-absent"}`；
`slots: []` 仍表示“有 capability、历史为空”。doctor 的 zenoh check 始终输出
`metrics.connectionTelemetry`，没有 forwarding 段时输出
`{"state":"unavailable","reason":"forwarding-absent"}`。

## service-connect-v1

`hello.capabilities` 增加 `service-connect-v1`。扩展请求均带非空且唯一的 `requestId`：
`map-service`、`unmap-service`、`service-status`；响应使用同一 ID 的
`service-mapped`、`service-unmapped`、`service-status` 或非致命
`service-failed`。请求/响应字段集合严格闭合。

service listener 与 peer mapping 分属两个 namespace。`map-service` 的 Tailcat address
只存在于输入与内存；输出 observation 不含 address/server key。边车先解码 address 并核对
`serverPublic`，再绑定 literal `127.0.0.1`；`localPort=0` 由 OS 分配，固定端口必须在
1024..65535。listener ready 不代表上游健康：初始 path/dial 均为 unknown，只有新的本地
连接会更新 dial duration/state/error；没有权威 path 数据时保持 unknown/null。

Engine 按远端 server public key 对 mesh/service listener 共用引用计数 Tailcat client。
卸载一个 service 不得关闭同远端 peer 或其他 service；`down` 取消拨号并关闭 listener、
stream 与最终 transport lease。同 server 的 address/PSK 变化执行上述关闭旧 generation、
迁移 lease、再发布新 generation 的交接。该 pooling 不构成同 caller key 连接多个同
DERP server 的证明；那一能力必须通过独立、确定性的双 server 并发与撤销/恢复测试。

## n-a2-envelope-vectors（预留接入点，尚未落向量）

N-A2 请求信封的**正式跨语言一致性向量**将落于本目录
`golden/n-a2-envelope-vectors-*.json`（每文件形如
`{"schema": "hyprial-n-a2-envelope-vectors/v1", "vectors": [...]}`，每个向量含
`derive`（role/peer/key_base64）、`request`（帧体去 auth）、`auth`（deviceId/ts/nonce）
与 `expected`（digest/mac_text/mac））。Python 侧消费测试
`tests/shell/gui/adapter/test_app_n_a2_seams.py` 会扫描本目录：向量文件到达即自动校验，
无需改代码；今天的静态同形向量在 `tests/fixtures/network/n_a2_envelope_vectors.json`
（以 fake sidecar 的确定性 derive 键与冻结规则生成）。key 均为占位/确定性测试值，
不含真实密钥。

## peer-slots-v1（协议 v4）

v4 引入固定槽位池（design-stable-zenoh-session-slots-2026-10-08 §4），
`hello.capabilities` 宣告 `peer-slots-v1`：

- 槽位 wire id 有两种形态：绝对 socket 路径（unix 传输，默认），或
  `tcp:<loopback-ip>:<port>`（tcp 传输——Windows 方案：zenoh 1.9.0 在 Windows 不编译
  unixsock-stream，见 notes/windows-unixsock-2026-10-08.md）。`tcp:127.0.0.1:0` 是
  grow spec：请求内核分配端口的新监听。
- `up` 可选 `slots`（wire id 列表；除可重复的 grow spec 外互不重复）：边车在 up 时即监听
  全部槽位，未绑定的槽位工作在 hold 模式——接受连接但不读，让拨号方的首个握手报文停在
  内核缓冲区；可选 `slotHoldMs`（默认 10000，对应 zenoh 的 open timeout）限定挂住时长，
  超时即丢弃，拨号方自行重拨。`slotHoldMs` 不带 `slots` 时拒绝。
- `map-peer` 必带 `slot`：把该槽位绑到 peer 的 Tailcat 地址，已挂住的连接与之后的新连接
  都转发过去；成功事件 `peer-mapped` 携带 `slot`（解析后的 wire id）。
  未在 up 声明的槽位按扩容契约懒绑定：unix 路径须落在某个已监听 unix 槽位的同一目录且
  符合 `slot-NN` 命名规则；tcp 只接受 grow spec（应答携带新分配的解析 id）。其余未知槽位
  拒绝（BAD_REQUEST）。同一 peer、同一地址、同一槽位重复调用幂等；换 peer 或换地址的
  改绑先关闭旧连接。不带 `slot` 的 `map-peer`（旧自分配 `127.0.0.1:0`、`localPort` 形状）
  曾作为 N1→N2 过渡短暂保留，随 `HYPRIAL_ZENOH_SLOTS=0` 回滚阀一起退役：现在解析即拒绝
  （missing slot），`peer-mapped` 与 `state.peers` 均无 `localPort` 键
  （golden `invalid-commands.jsonl` / `invalid-events.jsonl` 钉住退役形状）。
- `unmap-peer` 不变：关闭该槽位上的现有连接，槽位回到 hold 模式（监听不关闭）。
- `state` 事件的 `peers` 条目只有 `{peer, slot}` 形状；带槽位的 up 后，`state` 另携带
  `slots`（解析后的 wire id，up 顺序在前、扩容追加）——tcp 传输下 daemon 以此获知
  内核分配的端口并组装 connect set。
- `allow` 不变：入站准入与出站槽位是两件事。
