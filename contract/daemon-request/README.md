# Daemon request contract projections (A-03 skeleton)

`openapi.json` is generated from the daemon's pure `MethodSpec` declarations.
It is a **contract projection**, not a running HTTP gateway: A-03 starts no
server and the document contains no clock, home path, socket or live identity.

Regenerate and verify:

```sh
uv run python scripts/verify/dump_daemon_openapi.py --write
uv run python scripts/verify/dump_daemon_openapi.py --check
```

Native Auth import provenance uses the existing `agent.provider.login/logout`
and `agent.provider.auth.list` methods. `importedAt` is a Unix millisecond value
from the registry clock, recorded after the complete native file set is installed.
The record stays in the existing Agent registry, outside the harness projection,
and binds the Agent entity token, home resource token, harness and file-set digest.
Inventory internally hashes at most 1 MiB per file when a record exists; it never
returns credential contents, paths or digests. Files larger than 1 MiB retain the
existing import behavior without provenance; a successful replacement clears any
old record and returns a null import time. Existing transport limits still apply.
Legacy files without a record, changed files, incomplete sets and crash residue
return a null import time.
Identical explicit reimports retain an already matching record; a replacement gets
a new import time. Explicitly importing legacy files creates a record of this new
import, even when the bytes match; merely reading or reusing them does not.
Agent home revocation clears the old incarnation's records in the same transaction;
authority rotation never carries provenance to a new incarnation.
Removal invalidates the record before touching files. An import
record failure is not successful login, even if the new files are already present.
`startupConfirmedAt` is the latest successful carrier response time for the current
import generation and unchanged file set. The carrier must select this Agent's
native authentication, and its admitted turn must complete with a nonempty reply.
Claude SDK native selection, Codex app-server file auth with an authenticated account,
and Pi RPC's explicit native DeepSeek selection supply this evidence. Other Pi
sources remain unknown. Key/environment grants, shared external
auth paths, readiness and unrelated replies never confirm an import. A true no-op
import retains the generation; replacement, removal, incomplete files and changed
Agent/home authority invalidate matching evidence, including delayed old turns.
The record survives daemon restart and exposes no digest, import ID, path or content.
Null means no matching evidence, not failed authentication or an Agent that never ran.
Both timestamp fields are positive integer Unix milliseconds or null. Import,
confirmation and rollback writers reject zero, negative, boolean and non-integer
timestamps before changing the record. Inventory exposes an invalid stored
confirmation as null; an invalid stored import time also prevents capturing or
publishing a confirmation for that record.

`x-hyprial-methods` is the complete declared catalog (including methods that
are not HTTP-exposed). `paths` contains only `http_exposed=true` methods, one
`POST /daemon/request/<method>` projection each. `daemon.catalog` is the read-only
authority the HTTP entry consumes at runtime; it is catalogued with
`httpExposed=false` and has no path.

`httpBinding` (catalog entry and `x-hyprial-http-binding` path extension) is a
**transport binding**, not an authorization claim. An explicit binding names the
HTTP identity mode/selector parameter and the readiness probe key. A declaration
with `http_exposed=true` but no explicit binding is still eligible for the
projection and receives the default no-selector binding (`identity.mode=none`);
that never infers a caller from a business parameter. Runtime routing additionally
requires the audience/unary/safe profile in the HTTP entry, and the actual
session/domain authorization stays in the daemon. Error responses are a typed
`oneOf` over the method's declared domain codes and their `data` schemas.
Error declarations accept uppercase underscore codes and the shipped OrgFS
lower-kebab convention. Migration preserves their wire spelling; mixed-case,
whitespace and other separators remain invalid. `org.join` and
`org.invites.accept` share the existing join error vocabulary, including the
typed legacy OrgFS details map. No undeclared error is accepted.

### Missing Agent runtime executable

`agent.start` returns `HARNESS_BINARY_MISSING` when a fresh Claude, Codex or Pi
launch cannot resolve its executable on the daemon's `PATH`. This replaces
`INVALID_ARGUMENT` for that specific refusal; malformed parameters keep their
existing codes. The check runs before Agent record or durable launch intent
mutation and does not automatically install programs, change `PATH` or retry.
It also applies to an existing Agent without a saved executable command.
Existing pinned launch commands keep their current resolution and failure path.

