# Daemon bootstrap/control authentication contract (version 1)

This contract defines the daemon-owned GUI control socket used to bind an HTTP
child process to one daemon instance and to issue the v2 IPC transport
capability that child then presents on the ordinary GUI IPC socket. The control
socket carries only session binding, child registration, revocation and close.
It is not a business method channel.

Status in this branch: the daemon-side consumer, serving-phase registration and
the v2 catalog gate are implemented. The real GUI host producer, the HTTP-side
registration caller, bootstrap-phase registration, same-PID promotion handoff
and non-Linux peer authentication remain OPEN integration work. This document
is an implementation agreement, not a product PASS.

## Transport

- Endpoint: `<state_dir>/gui/daemon/control.sock` (Unix stream socket).
  `state_dir` defaults to `<hyprial_home>/state`.
- The path must encode to at most 107 bytes.
- The state directory and the `gui`/`daemon` directories must be owned by the
  daemon user with mode exactly `0700` for `gui` and `daemon`; the state
  directory must not be group- or other-writable and must not be a symlink.
- The socket file must be a socket owned by the daemon user, mode `0600`, and
  is never followed through a symlink.
- Framing: one UTF-8 JSON object per line, terminated by `\n`. A frame is
  between 1 byte and 65536 bytes, excluding the newline. Empty frames, frames
  larger than 64 KiB, non-UTF-8 bytes, duplicate object keys, `NaN`,
  `Infinity`, `-Infinity`, non-object top-level values and excessive nesting
  are rejected with `CONTROL_BAD_FRAME` before any state change.
- Every frame has exactly `"version": 1` (the JSON integer token; `true`,
  `"1"` and `1.0` are rejected) and a `type` string.

Request frames use exact field sets. Any missing field, extra field, wrong
type, empty string, string longer than 256 characters, non-ASCII string, or
string with leading/trailing whitespace is rejected with `CONTROL_BAD_FRAME`.
Requests are rejected before proof, process or capability processing.

Response frames are produced only by the daemon and always carry `version` and
`type`. Clients must validate every documented required field and type. Extra
response fields are reserved and must be ignored; they never add authority.
Missing or mistyped required fields are a protocol failure.

Text fields below mean: non-empty ASCII string, at most 256 characters, with no
leading or trailing whitespace. `sha256:` and `hmac-sha256:` values are
lowercase hexadecimal digests with that exact prefix.

## Request frames

### `hello`

```json
{"version":1,"type":"hello","sessionId":"...","controller":"...","clientChallenge":"..."}
```

All four fields are required and no others are accepted. `controller` is the
controller identity the caller claims; the daemon does not trust it for peer
authentication. Identity comes from OS peer credentials: Linux `SO_PEERCRED`,
same UID as the daemon, positive PID, and a `/proc/<pid>/stat` birth identity
comparable with `process_birth_identity`. Non-Linux platforms, or a missing
peer/birth identity, make the control capability BLOCKED; they never fall back
to an unauthenticated or v1 path.

The hello response includes:

- `protocol` (`"hyprial-gui-ipc"`), `protocolVersion` (`2`),
  `ipcFrameVersion` (`2`), `phase` (`"serving"` or `"bootstrap"`),
  `authKinds` (array containing exactly one of `"local-gui"` or
  `"local-bootstrap"`), `daemonPid`, `daemonBirth`, `epoch`, `homeBinding`,
  `socketBinding`, `controlSocket`;
- `controllerChallenge` and `childTicket`, each 64 lowercase hex characters
  generated per connection/session with a CSPRNG;
- `clientChallenge`, echoed from the request;
- `declarationDigest` of the static method declaration projection;
- `controlProof`, the connection-bound proof defined below.

A duplicate live `sessionId` is rejected with `CONTROL_SESSION_EXISTS`. Frames
other than `hello` or `prove-child` before a successful hello are rejected with
`CONTROL_HANDSHAKE_REQUIRED`.

### `prove-child`

```json
{"version":1,"type":"prove-child","sessionId":"...","childTicket":"...","childChallenge":"..."}
```

The child process must open its own control connection and send this as its
first frame. It is rejected unless it arrives before any hello on that
connection and the ticket matches the session ticket in constant time. The
child peer PID must differ from both the daemon PID and the controller peer PID,
otherwise the frame fails with `CONTROL_PROCESS_MISMATCH`.

The `child-proven` response binds `sessionId`, `proofId` (32 hex chars),
`pid`, `birth`, `childChallenge` and `expiresInSeconds`. The proof is stored
only in daemon memory, is valid for 60 seconds, is limited to 16 outstanding
proofs per session, and can be consumed once by a later `register-http`. A
child challenge without a matching `child-proven` proof is never sufficient.

### `register-http`

