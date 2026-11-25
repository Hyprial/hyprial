import { REGION_900 } from "./region900.ts";
import snapshot from "./snapshot.json" with { type: "json" };

const UPSTREAM = "https://tailcat.dev/derpmap.json";
const CACHE_TTL_SECONDS = 300;

type DerpMap = { Regions?: Record<string, unknown> } & Record<string, unknown>;

export function stableStringify(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(stableStringify).join(",")}]`;
  if (value && typeof value === "object") {
    const keys = Object.keys(value as Record<string, unknown>).sort();
    return `{${keys.map((k) => `${JSON.stringify(k)}:${stableStringify((value as Record<string, unknown>)[k])}`).join(",")}}`;
  }
  return JSON.stringify(value);
}

export function mergeDerpMap(upstream: DerpMap | null): DerpMap {
  const base: DerpMap = upstream && typeof upstream === "object" ? upstream : (snapshot as DerpMap);
  const regions: Record<string, unknown> = { ...(base.Regions ?? {}) };
  regions["900"] = REGION_900;
  return { ...base, Regions: regions };
}

async function loadUpstream(cache: Cache | undefined, waitUntil: (p: Promise<unknown>) => void): Promise<DerpMap | null> {
  if (!cache) return null;
  const key = new Request(UPSTREAM);
  const hit = await cache.match(key);
  if (hit) return (await hit.json()) as DerpMap;
  const response = await fetch(UPSTREAM);
  if (!response.ok) return null;
  const text = await response.text();
  const clone = new Response(text, { headers: { "content-type": "application/json", "cache-control": `public, max-age=${CACHE_TTL_SECONDS}` } });
  waitUntil(cache.put(key, clone));
  return JSON.parse(text) as DerpMap;
}

export default {
  async fetch(request: Request, env: { DERPMAP_CACHE?: Cache }, ctx: { waitUntil: (p: Promise<unknown>) => void }): Promise<Response> {
    const url = new URL(request.url);
    if (url.pathname !== "/derpmap.json") return new Response("not found", { status: 404 });
    if (request.method !== "GET" && request.method !== "HEAD") {
      return new Response("method not allowed", { status: 405, headers: { allow: "GET, HEAD" } });
    }
    let upstream: DerpMap | null = null;
    try {
      upstream = await loadUpstream(env.DERPMAP_CACHE, ctx.waitUntil);
    } catch {
      upstream = null;
    }
    const body = stableStringify(mergeDerpMap(upstream));
    return new Response(request.method === "HEAD" ? null : body, {
      headers: {
        "content-type": "application/json",
        "cache-control": `public, max-age=${CACHE_TTL_SECONDS}`,
      },
    });
  },
};
