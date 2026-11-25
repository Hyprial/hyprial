// handler 级自证：⛔ 不依赖 CF / wrangler / 网络（JWKS 用测试密钥 + 桩 fetch）。
// 跑法： node test/handler.test.ts
//
// 为什么补这一层：`wrangler deploy --dry-run` 只验配置与打包，不验运行时；
// 而 `wrangler dev` 起 remote 的 AI binding 时需要登录态，本地会超时（实测
// "TimeoutError: The operation was aborted due to timeout"）⇒ 那既不是我们代码的
// 行为，也不能用来判它是好的。⇒ 顶层 fetch 路径必须有一层不需要外部依赖的判据。
import { test } from "node:test";
import assert from "node:assert/strict";
import worker from "../src/index.ts";
import { signForTest, type Jwks } from "../src/jwt.ts";

const ISS = "https://auth.hyprial.com";
const AUD = "2cdd1defc1a0e219fafb";
const JWKS_URL = "https://auth.hyprial.com/.well-known/jwks";
const URL_BASE = "https://model.hyprial.ai";

type RunCall = { model: string; input: unknown; opts: any };

function harness() {
  const runs: RunCall[] = [];
  const waited: Promise<unknown>[] = [];
  const env = {
    AI: {
      run: async (model: string, input: unknown, opts: any) => {
        runs.push({ model, input, opts });
        return { id: "msg_1", usage: { input_tokens: 10, output_tokens: 2 }, content: [] };
      },
      aiGatewayLogId: "log-abc",
    },
    GATEWAY_ID: "default",
    CASDOOR_ISSUER: ISS,
    CASDOOR_JWKS_URL: JWKS_URL,
    CASDOOR_AUDIENCE: AUD,
  } as any;
  const ctx = { waitUntil: (p: Promise<unknown>) => void waited.push(p), passThroughOnException() {} } as any;
  return { env, ctx, runs, waited };
}

function stubFetch(impl: (url: string) => Promise<Response>) {
  const calls: string[] = [];
  (globalThis as any).fetch = async (u: any) => {
    const url = String(u);
    calls.push(url);
    return impl(url);
  };
  return calls;
}

const post = (path: string, body: unknown, headers: Record<string, string> = {}) =>
  new Request(`${URL_BASE}${path}`, {
    method: "POST",
    headers: { "content-type": "application/json", ...headers },
    body: typeof body === "string" ? body : JSON.stringify(body),
  });

async function makeJwks() {
  const kp = await crypto.subtle.generateKey(
    { name: "RSASSA-PKCS1-v1_5", modulusLength: 2048, publicExponent: new Uint8Array([1, 0, 1]), hash: "SHA-256" },
    true,
    ["sign", "verify"],
  );
  const pub = (await crypto.subtle.exportKey("jwk", kp.publicKey)) as any;
  pub.kid = "k1";
  pub.alg = "RS256";
  pub.use = "sig";
  return { kp, jwks: { keys: [pub] } as Jwks };
}

// ⚠️ JWKS 在 index.ts 里是模块级缓存（5 分钟）。⇒ 所有用例必须共用**同一把**密钥对，
// 否则后面的用例拿着新公钥去验新私钥的签名，会撞上缓存里那把旧公钥而必红（写这份测试时
// 第一版就踩了：每个用例各生成一把）。
let _shared: { kp: CryptoKeyPair; jwks: Jwks } | null = null;
async function sharedJwks() {
  if (!_shared) _shared = await makeJwks();
  return _shared;
}

const serveJwks = (jwks: Jwks) => async () =>
  new Response(JSON.stringify(jwks), { headers: { "content-type": "application/json" } });

test("① /healthz ⇒ 200，且不求任何人", async () => {
  const { env, ctx } = harness();
  const calls = stubFetch(async () => new Response("{}"));
  const res = await worker.fetch(new Request(`${URL_BASE}/healthz`), env, ctx);
  assert.equal(res.status, 200);
  assert.equal(await res.text(), "ok");
  assert.deepEqual(calls, [], "healthz ⛔ 不应有任何外部调用");
});

test("② 未知路径 ⇒ 400 且带可读码", async () => {
  const { env, ctx } = harness();
  stubFetch(async () => new Response("{}"));
  const res = await worker.fetch(post("/v1/nope", {}), env, ctx);
  assert.equal(res.status, 400);
  assert.equal((await res.json()).error.code, "BAD_REQUEST");
});

