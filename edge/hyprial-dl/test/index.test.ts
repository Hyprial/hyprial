import { test } from "node:test";
import assert from "node:assert/strict";
import worker, { resolveKey } from "../src/index.ts";

function stubR2(
  objects: Record<string, { bytes: Uint8Array; etag: string }>,
  opts?: { ignoreRange?: boolean; failRange?: boolean; failHead?: boolean },
) {
  const calls: string[] = [];
  const RELEASES = {
    async get(key: string, o?: any) {
      calls.push(key);
      const obj = objects[key];
      if (!obj) return null;
      // failRange 模拟**线上真 R2 的行为**：不可满足的 range 直接抛 ⇒ Worker 异常 ⇒ CF 1101 / HTTP 500
      // （2026-10-27 实测 dl.hyprial.com：`Range: bytes=999999999-` ⇒ 500「error code: 1101」）
      if (o?.range && opts?.failRange) throw new Error("range not satisfiable");
      const body = obj.bytes;
      // ignoreRange 模拟「R2 没按 range 返回」——整份 body、**不带 .range 信息**（真 R2 对不可满足/被忽略的
      // range 就是这个形状）。这一支以前被 `?? 0` / `?? size` 掩盖成 206，见 B1。
      const ranged = o?.range && !opts?.ignoreRange;
      return {
        body,
        size: body.byteLength,
        httpEtag: obj.etag,
        range: ranged ? { offset: 0, length: Math.min(16, body.byteLength) } : undefined,
      };
    },
    async head(key: string) {
      if (opts?.failHead) throw new Error("head blew up: token=leaky");
      const obj = objects[key];
      if (!obj) return null;
      return { size: obj.bytes.byteLength, httpEtag: obj.etag };
    },
  };
  return { RELEASES, calls };
}

const env = (objs: Record<string, { bytes: Uint8Array; etag: string }>) => stubR2(objs) as any;
const bytes = (s: string) => new TextEncoder().encode(s);

test("合法路径：三类不可变前缀 + latest.json", () => {
  assert.equal(resolveKey("/wheel/internal/a.whl"), "wheel/internal/a.whl");
  assert.equal(resolveKey("/desktop/v1/a.dmg"), "desktop/v1/a.dmg");
  assert.equal(resolveKey("/desktop/v1/a.exe"), "desktop/v1/a.exe");
  assert.equal(resolveKey("/latest.json"), "meta/latest.json");
});

test("同目录规则：SUMS 与资产同前缀可达（安装器按 SUMS 目录推导资产 URL）", () => {
  assert.equal(resolveKey("/wheel/internal/SHA256SUMS"), "wheel/internal/SHA256SUMS");
  assert.equal(resolveKey("/desktop/v1/SHA256SUMS"), "desktop/v1/SHA256SUMS");
});

test("⛔ /sums/ 不是路径（安装器按配置字符串推目录，第二个 SUMS URL 会静默弄死 wheel）", () => {
  assert.equal(resolveKey("/sums/internal/SHA256SUMS"), null);
  assert.equal(resolveKey("/sums/internal/a.whl"), null);
});

test("'+' 文件名：%2B 与裸 '+' 都要解析成同一个对象键（5/5 wheel 名都带 '+'）", () => {
  const name = "hyprial-0.5.4+g14e3281-py3-none-macosx_13_0_arm64.whl";
  const enc = "hyprial-0.5.4%2Bg14e3281-py3-none-macosx_13_0_arm64.whl";
  assert.equal(resolveKey(`/wheel/internal/${name}`), `wheel/internal/${name}`);
  assert.equal(resolveKey(`/wheel/internal/${enc}`), `wheel/internal/${name}`);
});

test("拒绝：编码过的分隔符与穿越（%2F、%5C、%2e%2e）", () => {
  assert.equal(resolveKey("/wheel/internal%2F..%2Fetc/passwd"), null);
  assert.equal(resolveKey("/wheel/internal/%2e%2e/x.whl"), null);
  assert.equal(resolveKey("/wheel/internal%5cx.whl"), null);
});

test("拒绝：目录索引 / 穿越 / 未知前缀 / 层级不足", () => {
  for (const bad of ["/", "/wheel/", "/wheel/internal/", "/wheel/x", "/pkg/a/b", "/api/x/y",
                     "/wheel/../etc/passwd", "/wheel/./a.whl", "//wheel/a/b"]) {
    assert.equal(resolveKey(bad), null, bad);
  }
});

