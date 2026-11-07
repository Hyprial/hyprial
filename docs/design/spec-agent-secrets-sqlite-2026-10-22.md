规格：`hyprial agent secret` —— 凭据存入 agent home 的 sqlite，操作留 log  2026-10-22

状态：**规格交付，未实现**。本文不是「已实现」或「已验证」的声明。
依据：Allen 2026-10-22 决定 —— 「纯粹的 hyprial cli 研发事务，你来决定」、
**「不要考虑过于复杂的安全限制，当前唯一的安全要求是操作有记录」**。
⇒ 本规格的最小目标只有一条：**每一次对凭据的写、删、轮换、注入都留下一条不可改写的记录**。
权限模型、批准流程、外部审计落地、文件交付、跨 agent 共享，**明确不做**（见 §8）。

## 1. 为什么换

现状（`src/hyprial/identity/impl/agents/runtime/secrets.py`）：

- `SECRET_ENVIRONMENT_NAMES`（`:42`）是硬编码的 8 个模型厂商 env 名；
- 不在集合里 ⇒ `:142` 抛 `secret environment names must be explicitly approved`，
  且发生在 `SecretGrant.__post_init__`，即**铸 grant 时就失败**，CLI 无绕过；
- 交付路径只有一条：`registry_ops.py:890-901` 把 `secret.environment()` 写进子进程 env。

结果：非模型凭据（Cloudflare/R2、Forgejo、registry、SMTP）**没有受许路径**，只能手工抄成
文件。实测 8 个 agent 各自 `secrets/` 下有一份 0600 的 `forgejo-token`，**8 个值互不相同**
（hash 实测）—— 各自独立的身份凭据，不是同一把的副本。

⇒ 缺陷是**交付通道**，不是明文落盘本身。加名字不对（每次发版、且把解释权留在常量里），
换成本规格的存储 + 记录。

## 2. 存储

```
<agent home>/secrets.sqlite      文件 0600，父目录 0700
```

```sql
CREATE TABLE secrets (
  service        TEXT    NOT NULL,          -- 例 'forgejo' / 'cloudflare-r2' / 'smtp'
  field          TEXT    NOT NULL,          -- 也即注入时的 env 名（见 §4）
  value          TEXT    NOT NULL,          -- 明文；本规格接受明文落盘（0600）
  fingerprint    TEXT    NOT NULL,          -- 值的派生，见 §3
  updated_at_ms  INTEGER NOT NULL,
  PRIMARY KEY (service, field)
);

CREATE TABLE secret_events (
  seq         INTEGER PRIMARY KEY AUTOINCREMENT,
  at_ms       INTEGER NOT NULL,
  actor       TEXT    NOT NULL,             -- agent 名，或 'user:<owner>'
  via         TEXT    NOT NULL,             -- 'cli' | 'daemon'
  action      TEXT    NOT NULL,             -- set | rotate | remove | materialize | rejected
  service     TEXT    NOT NULL,
  field       TEXT,                         -- materialize 时为逗号分隔的注入清单
  fingerprint TEXT,                         -- 值的派生；⛔ 永不写明文
  result      TEXT    NOT NULL              -- ok | rejected:<reason>
);

CREATE TRIGGER secret_events_no_update BEFORE UPDATE ON secret_events
BEGIN SELECT RAISE(ABORT, 'secret_events is append-only'); END;

CREATE TRIGGER secret_events_no_delete BEFORE DELETE ON secret_events
BEGIN SELECT RAISE(ABORT, 'secret_events is append-only'); END;
```

`(service, field)` 是键值行，⛔ 三元组 `<service, token_id, token_secret>`：
R2 需要 account_id + access_key + secret_key，AWS 需要 key + secret + region，
SMTP 需要 host/port/user/pass，Forgejo 需要 base_url + token。

## 3. fingerprint

```
高熵值（token / api key，长度 ≥ 16）   sha256(value) 前 16 位
低熵值（密码、pin 等）                 ⛔ 不许裸 sha256（可字典反查）
                                       记 "len=<n>,tail=<末 4 位>"
```

用途只有两个，都不需要知道值：**本次写入与上次是否同一个值**、**轮换后旧值是否再次出现**。

## 4. 注入（materialize）

- `field` 直接作为子进程 env 名（大写，`^[A-Z][A-Z0-9_]*$`）。
- 保留名（**正确性问题，不是权限设计**）：harness 与操作系统自己解释的名字不许物化 ——
  `PATH`、`HOME`、`HYPRIAL_*`、`CODEX_HOME`、`LANG`、`TMPDIR`。写入时拒绝并记
  `action=rejected, result=rejected:reserved-name`。
