# Tailcat cycle fix: implementation contract (0.5.5.1)

Status: implementation contract for the Go and Python shards, not release evidence.
Base: `80a5baafa016cb3db0bc6469aff11bee6a342bbb`.
Brief SHA256: `4157c905ad2c2ebe72cfa29b3472e73d2835b7d296c9d589171873aba03279bc`.
The integrate node must reconcile any subsequent wire changes in this document,
`contract/`, both implementations, and protocol fixtures before acceptance.

## 1. Decisions

- Establish **tailnet → Zenoh → OrgFS**. Remove members in the reverse order.
- The sidecar membership table is the only source of tailnet admission.
  Python issues explicit operations. Python does not compute or replay allow sets.
- Each tailnet has one fixed authority, initially the org owner's device.
  There is no election. Members refresh directly from that authority every 60 seconds.
  A successful refresh grants a 300-second lease.
- Trust member announcements in this release. Do not implement MACs, signatures,
  challenges, incarnation proofs, or a new replay-authentication protocol.
  A claimed master must match an active roster row. This is NOT proof of master-key possession.
- Keep per-server derived transport keys. Never register a master key as a DERP client.
- No mixed-version interoperability. Upgrade the authority first during a maintenance window.
  Reject old invitations. Issue new v2 invitations.
- Keep per-org publication fault isolation from `fix/directory-record-compat@9dd91e612`.
  Do not retain the directory-driven bootstrap or admission compatibility patches.

Terminology: `masterPublic` is the existing master **client** public key.
`serverPublic` is the key embedded in a Tailcat address. These are different keys.
“Master-key-addressed control dial” means selecting the pinned authority by its
master identity, then dialing its address with a per-server derived client.
It does not mean using the master private key as a transport client key.

## 2. Persisted membership schema and configuration

The sidecar exclusively owns `<HYPRIAL_HOME>/tailnet/membership.json`.
Use an exclusive process lock, atomic replacement, and durable writes before success replies.
The containing directory is owner-only (0700). Files and backups are 0600.
Reject corrupt or unsupported stores. Never reset them or import again automatically.
An implementation may use another internal storage engine without changing the logical schema.

All public keys use the existing canonical Tailcat key string encoding.
IDs are nonempty UTF-8 strings, at most 1024 bytes. `tailnetId` is an opaque stable string.
For upgraded and new orgs, use the existing org directory `spaceId` as `tailnetId`.
This lets disconnected local copies select the same tailnet without coordination.
Numbers are nonnegative JSON integers, bounded by 2^53−1. Revisions start at 1.

Logical records:

| Record | Required fields |
| --- | --- |
| Store | `schemaVersion:1`, `tailnets` (map keyed by tailnetId), `operations` (map keyed by operationId) |
| Tailnet | `tailnetId`, `authority`, `revision`, `members`, `migration`, `invites` |
| Authority | `owner`, `deviceId`, `masterPublic`, `serverPublic`, `address`, `controlPort` |
| Member | `owner`, `deviceId`, `masterPublic`, `serverPublic`, `clientPublicByServer`, `address`, `state`, `revision` |
| Migration | `source` (`new` or `directory-0.5.5`), `sourceDigest`, `completed:true` |
| Invite | `inviteId`, `token`, `owner`, `expiresAtMs`, `state` (`issued`, `redeemed`, `revoked`), `deviceId` (empty before redemption) |
| Operation receipt | `operationId`, `op`, `tailnetId`, normalized request, terminal result |

`members` is a map keyed by deviceId. `invites` is a map keyed by inviteId.
Member `state` is `active` or `tombstone`.
`clientPublicByServer` maps target server public keys to announced derived client public keys.
An empty map is valid, especially after migration. It never grants master-key admission.
`address` can be empty while a member starts. Otherwise its server key must match `serverPublic`.
Tombstones retain identity and revision, but clear address and derived mappings.
Only the authority changes membership revisions. A transaction increments the tailnet revision once.
Every changed member receives that revision. Identical announcements do not increment it.