test("根安装脚本：四个字面量精确放行（桶根 key）", () => {
  for (const p of ["/install.sh", "/install.sh.sha256", "/install.ps1", "/install.ps1.sha256"]) {
    assert.equal(resolveKey(p), p.slice(1), p);
  }
});

test("⛔ 根路径不放通配：其余根路径、编码变体、后缀变体全部 404", () => {
  for (const bad of [
    "/install.sh.bak", // 后缀变体
    "/install.sh ",
    "/install",
    "/install.sh/", // 目录形式
    "/INSTALL.SH", // 大小写：R2 key 区分大小写，但边缘只认一个 URL
    "/install%2Esh", // 编码变体：一个对象只能有一个 URL
    "/install.sh%00",
    "/artifacts/install.sh", // 换前缀不算
    "/wheel/install.sh",
    "/env.sh",
    "/config.json",
  ]) {
    assert.equal(resolveKey(bad), null, bad);
  }
});

test("根安装脚本：可缓存但⛔ 不得 immutable（重跑 install-scripts 会就地覆盖同名 key）", async () => {
  const e = env({
    "install.sh": { bytes: bytes("#!/bin/sh\n"), etag: "s1" },
    "install.sh.sha256": { bytes: bytes("abc  install.sh\n"), etag: "s2" },
    "install.ps1": { bytes: bytes("Write-Host hi\n"), etag: "s3" },
    "install.ps1.sha256": { bytes: bytes("def  install.ps1\n"), etag: "s4" },
  });
  for (const p of ["/install.sh", "/install.sh.sha256", "/install.ps1", "/install.ps1.sha256"]) {
    const r = await worker.fetch(new Request(`https://dl.hyprial.com${p}`), e);
    assert.equal(r.status, 200, p);
    assert.equal(r.headers.get("content-type"), "text/plain; charset=utf-8", p);
    const cc = r.headers.get("cache-control") ?? "";
    assert.equal(cc, "public, max-age=60, must-revalidate", p);
    assert.ok(!/immutable/.test(cc), p);
    assert.equal(r.headers.get("x-content-type-options"), "nosniff", p);
  }
});

test("根安装脚本不影响 wheel/ 与 latest.json 的读数", async () => {
  const e = env({
    "wheel/internal/a.whl": { bytes: bytes("W"), etag: "e2" },
    "meta/latest.json": { bytes: bytes('{"tag":"stable"}'), etag: "e1" },
  });
  const w = await worker.fetch(new Request("https://dl.hyprial.com/wheel/internal/a.whl"), e);
  assert.equal(w.headers.get("cache-control"), "public, max-age=31536000, immutable");
  const l = await worker.fetch(new Request("https://dl.hyprial.com/latest.json"), e);
  assert.equal(l.headers.get("cache-control"), "public, max-age=60, s-maxage=60");
});

test("latest.json：60s TTL + 命中 meta 对象", async () => {
  const e = env({ "meta/latest.json": { bytes: bytes('{"tag":"stable"}'), etag: "e1" } });
  const r = await worker.fetch(new Request("https://dl.hyprial.com/latest.json"), e);
  assert.equal(r.status, 200);
  assert.equal(r.headers.get("cache-control"), "public, max-age=60, s-maxage=60");
  assert.equal(r.headers.get("content-type"), "application/json; charset=utf-8");
  assert.equal(await r.text(), '{"tag":"stable"}');
});

test("不可变路径：immutable 缓存 + 类型按扩展名", async () => {
  const e = env({ "wheel/internal/a.whl": { bytes: bytes("W"), etag: "e2" } });
  const r = await worker.fetch(new Request("https://dl.hyprial.com/wheel/internal/a.whl"), e);
  assert.equal(r.status, 200);
  assert.equal(r.headers.get("cache-control"), "public, max-age=31536000, immutable");
  assert.equal(r.headers.get("content-type"), "application/zip");
  assert.equal(r.headers.get("accept-ranges"), "bytes");
});

test("SHA256SUMS：tag 是移动别名 ⇒ ≤60s 可重验，⛔ 不得 immutable", async () => {
  const e = env({ "wheel/internal/SHA256SUMS": { bytes: bytes("h  a.whl\n"), etag: "e5" } });
  const r = await worker.fetch(new Request("https://dl.hyprial.com/wheel/internal/SHA256SUMS"), e);
  assert.equal(r.status, 200);
  assert.equal(r.headers.get("cache-control"), "public, max-age=60, must-revalidate");
  assert.ok(!/immutable/.test(r.headers.get("cache-control") ?? ""));
});

