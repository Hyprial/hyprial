# Provider key：接口与数据契约冻结（第 1 步）

状态：**FROZEN r3**（2026-11-03，HQ 裁定见 §11）。r2 处理了 settings 线（#2328 @`e81a2db47`）提出的 4 项差异，
以及 Allen 关于 squire 普通化的约束（#2313 S10）；r3 记录 HQ 对 F-1～F-10 的裁定，见 §13。改动本文 §3–§8 需要 HQ 重新评审。
作者：cloud-claude（provider key 后端线，经 Allen 向 HQ 研发负责人汇报）。PR：#2195（`feat/provider-keys-0553` → `release/v0.5.5.4`）。
本文只冻结契约，不含实现，也不含运行结论。实现见第 2 步（E2/G1/G2a）和第 3 步（旧入口转接）。

## 0. 基线与输入

| 输入 | 引用 |
| --- | --- |
| 实现基线 | `feat/provider-keys-0553` @ `d5eba97d5`（已含 HQ PAC 的 release merge `d55d4e82c` 与修红 `a366fce2c`/`d5eba97d5`） |
| 设计 #2077 | `design/provider-key-ownership` @ `8d5933f6b`：`docs/design/provider-key-ownership-2026-10-29.md`（下称“设计”） |
| 实施计划 | 同分支 `docs/design/provider-key-ownership-impl-plan-2026-10-31.md`（下称“计划”），§8.1 R1 已裁 |
| 已交付核心 | `contract/identity/provider-keys.md`（E1：信封、grant、六码）、`contract/identity/user-key.md`（U1/U2/X1） |
| org 模型 #2078 | `design/org-membership-model` @ `abddc8321`：设计 §8 第 7 条（Q7=A）；实施计划 §3 H1、§8 R8/R9、§8.1 |
| 稳定 id #1886 | `intg/uuid-addressing-20261023` @ `89302601a`：`uuid-addressing-migration-path.md` D1、`uuid-addressing-implementation-2026-11-03.md` |
| cell 阶段 A #2323 | `intg/cell-model-phase-a` @ `6cfdaa863`：`cell-model-phase-a-plan-2026-11-03.md` |
| settings 后端 #2328 | `intg/gui-settings-backend` @ `e81a2db47`（目标 `intg/gui-v1`）。规范源：`agent.secret.list` 在 `daemon/impl/request_catalog/agent/__init__.py`（`AGENT_SECRET_LIST_SPEC`）；`provider-write`/`grant` 在 `daemon/impl/settings_contracts/credentials/__init__.py`，绑定在 `request_catalog/agent/secret_write/__init__.py`；处理逻辑在 `daemon/impl/application/actors/agents_admin/secrets/__init__.py` |
| squire 普通化 | #2313 设计稿 `c3cd1706d0`，阶段 B 切片 S10：squire 是普通 agent，provider key 不为它设任何特例 |

“六码”指 E1 已注册的六个 provider 码：`PROVIDER_GRANT_MISSING`、`PROVIDER_GRANT_REVOKED`、`PROVIDER_KEY_NOT_FOUND`、
`PROVIDER_KEY_RECIPIENT_PENDING`、`PROVIDER_KEY_UNSEAL_FAILED`、`PROVIDER_KEY_SIGNATURE_INVALID`。
仓库里没有更上层的错误分类：`ipc_errors` 是扁平注册表，HTTP 状态只看 `STATUS_BY_CODE`（`shell/impl/request_http/errors.py:18-61`）。

## 1. 冻结范围

本文冻结以下内容：

1. 七个新 MethodSpec 及其 CLI：`user.provider.login/list/logout/rotate`，`agent.provider.allow/revoke/status`（§5、§6）。
2. 每个方法的错误码及其与六码的关系（§7）。
3. 数据：ProviderKey 元数据记录、grant 状态记录、本地 UsageReceipt；OrgFS 空间与路径；写入、CAS、墓碑和准入规则（§3、§4）。
4. 旧 `agent.secret.*` / `agent.provider.*` 到新面的映射，以及适配层规则（§8）。
5. 跨线问题和建议答案（§9）。

不在本次冻结范围内（HQ 会另行拆分）：OAuth/refresh（R5）、跨用户宿主取钥（R2 的协议细节）、move 重封（M1）、
旧数据迁移与回滚（MIG1）、旧命令退役（RET1）、GUI。设计 §7 的 `provider.key.*` / `provider.grant.*` 事件名保留，
字段等 GUI 线开工时再冻结；本阶段不发事件。

## 2. 标识与编码

| 名称 | 冻结规则 |
| --- | --- |
| `owner` | Casdoor `sub`，与 U1/U2 的 owner 相同；E1 约束：非空，≤256 字符，不含 NUL |
| `keyId` | `pk.` + 26 位小写 base32（RFC 4648 字母表，去掉填充），由 128 bit CSPRNG 生成，例如 `pk.mfrggzdfmztwq2lknnwg23tpob`。只分配一次，永不复用（删除后留墓碑，见 §4.4） |
| `provider` | `^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$`，与旧 `_ENTRY_PATTERN`（`identity/impl/agents/runtime/secrets.py:45`）相同，所以旧 entryId 可以无损映射（§8）。用作路径段，区分大小写 |
| `agent`（grant 内） | E1 规定的规范小写带横线 UUID。**取值 = `str(uuid.UUID(hex=entity_token))`**（§9.1）。`entity_token` 不是 32 位小写 hex 时拒绝，不做大小写归一 |
| `agentId`（展示和寻址） | #1886 的 `a.` + 26 位 base32，由同一个 `entity_token` 派生（`Agent.agent_id`）。只用于输出和输入解析，不进入签名结构 |
| `environmentNames` | 每项匹配 `^[A-Z][A-Z0-9_]*$`（同旧 `_ENV_PATTERN`），1–8 项，不重复 |
| 时间 | 非负整数 Unix 毫秒（同 E1） |
| `label` | 0–128 个 Unicode 字符，不含控制字符；不参与任何唯一性判断 |

规范编码沿用 U1 的 `canonical_bytes`（E1 已使用），不新增第二种编码器。

## 3. 数据

### 3.1 签名记录（新增 purpose）

E1 只签信封和 grant。元数据和 grant 状态里也有安全相关字段（删除墓碑、当前信封摘要、撤销状态、序号），
如果不签名，OrgFS 对端可以伪造墓碑（拒绝服务），或者把指针指回旧信封（回滚）。因此新增 **一个** purpose，
用于主人签发的类型化记录：

```text
record          = {payload, sealedBy: {keyId, sig}}
signingInput    = UTF8("hyprial/provider-key/record/v1") || 0x00 || SHA-256(enc(payload))
payload.type    ∈ {"provider-key", "provider-grant-state"}
```

签名规则与 E1 相同：主人 U1 签名钥，`sealedBy.keyId` 要与经 U2 验证的 OwnerKey 一致，先验签再读其他字段，
未知字段一律拒绝。三个 purpose（envelope/grant/record）互不相同，签名不能互换。
**这一项需要安全评审**（crypto reviewer 复核与 E1 同构）；未通过之前，E2 不进入生产路径。

### 3.2 ProviderKey 元数据记录（`type: "provider-key"`）

