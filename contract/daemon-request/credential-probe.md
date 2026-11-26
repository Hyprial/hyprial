# Temporary model credential probe

`agent.secret.provider-test` tests an explicitly supplied credential, or reads one
operator-selected saved connection at its exact revision. It never writes saved
credentials, grants, profiles, agents or sessions.
It belongs to the existing `agent.secret.*` request catalog. It does not use
the `user.provider.*` owner-key authority or change its frozen records.

The request is unary, HTTP exposed, local-operator only, `mutates=true` and
`retry=never`: the upstream request can consume paid model usage even though
it changes no local product state. IPC origin and the existing interactive-peer
check run before parameter validation; caller-supplied actor/session identity
is refused. HTTP uses the existing bearer and trusted host admission. No new
permission or identity source is introduced.

Input is a closed object. The original temporary-input shape remains supported.
The alternative saved-input shape is `{connection, model, timeoutSeconds?}`:
`connection` is the closed object `{entryId, fieldName, connectionRef, configRevision}`.
It cannot be mixed with temporary `provider`, `secret`, `endpoint` or `protocol`.
The existing local-operator admission runs before reading the existing Secret
record, and a single private atomic read resolves the endpoint, key and revision.
An absent/unreadable authority returns `MODEL_AUTH_UNAVAILABLE`; a different
reference/revision returns `MODEL_CONFIGURATION_CONFLICT`, before any network I/O.
Neither case falls back to a different key. No grant or Agent launch is created.
Conflict data is `phase=bind, reason=revision-mismatch` (HTTP 409). Missing or
unreadable authority data is `phase=authorize, reason=missing|unreadable` (HTTP
403); an unreadable gate is not reported as a generic daemon error. Operator
saved probes do not infer Agent grant revocation from a missing file.
Saved-connection model IDs are sent exactly as supplied; leading/trailing whitespace
is refused, rather than silently changing the selected ID. Legacy temporary inputs
retain their existing trimming behavior.

Temporary input fields:

- `provider`: nonempty provider identifier, at most 128 characters.
- `secret`: write-only, 1–16384 printable non-space ASCII characters, suitable
  for an HTTP authentication header. Never echoed in results or errors.
- `model`: nonblank model identifier, at most 256 characters, no Unicode category
  C characters (controls, format characters, surrogates, private-use or unassigned).
- Optional `endpoint` and `protocol`: an explicit endpoint requires the protocol
  `openai-completions` or `anthropic-messages`. An explicit protocol without an
  endpoint must match the provider default. Unknown providers require both.

Default bases: DeepSeek `https://api.deepseek.com`, OpenAI
`https://api.openai.com/v1`, Anthropic `https://api.anthropic.com`, Hyprial
`https://model.hyprial.ai/v1`. Anthropic uses Messages; the others use Chat
Completions. Other vendors require an explicit base and protocol in this slice.
No endpoint is taken from process environment, saved Keys or a harness profile.

Bases allow HTTPS, or HTTP to `127.0.0.1`, `[::1]`, or `localhost` (pinned to
127.0.0.1 before dispatch). They reject credentials, query, fragment, whitespace,
backslashes and invalid ports; maximum length is 2048. HTTPS destinations may
be private services explicitly selected by the trusted operator. The client
does not inherit proxy credentials, follow redirects or accept compressed bodies.

Chat Completions appends `/chat/completions`; Messages appends `/v1/messages`,
or `/messages` when the base already ends in `/v1`. Exactly one nonstreaming
request asks `Reply OK.` with a 64-token output limit. OpenAI's default uses
`max_completion_tokens`; compatible endpoints use `max_tokens`. Messages uses
`x-api-key` and API version `2023-06-01`. No tools, files, history or user prompt
are sent. There is no retry, alternate destination, fallback protocol or saved
credential fallback.

Optional `timeoutSeconds` is an integer 1–60 in either shape (booleans, null,
fractional and out-of-range values are refused). It controls the total async
I/O deadline; it is never sent upstream. When explicitly present and valid, only
this method receives `timeoutSeconds + 5` seconds at the request-http carrier,
capped at 65 seconds and reduced by elapsed request time. Other methods retain
the configured outer budget. Clients must allow this outer budget explicitly.
Omitting the field preserves the existing 10-second probe and configured outer
budget (15 seconds by default); the carrier never supplies a new default of 30.
JSON integral numbers such as `30.0` are accepted consistently with JSON Schema
`integer`; booleans and fractional numbers are not integers. New GUI callers
send 30 and use a 35-second outer budget. Cancel, closing the test dialog or
leaving the page aborts that request and ignores late results; no automatic retry
is made. Model IDs are manually entered, never selected from a guessed catalog.

The existing `agent.secret.provider-list({connectionVersion:1})` response may
add `presets: [{label, connection}]`. These are daemon-owned endpoint/protocol
templates (currently DeepSeek and OpenAI Chat Completions), not supported-model
lists or authentication evidence. An older response without `presets` remains
valid. Legacy `{}` calls keep their previous response. Custom addresses remain
explicit; an official label requires the full template to match, not just its ID.

Four simultaneous probes are permitted per daemon process; a fifth receives
`RATE_LIMITED` without queuing or sending. Without an explicit deadline, each probe has a 10-second cooperative
async I/O deadline covering connection and response reading, and a 64 KiB body
limit, leaving five seconds for the default 15-second HTTP envelope. Deployments
with a smaller outer budget can still return an HTTP transport timeout instead
of a probe result. Timeout cancels the request and closes the response/client. Blocking OS
resolver work can delay event-loop shutdown; this is not an OS-level process
execution deadline. No detached worker or persistent probe thread is created.
For this method only, HTTP disconnection cancels its local IPC call; IPC
disconnection or expiry signals cooperative cancellation to the probe task,
which closes its upstream HTTP stream/client and releases its concurrency slot.
Cancellation does not retry, roll back already consumed upstream usage, or
cancel other durable daemon operations. It is not a promise that an external
service stops work already accepted by that service.

The result is `{ok:true, outcome, httpStatus, latencyMs}`. `ok` says the probe
operation produced a result, **not** that authentication succeeded. Only
`outcome="passed"` records a protocol-shaped response with a nonblank model
identifier and assistant text. HTTP 200, HTML, an error object, an empty result,
or a response containing only reasoning/tool calls is insufficient.

Other outcomes are `authentication-rejected` (401/403), `rate-limited` (429),
`redirect-refused` (3xx), `request-rejected` (other 4xx), `upstream-error` (5xx),
`invalid-response`, `timeout`, or `unreachable`. `httpStatus` is null until an
HTTP response arrives. No response content, upstream error message, secret,
header, private path or credential fingerprint is returned. Framework errors
are fixed `INVALID_ARGUMENT`, `CALLER_NOT_AUTHORIZED`, `RATE_LIMITED` or
`DAEMON_ERROR`, without raw exception text.
Both temporary and saved timeouts additionally return `phase="probe"` and
`reason="deadline-exceeded"`. These optional fields occur together and only for
`outcome="timeout"`. The schema still accepts older results without these fields
for rolling compatibility; current handler tests require them on every timeout.

A passed probe does not save or authorize the Key, prove the selected harness
supports that service/model, or replace actual agent startup and reply evidence.
It does not promise every model accepts a 64-token minimal request. Test-model
selection is transient. Per-Key endpoint storage and Pi launch consumption are
separate C1 work; GUI test/save wiring is separate C3 work.

Protocol references: [Chat Completions](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create)
and [Messages](https://platform.claude.com/docs/en/api/messages). The isolated
loopback protocol tests do not establish real-vendor credential validity.
