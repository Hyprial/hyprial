# 服务授权：接收方看到什么，还要手工做什么

适用于：收到别人授权的服务（`service.grant`）、想在本设备上连上去的成员，或代为操作的 agent。
授权人一侧（怎样授权、怎样撤销、org 目录需要什么准备）见 [迁移说明：组织目录新增受保护段 `service-grants`](migration-service-grants.md)；
映射本身的边界（`127.0.0.1` 监听、固定端口、`service-access.json`）见 [Connect to an operator-approved service](service-connect.md)。

一句话：别人授权给你之后，**服务条目和 Key 都自动到位，你不需要手工放置 catalog 或 `service-access.json`**。
你需要做的是把它连上；带了 Key 的，还要把 Key 绑给要用的 agent。

> [!NOTE]
> 本文区分**实测**（本切片的测试在真实 OrgFS 存储、真实加密信封与真实 CLI 渲染函数上运行得到的行为）与**读代码推断**（由源码推导、未做双机联机实测的行为）。未实测的都标了出来。

## 1. 接收方自动得到什么

下面三样都由 daemon 在后台推导。服务条目与设备只在内存里，不写进 catalog 或 `service-access.json`；Key 写成你 home 下的一个 user-provider 条目，另有一份私有台账记下“这条是哪份授权写的”（`state/service-grants/consumer-ledger.json`）。连接意图照旧由 `connect` 自己持久化：

| 得到什么 | 来自哪里 | 条件 |
| --- | --- | --- |
| 一个可连接的服务条目：服务名、端口、协议、备注 | 授权文档 `directory/service-grants/<授权人>/<grantId>.json` | 授权有效，且接收方是本机 `<owner>/<deviceId>` 或 `@members` |
| 设备的 `serverPublic` 与 `address`（相当于受保护记录里的一行） | 授权人名下的设备行 `directory/devices/<授权人>/` | `service.deviceId` 在授权人名下有设备行 |
| 一个 Key（可选） | 授权文档里的密文 | 仅当接收方**精确等于**本机；`@members` 授权永远没有 Key |

授权人只取自文档路径，不取自文档内容。一份授权能连到哪台机器，取决于授权人**此刻**发布在目录里的设备行：授权人换了设备密钥或地址，本机的映射在下一个维护周期跟上。

### 什么时候推导

- daemon 启动时，如果本机既没有 `service-access.json` 也没有已保存的连接，但任一所属 org 里有指向本机的有效授权，service-connect 照常启动（**实测**：`tests/integration/service_connect/test_grant_only_startup.py`）。
- 之后每个维护周期（默认 5 秒）重新检查目录；目录没变就什么都不读、不写（**实测**：`tests/daemon/service_grants/consumer/test_maintenance.py`）。

## 2. 接收方看到什么

### 2.1 哪些授权是给我的

```sh
hyprial org service grants
```

`Received:` 一节列出指向本机的授权（**实测**，输出格式来自 CLI 的渲染函数）：

```text
Granted:
  (none)
Received:
  sg.nubos7mltmaedzhx4eohns3aan	group-acme	alice	device-a:8447	magpie	bob/device-b	cred=yes	effective
```

列依次是：授权 id、org、授权人、设备:端口、服务名、接收方、是否带 Key、状态。`--org <org>` 只看一个 org，`--include-revoked` 连已撤销和已过期的也列出。

这一节目前**不显示** Key 是否已经存到本机、连接是否已建立；那要看 2.2 和 2.3。

### 2.2 服务条目

```sh
hyprial org service guide magpie --full
```

（**实测**，`source` 与 `auth` 两行是本切片生成的文本）

```text
Untrusted catalog text (review before use):
source: discovery
name: magpie
deviceId: device-a
remotePort: 8447
protocol: http
localPort: -
usage: Internal gateway
auth: granted, stored as user-provider entry grant.nubos7mltmaedzhx4eohns3aan (field KEY); bind it to an agent: hyprial agent secret grant <agent> --source user-provider --entry-id grant.nubos7mltmaedzhx4eohns3aan --field-name KEY --environment-name <ENV> --grant-id <grant-id> --revision 1

Granted by alice in group-acme (grant sg.nubos7mltmaedzhx4eohns3aan).
```

- 最后一行是本切片写的固定文本，说明这份条目来自哪份授权。
- `auth:` 行是三种状态之一（**实测**）：
  - 带了 Key 且已存好：`granted, stored as user-provider entry grant.<id> (field KEY); bind it to an agent: …`，后面跟可直接执行的命令。
  - 带了 Key 但本机拿不到：`granted, but not available on this device; see service.grant.rejected events.`（原因见第 5 节）。
  - 没带 Key：`none granted; the service admits this device by its own rules.`
