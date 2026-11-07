# `hyprial login` 设计定稿：用户身份来自 Casdoor，与 host 分离

> 状态：设计定稿候选；不含实现。
>
> 本文收编 PR #293 的 tsnet 登录草案，并接上 #352 已落地的单一 owner 落点。除第 7 节列出的 Allen 决策外，不再保留平行方案。
>
> **2026-09-13 增补：**[身份命令面设计稿](design-login-identity-surface.md) 承接本文。D-U5-1「login 不编排停启」**已被取代**；D-U2-5/U5 的人工 owner 入口裁定**部分已被取代**（新增显式 `login --local-usage --owner`，不恢复 `init --owner` / `squire setup --owner`）。本文 §3.2 的只提示引导、§5 的人工停启步骤保留作历史，目标行为以新稿 §2 为准；provider/profile/身份与网络分离等未点名裁定继续有效。新稿另列 whoami、dry-run 与接收方 tailnet 身份绑定的前置，未宣称这些功能已实现。
>
> 基线：开工前 `git ls-remote origin refs/heads/dev` = `7d9aa1cd163c4d542d7f4de0d0f40abf181021ab`。
>
> 证据写法：仓内代码用 `file:line`；HQ/Infrastructure 材料用仓名加 `file:line`；隔离实测用 Q 号；“Casdoor v3.144.0”源码均指精确 tag `https://github.com/casdoor/casdoor/tree/v3.144.0`；没有证据的一律写“未核”。

## 1. 身份模型

### 1.1 唯一主体

`hyprial` 的用户身份是 **Casdoor `auth.hyprial.com` 中 `hyprial` 组织的主体**。`hyprial` 直接信任的 issuer 是 `https://auth.hyprial.com`，不是 tsidp；tsidp 只是 Casdoor 的一个上游登录方法。这样，同一个 Casdoor 账号无论用密码还是已绑定的 tsidp 登录，都落到同一个 hyprial 用户。（证据：`Infrastructure/casdoor-config/README.md:3-20`；`Infrastructure/casdoor-config/skills/casdoor-ops/references/oidc-and-providers.md:3-18,31-44,77-88`）

本稿的可执行基线是：

```text
hyprial user identity = Casdoor preferred_username = Casdoor User.Name
settings.owner     = preferred_username
URI owner segment  = settings.owner
```

即选择 **username**，不选择 `sub`，也不选择 email；第 7 节 D1 请 Allen 最终裁定。Casdoor v3.144.0 的 UserInfo 把 `user.Id` 放进 `sub`，把 `user.Name` 放进 `preferred_username`；`profile` scope 才填后者。（证据：Casdoor v3.144.0 `object/user.go:250-256,1213-1247`；`object/token_jwt.go:54-63,541-545`）

⚠️ 2026-09-04 只读生产 discovery 的 `claims_supported` 没列 `preferred_username`，而 pinned 源码会在 `profile` scope 返回它；这是文档/实现表面不一致，不能仅凭 discovery 判缺失，也不能在未跑真实 UserInfo 前宣布可用。（证据：生产 discovery 配置项 `claims_supported`；Casdoor v3.144.0 `object/wellknown_oidc_discovery.go:156-178` 与 `object/user.go:1242-1247`）

选择 username 的理由：

1. #352 已把当前正确用户写成 `alicesmith` 并把 `settings.owner` 定为 login 的唯一落点；改用 opaque `sub` 会再次改写现有地址，而材料没有给出这样改语义的裁定。（证据：`hq/notes/spec-e2e009-user-identity-2026-09-03.md:1-13`；`src/hyprial/daemon/identity.py:143-163`）
2. Headscale + Casdoor 的隔离实测中，Headscale 自动以 Casdoor username 建 user，且节点按该 username 归属；这让身份面和网络归属面可以使用同一裸名。（证据：`hq/notes/tsnet-headscale-casdoor-2026-08-26.md` Q1、Q2(A)，尤其 `:9-15,48-72,103-116`）
3. email 不是主键：上游映射把 `id`、`username`、`email` 分成三个字段，且现有账号绑定后登录会落到既有 Casdoor 账号；因此上游 email 或上游 `sub` 都不能越过 Casdoor 账号成为 hyprial 身份。（证据：`Infrastructure/casdoor-config/docs/deployment-runbook.md:154-179`）

username 一旦被 hyprial 使用，应视为网络地址名；重命名是一次显式身份切换，不做旧名 alias。当前 Casdoor 是否已禁止普通用户自助改 username：**未核**，列入 D8。（证据：不写兼容/别名的既定原则见 `hq/notes/spec-e2e009-user-identity-2026-09-03.md:15-17,21`；username 可变性配置未核）

### 1.2 与 owner、host 和 tailnet 名的关系

规范 URI 仍是：

```text
agent:<owner>:<machine>:<actor>
adapter:<owner>:<machine>:<name>
```

- `<owner>` 是上述 Casdoor username；同一用户所有机器相同。（证据：`src/hyprial/daemon/identity.py:143-169`）
- `<machine>` 是 host/node 身份；同一用户的不同机器靠它区分，不得拿 owner 单独判断“哪台节点”。（证据：`src/hyprial/daemon/identity.py:162-169`；`src/hyprial/management.py:236-259`）
- 机器登录名（本机曾是 `bobadmin`）不是用户身份；它只可留在 host 侧的 `ownerKey` / `loginName` 等查找字段。（证据：`hq/notes/spec-e2e009-user-identity-2026-09-03.md:3-12`）
- tailnet 节点名也是 machine，不是用户。`studio-node-mac-01.example-tailnet.ts.net` 是本机旧名，`node-hq-01.example-tailnet.ts.net` 是改名后的本机名；不得把旧名误判成另一用户或另一台机器。（证据：`src/hyprial/daemon/owner_migration.py:16-37`；`user-identity-from-login-plan.md:18`）
- 多机同 owner 是目标形态，例如 `agent:alicesmith:host-a:squire` 与 `agent:alicesmith:host-b:squire`；二者是同一用户的两个 host 上各自的 actor，不冲突。（证据：`src/hyprial/daemon/identity.py:162-169`；#352 对同 owner / 不同 machine 的检查见 `src/hyprial/management.py:240-259`）

### 1.3 存储边界

运行时用户身份的**持久化**非机密真源只有 `~/.hyprial/settings.json` 的 `owner`；`HYPRIAL_OWNER` 仍是更高优先级的显式 override。daemon 不从 token、tsidp、tailnet 状态或 host 登录名临时猜 owner。（证据：`src/hyprial/daemon/identity.py:145-185`）

只存 owner 裸名；完整 URI 在注册/展示时由 owner + machine + actor/name 生成，不另存一份“login identity URI”。这避免 username/host 改动制造另一份永久迁移债。（证据：`store-the-name-not-the-derived-uri.md:15-29`）

OIDC token 只证明本次登录，不成为 daemon 的离线身份源；token 失效不应让已经写入 `settings.owner` 的 daemon 在离线时换身份或拒绝读取本地状态。（证据：#352 已把 daemon 来源封闭为 env > settings > error，`src/hyprial/daemon/identity.py:145-185`；token 的离线行为尚未实测，细化见第 2 节）