Active duplicate master/server/derived identities across different devices are conflicts.
The same device may belong to several tailnets with identical owner/master/server identity.
A business connection requires at least one shared, leased, active membership.
Removing one membership must not destroy another tailnet's valid membership.
For inbound business traffic, the actual transport peer key must equal an active member's
`clientPublicByServer[localServerPublic]` in a locally valid tailnet. Missing mapping means deny.
For outbound business traffic, the target must have an active row in a locally valid tailnet.
A locally valid tailnet has an active local row and an authority-local or unexpired lease.
No implicit replacement of a master/server key is allowed.
A tombstoned device needs a new invitation for explicit re-enrollment.

Python settings use the following new keys:

| Key | Default and meaning |
| --- | --- |
| `forwarding.tailnet.membershipStore` | `<home>/tailnet/membership.json`, absolute path |
| `forwarding.tailnet.controlPort` | `7448`, separate from mesh port 7447 |
| `forwarding.tailnet.authorities` | map `tailnetId → Authority`, empty initially |
| `forwarding.tailnet.trustMembersForUpperLayers` | `true` for 0.5.5.1, transitional flag described in §7 |

Keep `forwarding.mode` and existing DERP settings. Do not add automatic pin discovery.
Authority addresses and invite tokens are secrets. Keep them in private settings/state,
never in ordinary logs or public diagnostics. Private stdio/control replies may carry them.
A settings pin must match the persisted pin. A mismatch requires explicit re-pin, not overwrite.
Lease deadlines are process-local monotonic times. Do not persist a usable lease across restart.

## 3. Stdio v6: exact command and response shapes

Continue newline-delimited JSON on stdin/stdout. Diagnostics use stderr.
Require `v:6` and advertise `tailnet-membership-v1`, `tailnet-control-v1`, and
`unix-inbound-v1` in `hello.capabilities`. Remove the `allow` capability.
Retain existing unrelated capabilities and operations, with their version changed to 6.
Reject v5 peers with `SIDECAR_PROTOCOL_MISMATCH`. No v5 fallback is permitted.

Every new command has exactly `{v,op,requestId,params}`.
`requestId` is nonempty and at most 128 bytes. `params` is an object.
Reject unknown fields, unknown operations, invalid types, and duplicate JSON keys.
One command gets one correlated terminal reply, including failures.
Unsolicited events do not carry `requestId`.
Limit each stdio or control frame to 1 MiB and each tailnet to 1024 member records.
Reject larger results with `MEMBERSHIP_LIMIT_EXCEEDED`, never return a partial roster.

Success: `{"v":6,"event":"membership-result","requestId":"r1","op":"roster-fetch","data":{...}}`.
Failure: `{"v":6,"event":"error","requestId":"r1","op":"roster-fetch","code":"AUTHORITY_UNREACHABLE","message":"Authority is unreachable."}`.
The placeholder `...` denotes the exact data shape listed below, not a wire field.

Mutating commands require `operationId` in params, at most 128 bytes.
Receipts belong to the authority, except local init/re-pin receipts.
Retrying the same operationId and normalized request returns the original result.
Reusing it for another request returns `OPERATION_CONFLICT`.
`requestId` correlates one exchange and is excluded from operation comparison.
Do not journal transient failures as terminal success or advance an operation's stage.

### 3.1 Startup changes to existing `up`

Keep the existing flat `up` shape, not the new params envelope.
Add required `membershipStore`, `owner`, `deviceId` and optional `controlPort` (default 7448).
Remove `allow` and `allowAny`. Those fields and the old `allow` operation are rejected.
Require `inboundTarget:"unix:/absolute/path"` in daemon-managed mode.
The socket need not exist when `up` succeeds. The sidecar must not probe Zenoh readiness.
Validate the parent directory's ownership and 0700 mode before forwarding.
Keep the existing key file, address file, DERP settings, slots and hold behavior.
Production exposure `admission:"any"` is rejected. Only the internal control listener bypasses membership admission.