- 不带 `--full` 只显示最后一行。`--json` 返回的对象里，`entry` 是条目的九个字段。
- `source` 显示的是 `discovery`，与手工放置的条目一样：**列表和指南不标识“这份条目来自授权”**，要知道来源看最后一行，或看 2.1。

**重名**：如果本机已经有一个同名服务（手工放置的，或内置的），推导出来的条目改名为 `<服务名>-<授权人>`，例如手工放置的 `magpie` 和 Alice 授权的 `magpie` 并存时，后者叫 `magpie-alice`（**实测**：`tests/daemon/service_grants/consumer/test_connect.py`）。服务名已经很长、放不下 `-<授权人>` 时，先截短服务名再接上（后缀最多 16 个字符）；授权人名字里没有可用字符（例如全是中文或符号）时，后缀改用授权人名字的 8 位十六进制摘要。改出来的名字总与原名不同，且同一授权人每次得到同一个名字（**实测**：`tests/daemon/service_grants/consumer_fixes/test_connect_fixes.py`）。两个名字都被占用时，这份授权不生成条目，并发 `service.grant.rejected`，原因 `name-conflict`。

### 2.3 `service list` 在连接之后才有内容

`hyprial org service list` 列出的是**映射**，不是目录条目。所以刚收到授权时它是空的，`connect` 之后才出现（**实测**）：

```text
NAME  DEVICE:PORT  LOCAL            PATH     DIAL  STATE     LAST ERROR
magpie device-a:8447 127.0.0.1:18080         unknown  -     listening —
source: discovery
```

## 3. 还要手工做的三步

### 第 1 步：连接（都要做）

```sh
hyprial org service connect magpie
```

（**实测**）

```text
Connected: magpie (device-a:8447) → http://127.0.0.1:18080   [unknown]
Use:  connect to 127.0.0.1:18080
Key:  granted, stored as user-provider entry grant.nubos7mltmaedzhx4eohns3aan (field KEY); bind it to an agent: hyprial agent secret grant <agent> --source user-provider --entry-id grant.nubos7mltmaedzhx4eohns3aan --field-name KEY --environment-name <ENV> --grant-id <grant-id> --revision 1
Guide: hyprial org service guide magpie
```

上面的 `18080` 是测试替身给出的端口，实际以命令输出为准：授权条目没有固定端口，本机端口在建立映射时分配，`hyprial org service list` 会再列出一次。应用里填 `http://127.0.0.1:<本机端口>`。

### 第 2 步：把 Key 绑给 agent（授权带 Key 时才做）

Key 存在**你自己的** user-provider 条目里，条目名固定是 `grant.<授权 id 去掉前缀 sg.>`，字段名固定是 `KEY`。把它授权给某个 agent：

```sh
hyprial agent secret grant <agent> \
  --source user-provider \
  --entry-id grant.nubos7mltmaedzhx4eohns3aan \
  --field-name KEY \
  --environment-name <ENV_NAME> \
  --grant-id <grant-id> --revision 1
```

（命令与参数名**对照 CLI 源码核实**：`src/hyprial/shell/impl/cli/commands/agent/secret.py` 的 `agent secret grant`；**未做**带 agent 的端到端启动实测。）
`<ENV_NAME>` 是注入给 agent 的环境变量名（例如应用期望的 `API_KEY`），`<grant-id>` 是你给这次绑定起的名字，与授权 id 无关。

给多个 agent 绑同一个 Key：

```sh
hyprial agent provider grant-key --actor <agent-a> --actor <agent-b> \
  --entry-id grant.nubos7mltmaedzhx4eohns3aan --field-name KEY \
  --environment-name <ENV_NAME> --grant-id <grant-id> --revision 1
```

值始终不出现在命令行、输出或事件里。更多说明见 [Agent 凭据](agent-secrets.md)。

> [!WARNING]
> 授权仍然有效时，不要手工删除这个条目：下一个维护周期会把它重新写回来。想停止使用，让授权人撤销授权。

### 第 3 步：被授权的服务是模型网关时，建一条指向本机映射端口的模型连接

只有当对方授权的是一个模型网关（OpenAI 兼容接口）、而你想让 agent 经由它调用模型时才需要。

**这一步目前没有 CLI 命令。** 能力在 daemon 方法 `agent.secret.provider-configure`（本地操作员权限；GUI 的凭据管理功能经它保存自定义连接）。本切片核实过 `hyprial agent provider` 下只有 `login` / `default` / `logout` / `grant-key`，没有任何子命令调用这个方法（**读代码推断**；GUI 的这一条路径也未实测）。它需要的参数是：

