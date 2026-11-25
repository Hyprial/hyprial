// 纯逻辑 + 验签的自证测试：⛔ 不依赖任何外部服务/凭据。
// 跑法： node --test test/
import { test } from "node:test";
import assert from "node:assert/strict";
import { identityFromClaims, contextFromHeaders, gatewayMetadata } from "../src/identity.ts";
import { parseUsage, estimateCostUsd } from "../src/usage.ts";
import { surfaceForPath, normalizeModel, errorResponse } from "../src/routing.ts";
import { verifyJwt, signForTest, type Jwks } from "../src/jwt.ts";

test("身份：org 与 sub 必填，缺失即拒（⛔ 不从请求体读身份）", () => {
  assert.deepEqual(identityFromClaims({ sub: "u1", owner: "hyprial/orgA" }), { org: "hyprial", user: "u1" });
  assert.throws(() => identityFromClaims({ sub: "u1" }), /IDENTITY_MISSING_ORG/);
  assert.throws(() => identityFromClaims({ owner: "hyprial/orgA" }), /IDENTITY_MISSING_SUB/);
});

test("metadata：≤5 项且不含 cf.* 键", () => {
  const m = gatewayMetadata(
    { org: "hyprial", user: "u1", agent: "a", harness: "claude", run: "r" },
    "req-1",
  );
  assert.ok(Object.keys(m).length <= 5);
  assert.ok(!Object.keys(m).some((k) => k.startsWith("cf.")));
  assert.equal(m.req, "req-1");
});

test("客户端上下文只作标注（缺失不报错，⛔ 不参与授权）", () => {
  assert.deepEqual(contextFromHeaders(new Headers()), { agent: undefined, harness: undefined, run: undefined });
  const h = new Headers({ "x-hyprial-harness": "pi" });
  assert.equal(contextFromHeaders(h).harness, "pi");
});

test("usage：解析 anthropic 三段 + OpenAI 的 cached_tokens", () => {
  const a = parseUsage({ input_tokens: 13, output_tokens: 4, cache_creation_input_tokens: 3203, cache_read_input_tokens: 0 });
  assert.deepEqual(a, { inputTokens: 13, outputTokens: 4, cacheCreationTokens: 3203, cacheReadTokens: 0 });
  const o = parseUsage({ prompt_tokens: 100, completion_tokens: 9, prompt_tokens_details: { cached_tokens: 64 } });
  assert.deepEqual(o, { inputTokens: 100, outputTokens: 9, cacheCreationTokens: 0, cacheReadTokens: 64 });
});

test("计价：三段费率（与 §16 实测的 CF cost 反算一致）", () => {
  // 写缓存那次： (13 + 3203*1.25)*3e-6 + 4*15e-6 = 0.01211025
  const write = estimateCostUsd("anthropic/claude-sonnet-4-6", { inputTokens: 13, outputTokens: 4, cacheCreationTokens: 3203, cacheReadTokens: 0 });
  assert.ok(Math.abs((write as number) - 0.01211025) < 1e-9, `got ${write}`);
  // 读缓存那次： (13 + 3203*0.1)*3e-6 + 4*15e-6 = 0.0010599
  const read = estimateCostUsd("anthropic/claude-sonnet-4-6", { inputTokens: 13, outputTokens: 4, cacheCreationTokens: 0, cacheReadTokens: 3203 });
  assert.ok(Math.abs((read as number) - 0.0010599) < 1e-9, `got ${read}`);
  // ⛔ 费率未知不猜
  assert.equal(estimateCostUsd("unknown/model", { inputTokens: 1, outputTokens: 1, cacheCreationTokens: 0, cacheReadTokens: 0 }), null);
});

test("路由：两个兼容面；模型 id 必须 <provider>/<model>", () => {
  assert.equal(surfaceForPath("/v1/chat/completions"), "openai");
  assert.equal(surfaceForPath("/anthropic/v1/messages"), "anthropic");
  assert.equal(surfaceForPath("/nope"), null);
  assert.equal(normalizeModel("anthropic/claude-sonnet-4-6"), "anthropic/claude-sonnet-4-6");
  assert.throws(() => normalizeModel("claude-sonnet-4-6"), /MODEL_NOT_QUALIFIED/);
});

