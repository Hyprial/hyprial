# 设计稿：hyprial 身份命令面与接收方身份边界

> 作者：astra-loginid · 2026-09-13 · **待 infra-op 评审；本稿不是实现完成声明，也不授权生产切换。**
>
> 目标状态：经 infra-op 评审后合入 `intg/login-identity`，据此切片派实现，最终由集成分支的 WIP PR 合入 dev。
> 将既有裁定视为已生效，本单仍存在：Allen 2026-09-13 已定下述 D1–D4，2026-09-14 补定 owner 沿用现有语法、支持邮箱；本轮按评审修订设计稿。
>
> 本稿承接 [既有 login 设计](hyprial-login-design.md) 的身份/provider/profile/迁移边界；只取代下表点名的旧裁定。
> [sidecar-via-login](design-sidecar-via-login.md) 已由 #463 实现，其下载同意形态不重开。
> [agent 身份设计](design-agent-identity.md) 的 owner/machine/actor 与运行分离保持不变。

## 0. 裁定与证据对象

### 0.1 Allen 三句原话（逐字引用输入材料 §0）

1. 「修改 owner name 的能力实际上应该放进 hyprial 命令中,和 login whoami 等形成关联」
2. 「我建议实际上应该在 switch-account(或者没有 login 第一次 login 的时候)自动停止/重启」
3. 「按照只能通过 casdoor login 的方式来处理,但增加 `hyprial login --local-usage --owner=xxx` 等命令形式,有必要的时候用户可以绕过我们提供的基础设施去使用」

### 0.2 Allen 对 D1–D4 的答复（逐字引用）

> D1 login自己起，看门狗是临时操作，未来需要都删掉；D2 不能，只允许一个provider去指定owner；
> D3 这个问题的本质应该是怎么避免本地owner和线上登录系统的owner混淆？本地设置的身份只在用户本地生效，我们完全不用管；
> D4 “对端”是谁，为什么要校验身份？

输入材料另记：**D4 定（Allen 21:4x「可以,请去做设计稿」）**，针对 §3.1 的接收方方向。
前提是同一 tailnet 用户默认可信、tailnet 身份已经校验；要补的是 **hyprial 自报身份不得冒充另一 tailnet 身份**，不是重做成员表或 Casdoor 消息 token。

D3 表示层补充裁定（2026-09-14 00:0x，infra-op 本轮派单转录）：Allen 原话「owner名字沿用现有规则，但可以支持邮箱吗？」；infra-op 已答可以。采用现有 owner 语法（非空、不含 `:`），邮箱形 owner 合法；这是传输语法，不是防混淆的命名政策。其余 D1–D4 不变。

本稿采用输入材料及上述补充裁定已经明确的解释，不重新裁语义：

- **D1**：login 自己停/起 daemon。cron 看门狗是临时设施，**终态删除**，不得把等待 cron 拉起包装成 login 成功。
- **D2**：local-usage **不入网**；同一网络的 owner 只由一个 provider（Casdoor）指定。
- **D3**：不靠限制本地名字、保留名前缀或禁止与线上重名来防混淆。隔离靠模式和网络边界。
- **D4**：接收方从可信连接来源取 tailnet 身份，再核自称 owner/machine；发送方自报或写入 payload 的字段不是认证依据。排在 mesh 切到自建 Headscale 之后，见 §3.1。

### 0.3 被取代的既有裁定（保留，不删除历史）

| 既有裁定及出处 | 状态与取代范围 | 仍生效的部分 |
| --- | --- | --- |
| **D-U5-1**：`login 只检查「daemon 已停」并拒绝(DAEMON_RUNNING),停启由操作者/runbook 做;⛔ login 不编排 daemon 生命周期。` hq `6ea911cffdd17edd96fc5c35248c635d6c955c4c:notes/spec-h2b-login-u5-switch-2026-09-04.md:69` | **已被取代**：默认 login 编排，依据原话 2 / D1 | 不得在旧 daemon 活着时改其 owner；这条安全目的保留，实现从“拒绝用户”改为“停机并证明退出” |
| **D-U2-5 / U5**：删除 `init --owner` / `squire setup --owner`；`login and HYPRIAL_OWNER are the only ways in`。hq 同 SHA 的 `notes/spec-h2b-login-u2-identity-2026-09-04.md:85`；本仓 `src/hyprial/daemon/identity.py:80-89` | **部分已被取代**：增加 `login --local-usage --owner` 这个显式本机入口；login 不再仅指 Casdoor 流程 | 不恢复两个已删的 owner flags；不恢复 getpass fallback；env override 顺序不变；override 不能成为入网认证证据 |

旧 `hyprial-login-design.md` §3.2 的“缺 owner 只提示”、§5.2/§5.4 的人工停启步骤也由本稿 §2.2/§2.3 更新；原文留作历史。旧文里的 D1–D12 是 **2026-09-04** 那组编号，不是本稿的 D1–D4。

### 0.4 固定基线与材料