`up` returns the existing `state:Running` response with the requestId and new
`readiness:{"sidecar":true,"control":true,"member":false}` for an uninitialized
store. These are actual stage facts: a restored authority with an active local
row can return `member:true`; a non-authority restart needs a fresh roster lease.
The example does not require an unconditional member reset.
Running means the process and control listener are ready, not that Zenoh is ready.

### 3.2 New commands

All fields in these params/data tables are required unless marked optional.
`Authority`, `Member`, and `Migration` mean the exact objects in §2.
Arrays use stable deviceId order. Empty arrays and maps are serialized explicitly.

| op | params | success data |
| --- | --- | --- |
| `membership-init` | `operationId`, `tailnetId`, `authority:Authority`, `migration:Migration`, `members:Member[]` | `tailnetId`, `revision`, `initialized:true` |
| `roster-fetch` | `tailnetId` | `tailnetId`, `authority:Authority`, `revision`, `members:Member[]`, `leaseSeconds:300` |
| `member-announce` | `operationId`, `tailnetId`, `address`, `clientPublicByServer` | `tailnetId`, `revision`, `member:Member` |
| `member-revoke` | `operationId`, `tailnetId`, `deviceIds:string[]`, `expectedRevision`, `confirmed:true`, `reason` | `tailnetId`, `revision`, `deviceIds`, `backupPath`, `propagation:"lease-bounded"` |
| `membership-status` | optional `tailnetId` | `readiness`, `tailnets:TailnetStatus[]` |
| `invite-create` | `operationId`, `tailnetId`, `owner`, `expiresAtMs` | `invite:InviteV2` |
| `invite-redeem` | `operationId`, `invite:InviteV2` | same data as `roster-fetch`, plus `deviceId` |
| `authority-repin` | `operationId`, `tailnetId`, `authority:Authority`, `expectedRevision`, `confirmed:true` | `tailnetId`, `revision`, `backupPath`, `restartRequired:true` |

`membership-init` runs only once per tailnet. Same-operation retries are idempotent.
Other attempts return `MEMBERSHIP_ALREADY_INITIALIZED`.
An authority imports the supplied members with revision 1 and empty derived maps.
A non-authority supplies `members:[]`, stores only the pin and migration receipt,
and remains unleased at revision 0 until it fetches the authority's roster.
A new authority must include its own active row. Do not read OrgFS from Go.

`member-announce` updates only the local device's row, never another device's identity.
The sidecar derives the local keys. Python supplies an empty map `{}` to request automatic
announcement for every server currently in the roster. Nonempty maps must equal local derivation.
Repeat automatic announcement when the roster introduces a server or the local address changes.
Freeze expanded announcement params for each operationId. A new announcement uses a new operationId.

`member-revoke` is all-or-nothing for the specified devices. Reject unknown devices.
Check operation replay before expectedRevision. A stale fresh request returns `REVISION_CONFLICT`.
Before commit, create a durable pre-change backup and close the affected admission gates.
After commit, terminate affected inbound/outbound business streams and leases.
Do not terminate valid streams shared through another tailnet.
Never permit the authority to remove itself, nor a local store mutation to remove its executing device.
A confirmed member self-leave forwards to the authority, which executes the remote mutation.
The member closes its local business gates after the authority confirms removal.
A one-node authority cannot leave/delete its tailnet through an accidental member removal.
It needs a separate confirmed shutdown or authority-transfer maintenance procedure.

`invite-create` requires an authority-local management call. Set validity to at most one hour.
`InviteV2` is exactly `{version:2,tailnetId,authority,inviteId,token,owner,expiresAtMs}`.
Use a random 32-byte base64url token. The existing outer org link keeps its space metadata
and carries this object in `tailnet`. The Python invite decoder rejects any non-v2 link.
No offline join-request or new signature scheme is required in this simplified release.
The invitation binds the owner. First redemption binds the device and consumes the invitation.
Same-device retries return the original receipt. Another device gets `INVITE_CONSUMED`.
Redemption atomically adds the active device, its address and derived key for the authority,
and the authority's derived key for the new server in the same revision, then
returns the complete roster. This permits either lexical dial direction before
the next 60-second refresh. The joiner announces keys for the remaining servers next.
No OrgFS holder, directory row or Zenoh session is a prerequisite for redemption.