test("白名单命中但对象为 null ⇒ 503 + retry-after（⛔ 不是 404），且不泄露内部信息", async () => {
  // 2026-10-28T10:28–10:30Z：stable 两件 manylinux 短窗内 404，其余全 200；该路径不经边缘缓存，
  // 每次都是真 Worker→R2 读 ⇒ 只能是 get() 当时返回了 null。对一个**已发布**的键，404 是句谎话：
  // install.sh 拿到 404 会静默回落源码安装，用户侧只表现为“迁完还是慢”。⇒ 这类情况必须可重试。
  const e = env({});
  const r = await worker.fetch(new Request("https://dl.hyprial.com/wheel/internal/none.whl"), e);
  assert.equal(r.status, 503, "白名单命中的键读不到对象 = 存储故障，不是“不存在”");
  assert.equal(r.headers.get("retry-after"), "1");
  assert.equal(r.headers.get("content-type"), "text/plain; charset=utf-8");
  const text = await r.text();
  assert.ok(!/none\.whl|internal/.test(text), `不得回显键名：${text}`);
});

test("Range 预检的 head() 返回 null 同样 ⇒ 503（⛔ 416、⛔ 404）", async () => {
  const e = env({});
  const r = await worker.fetch(
    new Request("https://dl.hyprial.com/wheel/internal/none.whl", { headers: { range: "bytes=0-15" } }),
    e,
  );
  assert.equal(r.status, 503, "range 与“对象不存在”是两件事：416 只能表示范围不可满足");
  assert.equal(r.headers.get("retry-after"), "1");
  assert.equal(r.headers.get("content-range"), null);
});

test("白名单未命中仍是 404（404 只剩这一个含义），且不触达 R2", async () => {
  const s = stubR2({});
  for (const p of ["/wheel/internal", "/wheel/a/b/c.whl", "/sums/stable/x.whl", "/wheel/stable/x.whl".replace("stable", "stable%2fx")]) {
    const r = await worker.fetch(new Request(`https://dl.hyprial.com${p}`), s as any);
    assert.equal(r.status, 404, p);
  }
  assert.deepEqual(s.calls, [], "白名单未命中不得触达 R2");
});

test("目录与未知前缀 → 404，且不触达 R2（不泄露桶内是否存在）", async () => {
  const s = stubR2({});
  for (const p of ["/", "/wheel/", "/api/.env", "/phpinfo.php"]) {
    const r = await worker.fetch(new Request(`https://dl.hyprial.com${p}`), s as any);
    assert.equal(r.status, 404, p);
  }
  assert.deepEqual(s.calls, []);
});

test("非 GET/HEAD → 405 且带 Allow", async () => {
  const e = env({});
  const r = await worker.fetch(new Request("https://dl.hyprial.com/latest.json", { method: "POST" }), e);
  assert.equal(r.status, 405);
  assert.equal(r.headers.get("allow"), "GET, HEAD");
});

test("Range 请求 → 206 + content-range", async () => {
  const body = new TextEncoder().encode("0123456789abcdefghij");
  const e = env({ "desktop/v1/a.dmg": { bytes: body, etag: "e3" } });
  const r = await worker.fetch(
    new Request("https://dl.hyprial.com/desktop/v1/a.dmg", { headers: { range: "bytes=0-15" } }),
    e,
  );
  assert.equal(r.status, 206);
  assert.equal(r.headers.get("content-range"), `bytes 0-15/${body.byteLength}`);
});

test("HEAD → 无 body 但带 content-length", async () => {
  const e = env({ "wheel/internal/SHA256SUMS": { bytes: bytes("abc"), etag: "e4" } });
  const r = await worker.fetch(
    new Request("https://dl.hyprial.com/wheel/internal/SHA256SUMS", { method: "HEAD" }),
    e,
  );
  assert.equal(r.status, 200);
  assert.equal(r.headers.get("content-length"), "3");
  assert.equal(await r.text(), "");
});

// --- B1（e2e-verifier 2026-10-27 判词，阻断）：R2 没按 range 返回时不得答 206 ---------------

