# 迁移说明：暴露端口默认拒绝（按服务授权放行）

> [!IMPORTANT]
> 这是同一版本四项改动之一。**升级顺序以 [升级说明：把已有 org 升级到服务授权版本](migration-service-grants-upgrade.md) 为准**；本文只解释暴露端口这一项的行为与排错。

适用于：把**已在组织网络上暴露服务端口**的设备升级到「暴露方按授权下发准入集合」版本的管理员，或代为操作的 agent。

一句话：**没有迁移代码，也没有专用迁移命令**。升级后，本机暴露的每一个用户服务端口在 sidecar 看来**默认谁也不放行**；只有收到有效服务授权（按设备的授权，或该 org 的 `@members` 授权）、同时是该 org 的 tailnet 活动成员的设备才能连入。**升级前靠「同 tailnet 即可连」使用这些端口的设备，升级后会连不上，直到拿到授权。** 接收方设备与暴露方属于同一个 org 时，必须与暴露方在同一窗口一起升级（原因见总说明）。

> [!NOTE]
> 本文区分**实测**（在真实 `OrgFsRuntime`、真实 supervisor 与带名册的 stdio sidecar 夹具上运行验证）与**读代码推断**（由源码推导）。**未在真实两台机器上做联机验证**（见第 7 节与 [已知问题](../reference/known-problems.md) KP-134）。

## 1. 这次改动做了什么

- 暴露方 daemon 为本机每个已暴露端口派生一个准入集合，并把它随 `expose` 下发给 sidecar（协议 v7 的 `admission: "keys"`，见 [迁移说明：hyprial-tailcat stdio 协议升级到 v7](migration-sidecar-protocol-v7.md)）。集合是下面两部分的并集，跨该端口的全部 org：
  - 按设备的**有效**授权：该接收设备的准入公钥；
  - `@members` 授权：写这条授权的 org 的**成员设备**的准入公钥。只算同时满足两条的设备：是该 org 的 tailnet 名册里的活动成员，且属于该 org 本身。子 org 继承父 org 的 tailnet 时，只在父 org 里的设备不算。
- 没有任何授权时集合为**空**，即拒绝所有连接。daemon 不再对用户暴露端口下发 `admission: "allow"`。
- 接收方的准入公钥取自 **sidecar 自己的成员名册**（`membership-status`）里该设备那一行，不取自组织目录的设备行。
- 派生结果**不落盘**：每次都由授权与名册重新算出，所以 daemon 重启后得到同一集合（**实测**：空记忆的新对象派生出相同集合；sidecar 重启后重新收到同一集合）。
- 派生的时机：授权、撤销、暴露、sidecar 安装之后**立即**一次；此外按**准入派生周期**重算，默认至多每 30 秒一次。
- `hyprial org service unexpose <PORT>` 成功后，**撤销本机在该端口上的全部服务授权**（**实测**）。撤销失败不会让 unexpose 失败，只记一条日志事件。
- 接入方（join）端口 `7447` 与 daemon 自身的 zenoh 入站**不受影响**（见第 6 节）。

## 2. 升级前要做什么

1. **盘点本机已暴露的端口**，以及现在有哪些设备在连它们、各属于哪个 org：

   ```sh
   hyprial org service exposures
   ```

2. **准备授权命令**（升级后再执行：授权要求端口已暴露，且由暴露方执行）。`--org` 写完整的目录空间名 `group-<name>`：

   ```sh
   # 给一台设备
   hyprial org service grant <PORT> --org group-<name> --device '<owner>/<deviceId>' --name <服务名>
   # 给该 org 的全部成员设备（升级前「同 tailnet 可连」在该 org 内的等价物）
   hyprial org service grant <PORT> --org group-<name> --device '@members' --name <服务名>
   ```

   同一端口被几个 org 的成员使用时，每个 org 各写一条 `@members`。`--name` 可省略，但省略后只靠授权的接收方得不到服务条目（见 [接收方指南](service-grants-recipient.md)）；带 `--credential` 时必须带 `--name`。

