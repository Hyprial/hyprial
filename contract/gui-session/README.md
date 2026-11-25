# GUI session contract v1

The browser session contract between the GUI client (`gui/src/app/backend/browser-session.ts`)
and the same-origin host that serves it. Two parties, possibly built from different commits:
a JavaScript client and a host (the Python session host in
`src/hyprial/shell/impl/gui/host/`; the Node host that predates it was deleted on
2026-11-16). This file is normative prose; `gui-session.v1.json` is the machine corpus
(routes, headers, cookies, limits, check order, the closed error table, the session body
schema). It is tested from both sides: the conformance suite in
`gui/tests/daemon-request-host/` drives the Python host over HTTP, `session-contract.test.ts`
checks the corpus itself and the client, and `tests/shell/gui/host/` checks the host's
constants against the corpus. There is no `contract/run-all.sh` step.

Purpose: a host written from this document and `gui-session.v1.json` alone must be
indistinguishable from the reference host for every statement below, and the shared suite is
the test of that. Anything not stated here is implementation-defined (section 15.2).

Words: MUST, MUST NOT and MAY are normative. "Refused" means a host failure envelope with the
named status and code (section 7).

Out of scope: which business methods exist (the catalog's job), the upstream's endpoint and
bearer (for the daemon driver no bearer crosses a socket or the environment: the host holds it in
memory), runtime files other than the two pinned in sections 3 and 11, the launcher-to-host
ready line. Those are launcher-to-host details shipped from one wheel at one commit.

## 1. Parties and trust

| Party | Trust | Role |
|---|---|---|
| launcher (`hyprial gui`) | trusted | names the driver and the one-time grant; with the Contract Mock also the principal, the upstream endpoint and its bearer |
| host | trusted | owns browser sessions, same-origin admission, bounded framing; relays method calls as bytes |
| browser view | untrusted | holds cookies and a view proof; never chooses an endpoint, a principal or a catalog |
| upstream | trusted by the host only | `request_http` in the host's process (daemon driver) or the Contract Mock over loopback; a bearer the browser never sees |

A native shell is a browser and nothing more: it opens the same origin and has no IPC to it.

## 2. Origin

One loopback HTTP origin serves the page and `/daemon/*`.

**Bind.** The host MUST listen only on a loopback address (`127.0.0.1` or `::1`), never on a
wildcard or a routable address, and MUST report the address it bound (the conformance driver
exposes it as `listenAddress`). The Host check below is a second line of defence, not a
substitute for the bind.

Every request MUST satisfy, in this order:

1. **Host.** `Host` is present and non-empty. It is parsed as the authority of `http://<Host>`
   with WHATWG URL rules: the hostname is case-folded, IPv6 is written `[::1]`, a port with
   leading zeros is normalised. The hostname MUST be `localhost`, `127.0.0.1` or `[::1]`; the
   normalised port MUST be present and equal to the listener's port (so a Host without a port,
   or on the default port 80, is always refused, because its origin carries no port); there
   MUST be no userinfo and the path MUST be empty. Otherwise 403 `HTTP_HOST_DENIED`. A
   hostname that is an IP literal other than the two loopback ones (`10.0.0.1`) is
   refused the same way.
2. **Origin.** `Origin` MUST equal the normalised origin of that Host (`http://127.0.0.1:41234`,
   lower case, no leading zeros), compared as an exact string; `null`, a different scheme,
   host or port, or a differently cased host is 403 `ORIGIN_REJECTED`. An empty `Origin` value
   is a present value and is refused the same way. `Origin` may be absent only when the method
   is `GET`; absent on any other method (`HEAD`, `OPTIONS`, `DELETE`, `POST`) is 403
   `ORIGIN_REQUIRED`.
3. **Fetch Metadata.** `Sec-Fetch-Site`, when present, MUST be `same-origin` or `none`
   (case-sensitive, as browsers send them); `same-site`, `cross-site`, any other value and the
   empty value are 403 `ORIGIN_REJECTED`.

The rule applies to every path the origin serves. A host MUST NOT answer CORS: it adds no
`Access-Control-*` header to any response and treats `OPTIONS` like any other method (the
Origin step first, then 405 `HTTP_METHOD_NOT_ALLOWED` on a `/daemon/*` route).

Header names are case-insensitive; an empty value of an `x-hyprial-*` request header counts
as absent (section 6).

## 3. Launch

- The grant travels only in the URL fragment: `<origin>/#grant=<one-time grant>`. The client
  removes it with `history.replaceState` before any request; it is never in a query, a header,
  storage or a log.
- It is valid once. Its TTL (default 60 s) starts when the launcher publishes the readiness
  marker (`launch.ready`) if one is named, otherwise at host construction.
- A grant presented while the launcher names a marker that does not exist yet is expired: 401
  `LAUNCH_GRANT_EXPIRED`. It becomes usable, with its TTL starting then, once the marker
  appears.
- Grant check order on a presented grant: **invalid** (not the launcher's grant, compared in
  constant time) is 403 `LAUNCH_GRANT_INVALID`; then **used** (already consumed) is 409
  `LAUNCH_GRANT_USED`; then **expired** (TTL passed, or marker not yet published) is 401
  `LAUNCH_GRANT_EXPIRED`. A grant that was used and whose TTL has also passed is therefore
  reported as used.
