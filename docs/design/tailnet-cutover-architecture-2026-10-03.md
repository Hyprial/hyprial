# Tailcat cutover 架构与边车协议 v3

日期：2026-10-03。上游：[M0 准备与裁决](tailnet-cutover-m0-2026-10-03.md)、[实验与迁移计划](tailcat-casdoor-experiment.md) §13–§18。
本轮目标与边界见 M0 §4.3：结构完整、基本流程跑通；权限可以宽松，但宽松点必须标为 `LAX`。

## 1. 总体结构

```text
shell   hyprial login / org create|invite|execute|network / desktop_network
          │  只做编排，不复制身份、目录或 ready 的判定
daemon  network/tailcat/   边车安装、进程监督、v3 客户端
        forwarding/        沿用现有 supervisor/controller，协议改为 v3
        orgfs/             目录文档的存储与同步；web 入口的身份改由 peer-key 解析
        application/netendpoints  从组织目录得到 peers → map-peer → Zenoh connect
identity device/           设备 key（文件位置、公钥、代次）、设备记录
        org_directory/     组织目录与邀请的语义模型（纯模型 + 存储端口，不 import daemon）
kernel  基础类型（不变）
Go      sidecar/hyprial-tailcat   Tailcat 引擎 + stdio JSON v3；不是用户或组织权威
```

依赖方向保持 kernel → identity → daemon → biz → shell。identity 定义目录模型和存储端口（Protocol），daemon 提供基于 OrgFS 的实现，由组合根注入。

## 2. 基本流程（本轮要跑通）

1. `hyprial login`：只走 auth.hyprial.com（现有身份流程保留），去掉边车安装和 Headscale join。登录成功后确保本机设备 key 存在（`hyprial-tailcat genkey`），并写出设备记录。
2. daemon 启动：forwarding supervisor 启动 `hyprial-tailcat forward`，发送 `up`（key 文件、allow 集、入站目标 = 本机 Zenoh 监听）。边车把本机 Tailcat 地址写入 0600 文件；daemon 把本机设备记录（含地址引用）发布到所属组织的目录。
3. `hyprial org create <org>`：Casdoor 建组（尽力而为，失败只警告，`LAX`），建立该组织的 OrgFS 空间和目录文档，写入本机设备。
4. `hyprial org invite <org>`：生成邀请链接，内容为 orgId、邀请者设备 id、邀请者 Tailcat 地址、一次性 token、引导 workflow 及其 sha256；在目录中登记待处理邀请；邀请窗口内 owner 边车 `allowAny=true`（`LAX`）。
5. `hyprial org join <link>`（原 `org execute`，已并入 `join`）：解析链接 → 校验来源（本轮只核对链接内 sha 与 token 格式，`LAX`）→ 依次执行声明式引导步骤：`connect-inviter`（map-peer）→ `join-org`（经 Zenoh 向邀请者 daemon 发送加入请求，邀请者写入目录）→ `sync-directory` → `connect-members`。
6. `hyprial org network [<org>]`：分三栏展示目录成员/设备、本机 mapped peers、边车 status，并标明各自的数据来源。

## 3. 边车协议 v3（`sidecar/hyprial-tailcat`）

### 3.1 子命令

| 子命令 | 行为 |
|---|---|
| `version` | stdout 输出一行 JSON `{"v":3,"sidecar":"<semver>","tailcat":"b4dc28e8aa8936f0a90a41ad8293a64e3d6b645f"}` |
| `genkey --out <abs path> [--force]` | 生成设备 key 文件（0600），退出码 0；已存在且无 `--force` 时不覆盖，退出码 3。两种情况下 stdout 都输出该 key 文件的公钥 JSON `{"v":3,"serverPublic":"nodekey:…","clientPublic":"nodekey:…"}`，不含私钥和 PSK；key 文件损坏时退出码 1 |
| `forward` | 进入 stdio 协议（§3.3） |
| `host --config <absolute config path>` | 以单一 Engine owner 运行通用 Docker service host；严格读取挂载的 key/address/config，应用 validated reload，SIGTERM 有界关闭。镜像默认非 root、只读，既不生成 key，也不把地址或 key 放入 argv/env/stdout |

### 3.2 key 文件

```json
{"v":3,"server":{"private":"privkey:…","presharedKey":"…"},"client":{"private":"privkey:…"}}
```

