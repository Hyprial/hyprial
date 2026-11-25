// 会员入口 Worker：验签 → 转发（AI binding）→ 记录用量。
// 设计：hq/notes/impl-gateway-worker-skeleton-2026-10-12.md（P1 骨架；§9 binding 定稿）
import { identityFromClaims, contextFromHeaders, gatewayMetadata, type Identity } from "./identity.ts";
import { parseUsage, estimateCostUsd } from "./usage.ts";
import { surfaceForPath, normalizeModel, errorResponse } from "./routing.ts";
import { fetchJwks, verifyJwt, type Jwks } from "./jwt.ts";

type BindingRun = (
  model: string,
  input: unknown,
  opts?: { gateway?: { id: string; skipCache?: boolean; collectLog?: boolean; metadata?: Record<string, string> } },
) => Promise<Record<string, unknown>>;

export interface Env {
  AI: { run: BindingRun; aiGatewayLogId?: string };
  GATEWAY_ID: string;
  CASDOOR_JWKS_URL: string;
  CASDOOR_ISSUER: string;
  CASDOOR_AUDIENCE?: string;
  USAGE_SINK?: string; // P2：控制面地址（P1 可空 ⇒ 只打结构化日志）
}

let jwksCache: { at: number; jwks: Jwks } | null = null;
async function getJwks(env: Env): Promise<Jwks> {
  const now = Date.now();
  if (jwksCache && now - jwksCache.at < 300_000) return jwksCache.jwks;
  // ✅ CASDOOR_JWKS_URL 已实测为真实 JWKS 端点（不是发现文档）⇒ 直接拉
  const jwks = await fetchJwks(env.CASDOOR_JWKS_URL);
  jwksCache = { at: now, jwks };
  return jwks;
}

async function recordUsage(env: Env, rec: Record<string, unknown>): Promise<void> {
  // ⛔ 不含任何凭据/token 内容；P1 只记不扣
  console.log(JSON.stringify({ ev: "model.usage", ...rec }));
  if (env.USAGE_SINK) {
    try {
      await fetch(env.USAGE_SINK, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(rec) });
    } catch {
      // 写回失败 ⇒ 交给对账补（设计 §1 的 ⑦），⛔ 不影响本次响应
    }
  }
}

export default {
  async fetch(request: Request, env: Env, ctx: ExecutionContext): Promise<Response> {
    const url = new URL(request.url);
    if (url.pathname === "/healthz") return new Response("ok");

    const surface = surfaceForPath(url.pathname);
    if (!surface) return errorResponse("BAD_REQUEST", `unsupported path ${url.pathname}`);

    // ① 凭据：成员只带 Casdoor 身份
    const auth = request.headers.get("authorization") ?? "";
    const token = auth.startsWith("Bearer ") ? auth.slice(7) : "";
    if (!token) return errorResponse("UNAUTHENTICATED", "missing bearer token");

    // ② 验签（fail-closed：JWKS 拉不到 / 签名不对 / 过期 ⇒ 全拒）
    let identity: Identity;
    try {
      const jwks = await getJwks(env);
      const claims = await verifyJwt(token, jwks, {
        issuer: env.CASDOOR_ISSUER,
        audience: env.CASDOOR_AUDIENCE,
        // ⭐ 只认访问令牌；refresh token 不得当 API 凭据（判别字段实测是 tokenType，⛔ 不是 type）
        allowedTokenTypes: ["access-token"],
      });
      identity = { ...identityFromClaims(claims), ...contextFromHeaders(request.headers) };
    } catch (e) {
      return errorResponse("UNAUTHENTICATED", (e as Error).message);
    }

    // ③ `GET /v1/models`：P1 原样透传（P3 再按组织过滤）
    if (url.pathname === "/v1/models") {
      return new Response(JSON.stringify({ object: "list", data: [] }), { headers: { "content-type": "application/json" } });
    }

    // ④ 入参：模型 id 必须 `<provider>/<model>`；anthropic 面要求 anthropic-version
    let body: Record<string, unknown>;
    try {
      body = (await request.json()) as Record<string, unknown>;
    } catch {
      return errorResponse("BAD_REQUEST", "body must be JSON");
    }
    let model: string;
    try {
      model = normalizeModel(body.model);
    } catch (e) {
      return errorResponse("BAD_REQUEST", (e as Error).message);
    }
    if (surface === "anthropic" && !request.headers.get("anthropic-version")) {
      return errorResponse("BAD_REQUEST", "anthropic-version header is required");
    }

    // ⑤ 转发：AI binding（⭐ 预认证、无需凭据）＋ ⛔ 关 CF 缓存 ＋ ⭐ 可信 metadata 由我们盖章
    const reqId = crypto.randomUUID();
    const started = Date.now();
    let out: unknown;
    try {
      out = await env.AI.run(model, body, {
        gateway: { id: env.GATEWAY_ID, skipCache: true, collectLog: true, metadata: gatewayMetadata(identity, reqId) },
      });
    } catch (e) {
      return errorResponse("UPSTREAM_ERROR", (e as Error).message);
    }

    // ⭐⭐ 2026-10-13 **更正**（推翻我当天早先的结论）：流式请求时 binding 返回的是一个
    // **`ReadableStream`** —— **它会流**。早先看到的"HTTP 200 + 空对象 `{}`"**是本 Worker 的
    // 缺陷**：我对结果做了 `JSON.stringify(out)`，而 `JSON.stringify(ReadableStream)` 恰好就是
    // `"{}"` ⇒ 那个静默空成功的来源在此，⛔ 不在 binding。
    // 证据：一次性探针 Worker 直测 binding ⇒ `isStream:true, ctor:"ReadableStream"`（含 chat.completion.chunk）。
    // ⇒ 流式必须**原样透传**（并把此前基于错误结论加的 501 拒收去掉）。
    if (out instanceof ReadableStream) {
      // ⚠️ 流式下 usage 在**末块**（SSE），本片不解析 ⇒ 记录里留空、成本以 **CF 网关日志**为准
      //    （设计 §1 的"甲"口径）；P2 可改 tee 一份出来解析。⛔ 但绝不因此丢掉整条记录。
      ctx.waitUntil(
        recordUsage(env, {
          reqId, org: identity.org, user: identity.user, agent: identity.agent, harness: identity.harness, run: identity.run,
          model, surface, streamed: true, usage: null, estUsd: null,
          gatewayLogId: env.AI.aiGatewayLogId ?? null, latencyMs: Date.now() - started,
        }),
      );
      return new Response(out, {
        headers: { "content-type": "text/event-stream; charset=utf-8", "cache-control": "no-store" },
      });
    }

    // ⑥ 用量记录（含 provider 三段 + binding 的 logId + 自算钱；P1 只记不扣）
    const payload = out as Record<string, unknown> | null;
    const usage = parseUsage(payload?.usage);
    ctx.waitUntil(
      recordUsage(env, {
        reqId, org: identity.org, user: identity.user, agent: identity.agent, harness: identity.harness, run: identity.run,
        model, surface, streamed: false, usage, estUsd: estimateCostUsd(model, usage),
        gatewayLogId: env.AI.aiGatewayLogId ?? null, latencyMs: Date.now() - started,
      }),
    );

    return new Response(JSON.stringify(out), { headers: { "content-type": "application/json; charset=utf-8" } });
  },
} satisfies ExportedHandler<Env>;
