## Verdict: FAIL

The code side is sound: the CSRF change is correct, the driver seam and the gates are fine, and every command passes. The verdict is FAIL because of the contract itself. Its job is to let a Python host reproduce the Node host exactly, and in several places it states something different from what the host does, leaves out behaviour the host has, or contradicts itself. One security property (CSRF on logout) is not pinned by any test, so a Python host could drop it and still pass the suite. All the fixes are small README/corpus edits plus one test case.

Worktree left clean: HEAD is still bdd21587b. My scratch tests (`vfyzz-*`) and the temporary base worktree are removed. Every file I mutated was restored with `git checkout`.

## Must-fix findings

**1. The relayed `x-hyprial-catalog-digest` is described wrongly.**
- README §6 and corpus `headers.response` say it is "the pinned digest on relayed responses".
- The host actually uses the upstream's header first, then `body.catalogDigest`, then the pin (`upstream.ts:344`).
- A probe confirmed it: when the upstream sent `upstream-digest`, the browser received `upstream-digest`, not the pinned `d`.
- The request-id order is also unstated: the body's `requestId` is used first, then the upstream header (`upstream.ts:343`).
- Fix: state both precedence orders.

**2. Session and control lifetime is ambiguous, and a divergent host would pass.**
- The control's expiry is fixed when the session is minted (`session.ts:154`). A refresh never extends it.
- Each refreshed session gets `expiresAt = min(now+ttl, control.expiresAt)` (`session.ts:260`). Cookie `Max-Age` is the time remaining (`session.ts:305`).
- Probe: a refresh 1.2 s into a 2 s TTL returned an `expiresAt` change of 0 and `Max-Age=1` on both cookies.
- README §4 only says "the session TTL bounds both cookies".
- A mutant that extends the session on refresh passed every test.
- Fix: state the rule in the README and add a test.

**3. A grant used before the publication marker exists is refused, and the contract doesn't say so.**
- The host returns 401 `LAUNCH_GRANT_EXPIRED` (`session.ts:128,137`); the probe confirmed it.
- The check order (invalid, then used, then expired) and "a failed probe does not consume the grant" are also not stated.
- Fix: add these to §3 or §5.

**4. The versioning rules are incomplete, and §12 contradicts §13.**
- **Check order:** nothing requires the `SESSION_CONTRACT_UNSUPPORTED` check to happen before the grant check, the probe and consumption. A Python host that checks late would burn the one-time grant.
- **Malformed header:** a value like `abc`, `1.0`, empty, or a repeated header has no defined handling.
- **Response field:** the meaning of `sessionContract.version` is undefined (the client's version? the host's max?).
- **Contradiction:** §13 says adding a route is a compatible change; §12 says new routes "enter the closed route set only with a contract bump".
- Fix: state the version check as the first step after the method/body checks on `POST /daemon/session`, define the malformed case (I suggest a 409 refusal), define `version`, and resolve the route contradiction.

**5. §15's list of Node deviations is incomplete, which matters now that the driver runs on Vite preview.**
- In `vite preview` and the dev server, Vite's CORS and Host-validation middleware run before the plugin (`gui/node_modules/vite/dist/node/chunks/node.js:35823-35826`, and `:25058` for dev).
- Effects seen in the probe:
  - `Host: evil.example:<port>` or `127.1:<port>` gets a `text/plain` 403 "Blocked request", not JSON `HTTP_HOST_DENIED`. IP literals such as `10.0.0.1` do reach the host and get JSON `HTTP_HOST_DENIED`.
  - Any `OPTIONS` request to `/daemon/*` is answered 204 by Vite's CORS middleware.
  - `Access-Control-Allow-Origin` echoes any localhost-style Origin on every response.
- None of this is a hole: the host still enforces the Origin check. But a conformance case asserting JSON `HTTP_HOST_DENIED` for a DNS name would fail on the Node driver.
- Fix: list these in §15.

**6. Logout CSRF is not pinned by any test.**
- I removed `requireCsrf` from `logout()` (`proxy.ts:303`); `session-contract.test.ts` and `host.test.ts` all stayed green.
- The corpus says CSRF is `always` required for logout, and this suite becomes the Python host's spec.
- Fix: add one `host.test.ts` case where logout without CSRF, or with a stale one, returns 403 `CSRF_REJECTED`.

**7. The corpus wording on `HTTP_NOT_READY` is inaccurate.**
- The `when` text says "data.upstreamCode carries its code".
- Only the `statusFailure` path sets `data` (`upstream.ts:134-140`). The direct paths do not (`upstream.ts:177,182,189,210,266`).
- Fix: say `data` is present only when the upstream answered 503.

## Contract gaps the Python host implementer must know
These are things the Node host does that the contract omits or leaves vague. They are not blocking, but each one can make the Python host diverge.