3. 停机、安装、启动的顺序按 [总说明](migration-service-grants-upgrade.md) 第 3 节：org owner 先启动、接收方次之、暴露方最后，暴露方启动后立即补发授权。

## 3. 升级之后、补发授权之前

暴露方 daemon 启动时把每个暴露端口以空集合重放给 sidecar。**补发授权之前，所有已暴露端口对所有人关闭。** 授权补发后**立即**生效，不必重启（**实测**：授权与撤销都会在准入派生周期之外立即触发一次下发）。

## 4. 授权一台设备

```sh
hyprial org service grant <PORT> --org group-<name> --device '<owner>/<deviceId>' --name <服务名>
```

生效条件**同时**满足：

- 授权有效（未撤销、未过期）；
- 接收方是该 org 的 **effective tailnet** 的**活动成员**（子 org 继承父 org 的 tailnet 时按继承后的 tailnet 计）；
- 名册里该设备的行带有「针对本机 `serverPublic`」的公钥条目。

`@members` 授权另外要求该设备属于写授权的那个 org。任一条件不满足，该设备不进集合，端口对它保持关闭。

**续期或换 Key**：对同一端口、同一接收方再执行一次 `grant`。已有有效授权时原地更新那一份：授权 id 不变，接收方的服务条目名也不变。没有有效授权（从未授权、已撤销、已过期）时才新发一个授权 id。

**文档数上限**：本机在一个 org 里名下的授权文档上限 256 份。已撤销或已过期超过 7 天的文档，在本机下一次授权或撤销时自动删除。清理之后仍有 240 份时，新发授权 id 被拒绝，错误码 `SERVICE_UNAVAILABLE`，消息以 `service grant document limit reached in org` 开头（原地更新不受限）。无法解析的文档不会被清理。`hyprial org service grants` 在 `Grant documents:` 一节按读到的每个 org 列出本机名下的 `<份数>/256`（`--json` 里是 `documentCount` 与 `documentLimit`）。清理删掉文档时记 `service.grant.cleaned`（`org`、`grantor`、`grantIds`）；删除失败记 `service.grant.cleanup_failed`（warn），不影响这次授权或撤销本身。

不带 `--name` 授权时，命令输出多一行 `Note: no --name, so the recipient is only admitted and will not get a service entry automatically.`；原地更新时首行是 `Updated:` 而不是 `Granted:`。

## 5. 怎么判断「没人被授权」还是「授权了但取不到公钥」

两者在端口上表现一样（都连不上），日志里不同。在**暴露方**的 daemon 日志里查 `service` 组件：

```sh
hyprial log --component service --json
```

（`hyprial log` 只能按组件和级别过滤；按 `event` 字段在结果里查找。）