`authority-repin` is local, confirmed, and allowed only while business networking is stopped.
It makes a pre-change backup, installs the explicit pin, clears the usable lease, and requires restart.
It does not elect an authority or create a new authority's membership table.
Transfer requires stopping writes, transferring the full table and receipts, then re-pinning all nodes.
The new authority must be an active member in that table. No online authority transfer is supported.

`TailnetStatus` is exactly `{tailnetId,role,revision,authorityDeviceId,memberReady,
leaseRemainingSeconds,activeCount,tombstoneCount,code,members}`.
`role` is `authority` or `member`. `leaseRemainingSeconds` is null for authority, otherwise 0–300.
`code` is null when healthy or one error code from §8.
`members` contains Member objects for private daemon consumption, not public log rendering.
A filtered `membership-status` for an unknown tailnet returns
`MEMBERSHIP_NOT_INITIALIZED`. Unfiltered status returns an empty `tailnets` array
for an empty store; first-create and first-redemption probes use that form.
Python uses this projection for candidates and derived-key→owner lookup.
It must not read the membership file directly or substitute directory candidates on failure.

## 4. Tailcat control v1

Port 7448 is sidecar-owned. It never proxies to Python, Zenoh or OrgFS.
Control and business dials use the same per-server pool in `engine/clients.go`.
A control request uses one JSON line and one terminal JSON response, then closes its stream.
Use the same op, params, data and error shapes as §3, except `v:1` on control frames.
Every control request also has `sender:{owner,deviceId,masterPublic,serverPublic,address}`.
The local sidecar constructs sender from its key material and device identity, not caller-supplied JSON.
Allow only `roster-fetch`, `member-announce`, `member-revoke`, and `invite-redeem` remotely.
For redemption, add `clientPublicByServer` to params, generated by the joiner sidecar.
Other params are unchanged. Control timeout is 30 seconds.

The authority checks the pinned tailnet and the sender's active master/device/owner tuple.
It accepts roster-fetch before any derived mapping exists. This is the migration escape from the cycle.
For announcement, check that the transport peer key equals the announced key for this authority's server.
This consistency check does not prove possession of the claimed master private key.
For redemption, check the invitation instead of requiring an existing member row.
Unknown/tombstoned senders cannot fetch or announce. Neither roster-fetch nor announce can resurrect them.

Remote revoke permits only the sender's own deviceId. Other-member removal requires authority-local management.
Consequently, an owner using a non-authority device cannot silently perform remote administrative removal.
Return `AUTHORITY_REQUIRED` and retain the pending removal operation for execution on the pinned authority.
This limitation also applies to Casdoor-driven removal, regardless of which daemon detects it.

A member accepts rosters only from the pinned authority address/server, never from a peer's cache.
No derived map is needed to dial that server: derive locally from the existing private master and server public.
The control port remains reachable while business membership is absent or expired.
Do not use a broad business-port admission exception to make control reachable.

On each refresh, record monotonic t0 before sending. The reply must arrive within 30 seconds.
The lease ends at t0+300 seconds, not response time+300. Refresh every 60 seconds.
Apply each full roster atomically. Reject lower revisions or changed contents at the same revision.
Persist the revision and tombstones. Restart requires a fresh lease before business traffic.
Authority-local membership does not need a lease from itself.
At expiry, close business gates and existing business streams. Retain control connectivity for recovery.
The sidecar performs refresh and announcement itself, without a Python maintenance tick.
A stdio `roster-fetch` explicitly requests the same refresh operation.
Polling bounds revocation propagation to 300 seconds after the last authorized refresh.
The authority closes its own affected connections immediately. Do not report instantaneous global revocation.
A revoked device cannot renew. Other members also stop admitting it after refresh or lease expiry.