```text
payload = {
  type: "provider-key", v: 1,
  owner, keyId, provider,
  kind: "api-key",                      # 本阶段只接受 api-key；"oauth" 保留给 R5 之后
  label,                                # 见 §2
  state: "active" | "deleted",
  seq: 正整数,                          # 每次改动 +1；准入拒绝回退（§4.5）
  createdAt, updatedAt,
  dekVersion: 正整数 | null,            # deleted 时为 null
  envelopeDigest: 64 位小写 hex | null, # SHA-256(enc(当前信封))；deleted 时为 null
  recipients: [{recipientKeyId, role: "owner-device" | "host", deviceUid}],  # deleted 时为 []
  environmentNames: [...],              # 默认注入名；grant 可以覆盖（§3.3）
  legacyRef: {entryId, fieldName} | null  # 只有经旧 provider-write 建的钥才有；同一 owner 下唯一（§8.4）
}
```

- `recipients` 要与当前信封的 `recipients[].recipientKeyId` 集合**完全相等**，否则按 `PROVIDER_KEY_SIGNATURE_INVALID` 处理。
  `deviceUid` 和 `role` 只是展示投影；真正决定谁能解封的是信封本身。
- 本阶段 `role` 只写 `owner-device`；`host` 的签发者（R6）交付之前不写。
- **`lastUsed` 不进 OrgFS。** Q4 已裁：本版用量只记本地，所以 `lastUsedAt` 由本机 UsageReceipt 在 list 时合并（§3.4）。
- 授权给了哪些 agent，同样不存在元数据里，list 时由 grant 状态记录推出（§5.2）。这样避免两份权威互相漂移。

### 3.3 Grant 状态记录（`type: "provider-grant-state"`）

每个 `(owner, agent, grantId)` 恰好一条，是撤销和新鲜度的权威记录（E1 的 grant 本身不带 revision/revoked）。
`grantId` 是授权槽位：新 API 固定为 `grantId = provider`（所以对新 API 来说就是“每个 provider 一条”）；
旧 `agent.secret.grant` 用调用方给的 grantId。这样旧接口里“同一 agent 对同一 entry 的不同 field 各有一条 grant”
（例如一组 id + secret）也能原样表达。`grantId` 匹配 `^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$`（同旧规则）。

```text
payload = {
  type: "provider-grant-state", v: 1,
  owner, agent,                    # agent = E1 规范 UUID（§2）
  grantId, provider,
  state: "active" | "revoked" | "awaiting-key",
  seq: 正整数,                     # 每次 allow/renew/revoke/reissue/complete +1
  keyId: string | null,            # awaiting-key 时为 null
  grant: E1 signedGrant | null,    # 只有 active 时非 null
  environmentNames: [...],         # 本 grant 的注入名，缺省取元数据的值
  legacyRef: {entryId, fieldName, revision} | null,  # 只有经旧 agent.secret.grant 建的授权才有（§8.4）
  updatedAt
}
```

- `awaiting-key`：旧 `agent.secret.grant` 允许先授权、后写 entry（旧实现不检查 entry 是否存在）。
  这种情况下先落 `awaiting-key` 记录；之后同一 owner 用 `legacyRef` 相同的 `(entryId, fieldName)` 建钥时，
  daemon 把所有等待它的记录补签为 active（seq+1）。启动时遇到 `awaiting-key` 报 `PROVIDER_KEY_NOT_FOUND`。
  新 API 的 allow 不产生这个状态（钥必须已存在）。

- `grant.payload` 的 `provider/keyId/owner/agent` 必须与外层字段相等。`dekVersion` 必须等于元数据当前的 `dekVersion`，
  否则视为过期 grant（E1 `check_access` 用 `current_dek_version` 判断）。
- E1 的 `check_access` 输入：`current_grant_digest = SHA-256(enc(grant))`，`current_dek_version` 取元数据，
  `revoked = (state == "revoked")`。这三项都只能来自 §4.5 准入后的记录。
- `grant.payload.expiresAt` **必填**（§9.5 R4），默认 `issuedAt + 24h`。
- `recipientKeyId`：本阶段等于**发起启动的本机**的用户加密钥（X1 owner-device key），因此主人只能为在自己设备上运行的 agent 授权。
  跨用户宿主（host-for-grant）等 R2/R6。
- revoked 不是终态：再次 allow 会写 `seq+1, state:"active"`。删除钥会先撤销所有引用它的 grant（§4.3）。

### 3.4 本地数据（不进 OrgFS，不进 bundle/transfer）

| 文件 | 内容 | 权限 |
| --- | --- | --- |
| `H/state/provider-keys/receipts.jsonl` | UsageReceipt：`{at, owner, keyId, provider, agent, agentId, deviceUid, dekVersion, grantDigest, outcome}`，`outcome ∈ {injected, refused:<code>}` | 目录 0700，文件 0600；只追加；绝不含明文或 DEK |

`owner` 一律取自 grant，不取宿主登录用户（计划 G2 规则）。

r1 草案里的本地 `legacy-index.json` 已删除。旧接口需要的 `entryId/fieldName/grantId/revision` 改为写进签名记录的 `legacyRef`，
从 OrgFS 读取即可恢复，不存在“索引丢了”的状态（§8.4）。

## 4. 存储

### 4.1 空间（F-4 裁定后）

- **唯一存储入口 = owner 存储端口 `OwnerStorePort`**：provider 模块只经这个端口按 owner 取得存储空间，不解析任何空间名称，也不直接引用 #2078 或 #2323 的实现。
- **目标空间 = #2323 阶段 A S3 的 owner scope**（方案 §1.1：`CellRuntimePort` 的 owner scope 映射到 owner-home 空间）。HQ 裁定 F-4：provider 记录与 S3 owner scope 共用同一个空间，不另设第二个存储位置。
- **过渡适配器**：S3 落地之前，端口适配到个人 org 目录空间（#2078 H1 的 `personal_org_for(owner)`）；S3 落地之后，只替换适配器，provider key 代码不改。
- **现状（`release/v0.5.5.4` @ `e63971574`）**：S3 的 `CellSpaces.private_space` 和 #2078 的 `personal_org_for` 都还不存在，生产适配器返回 `PROVIDER_STORE_UNAVAILABLE`（§7.1）。测试注入隔离的 OrgFS 空间。
- **两者是否同一个空间**：按 #2323 计划 §2.2，owner-home 是 `CellSpaces.private_space(owner)`，标签为 `{kind:'hyprial-owner-home', owner}`；个人 org 是 Casdoor group `personal-<userKey>` 对应的空间。按现有计划，两者**不是同一个空间**，因此不满足“各留一句确认”的前提，改为在 #2195 报告这一差异（§13 r3）。
- 写入侧准入（§4.5）按空间类型挂接：个人 org 目录空间走受保护分区 hook；owner-home 空间走 S3 的 `codec.verify`（作者必须等于 authority）。读取侧对两者一律验签（fail closed），不依赖写入侧准入。
- §4.2 的路径在两种空间下相同。

### 4.2 路径

```text
directory/provider-keys/<keyId>/meta.json                  # 签名记录，type=provider-key
directory/provider-keys/<keyId>/envelope-<dekVersion>.json # E1 信封，create_only，写后不改
directory/provider-grants/<agent>/<grantId>.json           # 签名记录，type=provider-grant-state
```

`provider-keys`、`provider-grants` 作为新的受保护目录分区，与 `user-keys` 同级（`identity/impl/org_directory/models.py`
的 `_PROTECTED_DOC_SECTIONS`）。路径主体（keyId、agent、grantId）必须与记录内字段相等。

### 4.3 写入协议（全部由主人设备上的 daemon 执行）

所有条件写都用 OrgFS 的 `write_text(..., expect_version=...)` / `create_only=True`（`daemon/impl/orgfs/api.py:248-268`），
冲突时得到 `stale-write`。不直接写 replica DB，不建第二份同步库。同一把钥、同一条 grant 的变更在本机持有进程内锁。

