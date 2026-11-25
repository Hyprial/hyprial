# Identity device authority client (one request per TLS connection)

This contract covers the identity-owned client for the two device-mTLS routes
`POST /v1/transport/device-certificate` (enroll and renew) and
`POST /v1/transport/retire-binding`. It stores nothing and commits nothing; the
M1 transport credential boundary remains the only local writer. The origin, SNI
and timeouts are supplied by trusted local configuration. There is no product
default, discovery, proxy, redirect or `Host`/`Forwarded` derivation.

## Request framing

Exactly one HTTP/1.1 request per TLS connection. The request is:

```text
POST <route> HTTP/1.1
Host: <configured origin host[:port]>
Content-Type: application/json
Content-Length: <exact body bytes>
Connection: close
```

The body is the exact strict route body, serialized with compact separators and
no extra fields:

- device-certificate: `version`, `operationId`, `deviceUid`,
  `expectedCredentialGeneration`, `csrPem`;
- retire-binding: `version`, `operationId`, `bindingId`,
  `expectedCredentialGeneration`.

`version` is the constant `hyprial.transport.v1`. Maximum request and response
size is 65536 bytes. `operationId` and `bindingId` are canonical hyphenated
UUID4 text, `deviceUid` is the 32-character lowercase UUID4 hex form, and
generations are non-boolean integers in `0..<2**31` (`>= 1` for retirement).

The client performs one TLS handshake with TLS 1.2 or newer, `check_hostname`,
`CERT_REQUIRED`, ALPN `http/1.1`, no compression, and only the configured
dedicated trust bundle as its trust anchor. When the route requires mTLS, the
client chain and key are read through the M1 no-follow safe readers and loaded
without exposing a path to the caller. Connect, handshake, read and write
timeouts are required positive values.

## Response handling

