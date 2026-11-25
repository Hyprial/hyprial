// 身份与 metadata：把「谁在调」做成可信用量标识。
// 设计依据：hq/notes/impl-gateway-worker-skeleton-2026-10-12.md §2（盖可信 metadata）
export type Identity = { org: string; user: string; agent?: string; harness?: string; run?: string };

/** 从已验签的 JWT claim 里取身份。⛔ 只认我们签发/约定的字段，不从请求体里读。 */
export function identityFromClaims(claims: Record<string, unknown>): Identity {
  const sub = String(claims.sub ?? "");
  if (!sub) throw new Error("IDENTITY_MISSING_SUB");
  // Casdoor 的 org 通常落在 owner/affiliation 一类字段；此处保守取法：org 必填，缺失即拒。
  const owner = claims.owner ?? claims.affiliation ?? claims.org;
  const org = typeof owner === "string" ? owner.split("/")[0] : "";
  if (!org) throw new Error("IDENTITY_MISSING_ORG");
  const pick = (v: unknown) => (typeof v === "string" && v ? v : undefined);
  return { org, user: sub };
}

/** 客户端声明的上下文（agent/harness/run）—— ⭐ 只作标注，⛔ 不可作为授权依据。 */
export function contextFromHeaders(h: Headers): Pick<Identity, "agent" | "harness" | "run"> {
  const g = (k: string) => h.get(k)?.trim() || undefined;
  return { agent: g("x-hyprial-agent"), harness: g("x-hyprial-harness"), run: g("x-hyprial-run") };
}

/** 给 GW 日志用的 metadata（≤5 项/请求，见 P0 实测）。⛔ 键不得以 cf. 开头。 */
export function gatewayMetadata(id: Identity, reqId: string): Record<string, string> {
  const m: Record<string, string> = { org: id.org, user: id.user, req: reqId };
  if (id.agent) m.agent = id.agent;
  if (id.harness) m.harness = id.harness;
  if (id.run) m.run = id.run;
  // 截到 5 项（CF 上限），并保证不含 cf.*
  return Object.fromEntries(Object.entries(m).slice(0, 5));
}