## 2. 登录流程

### 2.1 命令面与两个不同的“登录”

保留 #293 的命令形状：

```text
hyprial login --provider <tailscale|headscale> [--no-open] [--json]
```

> 2026-09-04 定稿(D-U2-3):#293 §1 的 `--provider` 已被 `profile.controlPlane.kind` 收编(§9.6)。

`--provider` 只选择 **network control plane**；它不选择用户 IdP。用户身份总是先向 Casdoor issuer `https://auth.hyprial.com` 登录，浏览器里的 Casdoor 页面可以再让用户选择 tsidp。CLI 不提供 `--idp tsidp`，也不直接持有 tsidp client/token。（证据：PR #293 `docs/design/hyprial-login-design.md` §1；Casdoor 双重角色见 `Infrastructure/casdoor-config/skills/casdoor-ops/references/oidc-and-providers.md:3-18,31-54`）

必须区分两个协议：

1. **Casdoor device authorization**：确认“这个 hyprial 用户是谁”，返回 OIDC access/refresh token；这是 owner 的来源。（证据：Casdoor v3.144.0 `controllers/auth.go:1510-1604`、`controllers/token.go:272-364`）
2. **tsnet 节点入网**：确认“这台 host 如何加入 overlay”。`headscale` 目标路径消费后端签发的 preauthkey；`tailscale` 验证路径使用 tsnet 的 BrowseToURL。它们都不得反向覆盖 Casdoor owner。（证据：`hq/notes/tsnet-research-brief-2026-08-25.md:81-90,129-152`；`hq/notes/evidence-tsnet-watchipnbus-2026-08-27.log:4-19`）

`--no-open` 只禁止自动拉起浏览器，仍打印 verification URI 与 user code。`--json` 输出状态、URL、user code、owner 和 network 状态，但绝不输出 device code、access token、refresh token 或 preauthkey。（证据：Casdoor 的返回结构含公开 `verification_uri/user_code` 与私密 `device_code`，v3.144.0 `object/token_oauth_util.go:129-135`；hyprial 的 secret/token 日志过滤边界见 `src/hyprial/log/__init__.py:34-46`）

### 2.2 Casdoor 身份阶段

顺序固定如下：

1. 从 `https://auth.hyprial.com/.well-known/openid-configuration` 读取 endpoints，并要求 issuer 精确等于 `https://auth.hyprial.com`。（证据：`Infrastructure/casdoor-config/skills/casdoor-ops/references/oidc-and-providers.md:7-14`；`Infrastructure/casdoor-config/skills/casdoor-ops/references/configuration.md:33-42`）
2. 以固定的 `app-hyprial` client ID 请求 device authorization，scope 为 `openid profile offline_access`；不请求 email，因为 owner 不取 email。（证据：生产 discovery 配置项 `device_authorization_endpoint=/api/device-auth`、`scopes_supported` 含三项；username 由 `profile` 填充见 Casdoor v3.144.0 `object/user.go:1242-1247`）
3. 自动打开或打印 Casdoor 返回的 verification URI 和 user code；客户端严格使用服务端返回的 `expires_in` 与 `interval`，不自造常量。（证据：Casdoor v3.144.0 `controllers/auth.go:1570-1603`、`object/wellknown_oidc_discovery.go:282-291`）
4. 用户在 Casdoor 页面登录。tailnet 用户点击 tsidp provider，但这只是 Casdoor 的上游；已绑定身份最终仍落在现有 Casdoor account。`app-hyprial` 不允许 JIT 注册。（证据：`Infrastructure/casdoor-config/docs/deployment-runbook.md:167-179`；`Infrastructure/casdoor-config/skills/casdoor-ops/references/oidc-and-providers.md:77-88`）
5. 客户端按 RFC 8628 状态轮询 token endpoint，处理 `authorization_pending`、拒绝、过期与成功；Casdoor 会把本次请求绑定到最初的 client ID。（证据：Casdoor v3.144.0 `controllers/token.go:272-350`）
6. 成功后先验签并检查 `iss`、`aud`、`exp`，再调用 UserInfo；要求 UserInfo `sub` 与 token `sub` 相同，并取非空 `preferred_username` 作为候选 owner。具体 JWT/OIDC 验证库尚未选，**未核**。（证据：Casdoor v3.144.0 `object/user.go:1213-1247`、`object/token_jwt.go:541-547`；客户端库未核）
7. 候选 owner 必须满足现有 owner grammar（非空、不得含 `:`）。若 `HYPRIAL_OWNER` 非空且不同，立即报“login identity 与 override 不一致”，不写 settings/credential。（证据：`src/hyprial/daemon/identity.py:172-185`）
8. access token 只留在本次进程内；refresh token 写 OS credential store；最后用现有 `write_settings_owner()` 写 `settings.owner`。跨 keychain/settings 没有事务：采用“先 credential、后 owner”的单向提交，任一步失败都报错，重跑同一 login 幂等，不造恢复状态机。（证据：`src/hyprial/daemon/identity.py:188-231`；credential store 具体实现未核）

重复登录同一 username 只更新 credential 并幂等写 owner。不同 username 走第 5 节的显式切换，不允许一个 HYPRIAL_HOME 同时持有两个活动 owner。（证据：`write_settings_owner` 的覆盖语义见 `src/hyprial/daemon/identity.py:206-231`；唯一身份裁定见 `hq/notes/spec-e2e009-user-identity-2026-09-03.md:1-13`）

### 2.3 network 阶段

**`--provider headscale`（两条路径，⛔ 一条）**

**(a) 交互式（已在代码里）**：`--control-plane headscale --url <自建 headscale> --join interactive`（`--join` 的默认值）⇒
hyprial 把 `controlUrl`（= `profile.control_plane_url`，⛔ `""`）原样放进侧车的 `up` 请求，由侧车驱动 tsnet 的交互登录，
再由 headscale 自己（已配 OIDC，例如自建 Casdoor）给出登录 URL。**⛔ 需要 hyprial 内部的 enrollment service**：
登录这一跳由 headscale 的 OIDC 承担，⛔ 由 hyprial 代签 preauthkey。（证据：`src/hyprial/tsnet_join.py:603-613`
的 `up` 载荷含 `"controlUrl": outcome.control_url`；`:258-261` 的 `_control_url` 对非 `tailscale` 返回
`profile.control_plane_url`；协议说明 `:27-29` 逐字「`controlUrl` … for headscale, **passed through verbatim**」；
`tests/test_tsnet_join.py::test_t3_up_payload_is_exact_for_every_kind_and_join` 逐 `kind × join` 断言整条 `up` 载荷、
`::test_t5_headscale_control_url_flows_through` 断言该 URL 流到 `state/tsnet/node.json` 的 `controlUrl`；
`src/hyprial/cli.py` 的 `--join` 默认 `interactive` 且 help 逐字「interactive or preauthkey (unattended; **either kind**)」）