```json
{"version":1,"type":"register-http","sessionId":"...","controlProof":"sha256:...","pid":123,"birth":"proc:...","childProofId":"...","childChallenge":"...","view":"primary"}
```

All fields are required and no others are accepted. `pid` is a positive JSON
integer (`true` is not an integer here). The frame requires an exact
connection-bound `controlProof` and an existing, unexpired, unconsumed child
proof from this session whose PID, birth and child challenge match exactly. The
daemon rechecks the child process: `/proc/<pid>` must exist, be owned by the
same UID, have the same birth identity, and differ from the daemon and
controller PIDs.

The inserted proof is burned before the duplicate check, so a replay cannot
succeed even when the first registration failed with
`CONTROL_REGISTRATION_EXISTS`. `(controller, view)` pairs are unique among live
registrations; `view` is an opaque caller-chosen binding name. Per-session
registrations are limited to 16. A session may hold at most 8 concurrent control
connections per daemon.

The `registered` response returns `sessionId`, `registrationId` (32 hex),
`capabilityId` (64 hex), `secret`, `kind`, `phase`, `view`, `pid`, `birth`,
`childChallenge`, `childProofId`, `childChallengeReceipt`, `epoch`,
`homeBinding`, `socketBinding`, `declarationDigest` and
`capabilityLifetimeSeconds`.

`childChallengeReceipt` is the one-time receipt binding this child challenge to
this registration:

```text
hmac-sha256:<hex>
HMAC-SHA-256(key = capability secret as ASCII, message = canonical JSON of
{"receiptVersion":1,"sessionId":S,"childChallenge":C,"childProofId":P,
 "registrationId":R,"capabilityId":K,"pid":PID,"birth":B,"epoch":E,"view":V})
canonical JSON = UTF-8, sort_keys=True, separators=(",",":"),
                 ensure_ascii=False, allow_nan=False
```

The child must recompute and verify it before using the capability. A receipt
that does not match makes the registration unusable; the capability is not
retried with another challenge.

### `revoke-http`

```json
{"version":1,"type":"revoke-http","sessionId":"...","controlProof":"sha256:...","registrationId":"..."}
```

Rejects unknown registrations with `CONTROL_BAD_FRAME`. A successful
`revoked` response carries `sessionId` and `registrationId`. Revocation removes
the capability from the registry and its transport binding immediately.

### `close`

```json
{"version":1,"type":"close","sessionId":"...","controlProof":"sha256:..."}
```

Drops the session, revokes every registration owned by that session and returns
`closed` with the session id.

## Connection-bound control proof

Every frame after a successful hello must carry the exact `controlProof`
returned by that hello. It is recomputed from both challenges and both process
identities, so replaying the same JSON body on another connection fails with
`CONTROL_PROOF_MISMATCH`:

```text
sha256:<hex>
sha256 over UTF-8 canonical JSON of
{"proofVersion":1,"sessionId":S,"controller":C,
 "clientChallenge":CC,"controllerChallenge":SC,
 "peerPid":PPID,"peerBirth":PB,"daemonPid":DPID,"daemonBirth":DB}
canonical JSON = sort_keys=True, separators=(",",":"),
                 ensure_ascii=False, allow_nan=False
```

Missing or empty proof fails with `CONTROL_PROOF_REQUIRED`. A frame with a
mismatched `sessionId` fails with `CONTROL_BAD_FRAME`. A second hello on an
established connection, or `prove-child` after hello on the controller
connection, fails with `CONTROL_HANDSHAKE_REQUIRED`.

The proof, ticket and capability secret are secrets. They must never appear in
logs, exceptions, diagnostics or error messages. Error messages are fixed,
sanitized strings and never echo request payload, proof material or secrets.

## Stale-socket and instance record rules

A refused connection (`ECONNREFUSED`) alone never proves that the old daemon is
dead and never authorizes deletion. Safe stale replacement requires an adjacent
`control.instance.json` record:

- regular file, not a symlink, owned by the daemon user, no group/other bits
  (mode `0600`), between 2 and 4096 bytes;
- exact JSON fields `version` (integer `1`), `pid` (positive integer), `birth`
  (non-empty ASCII string), `socketDev` (non-negative integer), `socketIno`
  (positive integer), `socketPath` (exact control socket path string); no extra
  or missing fields;
- the recorded `(socketDev, socketIno)` must equal the currently listed socket
  inode;
- the recorded PID must be proven not live, or live with a different birth
  identity (PID reuse). A live process with the recorded birth means
  `control-socket-in-use`; zombie/exited/no-such-process is dead; unreadable or
  unknown liveness is `control-socket-instance-unproven`.

Refusals preserve both the socket and the record. The daemon rechecks the
socket inode immediately before unlinking and removes only an instance record
it owns or has proven stale. Symlinked or non-socket paths are
`control-socket-unsafe`; recorded inode mismatch is
`control-socket-replaced`; a missing record is
`control-socket-unknown-instance`; unsafe or invalid records are
`control-instance-record-unsafe`. Directory ownership/mode failures fail
closed.