1. **Check order inside routes:**
   - Method route: 405 → authenticate (401 / 403 `GENERATION_REJECTED`) → 404 not in pin → 409 not callable → 409 digest pin → 403 CSRF → body checks (415 → 413 → 400) → authority re-check → re-authenticate → relay (`proxy.ts:460-507`). Probe: junk text/plain with no auth gets 401, not 415.
   - Session, status and logout read the body before authenticating.
   - Global order: Host → Origin → Sec-Fetch-Site → authority read → route; a malformed `/daemon/request/<bad>` gives 404 before the 405 check.
2. **Query strings:** `GET /daemon/readyz` drops the query when relaying (`proxy.ts:426`; the upstream saw `GET /readyz`). The catalog and method routes keep it.
3. **Logout body:** any JSON object is accepted (`proxy.ts:301`; `{"extra":1}` returned 200). The corpus form says `{}`. Status and refresh do require `{}`.
4. **Method envelope check** (`upstream.ts:316-331`): `schemaVersion===1`, `ok` boolean, non-empty `requestId`, `dispatch` in the enum, `method===name`. Otherwise 502 `UPSTREAM_INVALID_RESPONSE`. A non-JSON or non-object relayed body for readyz or the catalog is also 502. Relayed status codes and non-2xx bodies pass through; every other upstream header is dropped; `Content-Length` is set.
5. **Mock-driver synthesized bodies** are unspecified: readyz `{ready, transport, driver, backend{available,epoch}, catalog{fresh,catalogDigest,declarationDigest}}` and the catalog `catalogBody` (`proxy.ts:308-335,412-423`). The client consumes `GET /daemon/request`. The mock probe derives `mutates` from the GUI build's `methodMetadata`, defaulting to true (`upstream.ts:280`). All methods are `callable`, `state: available`; `backendEpoch` is `contract-mock:<digest>`; the declaration digest equals the digest.
6. **Mock driver with no principal:** phase is `ready` and the principal is `{kind:'serving', owner:null,…}`. That fails the corpus schema (checked with both validators) and §9, and the client would reject it. The launcher always gives the mock driver a principal (`admission.py` `_admit_mock`). The contract should require one, or the Python host should refuse.
7. **Probe edge:** an upstream 401/403 whose body is not JSON becomes `UPSTREAM_INVALID_RESPONSE`, not `UPSTREAM_AUTH_REJECTED`, because the body is parsed before the status is checked (`upstream.ts:125` vs `175`). Any other probe status echoes the upstream's own code at 502, so the "closed" error table is open at 502. The probe acceptance criteria themselves are not in the contract.
8. **Host and URL normalisation:**
   - The Host check uses WHATWG URL: the hostname is case-folded, `[::1]` is the accepted form (`session.ts:71`), and port leading zeros are normalised. Origin is compared exactly against the normalised origin. A Host on port 80 parses with no port and is always refused.
   - Request targets are normalised: `/daemon/x/../session` is routed to the session route (probe: `LAUNCH_GRANT_INVALID`); absolute-form targets are routed by their path. The plan's "reject dot segments with 400" therefore diverges, and "unparseable target" is vague.
9. **Failure headers and fields:** host failures carry `X-Hyprial-Request-Id` but no `x-hyprial-dispatch`. Every failure on a `/daemon/request/<name>` path carries `method: <name>` (`proxy.ts:100-103,512`). An empty header value counts as absent. Duplicate cookies: the first wins; empty values are skipped.
10. **Authority file:** any content change revokes business sessions before it is validated; an invalid file also revokes and keeps returning 503 until a valid new revision appears. Revisions never repeat. A principal change flips the phase (bootstrap → ready) at the next refresh. The status route does not probe, and returns `current:false` when the session cookie is missing.
11. **Loopback bind:** the README requires the Host check but never states "bind only to loopback" as a requirement (plan A.9).

## Checks by point

**1. Contract accuracy.** Covered by the findings above. Everything else matches: routes and methods, cookie names and attributes, limits, TTL defaults and ranges, the error codes with their statuses and dispatch values, the principal shapes, logout, launch-file removal and rotation.

**2. CSRF change.** Correct.
- The host enforces CSRF only from its own pinned list (`proxy.ts:470-491`). Nothing from the client's view is consulted.
- The client's method list comes from that same host's session body, so the trust anchor is the host. A "hostile list" would mean a hostile host, which is already fully trusted.
- An unknown name still falls back to the generated metadata, then to true (`browser-session.ts:31-39`). The pinned `mutates` is used only for CSRF inclusion (`transport.ts:161`), not for retries.
- Sending CSRF on a read is harmless: the host ignores it and never forwards it.
- Red→green: with `browser-session.ts` reverted to base, 3 of the 5 new tests fail (CSRF from the list, and the header on grant and refresh); HEAD passes 20/20. The other two new tests pass on both base and HEAD.