**(b) 自动签发（产品目标，未实现）**：hyprial 把短期 Casdoor access token 发给一个受信 enrollment service；该服务验证 Casdoor token，把 Casdoor username 映射为 Headscale user，通过 Headscale REST API 签一次性、短期 preauthkey，客户端只在内存里交给 `tsnet.Server.AuthKey`。Headscale API 的“建 user + 签 key”以及不同 user 的节点隔离已实测；enrollment service 本身、URL、audience、响应协议均不存在，记为 **未核/前置交付**，不得在客户端内置 Headscale API key。（证据：`hq/notes/tsnet-headscale-casdoor-2026-08-26.md` Q1、Q2(B)，`:32-74,120-127`；服务不存在见 `hq/notes/tsnet-poc1-real-sidecar-2026-08-26.md` §B.b）

获得 key 后，tsnet 写自己的 node state；命令等到节点 `Running` 再报告 network success。preauthkey 不落盘，后续重启复用 tsnet state 而不是重新用 key。（证据：`hq/notes/tsnet-research-brief-2026-08-25.md:83-90,131-142`；持久/ephemeral 节点选择见 D10）

**`--provider tailscale`（验证/后备）**：Casdoor 身份阶段完成后，启动 tsnet、从 `LocalClient().WatchIPNBus()` 读取 `Notify.BrowseToURL`，打印 URL，等待 `NeedsLogin → Starting → Running`。同一 bus 推出完整迁移已实测，但触发授权用的是管理员 CLI 注册，OIDC 点击路径仍 **未核**。（证据：`hq/notes/evidence-tsnet-watchipnbus-2026-08-27.log:4-19`；PR #293 §4 的作用域说明）

官方 Tailscale 这条路径不会决定 owner，也不能作为最终自动签发方案：官方 OAuth key 必须带 tag、不能表达个人归属；它的价值是遵守 #293 的“先官方验证 tsnet，再上自部署 Headscale”顺序。（证据：`hq/notes/tsnet-headscale-casdoor-2026-08-26.md:3-15`；PR #293 §6）

身份阶段成功、network 阶段失败时，保留已确认的 Casdoor owner/credential，并把结果明确拆成 `identity.status=authenticated` 与 `network.status=failed`；不得把“用户已经登录”回滚成未登录。重跑同一命令只重试 network。（证据：身份与 node 是两个对象的既定区分见 `hq/notes/spec-e2e009-user-identity-2026-09-03.md:1-12`；失败后的重试行为未实测）

### 2.4 token 落点与离线 daemon

| 数据 | 落点 | daemon 离线是否读取 |
| --- | --- | --- |
| `settings.owner` | `~/.hyprial/settings.json` | **是**，这是非机密身份落点 |
| refresh token | OS credential store，按 issuer + client 固定 key | **否** |
| access/ID token | `hyprial login` / enrollment 进程内存 | **否** |
| device code / preauthkey | 当前命令内存，成功或失败后丢弃 | **否** |
| tsnet node key/state | tsnet 自己的专用 state dir | network 组件读取，不参与 owner 解析 |

因此，Casdoor 不可达、refresh 过期或机器离线时，daemon 仍用 `HYPRIAL_OWNER > settings.owner > error` 启动并保持同一身份；它不尝试在线 introspection，不从过期 token 猜 owner。只有需要 enrollment/账号操作时才刷新 token；刷新失败要求用户重跑 `hyprial login`。（证据：`src/hyprial/daemon/identity.py:145-185`；tsnet state 重用见 `hq/notes/tsnet-research-brief-2026-08-25.md:83-90,131-142`）

这意味着 Casdoor 禁用/撤销不会即时让一个离线 daemon 停止：这是 offline-first 取舍，不是已核安全能力；是否以后加在线会话撤销属于后续安全设计，本文不做。（证据：当前 daemon 完全不读 token，`src/hyprial/daemon/identity.py:138-185`；撤销要求未核）

## 3. 与现有落点的关系

### 3.1 `settings.owner` 与 `HYPRIAL_OWNER`

#352 已经完成本设计所需落点：`resolve_node_owner()` 的顺序是 `HYPRIAL_OWNER > settings.json owner > loud failure`，没有 host-login fallback；login 不新增第二个 owner 文件，只调用现有 `write_settings_owner()`。（证据：`src/hyprial/daemon/identity.py:138-185,188-231`）

`HYPRIAL_OWNER` 优先级保持不变。它只定位为测试/受管部署 override；交互式 login 遇到不同值必须在提交前报错。相同值时仍写 settings，使移除 override 后身份不变。（证据：#352 定序见 `hq/notes/spec-e2e009-user-identity-2026-09-03.md:7-13`；login 冲突行为为本设计，尚未实测）

### 3.2 `hyprial init --owner` 的去留

当前 `hyprial init --owner` 会在 daemon 启动前调用 `write_settings_owner()`，是 login 尚未实现时的人工引导路径。（证据：`src/hyprial/cli.py:3027-3079`）

`hyprial login` 发布的同一切换版本中：

- 删除公共 `hyprial init --owner`；`hyprial init` 只初始化/启动，缺 owner 时提示先运行 `hyprial login --provider ...`。
- 不保留 hidden alias、deprecated alias 或从 host 猜值的兼容分支。
- 自动化继续可显式设置 `HYPRIAL_OWNER`；这不改变既定 source order。（证据：用户身份必须来自 login 的裁定见 `hq/notes/spec-e2e009-user-identity-2026-09-03.md:1-8`；不写兼容见 `no-defensive-code-before-release.md:8-15`）

### 3.3 `hyprial squire setup --owner` 的去留

当前 CLI 要求 `--owner` 并把它传入 `SetupIdentity`；实际代码 **没有** 调用 `write_settings_owner()`。因此 `identity.py:146-147` 所写“squire setup puts it there”与现代码不一致，实施 login 时必须一起校正文档/测试，不能把这句当事实。（证据：`src/hyprial/cli.py:7002-7008,7070-7078`；全仓生产调用 `write_settings_owner` 只有 `src/hyprial/cli.py:3074-3077`）

切换后删除 `squire setup --owner`，由命令读取 `resolve_node_owner()` 并传给 `SetupIdentity`。`--owner-key`、`--login-name`、`--machine`、`--machine-key` 继续描述 host 侧 profile/receiver 查找，不替代 owner；`ensure_squire` 继续同时比较 owner 与 machine。（证据：`hq/notes/spec-e2e009-user-identity-2026-09-03.md:10-13`；`src/hyprial/management.py:236-259`）

> **2026-09-18 增补**：这四个参数已改为**可选覆盖**，缺省时推导 —— `machine`
> 取 `HYPRIAL_NODE_ID`（未设则 hostname），`owner-key`/`machine-key` 取 owner/
machine 的 slug，`login-name` 取平台（OS）登录名；显式传入且与 node id 不符的
> `--machine` 仍报错。见 `feat/squire-autoderive-params`（Allen 2026-09-18 任务）。

### 3.4 与同族在飞工作的边界

mcpfix（PR #362）把持久会话身份收成 actor 裸名、每次由 daemon 铸完整 URI；本设计同样只把 owner 裸名写 settings，不会要求 MCP 保存 Casdoor token 或完整 identity URI。（证据：`origin/fix/mcp-actor-by-name` 的 `src/hyprial/cli.py` `_interactive_actor_name` 及其“never persist”注释；`store-the-name-not-the-derived-uri.md:15-29`）

