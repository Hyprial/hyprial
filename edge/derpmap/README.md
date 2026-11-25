# derpmap.hyprial.ai

Stable DERP map for the tailcat sidecar: the upstream default map (tailcat.dev)
merged with our own **region 900** relay. Every response contains region 900,
even when the upstream is unreachable (the bundled snapshot is used then).

## Response contract

`GET /derpmap.json` (and `HEAD`) return the merged map as `application/json`
with `cache-control: public, max-age=300`. The body is deterministic for a given
upstream: keys are sorted, region 900 is always present. Any other path is 404;
any other method on the path is 405 with `allow: GET, HEAD`.

Region 900 (source of truth: `src/region900.ts`):

| field | value |
| --- | --- |
| RegionID / RegionCode | `900` / `derp-900` |
| Node Name | `900a` |
| HostName | `6ad6d7fbc3f1e.cdnyou.com` |
| IPv4 | `139.196.101.212` |
| DERPPort | `1279` |
| CertName | `sha256-raw:f0cafbd75cec0ea983ec25e18213d9c849a84256bae7533f3eb00050db513dda` |

`InsecureForTests` is **not set at all**. The v2 experiment proved the
`sha256-raw:` CertName pin works with the flag false; setting it true removes
CA-chain checks and would silently disable verification if the pin were ever
cleared (`docs/notes/derp-900-local-experiment-v2-2026-10-20.md`).

## Operations runbook (infra-ops)

0. **Before every deploy**: `npm run snapshot` (refresh `src/snapshot.json` from
   the live upstream) and commit the result, so the fallback is never stale.
1. Fill `account_id` in `wrangler.toml` (placeholder today) and confirm the
   custom-domain route `derpmap.hyprial.ai` is the intended shape; custom domain
   makes Cloudflare create the DNS record and certificate.
2. `cd edge/derpmap && npm ci && npm test` then `npx wrangler deploy`.
3. Verify: `curl -sS https://derpmap.hyprial.ai/derpmap.json | jq '.Regions["900"].Nodes[0]'`
   and `curl -sSI ... | grep -i content-type`. A `405`/`404` check:
   `curl -sS -X POST -o /dev/null -w '%{http_code}\n' https://derpmap.hyprial.ai/derpmap.json`
   must print 405.
4. Run `npm run schema` after any change to region 900: it regenerates
   `test/schema/merged.json` from the Worker's own merge function and parses it
   as `tailcfg.DERPMap` with the pinned Go toolchain (GOTOOLCHAIN=local,
   GOPROXY=off). Requires the cached toolchain; set `DERPMAP_GO` to a different
   go binary if yours lives elsewhere.
5. Refresh the fallback snapshot after an upstream change:
   `npm run snapshot` (fetches https://tailcat.dev/derpmap.json, validates it parses
   as JSON, rewrites `src/snapshot.json`) and commit the result.
6. **Rotating the pin** (relay certificate renewed): compute the SHA-256 of the
   leaf certificate's raw DER, e.g.
   `openssl s_client -connect 139.196.101.212:1279 -servername 6ad6d7fbc3f1e.cdnyou.com </dev/null | openssl x509 -outform der | shasum -a 256`
   then replace the `CertName` value in `src/region900.ts`, deploy, and verify
   with the curl in step 3. Never shorten the pin to a name or a prefix.

## Not in scope here

No secrets or bindings are needed. The map login is deferred; phase 1 relies on
these regions plus `tailcat --region=auto` (one region per address, chosen at
server start; there is no runtime fallback — see
`docs/notes/derp-900-local-experiment-v2-2026-10-20.md` on `exp/derp-900-native`).
Do not deploy from an agent session.
