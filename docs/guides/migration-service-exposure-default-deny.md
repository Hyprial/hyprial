# 迁移说明：暴露端口默认拒绝（按服务授权放行）

适用于：把**已在组织网络上暴露服务端口**的设备升级到「暴露方按授权下发准入集合」版本的管理员，或代为操作的 agent。

一句话：**没有迁移代码，也没有专用迁移命令**。升级后，本机暴露的每一个用户服务端口在 sidecar 看来**默认谁也不放行**；只有收到有效服务授权（或该 org 的 `@members` 授权）且同时是该 tailnet 活动成员的设备才能连入。**升级前靠「同 tailnet 即可连」使用这些端口的设备，升级后会连不上，直到拿到授权。** 有授权的设备不需要升级。

> [!NOTE]
> 本文区分**实测**（在真实 `OrgFsRuntime`、真实 supervisor 与带名册的 stdio sidecar 夹具上运行验证）与**读代码推断**（由源码推导）。**未在真实两台机器上做联机验证**（见第 7 节）。

## 1. 这次改动做了什么

- 暴露方 daemon 为本机每个已暴露端口派生一个准入集合，并把它随 `expose` 下发给 sidecar（协议 v7 的 `admission`，见 [迁移说明：hyprial-tailcat stdio 协议升级到 v7](migration-sidecar-protocol-v7.md)）：
  - 该端口在**任一 org** 有一条有效的 `@members` 授权 → `admission: "allow"`（该 tailnet 的成员可连）；
  - 否则 → `admission: "keys"`，`keys` 为本机在各 org 收到的**有效**设备授权的接收方公钥并集；没有任何授权时是**空集合**，即拒绝所有连接。
- 接收方的准入公钥取自 **sidecar 自己的成员名册**（`membership-status`）里该设备那一行，不取自组织目录的设备行。
- 派生结果**不落盘**：每次都由授权与名册重新算出，所以 daemon 重启后得到同一集合（**实测**：空记忆的新对象派生出相同集合；sidecar 重启后重新收到同一集合）。
- `hyprial org service unexpose <PORT>` 成功后，**撤销本机在该端口上的全部服务授权**（**实测**）。撤销失败不会让 unexpose 失败，只记一条日志事件。
- 接入方（join）端口 `7447` 与 daemon 自身的 zenoh 入站**不受影响**（见第 6 节）。

## 2. 升级前要做什么

1. **盘点本机已暴露的端口**，以及现在有哪些设备在连它们：

   ```sh
   hyprial org service exposures
   hyprial org service grants
   ```

2. **给需要继续访问的设备准备授权命令**（升级后再执行，因为授权要求端口已暴露，且应由暴露方执行）。授权格式：

   ```sh
   # 给一台设备
   hyprial org service grant <PORT> --org <org> --device '<owner>/<deviceId>'
   # 给该 org 的全部成员（等同升级前的「同 tailnet 可连」）
   hyprial org service grant <PORT> --org <org> --device '@members'
   ```

3. 确认**同一 org 的设备都已完成服务授权文档的升级**（见 [迁移说明：组织目录新增受保护段 `service-grants`](migration-service-grants.md)）；暴露方所在 org 的 `directory/service-grants` 父目录已由 org owner 的 daemon 建好。

## 3. 升级

没有额外步骤。照常停止、安装、启动 daemon：

```sh
hyprial daemon stop
hyprial daemon status
# 安装新版本（wheel 或源码）
hyprial daemon start
```

升级后、**发出任何授权之前**，所有已暴露端口对所有人关闭。授权可以随时补发，补发后**立即**生效，不必重启（**实测**：授权与撤销都会在维护周期之外立即触发一次下发）。

## 4. 升级后授权一台设备

```sh
hyprial org service grant <PORT> --org <org> --device '<owner>/<deviceId>'
```

生效条件**同时**满足：

- 授权有效（未撤销、未过期）；
- 接收方是该 org 的 **effective tailnet** 的**活动成员**（子 org 继承父 org 的 tailnet 时按继承后的 tailnet 计）；
- 名册里该设备的行带有「针对本机 `serverPublic`」的公钥条目。