The error has no data payload. HTTP keeps its redacted generic message, with
the declared code and `dispatch=dispatched`; GUI maps the code to an installation
and daemon `PATH` hint without displaying raw backend error text or local paths.
A missing HTTP reply remains an unknown outcome and is never automatically retried.

### Confirmed Agent destruction

`agent.destroy.preview({name})` and `agent.destroy({operationId})` are a paired
confirmation surface. Execute accepts only the operation ID; the former
name-based execute payload is rejected on both IPC and HTTP. The CLI keeps
`hyprial agent destroy NAME`, but always obtains a preview first, including with
`--yes` (which only omits the interactive prompt).

A preview expires after 300 seconds and belongs to one daemon epoch, caller,
transport session, and Agent entity token. The admitted GUI view session narrows
this binding; a body-supplied identity is not authority. Known worker callers
are refused and the worker relay allowlist is unchanged. Ordinary private IPC
continues to rely on socket OS access: it does not prove a human issued the call.

Execute reuses the existing destroy reservation, verifies the entity token and
observed home token, adapter bindings and routine registration IDs before
effects, and claims the preview once. A completed result (including a terminal
failure after claim) is retained for readback/replay within that registry. Unknown outcomes are not
retry instructions; clients inspect the Agent before explicitly requesting a
new preview. Restart invalidates outstanding operation IDs.

The preview describes stopping the Agent, removing its home and bindings,
removing bound routines, and discarding pending messages. Workspace counts and
pending-message counts are observations, not frozen snapshots. This does not
purge consumed message history or create a composite filesystem version.
Missing-Agent recovery remains an internal cleanup path, not name-based public
execute. GUI cancellation sends no execute request.

### HTTP-only parameter constraints

`HttpBinding.params_constraints` optionally exports `httpBinding.paramsConstraints`.
It narrows only the HTTP request body; `paramsSchema` remains the IPC authority.
Use `{type: "object", properties: {...}, required: [...]}` to require a stable
HTTP business key while retaining optional legacy IPC parameters and handler
errors. Constraints may only name declared, non-host-only properties. Property
rules intersect the original schema using `allOf`; they cannot relax its types
or additional-properties policy. Constraints must be inline (no references).
Declarations must add a new required field or a validation assertion: empty,
annotation-only, and obviously repeated/weaker rules are rejected. Formats known
to the default `jsonschema.FormatChecker()` are executable assertions, matching
the HTTP listener and Contract Mock; unknown formats remain annotations. Exported
HTTP examples use that same format checker. This guard is not a general JSON Schema
implication solver; arbitrary equivalent combinators are not proved distinct.
The safety guarantee is the `allOf` intersection: an HTTP constraint cannot
broaden the original allowed inputs. Method owners must still demonstrate the
intended restriction with accepted and rejected request examples.
Host-only fields are still removed before browser validation and injected only
by the authenticated HTTP host according to the identity binding.

`project_http_params_schema` in the kernel public API is shared by OpenAPI export
and request-http catalog assembly. Invalid constraints fail assembly; rejected
requests are `INVALID_ARGUMENT` / `not-dispatched`. Constraints affect declaration
and HTTP digests, so changing them requires the usual listener restart/catalog
pin handling. There is no method-name special case or retry policy override.
Non-invalid-params examples must satisfy the narrowed HTTP schema; Contract Mock
and generated clients consume this same projection. Legacy IPC-only success
examples that omit an HTTP-required field must not be published as HTTP success.

`message.send` is the current opt-in: its HTTP body requires one canonical agent
target, a nonblank `message`/`summary`, a 22-128 character URL-safe
`idempotencyKey`, and a nonblank `conversationId` only when the caller names one
(optional since 2026-11-18, see below). Its invalid-params example proves the HTTP-only
rejection while handler tests pin the unchanged raw IPC compatibility and
idempotency behavior. The registered HTTP write uses `retry=never`: a lost
response remains `dispatch=unknown` and is recovered by `message.send.key_lookup`,
never by an automatic resend. Generated clients and Contract Mock consume the
same projected schema.