| 操作 | 顺序（提交点加粗） | 崩溃后 |
| --- | --- | --- |
| login | 分配 keyId → 封信封（dekVersion=1，recipients = 主人全部可信 owner-device 钥）→ `envelope-1.json` create_only → **`meta.json` create_only（seq=1）** → 读回、验签、本机试解 DEK | 只有信封、没有 meta 的钥不可见；由 GC 清理孤儿信封 |
| rotate（DEK 或 `--replace`） | `envelope-(n+1).json` create_only → **meta CAS（seq+1，dekVersion=n+1，新摘要）** → 逐条重签引用它的 active grant（CAS，seq+1）→ 删 `envelope-n.json` | 重跑时从第一条未完成的步骤继续；重签之前的旧 grant 因 dekVersion 不符而拒绝启动（fail closed） |
| allow | 校验 meta active，本机是 recipient → 签 E1 grant → **grant 状态 CAS（seq+1，active）** | 幂等：内容等价（同 keyId、注入名、legacyRef）的 active grant 不升 seq |
| 补签 awaiting-key | login 提交后，找出同 owner 下 `legacyRef.(entryId, fieldName)` 相同的 awaiting-key 记录 → 逐条 **CAS 为 active** | 可重跑；login 本身已提交，补签失败只影响这些 grant，list 照实显示 |
| revoke | **grant 状态 CAS（seq+1，revoked，grant=null）** | 幂等：已经 revoked 时直接成功 |
| logout | 对每条 `keyId` 相同的 active grant 执行 revoke → **meta CAS（state=deleted，dekVersion/摘要置空，recipients=[]）** → 删除所有 `envelope-*.json` | 级联可以重跑；meta 未 deleted 之前，该钥上的新 allow 被本机锁拒绝 |

CAS 冲突的处理：重读，重新校验前置条件，最多重试 3 次，之后返回 `PROVIDER_STORE_CONFLICT`（§7.1）。
**任何合并都不能丢掉一次撤销**：比较 seq，取大；revoked 只能被更高 seq 的显式 allow 覆盖。

### 4.4 墓碑

- `meta.json` 的 `state=deleted` 就是墓碑，永久保留，不删文件，防止旧副本借过期写入复活这把钥。
  keyId 永不复用。删除后 `envelope-*.json` 和 OrgFS 历史里的旧密文会留下，但被移除的设备即便手里有旧 DEK，也对后续密文无效。
  明文本身已经被见过的问题见计划 R9。
- grant 状态没有删除，只有 `revoked`。

### 4.5 准入（E2a 交付的 OrgFS admission hook）

对上述三类路径的写入，以下条件全部满足才接受，否则拒绝（fail closed）：

1. 作者设备属于该个人 org 的主人：作者在 `directory/devices/<owner>/` 中，且 `owner` 等于空间主人。不能只信 `origin.author`（计划 C12）。
2. 记录或信封经 `OwnerKeyPort.lookup(owner, keyId)` 返回 `verified` 的钥验签通过。`unavailable` 时拒绝，并且不把它记成 absent。
3. 路径主体与内容一致；`seq` 严格大于当前已接受的版本；`deleted` 的 meta 不能再改。
4. `envelope-*.json` 只能 create_only；meta 提交时，`envelopeDigest` 必须与同目录下对应 dekVersion 的信封摘要相等。
   信封不到、摘要不符时 meta 暂不生效（与 OrgFS 现有 `content-pending` 一致）。

## 5. MethodSpec

公共约定：

- `owner = "daemon.provider-keys"`，`mode = "unary"`，`http_exposed = False`（HTTP 暴露交给 GUI 阶段再定），`confirmation = "none"`。
- `audience = ("local-operator",)`。注册时带 preauthorize（与 `userKey.*` 相同的钩子）：`origin` 必须是 `"ipc"`；
  worker origin、交互会话 peer、以及参数里出现 `actor` 或 `sessionRef` 的请求，都在 schema 校验之前以 `CALLER_NOT_AUTHORIZED` 拒绝。
  正因为这样，目标 agent 的参数名是 **`agent`** 而不是 `actor`：参数里带 `actor` 会被 router 当成 agent 调用者（`router.py:300-333`）。
- schema 一律 `additionalProperties: false`。秘钥字段只写不读：不出现在结果、错误、日志、审计里。
- `user` 是新方法族，加入 `MIGRATED_FAMILIES`（`request_catalog/registry.py:32`），所以未声明的 `user.*` 返回 `METHOD_NOT_FOUND`。
  `agent` 族不迁移，`agent.provider.allow/revoke/status` 作为声明方法注册，旧 router 分支不受影响。
- 绑定元组 `PROVIDER_KEY_BINDINGS` 放在新文件 `daemon/impl/provider_keys/ipc.py`，在 `_build_registry` 中只追加一项。

### 5.1 共用结果对象

```text
ProviderKeyView = {
  keyId, provider, kind, label, owner, state, createdAt, updatedAt,
  dekVersion|null, environmentNames,
  recipients: [{recipientKeyId, role, deviceUid}],
  lastUsedAt: integer|null,                       # 本机 receipt
  grants: [{agent, agentId|null, grantId, provider, state}]   # agentId 只在本机能解析时给出
}
GrantView = {
  agent, agentId, actor, grantId, provider, owner, keyId|null, state, seq,
  dekVersion|null, issuedAt|null, expiresAt|null, environmentNames,
  recipientKeyId|null,
  legacyRef: {entryId, fieldName, revision} | null
}
```

### 5.2 方法表

| 方法 | params | result | mutates / retry | 说明 |
| --- | --- | --- | --- | --- |
| `user.provider.login` | `{provider, kind:"api-key", secret, label?, environmentNames?}` | `{key: ProviderKeyView}` | true / never | `secret` 是 1–16384 字节的 UTF-8，单行，`writeOnly`。每次调用都新建一把钥；相同 secret 也不去重，因为去重需要指纹，而发布指纹会泄露低熵秘钥 |
| `user.provider.list` | `{provider?, includeDeleted?: bool}` | `{keys: [ProviderKeyView], invalid: [{keyId, code}]}` | false / safe | 验签失败的记录不进 `keys`，只在 `invalid` 列出 keyId 和码 |
| `user.provider.logout` | `{keyId}` | `{keyId, state:"deleted", revokedGrants: [{agent, agentId|null, grantId, provider}], alreadyDeleted: bool}` | true / never | 按 §4.3 级联；重复调用幂等 |
| `user.provider.rotate` | `{keyId, replaceSecret?}` | `{key: ProviderKeyView, reissuedGrants: integer}` | true / never | 不带 `replaceSecret` 时只轮换 DEK；带它时同时替换明文 |
| `agent.provider.allow` | `{agent, provider, keyId?, environmentNames?, replace?: bool, expectedAgent?}` | `{grant: GrantView, changed: bool}` | true / never | `agent` 接受本机名、`a.` id 或 agent URI，由 daemon 解析到本机 registry 记录。主人在该 provider 下只有一把 active 钥时，`keyId` 可以省略。槽位 `grantId = provider` |
| `agent.provider.revoke` | `{agent, provider, expectedAgent?}` | `{grant: GrantView, changed: bool, restart: {state: "not-running" \| "not-supported"}}` | true / never | 撤销槽位 `grantId = provider`。本阶段不做受控重启（G2b），运行中的进程报 `not-supported`，CLI 在 stderr 警告 |
| `agent.provider.status` | `{agent, provider?, expectedAgent?}` | `{agent, agentId, actor, grants: [GrantStatus]}` | false / safe | 只读；列出该 agent 的全部槽位（含旧接口建的）。会试解 DEK，但不解密 payload，DEK 也不出 daemon |