| 日志事件 | 含义 | 处置 |
| --- | --- | --- |
| （没有 `service.grant.*` 事件，端口连不上），且 `hyprial org service grants --org group-<name>` **正常**列出授权、列表里没有这个端口的授权 | 没有有效授权 | 按第 4 节授权 |
| `service.grant.directory_unavailable` | **目录读不到**，端口收紧为谁也不放行（即使授权文档还在）。字段：`org`（列不出 org 时为空）、`step`（见下表）。**不带**授权、路径或目录的报错文本（**实测**） | 不要重新授权，授权文档没有丢。按 `step` 修目录；修好后下一次准入派生自动恢复（**实测**）。**同一个 `org` + `step` 只在开始失败时记一条**，之后持续失败不再重复；目录恢复后再次失败才会再记一条（**实测**）。所以「只有一条」不代表问题已过去：以被授权设备在目录修好后的下一次准入派生恢复可连为准（**实测**）；目录修好后 `hyprial org service grants --org group-<name>` 也不再报错（**读代码推断**） |
| `service.grant.key_unavailable` | **已授权**，但暂时取不到该设备的准入公钥。字段：`org`、`port`、`grantId`、`recipient`、`reason`，**不含任何密钥** | 看 `reason`（见下表）。多数情况等 sidecar 名册刷新（约 60 秒）后，下一次准入派生自动补齐，无需操作 |
| `service.grant.roster_unreadable` | 读不到 sidecar 名册。此时所有依赖名册的授权（含 `@members`）都按取不到处理（端口收紧） | 确认 sidecar 在运行：`hyprial daemon status` |
| `service.grant.admission_limit` | 某端口的准入公钥数超过 sidecar 上限（4096）。**该端口保持上一版集合不变**，没有下发新集合 | 减少该端口的授权。`@members` 也按成员设备逐台计入，换成 `@members` 不会减少计数 |
| `service.grant.admission_unavailable` | 一次派生在读授权、取名册之外的地方抛了未预期的异常（字段 `errorType`）。该次不下发任何变更，下一次准入派生重试。**目录读不到不属于这一种**，见 `directory_unavailable` 一行 | 看 `errorType` 与同时间的其它日志 |
| `network.exposure.admission_rejected` | 一次 `expose` 没有得到 sidecar 的确认（字段 `port`、`code`）。`code` 为 sidecar 给出的错误码（如 `INTERNAL`，常见于 sidecar 还没 up）表示它拒绝了这次下发，端口保持原有的集合；`code` 为 `FORWARD_PROTOCOL` 表示没等到应答——命令可能没被执行（端口保持原有的集合），也可能已执行只是应答丢了（端口已是新集合）。三种情况**实测**都会在下一次准入派生重新下发（默认至多每 30 秒一次），无需操作 | 偶发可忽略。持续出现时看 `code`：`INTERNAL` 确认 sidecar 已 up；`FORWARD_PROTOCOL` 确认 sidecar 进程在应答 |
| `service.grant.unexpose_revoke_failed` | unexpose 之后撤销该端口的授权失败。**unexpose 本身已成功** | 按字段 `port`、`org`、`code` 手动撤销：`hyprial org service grants --port <PORT>` 列出后逐条 `hyprial org service revoke <GRANT_ID> --org group-<name>` |
| `service.grant.admission_trigger_failed` | 一次改动**已经生效**（授权或撤销已写盘、新暴露已登记、sidecar 已启动），但没能在当时安排一次立即重新派生。字段：`method`（`service.grant`、`service.revoke`、`network.expose` 或 `sidecar-installed`）、`reason`（`failed` 表示安排这一步本身抛了异常；`unscheduled` 表示派生队列已满、正在关闭或不存在）；`reason` 为 `failed` 时另有 `errorType`（异常的类名，**不含异常消息**）。不含 org、端口、授权或设备信息 | 无需紧急处理：改动已持久化，准入派生周期（约 30 秒内）会自动重新派生。持续出现说明派生队列长期不可用；`reason` 为 `failed` 时看 `errorType` 与同时间的其它日志 |
| `service.grant.admission_tick_deferred` | 准入派生周期没能把一次派生交给执行队列（队列满或正在关闭） | 同上，下一个周期重试 |

> [!WARNING]
> 不带 `--org` 时，`hyprial org service grants` 照常列出其余 org，读不到的 org 打印一行 `Not read: <org> could not be read; its grants are unknown, not absent.`（`--json` 里是 `unreadableOrgs`）。所以判断「没有有效授权」时，先看有没有这一行；带上 `--org group-<name>` 时，同一个故障直接报 `org directory unavailable while attempting to list service grants`；列不出 org 时，不带 `--org` 报 `org directory unavailable while attempting to list orgs`（**实测**）。日志里有没有 `directory_unavailable` 仍是最直接的依据。

`service.grant.directory_unavailable` 的 `step`：

