**Verdict: FAIL.** The browser-facing security core holds up under attack, but four must-fix items remain: one CI step that cannot pass as written, one contract §6 violation, one dispatch-truth violation, and a set of contract MUSTs that no test pins.

The h2b worktree is clean at HEAD b27b95739. I removed everything I created: scratch worktree v2b-scratch-dev (used twice), the `/tmp/gui-host-driver-*` and `/tmp/hyprial-gui-KMWJBj` dirs and the orphaned daemons left by my kill experiments, and all processes on ports 4780x–4783x. `src/hyprial/_core/actors.abi3.so` was moved aside for one test and put back. Probe scripts are in the planner's scratch area.

## Gate results on HEAD (each run alone)
| Check | Result |
|---|---|
| `pytest tests/shell/gui/host -n 2` | 303 passed |
| `pytest tests/shell/gui -n 2` | 756 passed |
| vitest `tests/daemon-request-host`, `HYPRIAL_HOST_IMPL=python` | 141/141 passed, 8 files, 194.6 s |
| same, Node default | 141/141 passed, 82.5 s |
| `npm --prefix gui run typecheck` | ok |
| `ruff check .` | ok |
| structural limits | PASS (host dir 7 files, host tests 7 files, largest file app.py 592 lines) |
| module boundaries | 0 violations |
| `check_inline_comments.py --strict` | 0 new inline comments |
| `check_gui_request_paths.mjs` | PASS (27 findings, 8 allowlisted, 19 pending) |
| node `scripts/ci/*.test.mjs` | 162/162 passed |
| `check_contract_impact.py` | 0 candidates |
| `pytest tests/tooling/ci -n 2` | 959 passed, 1 failed |

The single `tests/tooling/ci` failure (`test_ci_dependency_caches.py::test_real_tree_denominator_and_family_witnesses`, "unaccounted current image contexts") also fails on dev-f, so it is **pre-existing**. `test_snapshot.py::test_intranet_gate_on_a_real_repo_snapshot_reports_only_pending_hits` fails on dev-f (scratch worktree, staged engine, `PYTHONPATH=src` with this venv) and on HEAD with the same assertion, so it is also **pre-existing**.

## Must-fix
1. **The new CI step cannot pass as written.**
   - `test:host-conformance:python` includes `real-daemon-loopback.test.ts`. That test starts a real daemon, and dev's default actor backend is `native` (`kernel/impl/actor_runtime/port/config.py:27`).
   - The `gui-unit` job never builds the native engine: there is no rustup or `build_native_engine.sh` step, and `uv sync` editable only verifies the engine (`gui_build_hook.py:200`).
   - Reproduced with `actors.abi3.so` moved aside: the Python run gives "Tests 3 failed (3)", with "actor backend 'native' cannot use hyprial._core.actors … fail closed".
   - The earlier step `npm --prefix gui test` already includes the same file. That part is pre-existing, and because of `if: success()` the new step may never even run.
   - Minimal fix: build or stage the native engine in `gui-unit`, or take `real-daemon-loopback` out of this step and record it as BLOCKED. The `.venv` part is fine: `uv sync --locked` (gate-gui.yml:200) runs before the step.
2. **The upstream client keeps a cookie jar (§6 "the host forwards only …").**
   - `httpx.AsyncClient(trust_env=False, follow_redirects=False)` at `upstream.py:113` stores upstream `Set-Cookie` and replays it on every later upstream call, across sessions and across authority rotations, because cookies are not scoped by port.
   - Probe evidence: after the fake upstream sent `set-cookie: evil=1`, it received `Cookie: evil=1` and then `Cookie: evil=1; evil=1` on later relays.
   - The test `test_relay_sends_only_the_host_header_set…` (test_upstream.py:333) checks a denylist, not an allowlist, and builds a fresh client per call, so it cannot catch this (mutant M28, an extra forwarded header, also survives).
   - Fix: a reject-all cookie policy, the same `DefaultCookiePolicy(allowed_domains=[])` the test helper already uses at support.py:312, plus an exact allowlist test over two consecutive relays.
3. **A dispatched write is reported as `not-dispatched` (§6/§8).**
   - `_json_object` (upstream.py:83) catches only `ValueError`/`UnicodeDecodeError`. A deeply nested valid JSON envelope from the upstream raises `RecursionError`.
   - That reaches the generic handler at app.py:294, which answers 500 `HOST_INTERNAL_ERROR` with `dispatch: "not-dispatched"` after the write was sent. Reproduced on `agent.create`. Telling the client "not dispatched" for a write that went out invites a duplicate on retry.
   - Related: a non-latin-1 upstream `requestId` escapes `send_reply` (envelope.py:139) as a bare uvicorn `text/plain` 500. A CR/LF in it makes uvicorn drop the connection with no response.
   - Fix: catch `RecursionError` (and header-encoding failures) as 502 `UPSTREAM_INVALID_RESPONSE` with `unknown` for writes. Make the generic handler report `unknown` once a write relay has started, and validate relayed header values before sending.