test("错误面：每种错都可区分且有出路（fail-closed 语义）", async () => {
  for (const [code, status] of [["UNAUTHENTICATED", 401], ["QUOTA_EXCEEDED", 402], ["BAD_REQUEST", 400], ["QUOTA_SERVICE_UNAVAILABLE", 503]] as const) {
    const r = errorResponse(code, "d");
    assert.equal(r.status, status);
    const j = await r.json();
    assert.equal(j.error.code, code);
    assert.ok(j.error.hint && j.error.hint.length > 4, `hint 缺失: ${code}`);
  }
});

test("验签：自签 JWT 通过；篡改/换 kid/过期/算法混淆 全部拒绝", async () => {
  const kp = await crypto.subtle.generateKey({ name: "RSASSA-PKCS1-v1_5", modulusLength: 2048, publicExponent: new Uint8Array([1, 0, 1]), hash: "SHA-256" }, true, ["sign", "verify"]);
  const pub = (await crypto.subtle.exportKey("jwk", kp.publicKey)) as JsonWebKey & { kid?: string };
  pub.kid = "k1"; pub.alg = "RS256"; pub.use = "sig";
  const jwks: Jwks = { keys: [pub] };
  const now = Math.floor(Date.now() / 1000);
  const good = await signForTest({ sub: "u1", owner: "hyprial/orgA", iss: "https://auth.hyprial.com", aud: "model-gateway", exp: now + 60 }, kp.privateKey, "k1");
  const claims = await verifyJwt(good, jwks, { issuer: "https://auth.hyprial.com", audience: "model-gateway" });
  assert.equal(claims.sub, "u1");
  // 篡改 payload
  const [, p, s] = good.split(".");
  const tampered = `${good.split(".")[0]}.${Buffer.from(JSON.stringify({ sub: "evil", owner: "hyprial/orgA", exp: now + 60 })).toString("base64url")}.${s}`;
  await assert.rejects(() => verifyJwt(tampered, jwks), /JWT_SIGNATURE_INVALID/);
  // 未知 kid
  const other = await signForTest({ sub: "u1", exp: now + 60 }, kp.privateKey, "k2");
  await assert.rejects(() => verifyJwt(other, jwks), /JWT_KID_UNKNOWN/);
  // 过期
  const expired = await signForTest({ sub: "u1", owner: "hyprial/orgA", exp: now - 10 }, kp.privateKey, "k1");
  await assert.rejects(() => verifyJwt(expired, jwks), /JWT_EXPIRED/);
  // 算法混淆：header alg=none
  const noneTok = `${Buffer.from(JSON.stringify({ alg: "none", kid: "k1" })).toString("base64url")}.${p}.`;
  await assert.rejects(() => verifyJwt(noneTok, jwks), /JWT_ALG_UNSUPPORTED/);
});

test("验签：aud 为数组形态（Casdoor 实测）命中即过；数组内不含目标则拒", async () => {
  const kp = await crypto.subtle.generateKey({ name: "RSASSA-PKCS1-v1_5", modulusLength: 2048, publicExponent: new Uint8Array([1, 0, 1]), hash: "SHA-256" }, true, ["sign", "verify"]);
  const pub = (await crypto.subtle.exportKey("jwk", kp.publicKey)) as JsonWebKey & { kid?: string };
  pub.kid = "k9"; pub.alg = "RS256"; pub.use = "sig";
  const jwks: Jwks = { keys: [pub] };
  const ISS = "https://auth.hyprial.com";
  const CLIENT_ID = "2cdd1defc1a0e219fafb";
  const now = Math.floor(Date.now() / 1000);
  // ⭐ 真实形态：aud = ["<clientId>"]（可能还含别的）
  const tok = await signForTest({ sub: "u1", owner: "hyprial/orgA", iss: ISS, aud: [CLIENT_ID, "https://other.example"], exp: now + 60 }, kp.privateKey, "k9");
  const claims = await verifyJwt(tok, jwks, { issuer: ISS, audience: CLIENT_ID });
  assert.equal(claims.sub, "u1");
  // 数组里不含目标 ⇒ 拒
  const other = await signForTest({ sub: "u1", owner: "hyprial/orgA", iss: ISS, aud: ["someone-else"], exp: now + 60 }, kp.privateKey, "k9");
  await assert.rejects(() => verifyJwt(other, jwks, { issuer: ISS, audience: CLIENT_ID }), /JWT_AUD_MISMATCH/);
});