## 5. Migration and readiness sequence

Python reads each local directory copy once, offline, before starting network-dependent OrgFS services.
The parser may accept missing `clientPublicByServer`. It imports master keys, not derived admission maps.
Keep owner/device/master/server/address consistency checks and exclude removed records.
Use the org owner's selected device as authority. Never select arbitrarily among ambiguous owner devices.
If that device/pin/address is absent, return `MIGRATION_AUTHORITY_MISSING` for operator repair.
Record the source digest and accepted/rejected rows. Do not merge divergent copies automatically.
Only the authority's import defines membership. A non-authority imports its pin/address, not a competing roster.
Persist Python's migration receipt so deleting a sidecar store cannot silently re-trigger directory import.

Startup stages are independent facts, not synonyms:

1. `sidecar`: key/store loaded, local process and slots available.
2. `control`: Tailcat server and control listener available, even with no Zenoh backend.
3. `member`: local row active and authority-local or covered by a fresh roster lease.
4. `zenoh`: Python opens its private Unix listener and connects the sidecar's member candidates.
5. `orgfs`: Python binds OrgFS, joins spaces, and publishes local records independently per org.

New sidecar events use `{v:6,event:"membership-changed",tailnetId,revision,code}` and
`{v:6,event:"readiness",readiness:{sidecar,control,member}}` with Boolean readiness values.
`code` is null for normal change. Expiry/revocation emits a change and a readiness update.
Top-level `member` is true when at least one local tailnet is ready.
A caller must also check the specific TailnetStatus.memberReady before using that tailnet.
Zenoh/OrgFS readiness belongs to Python diagnostics and is not inferred by the sidecar.

Candidates come only from active, leased roster rows with nonempty addresses.
Use `(masterPublic,deviceId)` lexical order: the greater tuple initiates the mesh connection.
Do not consult directory join-time, Zenoh liveness, or unknown-view fallback dialing.
Normal retry of a selected candidate remains valid. Control refresh has no ordering dependency.
On projection failure, retain diagnostic errors and stop admitting new business traffic from stale candidates.
Do not create an OrgFS service merely to recover network candidates.

## 6. Removal, confirmation, and recovery

Python persists one operation and its target deviceIds before any destructive step.
Enumerate all target owner's devices from the sidecar roster, not only directory rows.
Every destructive user operation needs `--yes` or interactive confirmation.
Show the affected owner/devices, tailnets, authority, and connections that will close.
Confirmation is explicit operation input, never inferred from a missing TTY.
For `org.remove` / `org.leave` IPC, `confirmed:true` must carry the preview's
`operationId` and exact `deviceIds` array. A confirmation cannot create a new
operation. Missing, malformed, unknown, superseded or completed bindings, mismatched IDs,
or changed target identities before upper commit fail with
`REMOVAL_CONFIRMATION_STALE` and require another preview; no membership or
journal mutation follows that refusal. Completed confirmations are refused before
service-level completion effects can repeat. A fresh preview with no new targets
returns the original receipt, without a second confirmation RPC from the CLI.
Both interactive and `--yes` CLI paths send these fields for pending operations.
Directory and ACL removals are independent: absence in either space must not
skip the other. Do not commit the upper-layer receipt until both have succeeded.

Removal phases:

1. Obtain confirmation and record the fixed target set.
2. Commit OrgFS member/device changes and record the upper-layer receipt.
3. Record `zenoh-deny:not-implemented-0.5.5.1`. In 0.5.5.2 this phase must obtain ACL deny acknowledgement.
4. Send explicit `member-revoke` with the same operationId to the authority.
5. Record its committed revision and `propagation:lease-bounded`.

