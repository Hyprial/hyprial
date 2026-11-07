# Organization network status contract

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
