# Tailcat per-server client keys

**Decision:** option B. Keep one `tailcat.Client` and its private backend per
remote server. Give every such client a deterministic key derived from this
device's existing outbound client private key and the target server public key.
Tailcat remains transport infrastructure: this design has no Zenoh, OrgFS,
organization, or space dependency in the sidecar.

## 1. Problem and invariant

A sidecar currently pools one `tailcat.Client` per secret Tailcat address, but
constructs every client with the same `material.ClientPrivate`. Each client owns
a separate backend. DERP selects one active connection for a node key, so two
backends registered under that key can receive each other's frames. The isolated
DERP regression in
`sidecar/hyprial-tailcat/internal/derpidentity/shared_client_key_test.go`
reproduces lost or misrouted sustained traffic and duplicate-client
registration.

The new invariant is:

> For one device master client key and one target Tailcat server public key,
> exactly one stable derived client key identifies all pooled connections to
> that server. Different server public keys produce different client keys.

The master private key stays in the existing owner-only sidecar key file and is
never emitted. A Tailcat address is only parsed inside the sidecar; derivation
uses its authenticated target `ServerPublic`, never the free-text address, PSK,
DERP endpoint, device label, organization, or local slot.

## 2. Frozen derivation

Version 1 is:

```text
IKM  = raw32(master ClientPrivate)
salt = empty
info = "hyprial-tailcat/client-per-server/v1\x00" || raw32(serverPublic)
OKM  = HKDF-SHA256(IKM, salt, info, 32)
key  = X25519-clamp(OKM)
```

`X25519-clamp` clears bits 0–2 of byte 0, clears bit 7 of byte 31, and sets bit
6 of byte 31 before constructing the Tailcat node private key. The versioned,
NUL-terminated label is a domain separator. The server key is fixed-width raw
key material, not its textual spelling. The same key file and server public key
therefore produce the same result across restarts, address/PSK changes, and
organization membership changes; another server or derivation label does not.

The engine parses `ServerPublic` from `tailcat.ParseAddr` when it creates a
pooled client and uses the derived private key as `tailcat.Client.Key`. The pool
is indexed by that server public key, which is also the derivation input. A new
address or PSK for the same server performs a coordinated generation handoff:
the old client is closed first, every surviving mesh/service lease is migrated,
and only then is the replacement published for dialing. Thus old and new
credentials never put the same derived key on two simultaneous DERP
connections. A failed stream or remote-port dial alone does not rotate the
shared generation. A separate pure sidecar operation derives only the
corresponding public half for admission publication; it does not expose private
material.

## 3. Sidecar protocol

The closed stdio protocol advances from v4 to v5 and advertises
`client-key-derivation-v1`.

Request:

```json
{"v":5,"op":"derive-client-key","requestId":"...","serverPublic":"nodekey:..."}
```

Success event:

```json
{"v":5,"event":"client-key-derived","requestId":"...","serverPublic":"nodekey:...","clientPublic":"nodekey:..."}
```

Both public keys are validated node keys. The request is served only after
`up`, from the in-memory master client private key. Error text never contains
private material or a Tailcat address. Existing `map-peer`, `map-service`,
`allow`, `peer-key`, PROXY v2, and address-file semantics otherwise remain the
same.

The daemon sends this command through the existing bounded, single-owner
sidecar authority. It can be reached while an organization command publishes a
row, so the conservative IPC reachability gate records this one reviewed
`derive_client_key -> _request` path. The operation talks only to the local
sidecar process: it performs no remote Tailcat/DERP request, publishes no
secret, and remains subject to the authority's existing capacity and timeout.
Periodic directory reconciliation retries a failed or not-yet-ready derivation.

## 4. Authenticated directory admission

### 4.1 Record shape and contract change

The organization directory device row adds one required field:

```json
{
  "deviceId": "...",
  "owner": "...",
  "serverPublic": "nodekey:...",
  "clientPublic": "nodekey:...master public...",
  "clientPublicByServer": {
    "nodekey:...target server public...": "nodekey:...derived client public..."
  },
  "address": "...",
  "keyGeneration": 1,
  "updatedAt": "..."
}
```

`clientPublic` remains the public identity of the device's master outbound key;
it is not a Tailcat admission key after this change. `clientPublicByServer` is a
bounded map of validated node public keys. An empty map is valid while a new
row is converging. JSON publication remains deterministic (`sort_keys=True`).
The identity model, directory contract, conflict checks, fixtures, and any
peer-key-to-device lookup all change together.

For every current validated peer device in an organization, the publishing
daemon asks its local sidecar to derive the public key targeted at that peer's
`serverPublic`. It republishes its own authenticated, owner-scoped protected
row when this map changes. No private key enters Python or the directory. The
reconciler checks for newly observed server keys at a bounded cadence, so a
peer that joins after this row was first published gains an entry without an
address or key rotation.