- The grant is consumed only by a successful mint. A request that is refused for any other
  reason, including a failed upstream probe (503, 502), does not consume it and the same grant
  may be presented again. Consumption happens after the probe and the authority re-check
  (section 11).
- The only other carrier is the 0600 handoff file (`launch.url`) written for `--no-open`. The
  host removes `launch.url` and `launch.ready` on grant consumption, on expiry and on close,
  regular files only, never through a symlink.
- `hyprial gui --json` never contains the grant, the upstream bearer or a URL that carries a
  credential; `handoff_path` names the 0600 file.

## 4. Session model

| Value | Carrier | Rotates |
|---|---|---|
| control id | cookie `hyprial_gui_control` | never during a view; revoked on logout and expiry |
| business session id | cookie `hyprial_gui_session` | every handshake |
| view proof | header `x-hyprial-view`; browser `sessionStorage` | never; a refresh must return the same view |
| generation | header `x-hyprial-generation` | every handshake |
| CSRF | header `x-hyprial-csrf` | every handshake |

Both cookies: `Path=/daemon; Max-Age=<seconds, at least 1>; HttpOnly; SameSite=Strict`, no
`Secure` (loopback HTTP), no `Domain`. Values are percent-encoded; tokens are 32 random bytes,
base64url, compared in constant time. Cookie parsing: the `Cookie` header is split on `;`,
each pair on its first `=`; a pair without a name, or with an empty value, is skipped; for a
duplicated name the first non-skipped value wins (a later duplicate is never decoded); an
undecodable first value is 400 `INVALID_REQUEST`.

- A copied business cookie without the view proof is not a session (401 `SESSION_REQUIRED`).
  A control cookie can only refresh the view it was minted for.
- Refresh revokes this control's earlier business sessions and issues a new one with the same
  view, a new generation and a new CSRF value. A refresh that is refused (for example because
  the upstream probe fails) changes nothing: the earlier business session stays live.
- Sessions are in memory. A host restart ends them; durable state stays in the daemon.
- Logout revokes the session and the control and expires both cookies: `Max-Age=0`, empty
  value, same `Path`, `HttpOnly` and `SameSite` attributes.
- Capacity (default 32) is defensive: one host has one grant and so one control, and refresh
  revokes before it issues, so 429 `SESSION_CAPACITY` is unreachable over HTTP. It is pinned by
  unit tests, not by the HTTP suite.

**Lifetime.** `sessionTtl` defaults to 12 h and is between 1 s and 24 h.

- The control's expiry is fixed at mint: `control.expiresAt = mintTime + sessionTtl`. A refresh
  MUST NOT extend it. A refresh presented after it is 401 `SESSION_REQUIRED`.
- Every business session, at mint and at each refresh, gets
  `expiresAt = min(now + sessionTtl, control.expiresAt)`. The response's `expiresAt` is that
  value in epoch milliseconds, so every refresh returns the same `expiresAt` as the mint did,
  and a view lives at most one `sessionTtl` from its grant, however often it refreshes.
- A session or control is expired when `expiresAt <= now`.
- Each cookie's `Max-Age` is the time remaining to the expiry of the value it carries
  (control cookie: the control's; session cookie: the session's) as whole seconds rounded
  down, never less than 1, computed when the response is built.

## 5. Routes

All `/daemon/*` routes first run the checks of section 5.1.

| Route | Needs | Result |
|---|---|---|
| `POST /daemon/session` body `{"grant":"..."}` | Origin | check the grant, probe the upstream, consume the grant, remove launch files, mint control and business sessions. Body: `sessionResponse` (see corpus). Two `Set-Cookie`. |
| `POST /daemon/session` body `{}` | Origin, control cookie, `x-hyprial-view` | probe the upstream, refresh as in section 4. Same body and cookies. |
| `POST /daemon/session/status` body `{}` | Origin, control cookie, `x-hyprial-view` | `{"current": bool}`: is the presented session cookie and generation the live one for this control. Read-only: it does not contact the upstream. `false` when the session cookie is absent, retired or expired; 401 when the control proof is missing or wrong. |
| `POST /daemon/session/logout` | Origin, session cookie, `x-hyprial-view`, `x-hyprial-generation`, `x-hyprial-csrf` | revoke session and control, expire both cookies, `{"revoked": true}` |
| `GET /daemon/readyz` | session cookie, view, generation; optional digest pin | the upstream's readiness, relayed (the Contract Mock driver synthesises it from the session pin) |
| `GET /daemon/request` | same | the upstream's catalog, query preserved (the Contract Mock driver synthesises it from the pin) |
| `POST /daemon/request/{method}` | same, plus CSRF when the pinned catalog says `mutates` | the method call, relayed as bytes, query preserved |
| `POST /daemon/session/attach` body `{}` (only with developer tools, section 12) | Origin, control cookie, session cookie, `x-hyprial-view` | `{generation, csrf, expiresAt}` of the live business session of that control (`attachResponse`). It contacts no upstream, rotates nothing and sets no cookie. 401 `SESSION_REQUIRED` when the control proof is missing or wrong, or the session cookie is absent, retired, expired or not of that control. |
| `GET /daemon/openapi.json` (only with developer tools and the daemon driver) | session cookie, view, generation | the upstream's request schema, projected: only the paths `/daemon/request` and `/daemon/request/*`; no `security`, no `servers`, no `components.securitySchemes`. The query is dropped. A non-200 upstream answer is reported like a failed probe (section 10). |
| any other `/daemon/{anything else}` | none | 404 `METHOD_NOT_FOUND` |