test("B1: R2 忽略 range（对象不带 .range）⇒ 必须 200 整份，⛔ 206 配整份", async () => {
  const body = bytes("0123456789");
  const s = stubR2({ "desktop/v1/a.dmg": { bytes: body, etag: "e6" } }, { ignoreRange: true });
  const r = await worker.fetch(
    new Request("https://dl.hyprial.com/desktop/v1/a.dmg", { headers: { range: "bytes=2-4" } }),
    s as any,
  );
  assert.equal(r.status, 200, "R2 没给部分内容就只能回整份 200");
  assert.equal(r.headers.get("content-range"), null, "200 不得带 content-range");
  assert.equal(r.headers.get("content-length"), String(body.byteLength));
  assert.equal(r.headers.get("content-type"), "application/x-apple-diskimage");
  assert.equal(await r.text(), "0123456789", "必须是整份 body，⛔ 把整份当片段发出");
});

test("B1: 不可满足 range ⇒ 416（**按对象大小判定**，R2 那一支根本不走；⛔ 206 配整份、⛔ 500）", async () => {
  const body = bytes("0123456789");
  const s = stubR2({ "desktop/v1/a.dmg": { bytes: body, etag: "e7" } }, { ignoreRange: true });
  const r = await worker.fetch(
    new Request("https://dl.hyprial.com/desktop/v1/a.dmg", { headers: { range: "bytes=99-199" } }),
    s as any,
  );
  assert.notEqual(r.status, 206, "不可满足的 range 不能伪装成 206");
  assert.equal(r.status, 416);
  assert.equal(r.headers.get("content-range"), `bytes */${body.byteLength}`);
});

test("B1 control: R2 **按** range 返回时仍然是 206 + 正确 content-range（happy path 不得被改坏）", async () => {
  const body = bytes("0123456789abcdefghij");
  const e = env({ "desktop/v1/a.dmg": { bytes: body, etag: "e8" } });
  const r = await worker.fetch(
    new Request("https://dl.hyprial.com/desktop/v1/a.dmg", { headers: { range: "bytes=0-15" } }),
    e,
  );
  assert.equal(r.status, 206);
  assert.equal(r.headers.get("content-range"), `bytes 0-15/${body.byteLength}`);
  assert.equal(r.headers.get("content-length"), "16");
});

// --- N1：白名单是 /prefix/<tag>/<file> 三段 ⇒ 更深路径不是"白名单" --------------------------

test("N1: 四段及以上路径 ⛔ 放行（白名单口径是 /wheel/<tag>/<file>）", () => {
  for (const bad of [
    "/wheel/internal/deeper/x.whl",
    "/desktop/v1/sub/a.dmg",
    "/wheel/internal/a/b/c.whl",
  ]) {
    assert.equal(resolveKey(bad), null, bad);
  }
});

// --- N2：「一个对象只能有一个 URL」必须对不可变前缀也成立（只放行安装器必需的 %2B）-----------

test("N2: 不可变前缀只接受 %2B（安装器把 '+' 编成 %2B），其余百分号编码一律 404", () => {
  const wheel = "hyprial-0.5.4+g14e3281-py3-none-macosx_13_0_arm64.whl";
  assert.equal(resolveKey(`/wheel/internal/hyprial-0.5.4%2Bg14e3281-py3-none-macosx_13_0_arm64.whl`),
    `wheel/internal/${wheel}`, "%2B 必须仍可用（安装器依赖它）");
  assert.equal(resolveKey(`/wheel/internal/hyprial-0.5.4%2bg14e3281-py3-none-macosx_13_0_arm64.whl`),
    `wheel/internal/${wheel}`, "小写 %2b 等价");
  for (const bad of [
    "/wheel/internal/a%2Ewhl", // 编码的 '.' ⇒ 与 a.whl 同一对象 = 白送第二个 URL
    "/wheel/internal/a%2ewhl",
    "/wheel/internal/a%20b.whl", // 编码的空格
    "/wheel/internal/a%2Db.whl", // 无需编码的 '-'
    "/wheel/internal/a%25b.whl", // 编码的 '%'
    "/wheel/internal/%2E%2E/x.whl",
  ]) {
    assert.equal(resolveKey(bad), null, bad);
  }
});

// --- B1 第三支：线上真 R2 对不可满足 range **抛异常** ⇒ 正确语义是 416（⛔ 500 / CF 1101）------

test("B1: R2 对不可满足 range 抛异常（线上实测行为）⇒ 416 + content-range: bytes */size，⛔ 500", async () => {
  const body = bytes("0123456789");
  const s = stubR2({ "wheel/internal/a.whl": { bytes: body, etag: "e9" } }, { failRange: true });
  const r = await worker.fetch(
    new Request("https://dl.hyprial.com/wheel/internal/a.whl", { headers: { range: "bytes=999999999-" } }),
    s as any,
  );
  assert.equal(r.status, 416, "不可满足的 range 的正确答复是 416");
  assert.equal(r.headers.get("content-range"), `bytes */${body.byteLength}`);
});