h2b-developer installer 的命令挂载改动只触及 app manifest/动态 CLI command mount，不占用 `login` 身份落点或 token store；合并时唯一需做的是确认动态命令冲突集合仍把内置 `login` 视为保留名。（证据：`origin/feat/installer-mount-commands` 的 `src/hyprial/cli.py:7344-7487`；该分支尚未进 dev，最终行号待合并后复核）

## 4. `auth.hyprial.com` 侧配置

### 4.1 对接边界：Casdoor，不直连 tsidp

hyprial 注册成 Casdoor relying party/client：issuer、device endpoint、token endpoint、UserInfo 和 JWKS 全部来自 `auth.hyprial.com`。tsidp 保持 Casdoor 的 Custom OAuth upstream；它的 client secret 和 tailnet-only endpoints 不下发到 hyprial。（证据：`Infrastructure/casdoor-config/README.md:8-18`；`Infrastructure/casdoor-config/docs/deployment-runbook.md:135-170`）

原因不是多绕一跳，而是 Casdoor 才拥有主用户数据库、账号绑定和每应用注册策略。直连 tsidp 会把“tailnet 身份”重新抬成 hyprial 主身份，绕过 Casdoor 账号；也无法覆盖未来其它 Casdoor 登录方法。（证据：`Infrastructure/casdoor-config/README.md:16-20`；`Infrastructure/casdoor-config/skills/casdoor-ops/references/oidc-and-providers.md:3-18,69-88`）

### 4.2 新应用 `app-hyprial`

按 D3 倾向，在 `hyprial` 组织新增专用应用，而不改/复用 `app-hyprial`：

| 配置项 | 值 |
| --- | --- |
| name / organization | `app-hyprial` / `hyprial` |
| `enableSignUp` | `false` |
| `disableSignin` | `false` |
| signin method | 启用 `Device login` |
| grant types | `urn:ietf:params:oauth:grant-type:device_code`、`refresh_token` |
| token format | `JWT-Standard` |
| token fields | 至少允许 `Name`，使 `profile` UserInfo 返回 `preferred_username` |
| providers | `tsidp`: Can sign in=true、Can sign up=false、Can unlink=false；是否保留 Casdoor password 见 D9 |
| redirect URI | device flow 不使用 callback；不登记宽泛 wildcard |
| client secret | 服务端可生成，但不得编进/下发给 CLI；device flow 只使用公开 client ID |
| scopes | `openid profile offline_access`；不请求 email |

Device endpoint 明确要求应用启用 `Device login`；device token 路径按原请求 client ID 绑定，且 mint token 时不校验分发到客户端的 secret。refresh 路径允许空 client secret，但仍校验 refresh token。（证据：Casdoor v3.144.0 `controllers/auth.go:1518-1547,1577-1603`、`controllers/token.go:326-350`、`object/token_oauth.go:74-102`、`object/token_oauth_util.go:393-429`）

`JWT-Standard` 暴露标准 `preferred_username`；无论 token format，UserInfo 的 `sub=user.Id`，`profile` 下 `preferred_username=user.Name`。客户端以 UserInfo 为 claim 真相，不能依赖当前 JWT 默认格式里恰好出现的非标准 `name` 字段。（证据：Casdoor v3.144.0 `object/token_jwt.go:54-63,532-548`、`object/user.go:1213-1247`）

### 4.3 注册策略与 tsidp

`app-hyprial.enableSignUp=false` 是硬要求；provider 的 Can sign up 也设 false。结果是：已有 Casdoor `hyprial` 用户可通过已绑定 tsidp 登录，新的 tailnet identity 不会因为运行 hyprial login 自动成为 Hyprial 用户；新增用户必须走管理员明确 enrollment。（证据：`Infrastructure/casdoor-config/skills/casdoor-ops/references/oidc-and-providers.md:23-29,77-88`；现有应用清单 `Infrastructure/casdoor-config/docs/organizations-and-applications.md:22-28`）

沿用现有 tsidp 映射：`id -> sub`、`username -> username`、`displayName -> username`、`email -> email`。这份映射只负责把上游身份关联到 Casdoor 用户；hyprial 读取的是关联后的 Casdoor `preferred_username`。（证据：`Infrastructure/casdoor-config/docs/deployment-runbook.md:154-165`）

### 4.4 配置落点和运维前置

Casdoor 应用/组织/provider 对象在 PostgreSQL，不由本仓文件驱动；Infrastructure 变更必须通过管理 API/UI落库，并同步 `docs/organizations-and-applications.md`，同时纳入 PostgreSQL dump + `casdoor -export` 备份。本设计单不修改 production 或 `casdoor-config`。（证据：`Infrastructure/casdoor-config/docs/organizations-and-applications.md:1-9`）

生产 `redisEndpoint` 当前为空，device auth state 在 Casdoor 单进程内存中；若未来扩成多 replica，先配 Redis，否则轮询可能命中另一副本。当前部署是单 Casdoor 容器，进行中流程仍可能被重启打断。（证据：`Infrastructure/casdoor-config/conf/app.conf:14-18`；Casdoor v3.144.0 `object/token_oauth_util.go:41-53`、`object/device_auth_store.go:43-59`）

应用创建后必须用 **真实 Casdoor v3.144.0** 做 device + tsidp 浏览器流、refresh、UserInfo claim 的隔离验证；Q2(A) 只证明 Casdoor→Headscale 授权码链路，未测真实浏览器 JS、真实社交/tsidp device flow 或故障恢复。（证据：`hq/notes/tsnet-headscale-casdoor-2026-08-26.md` Q2(A) 及未测清单 `:151-159`）

## 5. 迁移与切换

### 5.1 已是正确 owner 的节点

现有节点若 `settings.owner` 已是 Casdoor username（本机目标为 `alicesmith`），login 成功后只更新 credential 并幂等重写同值；URI 不变化、#352 migration 第二次为 0。（证据：`src/hyprial/daemon/identity.py:196-231`；`src/hyprial/daemon/owner_migration.py:527-558`）

这也是从“手写 owner”到 login 的主路径：先用同一 Casdoor 账号登录，确认返回 username 与当前 owner 完全相同，再移除日常使用 `init --owner`/`squire setup --owner` 的入口。没有额外 data migration。（证据：当前人工落点见 `src/hyprial/cli.py:3027-3079`；唯一来源目标见 `hq/notes/spec-e2e009-user-identity-2026-09-03.md:7-13`）

### 5.2 owner 不同的节点

不同值不是兼容场景，而是一次账号切换：

1. 完成 Casdoor 认证并显示 `old -> new`；交互确认/显式 `--switch-account`（flag 名待 D6 裁定）。
2. daemon 停止后写 credential，再写 `settings.owner=new`。
3. 启动 daemon；现有 #352 migration 在任何 store 构造前检测旧 owner 并做一次锚定改写。
4. daemon 以新 URI 重新宣告；检查本机相关 DLQ/投递失败。
5. 不提供旧 owner alias、双写、旧版本哨兵或自动回滚。（证据：migration 启动顺序见 `src/hyprial/daemon/application.py:579-606`；幂等规则见 `src/hyprial/daemon/owner_migration.py:485-558`；不兼容裁定见 `no-defensive-code-before-release.md:8-15`）