- 入站 server key 与出站 client key 分开（实验 §18.4：同一 key 用于并发引擎会竞争 DERP 登记）。
- 持久化 server key 和 PSK，重启后地址保持可用（Tailcat `Server.PresharedKey` 文档）。
- 私钥、PSK 和 Tailcat 地址（含 PSK）**不进 argv、stdout 事件或日志**；地址只写入 `addressFile`（0600），或在 `map-peer` 命令中经 stdin 传入。

### 3.3 forward：换行分隔的 JSON

规则：
- 每行一个对象。命令为 `{"v":3,"op":…}`，事件为 `{"v":3,"event":…}`。
- 未知 op、未知字段、版本不是 3，一律回 `error`，code 为 `BAD_REQUEST`。
- 启动后先输出 `hello`。
- stdin EOF 或收到 `down`：关闭全部 listener、client 和 server，输出 `exited` 后以 0 退出。
- 诊断信息只写 stderr，不含秘密。
- 宣告 `forwarding-request-id-v1` 后，除 `up`/`down` 外的既有 forwarding 请求携带
  唯一 `requestId`，成功或错误事件回显它；未宣告的旧 v3 边车仍可使用无 ID 形状。

| op | 字段 | 成功事件 |
|---|---|---|
| — | — | `hello {sidecar, tailcat, capabilities:["map-peer","unmap-peer","allow","expose","unexpose","peer-key"]}` |
| `up` | `keyFile`（绝对路径）、`allow`（nodekey 列表，可为空）、`allowAny`（bool）、`inboundTarget`（`tcp:127.0.0.1:<port>`，可选）、`peerPort`（Tailcat 侧服务端口，默认 7447）、`addressFile`（绝对路径）、`derp`（可选，测试用本地中继：`{host, derpPort, stunPort, insecureForTests}`；缺省时用 Tailcat 默认 DERP map，即官方 DERP，D1） | 先 `state {state:"Starting"}`，再 `state {state:"Running", serverPublic, clientPublic, peers:[]}`；`peers` 键必须存在 |
| `map-peer` | `peer`（deviceId）、`address`（对端 Tailcat 地址，秘密）、`port`（可选，默认对端 `peerPort` 即 7447） | `peer-mapped {peer, localPort}`。本地监听 `127.0.0.1:0`，每个入站连接经 `Client.DialTCPPort` 转到对端；相同 peer 和地址重复调用时幂等 |
| `unmap-peer` | `peer` | `peer-unmapped {peer}` |
| `allow` | `keys`、`allowAny` | `allowed {count, allowAny}`。替换准入集；不在新集合中的已连接 client 调用 `DisconnectClient` |
| `expose` | `port`、`target`（`tcp:127.0.0.1:N` 或 `unix:/abs`）、`proxyProtocol`（`""` 或 `"v2"`） | `exposed {port, target, proxyProtocol}`。`Server.Listen("tcp", ":port")` 后代理到 target；v2 时写 PROXY v2 头，源地址为对端 Tailcat IP，TLV `0xE0` 为对端 nodekey 文本 |
| `unexpose` | `port` | `unexposed {port}` |
| `peer-key` | `addr`（`ip:port`，即 exposed 目标看到的对端地址） | `peer-key {addr, found, key}`，来自 `Server.PeerKey`；替代 v2 的 `whois` |
| `status` | — | `state {state, serverPublic, clientPublic, peers:[{peer, localPort}]}` |
| `down` | — | `exited` |

service-connect-v1 是同一 v3 `forward` 会话上的加法扩展，而不是第二个
边车或 peer alias。`hello.capabilities` 增加 `service-connect-v1`；daemon 发送
带唯一 `requestId` 的 `map-service`（字段为 name/deviceId/address/serverPublic/
remotePort/recordGeneration/localPort）、`unmap-service`（name）与
`service-status`，边车用同一 ID 回 `service-mapped`（service）、
`service-unmapped`（name/removed）、`service-status`（services）或非致命
`service-failed`。service listener 与
peer mapping 分表；两者通过 Engine 的引用计数 client lease 共用 transport，
卸载一个 service 不得关闭 mesh peer 或同远端的其他 service。address 只经
stdin 到达边车，所有 observation、事件和诊断都不得携带 address/server key。

错误事件为 `error {code, message, requestId?}`，有相关且已携带 ID 的 forwarding
请求时必须回显 `requestId`。code 取值：
`BAD_REQUEST`、`NOT_UP`、`ALREADY_UP`、`KEY_FILE_INVALID`、`START_FAILED`、`PEER_UNKNOWN`、`DIAL_FAILED`、`PORT_IN_USE`、`INTERNAL`。