Resume from the saved phase after crashes. OrgFS failure prevents the tailnet step.
An authority outage leaves revocation pending, not successful. Retry the same operationId.
Casdoor webhook, startup scan, and periodic scan all enter this operation path.
Unconfirmed automatic discoveries remain `pending-confirmation`, not `removed`.
A non-authority detector records `pending-authority` for other-member removal.
Do not silently stop at `_remove_orgfs_member` or claim the lease fixes an unsent revoke.
Direct edits to OrgFS do not revoke tailnet membership. Report that distinction.
Pre-change membership backups are mandatory for revoke, removal and re-pin/authority transfer.
A failed backup prevents the destructive table mutation. Backups do not authorize automatic rollback.

## 7. One transitional upper-layer flag

`forwarding.tailnet.trustMembersForUpperLayers=true` selects all of the following together:

| Check | 0.5.5.1 treatment |
| --- | --- |
| Directory-derived tailnet admission, dial candidates, dial direction | Deleted, not a flag-controlled fallback |
| Zenoh org ACL | Not implemented in this release, report that fact |
| OrgFS supplier directory-membership gate (`orgfs/mesh.py:280`) | Bypass the directory-membership test on the sidecar-managed path |
| OrgFS requester membership mode (`orgfs/network/ingress.py:289`) | Bypass `_member_mode`, retain requester format validation |
| Unknown-supplier startup exception | Replace with the same explicit transitional policy, not another fallback |
| OrgFS author/path/commit/hash/CRDT validation | Retain |
| Tailnet roster/admission/lease/revocation | Retain |
| Management permissions, Casdoor permissions, HTTP principal | Retain, resolve peer identity from sidecar instead of directory |

Apply the flag only to the sidecar-managed private Unix transport. Do not retain an open TCP Zenoh backdoor.
The flag grants no new local management rights and does not permit arbitrary protected-document authorship.
Report that shared sessions do not provide complete inter-org isolation in 0.5.5.1.
Setting the flag false retains the existing OrgFS membership checks, but does not create a missing Zenoh ACL.
0.5.5.2 must implement ACL enforcement and restore the gates before changing the default to false.
It must not restore the OrgFS→tailnet dependency.

## 8. Error codes

Retain unrelated existing errors. Add these stable codes and preserve them across Python and Go:

| Code | Meaning/action |
| --- | --- |
| `SIDECAR_PROTOCOL_MISMATCH` / `PEER_UPGRADE_REQUIRED` | Upgrade daemon+sidecar together / upgrade remote peer |
| `MEMBERSHIP_NOT_INITIALIZED` / `MEMBERSHIP_ALREADY_INITIALIZED` | Explicit init needed / refuse another import |
| `MEMBERSHIP_STORE_INVALID` / `MEMBERSHIP_LIMIT_EXCEEDED` | Preserve corrupt/unsupported store / reject oversized state |
| `MIGRATION_AUTHORITY_MISSING` | Supply an unambiguous owner authority pin/address |
| `AUTHORITY_UNREACHABLE` / `AUTHORITY_REQUIRED` | Retry control / execute management on pinned authority |
| `AUTHORITY_PIN_MISMATCH` | Refuse automatic pin replacement |
| `MEMBER_UNKNOWN` / `MEMBER_REVOKED` | No active matching device / tombstone exists |
| `MEMBERSHIP_LEASE_EXPIRED` | Control-only until refresh succeeds |
| `MEMBER_CONFLICT` / `REVISION_CONFLICT` / `ROSTER_ROLLBACK` | Identity conflict / stale write / stale or inconsistent snapshot |
| `OPERATION_CONFLICT` | operationId was used for different input |
| `REMOVAL_CONFIRMATION_STALE` | Daemon removal confirmation lacks a valid frozen operation/target binding; re-run preview |
| `CONFIRMATION_REQUIRED` / `SELF_REVOKE_REFUSED` / `LAST_AUTHORITY_REFUSED` | Require confirmation / refuse local self-cutoff / refuse authority removal |
| `MEMBERSHIP_BACKUP_FAILED` / `NETWORK_NOT_QUIESCENT` | No destructive write / stop networking before re-pin |
| `INVITE_VERSION_UNSUPPORTED` / `INVITE_INVALID` / `INVITE_EXPIRED` / `INVITE_CONSUMED` | Reissue old link / invalid binding or token / expired / different device already redeemed |