任一条件不满足，该设备不进集合，端口对它保持关闭。

## 5. 怎么判断「没人被授权」还是「授权了但取不到公钥」

两者在端口上表现一样（都连不上），日志里不同。在**暴露方**的 daemon 日志里查 `service` 组件：

| 日志事件 | 含义 | 处置 |
| --- | --- | --- |
| （没有 `service.grant.*` 事件，端口连不上），且 `hyprial org service grants` 正常列出授权、列表里没有这个端口的授权 | 没有有效授权 | 按第 4 节授权 |
| （没有 `service.grant.*` 事件，端口连不上），且 `hyprial org service grants` **报错** `org directory unavailable` | 目录读不到：列不出本机所在的 org、读不到某个 org 的授权目录，或该 org 的 tailnet 绑定无法确定。此时端口**收紧为谁也不放行**（即使授权文档还在），**并且不为这种情况记任何事件**（**实测**） | 不要重新授权，授权文档没有丢。修好目录（确认 org 目录空间唯一、OrgFS 可读）后，下一个维护周期自动恢复 |
| `service.grant.key_unavailable` | **已授权**，但暂时取不到该设备的准入公钥。字段：`org`、`port`、`grantId`、`recipient`、`reason`，**不含任何密钥** | 看 `reason`（见下表）。多数情况等 sidecar 名册刷新（约 60 秒）后，下一个维护周期自动补齐，无需操作 |
| `service.grant.roster_unreadable` | 读不到 sidecar 名册。此时所有依赖名册的授权都按取不到处理（端口收紧） | 确认 sidecar 在运行：`hyprial daemon status` |
| `service.grant.admission_limit` | 某端口的准入公钥数超过 sidecar 上限（4096）。**该端口保持上一版集合不变**，没有下发新集合 | 减少该端口的授权数量（改用一条 `@members` 授权） |
| `service.grant.admission_unavailable` | 一次派生在读授权、取名册之外的地方抛了未预期的异常（字段 `errorType`）。该次不下发任何变更，下一个节流的派生重试。**目录读不到不属于这一种**，见上一行 | 看 `errorType` 与同时间的其它日志 |
| `network.exposure.admission_rejected` | 一次 `expose` 没有得到 sidecar 的确认（字段 `port`、`code`）。`code` 为 sidecar 给出的错误码（如 `INTERNAL`，常见于 sidecar 还没 up）表示它拒绝了这次下发，端口保持原有的集合；`code` 为 `FORWARD_PROTOCOL` 表示没等到应答——命令可能没被执行（端口保持原有的集合），也可能已执行只是应答丢了（端口已是新集合）。三种情况**实测**都会在下一个节流的派生重新下发（默认每 30 秒最多派生一次），无需操作 | 偶发可忽略。持续出现时看 `code`：`INTERNAL` 确认 sidecar 已 up；`FORWARD_PROTOCOL` 确认 sidecar 进程在应答 |
| `service.grant.unexpose_revoke_failed` | unexpose 之后撤销该端口的授权失败。**unexpose 本身已成功** | 按字段 `port`、`org`、`code` 手动撤销：`hyprial org service grants --port <PORT>` 列出后逐条 `hyprial org service revoke <GRANT_ID> --org <org>` |
| `service.grant.admission_trigger_failed` | 一次改动**已经生效**（授权或撤销已写盘、新暴露已登记、sidecar 已启动），但没能在当时安排一次立即重新派生。字段：`method`（`service.grant`、`service.revoke`、`network.expose` 或 `sidecar-installed`）、`reason`（`failed` 表示安排这一步本身抛了异常；`unscheduled` 表示派生队列已满、正在关闭或不存在）；`reason` 为 `failed` 时另有 `errorType`（异常的类名，**不含异常消息**）。不含 org、端口、授权或设备信息 | 无需紧急处理：改动已持久化，维护周期（约 30 秒内）会自动重新派生。持续出现说明派生队列长期不可用；`reason` 为 `failed` 时看 `errorType` 与同时间的其它日志 |
| `service.grant.admission_tick_deferred` | 维护周期没能把一次派生交给执行队列（队列满或正在关闭） | 同上，下一个维护周期重试 |