`message.send` conversation attribution (2026-11-17): a `conversationId` the
caller passes is used verbatim for every target. When the caller passes none
(over IPC, and over HTTP since 2026-11-18), each target's message
belongs to the default conversation of the stored sender and that target. Its
id is derived from one key per end, is the same in both directions, and has
the form `dc.` plus 26 lowercase base32 characters. An id-form agent address
(`agent:<owner>:<machine>:a.<id>`) is keyed by its `a.<id>` alone, so the
conversation does not move when the agent's owner or machine segment changes;
every other end (name-form agent, `user:`, `adapter:`) is keyed by
its canonical address. A `user:` end stays keyed by `user:<owner>` (UA2,
2026-11-18): a target or a reply recipient that is a user agent address (any
machine's name form `agent:<owner>:<machine>:hyprial-user`, or this machine's
user agent in any spelling) is rewritten to `user:<owner>` before the id is
derived (a user agent address as the sender is refused, see
`wire/message-sender-auth.md`), so every machine of the owner derives the same
conversation. Each machine's user agent is an independent agent with its own
`a.<id>` (UA4, 2026-11-19): another machine's id-form address is an ordinary
agent address. The derived id replaces the
random id an omitted `conversationId` used to produce, so repeated sends
between the same two addresses share one conversation. Each `deliveries` entry
carries the `conversationId` its message was stored under. The top-level
`conversationId` is the explicit id when one was passed and otherwise the
first target's, so for a single-target send the two agree. The delivery field
is optional in the schema: a result relayed from a daemon older than this
change omits it. The derivation is
`endpoint_key` and `default_conversation_id` in
`hyprial.daemon.impl.conversations.ids`; clients must
read the id from the response and must not re-derive it.

Over HTTP the stored sender is the server-injected `user:<owner>` (the
`local-owner-user-selector` binding); a browser-supplied `from` or a different
`actor` is refused before dispatch, so the browser cannot choose which default
conversation a send joins. An HTTP `conversationId` that is present stays
nonblank-string-only and is used verbatim, as before. `message.send.lookup`
still takes the original `conversationId` as a selector, so a caller that
omitted it and lost the response reconciles with `message.send.key_lookup`
(the `idempotencyKey` alone), whose accepted result returns the stored
`conversationId`; a caller that has the send response uses the returned id.