Use `BAD_REQUEST` for invalid shape, deprecated allow fields and unsupported admission-any.
Errors never include tokens, full addresses, private keys or raw secret-bearing requests.
A protocol timeout does not prove the mutation failed. Retry using its operationId.

## 9. Deletion and lifecycle inventory

Line numbers below refer to base `80a5baafa`, except rows explicitly marked `9dd`.
`D` = `src/hyprial/daemon/impl`, `G` = `sidecar/hyprial-tailcat/internal`.
These are mandatory semantic changes, not permission to delete unrelated validation.

| Location | Required change |
| --- | --- |
| `G/engine/engine.go:202,343–403,754–759` | Replace allow inputs with membership admission, isolate control bypass |
| `G/proto/message.go:438–490`; `G/engine/engine.go:693–717` | v6 up fields, Unix inbound target, no Zenoh readiness dependency |
| `G/engine/clients.go:50–91` | Preserve shared pool and close-old-before-new generation handoff for control too |
| `D/org/network/admission/__init__.py:14–104` | Delete directory allow reconstruction |
| `D/org/network/access/snapshot.py:136–237,286–383` | Remove derived admission, bootstrap merge and unreadable→allow logic |
| `D/org/network/access/actor.py:157–161,248–331` | Remove desired_allow and rebuild→allow/after_allow coupling |
| `D/application/netendpoints/forwarding.py:88,339–350`; `D/forwarding/supervisor.py:766–776` | Remove admission fallback and replay after restart |
| `D/forwarding/controller.py:601–623`; `D/forwarding/authority.py:274–277` | Replace daemon allow writes with explicit membership operations |
| `D/org/network/service.py:612–648,777–794`; `D/org/network/state.py:122–220` | Delete add_bootstrap_peer and persistent dial-only bootstrap patches |
| `D/org/network/ipc.py:487–529` | Replace the entire candidate path, including OrgFS service construction, liveness and directory fallbacks |
| `D/org/network/service.py:604–609` | Remove network use of directory_peers |
| `D/forwarding/slots/reconcile.py:171–181`; `D/application/netendpoints/endpoints.py:535–540` | Consume sidecar candidates only |
| `D/org/network/dialing/__init__.py:72–218` | Remove directory order and 120-second liveness fallback |
| `D/org/network/service.py:352–356,698–743` | Remove bearer bootstrap exception and derived publication as admission prerequisite |
| `D/application/wiring/transport.py:383–385,963–971` | Start sidecar/control before Zenoh, remove old TCP listener dependency |
| `D/forwarding_config.py:113–130,146–160` | Replace TCP matching-listener prerequisite with private Unix configuration |
| `D/org/network/casdoor/sync.py:511–522`; `D/org/network/casdoor/membership.py:200–216` | Route managed removal through durable confirmed revoke phases |
| `D/orgfs/webserver/ipc.py:192` | Resolve derived identity from sidecar projection |
| `src/hyprial/identity/impl/org_directory/models.py:99–108,216` (9dd) | Do not merge global compat-read for admission, use a one-time migration parser |
| `D/org/network/directory.py:312–329`; `D/org/network/access/snapshot.py:216–223` (9dd) | Remove misleading republish-to-recover-admission warnings |
| `D/upgrade/steps/directory_record.py:1–25`; `D/application/netendpoints/forwarding.py:431–444` (9dd) | Do not merge forced republish recovery, replace with explicit one-time migration |
| `D/org/network/service.py:587–608`; `D/org/network/ipc.py:451–467` (9dd) | Keep per-org publish containment and structured error reporting |
| `D/application/netendpoints/forwarding.py:446–447` (9dd) | Keep failure/skipped results from advancing the success digest |
| `D/forwarding/slots/reconcile.py:150–169` (9dd) | May retain bounded PEER_UNKNOWN logging, not an admission fallback |