| `step` | 含义 |
| --- | --- |
| `orgs` | 列不出本机所在的 org。此时没有具体的 org，`org` 为空 |
| `grants` | 读不到该 org 的授权目录。该 org 的目录空间不唯一（同名目录空间多于一个）时也是这一种，因为读授权要先定位 org 的目录（**实测**） |
| `tailnet` | 授权读到了，但该 org 的 tailnet 绑定定不下来。这一种同时会对每条读到的授权另记一条 `service.grant.key_unavailable`，`reason` 为 `tailnet-unresolved`（**实测**；实测时替换了解析函数，见下方「已知限制」） |
| `grants-limited` | 本机在该 org 名下的授权文档超出读取上限（256 份，或按列举大小合计 512 KiB），按读不到处理。撤销不用的授权；已撤销或已过期超过 7 天的文档会在下一次授权或撤销时删除 |
| `devices` | 子 org 继承父 org 的 tailnet 时，还要读子 org 自己的设备目录来判断谁属于这个 org；这一步读不到。只影响该子 org 的授权 |

`service.grant.key_unavailable` 的 `reason`（共 7 种）：

| `reason` | 含义 |
| --- | --- |
| `member-not-in-roster` | 接收方不是该 tailnet 名册里的**活动**成员：尚未加入、已被移出 org，或已被标记为墓碑 |
| `member-without-local-key` | 名册里有该设备，但它的行里没有针对本机的公钥条目（常见于设备刚加入，名册尚未补齐） |
| `roster-unavailable` | 该 org 的名册读不到：整份名册读不到或为空、名册里没有这个 tailnet、或该 tailnet 在 sidecar 侧尚未就绪 |
| `tailnet-unresolved` | 名册读到了，但该 org 的 tailnet 绑定定不下来，无从取该 org 的名册行。与 `roster-unavailable` 的区别：这种情况 sidecar 是在应答的，要查的是 org 的目录绑定 |
| `key-malformed` | 名册里的公钥不是规范的 `nodekey:<64 位小写十六进制>` |
| `not-org-member` | 子 org 继承父 org 的 tailnet：接收方在父 org 的名册里，但不是这个子 org 的设备 |
| `org-devices-unavailable` | 子 org 继承父 org 的 tailnet：子 org 自己的设备目录读不到（同时记 `directory_unavailable`，`step` 为 `devices`） |

事件里的 `recipient` 是对象：按设备的授权为 `{owner, deviceId}`，`@members` 授权为 `{"recipient": "@members"}`。`@members` 授权只在整个 org 取不到时记 `key_unavailable`（如 `roster-unavailable`）；个别成员没有公钥条目不单独记。

`key_unavailable` 按 `(org, grantId, reason)` 去重：同一条授权、同一个原因只在开始时记一条；原因改变，或恢复之后再次出现，才再记一条。

### unexpose 可能要等一会儿，也可能失败

`hyprial org service unexpose <PORT>` 在取下端口之前，会先等**正在下发给这个端口的**那一次 `expose` 结束，这样端口的最后一条命令一定是 unexpose，不会被稍晚到达的下发重新打开。它只等这一个端口的下发，不等名册读取、目录读取或其它端口的下发。

- 最多等 **15 秒**。超过这个时间 unexpose **失败**，返回 `FORWARDING_UNAVAILABLE`，消息以 `is being pushed to the sidecar` 开头。此时**什么都没变**：端口仍然暴露、sidecar 上是原来的集合、授权都还有效。稍后重试即可（**实测**：上界处三者一致）。
- 同一个端口上一次 unexpose 还没结束时再发一次，第二次**立刻**失败，返回同样的 `FORWARDING_UNAVAILABLE`，消息以 `is already being unexposed` 开头。以第一次的结果为准。

### 已知限制