- 仓库：`HyprialOS/harness-bridge`。
- 开工权威读数：`git ls-remote origin refs/heads/intg/login-identity refs/heads/dev` 两者均为 **`5cb942743af7c6569a2da8fdff67197278b59203`**；本 worktree HEAD 同值，分支 `design/login-identity-surface`。
- **本文所有本仓 `file:line` 均已在该 SHA 核过**，不是沿用草稿的安装版行号。复核用 `git show "${SHA}:<path>"`，行号不跟未来分支漂移。
- 输入：hq 上述 SHA 的 `notes/design-login-identity-surface-2026-09-13.md`，SHA256 `16dbfa6b458ad1daba918ac56d124b5980d8bf220a3a20adbb6c85afca4c880b`；它是输入草稿，不是本稿成稿。
- 修订来源：[PR #487 评审 #11936](https://code.hyprial.com/HyprialOS/harness-bridge/pulls/487#issuecomment-11936)（infra-op，2026-09-13 22:17:23 +0800）提供 join/cron 更正；本轮派单 `workflow-16dda569-d2b2-5d3b-a464-2148776e2239`（会话 `wf-run-38d2f6fb7acb-astra-loginid`）补充 Allen 的 D3 表示层裁定与邮箱边界读数。修订开工 `ls-remote`：PR head=`98eeb5b7e7c29723bfa5f17da82ec92fdb5819e2`、base 仍为上述固定基线。
- 时间证据边界：草稿标记答复在 21:3x / 21:4x，本轮机器时钟开工读数为 21:38；原样保留材料标注，不将其当成独立验证的飞书时间线。
- 本轮只读源码、依赖接口与隔离实验；没有运行生产 login、停起生产 daemon、修改生产 home/cron/Casdoor/Headscale。上一单遗留债：**无新增交接债**；本稿识别的实施前置见 §4、§6。

### 0.5 具名例外 U6：`init` 的 self-host 分支（2026-09-18，Allen 裁定）

本条是**新增具名例外**，不改写上文 D1–D4 与 Login U5 的任何既有裁定条目（调研 `docs/notes/selfhost-login-research-2026-09-18.md` §3 的选项一，Allen 已裁）。Allen 原话（2026-09-18，逐字）：

1. 「不用另开命令。现在文档中说的是用hyprial init，当用户执行init发现没有身份的时候，给出用户选项：使用self-host还是hyprial 服务。如果使用self-host，但没有安装tailscale，则直接失败」
2. 「login只在选择使用我们服务的时候需要」
3. 「self-host用户身份就以tailnet身份为准，取消手动输入，如果返回的是tag而不是真实的用户名，那是用户的选择，self-host用户应该可以自己处理这个问题」

前置条件与边界（全部同时成立才适用本条）：

- **仅在 `hyprial init` 的 self-host 分支**：分流点是 init 发现本机没有身份（`resolve_node_owner` 取不到 owner）时的交互式选择；不新开命令，不给 `login` 加子命令面。选 Hyprial 服务 → 走既有 login（OIDC → sidecar → join）路径，行为不变。非交互（`--json` 或 stdin 非 TTY）问不了人，按惯例失败 `USER_ACTION_REQUIRED`，不替用户选默认。
- **owner 来源是 tailnet 断言**：self-host 分支的 owner 取宿主 tailnet 身份（`tailscale whoami --json` 的 `UserProfile.LoginName`），**不接受手敲输入**。检测不到已安装的 tailscale 直接失败，不兑底、不退回 Hyprial 服务；不连我们的登录平台、不取 sidecar、不 join 我们的 headscale。whoami 在 tagged 节点上返回节点 DNS 名时照常采用，但把采用的 owner 明确打出来。
- **落点不变**：仍是 `settings.json` 的 owner + mode/issuer 同一笔记录（`write_settings_identity`），不新增第二份身份真源；mode 用新增具名值 **`tailscale-selfhost`**（联网但非 Casdoor，区别于不入网的 `local-usage`；issuer 必须为空）。
- 与 D2 的关系：在用户自己的 tailnet 上，whoami 的 LoginName 是**该用户自己控制面**（官方 tailscale 或其 headscale）断言的用户名，可论证为 D2「同一网络的 owner 只由一个 provider 指定」中 provider = 宿主控制面的精神；本条仅以此解释为限，不重写 D2 原文。
- **已知缺口（记录，不随本条实现）**：接收方按 tailnet 身份校验 owner（§3.1 D4）尚未实现（M0 未验收），因此 self-host 用户的 owner 在接收侧目前没有已建校验可依附；跨网信任姿态待 D4 裁定，本条不宣称已解决。
- 实现记录：分支 `impl/selfhost-init-choice`（2026-09-18）；`identity.py` 的「入口」表述已同步为三条具名入口（login / init self-host 分支 / `HYPRIAL_OWNER`），D7 两句钉住的话未动。

## 1. 现状、公共身份模型与不变量

### 1.1 已有落点（不另造 owner 真源）

| 对象 | 当前读数 @ 固定基线 | 设计影响 |
| --- | --- | --- |
| owner | `daemon/identity.py:53-69,72-119`（本表路径均在 `src/hyprial/` 下）：env > settings > 明确失败；坏 settings 不折成缺失 | 复用 resolver，新增可诊断来源投影，不复制另一份解析政策 |
| 登录 | `login.py:850-973`：认证、门禁、credential、owner 写入在一个函数；`:783-812` 同名提前返回/活 daemon 拒绝 | 分开“取候选身份”与“提交”，否则 dry-run 无法零写入；同名也要比较模式/issuer |
| credentials | `login.py:180-201,266-329`：只有 version/issuer/refreshToken/obtainedAt | **没有可推算 refresh 到期日或已认证用户名的完整本地记录**；不得从 obtainedAt 硬加 30 天 |
| 节点摘要 | `network_profile.py:80` 路径 `state/tsnet`；`tsnet_join.py:722-749` 写 hostname/ip/user/joinedAt | 是历史显示摘要，不是当前连接或 owner 授权证据；不从它覆盖 settings |
| daemon 身份 | `daemon/application.py:639,659-661` 构造时解析 owner 并迁移；`:3300-3342` ping 含内存 owner、pid、epoch；`:3440-3446` ps 同样含 owner | whoami 读 ping，而非把 settings 值伪装成 daemon 值 |
| 迁移 | `daemon/owner_migration.py:452-469` 六库两文件；`:472-497` 每库一个事务；`:500-560` 从 agents 检测旧 owner | 预演必须覆盖六库两文件；跨库不是原子事务；不能宣称“任何失败都自动回滚” |

### 1.2 模式不是另一份用户名，也不是凭证

在同一 `settings.json` 中将 `owner`、`identityMode`（`casdoor` / `local-usage`）、`identityIssuer`（Casdoor 时来自所选 profile）作为同次提交的非机密记录，保留其它 settings 键。local-usage 的 issuer 为空，不保留“当前已认证”外观。它们供本机模式隔离和显示，**本地用户能改 settings，因此这些字段不能授权对端**。

- 身份切换比较 `(mode, issuer, owner)`，不是只比 owner 字符串。local → Casdoor 同名仍是切换，反向同理。
- 旧 settings 无模式：显示 `unknown/未记录`，不因 token 文件存在或名字看起来正确就显示 Casdoor 已认证。旧节点运行兼容现状，但 D4 认证上线门不得据此放行；只有真正完成登录/显式本机选择才能写入模式。
- `HYPRIAL_OWNER` 仍优先。whoami 同时显示有效 owner 与磁盘 owner、冲突。login 不擅自 unset override。**同名 override、模式字段或旧 token 均不能让 local-usage 入网。**
- 不增加第二个 durable owner 文件，不储存派生 URI alias。`identityIssuer` 不允许在一张网里引入第二 provider。
- owner 沿用现有传输语法、支持邮箱，见 §2.5；不另加 local 前缀或线上重名禁令来代替模式隔离（D3）。

### 1.3 观测与动作共同遵守的规则

未知不是否：缺文件、坏文件、权限错误、IPC 超时、进程身份不可读分别报告；只有缺失才能表示 absent。错误码在实现时进入 `contracts/ipc_errors.py` 注册表；本文用阶段名描述新失败，不伪称已有新错误码。

身份/网络/daemon/迁移各有结果：例如身份已提交、入网失败、新 daemon 未起，命令总体非成功，身份结果仍为已提交；不能用一个 `ok` 抹平 D12 的身份/网络分离。

增加 whoami/预演可达性只增加**本机诊断权限**，不授予改 owner、发跨机消息、停其它 home 的 daemon 或读取其它用户文件的能力。诊断不打印 token、preauthkey、原始 credential 内容或未脱敏异常值。

## 2. 五个命令面

### 2.1 `hyprial whoami [--json]`：只读、离线、无需 daemon

建议稳定输出层次（是目标字段契约，不是当前实测返回体）：

```text
identity: effectiveOwner / source / configuredOwner / mode / issuer / warnings
profile: name(org) / source / home / stateDir / controlPlane(kind,url)
credential: status(absent,present,unreadable,invalid) / obtainedAt / expiresAt(unknown)
node: status(absent,recorded,invalid) / hostname / ip4 / user / joinedAt
 daemon: state(running,stopped,unknown) / pid / epoch / owner / mode / observation
```

- 一屏把模式紧邻 owner 印出；local-usage 固定标“本机模式，身份未经认证”。JSON 与人类输出同一投影。
- Casdoor `preferred_username` 显示为**上次已提交的 Casdoor owner**（有相应 mode/issuer 元数据时）；env override 另列。旧记录不能推出，写 unknown；不假造持久化 username。
- refresh token 只报告是否存在、obtainedAt；未记录过期时间则到期未知。不得解析任意 opaque refresh token 来猜 JWT，也不联网刷新/验证。
- 读本地 socket 的 **ping**，有界一次；成功读取的是 daemon 内存身份。配置与运行 owner **或 mode/issuer** 不同必须告警。现有 ping 未含后两者，S1/S5 增补；缺字段不得当作相同。
- ping 超时不能判 stopped；结合已记录 PID/birth marker 能明确死亡才判 stopped，无法确定则 unknown。`home_guard.live_daemon_pid()` 对损坏/过期记录返回 None（`:86-124`），故不能独自作为“确定已停”的尺子。
- 不触发 daemon 启动、profile 写入、home 初始化、目录创建、credential 刷新或 app 安装。缺 home 也能打印 absent 和 login/init 下一步。
- 现码落点：`cli.py:1023` `_execute` 的 home 门、`:1354-1373` `_daemon_probe`、`:8759` 动态命令挂载；只读命令不能复用会创建 home 的入口。`daemon/application.py:3300-3342` 是内存身份观察口，**不使用 ps**，后者在 `:3426-3427` 还会过期清理 route，不是纯观察。现 `_daemon_probe` 在 METHOD_NOT_FOUND 时回退 ps，故 whoami 要直接走有界 ping，旧代不支持则标 unknown/unsupported，不能直接继承该 fallback。

### 2.2 `hyprial login`：自动编排，取代“请先手工停机”

#### 参数组合与默认动作

| 形状 | 目标行为 |
| --- | --- |
| 首次 Casdoor login，无 owner | 建必要 home 骨架（不先起无身份 daemon）→ sidecar 同意步骤 → 获取候选 → 提交身份 → 按 profile 入网 → 自动起 daemon并核验 |
| 同 mode/issuer/owner 重登 | 更新 credential，幂等身份；活 daemon 不重启；daemon 未运行则自动起。不能忽略磁盘/内存身份冲突 |
| `--switch-account` | 候选认证 → 显示/确认 → 在线副本预演 → 停旧代并证退出 → 停写后复核 → 提交 → 入网/新代启动 → 后验 |
| `--no-daemon`（新增） | 不停、不起，明确 `daemon=not-attempted`；**需要改变身份且旧代仍活/未知则拒绝提交**，不能成为制造双身份的后门。已停时提交后报告迁移 pending 与下一步 |
| `--skip-join`（已有） | 仅不执行 enrollment；不是 local-usage，不证明无网络，也不表示 daemon 完成。与 `--no-daemon` 各司其职 |
| `--only-install-sidecar`（已有） | 保持仅安装、不要求登录、仍要求已初始化 home（`cli.py:735-785`）；不进入本编排，也不拉起 daemon |

`--local-usage` 与 sidecar 安装、preauthkey、join 相关参数组合报参数冲突，不能悄悄走某一半；`--owner` 仅在 local-usage 合法。`--dry-run` 必须与 `--switch-account` 一起，不能与 sidecar 安装旗标组合；`--no-daemon` 对 dry-run 不额外赋权，预演本来就不碰生命周期。

#### 一次切换的阶段与完成围栏

1. **prepare**：解析明确的 home/state/profile 和当前身份，预检查 settings 能读取。保留 sidecar 下载的来源/tag/version/sha256 同意形态（`cli.py:724-775`）；旧代仍在跑时不写新 owner。OIDC 候选及新 token 暂存内存，不能调用当前 `run_login` 的写入尾段。
2. **confirm + plan-live**：沿用 `--switch-account` 显式授权；JSON 下 flag 是确认（现码 `cli.py:832`），TTY 需输入 yes；非 TTY 非 JSON 不问无人可答的问题。预演发现 unclassified / 读错 / 多旧 owner 即拒绝，**身份、credential、生产迁移零写入**，不停止 daemon。
3. **stop + exited**：持有 home 级身份事务互斥后重读当前代，抓取 pid **及 birth identity**，shutdown；调用严格 `require_exit=True` 路径，而不是 operator stop 的较弱收据。现码 `cli.py:4101-4122` / `:4124-4331` 区别已存在。旧 pid 未知时现 helper 允许仅凭 teardown 收据通过（`:4240`）；**新编排不得继承此回退**。不明、PID 复用、权限失败都不授权盲杀，也不写 owner；以 birth fence 区分旧代死亡与新进程。
4. **plan-stopped**：旧代确定退出、其它写入者受围栏约束后，重新对完整输入集快照并分类。在线预演六库不是同一时点，其成功不是提交许可证；若新增 unclassified，停在此处且 owner 未改。实际迁移不能直接 apply 在线副本中的 rowid 计划。
5. **commit**：先 credential、后原子替换 settings 的 owner/mode/issuer 同一记录；当前 `write_settings_owner()` `identity.py:122-167` 是直接 write_text，需收成保留其它键的原子写。保留 credential 先、owner 后的单向提交政策（`login.py:934-960`）；后者失败如实报 credential 已写，不谎称全回滚。
6. **join / start**：旧代/旧转发 sidecar 已退出，再用选定 profile 做入网，避免 tsnet state 双开。login 自己调用统一 detached launcher（`cli.py:1532-1724`），不 shell 递归执行 init，不依赖 service/cron。**身份已提交后的自动启动不以 join 成功为前提**：入网失败仍尝试按**该 home 既有网络配置**起新代，**不因 join 失败改变 listen/connect/forwarding 网络装配**，报告 `identity=committed / network(join)=failed / daemon=结果`；总体不得伪报完整登录成功。已有 mesh 的机器不能被 join 失败摘出现网：infra-op 评审读数指出当前生产 daemon 走系统 tailscaled 派生监听，与 tsnet join 无关；源码 `application.py:6883-6917` 与 `tests/test_tsnet_join.py:1160-1200` 的 T11 边界相符。这是保留网络配置，不是回滚身份或自动起旧代；**网络禁用只属于 local-usage**，不由 join 失败触发。`--skip-join` 的既有受管路径需把启动网络配置与 mode 明确呈报，不把显式跳过与尝试后失败混同。
7. **verify**：新代 `ping.epoch` 与旧代不同，owner/mode/issuer 等于目标；启动迁移成功或明确无须重写，而非只看 socket 存在。现构造器迁移早于可服务 socket（`application.py:639-661`），已有计数日志 `:947-950`；S3 把迁移结果加入该代的轻量诊断，缺失为 unknown。恢复 connectors 的 phase 单列，不能把 phase①当全部恢复已完成，也不等无限长全舰恢复才回应 login。

互斥需覆盖其它 login、init、upgrade、直接 `daemon run`，以及启动失败的补偿/最后一次重试，不只包住设置写。现 `daemon-launch.lock`（`cli.py:1551-1565`）仅序列化 launcher；`daemon.lock`（`daemon/ownership.py:24-48`、`cli.py:4047-4061`）管活代状态；`.active_daemon.lock`（`home_guard.py:358-365`）只包心跳事务，**三者都不能直接冒充完整切换互斥**。S3 必须定义统一锁序与新代启动时的授权交接（例如继承受控 FD 而非可自报 env bypass），不能让父持锁等 ready、子等同一把锁形成死锁；过渡期 cron 处理见 §4.4。

#### 失败不是自动倒带

输出固定包含 `failedPhase`、`identityCommitted`、`network.status`、`daemon.state/pid/epoch`、`migration.status`、`nextStep`。阶段状态与附加诊断分开，日志/清理失败也进入有限结果。

- prepare/确认/在线预演失败：旧代不动，身份/credential 不变；已显式同意的 sidecar 安装结果单列，不能把它说成全文件系统零写入。
- stop 未证明退出：旧身份保留，不重启、不自动 kill；报存活/未知的精确代与人工检查指引。
- 停机复核失败：旧身份保留；可提示在围栏下恢复旧代，但不能无条件 finally 起一个旧代。
- settings 写入后失败：不自动改回 old。`apply_plan` 是每库事务，多库中途故障可能已有部分迁移；尤其 `detect_previous_owner` 只看 agents，不能承诺重跑必修复其它库。操作员需查看该次只读备份/各库状态，停止全部写入者后统一恢复**完整**快照或走单独修复单；禁止只改回 settings 就宣布回滚成功。
- 清理/回滚/补偿、后台线程、finally、facade 缺失、最后一次重试、不可达 else 的失败同样必须落可观测结果。未知不得被布尔默认值消掉。

### 2.3 `hyprial init`：无 owner 时引导 login

现 `cli.py:3907-3991` 先 initialize home 再尝试起 daemon，docstring 明写 “owner is not an init concern”。改变的是**引导**，不是把 owner 写入口重新搬回 init：

- 有可解析 owner：维持当前 init 行为，不重新认证、不修改 owner。
- 缺 owner：创建骨架后，TTY 且非 JSON 时询问“现在登录？”；同意则调用同一个 login 协调器，登录成功由它起 daemon，init 不再第二次启动。拒绝/EOF 返回尚未登录及下一步，不伪报 daemon ready。
- 非 TTY 或 `--json`：立即结构化报缺身份，给 `hyprial login`；不发 device 请求、不等 input、不启动 daemon。坏 settings/权限失败不得误判缺身份并发起覆盖。
- 初次直接 login 同样可以调用 **home 骨架 helper**（`home.py:117`），不调用 init 的启动入口。切断原有 init → 缺身份报错 → login → 提示 init 的循环。
- init 的 listen/connect 只作用于本次 launch（`cli.py:1567-1608`）；引导经过 login 也要保留本次意图，local-usage 则由模式门禁止跨机端点。不要据 init 过时 help 承诺持久化 endpoints。

### 2.4 `hyprial login --switch-account --dry-run`：候选身份与迁移预演

**候选**来自正常 Casdoor device/UserInfo（仅在内存，认证可能联网），或与 local-usage 组合时的显式 owner；不另加未授权的 Casdoor `--owner`。**“预演不联网”不是要求；whoami 才不联网。** 预演不下载 sidecar、不入网、不更新 credential/settings、不停起 daemon，不创建生产 DB/目录。

1. 新建 owner-only 临时目录；对真实存在的 SQLite 用只读连接 + `backup()` 到副本，不复制活动 WAL 文件凑快照。记录缺库与读失败，失败不能折算成零改写。只读 SQL 不自动证明原目录零写入：WAL 读者可能需要共享内存文件；S2 要观察原目录全清单（含 -wal/-shm），不能用 `immutable=1` 忽略活动 WAL 来伪造只读保证。无法满足源文件系统只读时拒绝预演并报原因。
2. 六库：`agents.sqlite3`、`adapters.sqlite3`、`inbox.sqlite3`、`lifecycle-operations.sqlite3`、`routines.sqlite3`、`workflows.sqlite3`；两文本：`desired-state.json.v1`、`users.json`。清单直接来自 `build_plan` `owner_migration.py:452-469`，复用同一清单，不平行维护扫描器。
3. 在副本调用 `detect_previous_owner`/`build_plan`；当前 settings old 与数据 old 不同必须展示，两者不能互相覆盖；多旧 owner 分类失败不得任取一个。无 agents 但其它库有活地址，需显式 old 进入分类，不能把 `detect_previous_owner=None` 当作所有库已迁完。
4. 输出目标 tuple、settings/data 旧身份、快照起止时间、库/文件存在性、将改写格数（不是唯一行数）、文件变更数、unclassified 的库/表/列/有限上下文、明确登记的历史跳过列（`ARCHIVAL_COLUMNS` `:229-232`）、无法测量项及总扫描分母。输出路径映回源逻辑名，不泄露临时目录里的 inbox 正文。上下文先脱敏再限长。
5. `build_plan` 的 `MigrationPlan` 仅有 writes/unclassified/files（`:195-204`），**统计/历史跳过/扫描失败摘要是需补的返回投影**，不能宣称现有 API 已提供。`plan_database` `:321-323` 有容错 UTF-8 解码，预演不能因此宣称原始数据完整无损。
6. 副本从不 apply。成功、异常、Ctrl-C 都关闭连接并删除临时副本；删除失败报残留目录/原因，不打印敏感内容。崩溃后的残留需显式可发现、可人工清理，不能承诺 secure erase。后续实现不得在全局日志保存完整副本或片段。

默认切换复用此分类服务但要经过 §2.2 的停写复核。old==new 的 mode 切换仍做模式/运行检查，迁移内容可为 0；不能直接把 `build_plan(old==new)` 的残留规则当作跨模式拒绝依据（现 `migrate_owner` `:577-578` 对同名直接 0）。

### 2.5 `hyprial login --local-usage --owner=<name>`：模式隔离，不是冒名开关

- 无需 Casdoor、profile 在线可达、sidecar 或 Headscale；只提交本机 owner/mode/issuer 元数据，不写新 token、不启动入网进程。
- 从联网模式切过来，即使同名也要求 `--switch-account`，走同样预演/停机/迁移/新代核验；旧代、转发 sidecar 确定退出后才开始本机模式。旧 credentials/tsnet node state 可以留作非活动私密材料，**留存不等于允许 resume**，不自动删除远端注册节点。
- 新代可运行本机 actor/IPC；**不 announce，不发起、不接受跨机连接**。不能只设 `HYPRIAL_PEER_DISCOVERY=0`：`application.py:6960` 起的 `_discover_peer_endpoints` 在检查该变量**之前**已处理 forwarding（且 2026-09-15 起默认并存：转发与系统发现取并集）；显式 `HYPRIAL_ZENOH_LISTEN/CONNECT` 也可绕开 discovery。模式必须在网络装配前截断 forwarding、直接端点、动态发现、网络发布/接收；后续恢复/重试同样受门禁。仅 loopback 不够：sidecar 的入站也恰好来自 loopback。
- 门应在 daemon 自身生效，不能只在 login 派生的环境生效；直接 `daemon run`、init、重启/upgrade、残留显式 forwarding env 都不能突破。旧 mode 与新 settings 不一致时不得复用旧代。
- `whoami` 与 `ps` owner 邻接显示 `identityMode=local-usage` 和“身份未经认证”。相同名字不等于线上身份相同；UI/JSON 不只在首次登录时提醒一次。

**D3 表示层已定：沿用现有 owner 语法，支持邮箱，不另建 codec。**

按 Allen 2026-09-14 补充裁定，本地 owner 沿用 `identity.py:66-68,146-152` 及 `uri.py:15-38` 的规则：规范化后非空、不含 `:`；含 `@` 与 `.` 的邮箱形 owner（如 `alice@example.org`）合法。writer 与 reader 一致拒绝含冒号的 owner，不只在 login 放过、留到 daemon 启动才失败。此约束是现有 URI 的**传输语法**，不是命名政策；不引入 local 前缀，不禁止与线上重名，防混淆仍靠模式和不入网。

现有 `src/hyprial/transport/keys.py:8-31` 已将非安全字符用 URL-safe base64 编入 Zenoh key，并能解回；无需为邮箱另定 URI 转义。`owner_migration.py:267-279` 仍按既有地址锚点改写。infra-op 本轮交接已测 `user:<email>` 边界；基线的 `ADDRESS_PREFIXES`（`:82`）不含 `user:`，不能把邮箱被语法接受误读成授权改写 user 目标。S5 验收覆盖 settings→resolver→agent/adapter URI→Zenoh key→迁移与显示的邮箱正例，以及含冒号在 writer/reader 一致拒绝的负例；迁移保留既有 user 目标、host-login 与历史文本边界。这里只允许邮箱形字符串，不验证邮箱归属，也不改变 Casdoor 从 provider username 取 owner 的规则。

## 3. 信任边界与延后实施

### 3.1 接收方按 tailnet 身份校验 owner（D4）

**身份核验链必须从接收方拥有的连接开始**：

```text
接收的连接/代际 → 接收方对应 tailnet 的 LocalClient.WhoIs(remoteAddr)
 → 控制面节点 + UserProfile → 该网络唯一 provider 的 username
 → 比较消息 owner 与控制面 user、machine 与控制面节点名
 → 通过后才持久化/投递/生成成功收据
```

- 不读消息自报 IP 去 whois；不信 sender 写的 tailnet user、`origin_node`、source_info、attachment 或签名外观字段。前提是接收方及其本地 sidecar/IPC 可信，不能防接收方机器管理员改自己的程序。
- `WhoIs` 必须对正确的 profile/tsnet instance 查询，不能借系统 tailscaled 的另一张网。owner 比较用 provider 确定的用户名，不取 email、不截 `@` 猜名。机器名归一化限控制面明确给出的形式（例如 DNSName 末尾点），不能接受发送者自报 short-name alias。
- D4 排在自建 Headscale 的 user = Casdoor username 对齐之后；官方 Tailscale user 与 owner 不同不能靠字符串修补宣称相等。tagged 节点无可确认人类 user 时，不拿 tag 当 owner；preauthkey 本身不必然等于 tagged，须按实际控制面归属分类。无人值守节点的 provider 归属策略未明确前拒绝冒用人类 owner，单独报缺前置。
- 失败顺序：身份缺失/不匹配/WhoIs 超时或失败/节点映射过期 → 有界拒绝且诊断 → **无 inbox commit、无接受收据、无 presence 污染**；不降级为旧“可信 tailnet 所以相信 payload”的路径。

#### 现码可走到哪、断在哪里

1. `src/hyprial/transport/zenoh.py:196-203` `_sample` 仅保留 key/payload/kind；`:305-313` queryable 只传 selector。`src/hyprial/inbox/zenoh_link.py:23-68,99-103` 的 sender/origin_node 是 payload 或本机 stamp，`:239-257` 直接调用 receive/accept_custody/notice/progress，无连接身份参数。这是新验证门的落点，但**不是现成可信输入**。
2. pinned zenoh Python **能观察连接表，不能据此给每一条 Sample 绑定不可伪造的入站连接**。§4.2 已用真实两 session 验证 source_info 可伪造；不能把 `source_info.source_id.zid` join 到 links 当成授权。
3. 转发 v2 有 **出站映射**，不等于入站认证上下文。入站 `forward.go:216-243` 从 overlay Accept 后 dial 本地 listener，当前未输出 remoteAddr/本地 socket 四元组/代际/WhoIs。复用端口时旧映射必须失效；只打印 port→peer 不够。
4. Zenoh 可路由/relay、mailbox 可代保管：`zenoh_link.py:130-138` 发送 custody 时仍保留原 sender；`:247-251` 接收 custody。最后一跳身份不必等于原作者。**不得为了让直连测试绿，就声称最后一跳证明原始 sender**；也不得把现有 mailbox 身份当作任意 owner 的委托凭证。

#### D4 前置切片（M0，先证通路，不写伪验证器）

推荐先把可信上下文带到接收函数之前：接收端 sidecar/gateway 在真实 socket 上取得 tailnet 用户，产生仅接收端可构造、带连接代际的上下文，应用以该上下文处理 frame；Zenoh 只留在受控本地一侧，或者另行验证能在 transport/router 层拦截每个 frame 的绑定。**这是待验证的接线方案，不是已证明当前 Zenoh API 可以完成。**

不能在共享多对端 Zenoh session 的 Python callback 里拿一张 links 表补一行 if；缺的是绑定，不是缺一次字符串比较。M0 必须回答逐消息归属、多跳原作者与最后一跳、presence/query/receipt/custody 等所有入口的认证语义。若需要认证代理重构或原作者可验证证明，先退 infra-op 细化规格，不自改 D4 成“只验部分消息”。在 M0 未验收前不得开启“已认证 owner”产品标志，不把有 sidecar 映射当成 D4 已可交付。

## 4. 四个待核读数（事实、限制、下一步分开）

### 4.1 `service` 无子命令是不是 GUI 未挂载连带？——否，已核当前 0.4.5 源码/隔离命令树

- `cli.py:3994-4023` 用 `@app.command()` 注册 service，仅 probe 后返回 standalone/install=false/running；不是 Typer 子组。GUI 在 `:8759-8804` 另行动态挂载。
- 固定树版本 `pyproject.toml:3` 为 0.4.5。临时 home、无 GUI 的真实 Typer 命令树读数：`service_type=TyperCommand`、`service_subcommands=None`；`daemon_subcommands=['run','stop']`；`whoami_mounted=False`。
- 因此不能设计为 `hyprial service stop/start`；统一调用已存在的 daemon launcher/严格 stop primitive。**没有复现所有历史安装包**，但该 0.4.5 树上独立注册足以否定“此处缺子命令由 GUI 未挂载导致”。

### 4.2 zenoh 向应用层暴露连接来源吗？——连接表有，逐消息可信绑定未有

- 依赖 pin：`pyproject.toml:9` = `eclipse-zenoh==1.9.0`；实装反射同版。
- 官方随包 `zenoh/__init__.pyi:1636-1778`：`session.info.links()` 有 src/dst/zid/auth_identifier；`:1954-1969` 的 SourceInfo 是 entity id/sequence，而 Session.put 可显式传 source_info（`:1442`）。这些是依赖包行号，不冒充本仓行号。
- 临时 home、`HYPRIAL_PEER_DISCOVERY=0`，只建立两个显式 loopback Zenoh session：receiver 的 links 确实列出 sender zid/端点，普通 put 的 `source_info=None`；sender 用 **receiver 的 publisher.id** 构造 SourceInfo 后发给 receiver，接收值原样保留，`forged_source_accepted=True`。接收 Sample 公共属性共 10 项：attachment/congestion_control/encoding/express/key_expr/kind/payload/priority/source_info/timestamp，未含连接句柄。
- 这是真实协议负控，不是自造 Sample 桩；结论是 **不能信 source_info**，不是“Zenoh 完全没有连接 API”。首两次测量分别因误把 `session.info` 属性当函数、EntityGlobalId 对象 `==` 不按字段比较而失败，已分别修正为 `.info.links()`、比较 zid/eid；不把仪器错误当产品红。
- 尚未核实：上游 router/插件是否能提供满足逐 frame 来源归属的可用接口，以及多跳绑定方案。已有相关读数如上；要知道还差 M0 的真实多对端/路由接入实验，不需要先碰生产网。

### 4.3 sidecar 能报告「映射端口 ↔ 对端节点」吗？——已有出站 IP 映射，未有入站身份映射

- `sidecar/hyprial-tsnet/internal/forward/forward.go:35-38,110-145,174-183`：Mapping{Peer,LocalPort}，peer 按 IP 解析，本地 `127.0.0.1:0` 分配端口，Mappings 可枚举。
- `internal/proto/message.go:39-42,60-65,95-96`：wire `peer-mapped` 带 peer/localPort，status 带 mappings；`internal/node/manager.go:338-355` 填充；`src/hyprial/daemon/forwarding.py:226-241` 读取。
- `forward.go:216-243` **入站**只 Accept→dial→splice；无入站身份事件。因此已有报告的“对端节点”目前是目标 IP，不是认证后的 Node/UserProfile，也不是每条入站 frame 的来源。
- Go pin `sidecar/hyprial-tsnet/go.mod:5` = `tailscale.com v1.102.3`；该依赖 `client/local/local.go:323-340` 存在 `(*Client).WhoIs(ctx,remoteAddr)`，`client/tailscale/apitype/apitype.go:35-42` 返回 Node/UserProfile。现本仓 backend `internal/node/backend.go:20-31,62-76` 已有 local client，但 Backend/DataBackend 未暴露 WhoIs。
- **可加接口的源码证据不等于端到端已验**。未能核实真实接收 sidecar 的 WhoIs→节点用户→消息绑定：本轮未启动 tailnet 节点；下一步 M0 用批准的测试节点/隔离接线验证 RemoteAddr 与本地转发四元组/代际，不查询 payload 中的地址。

### 4.4 cron 过渡期最小互斥——终态删除，不建长期看门狗协议

- **部署读数（infra-op 2026-09-13 12:40 装入、同日读回，出处 PR #487 评审 #11936，22:17:23 +0800）**：crontab **7 行，活跃 6 行，指向旧家 0 行**；`~/.hyprial/bin/daemon-watchdog.sh` 每 **5 分钟**执行，`hyprial ps --json` 判 DOWN 且 `pgrep -f` 未找到 `uv/tools/hyprial/bin/python -m hyprial.cli daemon run` 进程时，使用 nohup 拉起。它**不读任何 marker 文件**。`hq/tools/h2b-tick-guard.sh` 那行已被 `#CUTOVER-2026-09-13` 前缀静默。本轮采纳带时刻的评审实测，不冒充作者重新执行生产读取；该快照不证明未来部署窗口无在飞实例。
- 当前仓内 §2.2 三把锁及严格停机原语也不提供“外部 cron 已停止调度”的事实。hq 固定 SHA 的 `notes/runbook-cutover-2026-09-12.md:94-108` 是旧部署记录，以上 09-13 读数取代其当前部署口径。
- 最小过渡方案是**在 S3 生产启用前，由节点操作者摘除该 watchdog 启动项并排空已在飞的 watcher，然后让 login 成为拉起者**。若删除交付尚未到，受管维护窗口临时暂停该特定任务及其自动恢复源；不由 login 猜机器排程、清空整个 crontab，更不每次登录自动装回 watchdog。
- 只创建一个 `switch-in-progress` 文件并无效：上述实测脚本不读取它；只暂停 cron 也不证明已经启动的脚本退出。必须分别核 **未来无调度** 与 **当前无在飞拉起者**，再进入 login 事务围栏；读不到任何一格就不进入停机/提交。
- S3 过渡验收：在临时 home 用上述部署脚本的只读快照（记录 hash，不自造 watcher 形态）重放 DOWN/无进程分支，证明仅放 marker 仍会尝试 spawn；暂停后分别断未来调度为 0、在飞 launcher 已退出，login 才能进 stop/commit。突变为“看到 marker 即判安全”或省略在飞排空时，对应负例必须红；不在生产触发 DOWN。
- 若必须并存直到终态删除，先单独交付 watcher 与 login **同一 OS 锁下的 probe→spawn 临界区**，并用受控新代交接避免自己死锁；不能用“检查 marker 后再 spawn”的 TOCTOU 方案。此比先暂停再删除成本高，不选作默认。最终删除临时 watcher 接线/部署项，而不是删 daemon 自己的所有 shutdown/内部健康观测。
- 下一步：操作者在部署窗口只读列本节点所有 cron/launchd/systemd/托管启动者与活实例，保存准确条目与时间，再有授权地摘除/验证；本设计单不执行。临时暂停只豁免过渡期立即删除部署文件，**不豁免** 无竞态、可观测停起、最终删除及其它 home 不受影响。

## 5. 切片、依赖与可证伪验收

所有新用例名为**规划名**；现有文件是可复用的测试锚点，不声称新用例已存在。外部形态来自 pinned 真实接口/仓内已有 fixture，不手写另一套 OIDC/sidecar/Zenoh 响应。每片至少以下正例、负例及变异；实现者记录 collected、预测失败集、真实失败集，并把**同一次突变**恢复后全文件重跑，不用 `-k` 缩小红日志。

| 切片 / 依赖 | 改动锚点 | 正例与负例（必须断事后状态） | 变异必须击中的判据 |
| --- | --- | --- | --- |
| **S1 whoami**，可先行 | §2.1；现 `tests/test_identity_guidance.py`、`test_home_guard.py` | temp home 缺身份仍能只读输出；env/settings/daemon 三值不同告警；坏 JSON、权限失败、过期心跳+活 pid、ping 超时是 unknown/error，不是 absent/stopped；secret 扫描；目录/文件前后清单与字节不变 | 将 daemon owner 替换为 settings 值；把 timeout 改 stopped；增加 mkdir/refresh 任一项，应分别红 |
| **S2 候选/预演**，可与 S1 并行 | `login.py:850-973` 拆提交；`owner_migration.py:452-469`；现 `test_login.py`、`test_owner_migration.py`、`test_login_u5.py` | 六库两文件真实 schema 由现有 stores/fixture 生成，WAL 未 checkpoint 的活 owner 也进副本；历史列保留；缺库与损坏库区分；预演 target 来自真 OIDC fixture；settings/credential/原库/daemon 代际不变 | 漏任一库/文本、copyfile 代替 backup、调用 apply_plan、跳过历史报告或把读错当 0 均有专门反例 |
| **S2-cleanup**，随 S2 | §2.4 | 成功/异常/中断副本清除；unlink 报错时输出残留而不泄露内容；首个 unclassified→拒绝且原件没变 | 吞 finally 清理失败、把原 DB 传给 apply、打印 token-bearing 上下文必须红 |
| **S3 协调器**，依赖 S2；部署依赖 §4.4 | `cli.py:1532,4124,4047`；`application.py:659,3300`；现 `test_login_u5.py`、`test_cli_init.py` | 首次 login 自动起；同身份活代不重启；switch 改 tuple 后新 epoch/owner；已有 mesh 场景注入 join 失败后仍尝试起新代，identity=committed / network(join)=failed / daemon=结果；新代 listen/connect/forwarding 与切换前一致（联网非零正控），不以远程计数为 0 验收；无 flag/拒绝/override 冲突零身份写；`--no-daemon` 活代换身份拒绝 | 恢复 DAEMON_RUNNING 默认拒绝、删自动 launch、join 失败跳过启动或清空既有网络装配、同名跨 mode 提前 return 各应红 |
| **S3-fence**，随 S3 | §2.2 stop/复核/互斥 | 实际隔离旧进程 socket 消失仍存活不提交；unknown birth 不放行；PID reuse 不误杀；在线预演后插 unclassified 必须在停写复核截住；两个 login、init/upgrade/direct run 竞争只一代提交；不得跨 home 停人 | `require_exit=False`、old_pid=None 收据放行、去掉第二次 plan、取消锁；按各全测试文件报告真实失败范围 |
| **S3-failure**，随 S3 | §2.2 失败表 | credential 写/owner 写/迁移中途/新代启动/后验/日志提取/补偿各注入失败，阶段与实际文件/活进程状态一致；重复执行不声称自动补齐跨库；JSON 非成功可机器判读 | 将 failedPhase 统一 success、finally 无条件起旧代、只改回 settings 充当 rollback，必须红 |
| **S4 init 引导**，依赖 S3 | `cli.py:3907-3991`、`home.py:117`；现 `test_cli_init.py`、`test_login_cli.py` | TTY yes → login 且只起一次；no/EOF 保留骨架无 daemon；非 TTY/JSON 立即结构化缺身份；坏 settings 不覆盖；已有 owner 不走 OIDC；listen/connect 本次 launch 仍生效 | 去 TTY/JSON 门、递归调 init、第二次 spawn、将 parse error 当缺身份均红 |
| **S5 本机模式**，依赖 S1/S3；§2.5 表示层已裁 | `identity.py:66,122`、`uri.py:15-38`、`forwarding_config.py:46`、`application.py:826,6949,8333`；现 `test_uri_leaf.py`、`test_forwarding_config.py` | 不联系任何基础设施可建本地身份；与线上重名合法且显示未认证；同名切回 Casdoor 仍执行 mode 切换；含 @ 与 . 的邮箱形 owner 全链路正例（settings/resolver/agent 与 adapter URI/Zenoh key/迁移/显示）；含冒号被 writer 与 reader 一致拒绝的负例；迁移不改 user 目标、host-login/历史文本 | 恢复 local 前缀命名限制、拒绝邮箱、writer 或 reader 单边放过冒号、同名跳过 mode 切换均红 |
| **S5-network**，随 S5 | §2.5 所有启动面 | 注入有效 forwarding env、显式非 loopback endpoints、旧 tsnet state、发现命令；分别从 login/init/direct run/重启/恢复启动，本地 IPC 可用而远程监听/拨号/announce/收发计数为 0；要有联网模式非零正控 | 仅关 discovery、让 forwarding 分支先跑、把 loopback 一律当可信，各必须被真实连接负控击中 |
| **M0/D4 通路预研**，在自建 Headscale mesh 后，独立安全规格 | §3.1 / §4.2-4.3；现 `test_sender_identity_gate.py` 不是远端连接认证证明 | 真实两节点正常 owner/machine 通过；伪造 owner/machine、source_info、payload IP、失效映射、端口复用、未知/tagged user、WhoIs timeout 均拒绝且无 commit/receipt；双 peer 并发与 relay/custody/query/presence 逐入口核绑定 | 删 gate、信 source_info、使用 sender 自报地址、信 outbound port 表作入站来源，不能通过对应负控；绑定不可得则 M0 明确失败而非 mock 绿 |

每片还要跑状态邻域与仓级守卫：新增事件名→启动事件白名单（`cli.py:1385` 起），新增 import→CLI/daemon 导入边界，身份字符串→`tests/test_uri_construction_guard.py`，错误码→IPC 注册表守卫；CLI 变更更新真实 help inventory，不造 hidden alias。全仓 `ruff check .` 与 CI 同尺全量 `pytest tests/ -q`，输出直接落盘；setup/collection 红先看共用装配，不改无关代码夹带进功能片。

D1–D3 及 D3 表示层均已裁，S5 不再有名字 codec 前置；按 S1/S3 依赖切片。S3 仍须兑现统一互斥接线与实施验收。M0 的未验证只阻塞 D4 功能上线，不阻塞 S1–S4；它不豁免将来兑现 D4，也不授权沿用伪认证。

## 6. 发布、回退与邻近事件检查

1. 本设计 PR base=`intg/login-identity`，不推 dev/intg；只改设计文档，infra-op 决定何时转非 draft/合并。各实现片进同一集成分支。
2. 发布前先验证模式隔离与 S3 竞争；cron 终态删除由部署单兑现。功能切换演练用临时 home/state，生产 hq owner 切换不在本稿授权内，也不拿本稿代替 #481 后那次实际预演。
3. 两个相邻但不同的事件：**名字不变模式变了**（必须覆盖，不是幂等重登）；**socket 消失但进程未退出**（必须覆盖，不是已停）。两者都已加入 S3/S5。
4. **在线快照后数据库继续写**（必须补停写复核）；**port 相同但映射代际变了**（必须作废旧认证上下文）；**mailbox 代发保持原 sender**（M0 必须解决，不有意留白）。
5. 有意不覆盖：Casdoor 组织/注册策略改造、控制面 provider 迁移部署、生产 cron 修改、消息 ACL/成员表、外部服务账户体系；不覆盖不等于宣称安全已实现。模式为 local-usage 只禁止 Hyprial 跨节点身份网络；不在这里禁止用户自己的模型 API 或其它应用联网。
6. 安全边界设计需要第二读者的不同默认假设：infra-op 审 §2.2/§2.5/§3.1 后再切片。签核记 head 与被审文档 blob；base 前进重算落地树，文本变化重审相应片。不能把 draft 的 mergeable 布尔当算过的合并读数。

## 7. 本轮验证状态与实施债

本单没有改 `src/`、`tests/`，§5 新断言/变异是后续实现验收，**本轮未实现、未执行这些新功能的红→绿**。不得拿当前旧行为测试绿来宣布新命令已可用。

本轮独立实测包括 §4.1 Typer 树与 §4.2 真 Zenoh 伪来源负控；不是功能实现测试，也没使用生产登录。代码/测试/CI 与固定基线逐文件无增量，是旧行为回归的比较对象；全量原始日志和工具命令/终态另随 PR 回执列出，不能把未结束/收集失败记作全量绿。

实施仍欠：S1–S5 全部代码/新测试与相同突变验证（S5 owner 表示层已裁，不再欠 codec 规格）；S3 所有启动者统一围栏/交接与部署删除 watchdog；M0 逐消息可信绑定与 relay/tagged 节点规格。未知项已有部分读数与补测动作均在 §4，不以“尚无读数”抹掉已知信息。

### 附：§4.2 可独立复跑的负控

2026-09-13 21:46 +0800，在 pinned `eclipse-zenoh==1.9.0`、临时 `HYPRIAL_HOME` + `HARNESS_STATE_DIR` + `HYPRIAL_PEER_DISCOVERY=0` 下运行以下 Python（不运行 daemon/login，不连接真实 tailnet）。

```python
import json
import queue
import socket
import time

import zenoh


def config(*, listen=(), connect=()):
    return zenoh.Config.from_json5(json.dumps({
        "mode": "peer",
        "listen": {"endpoints": list(listen)},
        "connect": {"endpoints": list(connect)},
        "scouting": {
            "multicast": {"enabled": False},
            "gossip": {"enabled": False},
        },
    }))


with socket.socket() as sock:
    sock.bind(("127.0.0.1", 0))
    endpoint = f"tcp/127.0.0.1:{sock.getsockname()[1]}"
received = queue.Queue()
with zenoh.open(config(listen=(endpoint,))) as receiver:
    with receiver.declare_subscriber("loginid/probe", received.put):
        with receiver.declare_publisher("loginid/unused") as own_publisher:
            with zenoh.open(config(connect=(endpoint,))) as sender:
                time.sleep(0.4)
                print("receiver_links", receiver.info.links())
                sender.put("loginid/probe", b"plain")
                print("plain_source_info", received.get(timeout=3).source_info)
                forged = zenoh.SourceInfo(own_publisher.id, 42)
                sender.put("loginid/probe", b"forged", source_info=forged)
                sample = received.get(timeout=3)
                actual, expected = sample.source_info.source_id, own_publisher.id
                assert str(sender.info.zid()) != str(expected.zid)
                same = str(actual.zid) == str(expected.zid) and actual.eid == expected.eid
                print("forged_source_accepted", same)
                assert same
```

实测核心输出：

```text
receiver_links [Link { zid: f4b6b1ac8eed3467320331fb2bf27f4b,
  src: tcp/127.0.0.1:47050, dst: tcp/127.0.0.1:47051, ..., auth_identifier: None, ... }]
plain_source_info None
claimed_source_id EntityGlobalId { zid: acb19dd6331f7b39c25df7bccc4b8a55, eid: 7 }
receiver_entity_id EntityGlobalId { zid: acb19dd6331f7b39c25df7bccc4b8a55, eid: 7 }
forged_source_accepted True
exit=0
```

Link 行省略纯传输参数，不省略判据字段；随机 zid/端口不是预期常量，判据是实际发送连接的 zid 与伪造 source entity 所属 zid 不同、伪造 entity 的 zid/eid 原样到达。这个成功的反例证明来源字段可伪造，**不证明 D4 已实现，也不证明所有 Zenoh 接入方案不可行**。