`message.send` to `user:<owner>` (UA2, 2026-11-18): when the target is the
daemon's own owner and the owner's user agent lives on this node, the message
is an ordinary queued message: it is submitted like an agent message (same
message id, same conversation id, `user:<owner>` kept as the stored
recipient), delivered to this node's inbox, recorded in its conversation, and
acknowledged by the daemon once the record was read back intact. Its delivery
entry answers the queue in `accepted`/`queued` and adds `pushed`: whether a
copy went to the owner's IM (R2, 2026-11-19; design
`user-im-binding-removal-tier2` 8.1 to 8.3). The copy is decided on this node
from the sender's own binding, never from an IM account of the person: a
sender whose cell declares no adapter member (an unbound agent, the
operator) is pushed by the daemon to the targets of this node's user agent
cell (I7a, 2026-11-24): its `user` connections in document order, else
every adapter member at its gateway's `defaultRoute`. The entry carries
`via: "user-agent"` and folds the targets exactly as for a bound sender
below; an empty user agent cell answers `pushed: false`,
`pushCode: TARGET_UNRESOLVED` at once, with no wait, and a user agent cell
that cannot be read `USER_DELIVERY_UNAVAILABLE`. A message whose sender is
an adapter address (`adapter:`, with or without a chat segment) is never
pushed (`pushed: false`,
`TARGET_UNRESOLVED`, logged `pushSkipped: sender-is-adapter`). A
sender bound to an adapter (its cell declares an adapter-role member, with
or without an `adapter` datum) is pushed by the daemon to the chats its own
cell names (I3, 2026-11-23): the targets of the emitting member under the
reserved `user` label, else every adapter member at its gateway's
`defaultRoute` when no connection in the cell carries `user`. Its entry
carries `pushed: true`, `via: "cell"` when at least one chat was posted,
plus `pushCode` when another was not; otherwise `pushed: false` with the
first target's code (`TARGET_UNRESOLVED` for a target the configuration
cannot resolve or no target at all, `USER_DELIVERY_UNAVAILABLE` when
nothing could be sent, also when the sender's cell cannot be read,
`USER_DELIVERY_FAILED` for a provider refusal, `SUBMIT_OUTCOME_UNKNOWN`
when a post may have arrived). That copy is best-effort: one inline attempt,
it never fails the queued send and nothing retries it; a bound sender is
never rescued by the user agent cell, even when its own cell names no chat.
There is no squire DM, no agent-bot DM by open id, no user-proxy hand-off,
no receivers read and no receipt wait on this path, and the entry no longer
carries `receiverAdapter` or `adapter`. A caller's own same-key replay
finds the queued message (no second row or record entry) and pushes again
under the same per-target keys; the platform deduplicates inside its own
window. A message carrying a
dispatch `role` is refused with `DISPATCH_ROLE_MISMATCH`: the user agent never
takes work. Another owner's `user:` target keeps the push-only path over
Zenoh to the machine that owns that person (its receiver posts it through
its own user agent cell; no target there is `TARGET_UNRESOLVED`, a post whose
outcome is unknown, or an unexpected error there, is `USER_DELIVERY_FAILED`
and is not attempted again) and
its result shape (`receiverAdapter`, `nativeMessageId`, `duplicate`). This
owner without a queue here (no local user agent or recorder) decides the copy
as above with nothing queued: with no user agent here an unbound sender is
refused with `TARGET_UNRESOLVED`, a push with at least one chat
posted answers `via: "cell"` or `via: "user-agent"` (plus `pushCode` when another chat was not),
a cell push whose only outcome may have arrived fails with
`SUBMIT_OUTCOME_UNKNOWN`, and no copy is refused with the first target's
code (`TARGET_UNRESOLVED` when there is none).
`message.reply` to `user:<owner>` is delivered to this node, recorded and
acknowledged, but never pushed to the owner (UA2b decision: before UA2 a
reply to the owner never reached Lark either); `message.send` with `replyTo`
to the owner is a send and is pushed like one. Such a reply answers
`queued: false` (before UA2 it waited in the outbox and answered `true`), and
`message.pending.list` for `user:<owner>` lists it until the daemon has
acknowledged it.
`message.status` for such a message reports the queue only (`fetched`,
reason `ACK_RECEIVED`, once this node's inbox holds it), never the push.
`message.send.key_lookup` resolves a send to `user:<owner>` like an agent
send. `targets` lists `user:<owner>` with status `user-agent` while this node
holds the owner's user agent.

The string for the user's end in conversation records is byte-identical to
the session principal actor the HTTP layer injects (`user:<owner>`): records,
`conversations.list` peers and history filters never carry a machine's user
agent address.

### Legacy IPC parameter admission

`MethodSpec.ipc_param_validation` defaults to `schema`. The following legacy-backed
methods set it to `handler`: `message.send`, `message.send.key_lookup`, `targets`,
`progress.list`, `identity.users.add`,
`agent.secret.provider-write` and `agent.secret.grant`.

`handler` skips registry-side raw IPC schema validation so the bound handler keeps
its established unknown-field admission, coercion and error order; it never
weakens the HTTP body, which still uses the strict projected schema, nor the
registry's declared-error and result checks. The catalog exports
`ipcParamValidation` only for non-default methods, and OpenAPI carries
`x-hyprial-ipc-param-validation`. Do not opt in unless that handler is the
authoritative validator for the method.

## Historical lowercase error codes

MethodSpec and the H-03 HTTP projector accept the uppercase error-code grammar
or an exact member of the frozen list below. These are compatibility entries
(historical lowercase code, kept for compatibility; 历史小写码，保留兼容): the
lowercase spelling stays on the wire, and a new lowercase spelling cannot
enter by matching a pattern. `src/hyprial/kernel/impl/contracts/daemon/legacy_error_codes/`
is the single source; both `http-envelope.schema.json` and
`contract/http-request/http-contract.v1.json` carry the same enum, and tests
keep the three copies equal. Adding a member requires an explicit snapshot and
contract change with a reviewed reason.

| Historical wire code | Uppercase name | Notes |
| --- | --- | --- |
| `already-exists` | `ALREADY_EXISTS` | historical OrgFS document register code, kept compatible |
| `ambiguous-path` | `AMBIGUOUS_PATH` | historical lowercase code, kept compatible |
| `ambiguous-space` | `AMBIGUOUS_SPACE` | historical lowercase code, kept compatible |
| `bad-cursor` | `BAD_CURSOR` | historical lowercase code, kept compatible; StoreError re-raised through OrgFS |
| `blob-unavailable` | `BLOB_UNAVAILABLE` | historical lowercase code, kept compatible |
| `checkout-io` | `CHECKOUT_IO` | historical lowercase code, kept compatible |
| `closed` | `CLOSED` | historical lowercase code, kept compatible |
| `content-pending` | `CONTENT_PENDING` | historical lowercase code, kept compatible; existing `ipc_errors.ORGFS_CONTENT_PENDING` constant |
| `cross-space-uri` | `CROSS_SPACE_URI` | historical lowercase code, kept compatible; existing `ipc_errors.ORGFS_CROSS_SPACE_URI` constant |
| `invalid-argument` | `INVALID_ARGUMENT` | historical lowercase code, kept compatible |
| `invalid-uri` | `INVALID_URI` | historical lowercase code, kept compatible; existing `ipc_errors.ORGFS_INVALID_URI` constant |
| `log-key-conflict` | `LOG_KEY_CONFLICT` | historical lowercase code, kept compatible |
| `no-holder-online` | `NO_HOLDER_ONLINE` | historical lowercase code, kept compatible |
| `not-a-member` | `NOT_A_MEMBER` | historical lowercase code, kept compatible |
| `not-owner` | `NOT_OWNER` | historical lowercase code, kept compatible |
| `out-of-range` | `OUT_OF_RANGE` | historical lowercase code, kept compatible |
| `pending-meta` | `PENDING_META` | historical lowercase code, kept compatible; StoreError re-raised through OrgFS |
| `projection-behind` | `PROJECTION_BEHIND` | historical lowercase code, kept compatible; StoreError re-raised through OrgFS |
| `protected-path` | `PROTECTED_PATH` | historical lowercase code, kept compatible |
| `purged` | `PURGED` | historical lowercase code, kept compatible |
| `resource-exhausted` | `RESOURCE_EXHAUSTED` | historical lowercase code, kept compatible |
| `snapshot-barrier` | `SNAPSHOT_BARRIER` | historical lowercase code, kept compatible |
| `snapshot-unavailable` | `SNAPSHOT_UNAVAILABLE` | historical lowercase code, kept compatible; StoreError re-raised through OrgFS |
| `space-purge-incomplete` | `SPACE_PURGE_INCOMPLETE` | historical lowercase code, kept compatible |
| `space-purge-refused` | `SPACE_PURGE_REFUSED` | historical lowercase code, kept compatible |
| `stale-plan` | `STALE_PLAN` | historical lowercase code, kept compatible |
| `stale-write` | `STALE_WRITE` | historical lowercase code, kept compatible |
| `sync-peer-incompatible` | `SYNC_PEER_INCOMPATIBLE` | historical lowercase code, kept compatible; StoreError re-raised through OrgFS |
| `too-large` | `TOO_LARGE` | historical lowercase code, kept compatible |
| `unavailable` | `UNAVAILABLE` | historical lowercase code, kept compatible |
| `unknown-blob` | `UNKNOWN_BLOB` | historical lowercase code, kept compatible |
| `unknown-doc` | `UNKNOWN_DOC` | historical lowercase code, kept compatible |
| `unknown-space` | `UNKNOWN_SPACE` | historical lowercase code, kept compatible |
| `unsafe-space-path` | `UNSAFE_SPACE_PATH` | historical lowercase code, kept compatible |

The snapshot is pinned by
`tests/tooling/contracts/test_daemon_openapi_contract.py`; that test also
fails on empty/boolean schemas, duplicate or unbound handlers, an unregistered
method, and an execute method that accepts replacement params.

Preview capacity must not erase execution custody. An unclaimed preview may
expire or be evicted; an executing claim survives expiry, capacity pressure and
discard attempts until the domain records its outcome. A completed record is
retained through its original expiry while capacity permits only unclaimed
evictions. If all slots hold executions, issuing another preview returns
`RATE_LIMITED` before creating a record. Preview MethodSpecs using this registry
must declare that refusal. A repeated running claim returns
`OPERATION_IN_PROGRESS`; no automatic write replay follows expiry. This remains
an epoch-local protocol: durable business recovery belongs to the domain.

## Org membership receiver and binding semantics

Personal organization receiver storage was removed (OC4 / J21) and S3a deleted
`org.receivers.get`, `org.receivers.set` and `receivers.json`. Local credentials and owner
records are stored locally under `<HYPRIAL_HOME>/state/`, not in an OrgFS
personal space.

## Per-method migration discipline

Methods leave the legacy string router one name at a time, never one family at
a time:

- `MIGRATED_METHODS` in `daemon/impl/request_catalog/migration.py` is the
  precise record of the method names that the catalog owns.  `freeze()`-time
  assembly refuses a declaration the record does not own, so a new method
  cannot be served by the registry while the router still classifies it as
  legacy.
- The router consults that record *after* the catalog: a migrated name with no
  binding is refused with `METHOD_NOT_FOUND` and never reaches a compatibility
  branch.
- `MIGRATED_FAMILIES` (`daemon`, `link`, `work`) holds only a family whose every member has migrated (a
  family-level name is refused too, so a removed handler cannot resurrect
  through the legacy chain).  A family with legacy-only members must stay out
  of it; the precise set is what protects the partially migrated family.
- Adding an entry to `MIGRATED_METHODS` is a declaration-and-binding change:
  it moves with the `MethodSpec`, its handler and the regenerated snapshot,
  never on its own.
## Same-source examples and Contract Mock (M-03)

`openapi.json` also carries the validated development examples when a method has
an example set:

- `x-hyprial-examples` on the operation: labelled success, empty/unconfigured,
  a true schema-invalid `invalid-params` case, and schema-valid
  `semantic-param-error`/`domain-error` cases.  The kind declares the validation
  boundary; the error code does not.  Legacy handlers keep authority over
  durations, coercions, requiredness and combined inputs, so a schema-valid
  value may still be an `INVALID_ARGUMENT` after dispatch.  An `invalid-params`
  case may carry any raw JSON value, including a non-object; for an
  object-universal schema such as `agent.provider.default/login`, that is the
  only true structural negative and the declaration is not tightened to create
  an object negative;
- `x-hyprial-example-gaps`: explicit reasons when a method has no legal
  empty/unconfigured state or no non-parameter domain error;
- JSON Schema `examples` inside the projected request/result schemas.

Error `data` is optional, including when its error declares a `dataSchema`.
An omitted value stays omitted through examples, OpenAPI, HTTP and Contract Mock;
no layer supplies `{}`. Without a data schema any present data is rejected. With
a data schema a present object (including `{}`) must validate against it.
`MethodExampleError.data=None` means omission; that Python type cannot represent
explicit JSON `null`. Serialized contract examples reject an explicit `data:null`,
as the OpenAPI error schema does. Error responses require only `code` and `message`.

The cases are declared in
`src/hyprial/daemon/impl/request_catalog/examples.py` and validated against the
owning `MethodSpec` schemas by `export_openapi()`. The current combination
candidate contains the five Agent methods and 23 cases; the gate passes:

```sh
uv run python scripts/verify/check_agent_contract_examples.py
```

Run the Contract Mock (loopback only, no daemon, no real credentials). It is
JavaScript (`gui/contract-mock`), needs Node 24 and no build step:

```sh
node gui/contract-mock/serve.ts --scenario success --host 127.0.0.1 --port 8770
```

`--scenario` is required: `success`, `empty`, `unconfigured` or `error`.
The `error` scenario serves either a `domain-error` or a schema-valid
`semantic-param-error`; use `--case METHOD=CASE` to choose among them.
Per-method overrides use `--method-scenario METHOD=SCENARIO` and
`--case METHOD=CASE`; `--preset NAME` picks a named preset from
`gui/contract-mock/presets.json`, and `--check` prints one line per method and
exits. The startup line prints the synthetic bearer token and the contract
digest. `GET /healthz` is unauthenticated; `GET /mock/contract`
reports the selected scenario, gaps, skipped un-mockable methods and each method's
declared `mutates` (`x-hyprial-mutates`, the value the GUI host turns into CSRF
requirements). A method
with no selected scenario returns 409 `MOCK_SCENARIO_MISSING` rather than a
fabricated result. A method whose selected scenario has cases but none whose
declared selector fields describe the request returns 409
`MOCK_EXAMPLE_MISSING` instead of answering with a different example's
`conversationId`, `view`, `spaceId` or other published object.

A pull request that changes `contract/daemon-request/openapi.json` (a new,
changed or removed method, a `x-hyprial-mutates` or example change) MUST
regenerate `gui/src/dev/contract-mock/contract.generated.json` in the same PR with
`node gui/scripts/generate-reference-client.mjs`. That file is generated and is
never edited by hand; `npm --prefix gui run client:reference:check` fails when it
is stale. A new method needs no other GUI change: the Contract Mock, its
in-browser form and the same-origin host derive the method list, `mutates` and
examples from that table.

The same module also runs inside the browser for front-end development
(`connectContractMock` in `gui/src/dev/contract-mock`). The Python package
`contract/daemon-request/contract_mock` no longer serves anything: it loads and
validates the examples (`load_contract`, `check_contract`) for the exporter and
the example gates.

`http-envelope.schema.json` pins the transport envelope/status map read from
H-01 candidate `02b9001fae32bd998a75654c0ea259d94276c7c5`. It is a temporary
M-side seam for frontend development, **not same-source with H-03**. The final
mock must consume the accepted H-03 projection and HM-01 must verify parity;
until then the mock does not prove real HTTP, browser, permission or
credential-write behavior.

Breaking changes are reviewed with:

```sh
uv run python scripts/ci/check_daemon_contract_diff.py \
  --base <old-openapi.json> --head contract/daemon-request/openapi.json \
  [--pr-body-file pr-body.txt]
```

A `BREAKING` finding stays red unless the PR body declares
`Contract-Impact: <reviewed reason>`. See
`docs/guides/daemon-request-contract-mock.md` for startup, scenario, field-map
and sensitivity details.

The catalog also declares the read-only `message.status` projection. It is
catalogued for IPC validation and is not present in `paths` because its
trusted caller and delivery-status mesh context are not HTTP-safe.

## Phase-gated methods

A method the daemon exposes over HTTP that the current phase cannot call is
`not-ready` in the GUI entry's catalog, never `disabled`: reason
`ACCOUNT_REQUIRED` while the home has no account, `BOOTSTRAP_ONLY` for
`onboarding.device.ensure` and `onboarding.initialize` once serving. The method
set and both digests (`catalogDigest`, `declarationDigest`) therefore do not
depend on the phase. Over HTTP such a call is 503 `HTTP_NOT_READY` with
`data.reason` set to that reason, `dispatch: not-dispatched`; a daemon that
answers `ACCOUNT_REQUIRED` to a forwarded call (the entry's view was one tick
behind) is mapped to the same refusal. `GET /readyz` ignores routes that are
not-ready only for the phase. No error code or status-table row is added.

## First login driven by the daemon

`auth.login.begin` takes an optional `until`: `authenticated` (the default, and
what an absent parameter means) leaves the clock of the flow with the client,
which polls `auth.operation.get`; `serving` makes the ownerless daemon drive the
login itself. It polls the issuer until the login is authorised, registers the
device, ensures the user key, accepts initialize and promotes itself to serving in
the same process, so a client needs no polling for progress. The result and the
login operation then carry `completion` (`stage`: `authorizing`, `device`,
`starting`, `serving`, `stopped`; plus `identity`, `device` and `failure`); it
appears only on logins started with `until: serving` and is visible to every
reader of that operation, for example a GUI polling a login the CLI started. It
is the daemon's in-memory projection, a snapshot taken at the end of the login:
the three receipts stay the durable truth and `onboarding.status` is the source of
truth for the device afterwards. It never carries a device code, a token, key
material, exception text (an error is a code with a fixed message), a pid or an
epoch; read `ping` after `stage: serving` for the latter two.

`auth.operation.get` takes an optional `waitSeconds` (0 to 900). For a login the
daemon drives, the call is parked in the daemon without holding its dispatch lock
(`ping` and `auth.operation.cancel` are answered meanwhile) and answers when the
stage is `serving` or `stopped`, when the daemon is ending (`stopped`), or when the
seconds run out, with the current record. The wait spans the listener swap: a call
parked while the daemon starts serving is answered by the serving daemon. For any
other operation, and on a serving daemon, the parameter is ignored and the call
answers at once. The parameter exists on the daemon's own wire only: it is
`x-hyprial-http-forbidden`, so the HTTP projection does not accept it.

`until: serving` is refused with `IDENTITY_CONFLICT` by a daemon that already has an
account (including one whose owner comes from the environment), with
`OPERATION_IN_PROGRESS` while a login the client polls is in flight, and a second
`until: serving` begin while a daemon-driven login is in flight returns that same
operation, whatever its `operationKey`.

A device that cannot be registered during a first login does not fail it: the login
succeeds, `completion.device.ready` is `false` and `completion.device.retry` is
`serving-start`: the only retry is the existing one, a single recovery attempt at
each daemon serving start, including the start this login leads to (that pass now
also runs on a start that promoted a bootstrap), with no loop and no other retry;
the initialize receipt stays `promoting` until the device is registered. The
explicit `onboarding.initialize` keeps refusing with `DEVICE_ENSURE_FAILED`.