- **取消暴露要排在其它 sidecar 命令后面（转发层的串行队列）。** 取下端口这一步本身不是立即执行的：转发层一次只向 sidecar 发一条命令，unexpose 排在它前面每一条命令的后面。前面那条命令若迟迟不被应答，unexpose 就跟着等，等它超时为止——名册读取最多约 5 秒，一次端口的下发最多约 10 秒（**实测**：分别为 5 秒、10 秒；被取消的端口自己并没有在途的下发时也是 10 秒）。这一段**不受上面 15 秒的限制，也不会因此失败**，只是晚一点成功。前面若排着多条都不被应答的命令，等待按条累加（**读代码推断**，未实测）。登记为 [KP-135](../reference/known-problems.md)。
- **被 sidecar 拒绝的下发不会立即重试。** 一次下发被拒后，要等下一次准入派生（默认至多每 30 秒一次）才重新下发（**实测**会重推；30 秒是默认配置值，未实测）。拒绝记为 `network.exposure.admission_rejected`。登记为 [KP-136](../reference/known-problems.md)。
- **`network.exposure.admission_rejected` 的 `FORWARD_PROTOCOL` 分不清两种情况。** 命令根本没被执行，和命令已执行只是应答丢了，记成同一条事件。两种情况都会在下一次准入派生重新下发，无需区分（**读代码推断**，重推本身**实测**）。
- **`tailnet-unresolved` 这条路径没有在真实目录上触发过。** 在能构造出来的真实目录故障里，读授权与解析 tailnet 绑定对「目录是否歧义」的判定相同，读授权总是先失败，所以这种故障记为 `step: "grants"`。`step: "tailnet"` 与 `reason: "tailnet-unresolved"` 是防御性的：实测时用替换解析函数的方式触发（**实测**，替换了解析函数）。
- **到期按各自的时钟判断。** 暴露方与接收方各用本机时钟比较 `expiresAtMs`；两台机器时钟不一致时，到期的时刻也不一致。登记为 [KP-137](../reference/known-problems.md)。

> [!TIP]
> 被移出 org 的设备，即使它的授权文档还在，也**不会**进集合（**实测**：名册里该设备被标记为墓碑后，下一次准入派生它即被移出集合，日志原因为 `member-not-in-roster`）。要彻底清除，仍应撤销其授权。

## 6. 不受影响的入口

- **接入方（join）端口 `7447`**：由 sidecar 进程内部在启动时创建，从不经 `expose` 下发，准入仍由 sidecar 按名册执行。`network.expose` 一贯拒绝 7447。
- **daemon 自身的 zenoh 入站**：同上，是 daemon 与对端 daemon 之间的通道，不是用户暴露的服务端口，不按服务授权放行。

这两者都不是「用户暴露的服务」，没有授权集合可言；把它们改成按服务授权放行会破坏设备互联本身。

## 7. 验证

在暴露方：

```sh
hyprial org service exposures                    # 端口仍在列表中
hyprial org service grants --org group-<name>    # 授权已列出且状态为 effective
```

在被授权设备上连一次该端口（**实测**范围见下）。

- **实测**（单机，真实 daemon 应用 + 真实 supervisor + 带名册的 stdio sidecar 夹具）：授权后下发的集合包含接收方公钥；未授权设备被拒绝；撤销后被授权方的连接被关闭；名册稍后补齐后下一次准入派生自动放行；超过 4096 不下发。
- **未验证**：真实两台设备跨网络联机（真实 tailnet、真实 sidecar 二进制）。升级后请在真实环境做一次第 4 节的授权与连接验证，再批量操作。

## 8. 回退

回退到不支持服务授权的版本会恢复「同 tailnet 即可连」的旧行为；已写下的授权文档仍然保留在目录里，不影响旧版本（见 [迁移说明：组织目录新增受保护段 `service-grants`](migration-service-grants.md) 第 5 节）。回退必须整个 org 一起做，见总说明第 5 节。再次升级后仍然有效的授权**立即重新生效**，无需重做（**读代码推断**）。
