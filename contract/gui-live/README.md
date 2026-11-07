# Local GUI host contract

This opt-in local host composes the existing AppChatAdapter and daemon APIs.
It is not the separately coordinated universal GUI HTTP gateway.

## Launch and trust

`HYPRIAL_GUI_BACKEND_DRIVER=daemon`, an absolute `HYPRIAL_GUI_PYTHON`, explicit
`HYPRIAL_HOME`, and `HYPRIAL_GUI_PRINCIPAL=user:<node-owner>` are required.
The shell validates the principal against that home's trusted owner. Optional
`HARNESS_STATE_DIR` selects the actual daemon state. No browser field supplies
identity, home, socket, credentials or an arbitrary daemon method.

The Node server owns a Python child consuming the shell public
`run_gui_backend` facade. The Python composition reuses the existing same-domain
daemon request client. The bridge is loopback-only, checks Host/Origin and a
per-launch CSRF capability, caps body size at 128 KiB and concurrent calls at16,
and bounds response waits. It never retries a mutation on timeout.

GET `/api/gui/v1/live/bootstrap` returns `{enabled:false}` in fixture mode, or
`{enabled:true,csrfToken}` in daemon mode. POST `/api/gui/v1/live` requires
`X-Hyprial-Gui-Token` and `{operation,params}`. Responses are `{ok:true,result}`
or `{ok:false,error:{code,message,definitive,canRetry}}`. Unknown/malformed or
transport errors must never be interpreted as successful writes or definite
non-execution. Production chooses mode from runtime bootstrap, even if build
and launch environments differ.

## Chat

- `targets.list {}` projects daemon targets and adds `preferredTargetUri`, the
  trusted local owner's Squire URI when it appears in the bounded directory,
  otherwise `null`. The browser never derives this address from a display name
  or chooses the directory's first row. `sessions.create {targetUri}`
  returns the stable session and daemon `conversationId` for the trusted sender.
- `sessions.history {sessionId,args:{limit,cursor?}}` reads daemon history.
  GUI requests at most20 rows/page, advances continuation in bounded batches,
  merges by authoritative event ID and supports a full rescan for late backdated rows.
- `sessions.send`, `sessions.send.lookup`, `sessions.send.confirm` all take
  `{sessionId,request:{text,idempotencyKey}}`. Text and key are frozen.
  A complete acceptance is `{status:'accepted',commandId,conversationId,messageId}`.
  All identifiers must match; missing messageId is unknown, not permission to
  discard the original request. Lookup uses `message.send.lookup` and never sends.
- `sessions.pending {}` returns `{requests:[{sessionId,targetUri,request,status:'unknown'}]}`.
  The host saves UI intent before invoking send. It retains the request even after
  receiving a valid daemon acceptance, because the browser response may be lost.
- Only after validating acceptance does the browser call `sessions.send.confirm`.
  This operation rechecks daemon evidence and scrubs pending text. It is a local
  receipt acknowledgement, not a second send or a business inbox ACK. Its failure
  does not erase already observed acceptance; the UI blocks new sends until this
  read-only reconciliation finishes.

The owner-only SQLite intent journal is not a transcript, business session,
durable inbox or delivery scheduler. It never automatically resends. It retains
at most100 unresolved requests (one per session) and100,000 key tombstones;
capacity exhaustion refuses new writes. Resolved text is scrubbed. General daemon
send replay/conflict semantics remain the separate follow-up documented in
`contract/wire/message-send-lookup.md`.

## Lark

- `lark.adapters {}` returns configured adapters with independent `authorization`,
  `binding:{actor}`, `runtime:{online}`, `bindingAvailable`, and optional latest
  `operation` from the current host.
- `lark.authorization.status/start {adapter}` and
  `lark.authorization.operation/cancel {operationId}` consume the public daemon
  authorization service. See `contract/lark-authorization/README.md`.
- `lark.binding.set {adapter,agentId,expectedActor}` requires current grants and
  explicit conditional-write support. The host checks `adapter.pins.supportsExpectedActor`
  before sending `adapter.pin`; older daemons cannot silently ignore the condition.
  UI confirmation displays old/new bindings. Unknown writes are reconciled by
  reading bindings; no automatic repeated pin.

Connected means current required grants, selected binding, and an online adapter.
An operation's earlier success cannot override a newer grant observation or lock
out reauthorization after permissions/App configuration change. Authorization
attempts are host-memory only; restarting the host cancels owned workers and the
next view rereads grants without restarting authorization automatically.

Real tenant approval and native-platform process behavior require separate
acceptance. Loopback official-SDK tests and HTTP stand-ins do not establish them.