## Catalog gate

The first v2 IPC request admitted for a capability must be `daemon.catalog`.
Only after that response is verified does the capability unlock other business
methods:

1. the response is v2 and carries `schemaVersion: 1`;
2. `epoch` equals the transport binding epoch;
3. the static declaration digest recomputed from `methods` equals the
   `declarationDigest` issued in hello/registered;
4. the method's `MethodSpec.phase_caller` allows this phase and caller kind.

Merely requesting the catalog, a malformed catalog, a wrong schema version, an
epoch mismatch or a digest mismatch does not mark the capability verified and
must not unlock business. Before catalog verification, business methods return
`CALLER_NOT_AUTHORIZED`. The gate applies per capability, so revoking one view
does not authorize another.

## Declaration digest

`declarationDigest` must be byte-identical to the HTTP catalog implementation in
`src/hyprial/shell/impl/request_http/catalog.py:128-142`:

- project every declared method to exactly these 13 fields, per method, taking
  the declared value (including null) for each:
  `name, owner, httpExposed, httpBinding, mode, mutates, retry, confirmation,
  audience, paramsSchema, resultSchema, errors, ipcParamValidation`;
- order methods by `name` ascending (stable for this purpose because names are
  unique);
- wrap as `{"schemaVersion":1,"methods":[<projection>...]}`;
- serialize with `json.dumps(sort_keys=True, separators=(",",":"),
  ensure_ascii=False, allow_nan=False)`, encode UTF-8, SHA-256, and prefix
  `sha256:`.

Dynamic serving/bootstrap state (epoch, package version, method state,
callable/reason fields) must never enter the digest. A daemon-side copy lands
through the public kernel facade and must not reverse-import the shell. The
same static declaration golden must produce the same digest on the HTTP and
daemon sides; the bootstrap projection and the serving projection use the same
field list and algorithm.

## Error codes

| Code | Meaning |
| --- | --- |
| `CONTROL_BAD_FRAME` | malformed frame, wrong version/type/fields, non-finite number, duplicate key, oversized/non-UTF-8 payload, frame-order violation for a session id |
| `CONTROL_HANDSHAKE_REQUIRED` | hello missing, duplicate hello, or child proof attempted on the controller connection |
| `CONTROL_SESSION_EXISTS` | duplicate live `sessionId` |
| `CONTROL_PROOF_REQUIRED` | post-hello frame missing the control proof |
| `CONTROL_PROOF_MISMATCH` | control proof does not match this connection bindings |
| `CONTROL_CHILD_PROOF_REQUIRED` | register references no live/unconsumed child proof |
| `CONTROL_CHILD_PROOF_MISMATCH` | child proof PID, birth or challenge does not match the register frame |
| `CONTROL_PROCESS_MISMATCH` | child PID equals daemon/controller PID, birth differs, or `/proc` ownership/identity check fails |
| `CONTROL_REGISTRATION_EXISTS` | live `(controller, view)` registration already exists |
| `CONTROL_REGISTRATION_LIMIT` | per-session registration or child-proof cap reached |
| `CONTROL_UNAVAILABLE` | control service stopped, session cap reached, or daemon birth identity unavailable |
| `CONTROL_SOCKET_UNSAFE` | socket path is unsafe during startup |
| `CALLER_NOT_AUTHORIZED` | v2 IPC business call before catalog verification, revoked/unknown capability, or wrong OS peer |

Refused control frames end that connection; the daemon never retries, never
downgrades to v1, and never discloses which proof field failed.

## Lifecycle and handoff boundary

The serving-phase `ControlConsumer` owns exactly one control listener and
installs the `GuiTransport` into the daemon application. The daemon creates the
control state; it must not be re-created by the HTTP host or mini-backend.

- Bootstrap and same-PID promotion must hand off the same control state
  explicitly. The control connection and its in-memory proof must survive a
  promotion; old business capabilities are revoked; the handshake is rebuilt
  only by a real process restart.
- Before irreversible promotion, an unavailable control capability (non-Linux,
  peer or birth unavailable) must leave the daemon running with the capability
  level marked BLOCKED. It must not raise after promotion, must not fall back
  to v1 and must not make the daemon or coldstart fail.
- Control startup must not re-acquire the identity transaction or state fence.
  Coldstart sends no v2 `local-bootstrap` request. No phase or caller bypass is
  permitted.
- The HTTP owner implements the host producer and the v2 client. The
  mini-backend integration port consumes this control service; it must not
  build a second control service.

Bootstrap registration, promotion proof retention and the HTTP producer are not
implemented by the consumer branch that introduced this document.