migration 只改 owner 语义，不改 `/Users/<host-login>` 路径、`ownerKey/loginName` 或本机历史 machine 名；不得为了 login 再写第二套宽泛 replace。（证据：`src/hyprial/daemon/owner_migration.py:9-37,49-69`；`hq/notes/spec-e2e009-user-identity-2026-09-03.md:21-24`）

### 5.3 多机切换

每台机器分别运行一次 login，同一 Casdoor account 得到同一 owner；每台机器只迁自己的本地活状态并以各自 machine 段重新宣告。对端历史 inbox/PAC 文本不改；活 targets/pins 通过重新宣告或既有迁移跟上，不设 alias 期。（证据：同用户多机语义见 `src/hyprial/daemon/identity.py:158-169`；历史/活数据边界见 `hq/notes/spec-e2e009-user-identity-2026-09-03.md:21,31-40`）

`studio-node-mac-01.example-tailnet.ts.net` 是本机旧名，不是等待迁移的第二台机器；当前真实对端 owner 是其他用户。切换清单不得据旧记录虚构“现有跨机同 owner”。（证据：`src/hyprial/daemon/owner_migration.py:31-37`；`user-identity-from-login-plan.md:18`）

### 5.4 发布按钮

发布顺序是一个动作：部署含 login 的版本和 `app-hyprial` → 用户 login → 必要时停 daemon/写 owner → daemon 启动迁移 → 重新宣告 → DLQ 观察。若非空 `HYPRIAL_OWNER` 与 Casdoor username 不同，先由操作者明确修改/移除 override，login 不偷偷越过它。（证据：既有生产切换顺序见 `hq/notes/spec-e2e009-user-identity-2026-09-03.md:29-32`；优先级见 `src/hyprial/daemon/identity.py:172-185`）

`--provider headscale` 的可用性按两条路径分栏：**(a) 交互式** ⇒ **可用**（代码已支持，见 §2.3(a)；仍须 headscale 侧已建 user 且已配 OIDC）；
**(b) 自动签发** ⇒ **enrollment service 未实现时不得宣称可用**（见 §2.3(b)）。身份 login 与 network 结果必须分栏，避免“owner 已落盘”被误报成“节点已入网”。（证据：缺失项见 `hq/notes/tsnet-poc1-real-sidecar-2026-08-26.md` §B.b；分阶段为本设计，未实测）

## 6. 不做项

1. **本设计单不写实现。** 不改 `identity.py`、`cli.py`、生产 `~/.hyprial` 或 `Infrastructure/casdoor-config`。（证据：本单 handoff 禁令）
2. 不把 tsidp 变成 hyprial 的直接 IdP，不保存/刷新 tsidp token；它只作为 Casdoor upstream。（证据：`Infrastructure/casdoor-config/README.md:8-18`）
3. 不开放注册；`hyprial login` 只登录现有 Casdoor Hyprial 用户。（证据：`Infrastructure/casdoor-config/docs/deployment-runbook.md:123-133,167-170`）
4. 不从 email、host 登录名、tailnet machine 名或 tsnet WhoIs 猜 owner。（证据：`src/hyprial/daemon/identity.py:145-169`；D1 若 Allen 改选 `sub`，只替换 username 这一项）
5. 不新增 identity 文件、派生 URI 副本、ownerKey 联结身份或多身份 profile。（证据：`store-the-name-not-the-derived-uri.md:15-37`；`user-identity-from-login-plan.md:8-16`）
6. 不写 alias/双写/旧版兼容/回退哨兵；不同 owner 直接走一次 #352 migration。（证据：`no-defensive-code-before-release.md:8-15`）
7. 不在客户端内置 Headscale API key，不在本单部署 Headscale、enrollment service、DERP/ACL 或 Casdoor Redis。（证据：Headscale REST root key 的引导边界见 Q2(B)，`hq/notes/tsnet-headscale-casdoor-2026-08-26.md:120-127`）
8. 不把“登录”变成免人工确认；每台新 host 一次浏览器身份确认是特性。（证据：PR #293 §5；Q2(A) `hq/notes/tsnet-headscale-casdoor-2026-08-26.md:110-116`）
9. 不用 login token 改造消息级认证/ACL；当前 envelope/tailnet 信任模型另案处理。（证据：`docs/design/design.md:33`）
10. 不改 mcpfix 的“持久化裸 actor 名”或 installer 的 mount command 语义；只在合并后核 `login` 名称冲突。（证据：第 3.4 节）

## 7. 待 Allen 裁的决策清单

下列每项都给出本稿倾向；Allen 只需逐项选“同意/改为……”。

### D1. owner 取 `preferred_username` 还是 `sub`

- **倾向：`preferred_username`（Casdoor `User.Name`）。** 它延续 #352 的 `alicesmith` owner，并与 Headscale Q2(A) 实际创建的 username 一致。（证据：`src/hyprial/daemon/identity.py:143-163`；`hq/notes/tsnet-headscale-casdoor-2026-08-26.md` Q2(A)）
- `sub` 的优势是 opaque/stable；Casdoor v3.144.0 确实把 `user.Id` 放进 `sub`。（证据：Casdoor v3.144.0 `object/user.go:1213-1220`；`object/token_jwt.go:541-545`）
- email 不选：它是独立映射字段且可能变化；没有材料证明它是不可变唯一键。（证据：`Infrastructure/casdoor-config/docs/deployment-runbook.md:160-165`；不可变性未核）

### D2. CLI 默认走 device authorization 还是 authorization code + PKCE

- **倾向：device authorization 为唯一 CLI 流程，自动打开验证页；无 GUI/远程 shell 只打印 URL + 短码。** 它不需要本地回调端口，也符合“浏览器确认一次”的产品定位；`--no-open` 是相对 #293 新增的建议选项。（证据：Casdoor v3.144.0 discovery 配置项 `device_authorization_endpoint=/api/device-auth`、`grant_types_supported` 含 device_code；Casdoor v3.144.0 `controllers/auth.go:1510-1604`）
- 风险：生产 `redisEndpoint` 当前为空，Casdoor v3.144.0 的 device 状态因而在单进程内存中；未配置应用超时时默认两分钟，Casdoor 重启会中断进行中流程。现部署单 Casdoor replica，是否接受该限制：**未核**。（证据：`Infrastructure/casdoor-config/conf/app.conf:14-18`；Casdoor v3.144.0 `controllers/auth.go:1570-1575`、`object/token_oauth_util.go:41-53`、`object/device_auth_store.go:43-59`）
- authorization code + PKCE 作为替代需要确定 loopback redirect 的端口策略；Casdoor 要求 redirect URI 逐字节匹配，当前没有已核的动态 loopback 规则。（证据：`Infrastructure/casdoor-config/skills/casdoor-ops/references/oidc-and-providers.md:16-20`；动态端口支持未核）

### D3. 新建 `app-hyprial` 还是复用 `app-hyprial`