`service.grant.key_unavailable` 的 `reason`（共 4 种）：

| `reason` | 含义 |
| --- | --- |
| `member-not-in-roster` | 接收方不是该 tailnet 名册里的**活动**成员：尚未加入、已被移出 org，或已被标记为墓碑 |
| `member-without-local-key` | 名册里有该设备，但它的行里没有针对本机的公钥条目（常见于设备刚加入，名册尚未补齐） |
| `roster-unavailable` | 该 org 的名册读不到：整份名册读不到或为空、名册里没有这个 tailnet、或该 tailnet 在 sidecar 侧尚未就绪 |
| `key-malformed` | 名册里的公钥不是规范的 `nodekey:<64 位小写十六进制>` |

### unexpose 可能要等一会儿，也可能失败

`hyprial org service unexpose <PORT>` 在取下端口之前，会先等**正在下发给这个端口的**那一次 `expose` 结束，这样端口的最后一条命令一定是 unexpose，不会被稍晚到达的下发重新打开。它只等这一个端口的下发，不等名册读取、目录读取或其它端口的下发。

- 最多等 **15 秒**。超过这个时间 unexpose **失败**，返回 `FORWARDING_UNAVAILABLE`，消息以 `is being pushed to the sidecar` 开头。此时**什么都没变**：端口仍然暴露、sidecar 上是原来的集合、授权都还有效。稍后重试即可（**实测**：上界处三者一致）。
- 取下端口这一步本身排在 sidecar 命令队列里，前面如果有一次没被应答的名册读取（最多 5 秒）或另一个端口的下发（最多 10 秒），unexpose 会跟着等，然后照常成功（**实测**：分别为 5 秒、10 秒）。这一段不受上面 15 秒的限制。
- 同一个端口上一次 unexpose 还没结束时再发一次，第二次**立刻**失败，返回同样的 `FORWARDING_UNAVAILABLE`，消息以 `is already being unexposed` 开头。以第一次的结果为准。

> [!TIP]
> 被移出 org 的设备，即使它的授权文档还在，也**不会**进集合（**实测**：名册里该设备被标记为墓碑后，下一个维护周期它即被移出集合，日志原因为 `member-not-in-roster`）。要彻底清除，仍应撤销其授权。

## 6. 不受影响的入口

- **接入方（join）端口 `7447`**：由 sidecar 进程内部在启动时创建，从不经 `expose` 下发，准入仍由 sidecar 按名册执行。`network.expose` 一贯拒绝 7447。
- **daemon 自身的 zenoh 入站**：同上，是 daemon 与对端 daemon 之间的通道，不是用户暴露的服务端口，不按服务授权放行。

这两者都不是「用户暴露的服务」，没有授权集合可言；把它们改成按服务授权放行会破坏设备互联本身。

## 7. 验证

在暴露方：

```sh
hyprial org service exposures      # 端口仍在列表中
hyprial org service grants         # 授权已列出且状态为 effective
```

在被授权设备上连一次该端口（**实测**范围见下）。

- **实测**（单机，真实 daemon 应用 + 真实 supervisor + 带名册的 stdio sidecar 夹具）：授权后下发的集合包含接收方公钥；未授权设备被拒绝；撤销后被授权方的连接被关闭；名册稍后补齐后下一个维护周期自动放行；超过 4096 不下发。
- **未验证**：真实两台设备跨网络联机（真实 tailnet、真实 sidecar 二进制）。升级后请在真实环境做一次第 4 节的授权与连接验证，再批量操作。

## 8. 回退

回退到本切片之前的版本会恢复「同 tailnet 即可连」的旧行为；已写下的授权文档仍然保留在目录里，不影响旧版本（见 S3 迁移说明第 5 节的回退说明）。再次升级后这些授权**立即重新生效**，无需重做。