4. **Contract MUSTs no test pins.** Each mutant below survived both pytest and the vitest Python run (host, contract-mock-loopback and session-contract files):
   - M51: concurrent grant consumption. The code is correct (my probe of 40 parallel grants gave exactly 1×200 and 39×409), but the `consume_grant` re-check is unguarded.
   - M57: authority-file owner check.
   - M56: tokens of 16 bytes instead of 32.
   - M28: an extra header forwarded upstream.
   - M36: a body of exactly the limit is refused.
   - M26: the second authenticate before the relay (§5.1) is dropped.
   - M52: refresh probes the upstream before checking the control cookie (§5.1 order).
   - M31: the relay passes a 3xx body through; only the probe's redirect refusal is tested.

## Should-fix
- **NaN accepted.** A method body `{"a":NaN}` is accepted and relayed (Python `json.loads`); the Node host answers 400.
- **Deep request JSON.** A deeply nested request body gives 500, not 400 or a relay.
- **Browser request id.** A latin-1 `x-hyprial-request-id` fails inside httpx before anything is sent, yet is reported as 502 `UPSTREAM_UNAVAILABLE` with `unknown`.
- **`/%ff`.** A static path with invalid UTF-8 escapes gives 500 (assets.py:92, strict `unquote`).
- **No HTTP stack limits.** An 8 MB header and 100,000 headers are both accepted. Partial headers or a partial body hold a connection indefinitely (40 s+ observed). There is no `limit_concurrency` (800 idle connections accepted). The attacker would have to be local.
- **Proxy headers.** Uvicorn's `proxy_headers` is left on, so `X-Forwarded-For`/`-Proto` rewrite `scope.client`/`scheme`. Unused today; pass `proxy_headers=False`.
- **Windows authority file.** The behaviour is undefined, though it does fail closed. `owner_ok` is skipped without `getuid` (config.py:207) and `O_NOFOLLOW` falls back to 0, but Windows `st_mode` always has 0o066 set, so every authority file is refused with a 503 on every request. Nothing is silently accepted, so this is not a security hole. A clear startup refusal would be better.
- **`/daemon/../index.html`.** Answered 400 before the Host check (app.py:229), while the contract says a target outside `/daemon/*` after parsing is static.
- **Timeout margin.** The slowest Python `host.test` case takes 15.6 s against the 30 s timeout. That timeout applies only when `HYPRIAL_HOST_IMPL=python` (vitest.config.ts:16), and it hides no hang: 6 of 141 cases exceed 5 s, and the real-daemon cases set their own 60–120 s timeouts.

## Mutation table (61 mutants; pytest first, vitest Python run for pytest survivors)
| Result | Mutants |
|---|---|
| Killed by pytest (45) | M01 same-site accepted, M02 port check dropped, M03 Origin never required, M04 `null` Origin, M05 userinfo checks dropped, M06 `SameSite=Lax`, M07 no `HttpOnly`, M08 `Max-Age` 0, M09 refresh extends TTL, M10 control ignores view, M11 session ignores view, M12 grant check order, M13 `>=`→`>`, M14 grant before marker, M15 last cookie wins, M16 consume before probe, M17 no CSRF on logout, M18 version range relaxed, M19 empty version = absent, M20 no CSRF on writes, M22 browser digest ignored, M23 static paths skip the Host check, M24 405→404, M25 not `unknown` after a write, M29 digest precedence, M30 `trust_env=True`, M32 mock ignores wire `mutates`, M33 403 not treated as auth refusal, M34 browser request id ignored, M35 declared `Content-Length` check dropped, M38 empty header not absent, M39 group-readable authority accepted, M40 revision reuse, M41 mock without principal, M42 SPA fallback for extensions, M43 symlink escape, M44 symlinks unlinked, M45 files kept on consume, M46 `0.0.0.0` bind, M47 empty `Sec-Fetch-Site`, M48 status body check dropped, M49 refresh accepts keys, M50 500 leaks detail, M55 authority follows symlink, M58 logout keeps control, M59 refresh does not revoke |
| Killed by vitest (4) | M21 pin checked before catalog lookup, M37 media type case-sensitive, M53 HEAD without Origin, M54 empty/duplicate Origin read as absent |
| Survived, missing test (8) | M26, M28, M31, M36, M51, M52, M56, M57 (must-fix 4) |
| Survived, equivalent (2) | M27 (session phase and principal always agree, and an authority change revokes sessions); M60 (`==` vs constant-time is a timing property, not functionally testable) |