test("B1: 非 range 的取对象失败 ⇒ 503 + retry-after，⛔ 泄漏内部信息", async () => {
  const s = {
    RELEASES: {
      async get() {
        throw new Error("internal r2 blew up: account=secret-ish");
      },
      async head() {
        return null;
      },
    },
  };
  const r = await worker.fetch(new Request("https://dl.hyprial.com/wheel/internal/a.whl"), s as any);
  assert.equal(r.status, 503, "存储故障 = 503");
  assert.equal(r.headers.get("retry-after"), "1", "503 必须带 retry-after");
  const text = await r.text();
  assert.ok(!/secret-ish|internal r2/.test(text), `不得回显内部错误：${text}`);
});

// --- 二轮复核残余（emergent 转 e2e-verifier 2026-10-27）：416 的判据与 head() 兜底 ----------

test("残余①：带 Range 的**瞬态故障** ⇒ 503 + retry-after，⛔ 416（416 只由「不可满足」造成）", async () => {
  const body = bytes("0123456789");
  // 对象存在、head 正常（size=10）、range 可满足，但 get 抛一个与 range 无关的错误
  const s = stubR2({ "wheel/internal/a.whl": { bytes: body, etag: "ea" } }, { failRange: true });
  const r = await worker.fetch(
    new Request("https://dl.hyprial.com/wheel/internal/a.whl", { headers: { range: "bytes=0-4" } }),
    s as any,
  );
  assert.notEqual(r.status, 416, "可满足的 range + 存储故障 ⛔ 416（416 表示客户端请求的范围不存在）");
  assert.equal(r.status, 503, "存储故障 = 503（明确的「稍后可重试」）");
  assert.equal(r.headers.get("retry-after"), "1", "503 必须带 retry-after，否则客户端不知道可以重试");
  assert.equal(r.headers.get("content-type"), "text/plain; charset=utf-8");
  assert.equal(await r.text(), "storage unavailable", "固定正文，⛔ 回显内部错误");
});

test("残余①：不可满足的 range 由**对象大小**判定 ⇒ 416 + bytes */size，且不触达 get", async () => {
  const body = bytes("0123456789");
  const s = stubR2({ "wheel/internal/a.whl": { bytes: body, etag: "eb" } });
  const r = await worker.fetch(
    new Request("https://dl.hyprial.com/wheel/internal/a.whl", { headers: { range: "bytes=99-199" } }),
    s as any,
  );
  assert.equal(r.status, 416);
  assert.equal(r.headers.get("content-range"), `bytes */${body.byteLength}`);
  assert.deepEqual(s.calls, [], "不可满足时根本不必向 R2 取对象");
});

test("残余①：畸形/多段 Range 按 RFC 忽略（200 整份），⛔ 416 / ⛔ 500", async () => {
  const body = bytes("0123456789");
  for (const h of ["bytes=abc", "bytes=0-1,4-5", "items=0-1", "bytes="]) {
    const e = env({ "wheel/internal/a.whl": { bytes: body, etag: "ec" } });
    const r = await worker.fetch(
      new Request("https://dl.hyprial.com/wheel/internal/a.whl", { headers: { range: h } }),
      e,
    );
    assert.equal(r.status, 200, h);
    assert.equal(r.headers.get("content-range"), null, h);
    assert.equal(await r.text(), "0123456789", h);
  }
});

test("残余②：head() 也抛时 ⛔ 变成 Worker 崩溃（1101）⇒ 必须 503 + retry-after 且不回显内部错误", async () => {
  const body = bytes("0123456789");
  const s = stubR2({ "wheel/internal/a.whl": { bytes: body, etag: "ed" } }, { failHead: true });
  const r = await worker.fetch(
    new Request("https://dl.hyprial.com/wheel/internal/a.whl", { headers: { range: "bytes=999999999-" } }),
    s as any,
  );
  assert.equal(r.status, 503, "head() 失败 = 503（⛔ CF 1101 / 500）");
  assert.equal(r.headers.get("retry-after"), "1", "503 必须带 retry-after");
  assert.notEqual(r.status, 416);
  const text = await r.text();
  assert.equal(text, "storage unavailable");
  assert.ok(!/token=leaky|head blew up/.test(text), `不得回显内部错误：${text}`);
});