- 每次启动注入一条 `action=materialize` 事件，`field` 记注入了哪些列（只记名字）。
- ⛔ 没有对应 `set` 事件的行不物化（见 §6 验收 5）。
- 只做 env 注入；**文件交付不在 v1 范围**。

## 5. CLI 面（最小集合）

```
hyprial agent secret set     --agent A --service S --field F            value via stdin
hyprial agent secret set     --agent A --service S --field F1 --field F2  （同一 service 多字段）
hyprial agent secret list    --agent A [--service S]                   只列 service/field/fingerprint/updated_at
hyprial agent secret rotate  --agent A --service S --field F           set + action=rotate
hyprial agent secret remove  --agent A --service S [--field F]          删行 + action=remove
hyprial agent secret history --agent A [--service S]                   读 secret_events，只读
```

- 值只走 stdin（沿用现有 `provider-write` 的惯例：拒绝 TTY，不进 argv，不进日志）。
- `--service` 的 validator 是**声明式**的：声明该服务必需哪些 field、各自形态
  （例：`cloudflare-r2` 需要 `CLOUDFLARE_ACCOUNT_ID` + `CLOUDFLARE_API_TOKEN`）。
  有 validator 时缺字段直接拒绝。
- **validator 只做形态，⛔ 能力**。实测反例（infra-ops，Cloudflare R2，今晚第三例同族）：
  桶级 token **创建成功**、策略回读正确，但拿它 PUT 对象得 **403**
  ⇒ 「创建成功」与「可用」是两件事，与「回执 ≠ 落地」同族。
  ⇒ 因此 validator 的返回值不得写成「可用」；能力只在第一次真实使用时被证明，
  本规格不为「能力探测」新增机制（若将来要，另开一格 `capability_probe`，
  且它必须是一次最小真实读/写，⛔ 一次元数据读回）。
- **`capability_probe` 的结果至少三分类，⛔ 二分类**（否则会把三种不同的事读成一种）：
  ```
  credential-invalid     凭据本身无效（401/403 且与操作无关）
  target-missing         凭据有效、目标不存在（例：worker 未创建）
  operation-not-permitted 凭据有效、目标在、但不允许这次操作
  ```
  实侧依据（infra-ops，2026-10-22 交付部署 token 时的自验）：`wrangler whoami` rc=0（凭据有效）
  与 `deployments list` 返回 "This Worker does not exist"（目标缺失）**同时成立**
  —— 一个只问「能不能连上」的探针会把后者读成前者。
- 三条同族的读数纪律（可并进任一验收）：**列表 ≠ 存在**、**创建 ≠ 可用**、**可解析 ≠ 合法**，
  再加本条 **凭据可用 ≠ 目标存在**。
- **无 validator 时**：默认拒绝，`--no-validate` 显式放行；放行时写
  `action=set, result=ok, ` 并在 stderr 打一行
  `warning: no validator for service 'x'; fields written unvalidated`，
  同时把 `credential_unvalidated_total` 加一（永久累加，每日写零行；与
  `credential_exception_total` 同一套计数习惯）。

## 5.5 撤销语义（「吊销 ≠ 删除」是本规格的一条否决条件）

实测两次发现「值在凭据生命结束后仍留在盘上」：

```
1  state.sqlite3.retired 里躺着 1 个 cfut_（infra-ops 实测，/user/tokens/verify 得 401）
2  secrets/providers/infra-ops-cf-r2create-20261023.json 静置保存**已吊销**的 cfut_
   ⇒ 这一条更严重：它发生在**受许路径自己的存储里**
```

⇒ 规则：**任何使凭据失效的动作，其完成态必须是「值不在盘上」**，⛔ 「标记为 revoked 仍然留着值」。

```
撤销 = 远端作废 + 本地删除值（只保留 fingerprint 与事件）
      两步各写一条事件：action=revoke_remote / action=remove
      只有两者都在，撤销才算完成；只做第一步的状态可被查询检出
轮换 = 新值 set + 旧值从原行覆盖（不产生 .bak / .retired / .old）
删除 = 行消失（值随之消失），事件保留
```

**不设保留窗口**（`revoked_hold_until` 一类被明确否决，理由由 infra-ops 给出）：

```
回滚窗口在「何时 revoke」—— 旧值仍然有效的那一刻；
不在「revoke 之后还留多久」。
保留已吊销的值 = 把第四种状态（值还在、被读作已作废）原样带回来。
若要保留撤销前的值以便回滚,那应当由「暂不 revoke」表达,⛔ 由「revoke 后留着」表达。
```