- **倾向：在 `hyprial` 组织新建 `app-hyprial`。** 每个 relying party 应有独立 client、redirect/grant/token/provider 策略；`app-hyprial` 是组织默认应用，不应承载 CLI 专属 device-login 配置。（证据：`Infrastructure/casdoor-config/skills/casdoor-ops/references/oidc-and-providers.md:16-29`；`Infrastructure/casdoor-config/docs/deployment-runbook.md:123-133`）
- `app-hyprial.enableSignUp=false`，只让既有 Hyprial 账号登录；不借 hyprial 开注册口。（证据：`Infrastructure/casdoor-config/README.md:16-20`；`Infrastructure/casdoor-config/docs/deployment-runbook.md:123-133,167-170`）

### D4. `hyprial login --provider` 的含义与默认值

- **倾向：保留 #293 已定命令面 `--provider <tailscale|headscale>`，但明确它是 network control-plane provider，不是 identity provider；身份 issuer 永远是 Casdoor。默认值在 Headscale 生产 endpoint/换 key 服务交付前不设，缺参即要求显式选择。**（证据：PR #293 `docs/design/hyprial-login-design.md` §1；Casdoor/tsidp 角色见 `Infrastructure/casdoor-config/README.md:8-18`）
- 产品目标走 `headscale`；`tailscale` 只保留“先官方验证”的验证路径，因为官方 OAuth auth key 无法按个人区分且必须带 tag。（证据：`hq/notes/tsnet-headscale-casdoor-2026-08-26.md:3-15` Q1-Q3；PR #293 §6）

### D5. refresh token 的系统落点

- **倾向：OS credential store；`settings.json` 只存非机密 owner，access token 只驻进程内存。** macOS Keychain、Windows Credential Manager、Linux Secret Service 的统一库与无 keyring 环境报错形态：**未核**。
- 不接受明文 `~/.hyprial/*.json` 作为正式落点，也不把 token 交给 daemon 作为身份源。（证据：hyprial 日志已有 token/authorization 脱敏边界，`src/hyprial/log/__init__.py:34-46`；具体 credential-store 方案未核）

### D6. 登录成与现有 owner 不同的账号时是否允许切换

- **倾向：允许，但必须是一次显式切换：停 daemon → 写新 owner → 复用 #352 启动迁移 → 重启/重新宣告；不设旧 owner alias，不做双身份并存。** 当前 `write_settings_owner` 已明确允许覆盖不同 owner。（证据：`src/hyprial/daemon/identity.py:194-231`；`src/hyprial/daemon/application.py:579-601`；`src/hyprial/daemon/owner_migration.py:527-558`）
- 是否在 CLI 上要求额外 `--switch-account`/确认：**倾向要求交互确认，`--json` 下不同 owner 直接报需显式 flag**；该 UX 未有 Allen 裁定，未核。

### D7. `HYPRIAL_OWNER` 在 login 后是否继续高于 settings

- **倾向：保持 #352 已定优先级，不重开；把它定义为测试/受管部署 override。** login 得到的 username 与非空 `HYPRIAL_OWNER` 不同则在写任何本地状态前报错，因为写 settings 也不会改变 daemon 实际身份。（证据：`hq/notes/spec-e2e009-user-identity-2026-09-03.md:7-13`；`src/hyprial/daemon/identity.py:145-185`）

### D8. Casdoor username 重命名策略

- **倾向：hyprial 上线前把已用于 owner 的 Casdoor username 视为不可自助改名；管理员确需改名时走 D6 同一切换，不留 alias。**（证据：owner 进入网络地址，`src/hyprial/daemon/identity.py:143-169`；不做兼容，`no-defensive-code-before-release.md:8-15`）
- Casdoor 当前是否能按应用/组织禁止 username 修改、现有 `hyprial` 用户是否有改名权限：**未核**。

### D9. `app-hyprial` 显示哪些登录方法

- **倾向：首版只显示 tsidp；Casdoor 仍是 issuer/account authority，但不让 hyprial 入口使用当前 `hyprial` 组织的 plain-password 存储。**（证据：tsidp 是 Casdoor upstream，`Infrastructure/casdoor-config/README.md:8-18`；`hyprial.passwordType=plain` 见 `Infrastructure/casdoor-config/docs/organizations-and-applications.md:13-20`）
- 代价：当前 `app-hyprial` 明确提供 password + tsidp，改成 tsidp-only 可能排除没有上游绑定的既有账号；这批账号数量/绑定覆盖率 **未核**。（证据：`Infrastructure/casdoor-config/docs/deployment-runbook.md:123-131`）

### D10. Headscale 节点是否 ephemeral

- **倾向：preauthkey 一次性、短期，但用户 host 节点非 ephemeral；actor 级临时节点以后另案。** hyprial 需要同一 tsnet state 重启后保持 node identity，PoC 已证明同 Dir 可保持身份。（证据：`hq/notes/tsnet-research-brief-2026-08-25.md:83-90,131-142`；`hq/notes/tsnet-poc1-real-sidecar-2026-08-26.md` A 节“重启后节点身份”）
- Q1/Q3 使用 ephemeral key/节点只证明隔离机制，不足以裁生产生命周期；生产清理时效 **未核**。（证据：`hq/notes/tsnet-headscale-casdoor-2026-08-26.md:42-48,151-159`）

### D11. access / refresh token 时长

- **倾向：access 1h、refresh 30d；refresh 每次成功后若服务端旋转则原子替换 credential。** 该时长没有项目内裁定或实测，**未核**。
- 不套用 DIPC 的 24h/168h：那是 public web session，不是本机 CLI credential。（证据：DIPC 时长只见 `Infrastructure/casdoor-config/docs/organizations-and-applications.md:30-34`）

### D12. identity 成功、network 失败时是否提交 owner

- **倾向：提交，并在结果中拆成两个状态；重跑只重试 network。** 理由是 Allen 已裁 user 与 host 为不同对象，network 故障不能抹掉已经验证的用户身份。（证据：`hq/notes/spec-e2e009-user-identity-2026-09-03.md:1-12`）
- 该失败恢复 UX 尚未跑过，**未核**；若 Allen 要求命令全有或全无，就必须定义跨 credential/settings/tsnet 三个存储的事务边界，当前没有这样的机制。（证据：现有 owner 只写 settings，`src/hyprial/daemon/identity.py:188-231`；跨存储机制未核）

## 8. 验证顺序

### 8.1 原则

更新 #293 §6 为：**先钉身份契约，再验 Casdoor 真流，再按 Allen 原顺序先官方 Tailscale、后 Headscale，最后多机/全量。** 不能先写 mock 让自造 token shape 变绿；协议夹具必须来自同版本真实 Casdoor，集成层直接跑隔离服务。（证据：#293 §6 的先官方顺序；当前生产版本 pin 见 `Infrastructure/casdoor-config/README.md:8`、`Infrastructure/casdoor-config/compose.yaml:75-76`）

### 8.2 顺序与判据

