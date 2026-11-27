# 升级说明：把已有 org 升级到服务授权版本（总说明）

适用于：管理一个**已有** org、要把其中设备升级到支持服务授权（`hyprial org service grant|revoke|grants`）版本的管理员，或代为操作的 agent。

这个版本同时带来四件改动，它们是**同一个版本**，不能分开升级：

| 改动 | 分项说明 | 为什么影响升级顺序 |
| --- | --- | --- |
| 目录设备行带上设备加密公钥 | [迁移说明：目录设备行携带设备加密钥公钥](migration-device-encryption-key.md) | 旧版本读到带新键的设备行会把整行判为无效并丢掉（实测） |
| 组织目录新增受保护段 `service-grants` | [迁移说明：组织目录新增受保护段 `service-grants`](migration-service-grants.md) | 父目录只有 org owner 能建；旧版本副本拒收授权文档（读代码推断） |
| `hyprial-tailcat` stdio 协议升到 v7 | [迁移说明：hyprial-tailcat stdio 协议升级到 v7](migration-sidecar-protocol-v7.md) | 同一台机器的 daemon 与 sidecar 必须一起换 |
| 暴露端口默认拒绝，按授权放行 | [迁移说明：暴露端口默认拒绝](migration-service-exposure-default-deny.md) | 暴露方一启动，它的所有暴露端口对所有人关闭，直到写下授权 |

四份分项说明解释各自的细节；**升级顺序以本文为准**。设计见 [service-grant 设计](../design/service-grant-2026-11-27.md)，接收方的操作见 [服务授权：接收方指南](service-grants-recipient.md)。

> [!NOTE]
> 没有迁移代码，也没有专用迁移命令。本文的步骤由代码与单机实测推出；**整套顺序没有在真实的多机 org 上走过一遍**（见第 6 节与 [已知问题](../reference/known-problems.md) KP-134）。第一次在生产 org 上升级前，建议先在一个可丢弃的两到三台设备的 org 上照做一遍。

## 1. 先认清三个角色

一台设备可以同时担任多个角色。

- **org owner**：org 目录空间的 owner。只有它的 daemon 能为已有 org 建出 `directory/service-grants` 父目录。
- **暴露方**：用 `hyprial org service expose` 暴露了端口的设备。升级后它要为这些端口写授权。
- **接收方**：连别人暴露的端口的设备。分两种：已经有手工放置的 `service-access.json` 的，和以后只靠授权的。

> [!IMPORTANT]
> **暴露方不是 org owner 时，org owner 必须先在线。** 父目录建好并同步到暴露方之前，暴露方的 `hyprial org service grant` 一律失败（`SERVICE_UNAVAILABLE`，“org service-grants directory is not initialized yet”），而升级后暴露方的端口在写下授权之前对所有人关闭。owner 不在线，这些端口就一直打不开。

## 2. 升级前

1. **定一个停机窗口，整个 org 一起升级。** 做不到让所有设备在同一窗口内升级，就先不要升级任何一台：
   - 旧版本读到新版本发布的设备行会把它丢掉，新旧混跑时互相看不见（实测，见设备加密钥说明第 2 节）；
   - 旧版本副本拒收 `service-grants` 段的文档，非 owner 成员在旧副本上的目录树更新也会被拒（读代码推断，见受保护段说明第 3 节）；
   - daemon 与 sidecar 的 stdio 协议 v7 不兼容 v6（同机必须一起换）。
2. **在每个暴露方上盘点端口和使用者**，写下升级后要补的授权：

   ```sh
   hyprial org service exposures
   ```

   升级前的规则是“同 tailnet 的活动成员都能连”。记下每个端口实际上有哪些设备在用，以及它们分属哪个 org。
3. **记下每个 org 的目录空间名**（`group-<name>` 形式，例如 `group-acme`）。授权命令的 `--org` 要求这个完整名字，写成 `acme` 会被拒绝。
4. （可选，建议）每台设备停掉 daemon 后备份整个 `HYPRIAL_HOME`。本次升级不改写已有文件，备份只用于回退。

## 3. 升级顺序