`up` 中 `inboundTarget` 与 `peerPort` 的含义：Tailcat server 在 `peerPort` 上 Listen，把连接代理到 `inboundTarget`（本机 Zenoh TCP 监听）。对端经 `map-peer` 得到的 loopback 端口即可作为 Zenoh connect 端点（实验 §17.1：认证目录 → 设备/服务授权 → Tailcat 适配器建立本机可拨入口 → 交给 Zenoh connect）。

### 3.4 本轮明确不做（`LAX` / 后续）

- 目录记录签名、Casbin 本地执行、策略版本与租期（M0 §4.2）：接口保留，实现为放行，并在代码中标 `LAX`。
- 邀请链接的管理员签名与原子一次性消费：本轮只校验格式和 sha。
- `allowAny` 邀请窗口：本轮的引导捷径，正式实现应改为独立 join 入口（M0 §4.1）。

## 4. Python 实施规格（冻结接口，供并行 worker 对齐）

路径相对 `src/hyprial/`。标 `LAX` 的地方是本轮的宽松实现，必须在代码注释中写 `# LAX(tailnet-cutover):` 并说明正式做法。

### 4.1 identity 域（W-B）

新增 `identity/impl/device/`：
```python
DEVICE_KEY_RELPATH = "secrets/tailcat/device-key.json"     # 0600，由边车 genkey 生成
DEVICE_RECORD_RELPATH = "state/tailcat/device.json"        # 公开部分，可以发布
ADDRESS_RELPATH = "state/tailcat/address"                  # 边车写入的本机 Tailcat 地址（秘密，0600）

@dataclass(frozen=True, slots=True)
class DeviceRecord:
    device_id: str          # = 本机 node_id（现有 resolve_node_id 的值）
    owner: str              # = settings owner（auth.hyprial.com 的 preferred_username）
    server_public: str      # "nodekey:…"
    client_public: str
    key_generation: int = 1
    def as_record(self) -> dict[str, object]: ...        # camelCase JSON
    @classmethod
    def from_record(cls, value: Mapping[str, object]) -> "DeviceRecord": ...   # 严格校验，坏数据 ValueError

def device_key_path(home: Path) -> Path
def device_record_path(home: Path) -> Path
def address_path(home: Path) -> Path
def read_device_record(home: Path) -> DeviceRecord | None
def write_device_record(home: Path, record: DeviceRecord) -> None    # 原子写
```

新增 `identity/impl/org_directory/`：
```python
@dataclass(frozen=True, slots=True)
class DirectoryDevice:      # OrgFS 中每台设备一条
    device_id: str; owner: str; server_public: str; client_public: str
    address: str            # Tailcat 地址，含 PSK。LAX：明文存在组织 OrgFS 空间，仅成员可读（M0 §4.1 ③）
    key_generation: int; updated_at: str   # ISO8601 UTC
    def as_record(self) -> dict[str, object]: ...
    @classmethod
    def from_record(cls, value) -> "DirectoryDevice": ...

ORG_SPACE_PREFIX = "org-"                  # 组织的 OrgFS space 名为 f"org-{org}"
DIRECTORY_DIR = "directory/devices"        # 每设备一个文档：directory/devices/<deviceId>.json
INVITES_DIR = "directory/invites"          # 待处理邀请：directory/invites/<token_id>.json

def org_space_name(org: str) -> str        # 校验 org 名：[a-z0-9][a-z0-9-]{0,62}
def org_from_space_name(name: str) -> str | None

class DirectoryStore(Protocol):            # 存储端口，由 daemon 用 OrgFS 实现
    def list_devices(self, org: str) -> list[DirectoryDevice]: ...
    def put_device(self, org: str, device: DirectoryDevice) -> None: ...
    def orgs(self) -> list[str]: ...       # 本节点所属组织

class OrgPolicy(Protocol):                 # 授权端口（Casbin 的接线点，M0 §4.2）
    def allows(self, *, org: str, subject: str, action: str, resource: str) -> bool: ...
class AllowAllPolicy:                      # LAX：恒真，正式实现为 pycasbin 本地执行
    ...

@dataclass(frozen=True, slots=True)
class BootstrapStep:
    kind: Literal["connect-inviter", "join-org", "register-device", "connect-members"]
BOOTSTRAP_STEP_KINDS: tuple[str, ...]      # 固定词表，其他 kind 一律拒绝
def default_bootstrap() -> tuple[BootstrapStep, ...]   # 上面四步按顺序
def bootstrap_yaml(steps) -> str           # 稳定序列化（yaml.safe_dump，sort_keys）
def parse_bootstrap_yaml(text: str) -> tuple[BootstrapStep, ...]   # 只接受 {version: 1, steps: [{kind: …}]}

@dataclass(frozen=True, slots=True)
class InviteLink:
    org: str; space_id: str; inviter: str; inviter_device: str
    inviter_address: str    # 秘密
    token: str              # secrets.token_urlsafe(24)
    workflow_yaml: str; workflow_sha256: str
    expires_at: str
INVITE_SCHEME = "hyprial-invite:v1:"
def encode_invite(link: InviteLink) -> str          # scheme + base64url(json)，无 padding
def decode_invite(text: str) -> InviteLink          # 校验 scheme、字段、sha256(workflow_yaml)==workflow_sha256、未过期
                                                    # LAX：不验签名；正式实现需要管理员设备签名
class InviteError(ValueError): code: str            # INVITE_MALFORMED / INVITE_EXPIRED / INVITE_DIGEST_MISMATCH
```
删除 `identity/impl/tailnet_identity.py`（self-host 退役，D4）。

