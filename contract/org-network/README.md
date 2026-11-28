# Organization network status contract

Membership records (`agents/<owner>/<agentId>.json`), the
personal org naming rule and their admission rules are in
[`membership.md`](membership.md); the frozen shapes are
`contract/fixtures/org-membership/{records,admission}.json`.

`org.network` is a best-effort view with three independent panes:

- `directory`, sourced from the local OrgFS projection;
- `mappedPeers`, sourced from the local forwarding-sidecar projection; and
- `sidecar`, sourced from the same forwarding projection.

`directory` contains every valid device recorded in the local OrgFS directory.
Forwarding presence never filters these rows. Each device has a separate
`presence` field; it is `"unknown"` when no independent local presence projection
is available. A valid directory device row includes `clientPublicByServer`, an
object mapping canonical `nodekey:` server public keys to the device's
HKDF-derived client public key for that server. The map is bounded to 1024 entries;
keys and values must be non-empty `nodekey:` strings, with no duplicate derived
value, and no derived value may equal the row's master `clientPublic`. Missing,
invalid, duplicate or cross-role-aliased maps make the entire row invalid.
`clientPublic` remains the master public identifier for directory identity only
and is never an inbound admission key. Directory maps are publication data,
not admission or HTTP attribution authority. With stdio v7 the sidecar roster
supplies the derived-key-to-owner mapping; absent, ambiguous, master or
foreign-server keys fail closed. Service grant admission follows the same rule:
the exposing daemon takes a recipient's admission key from its own sidecar
roster (`membership-status`), never from this directory map, which the
publishing path leaves empty (see
[contract/service-grants](../service-grants/README.md)). The `self` pane still exposes the local public
`serverPublic`. Directory outages do not change sidecar membership.

UUID addressing adds `deviceUid` as an output field only; no routing key
changes. The `self` pane always carries `deviceUid`: the local device's
immutable uid (32 lowercase hex digits of a UUID4), or `null` before the
device identity is provisioned. Directory rows may carry an optional
`deviceUid` of the same shape; readers accept rows with or without it, and a
directory row view shows it when the row has one. A device publishes
`deviceUid` in its own directory row only once id-form agent minting is
switched on, because readers that predate the field reject a row with an
unknown key. The local `device.json` wire record is unchanged.
An explicit `"deviceUid": null` in a directory row is rejected; writers omit
the key instead.

Flip hazard: turning id-form minting on is therefore also a change to the
peer-visible directory schema. On the next `publish_self` every device
republishes its own row with `deviceUid` (turning the switch off again
republishes it without the key). A peer still running a build that predates
the field rejects such a row and drops that device from its directory view
until it upgrades. Whoever flips the switch has to plan for this mixed-version
window; the rollout order is not decided here.

Not applied (explicit N/A): the forwarding membership roster observation
(`public_observation` in `daemon/impl/forwarding/membership/projection.py`)
gains no `deviceUid`. It copies tailnet-level rows only (`tailnetId`, `role`,
`revision`, `authorityDeviceId`, readiness, lease and counts), never one row
per device, and the Go sidecar's membership status carries no device uid.
`authorityDeviceId` is a sidecar routing key, which UUID addressing leaves
unchanged. The observation's only reader is the forwarding readiness
projection. Adding a uid would need a sidecar change or a join with the
OrgFS directory; that is not decided here.

Protected device rows remain backward compatible with the legacy required
shape. New writers can additionally publish `machineFingerprint` (64 lowercase
hex digits) and `firstPublishedAt` (an ISO-8601 UTC timestamp); readers accept
rows where either field is absent. The fingerprint is derived from machine
identity, never from Tailcat key material. Directory identity conflicts remain
reportable metadata problems, but cannot mutate the sidecar roster or select
candidate addresses. Bootstrap-peer admission/dial exceptions are removed.
The one-time offline migration and subsequent sidecar membership commits check
identity/address consistency. Do not use Directory conflict repair as an
implicit authority transfer or key reissue.

A directory row may additionally carry a device's encryption key as the
paired keys `encryptionKeyId` and `encryptionPublicKey`: either both are
present or neither. The key is an X25519 key of the `user` kind, the one
kind that can open a sealed credential on the device. `encryptionPublicKey`
is the 32 raw bytes of the public key as standard base64, the encoding the
local key store already uses, and `encryptionKeyId` is `x25519:` followed by
the first 32 lowercase hex digits of the SHA-256 of those bytes. A reader
refuses a row where only one key is present, where either is malformed, where
the id is not the digest of the public key, or where the public key is a
low-order point; such a row is invalid as a whole. The key set of a row stays
closed: `encryptionKeyPrivate` or any other unknown key is rejected. A service
grant credential is sealed to this public key and bound to this
`encryptionKeyId` ([contract/service-grants](../service-grants/README.md)); a
row without the pair can be granted access but cannot receive a credential.