`expectedAgent = {uri, createdAtMs}` 的格式和语义与 #2328 的 `agent.secret.list/grant` 完全相同，校验规则见 §8.3。
不符时返回 `AGENT_INCARNATION_MISMATCH`（加入这三个方法的错误集合）。

```text
GrantStatus = {
  grantId, provider,
  state: "ok" | "missing" | "revoked" | "awaiting-key" | "recipient-pending" | "unseal-failed"
       | "signature-invalid" | "key-not-found" | "unavailable",
  code: null | <六码之一> | "PROVIDER_STORE_UNAVAILABLE",
  owner|null, keyId|null, seq|null, dekVersion|null, expiresAt|null, unsealable: bool
}
```

status 只报告本机看得到的状态，不代表“启动一定成功”（启动时还要过 G2a 的新鲜度检查）。

### 5.3 GUI 准入（差异 1）

**2026-11-10 更新（已执行）**：产品决定提前打开，不再等 gui-v1 合入 release。理由是 GUI 启动在独立环境，
与 CLI 安装的 daemon 独立运行，不做 legacy Key 兼容。七个 spec 已按本节方案改为
`http_exposed=True`、`HttpBinding(IdentityBinding("none"))`；其余 spec 不变。HTTP 转发来源与拒绝规则
继续由 `tests/daemon/provider_keys/ipc/test_origin_e2e.py` 证明。

本阶段七个方法都是 `http_exposed=False`，因为 GUI 不在本阶段范围内。GUI 的准入方式现在一并冻结，到 GUI 阶段只需打开开关：

1. **打开方式**：GUI 阶段的 PR（在 gui-v1 合入之后）把七个 spec 改为 `http_exposed=True`，`http_binding=HttpBinding(IdentityBinding("none"))`，
   与 #2328 的 `agent.secret.provider-write/grant` 完全相同。除此之外 spec 不变；这个改动需要 HQ 复核，但不重新冻结形状。
2. **调用者身份**：浏览器不能声明身份。HTTP 壳负责认证本机 operator（可信 Web 宿主、token、readiness），再经本地 socket
   转发给 daemon，所以 daemon 看到的 origin 是 `"ipc"`，§5 的 preauthorize 照常放行。worker、交互会话 peer、带 `actor` 或
   `sessionRef` 的请求仍被拒绝。实现时要用一条测试证明：HTTP 转发的请求能通过 preauthorize，worker 转发的请求不能。
3. **满足 HTTP 视图的过滤条件**（`shell/impl/request_http/catalog.py` 的 `_unsupported_reason`）：audience 只有 `local-operator`，
   unary，`confirmation="none"`，写方法 `retry="never"`，读方法 `retry="safe"`。七个 spec 现在已经满足。
4. **只写字段**：`secret` 与 `replaceSecret` 声明 `writeOnly: true`，单行（`not: pattern [\r\n]`），与 #2328 的 `value` 相同。
   HTTP 壳和 daemon 都不记录请求体。
5. **确认**：logout 的二次确认由 GUI 自己做（同 #2328 的写操作），daemon 侧仍是 `confirmation="none"`。
6. **功能门关闭时**：七个方法返回 `PROVIDER_STORE_UNAVAILABLE`（HTTP 503），GUI 据此显示“凭据服务未启用”，不回退到旧接口。

在 GUI 改用七个新方法之前，GUI 继续使用 #2328 已暴露的旧兼容面（`agent.secret.provider-write/grant/list`）。
这些旧方法如何转到新权威，见 §8.6（转接点）与 §8.5（错误映射）。

## 6. CLI

| 命令 | IPC |
| --- | --- |
| `hyprial user provider login <provider> --api-key-stdin [--label L] [--env NAME]... [--json]` | `user.provider.login` |
| `hyprial user provider list [--provider P] [--all] [--json]` | `user.provider.list` |
| `hyprial user provider logout <keyId> [--yes] [--json]` | `user.provider.logout` |
| `hyprial user provider rotate <keyId> [--replace-stdin] [--json]` | `user.provider.rotate` |
| `hyprial agent provider allow --actor <agent> --provider <p> [--key <keyId>] [--env NAME]... [--replace] [--json]` | `agent.provider.allow` |
| `hyprial agent provider revoke --actor <agent> --provider <p> [--json]` | `agent.provider.revoke` |
| `hyprial agent provider status --actor <agent> [--provider <p>] [--json]` | `agent.provider.status` |

- 秘钥只从 stdin 读一行；stdin 是 TTY 时拒绝（同旧 `provider-write`）。不支持 argv 和环境变量传秘钥。
- 在 worker shell 里，七个命令都在联系 daemon 之前被拒绝（`_refuse_in_worker_shell`），daemon 侧再由 preauthorize 拒绝一次。
- 输出走 `CliResult`；`--json` 时 stdout 只有一个对象且带 `ok`。logout 没有 `--yes` 时用 `confirm` 在 stderr 列出受影响的 agent。
- CLI 的 `--actor` 映射到 IPC 的 `agent`。
- 设计 §4 里的 `login --from-cli` 是 OAuth 导入，本阶段不提供；`login --replace` 并入 `rotate --replace-stdin`。
- `contract/cli/surface.json` 和 `docs/reference/cli-command-inventory.json` 只追加，用生成脚本更新。

## 7. 错误码

### 7.1 新增的非 provider 码（**需要 HQ 批准**，F-1）

计划要求“不能用 missing 混淆 unavailable”，但六码里没有表达“存储不可用”或“并发冲突”的码，现有码也都不合适
（`DAEMON_UNAVAILABLE` 表示 daemon 本身不可达；OrgFS 的 `stale-write` 是小写的内部码）。因此提议新增两个：

| 码 | 含义 | HTTP |
| --- | --- | --- |
| `PROVIDER_STORE_UNAVAILABLE` | 个人 org 端口未交付或空间不可读写；或者功能门关闭（§8.7）；或者 U2 锚 `unavailable` | 503，可重试 |
| `PROVIDER_STORE_CONFLICT` | CAS 冲突，重试 3 次后仍失败 | 409，可重试 |

它们不是“第七个 provider 授权码”：不参与 `check_access` 的优先级，也不会出现在启动拒绝里。
启动拒绝里的“不可用”仍按 R4 的映射处理（§9.5）。如果 HQ 不批准，备选方案是两者都用 `DAEMON_ERROR`，代价是调用方无法区分。

### 7.2 每个方法的错误集合

“签名不可用”指 `USER_KEY_ABSENT`、`USER_KEY_UNBOUND`、`USER_KEY_BINDING_EXPIRED`、`USER_KEY_QUARANTINED`（沿用 U1）。