### 4.2 daemon 网络（W-C）

新增 `daemon/impl/network/tailcat/`；删除 `network/tsnet_join.py`、`network/tsnet_sidecar.py`。
```python
TAILCAT_COMMIT = "b4dc28e8aa8936f0a90a41ad8293a64e3d6b645f"
SIDECAR_BINARY_NAME = "hyprial-tailcat"
BINARY_ENV = "HYPRIAL_TAILCAT_BINARY"              # 显式路径，优先级最高
class TailcatSidecarError(RuntimeError): code: str  # SIDECAR_MISSING / SIDECAR_INVALID / KEYGEN_FAILED
def sidecar_binary_path(home: Path) -> Path         # $HYPRIAL_HOME/bin/hyprial-tailcat
def locate_tailcat_sidecar(home: Path, environ=None) -> Path      # env > home/bin；不存在则抛 SIDECAR_MISSING
def verify_tailcat_sidecar(home: Path, environ=None) -> dict       # 运行 `version`，核对 tailcat == TAILCAT_COMMIT
def ensure_device_key(home: Path, *, owner: str, device_id: str, environ=None) -> DeviceRecord
    # 运行 `genkey --out <device_key_path>`：退出码 0（新建）或 3（已存在）都从 stdout 读取公钥；随后写 DeviceRecord 并返回
```
- 安装：本轮不实现下载。`hyprial-tailcat` 由 `go build` 产出后放入 `$HYPRIAL_HOME/bin`，或用 `HYPRIAL_TAILCAT_BINARY` 指定（`LAX`）。发布与 CI 走 integration-request。
- forwarding 改用 v3：
  - `forwarding/policy.py`：`FORWARD_PROTOCOL_VERSION = 3`；exposure 的 Python 侧 `"none"` 在线上写成 `""`。
  - `forwarding/controller.py`：`up` 校验改为 v3 字段；hello capabilities 用 v3 集合；`map_peer(peer, address)`；`whois` 改为 `peer_key(addr) -> {"addr","found","key"}`；新增 `allow(keys, allow_any)`；`status` 的 peers 为 `[{peer, localPort}]`。
  - `forwarding/supervisor.py`：同步改名（`whois` → `peer_key`），新增 `allow` 透传。
  - `forwarding_config.py`：`daemon_forwarding_environment` 生成 v3 `up`，内容为 `keyFile=device_key_path`、`allow=[]`、`allowAny=True`（`LAX`：引导期全放行；正式实现由目录生成 allow 集）、`addressFile=address_path`、`inboundTarget`、`peerPort`；可选 `HYPRIAL_TAILCAT_DERP` 环境变量（JSON），透传为 `up.derp`，供测试使用本地中继。资格判定从「已 tsnet join」改为「设备 key 与 DeviceRecord 存在且边车可定位」。
