// JWT 验签（RS256）+ JWKS 获取：零依赖，只用 WebCrypto。
// 设计依据：impl-plan §2 第 1 步「用 Casdoor JWKS 验签」；裁决 fail-closed ⇒ 任何一步失败都拒。
export type Jwks = { keys: Array<JsonWebKey & { kid?: string; alg?: string; use?: string }> };
export type VerifyOpts = {
  issuer?: string;
  audience?: string;
  nowMs?: number;
  leewaySec?: number;
  /** ⭐ 只接受这些 `tokenType`（实测：access=`access-token`、refresh=`refresh-token`）。 */
  allowedTokenTypes?: string[];
};

const b64u = (s: string) => {
  const t = s.replace(/-/g, "+").replace(/_/g, "/");
  const p = t + "=".repeat((4 - (t.length % 4)) % 4);
  const bin = atob(p);
  const out = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
  return out;
};
const dec = (u: Uint8Array) => new TextDecoder().decode(u);
const enc = (s: string) => new TextEncoder().encode(s);

/** 取 JWKS。⛔ 不吞错：拉不到就抛（调用方转 fail-closed）。 */
export async function fetchJwks(url: string, fetchImpl: typeof fetch = fetch): Promise<Jwks> {
  const res = await fetchImpl(url, { headers: { accept: "application/json" } });
  if (!res.ok) throw new Error(`JWKS_FETCH_${res.status}`);
  const j = (await res.json()) as Jwks;
  if (!j || !Array.isArray(j.keys) || j.keys.length === 0) throw new Error("JWKS_EMPTY");
  return j;
}

type Claims = Record<string, unknown> & { sub?: string; iss?: string; aud?: string | string[]; exp?: number; nbf?: number };

/** 验签 RS256 并做 iss/aud/exp/nbf 检查。返回 claims；任何失败抛错（⛔ 不返回半可信结果）。 */
export async function verifyJwt(token: string, jwks: Jwks, opts: VerifyOpts = {}): Promise<Claims> {
  const parts = token.split(".");
  if (parts.length !== 3) throw new Error("JWT_MALFORMED");
  const [h, p, s] = parts;
  const header = JSON.parse(dec(b64u(h))) as { alg?: string; kid?: string; typ?: string };
  if (header.alg !== "RS256") throw new Error("JWT_ALG_UNSUPPORTED"); // ⛔ 不接受 none/HS256（防算法混淆）
  const key = jwks.keys.find((k) => (header.kid ? k.kid === header.kid : true));
  if (!key) throw new Error("JWT_KID_UNKNOWN");
  const cryptoKey = await crypto.subtle.importKey(
    "jwk",
    key as JsonWebKey,
    { name: "RSASSA-PKCS1-v1_5", hash: "SHA-256" },
    false,
    ["verify"],
  );
  const ok = await crypto.subtle.verify("RSASSA-PKCS1-v1_5", cryptoKey, b64u(s), enc(`${h}.${p}`));
  if (!ok) throw new Error("JWT_SIGNATURE_INVALID");
  const claims = JSON.parse(dec(b64u(p))) as Claims;
  const now = Math.floor((opts.nowMs ?? Date.now()) / 1000);
  const lee = opts.leewaySec ?? 0;
  if (opts.issuer && claims.iss !== opts.issuer) throw new Error("JWT_ISS_MISMATCH");
  if (opts.audience) {
    const aud = Array.isArray(claims.aud) ? claims.aud : [claims.aud];
    if (!aud.includes(opts.audience)) throw new Error("JWT_AUD_MISMATCH");
  }
  // ⭐ 凭据形态校验（P2；Allen 2026-10-13 裁定走 A + 会话内缓存）：
  // 实测 Casdoor 的判别字段是 **`tokenType`**（access="access-token"、refresh="refresh-token"），
  // ⛔ 不是 `type`（那是 "normal-user" —— 照它写会**永不触发**）。
  // 白名单语义：只认列出的值，其余一律拒（fail-closed）⇒ refresh token 不能当 API 凭据。
  if (opts.allowedTokenTypes) {
    const tt = typeof claims.tokenType === "string" ? claims.tokenType : "";
    if (!opts.allowedTokenTypes.includes(tt)) throw new Error("JWT_TOKEN_TYPE_REJECTED");
  }
  if (typeof claims.exp === "number" && claims.exp + lee < now) throw new Error("JWT_EXPIRED");
  if (typeof claims.nbf === "number" && claims.nbf - lee > now) throw new Error("JWT_NOT_YET_VALID");
  return claims;
}

/** 仅供测试/工具用：用私钥签一个 RS256 JWT。⛔ 生产代码不调用。 */
export async function signForTest(claims: Record<string, unknown>, privateKey: CryptoKey, kid: string): Promise<string> {
  const h = { alg: "RS256", typ: "JWT", kid };
  const b = (o: unknown) => Buffer.from(JSON.stringify(o)).toString("base64url");
  const data = `${b(h)}.${b(claims)}`;
  const sig = await crypto.subtle.sign("RSASSA-PKCS1-v1_5", privateKey, enc(data));
  return `${data}.${Buffer.from(new Uint8Array(sig)).toString("base64url")}`;
}
