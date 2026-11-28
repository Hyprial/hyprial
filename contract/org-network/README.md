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
not admission or HTTP attribution authority. With stdio v6 the sidecar roster
supplies the derived-key-to-owner mapping; absent, ambiguous, master or
foreign-server keys fail closed. The `self` pane still exposes the local public
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