### 第 1 步：停掉所有设备

在 org 的**每一台**设备上：

```sh
hyprial daemon stop
hyprial daemon status
```

原因：新版本一旦有一台设备启动并发布了设备行，未升级的设备就看不见它（见上文）。先全部停下，再一台台启动，混跑窗口为零。

### 第 2 步：每台机器同批安装 daemon 与 sidecar

在每台设备上安装新版本的 hyprial（wheel 或源码），**同时**换上新版本的 `hyprial-tailcat`。源码安装要重新构建 sidecar；用 `HYPRIAL_TAILCAT_BINARY` 指定 sidecar 路径的环境要指向新二进制。确认版本：

```sh
hyprial-tailcat version
```

输出里应为 `"v":7`。原因：v7 sidecar 拒绝 v6 的命令，反之亦然（`SIDECAR_PROTOCOL_MISMATCH`）。

### 第 3 步：先启动 org owner

```sh
hyprial daemon start
```

原因：owner 的 daemon 在发布自己的设备行时，为本机是 space owner 的每个 org 补建 `directory/service-grants`。设备行发布周期是：sidecar 写出地址文件后立即发布一次，之后至多每 10 秒一次。建父目录在 daemon 进程内每个 org 成功一次即止；失败记一条 `org.directory_roots.failed`（组件 `org`），下次发布重试。

确认父目录已建（在 owner 或任意已启动的成员上）：

```sh
hyprial org fs space list --json
hyprial org fs ls <spaceId> directory --json
```

列表里出现名为 `service-grants` 的目录即已建。不能用 `hyprial org service grants` 判断：目录缺失时它同样返回空列表。还没建时，查 owner 日志里的 `org.directory_roots.failed`，或由 owner 执行一次 `hyprial org create <org> --json`（对已有 org 幂等，返回 `"existed": true`，同样会补建）。

### 第 4 步：启动接收方与其它成员

在其余**不暴露端口**的设备上 `hyprial daemon start`。

原因：
- 按设备授权时，`service.grant` 要求接收设备在目录里已有设备行，否则返回 `SERVICE_DEVICE_UNAVAILABLE`；
- 带 Key 的授权还要求接收设备已经发布加密公钥（同上错误码，消息 “recipient device has not published an encryption public key”）。加密公钥只在设备已登录、设备记录带 `deviceUid` 时才会发布，条件见设备加密钥说明第 3 节；
- 暴露方放行一台设备，要等该设备成为暴露方 sidecar 名册里的活动成员、并带有针对暴露方的公钥条目。名册在 sidecar 内约每 60 秒刷新一次。接收方先启动，这段等待就和暴露方的启动重叠。

接收方此时连不上暴露方的端口：暴露方要么还停着，要么还没写授权。这是预期的。

### 第 5 步：启动暴露方并立即补发授权

```sh
hyprial daemon start
```

**从这一刻起，暴露方的所有暴露端口对所有人关闭**：daemon 启动时把每个暴露以空集合重放给 sidecar，直到授权写下并派生一次。所以启动后立刻补发授权：

```sh
# 恢复升级前“同 org 成员可连”的行为：每个端口、每个 org 一条
hyprial org service grant <PORT> --org group-<name> --device '@members' --name <服务名>

# 只给一台设备
hyprial org service grant <PORT> --org group-<name> --device '<owner>/<deviceId>' --name <服务名>

# 只给一台设备，并把本机保存的 Key 一起交给它（必须带 --name）
hyprial org service grant <PORT> --org group-<name> --device '<owner>/<deviceId>' \
  --name <服务名> --credential <entryId>[:<fieldName>]
```

要点：