```json
{
  "entryId": "grant.<授权 id 去掉 sg.>",
  "fieldName": "KEY",
  "connection": {
    "version": 1,
    "endpoint": "http://127.0.0.1:<本机映射端口>/v1",
    "protocol": "openai-completions",
    "profile": "openai-chat-completions/v1"
  },
  "expectedRevision": 0
}
```

- `endpoint` 用 `http://` 时只允许 `localhost`、`127.0.0.1`、`::1`（**读代码推断**：identity 的连接校验对其它 http 主机名报 `insecure model service endpoint`）。
- 不传 `value`：沿用条目里已有的 Key。
- `expectedRevision` 必须等于这个条目当前的连接版本：从未配置过时是 `0`，每次保存后加 1；对不上时拒绝（`MODEL_CONFIGURATION_CONFLICT`），不会覆盖（**读代码推断**）。
- 保存的 Key 必须是 1 到 16384 个可见 ASCII 字符（不含空格和换行），否则这一步被拒绝（**读代码推断**：identity 的 Key 校验）。
- 这一步会**改写同一个条目文件**（在 Key 旁边加上路由元数据）。本切片认的是“`KEY` 的值仍是授权给的那个”，所以改写之后条目仍然受管，撤销授权时照样收回（**实测**：`tests/daemon/service_grants/consumer/test_keys.py`）。

## 4. 收回

授权停止对本机生效时，下一个维护周期自动收回（**实测**，同一个测试文件）：

| 触发 | 效果 |
| --- | --- |
| 授权人撤销（`hyprial org service revoke <授权 id> --org <org>`） | 映射拆掉、连接意图删除、条目从 `service list` 消失、Key 条目被删 |
| 授权到期 | 同上 |
| 授权人清空 `credential`（授权还在） | Key 条目被删；**连接保留** |
| 授权人把那台设备移出目录 | 条目、映射、Key 都收回：没有设备行就无处可连 |
| 本机离开该 org | 该 org 的授权全部收回；其它 org 的不受影响 |

收回 Key 的前提是**这条 Key 还是授权给本机的那个值**：

- 你把条目里的 `KEY` 改成了自己的值：本切片停止管理它，撤销时不删（发出 `service.grant.rejected`，原因 `entry-modified`）。
- 删除会删掉**整个条目**，包括你为它配置的模型连接元数据。
- 你自己放的同名条目（`grant.<id>`）：从一开始就不会被覆盖，也不会被删除。即使它恰好出现在 daemon 检查之后、写入之前，写入也只在条目不存在时创建，已存在就放弃并发 `entry-exists`（**实测**：`tests/daemon/service_grants/consumer_fixes/test_keys_fixes.py`）。

授权人在同一份授权里**换了密文**（例如换了 Key）：台账记着上次开封的密文摘要（只是摘要，不是明文），摘要一变，下一个维护周期重新开封，并把条目里的 `KEY` 换成新值，再发一条 `service.grant.received`；配置过的模型连接元数据保留。条目里的 `KEY` 已被你改过时照旧不动（`entry-modified`）。新密文开不了时保留原条目和原 Key，发 `credential-unopenable` 或 `key-unavailable`，之后每个周期再试（**实测**，同一个测试文件）。

daemon 重启不会重复写入，也不会多出第二条。

## 5. 被拒绝的授权

有的授权不会变成条目。daemon 每遇到一种就发一条 `service.grant.rejected` 事件（warn 级别，组件 `service`），只含 org、授权 id、授权人和原因码，不含任何密钥或明文：

```sh
hyprial log --component service --json
```

（`hyprial log` 只能按组件和级别过滤，不能按事件名过滤；用 `jq` 之类在结果里找 `"event": "service.grant.rejected"`。**这条查询用法是读 CLI 源码得到的，未在真实 daemon 上实测。**）