1. **配置静态核**：对 Casdoor v3.144.0 源码和生产 discovery 核 `device_authorization_endpoint`、device grant、S256、scopes、UserInfo `sub/preferred_username`；结果作为 contract fixture，记录生成版本/hash，不手写 JSON。（证据：Casdoor v3.144.0 `object/wellknown_oidc_discovery.go:156-178`、`object/user.go:1213-1247`）
2. **身份字段焦点测试**：真实 UserInfo 中 `preferred_username=alicesmith`、`sub=<id>`、email 另值，断言 owner 只取 username；变异为改取 `sub` 或 email，该测试必须红。（对应修法：§1/D1）
3. **隔离 Casdoor device E2E**：创建 `app-hyprial` 等形应用，跑真实 device authorize → 浏览器批准 → poll → refresh → UserInfo；分别跑 Casdoor password 与已绑定 tsidp account，断言两者得到同一 Casdoor owner。禁止用自写 token endpoint stub 冒充。（对应修法：§2/§4；Q2(A) 尚未覆盖的真实浏览器/device 格）
4. **credential/settings 焦点测试**：真实 token response 经 credential-store adapter 写入平台测试 keyring；临时 HYPRIAL_HOME 只出现 owner、不出现 token；删 access-token 内存后以 refresh 恢复。变异为把 refresh/access 写 settings，secret 扫描必须红。（对应修法：§2.4）
5. **现有身份落点回归**：保留 `env > settings > error`、无 getpass、同值 login 幂等；新增 `HYPRIAL_OWNER` 同值/异值测试。变异为恢复 host fallback 或让 settings 高于 env，既有/新增测试必须红。（对应修法：§3.1；现有测试依据 `tests/test_actor_identity.py:108-160`）
6. **CLI 切面**：断言 `init --owner` 和 `squire setup --owner` 消失，squire owner 来自 resolver，缺身份提示 login；变异为重新暴露任一 owner flag，命令面测试必须红。（对应修法：§3.2-3.3）
7. **账号切换/迁移焦点**：临时状态从手写 old owner 登录到 new owner，复用 #352 migration 跑两次，第一次精确改活 owner、第二次 0；路径、ownerKey/loginName、本机旧 machine 名和历史文本不变。变异为 substring replace，三条负向断言必须红。（对应修法：§5；现有测试依据 `tests/test_owner_migration.py`）
8. **官方 Tailscale 验证**：先跑 #293 `WatchIPNBus` 真点击，补上此前未核的 OIDC/浏览器路径，观察 `NeedsLogin → Starting → Running`；需要管理员人工动作，不拿隔离 Headscale 结果冒充。（证据：`hq/notes/evidence-tsnet-watchipnbus-2026-08-27.log:4-19`；#293 §4/§6）
9. **Headscale + Casdoor 串联**：隔离 Casdoor + enrollment service + Headscale，用两个真实 Casdoor 用户拿两把一次性 key，两个 tsnet 节点归属不同且 zenoh 收发；再以同一用户两台 machine 注册同名 actor，裸名只解析本机完整 URI。（证据：Q1-Q3 已证明基础链路，`hq/notes/tsnet-headscale-casdoor-2026-08-26.md:7-17,131-147`；同 owner/machine 语义见 `src/hyprial/daemon/identity.py:162-169`）
10. **故障格**：Casdoor 重启使进行中 device flow 过期、离线 daemon 仍用 settings 启动、refresh 失效要求重登、network 失败不回滚 owner。Casdoor 故障恢复目前未测，结果必须落证据后才能宣称支持。（证据：Q 未测清单 `hq/notes/tsnet-headscale-casdoor-2026-08-26.md:151-159`）
11. **全量**：焦点绿后跑全量 pytest；先对同一 `origin/dev` 做同树对照。完整输出一次性落文件，不通过管道截断；只接受本分支相对基线没有新增失败。（证据：本单验收纪律）

### 8.3 实施测试矩阵

| 设计修法 | 至少一条测试 | 变异判据 |
| --- | --- | --- |
| username 是 owner | 真实 Casdoor UserInfo 三字段分离 | 改取 sub/email 必红 |
| Casdoor 而非直连 tsidp | 两种登录方法同 Casdoor owner | 直接用上游 username/token 必红 |
| device flow | pinned Casdoor 完整 authorize/poll/refresh | 自造未绑定 client 的响应不能过 contract |
| token 不进 settings/日志 | temp home + secret scan | 持久化/打印任一 token 必红 |
| offline daemon 用 settings | 断网启动与 token 过期测试 | 加在线 introspection 启动依赖必红 |
| env 优先级 | env/settings 同值与冲突 | 交换优先级必红 |
| 删除人工 owner flags | CLI help/调用面测试 | 恢复任一 flag 必红 |
| 切换复用 #352 | 迁移等价、幂等、三类残留不变 | substring replace 必红 |
| Headscale user 映射 | 两用户/多机真节点 | 把 machine 或 host login 当 owner 必红 |
| network 与 identity 分状态 | network 注入失败后 owner 仍可解析 | 回滚/混成一个 success 位必红 |

当前本文是设计，不实现上述测试；实施 PR 必须逐条给出“变异红 → 修复绿”的命令与输出位置，不能只报最终绿。

## 9. 裁定汇总(Allen 2026-09-04,与 hq-adjutant 飞书讨论;实现单以此为准,与前文冲突处以本节为准)

1. **组织模型走 B**:Casdoor 只管「你是谁」——一个 `hyprial` organization,身份唯一;不做「每组织一个 Casdoor organization」。
2. **D3 改为复用 `app-hyprial`**,不新建 `app-hyprial`。§4.2 的配置表改为对 `app-hyprial` 的修改:启用 Device login、grant types 加 `device_code` + `refresh_token`、token 字段放 `Name`、scopes `openid profile offline_access`。⚠️ 这是对生产 app 的修改,合入前必须核既有 tsidp 登录路径不受影响;运维步骤单列 runbook,由 Allen 执行。
3. **Headscale user = 人**:Headscale 经 OIDC 登录 Casdoor 自动建与 username 同名的 user;tailnet 用户名、hyprial owner 段、`settings.owner` 三者同名。⛔ 不把 Headscale user 当组织。
4. **组织 = 一个 Headscale 实例 = 一个 tailnet**。进组织由控制面决定(Headscale 的 OIDC 允许名单 / Tailscale 组织成员),hyprial **不维护成员表**;组织内访问控制用 ACL。preauthkey 只给无人值守节点,归人不归组织。
5. **多组织 = 多个控制面实例**,可共用一个 Casdoor。一期只托管 hyprial 自己这一套(手工部署);外部组织默认自托管;「代托管」是可选服务,出现第二个组织时再做 provisioner(部署模板起实例,不改 Headscale)。
6. **网络 profile 抽象**:profile = {组织名, 身份 issuer(OIDC), 控制面 kind(`tailscale`|`headscale`)+URL, 入网方式}。`hyprial login` 永远两步:①向 `profile.issuer` 做 device flow 取身份(owner = 该 issuer 的 `preferred_username`);②向 `profile.控制面` 入网(tailscale:tsnet BrowseToURL;headscale:OIDC 注册流或 preauthkey)。三种情形(已有 Tailscale 组织 / 自托管 Headscale / 代托管)只是 profile 取值不同,登录形态不变。
7. **issuer 与控制面 URL 是 profile 级配置**,不是写死的常量;§2.2 第 1 步的「issuer 精确等于 `https://auth.hyprial.com`」改为「精确等于该 profile 配置的 issuer」;默认 profile = hyprial(issuer `auth.hyprial.com`,控制面 Hyprial 的 Headscale)。DERP 默认用 Tailscale 公共 DERP map。
8. **一台机器可有多个 profile**(各自 `HYPRIAL_HOME` + tsnet 状态);同一人属多个组织即多个 profile。同一 profile 内只允许一个 issuer。
9. 其余 D1、D2、D4–D12 按本稿倾向(Allen「基本同意」)。
10. 实施形状:从 dev 开集成分支 `intg/hyprial-login`,单元 U1 profile 抽象 / U2 身份段 / U3 入网段 / U4 app-hyprial 运维 runbook / U5 迁移与切换各开 PR 进它;全绿 + E2E 后一次 `intg/hyprial-login → dev`。