test("③ 无 Bearer ⇒ 401，且【零外部调用】（拒在网络之前）", async () => {
  const { env, ctx, runs } = harness();
  const calls = stubFetch(async () => new Response("{}"));
  const res = await worker.fetch(post("/v1/chat/completions", { model: "anthropic/claude-sonnet-4-6" }), env, ctx);
  assert.equal(res.status, 401);
  const body: any = await res.json();
  assert.equal(body.error.code, "UNAUTHENTICATED");
  assert.ok(body.error.hint, "401 必须给出路（hint）");
  assert.deepEqual(calls, [], "⛔ 无凭据时不得发起任何外部调用");
  assert.equal(runs.length, 0, "⛔ 无凭据时不得触达模型");
});

test("④ 有 token 但 JWKS 拉不到 ⇒ 401（fail-closed，⛔ 不放行）", async () => {
  const { env, ctx, runs } = harness();
  stubFetch(async () => {
    throw new Error("network down");
  });
  const res = await worker.fetch(
    post("/v1/chat/completions", { model: "anthropic/claude-sonnet-4-6" }, { authorization: "Bearer aaa.bbb.ccc" }),
    env,
    ctx,
  );
  assert.equal(res.status, 401, "JWKS 不可达必须 fail-closed");
  assert.equal(runs.length, 0);
});

test("⑤ 模型 id 缺厂商前缀 ⇒ 400，且【未触达模型】（拒在花钱之前）", async () => {
  const { kp, jwks } = await sharedJwks();
  const { env, ctx, runs } = harness();
  stubFetch(serveJwks(jwks));
  const now = Math.floor(Date.now() / 1000);
  const tok = await signForTest({ sub: "u1", owner: "hyprial/orgA", iss: ISS, aud: [AUD], exp: now + 60, tokenType: "access-token" }, kp.privateKey, "k1");
  const res = await worker.fetch(
    post("/v1/chat/completions", { model: "claude-sonnet-4-6", messages: [] }, { authorization: `Bearer ${tok}` }),
    env,
    ctx,
  );
  assert.equal(res.status, 400);
  assert.equal(runs.length, 0, "⛔ 模型 id 不合法时不得调用上游");
});

test("⑥ 合法请求走通：转发参数正确（skipCache / gateway id / metadata ≤5 且带 org）+ 原样返回", async () => {
  const { kp, jwks } = await sharedJwks();
  const { env, ctx, runs, waited } = harness();
  stubFetch(serveJwks(jwks));
  const now = Math.floor(Date.now() / 1000);
  const tok = await signForTest({ sub: "u1", owner: "hyprial/orgA", iss: ISS, aud: [AUD], exp: now + 60, tokenType: "access-token" }, kp.privateKey, "k1");
  const res = await worker.fetch(
    post(
      "/v1/chat/completions",
      { model: "anthropic/claude-sonnet-4-6", messages: [{ role: "user", content: "hi" }] },
      { authorization: `Bearer ${tok}`, "x-hyprial-agent": "homepage-maintainer", "x-hyprial-run": "r-9" },
    ),
    env,
    ctx,
  );
  assert.equal(res.status, 200);
  assert.equal(runs.length, 1);
  const call = runs[0]!;
  assert.equal(call.model, "anthropic/claude-sonnet-4-6");
  assert.equal(call.opts.gateway.id, "default");
  assert.equal(call.opts.gateway.skipCache, true, "⛔ 必须关 CF 网关缓存");
  const md = call.opts.gateway.metadata as Record<string, string>;
  assert.equal(md.org, "hyprial");
  assert.equal(md.user, "u1");
  assert.ok(Object.keys(md).length <= 5, `metadata 不得超过 5 项，实得 ${Object.keys(md).length}`);
  assert.ok(
    Object.keys(md).every((k) => !k.startsWith("cf.")),
    "⛔ metadata 不得含 cf.* 键",
  );
  assert.equal((await res.json()).id, "msg_1", "上游响应原样返回");
  assert.equal(waited.length, 1, "用量记录应挂 waitUntil（不阻塞响应）");
});

test("⑦ anthropic 面缺 anthropic-version ⇒ 400（与真网关一致）", async () => {
  const { kp, jwks } = await sharedJwks();
  const { env, ctx, runs } = harness();
  stubFetch(serveJwks(jwks));
  const now = Math.floor(Date.now() / 1000);
  const tok = await signForTest({ sub: "u1", owner: "hyprial/orgA", iss: ISS, aud: [AUD], exp: now + 60, tokenType: "access-token" }, kp.privateKey, "k1");
  const res = await worker.fetch(
    post("/anthropic/v1/messages", { model: "anthropic/claude-sonnet-4-6", messages: [] }, { authorization: `Bearer ${tok}` }),
    env,
    ctx,
  );
  assert.equal(res.status, 400);
  assert.equal(runs.length, 0);
});