- **`@members` 只放行该 org 的成员设备。** 升级前同 tailnet 的成员都能连；升级后，一条 `@members` 授权只放行写它的那个 org 的在册成员设备。同一端口被几个 org 的成员使用时，每个 org 各写一条。子 org 继承父 org 的 tailnet 时，子 org 的 `@members` 不放行只在父 org 里的设备。
- **`--name` 决定接收方能不能自动得到服务条目。** 不带 `--name` 的授权只放行；已经有 `service-access.json` 的接收方够用，只靠授权的接收方什么也得不到。带 `--credential` 时必须带 `--name`，否则授权被拒绝（`INVALID_ARGUMENT`）。
- 授权写下后**立即**触发一次准入派生，不用等周期（准入派生周期默认至多每 30 秒一次）。
- 刚启动时暴露方自己的设备行可能还没发布，`service.grant` 会以 `SERVICE_DEVICE_UNAVAILABLE`（“this device has not published its row …”）失败；等几秒（设备行发布周期）后重试。
- 暴露方同时是 org owner 时，第 3 步与第 5 步是同一台设备：先确认父目录已建，再补发授权。

确认：

```sh
hyprial org service grants --org group-<name>
```

`Granted:` 下列出刚写的授权，状态为 `effective`。暴露方日志（组件 `service`）里出现 `service.grant.key_unavailable` 时，看它的 `reason`：`member-without-local-key` 与 `member-not-in-roster` 多数在接收方上线、名册刷新（约 60 秒）后的下一次派生自动消失，处置见默认拒绝说明第 5 节。

### 第 6 步：接收方

- **已有 `service-access.json` 的接收方**：不用做任何事。暴露方放行它之后，原有映射照常工作（新开一个连接即可；升级期间断开的连接不会自己恢复）。
- **只靠授权的接收方**：daemon 在发现第一份指向本机的有效授权后自动启动 service-connect：启动时检查一次；启动后才收到的，至多约 60 秒内自动启动，也可以执行任意一条 `hyprial org service list|guide|connect|disconnect` 立即启动。之后按 [接收方指南](service-grants-recipient.md) 第 3 节 `connect`、绑定 Key。

## 4. 端口不可用的窗口

对一个暴露端口，升级期间连不上的时间从**暴露方停 daemon** 开始，到下面三件事都完成为止：

1. 暴露方启动并写下授权（第 5 步）；
2. 一次准入派生把集合下发给 sidecar（授权写下后立即触发）；
3. 每个接收方都是暴露方 sidecar 名册里的活动成员、带有针对暴露方的公钥条目（名册约每 60 秒刷新）。

把 owner 和接收方放在暴露方之前启动（第 3、4 步），就是为了让第 3 件事在暴露方启动前已经就绪，窗口只剩“暴露方启动到授权写下”这一段。暴露方是 org owner 时，还要加上父目录建好的时间（设备行发布周期，通常几秒）。

窗口内已建立的连接被关闭；窗口结束后不会自己恢复，客户端要重新连接。

## 5. 回退

回退也必须**整个 org 一起**：daemon 与 `hyprial-tailcat` 一起退回旧版本，先全部停、再全部换、再启动。

- 暴露端口恢复旧行为：同 tailnet 的活动成员都能连。
- 设备行：旧版本 daemon 在下一次发布设备行时用不带加密公钥的旧格式行覆写自己的行，之后重新可见。还带着新键的行（仍在新版本上，或离线没启动旧版本的设备）会被旧版本持续丢弃（见设备加密钥说明第 6 节）。
- 授权文档留在目录里，旧版本副本拒收它们，不影响旧行为（读代码推断）。
- 接收方写下的 Key 条目（`grant.<id>`）留在原处，旧版本不管理也不删除它们。
- 再次升级后，仍然有效的授权**立即重新生效**，不用重做；接收方按台账接着管理原来的 Key 条目。

## 6. 尚未验证

- 整套顺序没有在真实多机 org 上走过；窗口长度（第 4 节）是推算，不是测量值。
- 授权文档在两个 daemon 之间的真实复制、接收副本的作者校验、非 owner 成员在 owner 建的父目录下写自己的子目录，都只在单机共用存储上测过。
- 暴露方 sidecar 入站看到的对端公钥是否等于名册里的 `clientPublicByServer[暴露方 serverPublic]`，只有读 sidecar 源码的依据。
- 回退后旧行为恢复、再升级后授权立即生效，是读代码推断。

这些都登记在 [已知问题](../reference/known-problems.md) KP-134。
