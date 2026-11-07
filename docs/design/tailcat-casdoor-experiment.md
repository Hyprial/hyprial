# Tailcat + Casdoor 实验、控制面职责与 Hyprial 迁移计划

日期：2026-10-02。负责人：k3s-maintainer。

**最新补充：第 16 节实测多客户端 Status 和按需 Web/SSH，最终一个集成测试22条断言通过、另2条观测；真实PAC网页和agent迁移仍未执行。**

**第二轮结果（18:50–18:57 CST）：已用真实 Casdoor API 与真实本地 Tailcat 串联跑完两个逻辑组织从创建到解散的周期，见第 12 节。** 过程中修正三处实验适配问题，保留失败及续跑记录；不是一次不间断全绿。第 8 节保留第一轮独立模型实验与当时的历史证据。

本文件由 k3s-config 实验记录迁入 harness-bridge `docs/design`（证据材料按 docs 五桶归入 `docs/evidence/tailcat-casdoor-2026-10-02/`），讨论用户场景与能力归属，实验部分不接入 CLI/daemon；第 13 节核查现有依赖，第 14 节按 Allen 后续要求规划硬切 tsnet 和消除 Hyprial 的 Headscale 依赖。Allen 已明确批准开发冻结期间进行此项实验、文档写入和文档 PR；不包含产品实现或发布授权。

## 1. 范围与问题

本次仅验证：在不使用 Headscale、tsnet 或按组织创建 K3s 实例的情况下，是否能把 Casdoor 的用户身份与组织关系，配合 Tailcat 的加密连接，组成“按组织授权的 daemon 通信”基础。

第 1–12 节记录独立实验；后续迁移规划见第 13–14 节。实验中的 `hyprial signup/login`、`hyprial org ...` 仅作为用户场景名称；实际运行实验脚本，不新增、调用或承诺产品中已实现这些命令。现有 Hyprial daemon、生产身份服务、Headscale 和集群不变。开发/发布冻结仅对本次明确授权的隔离实验和文档有例外。

**第一轮结论（后续真实周期见第 12 节）：Tailcat 连接与实验组织授权的组合具备可行性，但该轮不是全部通过。** 本地实验执行了 33 项断言，32 PASS / 1 FAIL；唯一失败是撤权后 `Status()` 仍保留 peer。**Casdoor 已有真实自助注册使用记录、默认入组配置变更与回读，但没有本需求的受控多组实验。** infra-ops 已提供详细读数：邮箱验证码注册未实际使用，具体登录方式未在该报告中核实，API 增删成员且保留其他组为 NOT_RUN。原生能力可以复用；“有能力”“已配置”“真实使用”“受控验证”不能混为一谈。

四类场景足够定义最小业务闭环，还需补齐多组织、权限拒绝、邀请生命周期、设备绑定、撤权及结果查询；不应只验证成功路径。

## 2. 这四类场景是否完善

四类场景覆盖主流程，但不足以判断控制面完整性。最少还需覆盖“权限边界、邀请生命周期、多组织与撤权效果”。

| 场景 | 本实验语义 | 必须观察的结果 |
|---|---|---|
| signup | 创建一个新的全局身份 | 真实注册成功；不因注册自动授予任意实验组织权限 |
| login | 认证已有身份 | 正确身份可登录，错误凭据被拒；身份键稳定 |
| org create | 创建逻辑组织并赋予创建者管理员资格 | 组织与成员记录产生；不创建 Headscale/K3s 实例 |
| org invite | 组织管理员签发一次性邀请 | 普通成员无权签发；邀请不提前授予成员资格 |
| org join | 已认证用户接受目标组织邀请 | 新账号和已有账号均可加入；保留其他组织 |
| org deactivate | 管理员移除/禁用目标组织内成员资格 | 仅撤销目标组织权限；不全局封禁 Casdoor 用户 |
| org leave | 用户主动退出目标组织 | 移除本人目标组资格；其他组织和账号仍有效 |

`deactivate` 不是账号禁用，也不是设备删除。实验采用：重新加入需要管理员再次授权，已消费或撤销的旧邀请不能恢复资格。是否需要单独的 reactivate 操作留作产品语义选择。

最后管理员规则：本实验先采用拒绝会留下无管理员组织的 leave/deactivate；管理员转移未实现；组织删除已在第二轮补做实验编排，见第 12 节。规则是实验假设，不把它当已定产品接口。

## 3. 最小参与者与拓扑

- 三个测试身份 A、B、C；两个组织 alpha、beta。
- 至少一个身份同时属于两个组织，用于检查“同一设备可连接”与“可以访问哪个组织的资源”不是同一件事。
- Casdoor：仅现有隔离 casdoor-test 实例 `http://127.0.0.1:18100`，验证码邮件只允许进入本机 Mailpit。
- Tailcat：独立测试端点和临时本地 DERP，不运行真实 hyprial daemon，不连接生产 tailnet。
- 实验控制逻辑：仅保存测试身份、角色、邀请、设备公钥与授权；不接入 Hyprial 现有代码。
- 角色表以各实验结果中的精确表为准；两个独立实验不能仅凭同名 A/B/C 冒充串联 E2E。

同主机独立 Tailcat 引擎可以验证真实加密通道和接口行为，不能证明跨主机、跨 NAT、公网可用性或生产规模性能。

## 4. 扩充后的验收矩阵

| ID | 用例 | 成功条件 |
|---|---|---|
| U01 | 新用户注册 | 经真实 Casdoor 注册路径，不用管理员 add-user 替代 |
| U02 | 登录与错误凭据 | 正确身份成功，错误凭据拒绝 |
| O01 | 创建组织 | 创建者拥有该组管理员资格，无全局提权 |
| O02 | 普通成员发邀请/开除他人 | 明确拒绝，无成员状态变化 |
| I01 | 新用户接受邀请 | 先注册/登录后加入明确目标组织 |
| I02 | 已有用户加入第二组 | 保留原组，新增目标组 |
| I03 | 邀请过期/撤销/篡改 | 不加组，不产生设备访问权限 |
| I04 | 重复或并发接受 | 同一邀请仅一个主体获得资格；合法同主体重试幂等 |
| I05 | 领取后上游写入失败 | 不报成功；可恢复，不能给另一主体重复领取 |
| N01 | 同组通信 | 真实 Tailcat 通道传递业务字节，有正向回读 |
| N02 | 跨组连接/资源访问 | 拒绝；不能靠目标不可用或普通连接失败伪造隔离 |
| N03 | 双组用户 | 共享 alpha 的人不能访问对方 beta 资源 |
| R01 | deactivate | 目标组访问停止，账号与其他组仍有效 |
| R02 | leave | 本人目标组访问停止，其他组不受影响 |
| R03 | 已有连接与重连 | 既有会话处理可观测，旧公钥重连不能恢复已撤权权限 |
| R04 | 重启后的撤权 | 恢复状态后旧授权仍拒绝，正常成员仍可连接 |
| R05 | 旧邀请重放 | 离组/开除后旧链接不能重新授予资格 |
| A01 | 最后管理员 | 按实验规则拒绝留下无管理员组织 |

实际结果必须逐条标 REAL、LAB/MOCK、FAIL、BLOCKED 或 NOT_RUN，并说明失败发生在认证、成员资格、连接授权还是业务资源鉴权。

## 5. 控制面必须覆盖的职责（不等于全部自研）

以下是职责与可观察结果，不涉及 Hyprial 代码布局、IPC 或集成方式。身份、用户/组、角色/权限和身份侧审计应优先复用 Casdoor；第 10 节逐项核对原生覆盖范围，不能把本表理解为重新实现一个 IAM。

| 能力 | 输入/依据 | 控制面必须保证 |
|---|---|---|
| 身份验证 | Casdoor 签发的身份材料 | 校验签发者、受众、有效期和主体；不能只信客户端用户名 |
| 组织和角色 | 已认证主体、Group | 建组权限、创建者角色、成员/管理员边界、组织状态 |
| 一次性邀请 | 管理员、目标组织、有效期 | 不提前加组；消费原子性、主体绑定、撤销、重放防护 |
| 成员加入与恢复 | 邀请、认证主体、Casdoor 回读 | 加目标组不覆盖其他组；上游失败不误报成功 |
| 设备身份绑定 | 用户授权、设备公钥所有权证明 | 公钥确属该设备/账号；不能提交别人的公钥冒认 |
| 组织节点目录 | 已授权组织、在线节点 | 只提供获授权的节点/服务信息；复用 Tailcat 的公钥与穿透，不另造对端地址探测协议 |
| 连接授权 | 设备身份和当前有效成员关系 | 默认拒绝；成员表的变化最终落实为连接准入变化 |
| 组织内资源权限 | 当前请求的明确组织范围 | 一条允许的隧道不自动授权另一组织的资源 |
| 撤权与会话生命周期 | leave/deactivate/设备失效 | 先阻止新授权，再回收已有组会话；保留无关组织访问 |
| 离线/失联处理 | 授权期限、版本和同步状态 | 定义旧权限最长可用窗口；不能承诺离线端瞬时撤权 |
| 可恢复状态与审计 | 操作ID、主体、组织、结果 | 可追踪部分失败与重试；日志不保存秘密邀请或地址 |
| 连接观测 | 端点状态、直连/DERP、拒绝原因 | 能区别身份失败、无权限、连接故障与中继不可用 |

不依赖“给每组部署一台网络控制器”实现这些职责，但身份/成员/授权状态仍需要可靠保存和恢复。Tailcat 只提供连接原语，不原生提供组织模型。

## 6. 需要特别验证的两类撤权

1. **该设备已无任何共同授权组**：拒绝新连接并断开该客户端。Tailcat 的断开可能表现为已有连接停滞至超时，不能把它描述为保证立即向应用返回错误。
2. **双方仍共享另一个组**：不应简单断开整个设备并永久阻止重连；需终止已撤销组织的业务会话，同时继续保护其他组的合法访问。仅公钥允许表不足以表达这一点。

拒绝行为必须同时有未撤权成员的正例、服务端授权状态与实际请求结果。单独的 timeout 不能证明权限隔离。

## 7. 上游版本与依据

