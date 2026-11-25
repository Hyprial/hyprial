# 2a contract delta for the Python host implementer

Source of truth: `contract/gui-session/README.md` (section numbers below) and `gui-session.v1.json`.
Suites (all in `gui/tests/daemon-request-host/`): `A` = `conformance-admission.ts`, `S` = `conformance-session.ts`,
`R` = `conformance-relay.ts` (plain modules registered from `host.test.ts`, which owns the single upstream listener and
binds it into `host-harness.ts`), `H` = `host.test.ts`, `C` = `session-contract.test.ts`. A Python driver changes nothing
in these files except `host-driver.ts`.
Capability flags (driver-declared, README 15.1): `sessionContractNegotiation`, `transportHostCheck`, `transportCors`,
`mockWireMutates`, `mockRequiresPrincipal`. A Python driver must declare `true, false, false, true, true`; the Node driver is
`false, true, true, false, false`. Cases with a flag assert the contract when the flag says conforming and the exact Node
deviation otherwise, so the Python driver makes the contract branch run. Python driver additions needed in `host-driver.ts`:
`origin`, `listenAddress`, `capabilities`, `advance`, `close`, plus an entry in `DRIVERS` and `CAPABILITIES`.

## Changed or added normative statements

| Section | Rule | Pinned by |
|---|---|---|
| 2 | Bind loopback only; the driver reports `listenAddress` (127.0.0.1 or ::1) | A "binds only to a loopback address" |
| 2 | Host: WHATWG parse of `http://<Host>`; hostname case-folded; `[::1]` bracketed; port leading zeros normalised; port required and equal to the listener's (no port or 80 refused); no userinfo; empty path; routable IP literals refused: JSON 403 `HTTP_HOST_DENIED` | A "accepts a normalised loopback Host...", A "answers DNS-name, userinfo..." (`transportHostCheck`) |
| 2 | Origin: exact string equal to the normalised Host origin; empty `Origin` is present and `ORIGIN_REJECTED`; absent only on GET (HEAD, OPTIONS, DELETE, POST all need it) | A "compares Origin as an exact string", A "requires Origin on every method but GET" |
| 2 | `Sec-Fetch-Site` only `same-origin` or `none`, case-sensitive; empty value refused | A "accepts only same-origin and none", C `fetchSiteAccepted` both directions |
| 2 | No `Access-Control-*` headers ever; `OPTIONS` goes through the pipeline (403 `ORIGIN_REQUIRED` / 405) | A "refuses CORS..." (`transportCors`) |
| 3 | Grant check order invalid 403, used 409, expired 401; a used grant whose TTL passed is "used" | S "reports a used grant as used..." |
| 3 | A grant presented while a named `launch.ready` is absent is 401 `LAUNCH_GRANT_EXPIRED`; usable (TTL then) once the marker appears | S "treats a grant presented before the publication marker..." |
| 3 | The grant is consumed only by a successful mint; a failed probe leaves it (and the launch files) in place | S "does not consume the grant when the upstream probe fails" |
| 3/5 | Initial body exactly `{"grant":"<non-empty string>"}`, else 400 before any grant check | S "answers a malformed initial body..." |
| 4 | Control expiry fixed at mint (`mint + sessionTtl`), never extended; refreshed session `expiresAt = min(now + ttl, control.expiresAt)` so a refresh returns the mint's `expiresAt`; cookie `Max-Age = max(1, floor(remaining/1000))` per value; refresh after control expiry is 401 | S "fixes the control expiry at mint" |
| 4 | A refused refresh changes nothing (earlier business session stays live) | S "changes nothing when a refresh is refused" |
| 4 | Cookie parsing: first non-empty duplicate wins, empty values and nameless pairs skipped, undecodable first value 400; logout cookies `name=; Path=/daemon; Max-Age=0; HttpOnly; SameSite=Strict` | S "requires the exact empty object..." (logout cookies), S "parses cookies: the first non-empty duplicate wins..." |
| 5 | Request target: WHATWG-parsed path; dot segments (incl. `%2e`) normalised, not rejected; absolute-form routed by path; `%2F` not a separator; trailing slash is another path; query/fragment split at first `?`/`#` | A "normalises dot segments..." |
| 5 | Query strings: `readyz` drops it, catalog and method routes forward verbatim, session/status/logout ignore | R "forwards the query..." |
| 5 | Body checks in order: content-type (case-insensitive, parameters ok) 415, size 413, JSON 400, object 400; empty body is invalid JSON | S "reads and validates the body before it authenticates...", S "accepts a case-insensitive application/json..." |
| 5 | Refresh and status bodies exactly `{}`; logout accepts any JSON object | S "requires the exact empty object..." |
| 5 | Status: no upstream contact, `current:false` without session cookie or with a retired generation, 401 without control proof | S "reports status without contacting the upstream..." |
| 5.1 | Global order: classify route, Host, Origin, Fetch Metadata, read authority (503), route; malformed method name is 404 before any 405 | A "runs Host, Origin, Fetch Metadata and the route in that order...", S authority case |
| 5.1 | Per-route order (session: 405, body, shape, version, grant/control, probe, re-check, consume; status/logout read the body before authenticating; method route: 405, authenticate, 404, 409 not callable, 409 digest, 403 CSRF, body, re-check) | R "authenticates before it reads the body...", S "reads and validates the body before it authenticates..." |
| 5.1 | Authenticate: wrong view (even with wrong generation) 401, right view wrong generation 403 | R "authenticates before it reads the body..." |
| 6 | Empty `x-hyprial-*` request header counts as absent (digest, CSRF) | R "authenticates before it reads the body..." (empty digest, empty CSRF), H logout CSRF "empty" |
| 6 | Relayed digest: upstream header, else body `catalogDigest`, else pin | R "prefers the upstream digest header, then body.catalogDigest, then the pin" |
| 6 | Relayed request id: body `requestId`, else upstream header, else absent; dispatch: header, else body, else absent; never on host-built responses | R "prefers the upstream body requestId..." |
| 6 | Relay passes status and body bytes for any status, drops every other upstream header, sets `Content-Length`; to upstream only accept/content-type/pin/request id/bearer | R "relays any upstream status and body..." (upstream-bound headers: existing H "preserves query, request id...") |
| 6 | Method envelope: `schemaVersion 1`, boolean `ok`, non-empty `requestId`, valid `dispatch`, `method` equals name, else 502 `UPSTREAM_INVALID_RESPONSE` (`unknown` write / `not-dispatched` read); readyz/catalog relays only need a JSON object | R "refuses a relayed method envelope...", R "refuses a readiness or catalog relay..." |
| 7 | Every failure on `/daemon/request/<x>` carries `method: x` (incl. malformed names, raw remainder); no other route does; host failures carry `X-Hyprial-Request-Id` equal to the body and no dispatch header | A "puts the request id in the header and the body...", R check-order case |
| 7 | `data.upstreamCode` only on probe status refusals (503 `HTTP_NOT_READY`, other-status 502); never on the direct not-ready refusals | R "maps upstream refusals as specified..." |
| 9 | Serving principal exactly `{kind:"serving", owner, node, actor:"user:<owner>"}`; corpus actor pattern `^user:.+$` | S "serves the launcher principal in the exact serving shape", C schema case |
| 10 | Probe criteria, in order, for the daemon driver (steps 1-11) and the Mock driver; body parsed before status (non-JSON 401/403 is `UPSTREAM_INVALID_RESPONSE`); other probe status echoes the upstream `code` at 502; unreachable is 503 `UPSTREAM_UNAVAILABLE` | R "maps upstream refusals...", R "refuses an unreachable upstream...", R "pins the catalog it probed...", R "refuses probe answers that are not the Contract Mock" |
| 10 | Mock driver: synthesised readyz and catalog bodies (exact shape), `declarationDigest` = digest, epoch `contract-mock:<digest>`, all methods `callable`/`available`, `mutates` = wire boolean, else local declarations (`agent.list` false, `agent.create` true), else true | R "serves a ready session, synthesises readiness and catalog..." (`mockWireMutates`) |
| 10 | Mock driver without a serving principal: the host refuses to start | R "refuses to start without a serving principal..." (`mockRequiresPrincipal`) |
| 11 | Authority file: minimum shape, read per request after origin checks, any change revokes business sessions, unsafe/invalid/repeated-revision file is 503 until a never-seen valid revision, flips phase at next refresh | S "stays unavailable on every request...", S "flips a bootstrap view..." |
| 13 | Version check is the first step after method and body checks on `POST /daemon/session` (grant and refresh forms), before grant check, probe and consumption; malformed header (`abc`, `1.0`, empty, `-1`, `+1`, `0`, `01`, repeated) is 409 `SESSION_CONTRACT_UNSUPPORTED` with `data {min,max}`; `sessionContract.version` is the applied version (client's, else 1) | S "refuses an unsupported or malformed version..." and "applies version 1..." (`sessionContractNegotiation`) |
| 13 | Adding a route is an additive revision, listed in the corpus, integer version unchanged (§12 and §13 now agree) | README only |
| 15 | Vite-fronted Node deviations and the five capabilities | C "declares the driver capabilities..." |
| 15.2 | Implementation-defined items with reasons | README only |

## Test-side changes the Python driver relies on

- `host-harness.ts` now holds the shared helpers (its `listen` delegates to the listener bound by `host.test.ts`) (`upstream`, `mockUpstream`, `call`, `rawCall`, `open`, `publishAuthority`, ...);
  `host.test.ts` imports them. `rawCall` writes the request line verbatim (dot segments, absolute-form).
- `session-corpus.ts`: `loadSessionContract()` walks every subschema and throws on unsupported keywords; keywords next to
  `$ref` now apply; `required`/`properties` are own-property checks.
- The README line budget in `C` is 1000 for README and corpus.

## Not pinned by the shared suite (README-only)

- Host with `?`/`#` suffix, IPv4 shorthand, `//` targets, HTTP stack pre-host errors (15.2).
- "revoke before validation" of an invalid authority file: not observable black-box (every request is 503), stated in section 11.
- Behaviour of a file that returns to its last accepted bytes after an invalid interval (Node accepts it; a new revision is the
  only recovery the suite exercises).