| 方法 | 通用码 | 六码 | 其他 |
| --- | --- | --- | --- |
| login | `INVALID_ARGUMENT`、`CALLER_NOT_AUTHORIZED` | `PROVIDER_KEY_UNSEAL_FAILED`、`PROVIDER_KEY_SIGNATURE_INVALID`（读回校验失败） | 签名不可用、`PROVIDER_STORE_*` |
| list | 同上 | 无（不合法记录放进 `invalid[].code`，取 `PROVIDER_KEY_SIGNATURE_INVALID` 或 `PROVIDER_KEY_NOT_FOUND`） | `PROVIDER_STORE_UNAVAILABLE` |
| logout | 同上 | `PROVIDER_KEY_NOT_FOUND`、`PROVIDER_KEY_SIGNATURE_INVALID` | 签名不可用、`PROVIDER_STORE_*` |
| rotate | 同上 | `PROVIDER_KEY_NOT_FOUND`、`PROVIDER_KEY_UNSEAL_FAILED`、`PROVIDER_KEY_SIGNATURE_INVALID` | 签名不可用、`PROVIDER_STORE_*` |
| allow | 同上、`AGENT_NOT_FOUND`、`AGENT_INCARNATION_MISMATCH` | `PROVIDER_KEY_NOT_FOUND`、`PROVIDER_KEY_RECIPIENT_PENDING`（本机不在 recipients 里）、`PROVIDER_KEY_SIGNATURE_INVALID` | `PROVIDER_CREDENTIAL_EXISTS`（已有另一把钥的 active grant 且没给 `replace`）、签名不可用、`PROVIDER_STORE_*`；多把钥但没给 `keyId` 时返回 `INVALID_ARGUMENT`（计划 G1） |
| revoke | 同上、`AGENT_NOT_FOUND`、`AGENT_INCARNATION_MISMATCH` | `PROVIDER_GRANT_MISSING`（从未有过 grant）、`PROVIDER_KEY_SIGNATURE_INVALID` | 签名不可用、`PROVIDER_STORE_*` |
| status | `INVALID_ARGUMENT`、`CALLER_NOT_AUTHORIZED`、`AGENT_NOT_FOUND`、`AGENT_INCARNATION_MISMATCH` | 六码只出现在 `grants[].code` 里，不作为错误抛出 | `PROVIDER_STORE_UNAVAILABLE` 也只出现在数据里 |

### 7.3 六码的落点

| 码 | 管理面（§5） | 启动面（G2a） |
| --- | --- | --- |
| `PROVIDER_GRANT_MISSING` | revoke 时没有记录；status `missing` | 没有 grant 状态记录 |
| `PROVIDER_GRANT_REVOKED` | status `revoked` | `state=revoked` |
| `PROVIDER_KEY_NOT_FOUND` | logout/rotate/allow 的 keyId 不存在或已 deleted；status `key-not-found` / `awaiting-key` | grant 指向的 meta 不存在或 deleted；grant 处于 `awaiting-key` |
| `PROVIDER_KEY_RECIPIENT_PENDING` | allow 时本机不是 recipient；status `recipient-pending` | 本机不在信封里；或者 grant 已过期而主人设备不在线、无法续签（§9.5） |
| `PROVIDER_KEY_UNSEAL_FAILED` | 读回或试解失败；status `unseal-failed` | 解封或解密失败 |
| `PROVIDER_KEY_SIGNATURE_INVALID` | 记录、信封或 grant 验签失败，或者 seq、dekVersion、摘要不一致 | 同左；E1 的优先级不变 |

全部拒绝启动，不回退到旧来源或宿主的钥（设计 §3.3 第 4 条）。

## 8. 旧面映射与适配层（第 3 步）

### 8.1 映射

| 旧入口（线上形状不变） | 新权威上的行为 |
| --- | --- |
| `agent.secret.provider-write {entryId, fieldName, value}` | 在当前 operator（owner）的 active 钥里找 `legacyRef = (entryId, fieldName)`：找到 → 对该钥执行 `rotate replaceSecret=value`；没找到 → `login provider=entryId, label="legacy:<entryId>/<fieldName>", legacyRef`，提交后补签等待它的 `awaiting-key` grant（§4.3）。结果 `{ok, entryId, fieldNames}` 与旧实现相同：`fieldNames = [fieldName]` |
| `agent.secret.grant` 且 `source=user-provider` | 先做 §8.3 的化身校验 → 解析 `legacyRef(entryId, fieldName)` 对应的钥 → 写槽位 `grantId` 的 grant 状态：钥存在时为 `active`，不存在时为 `awaiting-key`（与旧实现“不检查 entry 是否存在”一致）。`legacyRef = {entryId, fieldName, revision}`，`environmentNames` 原样保存。结果按旧行形状投影（§8.4） |
| `agent.secret.revoke {actor, grantId}` | 槽位 `grantId` 存在新记录时，执行 revoke；不存在时走旧路径（非 provider 来源） |
| `agent.secret.list {actor?, expectedAgent?}` | `agent-private`/`agent-shared` 的行照旧来自旧表；`user-provider` 的行**只**来自新权威（§8.4 投影），校验见 §8.3 |
| `agent provider grant-key`（CLI 循环 list+grant） | 因为下层已转接，无需单独处理 |
| `agent.secret.grant` 且 `source=agent-private/agent-shared` | **不转**，仍走旧路径（不在设计范围） |
| `agent.provider.login`（native 文件 / setupToken）、`logout`、`default` | **本阶段不转**：native 文件属于 OAuth（R5）；setupToken 和 default 的去向属于 R8，未裁之前不发明“默认 allow” |

适配层对任何 agent 一视同仁。**冻结契约里没有 squire 专用的字段、方法或回退路径**（Allen 已定 squire 是普通 agent，#2313 S10）。
首次设置流程（`management.squire.prepare`）只是旧 `agent.secret.list` 的一个普通调用方，它依赖的是 §8.4 的通用行投影。

### 8.2 单一权威，不双写

- 新写入只写新存储，不写 `H/secrets/providers/*.json`，也不写 `agent_secret_grants`。
- 对同一个 `(agent, grantId)`，一旦新权威里有记录，启动和 list 只看新权威，旧表里同槽位的行被忽略并记 warning。
  没有新记录时，旧 grant 继续生效，直到 MIG1 迁移验证通过。这是“只读旧数据”，不是双写。
- 迁移验证通过之前，不删除旧数据，也不退役 GUI 在用的旧接口。

### 8.3 化身校验（差异 4）

为避免重现 #2326 R1 那类化身漂移，`list` 和 `grant` 都保留 `expectedAgent = {uri, createdAtMs}` 校验，语义与 #2328 @`e81a2db47` 一致，并且更严格：

1. 参数校验沿用 #2328：`expectedAgent` 只能有这两个键；`list` 带 `expectedAgent` 时必须给 `actor`；格式不对返回 `INVALID_ARGUMENT`。
2. 从 registry 的**一次读取**中拿到目标记录的 `(uri, createdAtMs, entity_token)`。前两项与 `expectedAgent` 不符，或者记录不存在，返回 `AGENT_INCARNATION_MISMATCH`。
3. 后续读写只针对 `agent = str(UUID(hex=entity_token))`，这个值取自第 2 步的同一份快照：
   - `list` 只返回 `agent` 等于该 UUID 的记录。重建后的新化身 UUID 不同，因此旧化身的 grant **按绑定**就不可能出现在结果里，不依赖两次读取之间的时序；
   - `grant` 用该 UUID 签 E1 grant，并写 `directory/provider-grants/<该 UUID>/<grantId>.json`。CAS 之前再读一次 registry，
     `entity_token` 已变就返回 `AGENT_INCARNATION_MISMATCH` 且不写入。即便竞争漏过这一次检查，写下的 grant 也绑定在旧化身上，新化身用不了（fail safe）。
4. 不带 `expectedAgent` 时，按当前化身处理（与旧实现一致）。
5. 新方法 `agent.provider.allow/revoke/status` 也接受同样的可选 `expectedAgent`，规则相同（§5.2），GUI 阶段可以直接沿用。

### 8.4 旧行投影与授权恢复（差异 3）

**投影**：每条 `legacyRef != null` 的 grant 状态记录，投影成旧行：

```text
{actor: <目标 agent 当前名字>, grantId, source: "user-provider",
 entryId: legacyRef.entryId, fieldName: legacyRef.fieldName,
 environmentNames, revision: legacyRef.revision,
 ownerActor: null, ownerEntityToken: null}
```