- 实验候选固定为 Tailcat commit `b4dc28e8aa8936f0a90a41ad8293a64e3d6b645f`，2026-10-02 `git ls-remote` 核对；这是开发提交，不宣称 v0.7.0 已有同样 API。
- 该提交的精确模块依赖、工具链与实际可用接口以实验 manifest 为准；失败不得静默换版本后沿用原结论。
- 官方接口变更仍将 `AllowClient`/`DisconnectClient` 列在 Unreleased；准入校验不等于对既有连接持续重新鉴权。
- 官方说明：[Tailcat](https://tailscale.com/tailcat)、[Go 库和密钥说明](https://github.com/tailscale/tailcat/blob/v0.7.0/README.md)、[开发变更](https://github.com/tailscale/tailcat/blob/main/CHANGELOG.md)、[威胁模型](https://github.com/tailscale/tailcat/blob/v0.7.0/SECURITY.md)。

## 8. 第一轮实测结果、证据与清理（历史记录）

### 8.1 运行范围与结果

最终诊断运行完成于 **2026-10-02 16:44:31 CST / 08:44:31 UTC**，命令退出码 **1**，总执行 27.715 秒。33 项断言中 32 PASS、1 FAIL；14 项是 MODEL，19 项涉及真实本地传输、诊断、中继或清理。它们是一个有界实验中的断言数，不是 33 条真实用户 E2E。

真实部分使用同一测试进程内的独立 Tailcat 引擎、真实 WireGuard/TCP、loopback DERP/STUN；强制 DERP，未测试直连/跨 NAT。身份是 A/B/C 夹具，组织和邀请是内存模型。A 管理 alpha/beta，B 初始属于 alpha，C 初始属于 beta；B 后续加入 beta，再退出 alpha，最终被 A 移出 beta。

| 用户要求的场景 | 已取得证据 | 尚未证明 |
|---|---|---|
| signup/login | 生产存在 9 个 Application Signup 用户，与 1 个管理员 Add User 区分；本轮 health/discovery 可达 | 不是受控测试；具体登录方式未核实。邮箱验证码注册未实际使用，邮箱登录缺使用证据；casdoor-test 未做注册登录实验 |
| org create | MODEL：A 创建两组并获管理员角色 PASS；生产默认 signupGroup 配置变更及回读有记录 | 只有一个生产 group；没有受控自助建组、API 多组成员增删或组级管理员实验 |
| org invite/join | MODEL：管理员签发、普通成员拒绝、过期、目标篡改、并发唯一领取、消费重放、claim 写入故障重试 PASS；已有 B 加入第二组后，通过真实通道访问 beta 成功 | 浏览器新账号注册后续接邀请、API 多组成员写入/保留其他组、真实新旧用户接受邀请及串联 E2E 均 NOT_RUN |
| org deactivate | MODEL：移除目标组保留账号/另一组 PASS；真实通道下最后共同组撤销后准入回调拒绝旧 key，合法 C 仍通信 PASS | 离线传播、多设备批量撤权、全局账号停用 NOT_RUN |
| org leave | B 离开 alpha 后，同一条真实 TCP 连接的 alpha 请求 DENY、beta 请求成功；旧 alpha 邀请不能重放恢复资格 PASS | 真实 Casdoor 成员同步与持续业务任务终止 NOT_RUN |

### 8.2 直接观察到的行为

- B→A 的 alpha 数据、C→A 的 beta 数据经本地 DERP 传输成功。中继计数：发送 **15,219 bytes**，接收 **16,047 bytes**；计数包含协议流量，不是业务吞吐性能数据。
- B 尚不属于 beta 时，隧道可以连 A，但访问 A 的 beta 资源被实验请求授权逻辑拒绝；伪造请求主体也被 PeerKey 到用户的绑定拒绝。这证明需要业务组织范围校验，不是 Tailcat 原生有 Group。
- 无共同组的 B→C，在同一个目标端点的允许来源已实际发送成功之后，被服务端准入回调拒绝；不以单纯 timeout 判拒绝。
- B 退出 alpha 时仍保留 beta，因此不粗暴断掉整个设备隧道；后续每条消息都重新检查组织权限。**没有实现或验证已经开始执行的长任务即时中断。**
- B 被移出最后一个共同组后，先令准入拒绝，再调用 DisconnectClient，返回 true；再次调用返回 false。之前连接地址的 PeerKey 查询已不存在，旧 key 重新连接产生实际拒绝回调，C 的正向控制仍成功。
- 旧 TCP 连接在本实验设定的 **3 秒读取 deadline** 到期，未返回业务数据。这是应用等待超时的观测，不是“撤权传播耗时 3 秒”的 SLA，也不是立即收到 RST 的证明。
- 新建引擎、恢复内存快照后，C 可正常通信，B 的旧 key 被拒。这里只验证**新引擎＋内存状态恢复**，不证明磁盘持久化、进程崩溃恢复或密钥长期保管。

### 8.3 保留的一项失败与实验局限

`status_peer_absence` **FAIL**：断开后读到 `Status().Peer` 仍有已撤销 key，字段为 `InEngine=true`、`InMagicSock=false`。此前远端的 PeerKey 已不存在，重连已被拒绝，未观察到未授权业务访问成功。这个结果说明状态/引擎记录有残留，**不应直接把 Status 列表当作当前组织权限或可用连接列表**；也没有证明底层全部引擎状态已删除。保留失败断言，未修改上游来制造全绿。

早期运行记录也保留：依赖缓存目录布局错误；沙箱 `netmon.New: operation not permitted`；测试夹具中重复 key 的并行客户端和缺少新端点正向控制。修正的是实验夹具与观测，不是偷偷修改 Tailcat。最终测试固定源码后由 maintainer 运行并回读实际退出码。

多服务器客户端的 key 使用是另一个需明确的前提：本实验给不同并行客户端分配了不同 key 并将其绑定到同一模拟用户；同 key 重连前先关闭旧客户端。**没有证明一个 key 同时驱动多个独立 Tailcat client 引擎的正确性。**

邀请模型只有单进程互斥和内存状态；不是跨进程事务或可靠存储。已消费的同主体重试返回拒绝，没有实现完整的幂等成功响应。设备绑定直接由夹具设置，未测试真实所有权挑战。

### 8.4 Casdoor 事实更正与证据分层（17:2x 的历史读数）

以下记录的是第二轮受控实验之前的状态。随后已在同一天18:50后执行原生用户名/密码注册、多组成员API和完整组织周期，见第12节；不能继续将下面的NOT_RUN视为最新总状态。

此前根据“signup/login 和 Group 写入已经可用”的概括确认，文档将三项宽泛列为既有验证。infra-ops 于 **2026-10-02 17:2x CST** 交付了[详细报告](../evidence/tailcat-casdoor-2026-10-02/experiment/identity-existing-evidence.md)，必须按具体证据收窄：**产品原生能力可复用，不代表我们列出的每个注册方式、Group API 路径和多组流程都已实测。** 没有受控 signup/login 测试报告，也没有本需求的三账号、两组完整实验。

| 事实 | 证据级别和范围 | 不能推导的结论 |
|---|---|---|
| 9 个 `Application Signup` 用户，创建于 09-13 至 09-22；另 1 个 `Add User` 创建于 09-05 | infra-ops 只读生产计数；真实自助注册使用记录，区别于管理员建用户 | 不是受控测试计数，不能推导邮箱验证码路径或所有登录方式已通过 |
| 09-29 19:35 开启邮箱验证码注册/登录相关配置，回读确认 | 配置已生效；09-22 后没有新用户，邮箱验证码**注册**未实际使用 | 无新用户不能证明已有用户从未用邮箱**登录**；该报告未提供逐次登录方式证据，邮箱登录保持未核实 |
| 09-29 17:48 将 lark provider 的 signupGroup 改为 `hyprial/group-internal`，DB/API 回读 | 默认注册入组配置修改成功；不是用户成员数组追加测试 | 变更后没有新注册用户，不能称“signup→自动入组”已用新用户实测 |
| 现有 10 个用户都只有 internal，生产仅一个 group | 当前成员状态计数 | API 增删另一个组且保留原组 **NOT_RUN**；多组数据结构支持不等于多组流程实测 |
| casdoor-test 只有 built-in/admin、built-in 和 app-built-in，无实验 org/app/users/groups | infra-ops 只读测试库；测试实例未做过 signup/login 实验 | health/discovery 正常不能充当真实身份实验 |
| 生产 invitation=0、webhook=0；record=165,135 | 当时只读计数快照，不是恒定值 | 原生邀请/通知能力存在，不代表已配置或链路已验证；请求记录也不是 Tailcat 连接审计 |

版本为 Casdoor 4.2.0 加 device-flow 补丁。报告仅聚合计数、schema 和既有变更备份，没有输出用户记录或凭据。本轮没有新建账号、配置邀请/角色、修改 Casdoor 或重新执行注册。

报告原文作为当时证据快照保留。其能力判断与 maintainer 固定源码核对分开：

- `Group.Manager` 不自动授予组权限，但 `authz.IsAllowed` 存在 `CheckApiPermission` 回退；不能把“未配置本组管理员”扩大为“Casdoor 权限不能参与自己的 API”。
- 删除含成员/子组的 Group，v4.2.0 源码有拒绝条件；这是 SOURCE_ONLY，报告未做真实删除探针。
- Tailcat 的账号公钥绑定和节点目录不是原生 IAM 功能，也不是 Tailcat 自动提供的组织控制面。SSO logout 不自动撤掉独立 Tailcat key/通道；本方案无需因此回到 Headscale 删除节点，而是落实网络准入与业务组权限撤销。

早期 BLOCKED 记录表示当时缺少凭据/回报；后来“已验证可用”的概括也不能覆盖当前明确的 NOT_RUN 项。上述细分证据是本版事实基线。真实 Casdoor 身份 → 设备绑定 → 邀请成员 → Tailcat 撤权的串联 E2E 仍未执行。

实验传输 worker 的 PAC 启动失败后由独立本地 worker 准备、maintainer 执行；身份子任务已收敛为交付既有证据，不新建受控实验，也不解除开发/发布冻结。

### 8.5 证据与复跑

- [实验报告](../evidence/tailcat-casdoor-2026-10-02/experiment/EVIDENCE.md)
- [逐项结果](../evidence/tailcat-casdoor-2026-10-02/experiment/results.json)
- [实际退出码和源码哈希](../evidence/tailcat-casdoor-2026-10-02/experiment/run-metadata.json)
- [最终运行日志](../evidence/tailcat-casdoor-2026-10-02/experiment/approved-final-run.log)
- [版本与文件清单](../evidence/tailcat-casdoor-2026-10-02/experiment/manifest.json)
- [Casdoor 既有证据报告](../evidence/tailcat-casdoor-2026-10-02/experiment/identity-existing-evidence.md)
- [Casdoor 只读预检（早期快照）](../evidence/tailcat-casdoor-2026-10-02/experiment/identity-preflight.json)
- [材料说明与复跑入口](../evidence/tailcat-casdoor-2026-10-02/experiment/README.md)

冻结实验源码 SHA-256：`621ed77af3b6635f3a00bce363167714d98d442a8dbe3f6d31728bd8404263ca`。

运行使用 task-local Go 1.27.1，GOMAXPROCS=2、go -p=2、单项 TestLAB、100 秒超时，未跑全套。可复跑源码与上游固定源码已归档；缓存和二进制未归档。复跑仍保留一项诊断失败，不应期待 exit 0。

清理实测：所有本地 Tailcat 引擎和 DERP/STUN 服务已关闭，原 TCP/UDP 监听端口可重新绑定；进程已退出。测试 key、邀请 token、地址/PSK 仅驻内存，没有生成私钥文件。测试材料中的公开上游 TLS fixtures 不是本次实验凭据。未修改生产服务、Hyprial 代码或全局工具。

## 9. 本轮不作出的承诺

- 不宣称真实 Hyprial CLI 已支持这些命令，或已完成 tsnet/Headscale 替换。
- 不把 Casdoor 实测和独立模拟身份的 Tailcat 实测相加称为完整串联 E2E。
- 不验证跨 NAT、生产多租户对抗安全、容量、HA、商业中继 SLA 或大规模性能。
- 前两轮实验不制定 Hyprial 实现/集成路线；后续获准的迁移规划见第 14 节，不恢复冻结的开发、发布或旧 PAC 图。


## 控制面板的最小可见能力

控制面板至少需要让用户看到以下事实；页面组织方式和 Hyprial 实现不在本轮范围内。

| 使用者 | 能做/能看到什么 | 不能混淆的状态 |
|---|---|---|
| 未登录用户 | 注册、登录、打开邀请预览 | 打开链接不等于消耗邀请或成为成员 |
| 已登录用户 | 本人组织列表、创建组织、接受邀请 | 已登录不等于已加入组织，更不等于设备可连接 |
| 组织管理员 | 成员和角色列表、邀请、撤销邀请、开除成员 | 操作已受理不等于全部在线/离线设备都已撤权 |
| 普通成员 | 自己的角色、设备及连接状态、退出组织 | 离开 alpha 不影响 beta；全局账号仍可登录 |
| 运维/审计人员 | 操作结果、待恢复/待同步项、连接失败分类 | 权限拒绝与网络不可达分开，不以超时证明授权生效 |

推荐在控制面结果中区分：身份认证、成员资格、设备绑定、连接准入、业务请求授权、撤权传播。邀请失败或 Casdoor 写入失败应能重试；不能只显示一个含糊的“加入失败/成功”。

本次未要求但后续应明确：组织删除、管理员转移、设备解绑、全局账号停用、邀请签发者被移除后的未消费邀请是否一并撤销。它们不应与 org deactivate 的“开除目标组织成员”混成一个操作。

### 组织权限变化的验收边界

- Casdoor 登录成功只能证明身份。控制面还要检查当前组织资格，不能依赖登录时缓存的旧 groups 长期放行。
- 成员被开除、设备被解绑、全局账号被禁用分别影响不同范围；本次只要求目标组织成员操作。
- 退出后再次打开旧邀请不得恢复资格。若网络响应丢失，同一操作是否返回已加入结果要独立规定；实验模型拒绝重放不等于完整生产幂等协议。
- 控制面失联时，撤权无法保证瞬时传递给离线端点。需要明确允许缓存的期限与失效策略；本轮不虚构时间保证。
- 实验中的公钥到用户映射若直接由夹具装入，只证明后续按绑定鉴权，不证明真实设备登记的所有权挑战。

## 10. Casdoor 是否已经满足这些职责

### 10.1 调查方法和结论

核对时间为 2026-10-02。公开文档用于理解产品能力，源码固定到 **Casdoor v4.2.0 / `cc39a9395698ca9cf6cf869c963a641c7c015a71`**；不把会变化的在线文档直接当作部署版本已启用的配置。本文没有修改 Casdoor、配置角色或执行新邀请，也没有对下面的新增能力矩阵做真实 E2E。

**应当复用 Casdoor 的身份、Group、Role、Permission/Casbin、会话和审计基础，只补组织流程与 Tailcat 网络语义。** 上一版“控制面必须承担”描述的是总职责，不代表这些能力都要重新开发。控制面板可以呈现 Casdoor 原生数据和能力；“有 API”与“我们要求的流程已经完成”仍需区分。

| 职责 | Casdoor 已有能力 | 是否满足本需求／最小剩余职责 | 依据 |
|---|---|---|---|
| 身份验证 | 注册、登录、OAuth/OIDC、用户信息、令牌与会话 | **直接复用基础能力**；第二轮实际完成3个用户的原生用户名/密码注册与登录、错误密码拒绝；邮箱/Lark/浏览器路径仍未在该轮验证。调用方仍须校验 token，不能自报 username | [登录处理源码](https://github.com/casdoor/casdoor/blob/cc39a9395698ca9cf6cf869c963a641c7c015a71/controllers/auth.go)、[OIDC 文档](https://casdoor.org/docs/how-to-connect/oidc-client/) |
| 组织及多组成员 | Group CRUD、用户 `Groups`、查询和移除组成员；用户可有多个 group | **直接复用数据与管理 API**；第二轮已测2个Group创建、成员追加/移除与保留其他组、清空成员后删除。Hyprial 组织对应 Group；自助创建、创建者授权、配额/幂等仍是业务流程 | [Group](https://github.com/casdoor/casdoor/blob/cc39a9395698ca9cf6cf869c963a641c7c015a71/object/group.go)、[用户接口](https://github.com/casdoor/casdoor/blob/cc39a9395698ca9cf6cf869c963a641c7c015a71/controllers/user.go) |
| 组织角色 | Role 可关联 Users、Groups、Roles、Domains；Permission 可绑定用户/组/角色/域和资源/动作 | **配置后复用**；第二轮实际创建Role/Permission并调用enforce，普通成员邀请/开除被拒；不必默认自研角色数据库或策略引擎。需定义“本组 admin/member”的准确资源/域范围；Group.Manager 本身不是已验证的组管理授权 | [Role](https://github.com/casdoor/casdoor/blob/cc39a9395698ca9cf6cf869c963a641c7c015a71/object/role.go#L29)、[Permission](https://github.com/casdoor/casdoor/blob/cc39a9395698ca9cf6cf869c963a641c7c015a71/object/permission.go#L26) |
| 一次性邀请 | 注册邀请码、Quota/UsedCount、Active/Suspended、可指定 SignupGroup 和注册信息 | **部分满足**；能支撑新账号注册邀请，但不是已登录账号加入第二组的完整协议。还缺本需求的并发单次领取、主体固定、过期语义与上游写失败恢复 | [邀请源码](https://github.com/casdoor/casdoor/blob/cc39a9395698ca9cf6cf869c963a641c7c015a71/object/invitation.go)、[原生邀请文档](https://casdoor.ai/docs/invitation/overview/) |
| 设备公钥绑定 | WebAuthn/MFA、OAuth device flow、通用属性及 Key 资源 | **没有现成的 Tailcat 登记闭环**。WebAuthn 凭证或 OAuth 设备授权不等于 WireGuard 节点 key；仍需主体与节点 key 所有权证明、生命周期和撤销关系 | [User 字段](https://github.com/casdoor/casdoor/blob/cc39a9395698ca9cf6cf869c963a641c7c015a71/object/user.go#L213)、[Key 类型](https://github.com/casdoor/casdoor/blob/cc39a9395698ca9cf6cf869c963a641c7c015a71/object/key.go#L25) |
| 组织节点目录（不是 NAT/对端地址探测） | 用户、组、会话等身份记录 | **仍需网络目录职责**；没有查到 Tailcat 地址发布、在线租约、对端端点和按组节点发现的原生能力。能存字符串属性不等于实现发现协议 | [User/Group 模型](https://github.com/casdoor/casdoor/blob/cc39a9395698ca9cf6cf869c963a641c7c015a71/object/user.go)、[路由清单](https://github.com/casdoor/casdoor/blob/cc39a9395698ca9cf6cf869c963a641c7c015a71/routers/router.go) |
| 组级业务鉴权 | Casbin 模型/策略、Permission，以及 `/api/enforce`、`/api/batch-enforce` | **策略表达与判定可以复用**；应用仍须提交可信主体、目标组资源和动作，并执行拒绝结果。Casdoor 不会自动拦截 Tailcat 上的业务消息 | [鉴权文档](https://casdoor.ai/docs/permission/exposed-casbin-apis/)、[API 路由](https://github.com/casdoor/casdoor/blob/cc39a9395698ca9cf6cf869c963a641c7c015a71/routers/router.go#L228) |
| 撤权 | 移除组成员、变更角色/Permission、停用用户、撤销会话/令牌、SSO logout | **身份侧部分满足**；Tailcat 既有连接、旧设备 key、离线缓存授权、按组业务会话的撤销仍需处理。不能用全局登出/禁用账号代替只退出一个组 | [成员移除](https://github.com/casdoor/casdoor/blob/cc39a9395698ca9cf6cf869c963a641c7c015a71/controllers/user.go#L779)、[令牌失效](https://github.com/casdoor/casdoor/blob/cc39a9395698ca9cf6cf869c963a641c7c015a71/object/token.go#L259)、[SSO logout](https://github.com/casdoor/casdoor/blob/cc39a9395698ca9cf6cf869c963a641c7c015a71/controllers/account.go#L537) |
| 状态审计 | Record、Webhook、WebhookEvent；事件队列、尝试次数、重试/退避和 replay | **直接复用身份侧审计与事件基础**；生产 record 有记录、webhook 尚未配置，不默认再造相同 webhook 投递队列。网络授权版本、真实连接拒绝、撤权确认及跨系统操作关联仍需补证据；事件送达不等于连接已撤销 | [Record](https://github.com/casdoor/casdoor/blob/cc39a9395698ca9cf6cf869c963a641c7c015a71/object/record.go)、[WebhookEvent](https://github.com/casdoor/casdoor/blob/cc39a9395698ca9cf6cf869c963a641c7c015a71/object/webhook_event.go)、[投递/重放 worker](https://github.com/casdoor/casdoor/blob/cc39a9395698ca9cf6cf869c963a641c7c015a71/object/webhook_worker.go) |
| 管理员转移 | Role/Permission 更新、Group.Manager 字段更新 | **有数据操作原语，未见完整移交流程**；管理员接受、并发转移、最后管理员和部分失败规则仍需定义。不能把修改 Manager 字符串当作权限已经转移 | [Group 更新](https://github.com/casdoor/casdoor/blob/cc39a9395698ca9cf6cf869c963a641c7c015a71/object/group.go#L149)、[角色接口模型](https://github.com/casdoor/casdoor/blob/cc39a9395698ca9cf6cf869c963a641c7c015a71/object/role.go) |
| 组织删除 | `delete-group`；有子组或成员时拒绝删除 | **复用删除原语和约束**；第二轮实际观测非空组拒绝删除及清空后的成功删除；组织停用、撤销邀请/设备授权、处理已有会话、审计和失败恢复不是删除一行 Group 自动完成的。不得删全局 Casdoor Organization 或共享用户 | [DeleteGroup](https://github.com/casdoor/casdoor/blob/cc39a9395698ca9cf6cf869c963a641c7c015a71/object/group.go#L252) |
| 设备解绑 | 登录会话删除、WebAuthn/MFA 凭证管理等 | **认证设备管理可复用，Tailcat key 解绑仍缺**；删除浏览器会话不等于拒绝持有独立节点私钥的 peer；保留同账号其他设备的范围也需明确 | [会话管理](https://casdoor.ai/docs/session/management/)、[会话模型](https://github.com/casdoor/casdoor/blob/cc39a9395698ca9cf6cf869c963a641c7c015a71/object/session.go) |

### 10.2 不能混淆的几个细节

**Group 管理员不是 Casdoor Organization 管理员。** `authz.IsAllowed` 对同 Casdoor Organization 的 `user.IsAdmin` 提供宽范围管理能力；普通 API 鉴权拒绝后还有 `CheckApiPermission` 回退。因此不能再写“只有 admin/app 永远能建组”，也不能为实现某一个 Group 管理员就赋予 `hyprial` 整个 Organization 的管理权。能否配置出我们要求的委派范围需按 Role/Permission、API 和字段修改规则一起验证；`remove-user-from-group` 还检查 Groups 的 account-item 修改规则。[v4.2.0 授权源码](https://github.com/casdoor/casdoor/blob/cc39a9395698ca9cf6cf869c963a641c7c015a71/authz/authz.go#L157)

**注册邀请码有能力，但覆盖范围不同。** 在所核对的 OAuth 登录路径中，Invitation 检查和 SignupGroup 写入位于 `user == nil` 的注册分支。已有用户登录不会因此追加另一组。`Invitation` 对象有状态和次数限制，但该版本结构中未见专门的 TTL/ExpiresAt 字段；次数检查与新增用户后的计数更新分开，不能据此承诺本需求的原子单次消费。这里是源码边界，不是新做的并发实测；原生新用户邀请能力可以复用，已有用户 join 与一致的单次语义仍需补流程。[登录/注册路径](https://github.com/casdoor/casdoor/blob/cc39a9395698ca9cf6cf869c963a641c7c015a71/controllers/auth.go#L1007)

**Group 数据写入无需重造，但必须保留其他组。** v4.2.0 的 `update-user` 支持 `columns`，会从旧用户合并指定字段；不应沿用早期“只能盲目完整对象覆盖”的笼统说法。更新 `groups` 仍然是在写一个成员数组，不能由此推断并发加入不同组时一定没有丢更新；应将成员回读、幂等和并发语义列为业务验收。[UpdateUser](https://github.com/casdoor/casdoor/blob/cc39a9395698ca9cf6cf869c963a641c7c015a71/controllers/user.go#L264)

**身份令牌失效不自动关闭网络。** Casdoor 有令牌失效、会话删除、注销通知；但一个只离线验证旧 JWT 签名且不检查最新状态的服务，不会因为数据库里令牌被置为无效就自动获知。组级退出也不应注销同账号在其他组的全部合法会话。Tailcat 连接、应用授权和身份会话需要区分，不能把 SSO logout 当成网络层撤权已完成。

**审计与可靠通知有可复用基础。** v4.2.0 的 WebhookEvent 已有持久事件、重试状态和重放，不能一概说“Casdoor 只有无保证的一次 webhook”。但触发条件、接收端鉴权/幂等、乱序、最大重试后处理、对账和网络执行结果仍要核验。本轮没有验证线上 webhook 配置或投递保证，也不宣称跨 Casdoor/Tailcat 的 exactly-once。

### 10.3 收窄后的控制面工作清单

优先复用，不再把以下内容列为从零开发：账号注册登录、用户/组 CRUD、Role/Permission 模型与判定基础、身份会话撤销、身份操作记录及 webhook 投递基础。

真正需要围绕本产品补齐或验证的最小集合：

1. **组织流程**：自助创建、创建者最小组级权限、新旧用户统一接受邀请、最后管理员/移交/删除规则。
2. **Tailcat 节点目录**：设备公钥的所有权及账号绑定、按组发现、地址保密、节点存续/密钥轮换和解绑。
3. **授权执行**：将 Casdoor 成员/角色/策略判定用于连接准入和有明确组织范围的通道或请求；若多个组共享同一连接，不能只检查双方有任意共同组。
4. **撤权闭环**：组织资格变化后，新接入、既有组会话、重连及离线授权都遵守新状态，同时保留其他组的合法访问。
5. **状态与审计闭环**：把原生身份事件与网络执行结果关联，显式显示待同步/失败，不能把 API 写入成功当成端点撤权完成。

这是功能归属和验收规划，不规定 Hyprial 的代码布局、进程、数据库、IPC 或集成实现。管理员转移、组织删除和设备解绑是必须明确的产品语义；其中的 Casdoor 原语应复用，不能把整项都误标成“原生全有”或“需要全部自研”。

## 11. Tailcat 多节点、tailnet 与撤权边界澄清

**Tailcat 不限于两台机器，但并不原生创建一个受管理的 tailnet。** 在本实验固定源码中，Server 维护多个 client peers，而一个 Client 配置一个 Server 地址；可以由多组连接构造多节点应用网络。`B↔A` 与 `C↔A` 可以同时存在，却不会自动为 B、C 分发完整组成员目录、授权关系和彼此可直连的地址。这是接口/源码能力说明，不是本轮做过三台物理机器跨 NAT 全互联。

“Tailcat 没有设备公钥/节点发现”的说法过宽，准确区分为：

| 能力 | Tailcat 已负责 | 上层需要负责 |
|---|---|---|
| 加密设备身份 | 生成/使用节点密钥，WireGuard 对端认证，识别 peer 公钥 | 该公钥属于哪个 Casdoor 主体、允许访问哪个 Group，以及登记/解绑语义 |
| 已知对端的发现和连接 | 地址携带公钥及 DERP 引导信息，交换端点，NAT 穿透，必要时中继 | 向已认证成员提供获准的组内节点/服务目录及相应连接信息 |
| 多节点应用网络 | 一个 Server 接受多个客户端；复用多个连接原语 | 决定哪些节点应互相连接，保持组成员变化与授权一致；无需自行实现加密或穿透 |

依据：[Tailcat 官方定位](https://tailscale.com/tailcat)和[本实验固定源码的 Client/Server、peerConfig、advertiseEndpoints](https://github.com/tailscale/tailcat/blob/b4dc28e8aa8936f0a90a41ad8293a64e3d6b645f/tailcat.go)。官方明确省略控制面与 tailnet 管理；本实验所用 `AllowClient`/`DisconnectClient` 是固定开发提交的接口，不冒充已发布 v0.7.0 的接口。

**Tailscale/Headscale 本来就有网络访问撤销能力。** Tailscale 有设备移除、用户暂停/删除和访问策略；Headscale 有节点 expire/delete 及策略管理。这些可以切断相应网络访问，进而使通过该网络的会话无法继续正常通信。此前将“连接撤权”统一描述成新增自研能力不准确：采用 Tailcat 后，上层要接回的是原控制面承担的“组/设备权限变化 → 网络准入和断开动作”的编排，而不是重新发明网络撤权。

依据：[Tailscale 设备移除](https://tailscale.com/docs/features/access-control/device-management/how-to/remove)、[用户暂停/删除](https://tailscale.com/docs/features/sharing/how-to/remove-team-members)、[Headscale v0.29.3 节点管理源码](https://github.com/juanfont/headscale/blob/v0.29.3/cmd/headscale/cli/nodes.go)。注销 IdP 会话、令节点过期、删除节点和长期禁止重新登记并不是同一个操作，必须按具体目标选择；本文不承诺所有方法都永久阻止重新授权。

“业务会话撤权”需进一步限定：

- **本轮最小目标**：已取消组资格的节点不能新建/恢复相应组访问，已有通道的后续访问也不能继续被允许；正常成员有正向对照。
- **多组共享连接时**：需要区分被撤销组与仍获准组。本实验选择逐条消息携带 Group 并检查，只是一个实验选择；若采用已明确绑定组织的独立通道/端点，也可在通道边界隔离，本文件不预先规定产品实现。
- **不默认增加长任务管理系统**：断开网络不保证取消已经启动的后台任务、回滚已经完成的写入或让外部凭据自动失效。这是额外业务语义，不应当作替换 Headscale 的必需前置条件。只有产品明确要求时才另行定义。

因此更准确的最小职责是：复用 Casdoor 的身份/组/权限，复用 Tailcat 的加密/穿透/连接原语，补齐**组织与设备 key 的对应关系、获准节点目录、权限变化的网络执行**。能形成自己的多节点 daemon 网络，不等于 Tailcat 已自带完整的 tailnet 控制面。

## 12. 第二轮：直接接入 Casdoor 的完整 org 周期

### 12.1 结果与范围

**业务周期已完成，结论为 `LIFECYCLE_COMPLETED_AFTER_REPAIRS`。** 实跑开始于2026-10-02 18:50:59 CST，root清理回读完成于18:57:05，18:57:46停止使用凭据并删除自己的秘密文件。原始日志83行：80 PASS、3处已修正失败；这些是操作/观察事件，不是83条独立E2E，也不是一条命令首次执行就exit0。

本轮使用一个专用 Casdoor Organization 中的3个真实用户A/B/C，两个真实Casdoor Group alpha/beta作为Hyprial逻辑组织。没有部署Headscale、K3s资源或真实Hyprial daemon，没有新增实际 `hyprial org` 命令。Bash场景脚本驱动Python标准库HTTP/JSON适配及独立Go Tailcat helper；用户身份、Group和策略数据来自实际Casdoor，不再使用第一轮的虚构成员表。

凭据由infra-ops在Allen终端明确批准后签发：仅该实验组织的操作员身份、最多2小时、0600文件交付；仅Authorization header，不用URL密钥或全局管理员。实验中普通用户会话先通过get-account核实身份，再由组织级操作员调用native enforce及管理API。因此验证的是“真实用户身份＋原生策略判定＋受控管理写入”，不是声称普通用户token已直接获准调用所有Casdoor管理端点。

### 12.2 原生与模拟的界线

| 功能 | 实际使用 | 验证结果 |
|---|---|---|
| signup/login | Casdoor `/api/signup`、`/api/login`、`/api/get-account`，专用app的用户名/密码通道 | 3个用户 registerType=Application Signup，身份回读成功，A错误密码被拒；未用管理员add-user替代注册 |
| org create | 原生 Group、用户Groups、Role、Permission；native enforce | A创建alpha/beta，拥有组管理员权限，但没有被提升为Casdoor组织管理员 |
| org invite/join | 原生身份/Permission判定和Group写入；邀请的digest、过期、claim/consumed由脚本模拟 | 新B在邀请签发后注册并加入；已有C加入；重复领取、其他主体重放、过期、非管理员邀请被拒或按既有成员幂等返回 |
| 多组成员 | 原生Group数组增量写入与回读，native group access判定 | B同时加入alpha/beta；移除alpha不丢beta |
| deactivate/leave | 原生`remove-user-from-group`与enforce；最后管理员规则为LAB编排 | 开除B的alpha资格后beta继续；B主动退出最后一个组后账号仍能登录，原连接/旧key访问被撤销 |
| 通信/撤权 | 固定Tailcat真实本地WireGuard/TCP/DERP；native回读状态显式同步，LAB设备key映射 | 同组payload匹配，跨组请求DENY；组移除/解散后旧key重连有实际拒绝回调，不仅是timeout |
| dissolve | 脚本draining/deleted状态与邀请失效，原生成员移除、Role/Permission/Group删除 | 非空Group原生拒绝删除；alpha解散时beta仍传输；最终两组删除、三用户Groups为空 |
| fixture清理 | 只删除本轮ledger对象并回读 | root创建的14个app/user/group/role/permission均不存在，本地秘密和临时网络资源清除；bootstrap归infra-ops清理 |

“设备登记/节点目录”在该实验中是受控的别名到真实Casdoor主体及临时key映射，并非已实现真实设备PoP挑战或动态发现服务。普通业务操作的原生Permission已实际判定；Tailcat helper按回读Groups执行本例组规则，不执行任意Casbin模型，也未启用自动webhook更新。此限制不能省略。

### 12.3 关键观测

1. B被移出alpha后，同一条held TCP连接上alpha请求DENY、beta请求仍回传正确payload。并未为保留beta而让alpha继续访问。
2. B退出beta、alpha解散时的C、beta解散时的B，三个精确旧key重连都被准入回调拒绝（每次denyHookDelta=5）；有相邻合法流量对照。原held连接只记录3秒read deadline内无数据，UNREACHABLE行是观察，不单独当撤权证明。
3. alpha解散后B仍通过beta真实通信；待解散组的未消费邀请和后续invite/join不能恢复组。用户可通过**新管理员邀请**重新获准加入仍存在的beta，旧邀请不能起同样作用。
4. 两组都解散后，A/C重新登录成功；B在此前退出全部组时已重新登录成功，最终B存在且Groups为空有回读，但未再做最终一次B登录。之后才清理这些实验账号。
5. Tailcat仍暴露Status的InEngine残留，logical PeerKey移除和拒绝行为分开记录；没有宣称引擎记录完全消失或立即RST。最终DERP计数发送17,988 bytes、接收21,595 bytes，STUN计数0。只证明同主机强制中继，未验证跨NAT/STUN穿透。

### 12.4 修正与续跑记录

- 组织级操作员调用enforce时，最初仅permissionId不足以让v4.2.0鉴权过滤器识别目标owner。补入与实际Permission完全相同的id后通过；没有伪造其他对象或扩大凭据权限。
- helper要求绝对state/socket路径，脚本最初传相对路径而未启动；修正到实验目录内绝对路径后继续。
- 删除实验应用时需带上该应用回读的organization，最初只给owner/name被拒；补齐后删除成功、回读不存在。

失败、修复及各阶段退出码均保留。最终修正脚本没有再创建一整套新fixture做一次全新无中断重跑；不能据此宣称一次全绿。只读证据复核认为完整隔离生命周期有充分证据，并指出身份方式、两个Group与一个Casdoor Organization、脚本代理管理写入及最后B登录的边界。

### 12.5 证据、清理与后续边界

- [第二轮报告](../evidence/tailcat-casdoor-2026-10-02/cycle/EVIDENCE.md)、[阶段汇总](../evidence/tailcat-casdoor-2026-10-02/cycle/RESULTS.json)、[原始事件](../evidence/tailcat-casdoor-2026-10-02/cycle/events.jsonl)、[退出码与源码记录](../evidence/tailcat-casdoor-2026-10-02/cycle/execution-history.json)
- [只读复核](../evidence/tailcat-casdoor-2026-10-02/cycle/REVIEW.md)、[root清理回读](../evidence/tailcat-casdoor-2026-10-02/cycle/cleanup-readback.json)、[停止凭据使用回执](../evidence/tailcat-casdoor-2026-10-02/cycle/credential-stop-receipt.json)
- [复跑说明与实验源](../evidence/tailcat-casdoor-2026-10-02/cycle/README.md)：源码加`.txt`后缀，仅作证据，不接入项目运行。没有附入grant/cookie/password/原始邀请token/PSK或私钥。

清理已闭环：root此前已删除14个对象并停用凭据；infra-ops于2026-10-02 20:56:50 CST完成另外3个bootstrap对象的删除回读、6个token撤销及grant文件清除，并报告8类表的实验前缀计数均为0，仅剩built-in组织。维护者核对ledger的3项DELETED_VERIFIED和token清理记录，并只检查grant目录不存在，没有再使用凭据或访问Casdoor。见[最终清理确认](../evidence/tailcat-casdoor-2026-10-02/cycle/bootstrap-cleanup-confirmation.json)。历史RESULTS/EVIDENCE/REVIEW中的等待状态是当时快照，以本补充闭环；不改写原实验结果。

管理员转移、真实PoP/动态目录、多设备扩容、并发/崩溃恢复、控制面离线撤权上界、浏览器/Lark/邮箱验证码、跨主机NAT和生产集成都不是本轮已完成项。以上结果回答的是用户要求的独立脚本org创建到解散周期，不解除其他开发/发布冻结。

## 13. 何时移除 tsnet 接入与 Headscale 依赖

### 13.1 当前决策与核查边界

**已确定迁移顺序：先硬切 Hyprial 的 tsnet 接入为 Tailcat，再逐步消除产品对 Headscale 的剩余依赖。共享 Headscale 明确保留给团队其他服务，不计划停服。** 第 12 节证明的是独立实验可行，不是现有产品已完成替换。今后不为新组织供应一个 Headscale/K3s 实例，与立即关闭既有网络，是两个不同决定。

2026-10-02 本次只读核对了远端 `dev`，源码基线为 **`bccc754d5d5f96d0ad838006dd2e220825755222`**（`git fetch origin dev` 后读取）。以下“现有功能”指这个提交中的产品路径，标记为 **SOURCE_VERIFIED**；没有把代码存在等同于每台生产机器正在使用。未访问生产控制面、读取节点密钥或执行停网实验；实际客户端版本、Hyprial 剩余旧网络依赖及共享资源归属均为 **UNKNOWN，迁移及专属资源清理前必须核对**；不要求把团队其他消费者迁出 Headscale。后续重构若改变这些路径，需要在候选发布 SHA 重新核查。

三个对象应分别处理：

- **tsnet 接入**：Hyprial 的 `hyprial-tsnet` 边车、登录入网、转发、状态与发布依赖。
- **Headscale 依赖**：客户端对控制面提供的注册、授权、网络视图和配置更新的依赖；不等于每个业务数据包经过 Headscale。
- **共享服务及宿主 VPN**：现有 Headscale/内嵌 DERP、官方 Tailscale 客户端、内部 DNS 与其他应用的访问。daemon 通信迁走不自动使这一层无人使用。

Tailcat 提供无需 Tailscale 控制面的数据通道，不是 tsnet API、Headscale 注册协议或整个主机 VPN 的即插即用替换。移除本项目的 tsnet 接入，也不表示移除 Tailcat 自身使用的 Tailscale 网络组件或不再需要 DERP。[Tailcat 固定提交 README](https://github.com/tailscale/tailcat/blob/b4dc28e8aa8936f0a90a41ad8293a64e3d6b645f/README.md)

### 13.2 已确认的功能依赖与硬切影响

下表证据编号对应 §13.6 的固定提交源码；影响是根据调用关系的分析，不是生产断网实测。

| 现有功能 | 对 tsnet / Headscale 的依赖 | 未替换就删除或停止时的影响 | 退役前处置 |
|---|---|---|---|
| CLI 注册后登录/入网、profile | 默认 profile 指向 `https://head.hyprial.ai`；CLI 做边车校验/安装并调用 `run_join`，由 tsnet 完成 interactive/preauthkey 入网；Casdoor 身份阶段独立于入网。[S1] | 身份认证可能成功，但网络 join 失败、等待授权或边车报错。仅改控制面 URL 不能让 tsnet 变成 Tailcat 客户端。 | 新用户和已有用户的身份、组织选择、设备登记和通信 ready 有替代路径；旧 profile 有明确迁移/拒绝提示，不能再隐式加入旧网络。 |
| Desktop 登录和 ready 判断 | `desktop_network.status` 同时要求凭据、`state/tsnet/node.json`、node 目录和有效边车；onboard 调用 `run_join`。[S2] | 即使实验 Tailcat 连通，现有桌面仍可能显示未加入/未就绪，触发旧入网路径。 | 与 CLI 一并验收；不能只修改命令行就宣称所有入口迁完。 |
| 跨机发现、监听、Zenoh 连接与断线恢复 | 边车状态提供 peers，`ForwardingEndpoints` 将每个 peer 映射为 loopback 端口；Go 转发层调用 tsnet Listen/Dial。daemon 监督边车并重建连接。另有宿主 `tailscale status` 的并存路径。[S3] | 删除边车会使依赖它的远端连接失效；本地 daemon 仍可能在线。换库不会自动接管发现、端口映射、重连。 | 验证新设备首次发现、双向业务通信、进程重启、对端重启、网络切换；显式证明没有偷偷走旧边车/宿主 VPN。 |
| 跨机 agent 消息/回执、远端 PAC、OrgFS 同步和查询 | 这些功能通过现有 Transport/Zenoh 使用上行网络；没有各自单独调用 Headscale API。远端 PAC 和 OrgFS 源码还依赖受信任 tailnet 的边界。[S4] | 跨机消息和回执无法送达、远端请求可能超时、presence 过期、同步中断；不能把本地 IPC 正常当成跨机正常。只放开新连接而未迁移组织/主体信任，还可能把错误主体的数据放进来。 | 按业务做端到端验收，包含回执/幂等恢复、远端结算与文件一致性；保持主体和组级授权，而不只测 TCP 通。 |
| OrgFS 网页入口、端口暴露及访问者身份 | 边车 `Expose` 可携带由 WhoIs 解析出的 PROXY v2 身份；webserver 的另一入口使用 tailscaled LocalAPI WhoIs，无可信身份会拒绝。网页链接还从 MagicDNS/FQDN 派生。[S5] | 删边车后暴露端口不可达；只接上普通 Tailcat TCP 而不传可信身份，网页会拒绝访问；把拒绝改为匿名放行则破坏权限。旧 `fs.<tailnet domain>` 链接可能失效。 | 为网页/暴露入口提供明确替代身份与地址，或明确保留该功能的旧网络依赖；不能为上线取消鉴权。 |
| `hyprial transfer` 的目标发现和文件搬运 | 先从本地 daemon 的 hosts 选目标；真正远程控制/文件搬运是系统 SSH、scp、rsync，不是 tsnet 的 Dial。[S6] | daemon 能用 Tailcat 通信，也不代表系统 SSH 能访问原来的 tailnet 地址；工作目录/会话/镜像搬运仍可能失败。 | 核实实际 SSH 路由，保留独立可达路径或单独迁移此功能。不要把 OrgFS 同步与 transfer 当成同一传输。 |
| 自建 tailnet 身份入口、既有标识和状态 | `hyprial init` 的 self-host 分支使用宿主 Tailscale 身份；actor URI 包含 owner/machine；profile 和 tsnet 状态另有持久化契约。[S7] | 删除宿主客户端会影响 self-host 身份初始化；顺便改 machine/owner 可能使旧目标 URI、未完成请求和记录不再对应。旧 tsnet 密钥目录不会自动成为 Tailcat 设备身份。 | 决定该入口保留还是退役；对账号、节点和 URI 做显式迁移/兼容验证，不能用改名掩盖迁移。 |
| 安装、发布与运维诊断 | 边车版本/校验、独立构建资产和 Headscale E2E 仍在产品及工作流内。[S8] | 只删源码或二进制会留下安装下载失败、旧 ready 判定和失去覆盖的发布门槛。 | 所有受支持平台的新安装/升级都验证后，再移除旧资产依赖和替换专属测试；回滚窗口内保留所需旧版本。 |

**不是所有功能都依赖 Headscale。** Casdoor 的身份认证、Group/Role/Permission API 不因此退役；本地 IPC、本机 agent 会话和本地文件不会因停止边车而自动被删除。显式 LAN/static endpoint、官方 Tailscale 网络等其他路径可能继续工作，所以“本机还能用”或“还有一条连接通着”都不能证明迁移完成。飞书适配器的公网连接也不能单凭这张表断言必然停止；远端 agent 的路由若依赖被切断的 mesh，相关消息会受影响。

### 13.3 四种“硬切”不是同一件事

1. **删掉/停止 `hyprial-tsnet`，产品代码和配置不变**：依赖该边车的转发、入网、端口暴露和 Desktop ready 会失效。某些运行中的进程短期仍活着，不代表重启后可恢复。
2. **只停 Headscale，保留客户端**：新节点注册、重新授权及配置/peer 更新受阻；已缓存密钥与路径的连接可能暂时继续，不能写成“所有连接立刻断”，也不能用“关了仍能 ping”认定无依赖。官方对协调面停机与数据面的区分支持这个风险模型，但本次没有验证这台 Headscale 的实际断网时序。[官方说明](https://tailscale.com/docs/reference/coordination-server-down)
3. **把 daemon 全部换为 Tailcat，却不补控制面和上层契约**：旧 tsnet 节点不会自动出现在新目录；实验脚本中的账号/设备绑定、邀请与撤权状态不会自动变成产品功能；原先由网络边界承担的信任也不会自动保留。若新旧节点没有经验证的互通路径，升级一半就会把网络分成两部分。
4. **连共享 Headscale、DERP、宿主 Tailscale、DNS 一起撤掉**：可能同时影响内部 Git/SSH、transfer、网页和其他主机服务。Headscale 内嵌 DERP 若随服务退出，依赖它的中继流量也受影响，不能套用“仅协调面停机”的结论。现有拓扑有共享 Headscale/内嵌 DERP 的历史记录，实际消费者与占用量未在本轮复测，状态为 UNKNOWN；这一整网撤除不属于本次计划，保留部署不做停服实验。

对新旧并行期，不能把“Tailcat 撤权了”当成完整撤权：旧 tailnet 路径仍开放时，用户可能继续从旧入口访问。每个入口都应受同一组织权限约束，或在该组织切换时明确关闭旧入口。这里要求的是访问结果一致，不预先规定实现方式。

### 13.4 已确定的迁移节点

后续用户裁定取代早先“停共享 Headscale 再删控制面数据”的建议。正式工作包与验收见第 14 节。

| 节点 | 允许完成的结果 | 当前状态 |
|---|---|---|
| T0 实验与规划 | 采用 Tailcat + Casdoor 方向，不再为每个新组织供应 Headscale/K3s | 有独立实验依据；迁移计划已写入本 PR |
| T1 候选实现与隔离验收 | 做好替代版本、状态迁移、跨机业务验证及回滚包 | NOT_RUN |
| T2 硬切 tsnet | 统一切换 Hyprial 接入为 Tailcat，新版本不再启动/安装/回退 tsnet | NOT_RUN；实施细节见 §14.4 |
| T3 Hyprial 消除 Headscale 依赖 | 逐项迁移宿主发现、身份、网页、transfer 等剩余依赖，通过无旧网络的完整验收 | NOT_RUN；团队 Headscale 始终保留 |
| T4 专属旧资源归档 | 回滚保留期结束后仅清理经核实属于 Hyprial 的旧状态/节点/凭据 | NOT_RUN；不删共享服务、DNS、账号或他人资源 |

### 13.5 切换与回滚原则

生产采用用户指定的一次性切换，隔离环境先完成候选验收。不承诺新旧客户端互通，不以长期双栈为前置；具体在途工作、离线节点、状态和回滚步骤见 §14.4–14.7。发生问题通过固定旧版本和状态恢复，而不是新版本静默回退旧接入。

这一节的源码事实与风险分析仍有效，旧的共享 Headscale 停服建议已撤回。本 PR 只改规划文档，不实际切换、清理或解除开发/发布冻结。

### 13.6 固定源码依据

以下链接全部固定到本轮读取的 `dev` SHA，避免后续重构让证据指向另一版实现；源码核对不等于已执行这些路径。

- S1：[默认 profile](https://code.hyprial.com/HyprialOS/harness-bridge/src/commit/bccc754d5d5f96d0ad838006dd2e220825755222/src/hyprial/network_profile.py#L130)、[CLI 登录编排](https://code.hyprial.com/HyprialOS/harness-bridge/src/commit/bccc754d5d5f96d0ad838006dd2e220825755222/src/hyprial/cli.py#L1549)、[join 驱动](https://code.hyprial.com/HyprialOS/harness-bridge/src/commit/bccc754d5d5f96d0ad838006dd2e220825755222/src/hyprial/tsnet_join.py)。
- S2：[Desktop 身份/入网/ready](https://code.hyprial.com/HyprialOS/harness-bridge/src/commit/bccc754d5d5f96d0ad838006dd2e220825755222/src/hyprial/desktop_network.py#L15)。
- S3：[自动 forwarding 前置状态](https://code.hyprial.com/HyprialOS/harness-bridge/src/commit/bccc754d5d5f96d0ad838006dd2e220825755222/src/hyprial/forwarding_config.py#L272)、[发现与映射](https://code.hyprial.com/HyprialOS/harness-bridge/src/commit/bccc754d5d5f96d0ad838006dd2e220825755222/src/hyprial/daemon/discovery.py#L96)、[tsnet backend](https://code.hyprial.com/HyprialOS/harness-bridge/src/commit/bccc754d5d5f96d0ad838006dd2e220825755222/sidecar/hyprial-tsnet/internal/node/backend.go#L72)、[daemon 发现/重连](https://code.hyprial.com/HyprialOS/harness-bridge/src/commit/bccc754d5d5f96d0ad838006dd2e220825755222/src/hyprial/daemon/application.py#L14054)。
- S4：[消息与回执](https://code.hyprial.com/HyprialOS/harness-bridge/src/commit/bccc754d5d5f96d0ad838006dd2e220825755222/src/hyprial/inbox/zenoh_link.py#L27)、[远端 PAC 信任边界](https://code.hyprial.com/HyprialOS/harness-bridge/src/commit/bccc754d5d5f96d0ad838006dd2e220825755222/src/hyprial/dispatch/remote_workflow.py#L1)、[OrgFS ingress 信任](https://code.hyprial.com/HyprialOS/harness-bridge/src/commit/bccc754d5d5f96d0ad838006dd2e220825755222/src/hyprial/orgfs/mesh.py#L880)。
- S5：[Expose/WhoIs](https://code.hyprial.com/HyprialOS/harness-bridge/src/commit/bccc754d5d5f96d0ad838006dd2e220825755222/sidecar/hyprial-tsnet/internal/forward/forward.go#L376)、[网页 fail-closed 身份解析](https://code.hyprial.com/HyprialOS/harness-bridge/src/commit/bccc754d5d5f96d0ad838006dd2e220825755222/src/hyprial/orgfs/webserver.py#L818)、[网页地址推导](https://code.hyprial.com/HyprialOS/harness-bridge/src/commit/bccc754d5d5f96d0ad838006dd2e220825755222/src/hyprial/orgfs/web.py#L1)。
- S6：[transfer 目标发现](https://code.hyprial.com/HyprialOS/harness-bridge/src/commit/bccc754d5d5f96d0ad838006dd2e220825755222/src/hyprial/transfer/orchestrator.py#L284)、[SSH/scp/rsync runner](https://code.hyprial.com/HyprialOS/harness-bridge/src/commit/bccc754d5d5f96d0ad838006dd2e220825755222/src/hyprial/transfer/ssh.py#L88)。
- S7：[宿主 tailnet 身份](https://code.hyprial.com/HyprialOS/harness-bridge/src/commit/bccc754d5d5f96d0ad838006dd2e220825755222/src/hyprial/tailnet_identity.py#L72)、[owner 来源](https://code.hyprial.com/HyprialOS/harness-bridge/src/commit/bccc754d5d5f96d0ad838006dd2e220825755222/src/hyprial/daemon/identity.py#L74)、[URI 结构](https://code.hyprial.com/HyprialOS/harness-bridge/src/commit/bccc754d5d5f96d0ad838006dd2e220825755222/src/hyprial/uri.py)。
- S8：[边车安装/校验](https://code.hyprial.com/HyprialOS/harness-bridge/src/commit/bccc754d5d5f96d0ad838006dd2e220825755222/src/hyprial/tsnet_sidecar.py)、[独立发布与 Headscale E2E](https://code.hyprial.com/HyprialOS/harness-bridge/src/commit/bccc754d5d5f96d0ad838006dd2e220825755222/.forgejo/workflows/sidecar.yml)。

**深模块重构后的路径对照（2026-10-03，`refactor/reconstruct-modules`）。** 上面的固定链接保持原样作为证据；按 §13.1 要求在重构树上重新核查后，同一能力现位于下列位置（Go 边车与 workflow 路径未变）。候选发布 SHA 确定时需再核一次。

| 编号 | `bccc754d` 路径 | 重构后路径 |
|---|---|---|
| S1 | `network_profile.py#L130` / `cli.py#L1549` / `tsnet_join.py` | `daemon/impl/configuration/network_profile.py:138` / `shell/impl/cli_commands/setup/login_flow.py:577` / `daemon/impl/network/tsnet_join.py:480`（`run_join`） |
| S2 | `desktop_network.py#L15` | `shell/impl/desktop_network.py:15`（`status`） |
| S3 | `forwarding_config.py#L272` / `daemon/discovery.py#L96` / `application.py#L14054` | `daemon/impl/forwarding_config.py` / `daemon/impl/bootstrap/discovery.py:89`（`ForwardingEndpoints`） / `daemon/impl/application/netendpoints/endpoints.py:544`（`_discover_peer_endpoints`），监督见 `netendpoints/forwarding.py` |
| S4 | `inbox/zenoh_link.py` / `dispatch/remote_workflow.py` / `orgfs/mesh.py#L880` | `daemon/impl/inbox/links/zenoh_link.py` / `daemon/impl/dispatch/remote/__init__.py` / `daemon/impl/orgfs/mesh.py`，ingress 信任见 `daemon/impl/orgfs/network/ingress.py` |
| S5 | `orgfs/webserver.py#L818` / `orgfs/web.py` | `daemon/impl/orgfs/webserver/identity.py` / `daemon/impl/orgfs/web.py`；CLI `--whois-socket` 在 `shell/impl/cli_commands/orgfs/fs.py`，`network whois` 在 `shell/impl/cli_commands/config/network.py` |
| S6 | `transfer/orchestrator.py#L284` / `transfer/ssh.py#L88` | `daemon/impl/transfer/orchestrator.py` / `daemon/impl/transfer/execution/ssh.py` |
| S7 | `tailnet_identity.py#L72` / `daemon/identity.py#L74` / `uri.py` | `identity/impl/tailnet_identity.py` / `daemon/impl/configuration/identity.py:74`（`resolve_node_owner`） / `kernel/impl/primitives/uri.py`；self-host 入口 `shell/impl/cli_commands/setup/selfhost.py` |
| S8 | `tsnet_sidecar.py` / `.forgejo/workflows/sidecar.yml` | `daemon/impl/network/tsnet_sidecar.py` / 未变 |

## 14. 完整迁移计划：先硬切 tsnet，再完成 Hyprial 的 Headscale 解耦

### 14.1 目标、阶段边界与完成定义

Allen 已明确选择：**先将 Hyprial 的 tsnet 接入一次性替换为 Tailcat，然后逐步消除 Hyprial 对 Headscale 的剩余依赖；团队共享 Headscale 部署继续运行，供其他服务使用。** 本节取代之前把“共享 Headscale 停服”列为终点的建议。这里的硬切指在已验证的候选版本上统一切换，不是先删 tsnet 再现场补功能，也不要求长期双栈或新旧协议互通。

- **阶段 A 完成：TSNET_CUTOVER_COMPLETE。** 本批 Hyprial 客户端/daemon 使用 Tailcat，不再启动、安装或回退到 `hyprial-tsnet`；原边车承担的功能已迁移。宿主 VPN 上的网页、SSH transfer 等独立旧依赖可短期留在清单中，不能声称产品已完全摆脱 Headscale。
- **阶段 B 完成：HYPRIAL_HEADSCALE_INDEPENDENT。** 在没有 Headscale、tailscaled、旧 tsnet 状态、tailnet 路由/MagicDNS/WhoIs、Headscale 内嵌 DERP的干净环境，Hyprial 所有继续提供的功能可安装、注册登录、运行、升级和恢复。用隔离环境证明，不关闭团队服务来做实验。
- **团队基础设施保持。** 不停 Headscale/Headplane，不删共享 Casdoor VPN app、共享 DNS、ACL、路由或团队节点；不卸载供其他软件使用的官方 Tailscale 客户端。Hyprial 专属旧节点/凭据仅在范围核实后单独处置。

Headscale 保留是最终形态的一部分，不是还欠一个停服步骤。Tailcat 的 Tailscale 底层依赖可以保留；要移除的是产品直接使用的 tsnet/Headscale 接入。若还有一项 Hyprial 必需功能只能经旧网络运行，阶段 B 就没有完成。

本轮授权是规划并更新文档 PR，不是启动开发、派单、合并、发布或生产切换。以下负责人是建议责任边界，不表示已经派发；全部实施和迁移验收状态为 **NOT_RUN**。第 12 节的独立实验仅作为可行性输入。

### 14.2 工作包、产物与依赖顺序

| 工作包 | 范围及明确产物 | 建议责任方 | 前置 / 完成条件 |
|---|---|---|---|
| M0 固定迁移契约与对象清单 | 重构后候选基线 SHA；平台/版本与节点清单；功能、旧依赖、组织/账号/设备/URI 对照；离线节点处置；各项验收阈值；确定迁移组织和窗口 | k3s-maintainer 牵头；CLI/daemon 维护者核对；infra-ops 核对共享资源 | 在实现前冻结；§13 八类功能均有去向，没有未声明停用项 |
| M1 控制面产品化 | Casdoor 身份/Group/Role/Permission 复用；持久邀请与审计；设备公钥所有权及主体绑定；按组目录；权限版本/期限与撤权传播；成员、管理员转移、解散和设备解绑状态机 | 控制面维护者（待指定），infra-ops 提供环境 | M0；将实验模拟替换为持久、可恢复实现，身份材料真正校验，包含并发和上游失败证据 |
| M2 Tailcat 接入和通信契约 | 固定 Tailcat/toolchain；认证过的设备连接；发现/监听/双向传输/重连/状态；按组织授权；不依赖共享 Headscale 的 DERP/STUN 引导与可观测性 | daemon/传输维护者（待指定） | M0；可用假控制面开发，必须与 M1 联测。保留业务 Transport 的结果语义，不因换网络重新定义消息/PAC/文件提交成功 |
| M3 客户端与状态迁移 | CLI/Desktop 同步切换身份→组织→设备→连接 ready；版本化 profile 迁移及日志；新安装/升级包；删除新版本启动 tsnet 的路径；替换所有由边车承载的 Expose/网页入口 | hyprial-cli-developer 协调客户端维护者 | M1/M2；迁移可重试，不覆盖用户无关配置，不复制旧 tsnet 密钥冒充 Tailcat 密钥 |
| M4 阶段 A 验收和切换包 | 候选版本、平台资产、业务验收结果、资源与错误读数；逐步切换单、回滚包、在途任务处置表；临时 Headscale 依赖台账 | k3s-maintainer + 独立评审者（待指定） | M1–M3；§14.5 的 A 门槛全部通过，才能提执行窗口 |
| M5 一次性切换 | 按 §14.4 统一停旧接入、升级、启动新接入和回读；离线旧版本隔离/升级提示；观察报告 | 维护者执行产品更新；infra-ops 配合核查共享服务 | M4 + 实际生产切换授权；失败走显式回滚，不自动回退旧网络 |
| M6 清除剩余 Headscale 依赖 | 网页身份和地址、SSH transfer、self-host 身份入口、静态 endpoint、诊断、发布/E2E 等逐项关闭台账；只保留明确的历史兼容读取 | 各功能维护者，k3s-maintainer 汇总 | M5 后逐项交付；可提前准备但不把未完成项包装为阶段 A 全产品独立 |
| M7 全产品无 Headscale 验收与归档 | 干净环境完整业务测试、升级/恢复/权限负例；依赖扫描和运行访问证据；专属旧资源清理提案与最终责任交接 | 独立验收者 + k3s-maintainer；infra-ops 核对清理对象 | M6；§14.5 的 B 门槛通过，宣布产品独立，团队 Headscale 仍正常运行 |

关键顺序：`M0 → (M1 + M2) → M3 → M4 → M5 → M6 → M7`。并行只表示未来计划的依赖关系，不在本 PR 实际创建 worker。日期根据候选版本和环境实际就绪后确定，不以实验完成日期承诺上线。

### 14.3 必须在开发前明确的产品契约

**身份与组织。** 注册/登录使用 Casdoor；身份用验证后的 issuer/subject 绑定，不信客户端提交的用户名。无组织的用户可以完成身份认证并创建/接受组织邀请，但不能因此获得任意节点通信权限。现有默认 internal 组不等同于已选择目标组织。继续复用 `org` 命令的 create/invite/join/leave 或 quit/deactivate/delete/list/info 等成员管理入口；精确命令名在 M0 收敛，已有 show/status/fetch/import 的文档采纳语义不被悄悄改写。

**邀请、角色与解散。** 新旧用户在同一邀请流程内认证、确认、原子领取；原始链接不进入 argv/日志；失败有幂等恢复。Casdoor 的成员写入要保留其他组，处理读改写竞争。定义最后管理员保护和显式移交、组织 draining→撤权→删除、设备解绑、已离组用户重新加入的新授权；不能用全局禁用账号替代组内开除。数据库/存储与 API 精确实现留给契约评审，本计划不要求沿用先前 K3s/ConfigMap 方案，也不沿用“约 500 行”工作量估算。

**节点与授权。** 真正证明新设备持有其公钥，绑定账号、组织范围和设备代次；提供获准节点目录。Tailcat 的公钥加密身份不能替代业务主体校验；旧受信任 tailnet 的假设必须迁成明确的通信/资源授权。多组共用连接时撤销一组不影响另一组，失联端权限有明确失效上限。控制面故障不放宽权限，邀请/成员/设备操作失败不得报成功。

**业务和状态。** 默认保留稳定 owner、逻辑 node/machine ID 和既有 actor URI；旧 URI 中形似 DNS 的字段不能继续被默认当作网络解析地址。若确需改 ID，先交付显式对照和引用迁移，禁止自动新建第二个逻辑节点。messageId/幂等键、回执、PAC request-id/owner、文件版本和内容哈希必须跨切换保持；本地数据库、agent home、会话和未完成工作不因换传输被重置。

**服务端点。** orgService、Casdoor、目录、下载源和中继必须有明确可达性与信任配置；最终环境不能靠 `<internal MagicDNS domain>`/`.ts.net` 的旧解析或路由才能启动。Tailcat 的 DERP/STUN 与控制面分开记录：不得把指向 Headscale 内嵌 DERP 的旧地址当成“已经独立”。独立自建或外部中继的容量、故障、费用和版本需在 M0/M2 选定，不擅自部署或购买。

### 14.4 阶段 A：一次性切换执行单（待执行）

| 步骤 | 执行内容 | 放行/失败处理 |
|---|---|---|
| A0 准备 | 列出同一切换域全部参与节点、平台、版本及责任人；预下载新/旧资产；备份应用配置和持久状态，记录校验与恢复点；独立保存迁移日志，秘密不入报告 | 资产不全、未知节点或恢复包未验收则不开窗；共享基础设施不动 |
| A1 处理在途工作 | 暂停新跨机工作接收；盘点消息/回执、远端 PAC、OrgFS 写入、transfer；能完成的先完成，无法完成的标记持久化恢复或显式中断 | 不能仅等一个固定秒数就认为已排空；不清空队列、不伪造 complete，不静默重跑有副作用的任务 |
| A2 停旧接入并迁移 | 对本批客户端正常停应用/旧边车，证明进程退出；在停止状态备份/校验最终快照；升级到固定候选版，迁移配置/逻辑身份，登记新设备 | 不先删除状态；失败时停止切换并按最后一致恢复点回滚。宿主 tailscaled 和共享 Headscale 保持运行 |
| A3 启动 Tailcat | 一次性启用新版本；真实认证、组织选择/映射、设备绑定、目录获取；建立新的业务连接并显示真实 ready | 不提供运行时 tsnet fallback；旧版本不得以未验证设备加入新网络。混合版本互通不作为支持承诺 |
| A4 业务验收 | 双向消息与回执、远端 PAC 结算、OrgFS 同步、已迁移网页入口、加入/开除/退出/解散、重连及多组负例；检查实际网络路径 | 任何越权/数据错误立即停止放行；连通失败按约定回滚时限处理。恢复在途工作必须按原 ID 核对结果 |
| A5 观察并开放 | 按批恢复工作接收，记录版本/参与者、故障、撤权时延及共享服务健康；离线节点回归时先升级和重验设备授权 | 观察通过宣布 TSNET_CUTOVER_COMPLETE；剩余宿主网络依赖逐项进入 M6，不称为全产品独立 |

这是一批参与者在同一窗口里的协调切换，不是先升级一台后期待它继续与未升级节点互通。准备/隔离验收可以逐台做；生产窗口允许有明确中断。离线节点不无限阻塞窗口，但必须从本批范围排除并有责任人、升级入口、旧访问撤销措施；回来后不能继续获得该组织旧入口的旁路访问。

新版本可以删除旧 tsnet 实现；回滚依靠完整旧版本和状态快照，而不是在新版本内隐藏自动 fallback。观察期内旧资产仅作恢复资料，不应继续开着另一条未同步权限的 Hyprial 通道。

### 14.5 验收矩阵：实验成功不等于迁移成功

下表全部为未来验收要求；当前均 **NOT_RUN**，不能由第 12 节的单机实验抵扣。每项保留命令、版本、拓扑、实际结果和失败证据；mock/fixture 与真实结果分列。

| 验收项 | 阶段 A 放行要求 | 阶段 B 最终要求 |
|---|---|---|
| 身份和组织 | 实际 CLI/Desktop 的新/旧账号登录、建组、邀请、重复/过期/错误身份拒绝，多组操作及持久恢复；覆盖拟上线登录方式 | 干净环境重复通过，认证/控制服务均不经 Headscale 网络 |
| 设备与目录 | 多设备真实公钥所有权验证，冒认/撤销设备拒绝，节点上下线/密钥更换有证据 | 从零状态启动和恢复都不读取 tsnet/tailscaled/Headscale 状态 |
| 网络 | 至少两台不同网络机器实际 Hyprial 双向通信；分别验证直连与强制 DERP；重启/网络切换/中继故障有明确状态；三节点验证需要的组内拓扑 | 隔离环境无旧宿主 VPN/路由，禁止到旧控制面、旧中继和 LocalAPI 的访问后仍通过 |
| 业务一致性 | 消息投递与回执、远端 PAC、OrgFS 内容/版本均有回读；切换后无遗漏且不重复执行已确认副作用 | 同样覆盖所有保留功能，包括网页与 transfer，不允许“依赖旧网络的功能不测” |
| 组级撤权 | 新接入、已有组会话、旧 key 重连、另一组继续可用；控制面失联/重启后不会恢复旧权限；每个 DENY 有健康正向对照 | 所有保留入口使用新身份和组织规则，旧网络入口不能旁路 |
| 安装升级和恢复 | 各受支持 OS/架构的新装/升级、旧 profile 迁移、失败恢复；未升级节点明确拒绝或升级提示；单 daemon/进程退出边界验证 | 安装、运行、诊断和发布门槛不要求 Headscale 或旧 sidecar 资产；旧格式可只读识别并迁移 |
| 可观察性与资源 | 区分认证、权限、目录、连接、中继故障；采样 CPU/内存/连接数、重连时延；保留历史 Status peer 残留问题的实测结论，不冒充清理成功 | 依赖扫描与实际进程/流量/解析/LocalAPI 访问记录共同证明独立；仅 grep 零命中不算完成 |
| 回滚 | 在隔离环境真实恢复旧版本、配置和业务，不丢未完成状态，不恢复已撤销资格 | M6 每个增量也能恢复；最终清理前恢复资产仍有效 |

**建议验收数值，须在 M0 确认后才能执行**：在线端撤权从权威提交到拒绝新请求不超过 30 秒；失联端授权租期不超过 5 分钟且到期拒绝；网络恢复后的业务重连不超过 60 秒；已确认消息/文件提交零丢失、已确认副作用零重复；切换窗口预算 30 分钟，其中预留 15 分钟回滚，预计超窗立即停止放行。若实际产品协议不适用这些值，必须在切换前明确替换值、原因和责任人，不能事后放宽以制造通过。

建议 M5 后连续观察 7 天正常工作负载，M7 最终无 Headscale 候选版再观察 7 天；同一观察期只有在两套要求都满足时才可共用。这是待确认的观察建议，不是已批准的等待时长或上线日期。等待不能替代真实重启、权限期限、离线节点和回滚测试。

### 14.6 阶段 B：逐步清除 Hyprial 对 Headscale 的剩余依赖

| 遗留项 | 迁移方向 | 退出证据 / 禁止误判 |
|---|---|---|
| 宿主 `tailscale status` 自动发现/监听与旧静态 seeds | 使用认证后的组织目录和 Tailcat 通信端点；显式删除默认探测及隐式 fallback | 无 tailscaled/旧路由仍可发现并通信；地址字符串还在历史记录中不等于运行依赖 |
| profile、init self-host、身份/旧入网状态 | 默认身份/组织流程迁到 Casdoor/新控制面；旧 profile 版本识别和一次性迁移；停用入口给明确说明，不自动降级匿名身份 | 不再要求 head.hyprial.ai、preauthkey 或 `tailscale whoami`；URI/账号稳定性及升级恢复有证据 |
| OrgFS 网页、WhoIs、DNS 和非边车入口 | 将可信访问身份绑定到新身份/组织链，提供浏览器可达的入口；Tailcat 通道本身不等于普通浏览器可访问 | 无 WhoIs/旧 MagicDNS 下仍可合法访问并拒绝越权；若原入口依赖 tsnet，必须已在 M3 完成替换，不能拖到此阶段才修断链 |
| `hyprial transfer` SSH/scp/rsync | 目标是让所保留的远程控制与搬运路径通过新传输或独立于 Headscale 的授权连接完成；具体协议由该功能维护者设计并评审 | 真实搬运/恢复及内容核对通过；“团队还保留 VPN 所以能 SSH”只算阶段 A 临时可用，不算阶段 B 完成 |
| 控制面/中继/下载的间接路径 | 确保 Casdoor、orgService、目录、DERP/STUN、所需下载源不依赖 Headscale 下发的 DNS/路由或内嵌 DERP；锁定证书信任和服务地址 | 独立网络路径的安装/首次启动/重连都通过；不需要迁移团队 Git 服务本身，但不能把其仅 VPN 可达的路径作为产品安装必需入口 |
| 专属源码、诊断、发布与测试 | 删除产品对旧 tsnet/Headscale 客户端的调用、默认 URL/配置、自动安装/修复/状态假设；替换专属 E2E。保留说明性历史材料及只读迁移器 | 必须检查运行调用与资产引用；不要求删除 Tailcat 合法使用的 Tailscale 底层库，也不因迁移盲删所有含 tailscale 的字符串 |
| Hyprial 专属旧资源 | 观察和回滚保留期结束后列出自己的旧 node、凭据、sidecar 状态/资产，按台账撤销或归档 | infra-ops 核对所有权并按实际授权操作；不删除共享账号、Casdoor group/VPN app、服务数据库或别人的节点；不收集密钥值作证据 |

每一项用 `待迁移 → 实现/评审 → 独立路径实测 → 从依赖台账关闭` 跟踪；必须写责任人、候选 SHA、证据和剩余条件。若决定不再提供某个既有功能，需单独明确产品退役决定及用户迁移方式，不能为了过门槛静默取消。

### 14.7 回滚、移交与最终汇报

切换出现错误主体可访问、状态/数据不一致时立即停止放行；普通连通故障达到约定预算时转回滚。回滚步骤：暂停新跨机工作，记录已确认与未确认操作及权限变化，停新接入并证明进程退出，恢复固定旧版本和兼容状态，核对成员撤权/回执/文件结果后恢复服务。不得直接把过时授权快照覆盖回去；无法安全恢复最新权限时保持相关组织暂停，并明确报告阻塞。不要同时启动新旧两个接入来掩盖失败。

迁移日志至少记录 schema/迁移版本、节点逻辑 ID、前后版本/状态摘要、阶段、操作 ID 和恢复点；不记录 token、私钥、完整邀请或 Tailcat 秘密地址。完成回滚演练前不清除旧状态；未完成操作不因回滚换 request-id 重做。共享 Headscale 从头到尾保持，不把其重启作为 Hyprial 回滚步骤。

最终报告分开给出：

1. `TSNET_CUTOVER_COMPLETE`：切换范围、实际版本、业务回读、离线节点处理、剩余 Headscale 依赖。
2. `HYPRIAL_HEADSCALE_INDEPENDENT`：全部遗留项关闭、干净环境及真实业务验收、观察结果、回滚/归档状态。
3. `SHARED_HEADSCALE_RETAINED`：团队服务仍保留，Hyprial 专属清理与共享资源边界已核对；不得将其存在描述为迁移失败。

当前仅完成计划文档，三项都没有运行时验收结论。执行前需要候选版本、范围和具体窗口的授权；这不阻止当前把工作包、测试矩阵、切换与回滚材料规划完整。

## 15. 备选调研：fork Headplane 管理多个 Headscale 是否更好

本节由当前 session 的独立 subagent 调研 Headplane 上游，再由维护者核对 Hyprial 依赖并汇总；没有使用 PAC、运行服务或更改部署。它是方案比较，不自动撤销第 14 节已选的迁移方向。

**结论：保留 tsnet/Headscale 通常更有利于短期少改客户端；但 fork Headplane 不是自动获得多租户平台的捷径。若选择保留 Headscale，优先比较“每实例原生 Headplane + 统一组织门户”，而不是立即维护多实例深度 fork。** Tailcat 更符合长期 daemon-only、Hyprial 不再依赖 Headscale 的目标，但会把设备目录、组级授权和迁移责任转到产品控制面；本轮证据不足以断言它总成本更低。

### 15.1 三个选择，不能把 K3s 当成必选项

| 维度 | A：多实例 Headplane fork + Headscale/tsnet | B：原生 Headplane 每实例一套 + 统一门户 | C：Tailcat + Casdoor 产品控制面 |
|---|---|---|---|
| 现有 Hyprial 接入复用 | 较多，保留入网/转发/WhoIs，但仍需组织目标选择和多组织语义 | 与 A 相同 | 较少，需第 14 节客户端、业务和状态迁移 |
| 现成网络管理功能 | 复用 Headscale 的节点/策略/网络管理和 Headplane 界面 | 同 A，少改上游 | 复用 Tailcat 加密/穿透/中继；其余由控制面及应用提供 |
| 自助建组、邀请和撤权 | 仍需产品流程、Casdoor 组同步、实例供应和撤权编排 | 同 A | 仍需产品流程、Casdoor 组同步、设备/连接授权编排 |
| 每组运行资源 | 每组 Headscale 配置/数据/入口，另有共享管理层 | 每组 Headscale 和 Headplane，另有门户/供应器 | 不需要每组网络控制服务；仍需可靠控制服务、存储和中继 |
| 上游维护负担 | 多实例数据和授权模型侵入 fork，需要持续合并安全/兼容更新 | 原生版本升级与供应模板维护；门户接口自行负责 | Tailcat 版本/API 风险及自建控制面维护，无 Headplane fork |
| 主要风险 | 实例/凭据/会话串租户、实例生命周期、客户端多网络边界 | 门户与各面板身份/权限一致性、实例数量和运维 | 真实设备绑定、目录和撤权正确性、硬切回归、网络实测 |
| 能否去掉 K3s | 可以选 Compose/systemd/其他编排；运维职责仍在 | 同 A | 不需要因“每组一个 Headscale”部署 K3s；控制服务的宿主仍需选择 |
| 是否实现 Hyprial 不依赖 Headscale | 否，这是保留 Headscale 的替代方向 | 否 | 可以作为目标，尚未完成真实接入验收 |

表中的成本方向是架构分析，不是性能/工期实测。没有测量三条路线的内存、节点容量、每组资源、故障率或维护工时，不给伪精确的人天/百分比结论。去掉 K3s 与换掉 tsnet/Headscale 是独立决策，不能捆绑制造二选一。

### 15.2 Headplane 原生能力与 fork 的实际改造面

上游 v0.7.1 固定提交 `30c842ed8dbf011e78596e3a474133ec4069ba79` 的配置/运行上下文指向一个 Headscale；用户表保存一个 `headscale_user_id` 和一份 role。它已有 OIDC、角色/能力检查、节点、预授权 key、ACL/DNS 管理，不能说成只有展示页面。但多实例需要新增实例目录与凭据隔离、每实例 API/配置/集成上下文、用户与多实例身份/角色映射、会话/缓存/操作的实例范围，以及跨租户负例。只在页面增加下拉框不足以完成这些改动。

Headplane 的已有容器/进程集成不能被当作“创建 Casdoor Group 就自动供应新 Headscale”的流水线；供应、入口证书、数据备份、销毁恢复仍需另一个执行层。Headplane 面板登录和 Headscale 节点 OIDC 准入是不同入口，必须分别限制到正确实例和组织。

固定源码依据：[运行上下文](https://github.com/tale/headplane/blob/30c842ed8dbf011e78596e3a474133ec4069ba79/app/server/context.ts)、[用户/会话数据模型](https://github.com/tale/headplane/blob/30c842ed8dbf011e78596e3a474133ec4069ba79/app/server/db/schema.ts)、[Docker 集成](https://github.com/tale/headplane/blob/30c842ed8dbf011e78596e3a474133ec4069ba79/app/server/config/integration/docker.ts)。这里是 SOURCE_VERIFIED 和改造分析，没有运行 fork。

### 15.3 无论哪条路线都绕不开的产品工作

- 用户属于多个 Casdoor Group，与一个 daemon 同时服务多个隔离组织，是两个验收项。当前 Hyprial 一 profile 一 home，边车 Server 配一个 ControlURL；仅换面板不会改变它。tsnet 库允许多 Server，不能误称库不支持多网络；产品仍要实现多实例状态、身份/组织路由和生命周期。[tsnet 多 Server 原生能力](https://pkg.go.dev/tailscale.com/tsnet#example-Server-MultipleInstances)；Hyprial 基线见 §13.6 S1/S3。
- Headscale OIDC `allowed_groups` 用于准入；OIDC groups 不自动变成 ACL policy groups。Casdoor 改组不能被当作已经让在线节点撤权，仍需调用网络管理能力并读回验证。[Headscale v0.29.3 OIDC 文档](https://github.com/juanfont/headscale/blob/v0.29.3/docs/ref/oidc.md)
- 注册登录、普通用户建组、管理员授权、已有账号一次性邀请、最后管理员、移交/解散、跨系统失败恢复均需产品层编排。已有 IAM/网络管理原语应复用，但页面存在不等于产品闭环已存在。
- 独立网络控制实例提供网络边界，不能自动证明跨组织的业务消息、文件和身份全部正确。Tailcat 路线需要组级执行，Headscale 路线也需要防止客户端把不同组织的通道错误混用。

### 15.4 决策建议与最小判别实验

如果当前优先级是**尽快提供可用功能、尽量保留现有客户端和网页/SSH 能力**，建议优先验证 B；若必须统一原生管理面板、且愿意承担多租户 fork 的持续维护，再评估 A。不要在尚未证明 B 不够用前，为实例切换功能重构整套 Headplane。

如果优先级保持为**只让 daemon 按组织互通，最终 Hyprial 不依赖 Headscale，并避免每组维护一个网络实例**，C 仍然更贴合目标；这是结构上的契合，不是“实验已证明生产更稳/总成本更低”。当前单机 Tailcat 实验还不能消除真实网络及产品迁移风险。

进一步改变路线前，最有区分度的是一个隔离的两实例对照：不 fork 先起两套原生 Headscale/Headplane，接一个测试 Casdoor，用同一账号加入两组；核对当前 Hyprial 的切换/同时在线、定向撤权保留另一组、WhoIs/网页、实例创建删除恢复，并记录操作数、资源和代码缺口。与 Tailcat 路线使用同一业务用例、真实跨机拓扑和结果标准比较。这个对照目前 **NOT_RUN**，本次未部署或派发该实验。

研究结果不自动授权回退选型、fork 实施或产品开发。原生多实例方案若足以满足实际需求，可以省掉深度 fork；若两套方案都需要大量产品控制面工作，不能把 Headplane 当作已完成那部分功能。

### 15.5 独立调研材料与未发布功能边界

完整[独立 subagent 报告](../evidence/tailcat-casdoor-2026-10-02/headplane-comparison/REPORT.md)固定 Headplane v0.7.1 `30c842ed…` 与 main `19767541439e077d145cfb1705a4ffc39bfb5620`。main 的结构化 Groups UI 编辑 Headscale ACL policy，不能当作 Casdoor Group/租户供应功能，也不能冒充已经发布。v0.7.1 源码已有单实例 owner transfer，不将它错误列为完全缺失；产品跨实例角色一致性仍需设计。

[维护者 Hyprial 基线](../evidence/tailcat-casdoor-2026-10-02/headplane-comparison/hyprial-baseline.md)与 §13 的依赖表配套。维护者另行回读 Headplane context/schema/Docker 关键路径，核实单后端和管理已有容器的边界；没有执行部署或真实对照。材料的哈希与范围见[证据索引](../evidence/tailcat-casdoor-2026-10-02/headplane-comparison/README.md)。


## 16. Tailnet 概念校正、按需 Web/SSH 实测与实现计划

### 16.1 Tailcat 本身没有创建一个受管理的 tailnet

此前“Tailcat 创建的 tailnet”用语容易造成误解，应改为 **Hyprial 用 Tailcat 通道组成的组织应用网络**。传统 Tailscale/Headscale 网络由控制面登记设备、分发可见对端和策略；“属于同一网络”不要求每个节点当前都在线或彼此都有连接，策略也可以限制互访。Tailcat 不替我们提供这一份组织成员/设备/服务目录。[Tailscale 控制面与数据面说明](https://tailscale.com/docs/concepts/tailnet-lock-whitepaper)

| 问题 | Tailscale/Headscale | 本实验固定 Tailcat |
|---|---|---|
| 谁决定设备属于哪个组织网络 | 控制面注册和授权，按网络策略分发视图 | Hyprial 的控制面必须决定；Tailcat本身只按已知对端与准入建立通道 |
| 是否只能两台 | 可以多节点 | 同样可以多节点：一个Server能接多个Client；每个Client配置一个Server目标 |
| B和C都连接S后，会自动知道彼此吗 | 依控制面视图和策略决定可见/可达 | 不会因此自动分发B↔C目录和权限；如需直连，要另外提供目标信息和授权 |
| 是否有中央全组织名册 | 有管理面；单客户端status也不等于无限制全网管理视图 | 无；Server.Status只是本服务端观察到的对端/引擎状态 |
| 共用同一个DERP是否同组 | 中继不定义组 | 同样不定义组，必须由产品授权决定 |

需要的组内拓扑可以是多客户端访问一个Hyprial server，也可以按需要建立多个端点间的连接。不是“每组仅允许两人”，也不是一份Tailcat地址自动变成全互联VPN。

### 16.2 这次真正查了什么状态

固定源码 `b4dc28e8…` 的CLI实际构建并运行：

- `tailcat-cli --help`：没有 `status` 子命令；`tailcat-cli status --help`显示根帮助。
- `tailcat-cli status`：**exit 1**，错误是 `argument "status" is neither a valid tailcat address nor a DNS name`。它把status当目标参数，不是在查询网络名册；单标签参数在DNS/客户端创建前即被拒绝。
- 调用Go **`Server.Status()`**：连接前peerCount=0；B/C同时请求后peerCount=2。这里实际有S、B、C三个活跃Tailcat端点；另一个在AllowClient中被允许、但从未连接的设备不在status中。
- 撤销B并调用DisconnectClient之后，状态里仍残留B的引擎记录。这与之前实验一致，**Status不能单独作为“是否还获授权/是否在线”的判据**。

前两轮做过的是库Status检查，不是宿主 `tailscale status`。本轮也没有调用生产tailscaled；它的status不会自动列出另一个进程里的Tailcat引擎。固定CLI源码有 `TAILCAT_STATUS_LOOP=1` 调试输出，但那仍是局部Server状态，不能代替产品组织目录。见[CLI证据](../evidence/tailcat-casdoor-2026-10-02/services/cli-status-result.json)、[运行结果](../evidence/tailcat-casdoor-2026-10-02/services/results.json)、[固定Status源码](https://github.com/tailscale/tailcat/blob/b4dc28e8aa8936f0a90a41ad8293a64e3d6b645f/tailcat.go#L2607)和[CLI命令树/调试输出](https://github.com/tailscale/tailcat/blob/b4dc28e8aa8936f0a90a41ad8293a64e3d6b645f/cmd/tailcat/tailcat.go)。

### 16.3 按需 Web/SSH：本轮实测

**最终候选通过一个定向集成测试：22条断言通过，另有2条观测记录。** 前几轮编译、沙箱和实验SSH适配错误保留；没有修改上游或产品代码来制造通过。真实同机Tailcat多引擎强制本地DERP，数据面是真的，组织成员和服务后端是LAB fixture。

| 用例 | 结果与范围 |
|---|---|
| PAC样式网页 | 本地loopback端口 → Tailcat → Go HTTP fixture，HTML 200；不是实际PAC overview页面 |
| 实时更新 | SSE收到3条更新；WebSocket双向内容一致，错误Origin拒绝 |
| 组织授权 | LAB PeerKey成员表：错误组403；B撤权后原HTTP请求403，原WS下一消息被1008关闭；C仍正常 |
| SSH远程命令 | 系统OpenSSH经临时本地转发和Tailcat到实验SSH服务，命令marker返回成功 |
| SSH负例 | 非授权用户key拒绝；错误host key拒绝；未禁用主机身份校验 |
| scp文件 | 1,310,720字节上传和下载都成功，哈希一致 |
| rsync目录 | 系统rsync经同一临时入口完成目录同步，内容一致 |
| 临时入口回收 | 关闭SSH转发后本地端口拒绝新连接，一次性key/数据目录删除，测试进程退出 |

最终run-5用例部分6.35秒，包报告7.002秒；不是吞吐基准。run-3为20/24记录通过，run-4为21/24，run-5为24/24（包含2条纯观测）。SSH fixture先修正OpenSSH私钥格式，再修正SFTP发送退出状态与rsync输入管道回收；原始失败、源码、结果和修正边界见[完整实验报告](../evidence/tailcat-casdoor-2026-10-02/services/EVIDENCE.md)及[证据索引](../evidence/tailcat-casdoor-2026-10-02/services/README.md)。

**未执行：** 跨主机/NAT/直连、真实PAC网页的浏览器渲染与登录、真实Casdoor组鉴权、原生CLI forward/browse数据面、Tailcat内嵌SSH handler、真实hyprial transfer或VM迁移、正在运行的SSH任务撤权取消、长时/高并发/故障恢复。测试使用真实Tailcat库与本地转发适配器、真实系统SSH/scp/rsync客户端和一次性Go SSH/SFTP服务，不启动或改变宿主sshd。

### 16.4 实现计划：公共服务目录和状态面

归入M1/M2/M3，依赖先于具体Web/SSH入口。以下是计划，不表示已有这些产品命令/API。

1. 定义组织服务记录：`orgId + serviceId + providerNodeId + kind(web|ssh) + serviceGeneration`，描述允许的服务而不是授权客户端任意指定远端host/port。真实地址/PSK只在获授权的连接建立流程中交付，不进普通列表、日志或命令行。
2. 校验Casdoor主体与组织角色，再绑定设备公钥的持有证明；目录、连接、具体资源访问都使用该组织范围。连接存在或共用中继不能替代权限。
3. 设计“组织成员/已登记设备”“服务发布与心跳”“此刻连接对端”三个独立视图，分别标注时间和过期状态。客户端拟提供组织网络状态查询，合并目录与局部Status观测，不把两者伪装成同一事实；CLI名称在M0确定。
4. 服务租期和代次控制旧入口失效；撤组、解绑、服务重启后应阻止旧授权恢复。记录请求/操作ID、拒绝原因、连接方式和回收结果，秘密留在受控存储/内存。

### 16.5 Web：先交付 PAC overview 的最小路径

```text
组织成员浏览器
  → 本机 Hyprial 管理的 127.0.0.1:<临时端口>
  → 经过组织授权的 Tailcat 通道
  → 该组织 hyprial server 的 PAC Web 服务
```

实现顺序：

1. 常驻server发布该组织的PAC overview服务，后端用已有业务状态汇总该组PAC；不能通过全局列表泄漏其他组。端口转发不会自动完成PAC聚合。
2. 用户选择组织/服务后，本机建立租期内的loopback监听，ready后自动打开浏览器；端口冲突重新分配并返回实际地址。浏览器不需要安装Tailcat插件，产品包装应隐藏连接信息和手动命令。
3. 在受信任的连接/会话边界传递用户、组织与设备身份，服务端继续鉴权；去掉依赖MagicDNS/WhoIs的身份推断，不信客户端伪造身份header。会话只用于目标组织与服务，按实际需要提供只读/管理权限。
4. 适配绝对链接、静态资源、登录回调、WebSocket/SSE重连、Origin/CSRF和cookie。**不同localhost端口不能作为Cookie隔离**：Cookie按host跨端口共享；必须在会话设计中显式隔离组织/服务，必要时采用不同本地主机名和可信入口，而不是复用同一个宽域cookie。[RFC6265](https://www.rfc-editor.org/rfc/rfc6265#section-8.5)
5. 关闭页面/租期到期/退出组织后回收监听及相关会话，定义多标签共享连接的计数；server离线显示断连/最后更新时间，不能把旧页面当实时数据。

放行证据：真实PAC页面加载、两用户两组权限、SSE/WS实时更新与断线恢复、登录回调、错误Origin/跨组cookie负例、server重启、临时端口释放。只有这些通过，才称“PAC overview已迁移”，本轮fixture只证明通道可行。

### 16.6 SSH：优先保留 transfer 协议，替换传输入口

首选方向是让现有SSH/scp/rsync通过Tailcat访问目标已有SSH服务；这样保留远程命令、文件和目录同步语义。目标没有sshd时，可评估内嵌SSH/SFTP作为第二后端，但需额外验证账号/工作目录/环境/命令权限，不能把本轮临时Go服务等同于内嵌handler已验收。

1. transfer准备阶段根据真实目标node/service解析服务，取得仅限本次迁移、目标组织、源设备的短期访问授权；核对目标OS账号、空间、工具、版本与可信SSH host key，确认可达后才暂停源agent。
2. 建立临时Tailcat转发，监听loopback随机端口；由目标设备身份绑定host key，以稳定HostKeyAlias识别目标，不用关闭StrictHostKeyChecking解决随机端口问题。临时SSH配置/known_hosts/用户key放在任务目录，禁止继承错误代理或意外使用SSH agent内的其他身份。
3. 为现有SSH、scp和rsync统一传递这一临时连接配置（例如各命令显式使用同一 `-F` 配置）；不能只改ssh命令而让scp/rsync仍走旧网络，也不写全局 `~/.ssh/config`。
4. 保留原transfer的plan→源端quiesce→状态/工作目录/镜像传输→目标receive与恢复确认→提交收尾。每阶段有operationId、内容哈希和结果回读；多个SSH连接可复用同一临时转发，不要求把整个迁移塞进单个TCP连接。
5. 超时/失败关闭入口与新授权，区分“目标尚未接管”和“目标已接管但收尾失败”；后者不能简单把源端也拉起。需要恢复的源状态与未完成操作保留，敏感暂存按完成状态清理。
6. 断开网络不等于取消已在目标运行的后台命令。任务取消/进程退出证明由transfer负责；授权过期时如何处理进行中的搬运、部分文件和恢复尝试，需要明确并测试。

放行证据：真实源/目标的受支持agent搬迁、内容/会话一致性、缺工具/错key/错目标/中途掉线/目标恢复失败、最终接管和源端停止、重复请求不双启动、任务后无端口/凭据残留。当前dev对某些agent-home迁移路径已有不支持的边界；换网络不能绕过原拒绝或宣布整个虚拟机内存热迁移已经实现。

### 16.7 与 Headscale 的关系及下一项实验

Tailcat组织服务网络可与团队Headscale并存；同一Casdoor可映射两边身份，但需要分别维护设备/权限。将来给某组织提供可选系统VPN不必改掉Hyprial基础通道。跨两边服务互访需要显式、带授权的网关/代理，不是把Tailcat自动登记成Headscale的tailnet。

下一项最小集成验证应分开记录两份结果：真实PAC Web服务的浏览器用例；真实transfer的两机受支持路径。先把本轮公共目录/会话契约落实，再在明确的隔离机器上验证直连和DERP、退出/撤权、重启及失败恢复。不重新启用K3s供应或修改共享Headscale；本轮只交付实验和计划，没有启动产品开发或生产切换。

## 17. B/C 的 Zenoh P2P 与 DERP 启动依赖

2026-10-02补充，依据固定Tailcat `b4dc28e8…` 与Hyprial `bccc754d…` 源码和Zenoh部署文档；**SOURCE_VERIFIED / 设计分析，未执行新的直连或DERP故障实验**。

### 17.1 必须分开三件事

1. **成员/服务发现**：谁属于该组、谁提供什么服务、能否把连接资料交给此用户。Headscale/Tailscale由控制面分发网络视图；Tailcat把这些资料交付留给上层。B、C都连接实验S，不会自动得到彼此的目录和权限。
2. **Zenoh逻辑P2P**：B的Zenoh session直接连接C的Zenoh session，而不经S转发业务。可在双方daemon提供Tailcat服务端和必要的客户端连接，再建立各自的loopback映射实现。一个节点可以同时扮演服务端和客户端，不能把实验中B/C作为纯Client的配置当成永久限制。
3. **物理网络直连**：B/C的实际加密包是否走直接UDP路径。即使Zenoh逻辑上B↔C，底下仍可能因NAT限制经DERP中继。逻辑P2P通过与UDP直连通过必须分别报告。

```text
控制/目录：B、C向Hyprial组织服务获取获授权节点和服务资料
业务路径：Zenoh B → B本地转发 → Tailcat B↔C → C的Zenoh listener
物理路径：可打通则B↔C UDP；否则B↔DERP↔C
```

这里S不是必须留在业务数据通路中的中心节点。上次S承载测试网页/SSH，因此B/C访问S；它既不是Headscale控制面，也不证明所有Hyprial流量必须经过一个中心。

Zenoh原生有multicast/gossip scouting，但发现到的locator必须对本机可达。把C的 `127.0.0.1:端口` gossip给B，B访问的是自己，不能拨到C。标准Zenoh TCP transport也不会仅凭一个Tailcat秘密地址自动建立Tailcat会话。当前Hyprial的配置默认关闭multicast/gossip，用EndpointDiscovery和本地转发入口；不要只打开gossip就宣称已完成替换。[Zenoh部署文档](https://zenoh.io/docs/getting-started/deployment/)、[固定Hyprial discovery](https://code.hyprial.com/HyprialOS/harness-bridge/src/commit/bccc754d5d5f96d0ad838006dd2e220825755222/src/hyprial/daemon/discovery.py)、[固定Zenoh配置](https://code.hyprial.com/HyprialOS/harness-bridge/src/commit/bccc754d5d5f96d0ad838006dd2e220825755222/src/hyprial/transport/zenoh.py#L512)。

因此计划采用：认证目录→设备/服务授权→Tailcat适配器建立本机可拨入口→交给Zenoh connect。也可研究经已认证Zenoh通道交换目录事件，但它不能绕过组权限、初次接入和连接适配。不能把带PSK的原始连接地址放进无范围的gossip。

### 17.2 DERP 不可用时的准确边界

| 状态 | 固定版本的行为/判断 | 证据等级 |
|---|---|---|
| 两端首次建立关系，所有相关DERP不可达 | 标准Client启动路径不能靠magicsock自行找到服务器；首次Meow/Meowed经DERP完成，对端UDP地址也经DERP交换 | 源码核查；不是本轮故障实测 |
| 已经建立并验证UDP直连，随后DERP断开 | 已知且仍有效的直连路径可以有条件继续承载数据；不承诺无限续航，尤其网络/NAT映射变化或进程重启后 | 数据面/源码推断，故障注入NOT_RUN |
| 连接原本只能走DERP | 中继不可达时没有已验证的直连替代，业务会受阻 | 架构分析，具体恢复时序NOT_RUN |
| 只是默认公共DERP不可达，自建DERP可达 | 可以选自建DERP；嵌入DERP信息可避免另取公共derp-map，但不会免除实际DERP连接 | 文档/配置能力，具体部署NOT_RUN |

`Client.up`首次调用`Ping`，经`SendDERPPacketTo`发送Meow并等服务器确认；服务器收到Meow后加入该client。`advertiseEndpoints`通过DERP发送CallMeMaybe，magicsock才有候选UDP地址可探测。连接资料ConnInfo含密钥与DERP信息，没有公开的“给Client塞一个服务器UDP地址、跳过首次登记”的标准入口。STUN只能帮助发现自身外部地址，不能代替对端登记与双方交换。[固定源码](https://github.com/tailscale/tailcat/blob/b4dc28e8aa8936f0a90a41ad8293a64e3d6b645f/tailcat.go)

**DERP在此不仅是打洞失败后的中继，还承担启动/地址交换。** 本轮所有运行实验都强制本地DERP，因此没有证明UDP直连，更没有证明“没有DERP也能从零启动”。这应作为M2的关键基础设施约束，而不是将Tailcat描述为完全无公共可达服务依赖。[固定README](https://github.com/tailscale/tailcat/blob/b4dc28e8aa8936f0a90a41ad8293a64e3d6b645f/README.md)

### 17.3 下一次验证的判据（计划）

在隔离两机/三节点环境先证明真实Zenoh B↔C通道，停止S的业务转发后仍能交换有回执的业务数据；控制/目录和数据通路分开记录。再验证DiscoPing/实际流量路径为direct，之后分别阻断DERP、改变网络、重启一个端点、以及全新节点冷启动。正向对照、路径读数、超时/恢复时间都必须保存；不能用在同一局域网的普通TCP旁路冒充Tailcat直连。

若要求“DERP彻底不可达时仍能从零互联”，它超出当前固定版本已验证标准路径，需要替代的引导/地址交换设计或上游改造，并重新评估成本。本次不执行该改造，不新增中继部署，不恢复生产开发。

## 18. 用 Tailcat 组成 tailnet-like 组织网络：Bash 示例与节点身份

### 18.1 不准确的说法：Tailcat 节点“没有 IP”

**有内部IP，但没有自动交给宿主操作系统使用的tailnet IP。** 固定版本的 `tcAddrForKey` 从节点公钥派生 `fd7a:115c:a1e0::/48` 范围内的IPv6地址，供该Tailcat用户态网络栈路由；`Server.Addr()`返回的是这类内部地址。它不自动创建宿主VPN接口、路由或MagicDNS。不能因为看到了这个IPv6，就让任意系统程序直接访问它；一般由Tailcat Dial或本地端口/SOCKS入口接入。

一个逻辑Hyprial设备可运行多个Tailcat引擎/密钥角色，因此不能把某个内部IP当作永久设备ID或Casdoor用户ID。密钥更换也会改变由其派生的地址。加密身份应使用**完整公钥及其私钥持有证明**，不是IP、设备昵称或客户端自己填写的用户名。[固定地址派生与PeerKey源码](https://github.com/tailscale/tailcat/blob/b4dc28e8aa8936f0a90a41ad8293a64e3d6b645f/tailcat.go)

### 18.2 谁来判断“这是哪个用户的哪台设备”

需要三层绑定：

```text
Casdoor 已验证的 (issuer, subject)
    ↓ 产品控制面验证组资格和设备持钥证明
Hyprial deviceId / nodeId + 组织/服务权限 + key代次
    ↓ 绑定允许的完整公钥（可以区分 server/client 等角色）
Tailcat WireGuard 加密对端
```

- **接收端识别调用者**：Tailcat先验证加密对端；服务端通过 `PeerKey(conn.RemoteAddr())` 取得完整公钥，或子进程通过由Tailcat注入的 `TAILCAT_PEER_KEY` 取得。服务端把这个可信key查回设备/账号/组织权限，不能信HTTP请求里自行声明的user header。
- **调用端识别目标**：从可信且有组织授权的目录取得目标设备的预期server key与连接资料；在内存中解析连接资料并匹配预期key/代次，再连接。公钥验证能证明持钥者，却不能单独证明它属于“Bob”；账号归属由经过认证的登记流程提供。
- **登记时的证明**：用户必须真实登录、有登记该组织设备的资格，并对挑战证明持有待登记key；服务端核验后才签发/保存绑定。下面脚本由同一测试操作员生成全部key并预置绑定，**没有模拟成功的Casdoor登录或完整登记PoP**。真正连接的持钥证明由Tailcat执行。
- **不要泄露连接能力**：完整Tailcat地址通常含PSK，不能当公开IP、设备ID或成员列表字段。密钥JSON里名叫 `Public` 的段也包含PSK；只输出特定公钥字段或用 `printpub`，不要整段公布。

### 18.3 最小组成：目录、准入、连接、状态分开

| 组成 | 最小职责 | Bash示例中的位置 |
|---|---|---|
| 用户/设备目录 | 同组成员看到允许的用户与设备元数据；多组时按明确org查询 | 可信本地 `directory.json` + `members` 函数，LAB替代品 |
| 设备key绑定 | 用户/设备对应完整公钥与代次 | 操作员生成并登记独立server/client key；不是产品自助登记 |
| 接收准入 | 只有获准设备key能连接服务 | 原生 `tailcat serve --allow ...` |
| 解析与连接 | 获授权后取得私密连接资料，按设备/服务建立通道 | `connect` 函数；原生Tailcat SOCKS + curl |
| 活跃状态 | 区分已登记、心跳、连接/探测结果 | 本例列表只写registered，访问回执单独记录；不伪造在线名册 |
| 成员变更/撤权 | 更新目录和运行中的准入，处理已有连接 | 本例未实现；产品用当前策略 + AllowClient/DisconnectClient等闭环 |

因此tailnet-like不是给每组创建一个网络进程：可以让每个daemon发布自己的服务，目录决定谁能看见和连接谁。A访问B时业务无需经目录服务器中转，也不需要知道B的内部IP；但首次Tailcat连接仍有第17节的DERP引导约束。

### 18.4 可运行 Bash 示例

完整脚本：[demo.sh](../evidence/tailcat-casdoor-2026-10-02/directory-demo/demo.sh.txt)、[peer-http.sh](../evidence/tailcat-casdoor-2026-10-02/directory-demo/peer-http.sh.txt)。文件用`.txt`保存，防止文档PR变成产品入口；复制到独立临时目录并恢复扩展名即可运行。需要bash、jq、curl及固定版本Tailcat binary。运行方式：

```bash
# 在新建的实验目录中，两个脚本均已恢复为 .sh
TAILCAT_BIN=/absolute/path/to/pinned-tailcat bash demo.sh
```

脚本只启动本机临时服务，用上游本地DERP测试模式；不访问Casdoor、Headscale、旧grant或生产网络，不写全局HOME配置。退出trap停止自己启动的进程并清除测试密钥。**该本地测试模式不能原样当公网部署配置**。

核心步骤如下，省略的准备、授权过滤和清理代码都在完整脚本中；以下变量/函数不是已发布的Hyprial接口：

```bash
# 1. 每台设备生成自己的出站key；入站server另用独立key。
# 路径是私有目录；genkey --client的stdout只有公钥。
tailcat genkey --client --key "$NODE_DIR/client.json" > "$NODE_DIR/client.pub"

# 2. 目录按组织返回公开元数据，不包含Tailcat地址/PSK。
jq --arg org testers \
  '[.[] | select(.org==$org) | {node,subject,org,state}]' "$DIRECTORY"

# 3. 节点B按目录生成的允许公钥列表接受连接。
# 地址和默认stderr都进入私有文件，不打印到成员列表。
TAILCAT_ADDR_FILE="$NODE_DIR/address" \
  tailcat --key "$NODE_DIR/server.json" serve --allow "$ALLOWED_PUBLIC_KEYS" \
  exec -- /bin/bash ./peer-http.sh B \
  > "$NODE_DIR/stdout" 2> "$NODE_DIR/private-server.log"

# 4. 完整demo中的connect先检查同组，再写0600 curl配置。
# 配置内含目标地址，并重写Host，防止后端日志记下秘密地址。
# SOCKS从请求中的tc主机名获取目标；地址不进入argv。
tailcat --key "$CALLER_DIR/client.json" socks /usr/bin/curl \
  --config "$PRIVATE_CURL_CONFIG"
```

同一逻辑节点的入站server和出站client使用不同key，目录将它们绑定到同一deviceId；不要让并发独立引擎用同一key竞争DERP登记。本例每个client只有一个目标，更多对端时还需要单独验证引擎复用/每目标key与生命周期，不能从本例推导任意规模并发。

这里不用 `tailcat forward "$(cat address)" ...`，因为那样会把含PSK的地址放进命令行。SOCKS+私有curl配置是为了让示例全程不在argv传秘密；真实产品可直接用库从内存/受控文件连接并建立loopback端口，不必让用户接触这些配置。

### 18.5 实测结果与准确边界

测试数据：A=`lab-user-A`、B=`lab-user-B`同属testers；C=`lab-user-C`属于other。它们是本地可信fixture，不是真实账号。

```text
A sees: A, B
B sees: A, B
C sees: C
PASS A -> B: server observed registered caller key
PASS B -> A: server observed registered caller key
DENY directory: different org
PASS A -> B: server observed registered caller key
```

上面可见列表是对[原始输出](../evidence/tailcat-casdoor-2026-10-02/directory-demo/run-2.log)的简化展示。**A↔B是实际原生Tailcat CLI进程的数据通道**，接收端回传的 `TAILCAT_PEER_KEY` 与已登记的调用方完整公钥一致；实验还断言接收端看到的内部地址属于上述IPv6范围。同机强制本地DERP，不是跨机/UDP直连或全互联压力测试。

C→B在目录函数中被拒绝，没有发起网络拨号；这是LAB目录过滤的负例，不冒称已实测服务端对C的网络拒绝。脚本中的同组server `--allow`是真实配置，但它是启动快照；改JSON不会热更新服务器，也不会自动撤销既有连接。完整多组、心跳、权限版本、并发/故障恢复和生产可信目录仍属于第14/16节实现计划。

普通沙箱第一次启动未就绪（exit1，私有启动日志按trap删除，没有保留精确底层错误）；获准同一隔离范围后第二次exit0。测试结束全部私有目录删除、受控子进程已wait收尾。见[结果与范围](../evidence/tailcat-casdoor-2026-10-02/directory-demo/results.json)、[复跑说明](../evidence/tailcat-casdoor-2026-10-02/directory-demo/README.md)和[附件哈希](../evidence/tailcat-casdoor-2026-10-02/directory-demo/artifact-index.json)。

### 18.6 产品最小落点

将本例的共享文件替换成真实认证后的组织目录服务：按 `(orgId, deviceId, serviceId)` 查询元数据和获准连接资料，结合设备挑战/登记、key代次与租期；接收端始终用加密对端key做身份映射。普通用户的可见列表可以展示用户名、设备名、服务与独立的在线/探测状态，无需分配一个宿主可路由IP。

多组设备必须按本次请求的明确组织/服务授权；不能因为两设备共享任意一组，就开放该设备的全部服务。上述目录、绑定和准入一旦产品化，即可提供用户理解的“加入组织后，组内设备相互可见并按权限互通”，但不能称为已经复刻了通用VPN的接口、路由和DNS管理。
