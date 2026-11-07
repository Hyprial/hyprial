# Organization device admission into Tailcat

The daemon starts each forwarding child with an empty allow-set and
`allowAny=false`. Creating a loopback mapping or seeing an online actor does
not authorize an incoming connection. Previously no production caller replaced
that empty set: two such daemons could list each other's directory records and
mapped ports while both rejected inbound traffic.

During the forwarding maintenance reconcile, after membership/leave processing,
the daemon derives an exact replacement set from its live organizations under
the protocol-v5 per-server-key model:

- Resolve the unique organization directory space; ambiguous or unreadable
  authority produces an empty snapshot.
- Require the local owner and the device owner to remain current members.
- Read devices through `OrgFsDirectoryStore`, preserving its protected document,
  author, malformed-record and conflicting-key checks.
- Let `S` be this sidecar's canonical `serverPublic`. Select only
  `clientPublicByServer[S]` from each valid peer row. Exclude this device,
  invalid derived keys, master `clientPublic`, foreign-server mappings, missing
  mappings, and keys claimed by distinct device identities across organizations.
  Reject the entire row when any per-server value equals its master
  `clientPublic` or when two per-server values are equal; admission and inbound
  attribution apply the same fail-closed rule. Invite bootstrap addresses are
  not grants.
- Call the protocol-v5 `allow` command with `allowAny=false`, correlated under
  `forwarding-request-id-v1`. Require acknowledgement of the exact count and
  false flag.

Successful snapshots are cached by sidecar PID and sorted key set. A child
relaunch receives the set again. Device deletion, rotation, leaving an org or
membership revocation removes old keys on the next reconcile; the sidecar's
existing `Allow` implementation disconnects revoked clients.

An authority read failure clears previously admitted keys rather than retaining
them. If applying a replacement fails, close the sidecar through its controller
so stale grants cannot survive; its bounded supervisor owns recovery. Repeated
identical directory errors are logged once until recovery, avoiding a log storm.

Diagnostics are `org.admission.synced` (`peerKeys`, `sidecarPid`),
`org.admission.directory_failed` and `org.admission.apply_failed`
(`errorType` only). No key, Tailcat address, private credential or raw exception
is logged. Inbound attribution likewise selects the distinct `(owner, deviceId)`
whose `clientPublicByServer[S]` equals the authenticated PROXY TLV key across all
organization rows. Exactly one identity may be attributed; zero or multiple
identities produce the existing `no-identity` 403 decision log, without key
material.

The public CLI is unchanged, but the wire and directory schemas are not. The
sidecar protocol is v5 with `client-key-derivation-v1`; directory rows require
`clientPublicByServer`. This is a flag-day boundary: v5 rejects old sidecars,
legacy master-only rows grant nothing, and no compatibility path admits the
master key or enables `allowAny`. The full derivation, publication, rotation and
rollout contract is
[tailcat-per-server-client-keys-2026-10-21.md](tailcat-per-server-client-keys-2026-10-21.md).

This is not an alternative invitation or authorization mechanism. A newly
invited device whose authenticated directory row or local-server mapping has not
arrived is not admitted by this path. Host/agent visibility and a real message
ACK must be verified after rollout; a mapped port or `dialed=true` alone is not
connectivity evidence.