**Request target.** The host routes by the path of the request target parsed with WHATWG URL
rules against a dummy base, for origin-form (`/daemon/x?y`) and absolute-form
(`http://host/daemon/x`) targets alike. Dot segments (`.`, `..`, `%2e`, `%2E`) are normalised,
not rejected: `/daemon/x/../session` is the session route. A `%2F` is not a separator and no
other percent-decoding is applied to the path: `/daemon/request/a%2Fb` is the (malformed)
method name `a%2Fb`. A trailing slash is a different path (`/daemon/readyz/` is 404). The query
is the text from the first `?`; a fragment is dropped. A target that does not parse and starts
with `/daemon/` is 400 `INVALID_REQUEST`, answered before the Host check; it is unreachable
through a conforming HTTP/1.1 stack, and a target not starting with `/daemon/` after parsing
(including one starting with `//`) is not a `/daemon/*` request at all.

**Query strings.** `GET /daemon/readyz` drops the query: the upstream sees `GET /readyz`.
`GET /daemon/request` and `POST /daemon/request/{method}` forward the query verbatim, byte for
byte, after the path. The session, status and logout routes ignore it.

**Bodies.** Session, status and logout bodies are at most 8 192 bytes, method bodies 131 072
bytes (413 `HTTP_REQUEST_TOO_LARGE`; a body of exactly the limit is accepted, and a declared
`Content-Length` above the limit is refused without reading). The checks run in this order:
`Content-Type` is `application/json`, case-insensitive, parameters allowed, anything else or
absent is 415 `HTTP_UNSUPPORTED_MEDIA_TYPE`; size; the body parses as JSON, else 400
`INVALID_REQUEST`; the value is an object (not array, scalar or `null`), else 400
`INVALID_REQUEST`. An empty body is invalid JSON. Failures on `/daemon/request/{method}` carry
`method` (section 7).

- The initial session body is exactly `{"grant":"<non-empty string>"}`; any other key set,
  or a non-string or empty grant, is 400 `INVALID_REQUEST`.
- The refresh body and the status body MUST be exactly `{}`; any key is 400 `INVALID_REQUEST`.
- The logout body MUST be any JSON object. Its contents are ignored: `{"extra":1}` is accepted.
  Clients send `{}`.

A wrong HTTP method on a route is 405 `HTTP_METHOD_NOT_ALLOWED`.

A method name must match `^[a-z][A-Za-z0-9]*(?:[._-][A-Za-z0-9]+)*$` and contain no `/`;
otherwise the path is not a method route and is 404 `METHOD_NOT_FOUND` with `method` set to
the raw remainder after `/daemon/request/` (before the 405 check, whatever the HTTP method).
A well-formed name must exist in the session's pinned catalog (404 `METHOD_NOT_FOUND`) and be
`callable` (409 `METHOD_NOT_CALLABLE`).

### 5.1 Check order

Order is observable, because the first failing check is the one reported. It is normative.

**Global.** Route classification (the target parse above) first; then, for every `/daemon/*`
request: Host, Origin, Fetch Metadata (section 2); then read the current authority (503
`GUI_AUTHORITY_UNAVAILABLE`, section 11); then the route. So a request to an unknown
`/daemon/nope` with a bad Host is `HTTP_HOST_DENIED`, a POST without Origin to it is
`ORIGIN_REQUIRED`, and with an unusable authority it is 503, not 404.

**Within a route.**

| Route | Order |
|---|---|
| `POST /daemon/session` | 405 → body (415, 413, 400 JSON, 400 object) → body shape (400) → **contract version** (409, section 13) → grant form: grant check (403, 409, 401) → probe (section 10) → authority re-check (409 `GUI_SESSION_CHANGED`) → consume → mint; refresh form: control cookie and view present (401) → control valid (401) → probe → authority re-check → refresh |
| `POST /daemon/session/status` | 405 → body (415, 413, 400, 400 object) → body is `{}` (400) → control valid (401) |
| `POST /daemon/session/logout` | 405 → body (415, 413, 400, 400 object) → authenticate (401 `SESSION_REQUIRED`, then 403 `GENERATION_REJECTED`) → CSRF (403) → revoke |
| `GET /daemon/readyz`, `GET /daemon/request` | 405 → authenticate → digest pin (409 `CATALOG_MISMATCH`) → relay or synthesise → authority re-check |
| `POST /daemon/request/{method}` | 405 → authenticate → not in the pinned catalog (404) → not callable (409) → digest pin (409) → CSRF if `mutates` (403) → body (415, 413, 400, 400 object) → authority re-check (409) → authenticate again (401) → relay → authority re-check (409, `unknown` after a write) |

