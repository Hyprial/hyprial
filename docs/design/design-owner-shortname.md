# 设计稿：集中、持久的 owner shortname 分配

> pi-shortname · 2026-09-14 · **待 Allen 选择登记器位置；不是实现完成或生产切换授权。**
>
> 目标状态：规则及实现合入 `intg/login-identity` 后，node-hq-01 生产 owner **一次切到短名**，不先切邮箱再迁一次。将既有裁定视为已生效，本单仍存在：Allen 已定“先做 shortname 规则，碰撞自动加数字后缀”。
>
> 本稿与 [身份命令面设计（#487）](design-login-identity-surface.md) 配套：只细化 Casdoor 模式的 owner 候选来源、显示和 D4 的映射步骤，不重开其生命周期、local-usage 隔离或接收连接认证边界。

## 0. 裁定、对象与结论边界

### 0.1 已定与待定

- Allen 原话：2026-09-14 00:0x“owner名字沿用现有规则，但可以支持邮箱吗？”；00:1x 建议取 `@` 前部分并清洗；00:2x“先做shortname规则，碰撞自动加数字后缀”。这些时间标签沿用派单材料，不冒充本轮重新查过飞书时间线。
- 同一 Casdoor 账号在所有机器上取得同一 shortname；不同账号不得共享已分配名字。首次分配后，邮箱/Name 变化**不触发重新命名**。多种上游登录方式须先在 Casdoor 关联到同一账号；不以“邮箱相似/相同”合并两个账号，也不能推断两个账号背后是否同一自然人。
- **本轮规格澄清已生效**：中央登记器可用认证后的 `(issuer, 组织, Casdoor User.Id 即 sub)` 作内部关联键。`sub` 不作 owner、不写入 `settings.owner`、不进入任何 URI、不作为对外显示身份。查询用受保护的请求体/服务端认证上下文，不把 sub 拼进 URL、日志或错误回显。可变邮箱/Name 不是稳定键。[旧 login 设计](hyprial-login-design.md) 的 D1“owner 不取 sub”不变。
- 候选1（Casdoor 属性）和候选3（hyprial 中央登记器）均保留给 Allen 评审。**作者推荐3，不视为已批准选择**；选定之前不写生产对象、不切生产 owner。
- 本单只新增本文件，不改 `src/`、`tests/` 或 #487 文件；后续实施另派。生产 home/daemon/login/Casdoor 写入均不在本单权限内。

澄清出处：infra-op 重派消息 `workflow-9d1fb943-f0e3-5b6b-9c28-4fcd78fcf5ec`（2026-09-14）：
“允许：中央登记器用认证后的 (issuer, 组织, Casdoor User.Id 即 sub) 作为内部关联键，找到该账号已分配的 shortname。”
前轮因“只记录”字面边界暂停；本轮不再将它列为未决问题。

### 0.2 固定证据对象

- 本仓 `HyprialOS/harness-bridge` 基线：**`07d43589e254e17e279c65ec6122c4282293118a`**，2026-09-14 00:52 +0800 `git ls-remote origin refs/heads/intg/login-identity` 核对。未提交的 `design/owner-shortname` 经明确授权 reset 到此头，不是重写已评审提交。
- 下文本仓 `src/hyprial/...:行号` 均指该基线。它相对前轮 `5cb942743af7c6569a2da8fdff67197278b59203` 只新增/修改两份 login 设计文档，源码及测试无差；**前轮测试数字仍不冒充本轮 head 上的新运行**。
- 输入 spec：`notes/spec-owner-shortname-2026-09-14.md`，交接本机副本的 Git blob 为 `ef85ba322b451f5c75be2a581fc293b5ea95a0b2`；§4 那一句以本轮上引澄清为准。
- 已检索 HQ 同对象 notes；已读 `ruling-pac-v2-capability-gaps-2026-09-13.md` 追加 C 的 pi 提交安排。按 handoff，既有“不承担 Casdoor fork 代价”的边界保留，不另提注册逻辑 fork 作为默认路线。
- Casdoor：[v4.2.0 对应源码树/提交](https://github.com/casdoor/casdoor/tree/cc39a9395698ca9cf6cf869c963a641c7c015a71) **`cc39a9395698ca9cf6cf869c963a641c7c015a71`**。下载6份文件，6/6 blob 与该 GitHub tree 一致；tree API `truncated=false`。这是源码证据，**不是生产权限配置或实际 HTTP 响应的验收**。

| Casdoor 文件（下文简称） | 行数 | blob |
| --- | ---: | --- |
| `object/user.go`（U） | 1743 | `2daf0ed4c229d4dbbbb38641b9bc34db47caefec` |
| `controllers/account.go`（A） | 893 | `e232584356d8dfc613f0a701f34af4d3f7958829` |
| `object/token_jwt.go`（J） | 717 | `04556e55dc422b07e3adc2c11a60f63d68b3cf41` |
| `controllers/auth.go`（C） | 1807 | `2d13a4438b4034a2243b72e94ecee7b4f7386c96` |
| `object/user_util.go`（V） | 1214 | `e4c2e004f84d14a7b730a5464ead9b41de5cbfb7` |
| `controllers/user.go`（CU） | 1000 | `c6887ba7f81fc1b750f132c0631a94e3f8360712` |

复核任一本仓出处用 `git show "${SHA}:<path>"`，不依赖另一人的当前 checkout。源码采集曾请求不存在的 `object/user_update.go` 得404；它不计入上述6份成功材料，也不据此下否定结论。

## 1. 为什么要做：主证据是 PAC 的真实消费链

### 1.1 邮箱能作为 URI owner，不等于所有消费者都接受

- `src/hyprial/uri.py:15-38` 与 `daemon/identity.py:53-69` 的现有 owner 语法容许邮箱，禁止冒号；不需要为本单另造邮箱 URI codec。
- `transport/keys.py:8-31` 的安全字符集不含 `@`，因此这类身份整段编码为 `~...`，日志可读性下降。这是可读性成本，不是身份冲突证明。
- **主证据**：`pac/graph.py:80-88` 的 `_naming_principal()` 调用 `daemon.identity.node_owner_or_none()`；后者 `:53-69` 按 env 优先、否则读取 `settings.owner`。`graph.py:119-125` 将该 principal 加入 known owners，但 `:128-140` 在查 known owners **之前**用 `OWNER_NAME_PATTERN` 拒绝含 `@` 的输入。因此即使邮箱 owner 已被正确配置，拿它作 PAC 节点 owner 仍会被拒。
- `org/document.py:23` 的 `_PUBLISHER` 是 `^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$`，**另有128字符上限**；`:127-138` 读取的是 YAML `meta.publisher`，不是 settings。已读调用入口为 `cli.py:3848` 文件导入、`org/store.py:139,242` 文档读取及 `org/mesh.py:196` 网络文档解析。这些路径不自动把 settings.owner 填入 publisher。用户把邮箱填入该字段会被拒，但不能写成“任意邮箱 owner 必然使 org 操作失败”。（2026-09-15 更正：该句是立单时的读数；`fix/org-publisher-at-sign` 起 `_PUBLISHER` 由单一 `is_person_name` 判定取代，接受 `owner@domain` tailnet 登录形状（如 `bob.smith@example.com`），空格/冒号/斜杠/控制字符与 128 字符上限仍拒。本设计的短名分配结论不以此为前提。）

因此不改宽 PAC 正则去掩盖命名问题，也不误把 org metadata 当作身份认证器。短名生成取上述消费者语法的安全交集；旧 URI 解析能力、local-usage 表示层保持不变。

### 1.2 现有 login 没有分配记录

`login.py:676-706` 的 `_fetch_userinfo` 只读取 `preferred_username` 和诊断用 subject；`:917-969` 将前者直接送入 owner gates，再按 credential先、owner后落盘。现有 credential 记录（`:180-201`）没有中央分配回执或来源用户名。

新阶段须是 **认证 → 读取/取得中央分配 → 既有身份切换编排**，不能在每个 home 内加一个“已用名字集合”。`settings.owner` 仍是本机有效 owner 的既有落点；新增中央账本解决的是全网名字分配，不是让 daemon 从第二个本地 owner 文件竞争取值。

## 2. 共用命名规则 R1（两个可选位置完全相同）

### 2.1 只在首次分配时计算种子

输入必须是受信 Casdoor UserInfo 的非空字符串 `preferred_username`；不以 `email` claim、显示名、主机用户名、消息 payload 或请求者自报候选代替。当前 app 的 `Name=飞书邮箱`、`tokenFields=["Name"]` 是 spec 提供的配置读数，本轮没有重读生产配置。

1. 按第一个 `@` 切分，取左侧；无 `@` 则取整个 username。不做邮箱有效性/所有权推断。
2. 做 locale-independent Unicode 小写（实现固定 Unicode/运行时版本，R1 写入记录）；不做拼音、音译、NFKC 或邮箱 `+tag` 语义归并。
3. **删除**不属于 ASCII `[a-z0-9._-]` 的字符；保留中间及末尾的点、下划线、连字符。
4. 删除开头连续的 `.`、`_`、`-`，使首字符为 ASCII 字母或数字；结果为空则拒绝，报“用户名清洗后为空”，不造 `user`/随机名/sub fallback。
5. 基础串超过128 ASCII字节时，确定性取前128字节，并记录 `seedTruncated=true`。这是本稿明确提出的长度政策，不是既有代码行为；与其它有损清洗一样，**唯一性必须交给账本**，不靠截断避免碰撞。
6. 先按稳定账号键查询已有记录；已有 active 分配返回其原 shortname，不重新运行 R1、也不因 Name 变更换名。UserInfo 缺字段等认证契约错误仍拒绝；“清洗后为空”的命名错误适用于需要新分配的账号，不撤销旧分配。

最终每个新名字满足 `^[a-z0-9][a-z0-9._-]{0,127}$`。不得修改所有 owner 的底层 grammar：这是 **Casdoor 分配规则**，不是新增 local-usage 命名禁令。

### 2.2 正反例（尚未占用时）

| 认证后的 username | 基础串 / 结果 | 判据 |
| --- | --- | --- |
| `Bob.smith@example.com` | `bob.smith` | 邮箱前缀、小写 |
| `ab12cd34` | `ab12cd34` | 无 @ 的旧8位ID原样可用 |
| `AB12CD34` | `ab12cd34` | 不含 @ 也适用小写 |
| `lin+dev@example.org` | `lindev` | 删除 `+`，不删除其后内容 |
| `a b/中:c@example.org` | `abc` | 空格、斜杠、非ASCII、冒号删除 |
| `._-Alice@example.org` | `alice` | 仅剥离开头不合法首字符 |
| `a.._-@example.org` | `a.._-` | 内部/末尾合法标点保留 |
| `A@B@C` | `a` | 精确按第一个 @，不另造邮箱解析器 |
| `éA@example.org` | `a` | `é` 不音译成 e |
| `@example.org` / `._-@example.org` / `中文` | 拒绝 | 空结果，不兜底 |
| 空串、null、数组、缺失字段 | 拒绝 | 认证输入形态错误，不转字符串 |
| 129个 `a` | 128个 `a`，截断标记 | 不绕过 publisher 上限；随后查全局冲突 |

### 2.3 数字后缀与名字空间

优先尝试基础串 `b`；已被另一个账号占用（含 pending、退役保留记录）则依次尝试 **`b-2`、`b-3`……**。选择带连字符是为了让后缀边界易读；不把“末尾有数字”反解析为账号信息。

每个候选都查**同一全局唯一索引**，不仅查同一个 b 的计数器。例：P先占 `alice`，Q自己的基础串为 `alice-2` 并占用它，则R的基础串 `alice` 必须得到 `alice-3`。天然名字与合成名字不能维护两套冲突表。

附加 `-n` 前，如长度超128，使用 `b[:128-len("-n")] + "-n"`；例如128个 `a` 的第二个占用者得126个 `a` 加 `-2`。相同截断结果、来自不同邮箱域、大小写或清洗损失导致的冲突都走同一路径。理论上后缀已不能容纳一个首字符时明确拒绝空间耗尽，不绕过唯一性。服务每次请求有时间/尝试预算；预算耗尽报 retryable/未完成，不改算法为本地后缀或随机名。

“谁先提交谁得无后缀”允许首次并发时竞争顺序不同；不承诺按字母排序预留，但一旦提交结果就永久稳定。账号注销、失败补偿、数据库维护都不释放已经保留过的 shortname；不能让后来的人继承旧 URI 的含义。

## 3. 集中账本与可信账号：两个方案都不能省略

### 3.1 内部键不是对外身份

`K=(configured_issuer, Casdoor_organization, verified_sub)` 仅存在于受保护登记器的认证上下文/存储。issuer 精确匹配受管 profile；组织来自已验证的 Casdoor 账号/受管单组织 app 约束，不接受调用者提供的组织字符串。两个组织碰巧有相同 sub 不得合并；同一注册服务覆盖的 Hyprial 网络使用**全局唯一 shortname**，不按机器、邮箱域或组织各自从无后缀重分。

推荐认证边界：登记服务接收 app-hyprial 的当前 access token（受 TLS 保护，不记录），**服务自己**向固定受信 issuer 的 UserInfo 验证；校验非空 sub、preferred_username、匹配的 iss/aud，组织用受管配置及账号目录佐证。沿用 login 的同源、禁止跨源跳转与有界网络请求原则，不信客户端提交的“已认证 sub”。如果部署 token/UserInfo 契约不能证明受众或组织，就失败，不退化成任意 bearer 能分配。

Casdoor 源码事实不能漏掉：

- [U:59-66](https://github.com/casdoor/casdoor/blob/cc39a9395698ca9cf6cf869c963a641c7c015a71/object/user.go#L59-L66)：**Owner+Name 是复合主键，Id 只是普通 index，不是 unique**。
- [U:1301-1308](https://github.com/casdoor/casdoor/blob/cc39a9395698ca9cf6cf869c963a641c7c015a71/object/user.go#L1301-L1308)、J:543：UserInfo sub、JWT subject 都来自 User.Id。
- U:1409-1411 的 `GetId()` 返回 `Owner/Name`，不是该 Id 字段；它会随 Name 改变，不能代替K。
- U:851-853 只在新 Id 为空时恢复旧 Id；V:567-574 还存在 Id account item 的修改权限路径。**不能声称 Casdoor 源码已保证 Id 永远不可修改/重用**。

因此两个方案的共同上线闸门是：受管 issuer 内 `(组织,Id)` 唯一、稳定、不回收；账号创建/导入/同步/管理员修改/恢复均纳入该契约。上线前对**完整、分页读完的目录**检查重复/空 Id；读失败或无法覆盖的组织记不可测量、禁止启用。一次扫描不替代以后的约束：必须由部署单证明普通用户无权改 Id，以及所有受管账号写入口执行同一唯一/不重用政策；做不到则该 realm 暂不接入自动分配。

运行时目录有重复、已登记K的账号生命周期不符、Id被改写/删除后重用，都隔离该关联、拒绝分配和认证映射，告警待管理员核对，**不按邮箱合并、不把一个旧K自动绑定给新账号**。Name变更只更新已证同一K的来源 metadata。目录管理者被攻陷属于信任根失效，本方案不声称能抵抗 IdP 管理员伪造身份；这不豁免检测正常配置/导入错误及测试重复Id负例。

### 3.2 耐久结构与事务

建议用控制面上的独立 PostgreSQL 数据库/角色。以下是**目标结构**，不是现有表或API：

- `allocations`：私有K（复合唯一）、`record_id`（随机审计定位符，不是sub）、`shortname`（全局唯一+grammar check）、`base`、`rule_version`、`allocated_from_username`、`current_username`、`created_at`、`state`。
- `state`：`reserved/publishing/active/quarantined/retired`；后两态仍占名字。方案3的正常新分配在一个事务中直接 active；方案1需要 publishing。
- `control_bindings`：受信控制面身份绑定及状态/版本（§6）；别名是索引，不是第二份独立分配器。
- 方案1另有耐久 `publication_intents`：待写哪个账号、哪个**已保留**的 shortname、重试状态。不得持久化 bearer/管理密钥或完整敏感用户响应。

分配事务：验证账号 → 获取数据库内**全注册名字空间的分配互斥** → 重查K已有记录 → 按§2枚举候选 → 同事务插入K/shortname/状态/审计。所有实例用同一主库、同一锁及唯一约束；锁不能只按base分，因为 `alice` 的后缀会与基础串 `alice-2` 冲突。应用重试不能把连接错误误判成“名字已占用”。

唯一约束是最后围栏：K唯一使同人多机至多一条记录，shortname唯一使不同K不共享名字；事务提交后才返回。两个客户端同时请求同一K，后到者读取首个已提交结果，不再占另一个后缀。服务提交后响应丢失，按K重试返回同一个结果；客户生成的 request id 只用于追踪，不能覆盖K或挑名字。

服务角色只获必要的账本事务权限；普通节点不直连DB，不可调用改名/释放/按别人K分配的接口。管理员也不做“DELETE后重建”式修复。更换分配后端必须搬运原记录及全部墓碑，不能在新位置重新跑R1。

### 3.3 耐久与灾难恢复

- 主库提交确认必须耐久；多实例不是多份可写SQLite。只读副本有延迟时不得用“查无记录”决定分配，写和确认读走权威主库。
- 备份/WAL/故障切换覆盖全部已确认提交；演练须验证提交后立刻断进程、换实例、跨机重试仍逐字同名。账本为空、卷丢失或版本不符时**fail closed**，不能自动初始化另一份空注册表。
- 服务初次 genesis 与正常重启区分：首次建库需部署授权，重启只接受已初始化的固定注册域。旧备份恢复在确认所有已应答分配均恢复之前禁止新分配；不能拿“SQL成功启动”作为恢复完成。
- 所有已保留名字均不自动释放，包括“客户端取消登录”的记录。清理、补偿、后台重试、最后一次重试、finally/析构及缺失 facade 都必须返回/持久化可见失败；未知不折成 absent，连接异常不产生新后缀。

## 4. 候选逐个取证与可裁方案

### 4.1 候选1：Casdoor 受保护属性 + 唯一账本 + 可信读取

**能存，不等于能由 UserInfo 发，也不等于原子唯一。**

| 读数 @ Casdoor 固定树 | 能说明什么 / 不能说明什么 |
| --- | --- |
| U:234 `Properties map[string]string` | 可持久存如 `hyprialOwnerShortname`；没有跨用户属性唯一约束 |
| [U:255-271](https://github.com/casdoor/casdoor/blob/cc39a9395698ca9cf6cf869c963a641c7c015a71/object/user.go#L255-L271)、U:1301-1391；[A:744-761](https://github.com/casdoor/casdoor/blob/cc39a9395698ca9cf6cf869c963a641c7c015a71/controllers/account.go#L744-L761) | UserInfo 是固定字段结构；`profile` + `Name`白名单发 preferred_username。**没有Properties、自定义属性投影循环** |
| J:142,293,322,583-589 | 普通JWT的用户投影含嵌套Properties；这不是当前login已可信消费JWT的证据 |
| [J:421-440](https://github.com/casdoor/casdoor/blob/cc39a9395698ca9cf6cf869c963a641c7c015a71/object/token_jwt.go#L421-L440)、J:597-603 | JWT-Custom 的 tokenFields `Properties.foo` 发顶层 `foo`。只加 tokenFields 不会让 UserInfo 发foo，也不等于普通JWT会按此重排 |
| CU:216-249,264-369；U:812-868 | 有账号读取/更新和权限检查路径；更新Properties不提供跨账号唯一分配或JSON单键CAS |
| V:833-844 | Properties权限按整张account item检查；“UI没输入框”不证明用户/API/上游同步不能写该键 |
| V:276-371 | OAuth属性同步最终更新用户；不能忽略另一个Properties写入者与整张map的覆盖竞争 |

**建议的完整实现形态**：在 Casdoor 控制面部署专用 allocator/broker，持有§3的独立耐久账本和服务端 Casdoor 权限；属性是**发布到账号上的不可变投影**。唯一性由账本保证，消费必须确认属性与该K账本一致；两个存储不一致时不选“看起来更像正确”的一边。

- 首次认证后broker按K保留名字并提交 publication intent；调用 Casdoor 受限更新，只更改受管Properties键，保留所有无关键；回读目标账号、验证组织/Id及属性一致，才把账本置active并向login返回。
- 写入者是**控制面服务身份**，不是登录客户端。Casdoor管理凭据只留服务端，按组织、API操作最小授权；若v4.2.0实际权限只能给更宽的用户更新权限，必须如实列权限差额，由隔离服务/网络ACL及审计收口，不能谎称原生有per-property管理员scope。
- 可信读取首选broker：认证仍用UserInfo；broker用授权账号读取API取得当前账号属性，校验返回K及账本。U/CU证明有该读取形态，不证明现部署的服务角色已有权限。**不用 `/get-account` 充当无副作用只读接口**：A:716-725 在session token缺失时可能取/设置access token。
- Token直读是备选，不是本稿默认：普通JWT可带嵌套属性，JWT-Custom可带指定顶层属性，但客户端须新增真实验签/issuer/audience/expiry校验、与当前UserInfo账号关联并处理属性发布后的旧token。不能对现有opaque token做base64 decode取owner。现行不随意改tokenFormat的部署边界保留；不得为“方便发属性”默改所有app。若以后审批选择token直读，仍需查已激活账本，token里有属性不等于发布事务已完成。

**缺少条件写时的明确代价**：v4.2.0 Properties更新不能天然安全地与OAuth同步的整map写并发。方案1需要可验收的写入围栏：集中写入期间暂停相关realm的账号变更入口，排空在飞OAuth/管理更新，暂停后台同步/导入，并只允许allocator的受限属性写；完成回读后解除围栏。普通用户更改受管属性必须被服务端拒绝/恢复，不靠UI。该围栏须覆盖所有写者而不是只锁allocator；需要控制面运维接线，不是已存在的Casdoor能力。

若无法接受该短维护窗口、不能覆盖后台写者或无法证明无关Properties不丢失，**候选1暂不具备上线条件**；不能靠“失败后再补写短名”豁免丢OAuth属性的风险。可另派上游条件写能力研究，不能本单默认fork或直写生产Casdoor数据库绕过对象权限。

| 失败点 | 必须留下的状态与补偿 |
| --- | --- |
| 保留前认证/目录检查失败 | 无新分配、无Casdoor写；拒绝并给非敏感原因 |
| ledger已reserved，Casdoor拒绝/超时 | 名字仍被保留，返回pending/失败；后台只重试原值，不分配下一个后缀 |
| Casdoor写成、ACK丢失或DB激活失败 | 回读同一K；一致才继续激活；不一致/读不到就quarantine，不删属性“回滚” |
| 属性已存在但ledger缺失/不同 | 拒绝接管或自动导入；进入经审计的恢复/冲突处理，不信客户端拷来的属性 |
| 无关Properties同时被改、围栏不可得 | 不发布；若已发生覆盖，告警保留证据，单独恢复无关数据；不谎称短名一致即全成功 |
| Name变更 | 用K找原分配；重新解析当前Casdoor定位符并校验K后读写，不拿旧Owner/Name写错账号 |
| 重试耗尽/清理围栏失败 | durable pending + 有界告警/人工下一步；不静默挂起、不未证安全就放开写入围栏 |

**离线login**：新账号、未发布完成、Casdoor/broker/账本不可读时均拒绝本次登录提交，不本地造名。已分配账号也不能靠离线旧属性完成新一次认证/切换；只可只读展示上次提交的身份。既有daemon继续运行/离线whoami与“新login成功”是两件事。

### 4.2 候选2：Casdoor 注册时直接短 username

[C:1059-1075](https://github.com/casdoor/casdoor/blob/cc39a9395698ca9cf6cf869c963a641c7c015a71/controllers/auth.go#L1059-L1075)：`UseEmailAsUsername`取完整email，冲突时取UUID分段追加下划线，不是R1和数字序号。V:413-419 `applyUserMapping` 跳过username；配置映射不能完成截前缀+清洗+统一数字后缀。

若修改注册源码，还要覆盖既有账号、不同注册入口及更名/并发；仅改新建用户会使老用户一直走另一套规则。与既定不承担fork成本的边界不符，**不推荐、不作为待实施捷径**。它也不能豁免账号稳定性和已分配名字不回收。

### 4.3 候选3：hyprial 控制面的独立中央事务登记器（推荐，待 Allen 定）

**拟部署位置明确**：在 `auth.hyprial.com` 所属的 Casdoor 控制面部署主机/编排项目旁，新增独立 identity-registry 服务与 `hyprial_identity` 数据库/角色/持久卷；它不是任何客户端的 daemon，也不放在 node-hq-01 的生产 `~/.hyprial`。具体物理实例、资源预算与运维负责人由部署单确认；本轮没有读取生产compose来声称资源已就绪。

入口建议为受管TLS路由（例如同受信域的 `/hyprial-identity/`，**拟议而非已上线URL**），profile固定允许的登记authority，不从任意discovery字段自动信任第三方收token地址。新机器在加入Headscale前就可访问，避免“先分配owner才能入网、先入网才能找到登记器”的循环依赖。

- 写权限：只有registry服务角色可执行§3分配事务；普通节点向服务证明自己，不请求替别人占名。服务**不需要Casdoor写权限**。若完整目录审计/组织验证需要受限读取角色，由部署单配置只读凭据，不借管理员写scope方便实现。
- 原子边界：K、shortname、metadata、active状态在一笔DB事务提交；没有Casdoor属性发布第二阶段。Name/email变更只更新同一K的metadata，owner不变。
- 认证后、commit前崩溃：没有已应答记录，事务回滚；重试重新查K。commit后响应丢失：重试读出同一已分配值。客户端本地写settings失败：中央记录保留，下一次登录返回相同值，不“退订”名字。
- 重试有界；主库不可达/主从分歧/账本损坏/未知schema明确失败，客户端不得切备用空库或本地自行分配。恢复见§3.3。
- 离线login：与方案1相同，无法认证或无法确认中央记录就不提交本次登录。旧settings及来源metadata只供离线显示、既有进程按原生命周期运行；首次登录/新机器/切换不能靠旧缓存成功。`--local-usage`是显式本机模式，不是登记器超时后的自动fallback。

这仍是 **Casdoor唯一provider指定账号 → 由账号受信登记规则得owner**，不是允许第二provider任意指定owner。登记器管理员掌握的是名字映射权限，需要独立审计和最小权限；推荐3不表示该新服务没有安全/运维成本。

### 4.4 给 Allen 的选择表

| 维度 | 1：Casdoor属性 + ledger/broker | 3：独立ledger/broker | 4：Casdoor原生Group/Role名字锁（§4.5） |
| --- | --- | --- | --- |
| Casdoor内可见shortname | 有，需保护属性并处理发布滞后 | 无；可由受限registry诊断面查询 | 有，作受管对象name；不写用户Properties |
| 不fork | 可，但需证明所有属性写者的围栏；不是纯配置即成 | 不改Casdoor写逻辑；仍须兑现稳定账号契约 | 单名Insert不需fork；128长度、认证K绑定及同K协议尚未闭合 |
| 写权限/影响面 | registry DB + Casdoor用户更新 + 写入围栏 | registry DB；Casdoor只读认证/目录 | 原生对象add/get/list；不能下放可任填K的add或app管理凭据给客户端 |
| 失败补偿 | 两存储状态机、受管属性丢失及无关map覆盖风险 | 单事务；响应丢失按K重读 | PK只保单名；还须K锚点、重读/不回收协议与真实权限边界 |
| 是否减少中央服务 | 否：全局唯一和数字后缀仍需集中协调 | 否：明确承担一个小型控制面服务 | 可复用Casdoor存储；**未证可免可信分配逻辑**，不是纯配置已成立 |
| 离线首次login | 不可；不得各机算后缀 | 不可；同左 | 不可；读不到权威对象或认证失败即拒绝 |
| 作者建议 | 仅在“属性在Casdoor内”有明确产品价值且围栏已验时选 | **仍默认推荐**，状态与权限更少；最终由Allen定 | 保留为减基础设施研究方向；本轮不能推荐其完整无新服务形态上线 |

未决的是这项产品/运维取舍，不是“是否集中”或“是否数字后缀”。设计阶段可先完成共同规则、事务、测试和接口；部署与生产切换必须等待选择，不因“给了推荐”就替Allen批准3。

### 4.5 候选4补评：Casdoor原生对象的复合主键作名字锁

依据 infra-op [PR494评审评论12030](https://code.hyprial.com/HyprialOS/harness-bridge/pulls/494#issuecomment-12030)（2026-09-14 01:23:56 +0800）：其它内容接受，仅补此候选。**结论分两层：原生PK能提供单个名字的原子占位，成立；不新建服务、不写用户Properties且满足全部已接受规则的完整方案，本轮读数不足以成立。**这不是否定Casdoor可作存储底座，也不把“不支持UserInfo属性”错误套到原生对象上。

#### 4.5.1 固定树、对象与add重名行为

仍只读 `casdoor/casdoor@cc39a9395698ca9cf6cf869c963a641c7c015a71`。本节重点核 **2类对象**，下列8份新增引用源码8/8下载并匹配该树blob，无未读取文件混作否定证据；未穷举所有Casdoor对象。原U/J/CU读数继续复用，不用本仓测试替代Casdoor运行验证。

| 简称/文件 | blob | 关键行与结论 |
| --- | --- | --- |
| [G：object/group.go](https://github.com/casdoor/casdoor/blob/cc39a9395698ca9cf6cf869c963a641c7c015a71/object/group.go) | `898e19769be0e1ae7225b80e6b17814f1da9623a` | 29–53：Owner+Name PK，Name **varchar(100)**，对象自身Properties为mediumtext map；183–195：单个AddGroup先校验后Engine.Insert；273–288另禁与任何组织Name相同 |
| [R：object/role.go](https://github.com/casdoor/casdoor/blob/cc39a9395698ca9cf6cf869c963a641c7c015a71/object/role.go) | `cd74e917a6bd9e5f0a3ae3116a7f1678505a166e` | 29–44：Owner+Name PK，Name **varchar(100)**，Description为mediumtext；155–162：单个AddRole直接Engine.Insert |
| [CG：controllers/group.go](https://github.com/casdoor/casdoor/blob/cc39a9395698ca9cf6cf869c963a641c7c015a71/controllers/group.go) | `f5e0b9516a127654ebb374cef9beab9e275dfdd0` | 30–126原生get/list回完整对象；157–171反序列化请求体、核组织权限，再AddGroup |
| [CR：controllers/role.go](https://github.com/casdoor/casdoor/blob/cc39a9395698ca9cf6cf869c963a641c7c015a71/controllers/role.go) | `191a875c8bebaf4348f1f909127251f9b587fb3d` | 31–85原生get/list回完整对象；115–129核组织权限后AddRole |
| [B：controllers/base.go](https://github.com/casdoor/casdoor/blob/cc39a9395698ca9cf6cf869c963a641c7c015a71/controllers/base.go) | `0a6e2ad509c06e92980dc73d4b77a518c937ff0e` | 79–105组织准入、app被视为global admin；284–292的wrapActionResponse将DB error变成status=error |
| [Z：authz/authz.go](https://github.com/casdoor/casdoor/blob/cc39a9395698ca9cf6cf869c963a641c7c015a71/authz/authz.go) | `8fc882495bf226fea2956de4f1881e343ef4d9ac` | 40–139内置API规则含built-in全域通配；158–208：app/global admin/同组织admin放行，其余经Enforce和CheckApiPermission |
| [F：routers/authz_filter.go](https://github.com/casdoor/casdoor/blob/cc39a9395698ca9cf6cf869c963a641c7c015a71/routers/authz_filter.go) | `e50af218b791dc9a217df3e115dd4babd0cab44d` | 139–301按query/body核真实Owner/Name；389–472调用授权并记录主体/对象名；不是校验自定义K字段的分配入口 |
| [Q：object/check.go](https://github.com/casdoor/casdoor/blob/cc39a9395698ca9cf6cf869c963a641c7c015a71/object/check.go) | `22e696d4a383429fcdb60fb5a4cff733b5807f46` | 487–607：API Permission以组织、资源path、method及user/group/role判断；Enforce输入无请求体关联K |

`Group.Properties`是**Group对象自己的字段**，不是被禁止写的`User.Properties`；Role可把关联记录作为JSON字符串放Description。两个单个add都是普通Insert：数据库实施复合PK时，重复 `(owner,name)` 引发错误并回传，**既不是静默覆盖，也不是upsert**。CG/CR没有“已存在则update”分支。R:164–175的批量AddRoles另有按`Duplicate entry`字符串处理错误的逻辑，不可拿它代表单个`/api/add-role`；本候选不走批量导入接口。

统一使用一个固定受管组织/Owner作为**整个注册域**的名字空间，不按登录者组织拆表；同一种对象中`name=R1最终shortname`，才能把天然`alice-2`与合成后缀放同一PK空间。Role较Group少一条“不能与组织同名”的校验，故若继续研究优先Role；不能一半名字放Group、一半放Role再声称全局唯一。

**长度是实质边界**：两者模型都只声明100字符，不能从此证明已接受R1的128字符可无损持久化。真实数据库/SQL模式下可能拒绝或截断，某些后端又未必强制长度；本轮未读生产schema，不替部署猜一种行为。不得把R1偷偷改成100，不得依赖静默截断或把name换成hash（那已不是本候选的`name=shortname`）。Group的组织同名校验也是额外失败条件，不是“另一个K已占shortname”的证据。

#### 4.5.2 私有关联、最小权限与普通用户边界

- **UserInfo本身不输出这些关联字段**：U:1340–1354的Groups是字符串列表、Roles投影为role.Name，不含Group.Properties或Role.Description。禁止把原始sub/K塞进Group/Role的name、成员/嵌套角色标识、URL或日志来作索引；否则名字列表也会泄漏。需要K锚点时可考虑固定编码后HMAC索引，但密钥必须留可信控制面，并明确它仍是敏感关联索引，不是公共身份。
- **有JSON字段不等于字段私密**：CG/CR上述get/list直接回完整Properties/Description，没有只隐藏K的mask。Z的内置通配API列表没有这4个get/list入口；在没有额外授权时，匿名及**非built-in组织**的普通用户不会仅因登录就获准读取；built-in主体有Z:41的内置全域通配，不能归进这条拒绝结论。但一旦授权该组织的get/list，读者可得到关联内容；不得把“需要认证”写成“只可看自己的K”。其它有权限的app/admin也在信任边界内。
- 不用真正的角色/组成员关系存K。U/J/CU中的完整用户/普通JWT存在Role对象投影（J:144,295，CU:227；R:316–343、406–412只清Users，不清Description），所以将承载私有Description的Role挂到真实用户、组或角色继承链上会扩大泄漏面。研究用锁对象须不参与授权成员/继承、LDAP/同步业务及权限匹配；`IsEnabled=false`不是字段保密机制。
- **理想最小操作集**：专用可信写者仅`POST add-role`、`GET get-role`、`GET get-roles`，只针对固定登记Owner；正常分配不需update/delete/upload，也不需用户Properties写。采用Group则对应三项group API；若下述K锚点使用另一对象类型，还须追加该类型的受限add/get（需要扫描时才加list）。单名确认用get，按K恢复若靠扫描需完整list/分页；普通节点只应读自己的最小分配结果，不能获得全表私有metadata。
- **v4.2.0能表达的权限与欠缺**：Z+Q支持API Permission限制组织/API/方法/主体；B:79–90实码对非global user检查其Owner等于目标组织，**没有再检查IsAdmin**，不要被函数注释的“other admins”误导。因而可研究登记组织内专用非admin账号的三API授权，而不是默认发管理员凭据。登录者来自其它组织则不能直接借这条同组织准入创建锁。
- Z:41的built-in通配、Z:173–193和B:93–105对app/global/组织admin的放行更宽；尤其不能把client secret/API管理凭据发到每台机器，再称其仅能分配自己的名字。API Permission并未校验`Description.K/Properties.K == 当前认证账号K`，CG/CR接受客户端提供的关联字段。给普通客户端add权限，即使不允许update/delete，也可冒别人K、抢名或污染按K查询结果；**PK不能证明锁归属的真实性**。
- 非built-in且未授权的普通用户默认不能删改；但如果给了对应update/delete API授权，G:149–181/R:95–153允许全列更新和改名，G:241–271/R:202–226能物理删除。空的锁对象不会因Group的“有子组/有成员”检查自动变成墓碑。管理员及同步/导入仍须纳入保护；权限只排除普通删改，不能替代永不回收政策和灾备。

因此“不泄sub”的可行边界是**受限对象读取留在可信侧，客户端仅收最小结果**；不是原生get接口自带按关联K的私密投影。若复用已有可信控制面进程承载验证/过滤，可以不新增一个独立部署服务，但仍须新增有权限边界的分配逻辑、服务端凭据/密钥与接线。本轮没有已存在这条入口的读数，不能把一个尚不存在的broker藏进“无新服务”四字中。

#### 4.5.3 R1、同K、ACK、墓碑与离线如何对接

| 必须兑现的性质 | 原生对象能给出的部分 / 仍须补齐的协议 |
| --- | --- |
| R1后缀枚举 | 对固定Owner的单一锁对象类型逐个Insert `b,b-2,…`。仅在确认候选被**不同可信K**占用时前进；重复错误后回读同一候选。不能把超时、权限错误、组织名冲突、超长或DB失败统统当撞名 |
| 同人多机、Name变化 | 单名PK不约束关联值K唯一。反例：两机扫描均未见K，分别持邮箱变更前后不同base，可插入两个不同name且都成功；即便同base，ACK丢失后盲进后缀也会双占。只“先list再add”不是原子K约束 |
| 可研究的无跨表事务修补 | 另设不与名字锁重叠的**不可变K锚点**（例如同Owner下Group作锚点、Role独占所有shortname，锚点name为私有HMAC(K)），首次Insert固定base/R1版本；命中锚点仍验证完整K，摘要碰撞拒绝。后到者必须读胜出锚点，然后从相同base枚举不可删除的名字锁，遇自身K即停，绝不因未知前进。两对象之间崩溃保留锚点，重试帮助完成，而不是删除解锁。它表明不能仅凭“无两列事务”就宣布原生存储绝无可能；但需可信写者、私密K验证、长度与恢复证明，不是已实现/已验证协议 |
| ACK丢失后按K重读 | 请求方重新认证得K；可信侧读K锚点及权威名字记录，或在完整受限目录中精确匹配关联值。唯一匹配原值才成功；0且前次提交状态未知继续确认，多个匹配隔离。不得因换机器/Name变化重新算base；HMAC秘钥丢失/轮换也不得初始化新锚点空间 |
| 墓碑不回收 | 锚点/名字锁均不做DELETE/rename；取消、未完成分配、退役仍占位。用保留记录/另记撤销状态阻断使用，不释放name。Casdoor备份、恢复、同步覆盖与已应答记录缺失必须按§3.3 fail closed；物理PK本身不提供永恒墓碑 |
| 离线login / dry-run | 认证、权威对象或必要关联验证任一不可得即拒绝本次login，不动旧daemon/credential/settings，不本地算名。dry-run只能查已有确定分配，不创建K锚点/名字锁；未知仍未知 |

**推荐不变的理由**：4比1少了用户Properties多写者发布问题，比3少了另建账本数据库的潜在成本；但当前证据只证明了名字侧Insert，不证明安全的客户端直写、128长度和同K永久分配。维持3作为完整方案默认推荐，1保留原条件；4作为有价值但未闭合的减基础设施方向交Allen，不暗中改已接受的R1或把它提升为生产可选的纯配置方案。此结论仅限已查Group/Role及原生API路径，不声称其它对象/已有可信扩展均不可能。

#### 4.5.4 补充验收判据（仅规划，本轮没执行）

若另派候选4原型，必须用批准的隔离Casdoor固定镜像、真实DB/schema与原生HTTP响应取fixture；本单不创建生产或测试Casdoor对象，不手写成功/重名响应替代测量。

| 编号 | 真实正/负例与事后断言 | 同一次突变的预测失败 |
| --- | --- | --- |
| C4-A（T1/T2/T4） | 并发同名add只一条原记录；不同关联值不覆盖；100/101/128和组织同名分别记录实际响应及DB值；自然后缀同空间 | 改add为upsert、截断name、把所有error当重名，各在对应持久状态断言失败 |
| C4-B（T3/T5） | 两机同K且Name跨变更、锚点/名字锁每个commit与ACK边界断进程；重启均读同值，无第二个已分配名 | 去K锚点、ACK未知即进后缀、恢复时按新Name重算，在唯一/稳定断言失败 |
| C4-C（T6/T7） | 匿名/本人/他人/服务账号/app按真实权限矩阵read/list/add；伪造关联K拒绝；UserInfo/JWT/get-user/get-role/list、错误与日志逐个检查私有值 | 放开客户端add、把K放name/成员、给Role加业务成员或直接回完整对象，在绑定/泄漏断言失败 |
| C4-D（T8/T9/T21） | 普通update/delete/rename/upload不得改变锁；取消/退役/丢卷/旧备份/密钥轮换/补偿失败不释放或重建名字空间 | 放开删改、清理删除、回退空库/新HMAC空间，在墓碑/恢复断言失败 |
| C4-E（T15/T17） | 离线拒绝新login且旧身份不动；dry-run前后K锚点与名字锁计数/内容均不变 | 缓存/本地分配fallback、dry-run创建锚点，在无身份提交/零分配写断言失败 |

以上补项沿用§7同一突变、完整测试文件、预测失败集合与真实fixture纪律。**源码阅读不是运行验证，且本仓pytest不包含这些Casdoor新协议测试**；8份源码成功读取不豁免尚欠的实际权限、长度、崩溃及信息投影验收。

## 5. hyprial 接入、兼容与一次迁移

### 5.1 login与whoami

1. 保留既有device认证/UserInfo同源约束；将 `login.py:917-969` 拆出的“候选阶段”接到登记器。内部认证信息只在受保护路径流动，对外返回 **shortname、来源username、ruleVersion、分配状态/时间、authority** 的最小投影，不回显sub或管理token。
2. `owner`候选只取active登记记录；缺失/类型错误/非法grammar/record与当前认证账号不一致皆失败，不回退`preferred_username`、email或sub。第一台取到名字、第二台只读同一记录；本地缓存不参与冲突判定。
3. #487的确认/预演/停机围栏/credential先owner后/入网及新代核验保持原职责。获取中央记录失败时在停旧daemon、写credential/settings之前返回；不要为了取名先停机。中央分配是独立耐久保留，用户之后拒绝切换也不释放。
4. settings按#487的原子提交，将shortname和显示用来源metadata一起落到既有记录，保留其它键。不新增另一个本地owner真源。`HYPRIAL_OWNER`冲突照旧拒绝；同名还要比mode/issuer，不能跳过local→Casdoor的模式切换。
5. `whoami`（#487 S1）邻接显示有效shortname、配置owner、mode、登记authority、上次提交的来源username。username含邮箱时标“来源用户名（邮箱）”，无@则不假装有email；保留`allocatedFromUsername`与`lastAuthenticatedUsername`的区别。邮箱变化只改后者。**离线只读，上次观测不写成当前已验证；旧记录缺来源就unknown，不猜、不联网刷新。**sub不进入人类输出/JSON公共投影或可复制URI。
6. #487的`login --switch-account --dry-run`必须对中央读取也有边界：只查已存在active分配，不触发新分配或属性发布。无分配时明确“尚无中央分配，无法确定最终owner”，可以显示非授权的基础串预览，但不能预测为最终shortname。正式login才允许reserve；不能把“本地零写”偷换成“预演允许写远端分配记录/shortname属性”；正常OIDC认证本身可能产生IdP登录记录，不把这一点伪报为整个IdP零写。

### 5.2 已有home及生产顺序

- 现网owner状态沿用spec提供的事实：生产尚未切邮箱；实验home `~/.hyprial/profiles/headscale` 是邮箱owner。本轮不打开它、不运行迁移。下一实施/演练单先用隔离副本，不拿该路径默认授权。
- 实验邮箱home：上线登记器并认证得到shortname后，显式switch-account，使用#487 S2/S3的完整预演、停写复核和新代验证，一次邮箱→shortname迁移；不直接编辑settings绕过六库两文本的迁移。
- 生产hq：**先完成中央登记与同人跨机读数，再预演旧owner→最终shortname**；不要先落完整邮箱。实施依赖登记选择/部署、login候选接线、迁移剩余风险闭合及#487生命周期切片，不是合入本文即可切换。
- `owner_migration.py:452-469` 的六库两文件清单复用，live地址经现有anchored规则处理，host-login/历史列/非owner文本保持既定边界。对user目标等已有迁移裁定不在本单扩大：先用真实schema分类，未覆盖项明确失败并另派修正，不在设计里承诺“替换所有邮箱文本”。
- 迁移后验证有效owner、daemon内存owner、模式与新epoch、实际数据分类/计数；partial commit按#487保留阶段结果，不能只改回settings宣称回滚。旧名字是否保留为中央墓碑须在生产预演盘点并明确归属后登记，**禁止仅凭本地自报old owner就抢占任何中央名字**。
- 已分配短名的重登不变；服务后端1↔3的切换只搬原账本/墓碑，逐条比对，绝不重新分配。用户名变化和算法R2升级都不自动改历史shortname。

## 6. D4：控制面身份经同一记录映射，不能各机截邮箱

本节精确替换#487 §3.1中“控制面provider username直接等于owner”的比较步骤，不取代其M0可信连接前置：

```text
接收端拥有的连接 + 代际
 → 正确profile的WhoIs(remoteAddr)
 → 可信控制面节点/UserProfile
 → 受管控制面账号绑定（同一中央登记域）
 → 查该账号已分配且active的shortname
 → 比较自称owner；machine仍比可信节点名
 → 成功后才允许相关持久化/投递/收据
```

- `WhoIs`输入来自接收连接，不来自消息自报IP/source_info。registry只映射身份，不能使#487已证明可伪造的Zenoh字段变可信，也不解决多跳原作者/最后一跳差异。M0没验收时D4仍不可上线。
- 控制面上的Casdoor Name仍可为邮箱。解析器在registry内用**经证实绑定到K的控制面账号记录**找同一个allocation；不把邮箱前缀当owner，不按本机已有名字补后缀。映射只读；消息到达不能触发为发送者首次分配。
- `control_bindings`建议绑定 `(控制面注册域,稳定的控制面user标识/代际)` 与内部K；Name仅为受管alias。绑定从受信控制面账号资料和Casdoor目录对应关系建立/更新，不接受发送者自报配对。建立绑定的后台身份只有受管目录读取及绑定写权限，不获任意用户shortname改名权。
- **只有Name、无法证明生命周期的情况必须点名**：邮箱/Name被删除后重用，旧Headscale节点仍可能带同名字串；不能把它自动映射到新人的shortname。旧alias标retired/ambiguous，不自动转移；须验证稳定控制面user标识及其账号关系，无法取得就拒绝unknown。Name更名同理更新已证同一K的alias，shortname不变。
- 现有#487 §4.3只有 pinned `tailscale.com v1.102.3` 的WhoIs返回Node/UserProfile以及接线缺口读数；**尚未捕获本部署Headscale的稳定user标识、重命名/删除重建响应**。已有部分读数不能升级成绑定已可用。下一步M0在获准测试节点抓真实响应，验证本段键/代际可表示，再实现adapter；不存在的字段不能用桩补齐。此未核阻塞D4映射上线，不阻塞shortname规则/分配器设计；不豁免未来D4工作和正确性。
- receiver的映射查询需经认证、限定同一登记域，只回该查询所需shortname/状态/版本，不返回sub、完整邮箱目录或任意枚举入口。增加可读映射会扩大网络成员对名字的可见性，必须有权限/速率限制与访问审计，不能说“只读所以无权限影响”。
- authority签发的有界版本缓存可减少逐消息请求，但不接受发送者附来的映射；过期、撤销/代际不明、查询超时、404/坏结构分别可诊断地拒绝，无inbox commit/接受收据/presence污染。离线whoami可显示历史缓存，不代表D4可凭过期缓存授权。

## 7. 测试、真实形态与可证伪矩阵（实施单必做）

以下T编号/用例描述均是**待实现判据**，不是本轮新增了这些测试。本单不改src/tests，不能把旧套件绿或纸面矩阵称为shortname“红过绿回”。

### 7.1 fixture先取真实形态

- Casdoor：固定上述v4.2.0树/镜像，在批准的隔离实例由原生User/GetUserInfo/JWT序列化生成，记录版本/hash。分别采集`tokenFields=[Name]`、有/无scope、普通JWT与JWT-Custom属性、账号读/写权限失败的原始形态，按字段脱敏；不手写一个“userinfo含shortname”的JSON。
- 登记器：使用实现时真实schema/migrations及真实事务数据库，两独立服务进程/客户端，不用一个mock字典证明跨机唯一；进程/网络中断要打到commit/ACK真实边界。
- 迁移/home：复用已有store和六库两文本fixtures，含WAL与混合owner；不自造理想schema来避开残留。D4使用M0真实socket/WhoIs形态；未有该capture则测试前置未满足，不补假字段转绿。

| 测试 | 修法与正/负例，必须断事后状态 | 同一次实现突变必须失败于 |
| --- | --- | --- |
| T1 R1表 | §2逐行参数化；空/坏类型；128/129边界；所有输出符合grammar | 去lower、漏删冒号/首标点、空值fallback、长度off-by-one各对应断言 |
| T2 冲突全集 | 跨域同前缀、清洗/截断同名；自然`alice-2`先占后`alice`冲突得`alice-3` | 只用base计数器或不查全局索引，最终两个K同名 |
| T3 同人多机 | 两独立客户端同时首次请求同K，恰一记录、同名；不同Name的后续登录仍原名 | 客户端算后缀、每次按Name重分配、忽略K唯一约束 |
| T4 不同人并发 | 不同K同base、相交自然后缀同时请求；所有应答与DB一一对应 | 去事务/全局锁/shortname唯一约束，预测并发时重复或分配不一致 |
| T5 commit/ACK | kill分配进程分别在commit前/后、丢响应、换实例重试；未确认无伪成功，已确认同名 | 应答早于commit、响应丢失后增加后缀 |
| T6 认证K | 他人自报sub、错issuer/aud/org、缺sub、目录重复Id/读不到、Id重用、token已失效 | 信客户端K、缺aud仍放行、把重复/unknown当新账号 |
| T7 信息投影 | settings.owner、URI、whoami/JSON、异常/日志均无sub或token；来源Name只作metadata | 反射序列化把内部K泄入公共投影，或sub作fallback owner |
| T8 不变与墓碑 | Name/email变化、算法升级、账号退役、旧名字历史保留；新账号不继承旧名 | update覆盖shortname、删除释放、按邮箱自动合并 |
| T9 恢复 | 同耐久库重启/故障切换；丢卷/旧备份/schema错不得初始化新账本 | 异常时create-empty-db、读副本miss即新分配 |
| T10 属性通路 | 方案1：原生UserInfo确实不含属性；broker正确读回；JWT备选用真实签名和过期负例 | mock式从UserInfo取属性、仅decodeJWT或信任缺失/旧属性 |
| T11 属性权限 | 本人、上游映射、服务角色与管理员分权；不允许复制别人的属性；无关map保真 | 只关UI输入、开放整个update接口给客户端、盲覆盖map |
| T12 发布补偿 | 方案1每个reserved/write/readback/activate边界崩溃；账本与属性一致才active | 先active后写属性；写超时即重分配；ledger缺行自动接管属性 |
| T13 写入围栏 | 方案1真实并发OAuth同步/管理更新/后台任务，排空后才写；失败不放开未验证围栏 | 只锁allocator、漏一个map写者、吞解除围栏失败；除了owner还断无关Properties |
| T14 单事务失败 | 方案3认证/DB/commit/响应各故障；pending/失败可见，不写Casdoor | 把DB错误当冲突、rollback后仍返回成功、任一Casdoor写调用 |
| T15 离线/取消 | 两方案首次/切换离线拒绝且旧进程/credential/settings不动；已有身份只读显示；取消不释放保留名 | 超时fallback邮箱/本地名、先停机后查registry、取消删除分配 |
| T16 login编排 | #487同名跨mode仍切换；credential先、owner后；环境override冲突；中央回执与认证账号匹配 | 从preferred_username写owner、同名提前跳过mode门、信客户端缓存 |
| T17 dry-run | 已有分配只读、无分配报不可确定；分配记录数及Casdoor受管shortname属性前后不变，IdP认证自身记录另列 | 把get-or-allocate用于预演，远端出现reserved行/属性写 |
| T18 迁移 | 旧owner直接到最终shortname；六库两文件、历史边界、幂等；坏/缺库分别报；无第二次邮箱迁移 | 漏库/substring替换/先落邮箱/只改settings；按完整文件跑不能-k藏多红 |
| T19 D4映射 | 同一真实控制面账号在两receiver映射同名；伪造owner拒绝；registry lookup不分配 | 截@/本地后缀/收到消息便分配；错误仍commit/发成功收据 |
| T20 D4生命周期 | Name重用、旧节点、绑定过期/tagged/未知、WhoIs失败、port复用；不降级；多跳仍受M0约束 | 仅按当前邮箱查新人、信payload IP/source_info/缓存过期放行 |
| T21 边缘失败 | 清理/补偿线程/finally/缺facade/最后重试逐条故障注入，durable状态和外部非成功一致 | catch后只log、else默认空、后台失败静默，针对预测分支变红 |

每个突变先写**预测失败测试集合**，在整个相关测试文件跑，不用`-k`过滤红；记录collected及实际失败集，恢复**相同突变**再跑同一文件，不能换一个突变冒充证明。正常并发测试反复压力运行也不能替代数据库约束与真实崩溃边界。

实施改动还要点名仓级守卫：新事件→启动事件白名单，新import→CLI/daemon导入边界，新身份拼接→URI构造守卫，新错误码→IPC注册表；随后全仓 `ruff check .`、焦点/状态邻域、CI同尺的全量 `pytest tests/ -q`，日志直接落盘。未知/setup错误不被记成“0个失败”。

## 8. 落地次序、邻近事件与验证边界

1. **评审选择**：Allen决定1或3，以及R1长度/后缀政策；中央集中与数字后缀已定不重开。服务物理资源/运维责任、稳定Id契约及权限capture由部署切片补齐。已知源码读数足以写本稿，不足以冒称权限已上线。
2. **实现切片**：共同R1+事务/恢复+认证适配；所选位置的服务/权限/补偿；login/来源投影/dry-run；隔离迁移；M0后D4绑定。每片对应§7测试与同一次突变。候选1额外依赖全写者围栏，不能夹在客户端登录改动里默默引入。
3. **生产切换门**：中央已持久分配、另一机器读回同值、权限/恢复负例通过、完整迁移预演无未分类、#487切换围栏已实施。满足后另获部署授权，才一次切短名；本设计PR不是任何生产操作许可。
4. 邻近事件必须覆盖：提交成功但ACK丢失（同K重读）；名字没变但mode变了（仍切换）；Casdoor Name重用但旧Headscale节点尚在（不能认新人）；旧备份成功启动但遗漏已确认分配（禁止分配）。这些不是有意留白，均已进矩阵。
5. 有意不覆盖：自然人多账号自动合并、管理员攻陷IdP后的身份真实性、M0尚未解决的多跳原作者证明、生产基础设施部署操作。这些边界不豁免账号关联不误合并、短名唯一性、对不可验证输入的拒绝和后续实施工作。
6. 本轮没有新增功能实现/测试，也**未执行T1–T21的实现红→绿**。它们必须由后续实现验收补齐；设计单边界仅豁免本轮落实现，不豁免设计正确性、真实fixture及后续红绿义务。旧行为测试结果随本PR回执提供具体head/命令/日志，不能证明中央服务已存在。

## 9. 与 #487 的双向阅读索引

本单不修改已合入的 #487 文件，故不声称在那里已经新增反向链接。将两份稿的对应关系完整列在本文件；未来统一整理时可把本节链接补回命令面文档，而不在并行工作中代改对方文件。

| 从 #487 读到这里 | 从本稿返回 #487 |
| --- | --- |
| §1.2、§2.2 的Casdoor owner候选 | 本稿§2–5替换“直接取username”为中央已分配shortname；生命周期仍遵守[#487 的 §2.2](design-login-identity-surface.md) |
| S1 / §2.1 whoami | 本稿§5.1补shortname与来源username，不显示sub；只读协议仍见[#487](design-login-identity-surface.md) |
| S2 / §2.4 dry-run | 本稿§5.1补“中央只读lookup、不reserve”；六库两文本预演仍见[#487](design-login-identity-surface.md) |
| §3.1 D4 | 本稿§6替换比较链中的Name→shortname一步；M0连接归属/多跳欠项仍见[#487 的 §3.1](design-login-identity-surface.md) |
| §2.5 local-usage | **不被本稿改严**：本地模式仍可用邮箱形owner，不能因中央shortname新规误加保留名前缀/联网fallback |

#487 当前 `blob=8bd8c0e7a0cf064f0f2d4278673da71dfb47788a`。本稿仅取代上表点名的“Casdoor username直接作为owner”的解释，不把旧文档中所有“邮箱合法”字样一概裁掉。