## Confirmed sound
- **Bind:** loopback only. `--host` accepts only `127.0.0.1`, `localhost` or `::1`; `0.0.0.0`, `::`, `127.0.0.2`, hostnames and uppercase all exit 2. The live socket is 127.0.0.1 only per `/proc/net/tcp` (IPv6 is unavailable on this machine).
- **Host check** runs on every path, including assets and `/__gui/healthz`:
  - JSON 403 `HTTP_HOST_DENIED` for: an `evil.example` DNS name, `127.1`, `0x7f.1`, unbracketed `::1`, userinfo, no port, port 80, a path, a `?` suffix, a missing or empty Host, and a duplicate Host.
  - `LOCALHOST` is accepted (case-folded) and `047811` is normalised.
  - An absolute-form target with a foreign authority is routed by its path, and the Host header still decides.
- **Origin and Fetch Metadata:** correct; no `Access-Control-*` header anywhere, and OPTIONS goes through the normal pipeline.
- **Secrets compared in constant time:** grant, cookies, view, generation and CSRF all use `hmac.compare_digest`.
- **Tokens:** `secrets.token_urlsafe(32)`, 43 characters.
- **Cookies:** `Path=/daemon; Max-Age; HttpOnly; SameSite=Strict`, no `Domain` or `Secure`; logout clears them with `Max-Age=0`. Ids are always server-minted, so there is no session fixation.
- **Grant:**
  - Single use under 40 concurrent requests.
  - A probe failure does not consume it.
  - Before `launch.ready` it is reported as expired, and the TTL then takes effect.
  - Both launch files are removed on consumption and on expiry; a symlink is never followed.
- **Principal:** browser headers such as `x-hyprial-principal`, `Authorization` and `x-forwarded-for` never reach the upstream.
- **Bearer:** absent from responses, stderr and the ready line after exercising upstream 401, redirects, non-JSON and malformed replies.
- **Proxy and netrc:** `HTTP(S)_PROXY`, `ALL_PROXY` and `.netrc` are ignored.
- **Framing:** CL+TE and duplicate CL answered 400 by the stack; declared over-limit 413 without reading; `Expect: 100-continue`, HTTP/1.0 and pipelining fine.
- **Static paths:** traversal (encoded, backslash, NUL, `//`), symlinks out of the root, no directory listing, extensions 404 rather than SPA, and `/daemon/*` never falls back to SPA.
- **Capability flags:** all five confirmed by direct probes:
  - Version negotiation: malformed values, duplicate headers and a wrong grant sent with an unsupported version all give 409 before the grant or the probe.
  - JSON 403 for DNS-name Hosts.
  - No CORS headers.
  - The mock driver takes `mutates` from the wire, falls back to local declarations, then to true.
  - The mock driver without a principal refuses to start (exit 2).
- **Dependencies:** uvicorn, httptools, httpx and websockets are base product dependencies in pyproject.toml, not extras; `uv.lock` and pyproject are unchanged against dev.
- **Layering:** nothing outside the package and its tests imports the host (CLI reachability is unchanged; `tests/shell/gui` passes). There is no typer import. The stdout ready and stopped lines are documented in `__main__.py`'s decision block and are outside contract §14.
- **CI pins:** removing the new step from a copy of the workflow makes `test_root_gui_workflow` and `test_release_package_test_set_cannot_shrink_silently` fail.
- **Browser smoke** (Chromium 1243, contract mock plus the Python host serving a fresh `gui/dist`):
  - The app boots ("Hyprial OS"). Its own `/daemon/*` calls were all 200: session, `onboarding.status`, session status.
  - The `#grant` fragment is scrubbed from the URL, and `document.cookie` is empty.
  - Without CSRF, `agent.create` and logout are both 403 `CSRF_REJECTED`; a read without CSRF is 200.
  - Cross-origin fetches with credentials from `localhost:47813` fail ("Failed to fetch"; no-cors yields an opaque response).

## Notes
- **Driver child cleanup:** no stray host processes after the suite. After `kill -9` of the vitest worker, the host child exited within 3 s, but the driver's temp asset dir leaked. The real-daemon test harness (existing code) left an orphaned `daemon run`.
- Dotfiles in the asset root (`.env`) are served.
- Health output adds `assets` and `assetsVerified` (an additive change).
- Static headers (`nosniff`; `no-store` on index; `immutable` on `assets/*`) are stricter than Vite preview's. That comparison was not measured; the contract is silent on static headers.
- On Windows, `.js` content types depend on the registry through `mimetypes`.
- Duplicate `Cookie` header lines are joined with ", ".

## UNKNOWN
macOS; Windows (startup, signals, the authority behaviour, mimetypes); an IPv6 bind (no IPv6 here); a real daemon beyond the loopback suite, which passed only because the native engine is staged locally; CI execution of the new step (see must-fix 1).