## 10. 实施规划 U1–U5(hq-adjutant 2026-09-04,Allen 要求:先定方向与验收,infra-op 按 phase 细化后派单)

顺序:U1 → U2(依赖 U1 的 profile 配置)‖ U4(runbook 可与 U2 并行写,但必须在 U2 的真实 E2E 之前由 Allen 执行)→ U3(依赖 U1;与 U2 可不同 worker 并行,接口只有 profile)→ U5(最后)。每个单元:先红后绿的测试、变异验证、draft PR 进 `intg/hyprial-login`、正文列不做项;⛔ 不写兼容/哨兵代码(Allen)。集成分支 → dev 的门:全量 unit 绿、隔离 E2E(Casdoor + Headscale 容器,形态同 08-26 实验)绿、E2E-009 九项绿、U4 已由 Allen 执行、hq 节点切换演练通过。

### U1 profile 抽象
- 方向:「网络 profile」成为一等配置:每个 profile 一个 `HYPRIAL_HOME`;`profile.json` = {org, issuer, controlPlane:{kind: tailscale|headscale, url}, join};默认 profile = hyprial(issuer `https://auth.hyprial.com`,控制面 Hyprial 的 Headscale),没有文件时即默认;`hyprial profile list|use|create`;`settings.owner` 仍是身份落点,`resolve_node_owner` 的来源顺序不变(HYPRIAL_OWNER > settings > error)。只存名字与 URL,不存派生 URI。
- 验收:①读/校验的单测(kind 枚举、同一 profile 只有一个 issuer、缺文件 = 默认 hyprial);②一机两 profile 的隔离测试:两个 HYPRIAL_HOME 各起隔离 daemon,状态/socket/zenoh 互不共享;③现有所有测试不变绿(profile 是加法);④skill 文档新增 profile 一节(Skill-Impact: updated)。
- 不做:登录、入网、迁移。

### U2 身份段(`hyprial login` 的第一步)
- 方向:对 `profile.issuer` 做 OIDC device authorization:discovery(issuer 必须逐字等于 profile 配置)→ device endpoint(公开 client id,scope `openid profile offline_access`)→ 轮询 token(用服务端 `interval`/`expires_in`)→ UserInfo 取 `preferred_username` → `write_settings_owner`;access/refresh token 落 `secrets/`(0600,按 profile 分);`--no-open` 只不拉浏览器,`--json` 绝不输出 device code / token / preauthkey;离线时 daemon 只读 `settings.owner`(token 不是离线身份源)。D6:登成与现有 owner 不同的账号时按设计稿 D6 的裁定(切换需显式确认)。
- 验收:①用仓内假 OIDC 服务(discovery/device/token/userinfo 四端点)的测试覆盖:成功写 owner、issuer 不匹配拒绝、device code 过期、refresh 成功与失败、JSON 输出无机密;②针对真实 `auth.hyprial.com` 的 E2E 只在 U4 执行后跑,且用测试账号;③变异:去掉 issuer 校验 ⇒ 红。
- 不做:入网(U3);app-hyprial 的改动(U4)。

### U3 入网段(`hyprial login` 的第二步)

> ⏸ 2026-09-04 Allen:Hyprial 自己的 Headscale 一期**不部署**(U6 推后),生产与默认 profile 走官方 Tailscale;但 `kind=headscale` 的代码**保留并可测**(「做好部署后就能用的准备」):隔离 Headscale 容器仅作 CI 测试替身。CI 无人值守入网:官方路径用带 `tag:hyprial-ci` 的 ephemeral pre-approved auth key(join=preauthkey;Allen 2026-09-05 更正:此前误写为 `tag:` + `hyprial-ci`,真名以 runner.mjs 钥匙规格为准);interactive 路径对官方只验到拿 URL、真点一次由 Allen 验收,对替身无人值守跑通 OIDC 路径。preauthkey 两种 kind 都合法。
- 方向:按 `profile.controlPlane.kind`:`tailscale` ⇒ tsnet 的 BrowseToURL 验证路径(#293 §3/§4 的实测);`headscale` ⇒ Headscale 的 interactive 注册流(浏览器,底层是 OIDC)或无人值守 preauthkey(key 归人);控制面 URL 来自 profile;入网结果⛔不得反向改 owner;节点名 = `<machine>` 段。
- 验收:①隔离 E2E:一次性 Headscale 容器(形态同 08-26)+ 假/真 OIDC,入网后 Headscale 的 user == `preferred_username`、节点归属正确、`settings.owner` 不变;②tailscale 路径至少有 BrowseToURL 出现与状态机测试(真 tailnet 入网由 Allen 手动验一次);③失败路径:控制面不可达 ⇒ 明确错误,不落半状态。
- 不做:ACL、DERP 自建、provisioner。

### U4 app-hyprial 运维 runbook
- 方向:在 `Infrastructure/casdoor-config/docs/` 写 runbook(PR 由 infra-op 开,Allen 执行):启用 Device login、grant types 加 `device_code` + `refresh_token`、token 字段放 `Name`、scopes;⛔ 不动 tsidp provider、不改注册策略;附回滚步骤与「既有 tsidp 登录不受影响」的核验清单;同步 `docs/organizations-and-applications.md`。
- 验收:Allen 执行后,测试账号能对 `auth.hyprial.com` 走完 device flow 并拿到 `preferred_username`;既有 tsidp 登录路径核验通过;仓内无机密。
- 不做:新建 app、Casdoor 版本升级。

### U5 迁移与切换
- 方向:已手写 owner 的节点执行 `hyprial login` 后 owner 必须与 `settings.owner` 相同(不触发迁移);不同则走 D6;删掉所有 getpass/主机登录名残留与 `identity.py` 里过时的 docstring(logindsg 报的绊线);HYPRIAL_OWNER 优先级按 D7 裁定写死并有测试;切换顺序写成 runbook:U4 → 各节点 login → `hyprial message targets` 计数不变 → 生产 hq 节点由 Allen 按钮。
- 验收:hq 节点演练:login 后 owner==alicesmith、targets/hosts 计数不变、DLQ 不增;E2E-009 九项绿;设计稿 §5 与 runbook 一致;⛔ 无兼容代码。
- 不做:多组织 provisioner、Casdoor 组织变更。