**3. Driver seam.**
- Removed or merged test cases: none. The 79 base test names (7 files) are all present at HEAD, which has 96 (8 files). The "74 in 5 files" figure was the subset of five files the plan timed.
- Assertions changed:
  - The `onLaunchGrantConsumed` counter was replaced by checking that the real `launch.url` and `launch.ready` files are gone (stronger).
  - The injected-fetch case was replaced by an upstream that destroys the socket, plus exact `captured` URLs (equivalent).
  - The repeated-grant attempt no longer re-checks the counter (moot, since the files are already gone).
  - The `now()` clock was replaced by real sleeps.
- Timing: the two TTL cases now take about 1.03 s each instead of about 10 ms, roughly 2 s more for `host.test.ts`. Durations otherwise track machine load (load average 8–15 on 4 cores):

| Run | Test count | Durations |
|---|---|---|
| HEAD, `--maxWorkers=1`, 4 runs | 96/96 each | 57.3 s, 69.6 s, 83.9 s, 52.9 s |
| Base, `--maxWorkers=1`, 2 runs | 79/79 each | 46.0 s, 50.8 s |
| HEAD, default workers, 3 runs | 96/96 each | 34.4 s, 37.3 s, 49.1 s |

  No flake in any run. The sleeps are flake-safe: a delay only deepens expiry, and no case needs a 1 s session to still be alive.
- Ledger: allowlist 11 → 8, pending 19 → 19, so shrink-only. `--write-pending` reproduces the committed file byte for byte (the writer keeps the allowlist; the three rows were hand-removed). The gate passes and rejects nothing.

**4. Teeth (my own mutations).**
- **Caught:**
  - corpus: header rename, dropping `::1`, `cache-control`, request-id prefix, method pattern, healthz field;
  - host: method regex, Origin required only on POST, `SameSite=Lax`, CSRF on reads, CSRF removed entirely, a new code name in the same status class.
- **Survived:**
  - corpus drops `none` from `fetchSiteAccepted` (the scan checks one direction only);
  - host also accepts `Sec-Fetch-Site: same-site`;
  - logout without CSRF (must-fix 6);
  - serving `actor` replaced by `owner`;
  - relayed digest forced to the pin;
  - refresh extending the session.

**5. Schema validator** (`session-corpus.ts`).
- It throws on unsupported keywords (`maxLength`, `else`, `additionalProperties`) at any schema it actually visits.
- Results matched Python `jsonschema` (Draft 2020-12, via `uv run --with jsonschema`) on: the live daemon session response (valid), the mock-anonymous response (invalid in both), the status, logout and error responses, all examples, and three broken variants. All four corpus schemas pass `check_schema`.
- Gaps (non-blocking, since the corpus uses none of these):
  - keywords in unvisited subschemas are never checked (an absent property, an unused `$defs` entry), so "never silently unchecked" in its header is overstated;
  - sibling keywords next to `$ref` are ignored (2020-12 applies them);
  - `required` uses `in`, so `required:['constructor']` passes on `{}`.

**6. Gates on HEAD:**

| Check | Result |
|---|---|
| `npm --prefix gui run typecheck` | exit 0 |
| `npm --prefix gui test` (with `DEV01_CJK_FONT`) | 196 files: 195 passed, 1 failed; 2273 tests: 2271 passed, 1 failed, 1 skipped; 644 s. The only failure is `dev01-onboarding-browser` at `tests/dev01-onboarding-browser.mjs:91` (allowed) |
| `node --test gui/tests/architecture/daemon-request.node.mjs` | 9/9 |
| request-architecture gate | PASS |
| request-paths gate | PASS (findings 27, allowlist 8, pending 19) |
| request-paths selftest | 128/128 |
| unavailable-copy gate | PASS |
| node22 `scripts/ci/*.test.mjs` | 156/156 |
| `./contract/run-all.sh --isolated-only` | exit 0 |
| pytest `tests/tooling/ci tests/tooling/contracts -n 4` | 1080 passed (the flaky resolver test did not fail) |
| `ruff` | clean |
| structural limits / module boundaries | exit 0 |
| inline comments `--strict` | 0 new |
| contract-impact | 0 candidates |
| `test:browser` | 5/5 |
| `build` | exit 0 |
| `verify:dev-product-parity` | passed |

**7. Hygiene.**
- All 8 commits are conventional (`docs`, `test`, `fix`, `feat`, `refactor`, `design`).
- Each commit passes `tsc --noEmit` on its own.
- No `skip`/`only`/`todo` added; no new inline comments.
- `contract/gui-session` README is 230 lines, the corpus 443.
- No new root-level files.

## Non-blocking notes
- `contract/README.md` says the contract is checked "双侧校验" (on both sides); today only the Node side exists.
- The pinning tests are source-text scans, so they break on harmless refactors of the Node host's source. Acceptable, since that host is deleted in PR 3.
- Plan step 2a-1 asked for the test before the corpus; the commits land the corpus first.

## UNKNOWN
- Python-host conformance (no Python driver yet).
- `test:daemon-request-browser` (M8 pre-existing failures; not run).
- Behaviour of real browsers against the Vite-fronted host beyond `test:browser`.
- Whether the one skipped test in `npm test` predates this branch (this PR adds no skip).