Consequences that the suite pins: session, status and logout read the body before they
authenticate (an unauthenticated `text/plain` call is 415, not 401); a method call
authenticates before it looks at the body (the same call is 401); an unknown method with a
wrong digest is 404; a non-callable one with a wrong digest is 409 `METHOD_NOT_CALLABLE`; a
wrong digest on a mutating method without CSRF is `CATALOG_MISMATCH`; a missing CSRF on a call
with a `text/plain` body is `CSRF_REJECTED`. Authenticate means: a session cookie, view and
generation (section 4): unknown, expired, revoked or view-mismatched session is 401
`SESSION_REQUIRED`, a live session with a different generation is 403 `GENERATION_REJECTED`;
a wrong view together with a wrong generation is 401. Logout does not look at the catalog
digest.

## 6. Headers

Names are case-insensitive; the corpus lists them in lower case. An empty value of an
`x-hyprial-*` request header counts as absent.

| Direction | Header | Meaning |
|---|---|---|
| request | `x-hyprial-view` | view proof |
| request | `x-hyprial-generation` | current generation |
| request | `x-hyprial-csrf` | CSRF value, sent for mutating methods and logout |
| request | `x-hyprial-catalog-digest` | the digest the session pinned, 409 `CATALOG_MISMATCH` on difference; absent means no check |
| request | `x-hyprial-request-id` | browser request id, forwarded upstream when present |
| request | `x-hyprial-session-contract` | the version the client speaks, on `POST /daemon/session`; absent means 1 |
| response | `x-hyprial-request-id` | host-built: the envelope's `requestId`. Relayed: the upstream body's `requestId`, else the upstream's `x-hyprial-request-id` header, else absent |
| response | `x-hyprial-catalog-digest` | host-built readiness and catalog: the pinned digest. Relayed: the upstream's `x-hyprial-catalog-digest` header, else the upstream body's `catalogDigest`, else the pinned digest |
| response | `x-hyprial-dispatch` | relayed: the upstream's `x-hyprial-dispatch` header, else the body's `dispatch`, else absent. Never on a host-built response |

Host-built responses carry `Content-Type: application/json; charset=utf-8`,
`Cache-Control: no-store` and `X-Content-Type-Options: nosniff`, and `X-Hyprial-Request-Id`
equal to the body's `requestId`.

