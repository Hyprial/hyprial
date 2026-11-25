import test from "node:test";
import assert from "node:assert/strict";
import worker, { mergeDerpMap, stableStringify } from "../src/index.ts";
import { REGION_900 } from "../src/region900.ts";

const ctx = { waitUntil: () => {} };
const get = (path = "/derpmap.json", method = "GET") =>
  worker.fetch(new Request(`https://derpmap.hyprial.ai${path}`, { method }), {}, ctx);

test("region 900 is present with the exact node fields and pin", async () => {
  const map = JSON.parse(await (await get()).text());
  const region = map.Regions["900"];
  assert.equal(region.RegionID, 900);
  const node = region.Nodes[0];
  assert.equal(node.HostName, "6ad6d7fbc3f1e.cdnyou.com");
  assert.equal(node.IPv4, "139.196.101.212");
  assert.equal(node.DERPPort, 1279);
  assert.equal(
    node.CertName,
    "sha256-raw:f0cafbd75cec0ea983ec25e18213d9c849a84256bae7533f3eb00050db513dda",
  );
  assert.equal("InsecureForTests" in node, false, "the escape hatch must not be set at all");
  assert.match(
    String(node.CertName),
    /^sha256-raw:[0-9a-f]{64}$/,
    "the pin is the sha256-raw digest of the leaf certificate",
  );
});

test("merging keeps upstream regions and always adds 900", () => {
  const merged = mergeDerpMap({ Regions: { "1": { RegionID: 1 }, "2": { RegionID: 2 } } });
  assert.deepEqual(Object.keys(merged.Regions!).sort(), ["1", "2", "900"]);
  assert.equal((merged.Regions!["900"] as typeof REGION_900).RegionID, 900);
});

test("upstream failure still returns the snapshot plus 900", () => {
  const merged = mergeDerpMap(null);
  assert.equal((merged.Regions!["900"] as typeof REGION_900).RegionID, 900);
});

test("response is deterministic for a given upstream", () => {
  assert.equal(stableStringify(mergeDerpMap(null)), stableStringify(mergeDerpMap(null)));
});

test("only GET and HEAD on /derpmap.json are served", async () => {
  assert.equal((await get("/derpmap.json", "POST")).status, 405);
  assert.equal((await get("/other")).status, 404);
  const head = await get("/derpmap.json", "HEAD");
  assert.equal(head.status, 200);
  assert.equal(await head.text(), "");
});

test("content type and cache-control are set", async () => {
  const response = await get();
  assert.equal(response.headers.get("content-type"), "application/json");
  assert.match(response.headers.get("cache-control") ?? "", /max-age=\d+/);
});

test("no served node enables InsecureForTests", async () => {
  const map = JSON.parse(await (await get()).text());
  for (const region of Object.values(map.Regions as Record<string, { Nodes?: Array<Record<string, unknown>> }>)) {
    for (const node of region.Nodes ?? []) {
      assert.notEqual(node.InsecureForTests, true, `node ${node.Name} disables verification`);
    }
  }
});

test("the fallback snapshot carries real default regions", async () => {
  const snapshot = JSON.parse(await (await import("node:fs/promises")).readFile("src/snapshot.json", "utf8"));
  const regions = Object.keys(snapshot.Regions ?? {});
  assert.ok(regions.length >= 4, `snapshot has only ${regions.length} regions`);
});