`state=revoked` 的记录不投影（与旧实现“revoke 后从 inventory 消失”一致）；`active` 和 `awaiting-key` 都投影（旧实现里，
entry 未写的 grant 同样在 inventory 里）。经新 API（`agent.provider.allow`）建的 grant 没有 `legacyRef`，不出现在旧 list 里，只在新方法里可见。

**恢复**：r1 依赖本地索引，索引一丢就无法还原 `grantId/revision`，凭 `expectedGrant` 精确匹配的调用方因此会失败。
r2 把这些字段写进签名记录（§3.2、§3.3），恢复路径对所有 agent 相同：

| 场景 | 恢复方式 |
| --- | --- |
| 本机缓存或状态目录丢失 | 无需操作。投影所需的 `grantId/revision/entryId/fieldName/environmentNames` 都在 OrgFS 的签名记录里，重新读取即得到逐字段相同的行 |
| 主人换设备或新增设备 | 记录随个人 org 同步过来。新设备经 X1/U2 加入 recipients 后即可解封；在此之前 list 照常投影，启动报 `PROVIDER_KEY_RECIPIENT_PENDING` |
| agent 重建（新化身） | 旧记录绑定在旧 UUID 上，对新化身不可见（§8.3）。调用方按普通流程重新授权：`provider-write` 经 `legacyRef` 找到已有的钥（不新建），`grant` 为新 UUID 写新记录，revision 由调用方给定 |
| 重复提交同一次授权 | 幂等：内容等价时不升 seq，返回同一行，所以调用方的 `expectedGrant` 仍然匹配 |
| 主人签名钥轮换 | U2 生命周期要求轮换后由其余可信设备重签仍有效的记录（计划 §4）。重签只改签名，不改 `legacyRef` |
| 记录验签失败 | fail closed：list 整体返回 `AGENT_ERROR`，固定消息 `provider key authority unavailable`；不返回空 inventory、不回退旧表。显式 allow/grant 在可信签名锚可用时，以 CAS 和持久 high-water + 1 恢复；renew/revoke 不暗中修复 |

### 8.5 错误映射（差异 2）

适配层只能抛目标 MethodSpec 已声明的码。新权威的失败（`USER_KEY_*`、`PROVIDER_STORE_*`、六码）按方法映射：

| 旧方法 | 声明来源（@`e81a2db47`） | 新权威失败映射为 |
| --- | --- | --- |
| `agent.secret.provider-write` | `settings_contracts/credentials/__init__.py` 的 `_ERRORS` | `AGENT_SECRET_RESOLUTION_FAILED`（已声明） |
| `agent.secret.grant` | 同上 | `AGENT_SECRET_RESOLUTION_FAILED`（已声明）；化身不符为 `AGENT_INCARNATION_MISMATCH` |
| `agent.secret.list` | `request_catalog/agent/__init__.py` 的 `AGENT_SECRET_LIST_SPEC`：只声明 `AGENT_INCARNATION_MISMATCH`、`INVALID_ARGUMENT`、`AGENT_NOT_FOUND`、`AGENT_ERROR` | **`AGENT_ERROR`**（已声明），固定消息 `provider key authority unavailable` |
| `agent.secret.revoke` | 旧 router，没有 MethodSpec | `AGENT_SECRET_RESOLUTION_FAILED` |
| 启动类方法（`agent.start` 等，G2a） | `request_catalog/agent/start.py` 已声明 `AGENT_SECRET_RESOLUTION_FAILED` | 经旧启动面时，六码映射为 `AGENT_SECRET_RESOLUTION_FAILED`；新面保留原码（G2a 冻结） |

`list` 选择“换成已声明的码”，而不是给 `AGENT_SECRET_LIST_SPEC` 补声明。理由：这个 spec 的错误元组两条线都在改，
换码可以不改 spec、不改 openapi 摘要，也就没有合并冲突。如果 HQ 更希望 GUI 能区分这种失败，可以由 #2328 owner 在该元组里补一行
`AGENT_SECRET_RESOLUTION_FAILED`，适配层随后改用它即可，两种做法互相兼容。

### 8.6 转接点（差异 1 的旧兼容面部分）

- 唯一入口：本分支新增 `daemon/impl/provider_keys/legacy.py: dispatch(app, method, params) -> result | NOT_HANDLED`。
  功能门是 `legacy`，或者 `source` 不是 `user-provider`（revoke 时槽位没有新记录）时，返回 `NOT_HANDLED`，旧逻辑原样执行。
- 调用位置是**处理函数的开头，不是 registry 绑定**。@`e81a2db47` 上即 `_AgentSecretOpsMixin._handle_agent_secret`
  （`application/actors/agents_admin/secrets/__init__.py:17`）。这样能一次覆盖全部来路：
  - HTTP 绑定（`secret_write` → `_legacy_agent_call` → `app._handle_agent`）；
  - `AGENT_SECRET_LIST_SPEC` 绑定；
  - 直接调用 `app._handle_agent` 的内部调用方（`request_catalog/squire/operations.py:70`、GUI setup worker）；
  - 旧 IPC router。
- 在 release 基线上，同一段处理逻辑仍在 `registry_ops.py` 的分发里，本分支的调用点就加在那里；集成到 gui-v1 时，这三行调用移到上面的 mixin 开头（§10 第 3 项）。

### 8.7 功能门

新权威依赖 UK 门（可信用户签名钥）和 ORG 门（个人 org 端口），两者在生产中都还没交付。因此：

- 在 `settings.json` 设置 `providerKeys.authority = legacy | owner-keys`，**默认 `legacy`**，只能由 home 主人在本机修改。
- `legacy` 时，七个新方法返回 `PROVIDER_STORE_UNAVAILABLE`，未提供化身条件的旧凭据入口保持原行为；`agent.secret.list` 显式提供 `expectedAgent` 时仍执行化身校验，避免公开条件在默认权威下被静默忽略。内部可信化身围栏优先于公开字段。
- `owner-keys` 只有在 UK 门、ORG 门和 MIG1 都通过之后才允许打开。测试在隔离 home 里打开。
- 第 2、3 步的 provider authority 在 `legacy` 下不读写新钥库、不接管旧凭据；该门不覆盖整个 UserKey 生命周期。登录仍在所选 home/state、真实 deviceUid 与本机绑定就绪后执行本地 `UserKey.Store.ensure()`，可能创建 unbound Ed25519 钥并返回 `user-key` 阶段结果。缺绑定不伪造身份，不调用 issuer，不启用生产 signer；UserKey 状态/拒绝审计及 OrgFS user-key 分区保护仍生效。

## 9. 跨线问题与建议答案

### 9.1 agentId 与 E1 agent UUID（对齐 #1886）

事实：

- E1 要求 grant 里的 `agent` 是规范带横线的 UUID（`identity/impl/provider_keys/grants.py:21-24`）。
- 仓库里没有这样的字段。
- #1886 的 `agent_id` 是 `a.` + base32，由 `entity_token`（uuid4，32 位小写 hex）派生，属于**化身级**：
  destroy/recreate、跨账号 transfer、`rotate_secret_authority` 都会换新（#1886 D1；`daemon/impl/application/actors/transfer.py:168-172`）。

建议：

1. grant 的 `agent = str(UUID(hex=entity_token))`。这与 `entity_token` 一一对应，`agent_id` 可以从它推出，
   也和现有 `agent_secret_grants` 用 `(agent, entity_token)` 做化身围栏（计划 C6）一致。`entityToken` 本来就出现在 `Agent.to_json` 里，不是秘密。