## 10. Required verification and handoff

Both shards must record RED before implementation and GREEN after implementation.
The integrate node must test at least:

1. Exact v6 and control fixtures, unknown fields, missing requestId, and unsupported old versions.
2. First bootstrap with only local master rows and authority pin/address, no derived map or live OrgFS.
3. Repeated init, corrupt store, crash-before/after commit, lost reply and operationId retry.
4. Announcement propagation, duplicate identities, changed address, and persisted tombstones.
5. Fresh v2 invitation redemption, old-link rejection, expiry, retry and another-device reuse.
6. Confirmation, backup failure, self/authority guards, all target devices, and Casdoor removal recovery.
7. Authority outage, monotonic lease expiry, revoked renewals, stale roster and restart without lease.
8. Two-node 0.5.5 upgrade with real request/reply and three-node authority-first staged upgrade.
9. E2E-025 (`contract/e2e/run-tailcat-org-invite-join.py`) with fresh isolated homes.
10. DERP duplicate-key regression unchanged, plus concurrent control/mesh/service to at least three servers.
11. Sidecar/control readiness without a Zenoh socket, then Unix backend startup and normal OrgFS replication.
12. Per-org publish failure isolation, no directory/liveness fallback, and multi-tailnet removal without collateral cutoff.

Use `/tmp/hr-*` homes and local labrelay. Do not inspect production state or credentials.
Synchronize `contract/tailcat-admission`, `contract/org-network`, wire goldens, and ops/E2E skills in implementation shards.
Run native, structural, module-boundary, Ruff and the exact CI merge-tree gate during integration.
Unit/build success does not establish network acceptance. Report PASS, FAIL or BLOCKED per real scenario.

References read: round-1 design at `dffc9cc71`, its B2/B3 review, and deadlock evidence 02–05 at `9dd91e612`.
The round-1 cryptographic protocol is superseded, not a prerequisite for this contract.
Contract-Impact: design-only commit, implementation shards must update published contracts.
Skill-Impact: design-only commit, implementation shards must document changed operator procedures.
Product and real-network validation at this contract checkpoint: **UNKNOWN**, no product code changed.

Contract checkpoint validation (2026-10-27):

- `uv run --no-project --python 3.12 python scripts/ci/check_structural_limits.py`: PASS, no violations.
- `uv run --no-project --python 3.12 python scripts/ci/check_module_boundaries.py`: PASS, no violations.
- `uv run --extra test ruff check .`: PASS. Initial invocation without the test extra lacked Ruff.
- `git diff --check`: PASS.
- Product unit/full suites and real network scenarios: NOT RUN for this design-only shard.

## Clarification 2026-10-27 (R&D head, from the contract author during the Go shard)

`up.readiness.member` is governed by §5's readiness definition, not by the literal `member=false` in the §3.1 example. The §3.1 value is the uninitialized-start example only. A restored authority with an active local row may report `member=true` immediately after restart. A restarted non-authority node reports `member=false` until it holds a fresh roster lease from the authority. Fixtures (including `FakeMembershipSidecar`) and contract goldens must follow this rule.

## Clarification 2026-10-27 (R&D head): recovering a revoke after REVISION_CONFLICT

A confirmed revoke whose frozen `expectedRevision` no longer matches the roster fails with `REVISION_CONFLICT` and stays pending; it is never reported as done. Operator recovery is an explicit re-confirmation that refreshes only `expectedRevision`, under all of these conditions:
1. The removal is proven uncommitted (journal pending, no matching commit at the authority).
2. `operationId` and targets are unchanged; targets may not be widened or replaced.
3. A fresh preview is shown (new revision, affected members, difference from the previous preview) and `--yes` or interactive confirmation is required again; no automatic retry.
4. An audit record is written (old and new `expectedRevision`, confirmation time, operator).
5. A target already absent from the roster (removed by another operation) counts as done and is not sent again; a target whose identity reappeared with a new incarnation is refused and requires a separate new operation.
6. Committed OrgFS changes are never redone.