- `bootstrap/discovery.py`：删除 `TailscaleEndpoints`、`local_tailnet_endpoint`、`tailnet_status_projection`。`ForwardingEndpoints` 新增参数 `directory: Callable[[], Mapping[str, str]]`（deviceId → address，已排除本机），对缺失项调用 `map_peer(peer, address)`，对过期项调用 `unmap`。
- `application/netendpoints/endpoints.py`：去掉宿主 tailscale 发现和 tailnet listen 推导；directory 回调由 W-D 的目录读取函数提供（经 `self._tailcat_directory_peers()`，W-C 先实现为返回 `{}` 的方法，W-D 接线）。
- `network/peer_reachability.py`：删除 tailnet status 投影；保留 `tcp_probe`。
- `configuration/network_profile.py`：`NetworkProfile` 去掉 `control_plane_kind`、`control_plane_url`、`join`，只保留 `org`、`issuer`、`client_id`；`profile.json` 升到 version 2，读取 v1 时忽略这三个字段。删除 `CONTROL_PLANE_KINDS`、`TSNET_STATE_DIRNAME`。`DEFAULT_PROFILE` 中的 `auth.hyprial.com` 字面量保留唯一出处。
- `configuration/home_migration.py`：删除 tsnet 二进制改名迁移。
- `configuration/identity.py`：删除 `"tailscale-selfhost"` mode 和错误文案里的 self-host 选项。
- `ipc/router.py`：`network.whois` 改为 `network.peerKey`。

### 4.3 daemon 组织与 OrgFS（W-D）

- 新增 `daemon/impl/org/network/`：
  - `OrgFsDirectoryStore(fs)`：实现 `DirectoryStore`，以 `f"org-{org}"` 命名的 space 为组织，读写 `directory/devices/<id>.json`。
  - `OrgNetworkService`：提供 `create(org)`、`invite(org, user)`、`execute(link)`、`network(org)`、`publish_self()`、`directory_peers()`。
    - `create`：调用 `fs.create_space(org_space_name(org))`，写入本机设备记录。Casdoor 建组为尽力而为（`LAX`，失败只记警告）。
    - `invite`：调用 `fs.invite(space, user, "rw")`，生成 `InviteLink`（inviter_address 读自 `address_path`），并在 `INVITES_DIR` 登记，返回编码后的链接。
    - `execute`：调用 `decode_invite`，然后按步执行：
      1. `connect-inviter`：`map_peer(inviter_device, inviter_address)` 并触发 forwarding redial；
      2. `join-org`：`fs.join(space_id)`（带重试，直到 holder 可见）；
      3. `register-device`：写入本机设备记录；
      4. `connect-members`：触发 reconcile。
    - `publish_self`：daemon 启动且地址文件出现后，把本机设备写入所属的每个组织。
    - `directory_peers`：返回 deviceId → address，排除本机，供 W-C 的 directory 回调使用。
  - IPC：`org.create`、`org.invite`、`org.execute`、`org.network`，在 `ipc/router.py` 注册，与 W-C 改同一文件时由集成者合并。
- `orgfs/webserver/identity.py`：删除 `TailscaledWhoisResolver`（LocalAPI）。PROXY v2 TLV `0xE0` 改为解析 nodekey 文本，经 `DirectoryStore` 映射为 owner，得到 `Identity(login_name=owner, tags=())`；查不到时返回 403。
- `orgfs/web.py`：删除 tailnet FQDN 推导（`state/tsnet/node.json`、宿主 `tailscale status`）；web 主机只来自 settings `orgfs.web_host`，未设置时为 None，并说明原因。

### 4.4 shell（W-E，依赖 W-B/W-C/W-D 的接口）

- `setup/login_flow.py`、`setup/login.py`：
  - 删除边车获取/安装和 `run_join`；删除 `--preauthkey-file`、`--join-timeout`、`--skip-join`、`--install-sidecar`、`--only-install-sidecar` 选项。
  - 登录提交后调用 `ensure_device_key`；边车缺失时登录仍算成功，同时给出明确的下一步提示（沿用 D12「失败不回滚身份」）。
- `setup/first_run.py`：删除身份来源选择；没有 owner 时直接走服务登录。删除 `setup/selfhost.py`，并同步 `cli_runtime/entry.py`、`cli_commands/services.py`。
- `orgfs/org.py`：保留 `show/status/fetch/import` 语义不变，新增 `create`、`invite`、`execute`、`network` 四个子命令（走 IPC `org.*`）。
- `desktop_network.py`：
  - `status` 返回 `{owner, authenticated, deviceReady, sidecarError, ready, profile, profileSource}`；
  - `onboard` 的 action ∈ {login, device}；
  - 不再调用 `run_join`。