The connection is closed after the response is read under the 65536-byte cap.
The status line uses `HTTP/1.1 SP 3DIGIT SP [reason]` with both single-SP
separators required, including when the reason is empty. Following
[RFC 9112 section 4](https://www.rfc-editor.org/rfc/rfc9112.html#section-4),
reason bytes may contain HTAB, SP, visible ASCII or obs-text (`0x80..0xff`);
other C0 control bytes and DEL are invalid. Invalid status framing returns
`TRANSPORT_RESPONSE_INVALID` / `UNKNOWN` before authority envelope acceptance,
blocks a fresh operation on the route and permits exact replay. The reason
is never used as a diagnostic or identity signal. Header-value ASCII and OWS
constraints below are independent and unchanged.
Only SP and HTAB are optional whitespace around a header value (RFC 9110
§5.6.3); every other control or non-ASCII byte in a value is noncanonical. The
client rejects chunked transfer encoding, content encoding, upgrade, trailers,
keep-alive, interim or non-HTTP/1.1 status lines, duplicate or noncanonical
`Content-Length` (`digits` only, no padding beyond SP/HTAB), and any byte beyond
the declared body. A declared length with more digits than the response cap can
represent is rejected before numeric conversion. A 3xx is
`TRANSPORT_REDIRECT_REFUSED` with outcome `UNKNOWN`: a redirect is not a
validated authority rejection and cannot establish whether the sent mutation
committed. Its status and operation ID are retained, `retryable` stays false,
and the client never follows its location. An unchanged explicit replay remains
possible under the rules below.

A success response is the exact envelope
`{ok:true,version,operationId,data}` with HTTP 200 and `data` exactly:

- device-certificate: the 15 Interface A fields;
- retire-binding: `retired` (boolean true), `remoteEnforcementState`
  (non-empty string), `operationId` (equal to the request).

A rejected response is the exact envelope
`{ok:false,version,operationId,code,retryable,data:null}` with a non-2xx status.
The client preserves the authority `code` verbatim and requires the frozen
status and retryable flag for it:

| Status | Codes | Retryable |
| --- | --- | --- |
| 400 | `BAD_REQUEST`, `CSR_INVALID` | no |
| 401 | `TOKEN_INVALID`, `DEVICE_AUTH_REQUIRED`, `CREDENTIAL_EXPIRED`, `CREDENTIAL_REAUTH_REQUIRED` | no |
| 403 | `DEVICE_AUTH_MISMATCH`, `DEVICE_REVOKED`, `DEVICE_OWNER_MISMATCH`, `OWNER_DISABLED` | no |
| 409 | `DEVICE_BINDING_CONFLICT`, `IDEMPOTENCY_CONFLICT`, `KEY_GENERATION_STALE`, `KEY_GENERATION_EXHAUSTED` | no |
| 413 | `REQUEST_TOO_LARGE` | no |
| 431 | `REQUEST_HEADER_TOO_LARGE` | no |
| 503 | `AUTHORITY_UNAVAILABLE` | yes |
| 503 | `ISSUER_INVALID`, `UNSAFE_AUTHORITY_STATE`, `AUTHORITY_ISSUER_MISMATCH`, `AUTHORITY_CONFIG_INVALID` | no |

An unknown code, an unknown envelope field, a `data` value on an error, a
version or `operationId` mismatch, or a status/retryable contradiction is
`TRANSPORT_RESPONSE_INVALID`.

After the first successful request send, one containment boundary covers read
setup, receive/buffering, framing, decoding, parsing and stream close. Existing
typed failures retain their code. Any other exception except `KeyboardInterrupt`
and `SystemExit` becomes `TRANSPORT_RESPONSE_INVALID` with outcome `UNKNOWN`,
`reason_code=POST_SEND_EXCEPTION`, and a sanitized ASCII `exception_class`;
the exception message, response bytes, headers and paths are never copied into
the error or a log. Failures before a successful send keep their existing
pre-send semantics.

## Typed failure and outcome surface

`TransportClientError` carries `code`, `retryable`, `outcome`, `http_status`,
`operation_id`, and optional fixed `reason_code` / sanitized `exception_class`
diagnostics. It never contains a token, key, PEM, CSR, path, SQL, upstream body
or raw exception text; its message is the code. Outcomes are:

- `NOT_SENT`: no request left the machine (local validation, configuration,
  connect, or certificate-verification failure). A retry is safe.
- `REJECTED`: the authority answered with a definite nonretryable rejection.
- `UNKNOWN`: the request was sent and no definitive answer was accepted, so the
  operation may have committed. Only an exact replay settles it.

Client entrypoints, configuration/material validation and material loading
detach both public exception-chain links (`__context__` and `__cause__`) on
typed failures. A suppressed display (`raise ... from None`) alone does not
satisfy this boundary. The same typed error and its code, outcome, retryable,
HTTP status, operation ID and sanitized diagnostics are preserved, including
NOT_SENT restoration of an earlier UNKNOWN or REJECTED replay state. Callable
signatures/metadata remain intact; KeyboardInterrupt and SystemExit propagate.
This guarantee concerns public exception links and diagnostics; it does not
promise secrecy of every local variable reachable through traceback frames.

Client-only codes: `TRANSPORT_REQUEST_INVALID`, `TRANSPORT_REQUEST_TOO_LARGE`,
`TRANSPORT_CLIENT_CONFIG_INVALID`, `TRANSPORT_CREDENTIAL_ABSENT`,
`TRANSPORT_REDIRECT_REFUSED`, `TRANSPORT_RESPONSE_INVALID`,
`TRANSPORT_RESPONSE_UNKNOWN`, `TRANSPORT_REPLAY_MISMATCH`,
`TRANSPORT_OUTCOME_UNRESOLVED`, plus the shared `TRANSPORT_STORAGE_UNSAFE` and
`TRANSPORT_CHAIN_UNTRUSTED`. A lost, short, empty or oversize response after the
request was sent is `TRANSPORT_RESPONSE_UNKNOWN` with outcome `UNKNOWN` and
`retryable` true.

## Replay

Within one client instance, an `UNKNOWN` operation may be retried only with the
same route and the same exact body bytes, and therefore the same `operationId`.
A different body or route for that ID is `TRANSPORT_REPLAY_MISMATCH` before any
connection. A new operation ID for the same route is `TRANSPORT_OUTCOME_UNRESOLVED`
while an earlier operation is unresolved, so a new mutation cannot skip it. A
resolved operation may be replayed unchanged; the authority's idempotency
answer is returned. Both a validated success and a canonical nonretryable
authority error (`REJECTED`) resolve the attempt, so a new operation ID for that
route is allowed afterward. A retryable authority error, lost or invalid
response, or redirect remains `UNKNOWN` and blocks a new ID for that route.
An exact replay can resolve an earlier `UNKNOWN` through either success or a
canonical `REJECTED` answer. Route/body identity is still checked for every
previously used ID, including resolved IDs. Durable unknown-state bookkeeping is the caller's
responsibility; this client keeps no file.

The optional strict tailnet composition contract is
[tailnet-transport-workflow.md](tailnet-transport-workflow.md). It retains this
client's original instance/config and exact v1 requests across UNKNOWN replay;
it introduces no fields into the HTTP envelopes or certificate profile.