A device publishes the pair once it holds a committed account and a device
record that belongs to that account (settings `owner`, `identityIssuer` and
`userId`, and the record's `deviceUid`). A device without them publishes a
row without the pair, which stays valid, and its publish does not fail. When
the local key store is temporarily unreadable (such as a busy store lock or
transient I/O error), publication retains any pair already present on this
device's current directory row instead of overwriting it without keys; only
an explicit absence of prerequisites clears the pair. Serving start and
relogin ensure the key on disk under the home identity transaction lock, and
periodic `publish_self` caches the resolved public pair in memory by
`(userId, deviceUid)` (invalidating when either changes) and reads at most
once across all orgs per pass, with a short lock timeout on a cache miss. The
pair carries no switch of its own: an upgraded daemon that has the
prerequisites publishes the pair on its next `publish_self`. Whether the key
was made by serving start or by a cache-miss publish does not matter: both
read the same local key store and get the same key.

Mixed-version window. A directory reader on a build that predates the two
keys refuses a row that carries them. Measured on a real OrgFS store: an
upgraded build wrote two rows, one with the pair and one without, and the
reader of the build before this change kept only the row without the pair,
logged `directory device record has unknown keys: ['encryptionKeyId',
'encryptionPublicKey']` and reported the other row as an
`invalid-device-record` data problem; the reader of the upgraded build kept
both and reported no problem. The rows were written through the directory
store, not by `publish_self`, and no two daemon builds talked to each other
over a network. A daemon on the older build therefore leaves that device out
of its directory view until it upgrades, while its other rows are not
affected. On rollback, once an older daemon runs its next `publish_self`, it
overwrites its own row with an older-format row without the pair and becomes
visible again, whereas any device still running the newer build or offline
with a row carrying the pair continues to be dropped as
`invalid-device-record` by older builds. Because no switch delays the pair,
every device of an org has to be upgraded together, before the first upgraded
device republishes its row. The migration steps are in
`docs/guides/migration-device-encryption-key.md`.

A daemon on a build that publishes the pair but does not know it in its
`org.network` result schema is not a supported state: the result is checked
against a closed schema at request time, and such an answer fails as a
whole. The schema and the publication change ship together.

The response keeps the existing pane values and adds `paneStatus`, keyed by
`directory`, `mappedPeers`, and `sidecar`. Each status is one of:

```json
{"available": true}
```

or:

```json
{"available": false, "reason": "timeout|capacity|not-configured|error"}
```

An `error` status can also include the local exception class as `errorType`.
A failed or expired pane has its existing value represented as empty or null;
it does not fail the other panes or the `org.network` request. Pane reads use
independent one-second local deadlines and never initiate a sidecar refresh,
mesh query, organization join, or OrgFS synchronization.

The aggregate view also carries `unavailableOrgs` and `dataProblems`:

- `unavailableOrgs[].reason` is `creating`, `partial`, or `policy-denied`;
  `missing-acl-binding` is only an `org repair` candidate reason.
- each `dataProblems` object always has `{org, path, reason}`. Lifecycle
  marker problems use `path: directory/org.json`; their complete reason set is
  `invalid-json`, `invalid-object`, `missing-state`, `invalid-state`, and
  `unknown-state`. Only `unknown-state` adds `value`. Device-row problems use
  `reason: invalid-device-record` or `content-pending` and add `owner`,
  `deviceId`, and the observed content `version` when available. The same
  device problems also appear under that org's `directoryPending.dataProblems`.
  Invalid rows are isolated from valid devices and retried with bounded
  exponential backoff; a changed content version bypasses quarantine. The
  machine-readable reason sets are pinned in `data-problem-reasons.json`.
- a missing marker has no data problem and uses the legacy binding
  classification (`legacy-active` when `space_meta.aclSpaceId` is bound,
  otherwise `partial`). An unparseable marker reports `invalid-json`; a parsed
  non-object reports `invalid-object`; an object with an absent or null `state`
  reports `missing-state`; and an empty or non-string `state` reports
  `invalid-state`. Those malformed markers retain the legacy classification
  for status compatibility. An unknown non-empty string state reports
  `unknown-state` and is `partial`; explicit `creating`/`partial` fail closed;
  explicit `deleted` keeps the dev behaviour.

## Leave requests

A member leaving an org (`org.leave`, CLI `hyprial org leave`) removes its own
device row and files one leave request at `directory/leaves/<user>.json`, a
protected document whose path user is the requester. The record is a JSON
object written by the requester's node:

```json
{
  "org": "group-acme",
  "user": "bob@example",
  "deviceId": "node-b",
  "requestedAt": "2026-10-03T12:00:00+00:00",
  "memberAddedAt": "2026-10-01T08:30:00.123456Z"
}
```

`memberAddedAt` is copied verbatim from `addedAt` of the requester's current
member row in the org's directory space at the moment the request is filed.
The writer omits it only when that row cannot be read; such a request is never
fulfilled.

The owner/admin sweep (`process_leave_requests`) fulfils a request only when:

- the filename names a valid user;
- the document's authors are exactly that user; and
- `memberAddedAt` is a non-empty string equal to `addedAt` of that user's
  current member row.

A missing, non-string or empty `memberAddedAt`, an absent member row, or any
other value counts as stale: the request is skipped with the
`org.leaves.stale` log event and left in place. This keeps a request filed in
an earlier membership period from removing the user after they are removed
and invited again (a re-invite writes a new `addedAt`). The field is
self-reported like the rest of the record; it guards against stale requests,
not against a forging node. Requests written before this field existed are
therefore never fulfilled automatically.
