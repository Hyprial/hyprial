// hyprial-dl — 发布产物分发面（P1）
// 约束来源：group-internal/decisions/D-infra-ops-0002.md §3
//   ① 公开读只经 dl.hyprial.com（r2.dev 关闭）②无目录列表 ③只允许不可变路径
//   ④ 唯一可变路径 latest.json，TTL ≤ 60s ⑤发布不可逆 ⇒ 永不覆盖已发布路径
//
// 路径白名单（其余一律 404）：
//   /wheel/<tag>/SHA256SUMS     ← 安装器入口：SUMS 必须与 wheel 同目录
//   /wheel/<tag>/<file>.whl
//   /desktop/<tag>/SHA256SUMS   ← 桌面资产与其 SUMS 同目录
//   /desktop/<tag>/<file>
//   /latest.json                ← 由 Worker 读 meta/latest.json 返回；⛔ 安装器不得引用
//   /install.sh · /install.sh.sha256 · /install.ps1 · /install.ps1.sha256
//                               ← 安装脚本，**逐条精确**放行（桶根 key，2026-10-26 公开地址已写进用户指南）
//
// ⚠️ 为什么 SUMS 必须与资产同目录（2026-10-23 实测 install.sh:198-202 / install.ps1:241）：
//   两者都按「SUMS 的目录」推导资产 URL（`_BASE_URL` / `$dlBase`）。SUMS 与资产分开目录
//   ⇒ 安装器请求 /sums/<tag>/xxx.whl ⇒ 404 ⇒ 静默回落源码安装，用户侧表现为"迁完还是慢"。

export interface Env {
  RELEASES: R2Bucket;
}

const IMMUTABLE_PREFIXES = ["wheel/", "desktop/"] as const;
const LATEST_OBJECT = "meta/latest.json";

// ⚠️ 根路径只放行这**四个字面量**：⛔ 不放行整个根、⛔ 不用通配/后缀匹配。
//    根是唯一"猜名字就能试"的空间（`/<任何名字>`），开一个通配就等于开放枚举面。
//    按**字面量**比较（decode 之前）⇒ 编码过的变体（`/install%2Esh`）也 404：一个对象只能有一个 URL。
const ROOT_FILES = [
  "/install.sh",
  "/install.sh.sha256",
  "/install.ps1",
  "/install.ps1.sha256",
] as const;

// ⚠️ <tag> 是**移动别名**（internal / stable 会被重发）⇒ tag 目录下的 SHA256SUMS 不能 immutable，
//    否则边缘会把上一版的 SUMS 继续发出去，用户"装到上一版"而看不出来。
//    文件名已钉版本与构建 sha（hyprial-0.5.4+g14e3281-…whl）⇒ 那些才可以长缓存。
const IMMUTABLE_CACHE = "public, max-age=31536000, immutable";
const SUMS_CACHE = "public, max-age=60, must-revalidate";
// ⚠️ 根安装脚本**可变**（install-scripts 重跑会就地覆盖同名 key）⇒ 与 SUMS 同档短 TTL。
//    ⛔ 不得跟着 wheel 走 immutable：边缘会把旧脚本一直发出去，用户"装的还是上一版"且看不出来。
const ROOT_FILE_CACHE = "public, max-age=60, must-revalidate";
const LATEST_CACHE = "public, max-age=60, s-maxage=60";

const TYPES: Record<string, string> = {
  whl: "application/zip",
  dmg: "application/x-apple-diskimage",
  exe: "application/vnd.microsoft.portable-executable",
  pkg: "application/octet-stream",
  zip: "application/zip",
  json: "application/json; charset=utf-8",
  sha256sums: "text/plain; charset=utf-8",
  // 安装脚本按文本发（`curl … | sh` / `iex` 都不看 MIME，但浏览器要看，否则下载成二进制）
  sh: "text/plain; charset=utf-8",
  ps1: "text/plain; charset=utf-8",
  sha256: "text/plain; charset=utf-8",
};

function contentType(key: string): string {
  const name = key.slice(key.lastIndexOf("/") + 1).toLowerCase();
  if (name === "sha256sums") return TYPES.sha256sums;
  const dot = name.lastIndexOf(".");
  if (dot < 0) return "application/octet-stream";
  return TYPES[name.slice(dot + 1)] ?? "application/octet-stream";
}