2. 寻址和展示用 `agentId`；签名结构里只放 UUID 形式，不放 `a.` 形式，也不放名字。
3. **HQ 裁定 F-3**：销毁重建会换新化身，旧 grant 随之失效，这是预期行为（#1886 D1）。
   move 不改变化身标识，绑定随 agent 走（#2078 R9）。但 move 到目标域后，能否取钥由目标域的 OrgPolicy 重新判定，不自动生效
   （与 HQ 给 move 线 codex-sw 的裁定相同）。`receipt.owner` 始终取自 grant。
4. 本阶段 recipient 只放主人自己的设备，跨域取钥（OrgPolicy 判定 + R2 投递）属于 M1，不在第 2、3 步实现。

### 9.2 存储归属（对齐 #2323 cell；F-4 已裁）

- HQ 裁定：provider 记录与 S3 owner scope 共用同一个空间，经 `OwnerStorePort` 访问（§4.1）。r1/r2 的“不放 owner-home”建议作废。
- provider 记录仍不是 cell，也不铸 cell id；路径为 §4.2 的 `directory/provider-*`。阶段 B 若要让钥或授权成为 cell，由 cell 引用 `keyId` 或 `(owner, agent, grantId)`，不复制权威记录。
- 遗留风险（交 #2323）：#2323 计划自己登记了“同一主人两台设备各建一个 owner-home 空间、读者取并集”（plan:211）。
  对撤销记录来说，取并集是不能接受的语义。provider 侧的防护：seq 单调、revoked 只能被更高 seq 的显式 allow 覆盖、读取侧逐条验签（§4.3–§4.5）。
  空间唯一性本身由 S3 负责。

### 9.3 R2：跨用户宿主的受限密文读取

采用 #2078 计划 §8.1 R8 的裁定方向：**B 不读 A 的个人 org，由 A 的节点在启动时直接投递**。协议草案（不在本阶段实现）：

- B 的 daemon 向 A 的任一在线设备发 peer-node 请求 `provider.envelope.fetch {agent, provider, recipientKeyId}`。
  A 的设备核对：最新 grant 状态是 active；请求方设备就是 grant 里 recipientKeyId 对应的 host 钥持有者（经 R6 的 host 锚验证）。
  核对通过后，返回 `{envelope, grantState}`，两者都已签名。
- B 只在内存里缓存，不落盘；元数据可见范围仅限这一份信封和这一条 grant。
- A 的设备都离线时，B 报 `PROVIDER_KEY_RECIPIENT_PENDING` 并等待（PK-4），绝不改用 B 的钥。
- 依赖 R6（host 签发者）和 transport 线的 peer-node 方法。冻结时间放在 M1 之前。

### 9.4 R3：授权的权威 org

**已由 Q7=A 关闭**：唯一权威是 key 主人的个人 org。agent 属于哪些工作 org、宿主是谁、current org 是什么，
都不改变 grant 的位置（#2078 H1 交接验收第 1 条）。Q3 已裁“只看 grant，不看 org”，所以跨 org 的 agent 也能被授权。
需要 #2078 交付的：`personal_org_for(owner)`，以及受保护分区的 admission hook 接点。

### 9.5 R4：撤销的新鲜度

建议采用“本地强一致 + 限时 grant”两层：

1. **本机读自己写。** 启动时持 `(agent, provider)` 的本机锁读取本地副本。本机发出的 revoke 立刻生效。
2. **限时 grant。** `expiresAt` 必填，默认 24h（可配 1h–7d）。主人设备在半衰期自动续签（seq+1）；主人设备本机启动时可以就地续签。
   其他设备上发出的 revoke 需要经过 OrgFS 同步；最坏情况下，撤销在 TTL 内生效。这是给出的上界，不靠“没看到撤销就当没撤销”。
3. **启动侧的映射**：grant 过期且无法续签（主人设备不在线）时报 `PROVIDER_KEY_RECIPIENT_PENDING`（等主人），
   不报 `PROVIDER_KEY_SIGNATURE_INVALID`，避免把“需要等待”误报成“被篡改”。存储或锚不可用时报 `PROVIDER_STORE_UNAVAILABLE`，不报 missing。
4. **回滚防护**：准入要求 seq 单调（§4.5），并且 grant 绑定 dekVersion，因此旧签名的重放不会生效。
5. 边界（计划 R9）：以上只约束守规的 daemon。已经注入进进程的明文、恶意宿主保存下来的明文，只能靠厂商侧换钥。

本阶段（宿主就是主人自己的设备）实际只用到第 1 层，第 2 层在 G1 就写好，供 M1 使用。

### 9.6 其他

- **R6**：本阶段不使用 host 钥，recipient 只放主人设备，所以 G1/G2a 不依赖 R6。
- **UK 门**：record/envelope/grant 三个 purpose 在生产中签名，需要 daemon 内部签名端口（origin daemon），并在 `purposes.json` 登记。
  在 UK 门打开之前，`activeProductionPurposes` 保持为空，功能门保持 `legacy`。

## 10. 对 #2328 的影响清单（交 HQ 协调）

以 #2328 @`e81a2db47` 为准。第 3 步会在本分支实现，完成时再更新这份清单。

| # | 文件或位置 | 影响 | 建议解决方式 |
| --- | --- | --- | --- |
| 1 | `request_catalog/registry.py` `MIGRATED_FAMILIES` | 本分支是 `{daemon, link, userKey}` 再加 `user`；#2328 是 `{daemon, link, hosts, log, user-proxy}` | 取并集 |
| 2 | 同文件 `_build_registry` 与 `BoundMethod.preauthorize` | 本分支追加 `PROVIDER_KEY_BINDINGS`，并新增 preauthorize 钩子；#2328 改写了 import 路径和绑定元组（含 `SECRET_WRITE_BINDINGS`） | 按 #2328 的结构合，本分支的两处都只追加 |
| 3 | `application/actors/agents_admin/secrets/__init__.py:17` `_handle_agent_secret` | 适配层调用点（§8.6）。release 基线上对应 `registry_ops.py` 的分发 | 在函数开头加三行调用 `provider_keys.legacy.dispatch`；由 #2328 owner 加，或由 HQ 在集成时加。本分支不改这个文件 |
| 4 | `settings_contracts/credentials/__init__.py`（provider-write/grant）、`request_catalog/agent/__init__.py` 的 `AGENT_SECRET_LIST_SPEC` | 不改。适配层只返回已声明字段和码（§8.5）；list 映射到已声明的 `AGENT_ERROR` | 无需改动。可选：#2328 owner 给 list 补一行 `AGENT_SECRET_RESOLUTION_FAILED`，适配层随即改用它 |
| 5 | `expectedAgent` / `AGENT_INCARNATION_MISMATCH` | list 和 grant 都保留，规则见 §8.3，并额外按 entity_token 的 UUID 绑定 | 本分支在 `legacy.py` 里经 identity 公共面实现同一规则，不依赖 #2328 的私有函数 |
| 6 | `contract/daemon-request/openapi.json`、`contract/settings-request/openapi.json` | 两边都会重新生成 | 合并后重跑 dump 脚本，不手工合并 |
| 7 | `contract/cli/surface.json`、`docs/reference/cli-command-inventory.json` | 本分支只追加 `user provider *` 和 `agent provider allow/revoke/status` | 合并后重新生成 |
| 8 | `tests/daemon/ipc/contracts/test_method_specs.py` | 两边改的是同一条 `MIGRATED_FAMILIES` 断言 | 取并集 |
| 9 | `kernel/impl/contracts/daemon/ipc_errors.py` 及其测试 | 本分支追加 `PROVIDER_STORE_*`（待 F-1 批准） | 只追加 |
| 10 | `daemon/interface.py` | 本分支追加 `ProviderKeys` 门面 | 只追加 |
| 11 | GUI 阶段（gui-v1 合入之后） | 七个新方法打开 `http_exposed`（§5.3）；GUI 从旧兼容面切到新方法 | 由 GUI 阶段的 PR 做，不在本分支 |