⇒ 因此 `secrets` 表**没有 revoked 状态**：一行活着就是可物化的值，不活就是不存在。
状态词只会制造「值还在但被读作已作废」的第四种情况，而那正是上面两处发现的形状。

## 6. 验收（每条都能在修复前取到假值）

1. `set` 后：`secrets` 有行，`secret_events` 有 `action=set`；**对 sqlite 全文 grep 该值，
   `secret_events` 命中 0**。
2. `UPDATE`/`DELETE secret_events` ⇒ `RAISE(ABORT)` 生效（两个触发器各一条测试）。
3. `rotate` 后：`fingerprint` 变；旧 fingerprint 在 `secrets` 与 `secret_events` 里都只作为
   历史出现（不冒充当前值）；**再对 `~/.hyprial` 全文 grep 旧值 ⇒ 0 命中**（这条与
   infra-ops 的 `.retired` 发现同源：改名不是删除）。
4. `remove` 后：行消失、事件在；随后启动 **不注入**该 field。
5. 直接用 SQL 插入一行没有 `set` 事件的 secret ⇒ 启动**拒绝物化**该行并记
   `action=rejected, result=rejected:no-write-event`。
6. 保留名（§4）写入 ⇒ 拒绝 + 事件。
7. 无 validator + 无 `--no-validate` ⇒ 拒绝；带 `--no-validate` ⇒ 成功 + 计数 +1。
8. 撤销：远端作废后执行本地删除 ⇒ 该值在 `secrets` 与全文 grep 里 0 命中，事件里同时有
   `revoke_remote` 与 `remove` 两条；**只做 `revoke_remote` 不删值 ⇒ 该行仍可物化**，
   测试须断言这个「未完成态可被检出」，⛔ 断言它「已撤销所以安全」。
   8b. **`revoke_remote` 事件不得让任何读路径认为值已清**：以事件推导「该 secret 是否仍在」
   的实现只允许认 `remove`；测试须构造「只有 `revoke_remote`、行还在」的状态，断言
   「按事件推导」与「按表推导」给出同一个答案（都在），⛔ 一个说没了、一个说还在。
9. 轮换/撤销之后：`~/.hyprial` 下**不得出现**该值的 `.bak` / `.retired` / `.old` 副本
   （与验收 3 同一判据，作用于撤销路径）。

## 7. 迁移

- v1 **不自动扫描、不自动搬迁**现存手抄文件：8 个值互不相同、身份归属尚未核验
  （`git.internal:443` 当时不可达），自动搬迁会把「不知道属于哪个账号的值」写进受管位置。
- 路径是 owner 用 `agent secret set` 逐条导入，导完自行删除旧文件；导入期间旧文件保留。
- 每条导入都自然留下 `actor=user:<owner>` 的 `set` 事件 —— 这就是它的来源记录。

## 8. 明确不做（避免规格被读成更大的东西）

```
⛔ 权限模型 / grant / 批准流程     —— 批准 = 写入被记录（Allen 决定）
⛔ 外部审计落地 / 异地副本        —— log 的寿命等于文件寿命（见 §9）
⛔ 文件交付 / 跨 agent 共享       —— v1 只做 env 注入，一份 sqlite 只放该 agent 自己的凭据
⛔ 值加密                        —— 明文 + 0600，现状不更坏；加密不是本次目标
⛔ 自动轮换定时器                —— 轮换由 owner/agent 触发，本规格只保证它被记录
```

## 9. 已知边界（写进文档，⛔ 假装没有）

- **log 寿命 = 文件寿命**：删掉 `secrets.sqlite` 等于删掉 log。本规格不防这件事。
  将来若需要，另做外部落地；**⛔ 不要把它当成防篡改审计**。
- `secrets` 表里的值是明文；`secret_events` 里永不明文。
- 已执行的清理（infra-ops，非本规格实现的一部分）：`providers/infra-ops-cf-r2create-20261023.json`
  已删除（值已吊销、无 grant 引用）；该目录在我复核时 `epoch 1792730894` 已为空。
- 一份 sqlite 只应含**该 agent 自己的**凭据：写入位置在 agent home，该 agent 进程可读全部行。
  ⛔ 不做共享一份。

## 10. 影响面

```
identity 面     SecretGrant / SECRET_ENVIRONMENT_NAMES 的校验路径（删枚举、保留保留名）
launch 面       registry_ops.py:890-901 的注入点改读 sqlite + 写 materialize 事件
transfer 面     transfer/landing/credentials.py:351 也引用同一常量，需同批改，否则落地侧给出第二种判断
CLI 面          新增 agent secret set/rotate/remove/history（现有 grant 命令退役或保留为兼容壳）
```