- `config/network.py`：`whois` 命令改为 `peer-key`。`config/profile.py` 去掉控制面字段。`observe/doctor.py` 的 tsnet 检查改为 Tailcat 边车与设备 key 检查。

### 4.5 测试纪律（本轮）

- 能真实测就真实测，尤其是纯模型、编解码和协议层。
- 难以隔离的集成点可以写占位测试，并在 `tests/support/fake_test.py` 中统一提供：
  ```python
  def fake_test(reason: str) -> None:
      warnings.warn(f"This is a fake test: {reason}", UserWarning, stacklevel=2); assert True
  ```
- 被删除功能的旧测试一并删除，并在 worker 汇报中逐个列出文件名和原因；不得用 skip/xfail 留着。

## 5. 邀请的目标形态：https 落地页 + 账号绑定接受（Allen 第七轮）

### 5.1 链接格式（三种形式，同一个 payload）

`<payload>` = base64url(JSON(InviteLink))，无 padding；也就是 §4.1 中 `encode_invite` 去掉 `hyprial-invite:v1:` 前缀后的部分。

| 形式 | 示例 | 用途 |
|---|---|---|
| https（主形式） | `https://<INVITE_HOST>/join#v1.<payload>` | 聊天应用中可点击；落地页；手机和电脑都能用 |
| deep-link | `hyprial://join#v1.<payload>` | 本机装有 Desktop 时由落地页跳转，或直接点击 |
| 裸 token | `hyprial-invite:v1:<payload>` | 复制粘贴、脚本 |

- payload 只放在 `#` fragment 中，浏览器不会把它发给服务器，所以落地页只需静态托管。
- `decode_invite` 接受以上三种形式。
- `hyprial org invite` 默认输出 https 形式和一行可复制的 `hyprial org join '<链接>'`。
- `<INVITE_HOST>` 来自 profile 字段 `inviteBaseUrl`，默认值为 `https://hyprial.ai`（运维已上线落地页，2026-10-03 起替换原占位符）。

### 5.2 两条接受路径

```text
路径 1（直接）：在装有 hyprial 的设备上执行 `hyprial org join '<链接>'`，或经 Desktop deep-link 触发
              → 本机 daemon 直接执行引导步骤

路径 2（账号绑定，主路径）：任意设备、任意浏览器打开 https 链接
  → 落地页用 Hyprial 账号登录（auth.hyprial.com，OIDC + PKCE）
  → 点击「接受」→ 页面 JS 用该用户自己的 token，把邀请写入其 Casdoor 用户属性
     `hyprialInvites`（JSON 数组，元素为 {payload, acceptedAt}）
  → 该用户已登录的设备拾取后自动加入（或提示确认）
```

### 5.3 设备侧拾取（本轮实现客户端，页面由运维实现）

- **由 shell 执行，不由 daemon 执行**：daemon 不读取 `secrets/login.json`（T11 不变量）。
- 新命令 `hyprial org pending [--accept-all]`：
  1. 用 `refresh_access_token` 换取 access token；
  2. 调用 Casdoor `get-account` 读取 `hyprialInvites`；
  3. 向本机 daemon 查询 `org.list`，得到本机已加入的组织（按 `spaceId`）和主动退出过的组织（`leftOrgs`）；
  4. 跳过本机已加入或主动退出过的组织，其余邀请逐个执行与 `org join` 相同的步骤。
- **CLI 只读 Casdoor，不写**（Allen 2026-10-04 裁决，取代原来的 `consumedBy` 写回）：
  - 账号属性只记录用户意图，即「我在落地页接受了这个邀请」，唯一的写入方是落地页；
  - 「本设备是否已处理」是设备事实，由本机组织状态和 OrgFS 目录表达；
  - 主动退出过的组织不会被旧邀请自动拉回，只有显式执行 `hyprial org join` 才会重新加入，并清除退出记录；
  - 实测表明，真实部署也拒绝 CLI 客户端改写该属性。
- `hyprial login` 成功后自动执行一次 `org pending`。Desktop 可以定时调用（补丁中注明）。
- 验收（2026-10-04 实测，真实账号、真实落地页）：网页接受 → `org pending --accept-all` 加入：PASS。证据见 `docs/evidence/tailnet-cutover-2026-10-03/verification/landing-page-invite.md`。

### 5.4 后续项（不在本轮）

- 个人网络 / 在其他设备上加入（第六轮讨论的 C）。
- Universal Links / App Links（需要移动 App）。
- 邀请签名、原子一次性消费。