**To the upstream** the host forwards only: `accept: application/json`; `content-type:
application/json` when there is a body; `x-hyprial-catalog-digest` set to the pinned digest
(never the browser's); `x-hyprial-request-id`, the browser's when non-empty, otherwise the
host's id; and its own `authorization: Bearer <token>`. Cookies, `origin`, `host`, view,
generation, CSRF and every other browser header are never forwarded. The body is the browser's
bytes, unchanged. Redirects are not followed (a redirect is a transport failure) and the
upstream timeout is 15 s.

**From the upstream** the host relays the status code and the body bytes unchanged for any
status, 2xx or not, and rebuilds the headers: the three fixed headers above, the three
`x-hyprial-*` response headers by the rules in the table, and `Content-Length`. Every other
upstream header (`set-cookie`, `content-type` variants, anything custom) is dropped. The body
MUST parse as a JSON object, otherwise 502 `UPSTREAM_INVALID_RESPONSE` (for a write,
`dispatch: unknown`). For `POST /daemon/request/{method}` the body MUST also be a method
envelope: `schemaVersion === 1`, `ok` a boolean, `requestId` a non-empty string, `dispatch`
one of the three values, and `method` equal to the requested method name; otherwise 502
`UPSTREAM_INVALID_RESPONSE` with `method` (`unknown` for a write, `not-dispatched` for a read).
Readiness and catalog relays are not checked beyond being a JSON object.

## 7. Envelope and errors

Host-generated failures use `{schemaVersion: 1, ok: false, requestId, code, message,
dispatch}` plus optional `method` and `data`. Host request ids are `gui-<uuid>`.

- Every failure on a path under `/daemon/request/` that names a method (`/daemon/request/<x>`
  with a non-empty `<x>`) carries `method: "<x>"`, whatever the failing check (405, 401, 403,
  404, 409, 413, 415, 400, 502 from the relay), including a malformed name (its raw
  remainder). Failures on every other route, and on `GET /daemon/request`, carry no `method`.
- A host failure carries `X-Hyprial-Request-Id` equal to its `requestId` and no
  `x-hyprial-dispatch` header.
- `data` is present only where the corpus says so (`HTTP_NOT_READY`, `UPSTREAM_NOT_READY` and
  upstream-coded probe refusals: `data.upstreamCode`; `SESSION_CONTRACT_UNSUPPORTED`:
  `data.min`, `data.max`).

The closed table of host codes, with statuses and the dispatch values each may carry, is
`errors` in the corpus; a code may be added within an existing status class without a version
bump (section 13). Upstream envelopes that pass through are opaque bytes governed by
`contract/http-request` and `contract/daemon-request`; the host rebuilds only their headers.

| Status | Codes |
|---|---|
| 400 | `INVALID_REQUEST` |
| 401 | `SESSION_REQUIRED`, `LAUNCH_GRANT_EXPIRED` |
| 403 | `HTTP_HOST_DENIED`, `ORIGIN_REQUIRED`, `ORIGIN_REJECTED`, `LAUNCH_GRANT_INVALID`, `GENERATION_REJECTED`, `CSRF_REJECTED` |
| 404, 405, 413, 415, 429 | `METHOD_NOT_FOUND`, `HTTP_METHOD_NOT_ALLOWED`, `HTTP_REQUEST_TOO_LARGE`, `HTTP_UNSUPPORTED_MEDIA_TYPE`, `SESSION_CAPACITY` |
| 409 | `LAUNCH_GRANT_USED`, `METHOD_NOT_CALLABLE`, `GUI_SESSION_CHANGED`, `SESSION_CONTRACT_UNSUPPORTED`, `CATALOG_MISMATCH` (browser digest) |
| 500 | `HOST_INTERNAL_ERROR` |
| 502 | `UPSTREAM_AUTH_REJECTED`, `PRINCIPAL_MISMATCH`, `UPSTREAM_CONTRACT_MISMATCH`, `CATALOG_MISMATCH` (upstream digests), `UPSTREAM_UNAVAILABLE`, `UPSTREAM_INVALID_RESPONSE`, `UPSTREAM_RESPONSE_TOO_LARGE`, `UPSTREAM_NOT_READY` |
| 503 | `GUI_AUTHORITY_UNAVAILABLE`, `HTTP_NOT_READY`, `UPSTREAM_UNAVAILABLE` (probe) |

The probe's mapping of an upstream refusal is stated in section 10. The closed table is open
in one place: a probe refusal with an upstream status other than 401, 403, 503 is 502 carrying
the upstream's own `code`, any non-empty string, with `data.upstreamCode`. The limits: upstream
response 4 MiB (`UPSTREAM_RESPONSE_TOO_LARGE`), upstream timeout 15 s.

## 8. Dispatch truth

Every failure and receipt says whether the operation reached the daemon:
`not-dispatched`, `dispatched` or `unknown`. The host never retries. A read that fails before
a response is `not-dispatched`; a write whose response was lost, unreadable or invalid is
`unknown`, and the client reconciles instead of replaying.

## 9. Principal projection

The session body's `principal` is derived from the session phase and the launcher's
configuration, never from a request:

- `anonymous-bootstrap`: `{kind, owner: null, node: null, actor: null}`, phase `bootstrap`,
  an ownerless daemon only.
- `serving`: `{kind, owner, node, actor: "user:<owner>"}`, all non-empty, phase `ready`.

The probe refuses an upstream whose identity does not match (502 `PRINCIPAL_MISMATCH`).

## 10. Catalog pin and upstream probe

The Contract Mock driver is always `ready` and always serves a serving principal. The host
MUST refuse to start (a configuration error at construction, before listening) when the driver
is the Contract Mock and the launcher gave no serving principal.

At every mint and refresh the host probes the upstream and the session pins its catalog digest
and method list. `mutates` and `callable` in that list are authoritative: the host decides
CSRF and admission from them, and the client takes the digest, the method list, `callable`
and `mutates` from the session, not from its build. A client build whose generated metadata
disagrees with the session follows the session. The probe is two `GET` requests with
`accept: application/json` (all with the bearer except the Contract Mock's `/healthz`), and
any of its failures is `not-dispatched`.

**Probe transport.** Unreachable, timed out or unreadable upstream: 503 `UPSTREAM_UNAVAILABLE`;
a body over 4 MiB: 502 `UPSTREAM_RESPONSE_TOO_LARGE`. The body is parsed before the status is
looked at: a body that is not a JSON object is 502 `UPSTREAM_INVALID_RESPONSE` whatever the
status (so a non-JSON 401 or 403 is `UPSTREAM_INVALID_RESPONSE`, not `UPSTREAM_AUTH_REJECTED`).
For a JSON object with a status other than 200: 401 or 403 is 502 `UPSTREAM_AUTH_REJECTED`;
503 is 503 `HTTP_NOT_READY` with `data.upstreamCode` (the body's `code`, else
`UPSTREAM_NOT_READY`); any other status is 502 with the body's `code` (else `UPSTREAM_NOT_READY`)
and `data.upstreamCode` equal to it. `data` appears only on these status refusals, never on the
direct refusals below.

**Daemon driver** (`GET /readyz`, then `GET /daemon/request`). In this order:

1. `ok === true`, `ready === true`, `transport === "ready"`, else 503 `HTTP_NOT_READY`.
2. `backend` and `catalog` objects present, else 502 `UPSTREAM_CONTRACT_MISMATCH`;
   `backend.available === true`, else 503 `HTTP_NOT_READY`.
3. `backend.owner` present on the wire as a string or an explicit `null`; an absent or
   non-string key is 502 `UPSTREAM_CONTRACT_MISMATCH`.
4. `catalog.fresh === true`, else 503 `HTTP_NOT_READY`.
5. `backend.nodeId`, `backend.epoch`, `catalog.catalogDigest` and `catalog.declarationDigest`
   non-empty strings, else 502 `UPSTREAM_CONTRACT_MISMATCH`.
6. Identity against the launcher: a bootstrap view requires `owner === null`; a serving view
   requires `owner` and `nodeId` equal to the principal's; otherwise 502 `PRINCIPAL_MISMATCH`.
7. `notReady` a non-empty array: 503 `HTTP_NOT_READY`.
8. Catalog (`GET /daemon/request`; status handled as above): `ok === true`, else 502
   `UPSTREAM_CONTRACT_MISMATCH`; `schemaVersion === 1` and `generatedFrom === "daemon.catalog"`,
   else the same; its `backend` and `principal` present, `backend.owner` as in step 3, and the
   catalog's backend owner, `nodeId` and `epoch` equal to the readiness ones, else the same.
   `principal.owner` and `principal.actor` present (string or `null`, else 502
   `UPSTREAM_CONTRACT_MISMATCH`) and, for a bootstrap view, both `null`, for a serving view
   equal to the principal's `owner` and `actor`; otherwise 502 `PRINCIPAL_MISMATCH`.
9. `catalogDigest` present, equal to the readiness digest and, when the upstream sent an
   `x-hyprial-catalog-digest` header, equal to that too; `declarationDigest` present and equal
   to the readiness one; otherwise 502 `CATALOG_MISMATCH`.
10. `methods` an array, each entry with a non-empty `name`, boolean `mutates` and `callable`
    and non-empty `state`, else 502 `UPSTREAM_CONTRACT_MISMATCH`.
11. `httpContractVersion` is the body's positive integer, else 1; `catalogVersion` the body's
    non-empty string, else `"1"`; `backendEpoch` the readiness `epoch`.

**Contract Mock driver** (`GET /healthz` without the bearer, `GET /mock/contract`):
`/healthz` status 200 and `ok === true`, else 503 `HTTP_NOT_READY`; `/mock/contract` status
200 (else as above), `mock === true`, a non-empty `contractDigest` and a `methods` object whose
every value has a `scenarios` array, else 502 `UPSTREAM_CONTRACT_MISMATCH`. The session is:
phase `ready`, driver `contract-mock`, `catalogDigest` and `declarationDigest` both the
`contractDigest`, `backendEpoch` `contract-mock:<contractDigest>`, `httpContractVersion` 1,
`catalogVersion` the body's `contractVersion` when a non-empty string, else `"1"`; every
method `callable: true`, `state: "available"`; `mutates` is the method entry's boolean
`mutates` when the wire carries one (the Contract Mock publishes the declared value of every
method), otherwise `true`: a missing or non-boolean field is treated as mutating, so CSRF is
required. A host MUST NOT guess `false` and MUST NOT consult its own build's method
declarations; it is not a contract mismatch and the probe still succeeds.

The Contract Mock driver synthesises these two responses from the session pin, with
`X-Hyprial-Request-Id` and the pinned `X-Hyprial-Catalog-Digest`:

- `GET /daemon/readyz`: `{schemaVersion: 1, ok: true, requestId, ready: true, transport:
  "ready", driver: "contract-mock", backend: {available: true, epoch}, catalog: {fresh: true,
  catalogDigest, declarationDigest}}`.
- `GET /daemon/request`: `{schemaVersion: 1, ok: true, requestId, httpContractVersion,
  catalogVersion, catalogDigest, declarationDigest, generatedFrom: "contract-mock", backend:
  {kind: "contract-mock", epoch}, principal, methods: [{name, path:
  "/daemon/request/<name>", state, callable, mutates}]}`. The method list is the session's
  pinned list, in order, and `principal` is the session's projection.

## 11. Authority change

A new upstream authority (for example when a bootstrap home is promoted: the bearer and the
principal change) reaches the host in memory. With the daemon driver the host's own principal
watcher hands it over; no file or environment variable carries it, and the launcher writes
none. With the Contract Mock the authority never changes. The browser can never supply, read
or alter it. The rest of the launcher-to-host protocol is not part of the contract
(section 14).

The host reads its current authority at the start of every `/daemon/*` request (after the
origin checks):

- The same authority object: nothing happens.
- A new authority object revokes every business session (the control proofs stay) and becomes
  the current authority: the upstream endpoint and bearer, and the principal (a bootstrap view
  or a serving one). A changed principal flips the phase at the next refresh (bootstrap to
  ready).
- An authority source that fails, or that no longer serves, is 503
  `GUI_AUTHORITY_UNAVAILABLE`, revokes the business sessions, and keeps being 503 on every
  request.
- A request in flight when the authority changes is 409 `GUI_SESSION_CHANGED` (`unknown` if a
  write was already sent, else `not-dispatched`). The view then refreshes with its control
  cookie and view proof; it never replays the interrupted call.

## 12. Static resources

A host that serves the page serves `/`, hashed `assets/*`, a single-page fallback to
`index.html`, and `/__gui/healthz`. The health probe is for the launcher and carries no
business data: `ok`, `service`, `mode`, `dataMode`, `driver`, `backendConfigured`, `session`.
Nothing else outside `/daemon/*` is served by a host without developer tools.

**Developer tools.** A host started with the argument `--dev-tools` serves more; nothing else
turns them on (no environment variable, cookie, header, query or storage key), and a page
learns of them only from the session body, which then carries `devTools: {"apiDocs": bool}`
(absent means none). `/__gui/healthz` carries `devTools: bool` for the launcher, and the host's
ready line carries `devTools: bool` and `apiDocs: bool`. `apiDocs` is
`true` only for the daemon driver with the API docs files present. The corpus section
`static.developerTools` is the machine form of this paragraph.

| Path | Without the flag | With the flag |
|---|---|---|
| `dev-tools/*` | 404 `NOT_FOUND`, never the single-page fallback, whatever the tree contains | the file, `Cache-Control: no-store`; a missing file is 404 |
| `/docs-assets/*` | 404 `NOT_FOUND` | with `apiDocs`: `hyprial-docs.js`, `swagger-ui-bundle.js`, `swagger-ui.css`; anything else 404 |
| `/docs` | the single-page fallback; 404 `NOT_FOUND` in front of a live source | with `apiDocs`: the host's API docs document; otherwise as without the flag |
| `POST /daemon/session/attach`, `GET /daemon/openapi.json` | 404 `METHOD_NOT_FOUND`, before the method check | section 5 |

The prefix test is made on the path after one percent-decoding with the first segment
compared case-folded, and again on the file an explicit root resolved to, so a symbolic link
inside the root does not reach a tool file. A hard link is not covered: in an explicit root a
second name outside the prefix for the same file is served like any other file. The verified
tree serves the names of its manifest only. A host in front of
a live source (`--live-upstream`) answers the 404s of the first column itself and never asks
the dev server, and it never relays `/docs`: that path is the API docs document or 404. With
the flag it relays `dev-tools/*` like any other source path.

The API docs document is a static shell with no schema, session value or credential, sent
with a `Content-Security-Policy` that allows scripts and connections from this origin only,
`X-Frame-Options: DENY`, `nosniff` and `no-store`. Cookies are `Path=/daemon`, so the
document cannot be cookie-gated; its script reads the per-view proof from the
`sessionStorage` key `hyprial.gui.session-view`, which a tab opened from the page with its
opener kept inherits, calls `POST /daemon/session/attach`, then `GET /daemon/openapi.json`,
and attaches again before each call it makes to `/daemon/request/{method}`. Without the proof,
or when attach is refused, it navigates to `/` and shows no text. The upstream bearer never
reaches a browser: the document has no credential field and removes any `Authorization`
header before a request is sent.

## 13. Versioning

An integer `sessionContractVersion`, 1 for this document, independent of any commit. The host
declares the range it serves, the client declares the version it speaks, and the host applies
one version to the session it mints.

| Rule | Text |
|---|---|
| Host declares | the session body gains `sessionContract: {version, min, max}`. A body without it means `{1, 1, 1}`. `min` and `max` are the range the host serves; `version` is the version the host applied to this session: the client's declared version, 1 when the client declared none. |
| Client declares | request header `x-hyprial-session-contract: <n>` on `POST /daemon/session`, both the grant and the refresh form. Absent means 1, so older GUI builds keep working. |
| Header value | a decimal integer `>= 1` written as digits only, no sign, no leading zero, no fraction, no whitespace: `1`, `2`, `17` (corpus pattern `^[1-9][0-9]*$`). `abc`, `1.0`, `-1`, `+1`, `0`, `01`, the empty value and a header that appears more than once (more than one field line, whatever the values) are malformed and are refused like an out-of-range version. |
| Refusal | a malformed value, or a version outside `[min, max]`, is 409 `SESSION_CONTRACT_UNSUPPORTED`, `not-dispatched`, `data: {min, max}` (integers). The client shows its bootstrap error page; it does not guess. |
| Check order | the version check is the first step after the method and body checks of `POST /daemon/session` (section 5.1): before the grant check, the upstream probe, the control check and the consumption. A refused version therefore never consumes the grant and never contacts the upstream; a wrong grant sent with an unsupported version is reported as the version. |
| Compatible | adding a response field, an error code inside an existing status class, a route. Both sides ignore what they do not know. An additive change is a revision of this contract: it lands in the corpus (a new route is added to `routes`) and in section 16, the integer version stays, and a client built before it is unaffected because it never calls a route its corpus does not list and ignores fields it does not know. |
| Breaking | removing or renaming a field, header or cookie; changing a status or dispatch value; changing who supplies a value. Needs a new version, and the host serves `[min, max]`. |

## 14. Not in the contract

The upstream endpoint and bearer, runtime and private files (`launch.url`,
`launch.ready`), the
launcher-to-host environment, and the `HYPRIAL_GUI_READY` line. They are shipped together and
may change with the wheel.

## 15. Conformance

One black-box suite. `gui/tests/daemon-request-host/` starts the Python host as a child
process through `gui/tests/support/python-host.ts` and speaks HTTP to its origin; nothing in
it imports a host implementation. `tests/shell/gui/host/test_contract_corpus.py` pins the
host's constants and every code, status and dispatch literal to the corpus, and
`session-contract.test.ts` pins the corpus's own consistency. A case that depends on a
statement here asserts it unconditionally; none is skipped. One exception, since 2026-11-20:
section 11 (authority rotation and revocation) cannot be triggered from outside the host, so
the shared suite has no case for it. It is tested in-process by
`tests/shell/gui/host/app/test_authority.py`,
`tests/shell/gui/host/local/test_local_daemon_driver.py`,
`tests/shell/gui/host/local/test_contract_corpus_local.py` and
`tests/integration/gui_principal_change`.

The Node host that served this contract before 2026-11-16 deviated from it in places that a
conforming host does not share (it checked Host and Origin on `/daemon/*` only, was fronted by
Vite's host-validation and CORS middleware, ignored `x-hyprial-session-contract`, and
started a Contract Mock session without a serving principal). It is deleted and the suite
states only the contract.

### 15.2 Implementation-defined

The following are deliberately not pinned, each for the stated reason. Everything else in this
document is pinned, in particular dot segments, empty header values and duplicate cookies:
the reference host's choice there is the contract, so that a later host does not drift.

| Left open | Why |
|---|---|
| the text of `message` (it MUST be non-empty); key order in JSON; HTTP status text | no client branches on them; `code` is the identifier |
| the bytes of a token beyond "32 random bytes, base64url" | opaque to every party |
| a repeated single-valued request header (`Origin`, `Host`, `x-hyprial-view`, ...) other than `Cookie` and the version header | HTTP stacks join or keep one value differently; a browser never sends one twice |
| IPv4 shorthand or non-decimal Host spellings of loopback (`127.1`, `0x7f.1`): WHATWG parsing turns them into `127.0.0.1`; a host MAY refuse them | no browser sends them for a `http://127.0.0.1:<port>` page |
| a `Host` with a `?` or `#` suffix | a browser never sends one; the origin comparison still decides |
| `//...` request targets and anything else outside `/daemon/*` | those are the static server's, not the host's |
| requests an HTTP stack rejects before the host runs (HTTP/1.1 without `Host`, an unparseable request line, an over-long header section) | the stack answers with its own error page; the host is never called |
| capacity beyond the HTTP-unreachable 429 | defensive only |
| logging, thread or event-loop model, keep-alive | invisible on the wire |

## 16. Decisions

- 2026-11-15, location: `contract/gui-session/` because the two parties are a JS client and a
  host that may be different commits, and `contract/` is what `Contract-Impact` checks.
- 2026-11-15, negotiation is additive (a response field and an optional request header, absent
  means 1) so GUI builds older than this contract and hosts that do not know it both work.
- 2026-11-15, the Node host is described as it behaves today; the corpus adds only the
  negotiation, which no deployed pair depends on.
- 2026-11-15, the contract is a specification for a second host: where the Node host is
  looser than a stated rule, the rule wins and the difference is a section 15 deviation.
- 2026-11-15, adding a route is an additive revision, not a new integer version; it is
  recorded here and in the corpus.
- 2026-11-15, the Contract Mock publishes each method's declared `mutates` in
  `GET /mock/contract` and a host takes `mutates` from the wire boolean, otherwise `true`
  (section 10): a revision inside version 1, not a new integer version. The mock gains a
  field, which both sides ignore if they do not know it (section 13, Compatible). The first
  clause of the old rule already made the wire value win; what is removed is the middle
  fallback to the host build's own declarations, which let a host call a method read-only
  that the mock declared mutating, or the other way round for a method the build did not
  know. A mock that does not publish the field now yields `true` (fail closed) instead of a
  guess from the build. The integer version stays 1: the only producer is this repository's
  mock, updated in the same change. The Node deviation `mockWireMutates` is gone; the
  capability stayed in the corpus with `true` in both columns (removed 2026-11-16, below).
- 2026-11-16, developer tools (PR 4b-H): an additive revision inside version 1. The corpus
  gains the routes `session.attach` and `daemon.openapi` (both marked `"flag": "devTools"`),
  the optional session field `devTools`, the schema `attachResponse` and
  `static.developerTools`. A host without the flag answers exactly as before, apart from the
  new `devTools: false` in `/__gui/healthz` and 404 for `dev-tools/*` and `/docs-assets/*`,
  which no client requested. A second document needs attach because a refresh from it would
  retire the opener's session (section 4); attach grants nothing a refresh does not already
  grant to the same proof. Owner decisions Q-1 and Q-2 of the developer tools hand-over.
- 2026-11-16, the Node host is deleted (PR 3, one launch path). The corpus loses its
  test-harness-only `conformanceCapabilities` section, which listed where the Node driver
  deviated; no route, header, cookie, limit, error or schema changed, so the contract version
  stays 1 and no wire behaviour moves.
- 2026-11-20, the file authority is removed (owner approval, #2822). The host's authority
  reader and its black-box conformance cases are deleted. Section 11 (authority rotation and
  revocation) can no longer be triggered from outside the host, so the shared black-box suite
  has no case for it; it is tested in `tests/shell/gui/host/app/test_authority.py`,
  `tests/shell/gui/host/local/test_local_daemon_driver.py`,
  `tests/shell/gui/host/local/test_contract_corpus_local.py` and
  `tests/integration/gui_principal_change`. Section 15.2 loses the row about a file returning
  to its last accepted bytes. The `when` texts of `GUI_AUTHORITY_UNAVAILABLE` and
  `GUI_SESSION_CHANGED` and `checksInOrder` say "host authority" instead of "launcher
  authority"; no code, status, key or order changed.