本分支不改 `gui/**`，也不改 #2328 自己新增的 settings 后端文件。

## 11. HQ 裁定（2026-11-03，冻结）

r2（`860dcf8d3`）对 4 项差异的回应全部接受。F-1 到 F-10 的裁定如下，本文据此冻结：

| ID | 裁定 |
| --- | --- |
| F-1 | 同意新增 `PROVIDER_STORE_UNAVAILABLE`、`PROVIDER_STORE_CONFLICT`，同步 `contract/` 和错误码注册表 |
| F-2 | 同意新增 purpose `hyprial/provider-key/record/v1`。编码落地时 HQ 单独派安全评审，范围：签名覆盖哪些字段、防重放、拒绝回滚、轮换。**安全评审不过不能合** |
| F-3 | 不冲突，统一如下：grant 绑定化身标识（entity_token 对应的 UUID，即 `agent_id` 的来源）。销毁重建换新化身后，旧 grant 失效，这是预期行为（与 #1886 D1 一致）。move 不改变化身标识，所以绑定随 agent 走（与 #2078 R9 一致）；但 move 到目标域后，能否取钥由目标域的 OrgPolicy 重新判定，不自动生效（与 HQ 给 move 线 codex-sw 的裁定相同） |
| F-4 | 不同意两套存储位置：必须与阶段 A S3 的 owner scope 用同一个空间（#2323 方案 §1.1）。经存储端口访问；S3 落地前先接个人 org 目录空间，但只走这个端口；S3 落地后切过去，provider key 代码不改（§4.1） |
| F-5 | 同意“本地强一致 + 24h 限时 grant”。到期后续期的路径要有测试 |
| F-6 | 同意功能门默认 `legacy` |
| F-7 | 同意 OAuth 和 default 的旧入口本阶段不转接 |
| F-8 | 同意两处形状变更：`grantId` 槽位（新 API 默认 `grantId = provider`）、`legacyRef`、`awaiting-key` 状态 |
| F-9 | 同意 GUI 准入方案，但 `http_exposed` 要等 gui-v1 合入 release 之后再打开。“HTTP 壳经本地 socket 转发、来源记为 ipc、preauthorize 照常生效、worker 和交互会话仍被拒绝”这几条必须有测试证明。**2026-11-10 更新：产品决定提前打开；GUI 与 CLI daemon 独立运行，不做 legacy Key 兼容。七个 spec 已改 `http_exposed=True`，来源/拒绝测试保留。** |
| F-10 | 同意 list 用已声明的 `AGENT_ERROR` 映射 |

自主推进授权（HQ，2026-11-03）：第 2、3 步自主推进至全部完成，不逐片等 HQ 回复。只有三种情况停下等 HQ：
① 需要改动已冻结的契约（F-1～F-10）；② 需要改动 #2328 或 #2323 的文件，且不是追加；③ 安全相关代码（签名、加密、授权检查）每完成一片，在 review comment 里标 `[security]`，由 HQ 集中派安全评审。
每片发 review comment 汇报；全部完成后在 #2195 发 “ready for HQ alignment”。

## 12. 本次交付验证记录

- PASS：读完设计、实施计划（含 §8.1 R1）、`contract/identity/provider-keys.md`、`contract/identity/user-key.md`、#2078 设计与计划的相关节。
- PASS：在上述 SHA 上只读核对了 #1886、#2323、#2328 的相关事实，并在文中给出引用。r2 按 #2328 @`e81a2db47` 重新核对了
  `AGENT_SECRET_LIST_SPEC` 的错误元组、secret 写方法的 `_ERRORS`、`_handle_agent_secret` 的 `expectedAgent` 校验，
  以及首次设置流程对 `agent.secret.list` 行字段的精确匹配（`request_catalog/squire/operations.py:70-76`）。
- UNKNOWN：未改代码，未跑产品测试；本文的接口在冻结之前不实现。
- Contract-Impact：仅设计文档，不改已发布接口；冻结之后，第 2 步按本文同步 `contract/identity/provider-keys.md`、openapi 和 CLI surface。
- Skill-Impact：not-needed：本提交不改变 agent 操作方式。

## 13. 修订记录

### r2（2026-11-03，回应 settings 线 #2328 @`e81a2db47` 的 4 项差异，以及 #2313 S10）

| 差异 | 处理 | 位置 |
| --- | --- | --- |
| 1. 新方法 HTTP=false、preauthorize 只限 IPC，GUI 如何准入，旧兼容面如何转接 | 冻结 GUI 准入方式：GUI 阶段打开 `http_exposed` 并绑定 `IdentityBinding("none")`；HTTP 壳经本地 socket 转发，origin 为 `ipc`，preauthorize 照常适用；秘钥字段声明 `writeOnly`。旧兼容面在处理函数开头统一转接，覆盖 HTTP、内部调用和旧 IPC | §5.3、§8.6 |
| 2. `AGENT_SECRET_RESOLUTION_FAILED` 没有在 `agent.secret.list` 声明 | 改为逐方法映射：list 用已声明的 `AGENT_ERROR`；provider-write/grant 继续用已声明的 `AGENT_SECRET_RESOLUTION_FAILED` | §8.5 |
| 3. 本地索引丢失后无法恢复 grantId/revision | 删除本地索引；`legacyRef` 写进签名记录，从 OrgFS 读回即逐字段还原；给出通用的授权恢复表（换设备、重建、重复提交、钥轮换、验签失败） | §3.2–§3.4、§8.4 |
| 4. list 和 grant 都要保留 `expectedAgent` 校验 | 两者都保留，并按同一份 registry 快照里的 entity_token 绑定读写；CAS 前复核；新方法也接受同一参数 | §8.3、§5.2 |
| 引用旧 head `88196c5f0` | 全部改为 `e81a2db47`；`secret.list` 引用改为 `request_catalog/agent/__init__.py` | §0、§8.5、§10 |
| #2313 S10：squire 是普通 agent | 契约里不出现 squire 专用的字段、方法或回退路径；首次设置流程只作为旧 list 的普通调用方出现 | §8.1、§8.4 |

r2 新增的、改变 r1 形状的内容：grant 槽位从 `provider` 改为 `grantId`（新 API 默认 `grantId = provider`），路径随之变为
`directory/provider-grants/<agent>/<grantId>.json`；新增 `awaiting-key` 状态；记录增加 `legacyRef`；
`agent.provider.status` 的结果字段从 `providers` 改为 `grants`；三个 `agent.provider.*` 方法增加可选 `expectedAgent`。

### r3（2026-11-03，HQ 裁定，冻结）

- 状态改为 FROZEN；§11 改为裁定记录。
- F-3：§9.1 第 3、4 条按裁定改写。化身绑定随 move 保留；跨域取钥由目标域 OrgPolicy 重新判定；重建换化身后旧 grant 失效。
- F-4：§4.1 改为经 `OwnerStorePort` 访问 S3 owner scope 的同一空间；S3 前过渡到个人 org 目录空间；§9.2 的“不用 owner-home”作废。
  核对结果：按 #2323 计划 §2.2，owner-home（`CellSpaces.private_space`）与个人 org 目录空间不是同一个空间，因此在 #2195 报告这一差异，不在 #2323 留“相同”确认。
- F-5：续期路径列入第 2 步测试。F-9：`http_exposed` 推迟到 gui-v1 合入 release 之后；HTTP 转发来源与拒绝规则的测试列入第 2 步。