| 原因码 | 含义 | 怎么处理 |
| --- | --- | --- |
| `service-device-unknown` | `service.deviceId` 在授权人名下没有可用的设备行：还没发布、已被移除，或那一行本身无效被目录忽略（**实测**：设备行的代数为 0 时就是这一条） | 让授权人确认他的设备已发布到该 org 的目录，并且设备行是好的 |
| `service-device-invalid` | 设备行读到了，但设备或端口没通过 service-connect 的校验 | 授权人的设备行有问题，由授权人处理 |
| `entry-invalid` | 授权里的备注没通过 catalog 的校验，例如带了密钥样式的文本（**实测**：备注里出现 `nodekey:`）。服务名、协议和端口在授权时就已被校验，不会走到这里 | 让授权人改授权文档的备注 |
| `unnamed` | 授权没有服务名，无法成为可连接的条目 | 让授权人补上 `--name` |
| `device-conflict` | 对同一个 `deviceId` 给出了不同的公钥、地址或代数：可以是两个授权人的设备行、两个 org 的设备行，也可以是你手工放置的 `service-access.json` 里的一行与授权人的设备行。后一种情况下手工记录照常为它自己的条目服务，依赖这台设备的授权全部不生成条目、不保留 Key；两边改成一致后下一次读取即恢复（**实测**：`test_connect_fixes.py`） | 两台机器不能共用一个 `deviceId`，由其中一方改名，或改正手工记录 |
| `name-conflict` | 服务名和 `<服务名>-<授权人>` 都已被占用 | 改掉其中一个占位的服务，或让授权人改服务名 |
| `credential-unopenable` | 这份密文不是封给“本机 + 这份授权”的（被抄到别处，或授权人与设备对不上） | 不要使用；让授权人重新授权 |
| `key-unavailable` | 本机没有加密密钥、未登录，或密钥库正被占用 | 登录并让设备发布加密公钥后，下一个周期会自动补上 |
| `entry-exists` | `grant.<id>` 这个名字下已经有一条不是本切片写的条目（包括在检查与写入之间才出现的） | 换掉或挪走那条条目 |
| `entry-modified` | 本切片写的 Key 已被改成别的值 | 无需处理；这条条目不再受管 |

带 `@members` 的授权永远没有 Key：`@members` 文档里出现 `credential` 时，整份文档被 S3 的读取接口当作无效文档丢弃，既不连接也不开封，也没有事件。

Key 成功存好时发一条 `service.grant.received`（info 级别），同样只含 org、授权 id、授权人和条目名。

## 6. 本机没有加密密钥时

| 本机状态 | 连接 | Key |
| --- | --- | --- |
| 已登录，设备已发布加密公钥 | 有 | 有（如果授权带了） |
| 已登录，但没有加密密钥 | 有 | 没有，发 `key-unavailable`；不在本机创建任何密钥或锁文件（**实测**） |
| 未登录（没有已提交的账号） | **没有**：本机无法证明自己是谁，连 `@members` 的授权也不推导 | 没有 |

- 已登录但没有加密密钥：密钥就绪后的下一个维护周期自动补上 Key，不需要重启（**实测**：`test_keys.py`）。
- 未登录：没有账号的 home 上 daemon 只运行登录前的引导阶段，没有 service-connect。登录后 daemon 在同一进程里新建服务阶段，启动时照第 1 节的规则推导；登出时服务阶段整体停止（**读代码推断**：`src/hyprial/daemon/impl/application/onboarding/lifecycle/ownerless.py`，未在真实 daemon 上实测）。

## 7. 已知边界

- “读不到目录”不等于“没有授权”：某个 org 暂时读不出来时，保留上一次的结果，不收回任何 Key。
- 授权被撤销到本机收回之间有一个维护周期（默认 5 秒）的延迟；对方服务端自己的访问控制才是即时的边界。
- 启动时那次“有没有授权”的检查有 0.2 秒的预算，超时按“没有”处理，不拖慢 daemon 启动；worker 会在第一次 service 请求时补启动。预算一直传到列举和逐份读取授权文档，不只在 org 之间检查。
- 扫描上限按**授权人**计：每个授权人目录最多 256 份授权文档、按列举出的大小合计最多 512 KiB。某个授权人超限时只有他名下的授权按“未知”处理：周期性维护保留他上一次的结果（只去掉其间已经到期的授权），本机重启后没有上一次结果时，他的授权什么都不推导、他的 Key 也不收回；同一 org 里其他授权人照常读取，新授权和撤销都照常生效。启动检查里，其他授权人有有效授权就照常启动，否则回答“不启动”。
- 整个 org 另有两个上限：授权人目录最多 1024 个，周期性维护每个 org 有 2 秒预算（启动检查是上面的 0.2 秒）。超过其中任一个，整个 org 按“未知”处理：周期性维护保留上一次的结果，没有上一次结果时什么都不推导、什么都不收回；启动检查回答“不启动”。目录不变时同一个 org 不再重扫。
- 每种上限开始（或原因改变）时发一条 `service.grant.scan-limited`（warn 级别，组件 `service`），字段只有 org、`phase`（`startup` 或 `periodic`）、`reason`（`documents`、`bytes`、`grantors` 或 `deadline`），授权人超限时再加 `grantor`；同一上限持续期间不重复发（**实测**：`tests/daemon/service_grants/consumer_fixes/test_bounded_scan.py`）。
- 超限的授权人的撤销看不到：保留上一次结果意味着他已撤销的授权在本机继续生效，直到他的目录回到上限以内。整个 org 超限时，这一点对该 org 的所有授权人都成立。