/** 解析 object key；不合法返回 null。⛔ 不做任何目录/列表语义。 */
export function resolveKey(pathname: string): string | null {
  // 单一可变路径
  if (pathname === "/latest.json") return LATEST_OBJECT;
  // 根安装脚本：只认这四个字面量（⛔ 通配、后缀、其余根路径一律 null）
  if ((ROOT_FILES as readonly string[]).includes(pathname)) return pathname.slice(1);
  // ⛔ 只允许单个前导斜杠：`//wheel/…` 与 `/wheel/…` 不能同时可达（一个对象两个 URL = 缓存与审计都会分叉）
  if (!pathname.startsWith("/")) return null;
  const raw = pathname.slice(1);
  if (!raw || raw.endsWith("/")) return null; // 无目录索引
  // ⚠️ 必须解码：wheel 文件名含 '+'，install.sh 把它编成 %2B 再取；不解码就取不到对象。
  // ⛔ 但只放行**安装器必需的那一种编码**：%2B（大小写等价）。其余百分号编码一律拒 ——
  //    理由：`%2E` 会把 `/x%2Ewhl` 与 `/x.whl` 映到**同一个对象**⇒ 一个对象两个 URL
  //    ⇒ 两份 CDN 缓存、两套审计（正是上面那条“一对象一 URL”要防的分叉）。
  //    顺带也就盖住了 %2F / %5C / %20 等（段边界会失真 / 白送第二个 URL）。
  if (raw.replace(/%2b/gi, "").includes("%")) return null;
  let key: string;
  try {
    key = decodeURIComponent(raw);
  } catch {
    return null; // 畸形百分号编码
  }
  if (key.endsWith("/")) return null;
  const segs = key.split("/");
  // ⚠️ **恰好三段** `/<prefix>/<tag>/<file>`：只有下界会让 `/wheel/a/b/c.whl` 也可达，
  //    而“白名单”这个词就不成立了（键空间无界）。目录语义不存在 ⇒ 深一层就是另一个东西。
  if (segs.length !== 3) return null;
  for (const s of segs) {
    if (s === "" || s === "." || s === "..") return null;
    // 控制字符 / NUL 不进对象键
    if (/[\u0000-\u001f\u007f]/.test(s)) return null;
  }
  const prefix = `${segs[0]}/`;
  if (!(IMMUTABLE_PREFIXES as readonly string[]).includes(prefix)) return null;
  return key;
}

/** 单个 `bytes=` 范围的判定。⛔ 不解析多段（multipart/byteranges 本 Worker 不支持）。 */
export function parseRange(
  header: string | undefined,
  size: number,
): "none" | "satisfiable" | "unsatisfiable" {
  if (!header) return "none";
  const m = /^bytes=(\d*)-(\d*)$/.exec(header.trim());
  // 畸形 / 多段 / 其他单位 ⇒ 按 RFC 9110 **忽略 Range**（当成没写），而不是报错：
  // 一个写坏的 Range 不该把本来能下的文件变成 4xx/5xx。
  if (!m) return "none";
  const start = m[1];
  const end = m[2];
  if (start === "" && end === "") return "none";
  if (start === "") return Number(end) === 0 ? "unsatisfiable" : "satisfiable"; // suffix `-N`
  if (Number(start) >= size) return "unsatisfiable";
  if (end !== "" && Number(end) < Number(start)) return "none";
  return "satisfiable";
}

function plain(status: number, body: string): Response {
  return new Response(body, {
    status,
    headers: { "content-type": "text/plain; charset=utf-8" },
  });
}