For inbound admission, a server with public key `S` reads each current member's
validated row and selects only `row.clientPublicByServer[S]`. The existing
protected-document path, authenticated author, organization membership,
malformed-row quarantine, and cross-device conflict rules remain the trust
path. The daemon replaces the sidecar allow set with those selected keys and
`allowAny=false`. A bootstrap address, observed connection, master
`clientPublic`, mapping for another server, missing mapping, nonmember row, or
conflicting claim grants nothing. There is no TOFU and no new unauthenticated
channel. The server cannot recompute a peer's derived public key; it verifies
who asserted it through the same authenticated directory path that already
binds the device's public keys.

This makes the admission test precise: an admitted device's key derived for
this server succeeds; that device's master key and a key derived for any other
server fail.

### 4.2 Revocation and lookup

Membership/device removal removes every derived key of that device at once:
all rows cease contributing to every server's next exact replacement set.
Master rotation increments `keyGeneration`, changes all derived keys, and
replaces the map. Server-key rotation changes the map selector; peers publish a
new entry after observing the new server row, while the old target entry grants
nothing on the rotated server.

Inbound Tailcat and DERP now authenticate the derived key. Consequently:

- `peer-key` returns the derived public key;
- PROXY v2 TLV `0xE0` carries that derived public key;
- connection audit records the derived public key;
- identity attribution finds the unique admitted directory identity whose
  `clientPublicByServer[localServerPublic]` equals that key. The current
  OrgFS HTTP resolver returns that row's owner; its validated directory row
  remains the device binding. It must not attribute via master `clientPublic`.

Conflicting derived-key claims are quarantined/fail closed just like current
server/master key conflicts.

## 5. Rotation and rollout

This is an explicit flag-day migration, not a silent compatibility mode.
Protocol v5 sidecars and the new daemon/directory model must be deployed to all
participating nodes before their directory rows are republished. Old strict
readers reject the added field; new readers reject legacy rows without it for
admission. During a mixed-version interval, peers can remain visible but native
Tailcat traffic is **not guaranteed** and must be reported unavailable, never
made to work by admitting master keys or enabling `allowAny`.

Deployment order is: install v5 sidecar+daemon everywhere; stop native network
traffic; restart/reconcile all nodes so each writes the new row; wait until all
rows contain the per-server mappings; then resume acceptance. Rollback requires
all nodes to the old record/protocol together and republishing old rows. There
is no dual-key allow window because admitting the master key would retain the
original DERP duplicate failure and weaken the acceptance test.

## 6. Connection cost and direction finding

For `N` devices in one converged full mesh, let `D` be the number of directed
peer mappings selected by the daemon. The sidecar creates one Tailcat client
backend and normally one DERP client connection per distinct mapped server, so
clients/backends are `D` mesh-wide and `outdegree(node)` on each node. There are
also `N` Tailcat server backends/registrations. Worst-case mutual dialing has
`D=N(N-1)` and about `N²` server+client backends/DERP registrations. The normal
single-dialer policy has `D=N(N-1)/2`, for about `N(N+1)/2` total. Multiple
services/slots to the same server public key share the client and do not
multiply this count. A changed secret address synchronously closes the old
transport and migrates its leases before the replacement can dial; it never
retains two live generations with the same derived identity.

Current behavior is **not normally two tunnels per device pair**. Code in
`src/hyprial/daemon/impl/org/network/dialing/__init__.py` orders devices by
membership `added_at` and device id: the later device dials the earlier one.
The executable fixture
`tests/daemon/org/test_org_dial_direction.py::test_every_pair_has_exactly_one_dialer_and_everyone_dials_the_owner`
asserts XOR (exactly one dialer) for every pair. On this design head the whole
fixture passed (`9 passed`), confirming the normal one-direction finding.
Unknown/tied/disagreeing directory views, direction-selection failure, or the
120-second liveness fallback deliberately degrade to mutual dialing, so two
outbound clients for a pair can occur transiently or as a safety fallback.
This change does not alter that policy.

E2E-025's two-node fixture additionally drives application data both ways, but
bidirectional data over a connection is not evidence of two independently
built tunnels. The required acceptance observation is the mapping set on both
nodes: under converged membership exactly one side maps the other; fallback
cases may map both.

## 7. Required tests

1. Existing sustained forced-DERP regression passes unchanged and DERP reports
   no duplicate client registration.
2. Derivation is deterministic across reload/restart, domain-separated, and
   different for different server public keys.
3. One admitted device can connect only with its derived key for this server;
   its master key and a foreign-server derived key are denied.
4. Removing membership/device state replaces the allow set and disconnects the
   derived key.
5. The flag-day mixed case fails closed: v5 does not admit a legacy master-only
   row and v4 rejects the v5 row/protocol rather than broadening admission.
6. Peer-key, PROXY TLV, audit/attribution, protocol goldens, identity row
   round-trips, conflict handling, and restart reapplication cover the new key.
7. The dial-direction fixture remains unchanged and continues to show exactly
   one normal dialer plus its documented mutual-dial fallbacks.