test("⑧ 上游抛错 ⇒ UPSTREAM_ERROR 且不吞（有可读码）", async () => {
  const { kp, jwks } = await sharedJwks();
  const { env, ctx } = harness();
  env.AI.run = async () => {
    throw new Error("gateway exploded");
  };
  stubFetch(serveJwks(jwks));
  const now = Math.floor(Date.now() / 1000);
  const tok = await signForTest({ sub: "u1", owner: "hyprial/orgA", iss: ISS, aud: [AUD], exp: now + 60, tokenType: "access-token" }, kp.privateKey, "k1");
  const res = await worker.fetch(
    post("/v1/chat/completions", { model: "anthropic/claude-sonnet-4-6", messages: [] }, { authorization: `Bearer ${tok}` }),
    env,
    ctx,
  );
  assert.ok(res.status >= 500, `上游失败应报 5xx，实得 ${res.status}`);
  assert.equal((await res.json()).error.code, "UPSTREAM_ERROR");
});

test("⑨ stream:true ⇒ 上游流**原样透传**（⛔ 不得 stringify 成 `{}`）", async () => {
  const { kp, jwks } = await sharedJwks();
  const { env, ctx } = harness();
  const chunks = [
    'data: {"object":"chat.completion.chunk","choices":[{"delta":{"content":"1"}}]}\n\n',
    'data: {"object":"chat.completion.chunk","choices":[{"delta":{"content":"2"}}]}\n\n',
    "data: [DONE]\n\n",
  ];
  // ⭐ 实测：binding 对流式请求返回 ReadableStream（曾被我 stringify 成 `{}`，静默空成功）
  env.AI.run = async () =>
    new ReadableStream({
      start(c) {
        for (const s of chunks) c.enqueue(new TextEncoder().encode(s));
        c.close();
      },
    });
  stubFetch(serveJwks(jwks));
  const now = Math.floor(Date.now() / 1000);
  const tok = await signForTest({ sub: "u1", owner: "hyprial/orgA", iss: ISS, aud: [AUD], exp: now + 60, tokenType: "access-token" }, kp.privateKey, "k1");
  const res = await worker.fetch(
    post(
      "/v1/chat/completions",
      { model: "deepseek/deepseek-chat", stream: true, messages: [{ role: "user", content: "hi" }] },
      { authorization: `Bearer ${tok}` },
    ),
    env,
    ctx,
  );
  assert.equal(res.status, 200);
  assert.match(res.headers.get("content-type") ?? "", /text\/event-stream/, "流式必须声明为 event-stream");
  assert.equal(await res.text(), chunks.join(""), "逐块必须原样到达（⛔ 不是 `{}`）");
});

test("⑩ ⭐ refreshToken 不得当 API 凭据（tokenType 白名单，fail-closed）", async () => {
  const { kp, jwks } = await sharedJwks();
  const { env, ctx, runs } = harness();
  stubFetch(serveJwks(jwks));
  const now = Math.floor(Date.now() / 1000);
  // 实测（2026-10-13 真实交换）：refresh token 带 tokenType="refresh-token"；access 才是 "access-token"
  const tok = await signForTest(
    { sub: "u1", owner: "hyprial/orgA", iss: ISS, aud: [AUD], exp: now + 60, tokenType: "refresh-token" },
    kp.privateKey,
    "k1",
  );
  const res = await worker.fetch(
    post("/v1/chat/completions", { model: "deepseek/deepseek-chat", messages: [] }, { authorization: `Bearer ${tok}` }),
    env,
    ctx,
  );
  assert.equal(res.status, 401);
  assert.equal(runs.length, 0, "⛔ 被拒的凭据不得触达上游");
});

test("⑪ 缺 tokenType 也拒（白名单＝只认 access-token，⛔ 无缺省放行）", async () => {
  const { kp, jwks } = await sharedJwks();
  const { env, ctx, runs } = harness();
  stubFetch(serveJwks(jwks));
  const now = Math.floor(Date.now() / 1000);
  const tok = await signForTest({ sub: "u1", owner: "hyprial/orgA", iss: ISS, aud: [AUD], exp: now + 60 }, kp.privateKey, "k1");
  const res = await worker.fetch(
    post("/v1/chat/completions", { model: "deepseek/deepseek-chat", messages: [] }, { authorization: `Bearer ${tok}` }),
    env,
    ctx,
  );
  assert.equal(res.status, 401);
  assert.equal(runs.length, 0);
});