/** 存储侧失败：5xx + 可重试，⛔ 回显内部细节（⛔ 也⛔ 让它蒸发成 Worker 异常 / CF 1101）。 */
function unavailable(): Response {
  return new Response("storage unavailable", {
    status: 503,
    headers: { "content-type": "text/plain; charset=utf-8", "retry-after": "1" },
  });
}

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    const url = new URL(request.url);
    if (request.method !== "GET" && request.method !== "HEAD") {
      return new Response("method not allowed", {
        status: 405,
        headers: { allow: "GET, HEAD", "content-type": "text/plain; charset=utf-8" },
      });
    }

    const key = resolveKey(url.pathname);
    // ⚠️ 404 只能表示「这个 URL 不在本 Worker 的对象空间里」——即**白名单未命中**。
    //    ⛔ 不得用它表示「白名单命中了、但 R2 说没有」。后者对一个**已发布**的键是句谎话：
    //    install.sh 拿到 404 会**静默回落源码安装**（见文件头 2026-10-23 那条），用户侧只表现为
    //    "迁完还是慢"。2026-10-28T10:28–10:30Z 的 manylinux 瞬态就正是这种形状（两件 404，其余全 200，
    //    而该路径不经边缘缓存、每次都是真 Worker→R2 读 ⇒ 只能是 get() 当时返回了 null）。
    //    ⇒ 命中白名单之后的 null 一律按**可重试的存储故障**答 503，让它吵、让客户端重试。
    if (key === null) return plain(404, "not found");

    const range = request.headers.get("range") ?? undefined;
    // ⚠️ 416 的判据必须是「**这个范围不可满足**」，⛔ 「请求带没带 Range」：
    //    后者会把“带 Range 的瞬态故障”答成 416（客户端据此认为该范围不存在，不会重试）。
    //    ⇒ 先用 head() 拿对象大小、自己判范围；不可满足就**根本不向 R2 取对象**。
    let useRange = false;
    if (range) {
      let size: number;
      try {
        const meta = await env.RELEASES.head(key);
        // 同上：白名单已命中的键，head() 返回 null 不是"不存在"，而是读侧异常 ⇒ 503
        if (meta === null) return unavailable();
        size = meta.size;
      } catch {
        // head() 自己抛 ⇒ ⛔ 让它变成 Worker 异常（CF 1101）：回一个明确的 5xx，可重试。
        return unavailable();
      }
      const verdict = parseRange(range, size);
      if (verdict === "unsatisfiable") {
        return new Response(null, {
          status: 416,
          headers: {
            "content-range": `bytes */${size}`,
            "accept-ranges": "bytes",
            "content-type": "text/plain; charset=utf-8",
          },
        });
      }
      useRange = verdict === "satisfiable"; // 畸形/多段 ⇒ 当成没写 Range
    }

    let obj: R2ObjectBody | null;
    try {
      obj = await env.RELEASES.get(key, useRange ? { range: request.headers } : undefined);
    } catch {
      // 到这里就**不知道**具体原因了（range 已预验为可满足）⇒ 只能按 5xx 可重试语义答：
      // ⛔ 猜成 416，也⛔ 回显内部错误文本。
      return unavailable();
    }
    if (obj === null) return unavailable();

    const headers = new Headers();
    headers.set("content-type", contentType(key));
    const isSums = key.endsWith("/SHA256SUMS");
    const isRootFile = (ROOT_FILES as readonly string[]).includes(`/${key}`);
    headers.set(
      "cache-control",
      key === LATEST_OBJECT
        ? LATEST_CACHE
        : isRootFile
          ? ROOT_FILE_CACHE
          : isSums
            ? SUMS_CACHE
            : IMMUTABLE_CACHE,
    );
    headers.set("etag", obj.httpEtag);
    headers.set("accept-ranges", "bytes");
    // 只暴露"知道 tag 就能构造"的信息；不给目录、不给指纹
    headers.set("x-content-type-options", "nosniff");

    const size = obj.size;
    // ⚠️ B1（e2e-verifier 2026-10-27 阻断）：**只有 R2 真的按 range 返回时才答 206**。
    //    R2 对“不可满足/被它忽略”的 range 会返回**整份对象且不带 `.range`** ——
    //    旧写法用 `offset = range?.offset ?? 0` / `length = range?.length ?? size` 把它掩盖成
    //    “206 + content-range: bytes 0-<size-1> + 整份 body” ⇒ 续传客户端按 content-range 归位，
    //    实际收到整份 ⇒ **静默错位**（README 卖的恰好是 222MB dmg 的断点续传）。
    //    ⇒ 没拿到片段就回 200 整份（无 content-range），让客户端自己重下，⛔ 不装作成功了。
    const ranged = (obj as R2ObjectBody & { range?: { offset: number; length: number } }).range;
    if (useRange && ranged) {
      headers.set("content-range", `bytes ${ranged.offset}-${ranged.offset + ranged.length - 1}/${size}`);
      headers.set("content-length", String(ranged.length));
      return new Response(request.method === "HEAD" ? null : obj.body, {
        status: 206,
        headers,
      });
    }

    headers.set("content-length", String(size));
    return new Response(request.method === "HEAD" ? null : obj.body, { status: 200, headers });
  },
};
