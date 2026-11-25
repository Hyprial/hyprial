# hyprial model gateway（CF Worker）

会员入口 **`model.hyprial.ai`**：成员以自己的 **Casdoor 身份**调用模型，由本 Worker 验签 →
（AI Gateway binding）转发 → 记录用量。设计见 `hq/notes/impl-gateway-worker-skeleton-2026-10-12.md`（§9 = binding 定稿）与
`hq/notes/impl-gateway-quota-and-harness-wiring-2026-10-12.md`（P2 额度 / P4 harness）。

## 两个兼容面
| 路径 | 面向 | 说明 |
|---|---|---|
| `POST /v1/chat/completions`（+ `GET /v1/models`） | pi / codex 的 OpenAI 模式 | 模型 id 必须 `<provider>/<model>` |
| `POST /anthropic/v1/messages` | claude code | 必须透传 `anthropic-version` |

## ⛔ 三条硬约束（都来自已有实测，改代码时别绕过）
1. **Worker 不持任何 CF 凭据**：转发走 **AI Gateway binding**（预认证）；那把 `wrangler` token 只用于**部署**。
2. **关 CF 缓存**：binding 传 **`skipCache: true`**（§16 实测：CF 回放会连上一次 `usage` 一起回放）。
3. **metadata 由我们盖章**：`gateway.metadata = {org,user,req,...}`（≤5 项，⛔ 不得含 `cf.*`）；客户端给的 `x-hyprial-*` 只作标注、⛔ 不作授权。

## 本地自证（不需要 CF、不需要 npm install）
```bash
node test/core.test.ts        # 8 条：身份/metadata/usage 三段/计价(与 §16 实测 cost 反算一致)/路由/错误面/JWT 正反例
```

## 待核（⛔ 不猜，部署前确认）
1. ✅ **`jwks_uri` 已核**（2026-10-13 实测）：`https://auth.hyprial.com/.well-known/jwks` ⇒ 1 把 key、`kid=cert-built-in`、`kty=RSA`、**`alg=RS256`**（⇒ 验签器只需 RS256，与实现一致；OIDC 发现文档里列出的 ES256/RS512 等**实际未使用**）。
   ✅ **`audience` 也已核**（2026-10-13，infra-ops 从一枚真实 refresh token 只读解出）：成员 `hyprial login` 用的就是现有 **app-hyprial**（**不必新建 application**）⇒ `CASDOOR_AUDIENCE = "2cdd1defc1a0e219fafb"`（其 `clientId`）。
   ⚠️ **关键形态**：token 的 `aud` 是**数组** `["<clientId>"]` ⇒ 验签器必须支持数组分支（实现已支持，且有专门用例）。
2. ⚠️ **binding 是否支持流式**（`env.AI.run` 是结构化输入）：这是我们 harness 的主要形态 ⇒ **部署后第一个探针**；不支持则切 HTTP provider-native 端点 + Access 服务令牌（设计 §9-C）。
3. 兜底：`/v1/models` 目前原样透传，P3 再按组织过滤。

## 部署（归 infra-ops）
`npx wrangler deploy`（或 `--dry-run` 校验）；binding 要求 gateway 与 Worker **同账户**。

## 部署后探针（`probe.sh`）

```sh
HYPRIAL_TOKEN='<成员的 Casdoor 访问令牌>' ./probe.sh [base-url]
```

一次跑清两格：① **非流式**是否整条通（验签 → binding 转发 → 用量记录）；② ⭐ **流式是否逐块到达** —— 这是 P1 唯一未验的关键假设（binding 的结构化入参没有承诺支持 `stream`；不支持就切设计 §9-C：HTTP provider-native 端点 + Access 服务令牌）。

⚠️ **令牌必须是成员的 Casdoor 访问令牌**，⛔ **不是 CF API key**：本 Worker 验 RS256 签名 + `iss` + `aud`，用 CF key 一律 401，而那个 401 是"用错凭据"，不是"验签坏了"。⛔ 令牌只从环境变量读，不进 argv/日志。

⚠️ **P1 的已知边界（P2 项）**：本片**不区分 access / refresh token**（只验 `iss`/`aud`/`exp`/签名）。Casdoor 两种都是同一签名的 JWT，所以拿 refresh token 也能过 —— 语义上不该如此，P2 应加 `type`（或等价）校验。

## ⚠️ 两个实测硬事实（2026-10-13，生产）

1. **模型 id 必须用网关目录里真实存在的那个**：`anthropic/claude-sonnet-4-6`（连字符）**不存在** ⇒ 网关返回 `404`，而日志里表现为 **`provider=unknown` + `status_code=404`**。
   ✅ 正确写法是 **`anthropic/claude-sonnet-4.6`（点）**。判定口诀：**`provider=unknown` ⇒ 不是上游故障、是模型名没对上目录**。
2. ⭐⭐ **更正（2026-10-13 晚）：binding **会**返回流 —— 是我的 Worker 把它弄没了。**
   实测（一次性探针 Worker 直测 `env.AI.run`）：`stream: true` 时返回 **`ReadableStream`**
   （`isStream:true, ctor:"ReadableStream"`，内容为 `chat.completion.chunk`）。
   早先观测到的"HTTP 200 + 空对象 `{}`"**是本 Worker 的缺陷**：我对结果做了 `JSON.stringify(out)`，
   而 `JSON.stringify(ReadableStream)` 恰好是 `"{}"` ⇒ 静默空成功的来源在此，⛔ 不在 binding。
   ⇒ 现已改为**原样透传**（`new Response(stream)`，`text/event-stream`），并把基于错误结论加的 501 拒收**去掉**；
   流式下 usage 在末块（本片不解析，成本以 CF 网关日志为准）。
   ⇒ **设计 §9-C（Access 服务令牌）因此不必做**，Worker"零 CF 凭据"的属性保住。