## 11. 威胁模型与 `path_authz` 的关系（本节由 hq-adjutant 提供坐标，我复核过）

```
策略      src/hyprial/daemon/impl/path_authz/policy.py:175  session_policy()
         —— 以该 agent 自己的 workspace 与登记的 cwd 为根；拒绝 `/`、`$HOME`、
            以及任何包含 hyprial home 的目录当根
强制点    application/messaging/orgfs_bridge.py        read_authorized_file / write_authorized_file
         application/messaging/inbox_surface/send.py:593  "Which local files this caller may attach"
         network/route_delivery.py:98                    "One authorized local resource for a proactive Lark route send"
```

⇒ 它拦的是**「daemon 代读代写与路径交付」**，⛔ 「agent 自己 cat」。后者由操作系统按 uid
放行，任何同 uid 进程都能读任一 agent 的明文。

⇒ 入档字面文本（由 hq-adjutant 给定，原样采用，我不改词）：

```
防：跨 uid / daemon 代读代写与路径交付 / 命令面留痕
不防：同 uid 直接读、读取不产生事件
不含：独立 uid、keychain ACL、值加密、异地审计
```

⇒ 这两个词不能混：写成**缺陷**会引来一个无法在 uid 内关闭的工单（见 §2 的封闭结论）；
写成**边界**则同时说了“它防到哪里”与“它不防什么”。

### 11.2 `route_delivery` 的负控已实测（不是"未测"）

`route_delivery.parse_route_resources()` 的附件路径走 `read_authorized_file(raw, policy, …)`；
`policy` 来自 `inbox_surface/send.py:591 _attachment_policy()`，它只会返回三种值：
`operator_policy` / `session_policy` / `None`（不允许附件）。三种都测过：

```
operator -> /tmp 下的小文件（hyprial home 之外）   ALLOWED            正控
operator -> 另一 agent 的 secrets/                REFUSED other-agent-home
session  -> 另一 agent 的 secrets/                REFUSED other-agent-home
session  -> 自己 workspace 里的文件                ALLOWED            正控
```

⇒ 探针 `epoch 1792730842`，用 `uv run python` 直接调用 dev 上的 `path_authz`。

**读数纪律（hq-adjutant 2026-10-22，两条，本节与其它控制一律照此写）：**

```
1  REFUSED 只有在带 reason 时才算读数；期望里必须写死具体 reason。
   反例:我第一次跑 operator 正控用 /etc/hosts 得到 REFUSED reason=too-large
   —— 那是 max_bytes 挡的,不是策略拒绝;若不写死 reason,
   「too-large」「otherwise」这类拒绝会与真拒绝一起把结论做对。
2  验收写成两行:（a）已测哪层  （b）未测哪层 + 触发条件。
   ⛔ 只写「已测」或只写「未测」——前者会把覆盖面读大,后者会被当成没做。
```
⇒ 结论：**在这条调用点的可达策略集内，没有第三个越权面**。`None` 情形按定义不带附件。
⇒ 两行式结论：

```
已测  策略层 + 调用点的组合:operator/session/None 三种可达策略,对另一 agent 的
      secrets 均 REFUSED reason=other-agent-home;两条正控通过
未测  一次真实的端到端主动 route 投递(会真的发消息)
      触发条件 = CLI secret 面在 intg/agent-secret 落地后,于集成环境跑一次真实投递并记录结果
      挂载位置 = WI M-hyprial-cli-developer-0001 的 log(已写)
```

### 11.1 一对正负控（可为 §5 的命令面各一条）

```
负控  以 agent A 的身份、通过 daemon 代发一条消息,附件路径指向
      ~/.hyprial/agents/B/secrets/…  ⇒ 必须 PathRefused（不是沉默、不是成功）
正控  同一路径换成 agent A 自己 workspace 内的文件 ⇒ 必须成功
      （证明策略不是“一律拒绝”,否则负控毫无信息）
```

只写负控不够：一个“什么都不允许”的实现会把负控跑绿，而它是不可用的。

实现归 backend（identity/launch 面）；本规格与 CLI 面由研发部定。

集成分支：`intg/agent-secret`。本规格与后续改动一律 base 在该分支，完成后再整体并入 dev。

落地标识（供其它部门引用触发条件时使用，⛔ 引用已关闭的 PR 号）：

```
分支    intg/agent-secret
PR      #1866        （#1865 已关闭，仅作记录，⛔ 作为触发条件引用）
WI      M-hyprial-cli-developer-0001   group-internal/work
机制上线的定义  intg/agent-secret 合入 dev 且 §5.5 的撤销语义在实现中可验
```
