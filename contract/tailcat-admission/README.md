# Sidecar-owned tailnet admission (stdio v6)

The frozen wire/control and store contract is
[tailcat-cycle-fix](../../docs/design/tailcat-cycle-fix-contract-2026-10-27.md)
(commit `040be17c7fd95b4c2e9515d61347647cf4f3b2c4`). This replaces the v5
Directory-to-allow model. Python does not compute or replay admission sets.

| Input/state | Required behavior |
| --- | --- |
| Active, leased roster row | Admit its derived key for this server only |
| Master key, foreign-server derived key, revoked/unknown device | No business admission |
| Directory write, presence, bootstrap address | No admission effect |
| Authority outage | Existing lease at most 300 seconds; then control-only |
| Restart | Sidecar loads durable membership; Python does not replay allow |
| Explicit confirmed revoke | Backup, close affected gates, commit tombstones and close streams |
| Direct OrgFS mutation | Does not revoke tailnet membership |
| Failed projection | Stop selecting stale candidates; retain diagnostic error |

Managed v6 rejects `allow`, `allowAny` and open exposure admission. It requires
`tailnet-membership-v1`, `tailnet-control-v1`, `unix-inbound-v1` and private Unix
inbound; no old-sidecar or TCP fallback. Per-server derivation and single-active
DERP client generation remain unchanged. Before TCP dialing, the existing pooled
client resends Tailcat transport registration so a restarted server can relearn
that derived identity. This does not grant membership, replace the generation,
or use Ping's cached acknowledgment as proof of current connectivity.
The daemon reconciles its mapping memo with successful sidecar status, because
lease expiry can remove mappings independently. An explicit `PEER_UNKNOWN` from
`unmap-peer` means already absent; other errors retain the assignment. Neither
case changes the fixed Zenoh connect set or grants admission.
`clientPublicByServer:{}` on announce
requests Go-side derivation, not master-key admission.

Filtered `membership-status` on an unknown tailnet returns
`MEMBERSHIP_NOT_INITIALIZED`; initial create/redeem probes use unfiltered status
and inspect its tailnet list. Redemption commits both the new member's key for
the authority and the authority's key for the new server in one revision, so
either lexical dial direction can connect without waiting for periodic refresh.

Python imports local directory state only once, offline. Its compatibility
parser may accept a missing derived map but discards that map. Only the pinned
org-owner authority imports membership. Other devices import the pin and fetch
the roster. Ambiguous authority refuses migration. Durable completed receipts
prevent reimport after store loss. Old invitations require reissue as InviteV2.

Removal freezes devices/revision/operation ID in a private journal. Confirmation
precedes OrgFS changes, followed by `zenohDeny:not-implemented-0.5.5.1`, then
explicit revoke. With zero frozen devices there is no tailnet mutation:
the operation reports `tailnetUnchanged:true` and no forged Go revoke receipt
(empty `member-revoke.deviceIds` is invalid). Outages remain pending. Casdoor discoveries are not implicit
confirmation; non-authority detectors record pending-authority.

`org.remove` and `org.leave` IPC confirmations require `confirmed:true`, the
preview's `operationId:string` and its exact `deviceIds:string[]` (including an
empty list). CLI interactive and `--yes` paths carry both fields automatically.
A confirmation never creates an operation. Missing/malformed bindings, unknown
or superseded/completed operation IDs, mismatched target lists, or changed active target
identities before upper-layer commit return `REMOVAL_CONFIRMATION_STALE` and
instruct the operator to re-run preview. They do not mutate membership or the
journal. Completed confirmations are refused before service-level completion
effects can repeat (including the local self-leave decision). A fresh preview
with no new targets returns the original completed receipt; the CLI returns it
without a second confirmation RPC. A fresh preview may select newly enrolled
devices under a new operation ID; an old confirmation cannot confirm that ID.
Once upper work starts, targets cannot be replaced by a fresh preview. Recovery
after an explicit revision conflict retains the rules below.

For `org.remove` only, a preview (`confirmed:false`) may provide a non-empty
`deviceIds:string[]` to select active devices belonging to the named user.
Unknown, inactive or other-owner IDs return `DEVICE_NOT_FOUND`; malformed or
empty selections return `INVALID_ARGUMENT`. Selecting every active device of
that user returns `LAST_DEVICE_REFUSED`: use the ordinary unfiltered removal
to remove the user. Selection is sorted/deduplicated and persisted as
`selectedDeviceIds`, separate from the exact confirmation binding. A selected
operation reports `membershipAction:preserved` and `remainingDeviceIds`;
Casdoor, directory and ACL memberships are retained, with
`upperLayerReceipt:preserved` and (when configured) `casdoorGroup:retained in
<group>` on completion. At least one unselected active device must still exist
at confirmation and retry; revision fencing prevents a stale roster from
committing the revoke. Selection cannot widen during recovery. A fresh preview
of an in-flight confirmed operation replays its frozen revoke receipt even if
the target is already tombstoned after a lost reply. It does not re-select it.
A fresh preview
may replace a selection only before upper work begins. `org.leave` and previews
without `deviceIds` keep their existing behavior and result shape.

CLI `org remove <org> <user> --device <id> [--device <id> ...] --yes` carries
the selection in the preview and its exact binding in confirmation. `--yes`
is mandatory for selected-device removal. The existing unfiltered interactive
confirmation remains supported. Selected removal never removes a user's last
active device; unfiltered removal still removes the user and all their devices.

Directory and ACL membership removals are independently idempotent. An absent
directory row must not skip the ACL write, including after crash/retry and on
Casdoor-origin operations. The upper-layer receipt is recorded only after all
required removals succeed; failures leave `orgfs-pending` and send no revoke.

The R&D clarification at `f2d9f679dbff25d98361b8f2de17214c0bb5ca59`
permits explicit revoke recovery only after a proven uncommitted
`REVISION_CONFLICT`. The journal enters `revision-conflict`, then a new preview
enters `revision-confirmation` with `revisionRefresh` (old/new revisions,
remaining/already-removed/unselected device IDs). Re-confirmation preserves the
operation ID and original `deviceIds`, refreshes expectedRevision and records
`revisionAudit` with time and operator. `pendingDeviceIds` excludes targets
already removed elsewhere; it never includes new devices. Changed target
identities or reused target keys under new IDs refuse with `MEMBER_CONFLICT`.
Committed upper-layer changes do not repeat. Ambiguous failures still replay
identical parameters; they cannot enter revision refresh. Existing journals
without identity evidence refuse recovery rather than guess.

`hyprial org repin <tailnetId> --authority <deviceId> [--control-port 7448] --yes`
is an offline local maintenance command. It holds the daemon ownership fence,
starts no business backend/peer maps/exposures, previews affected devices, then
uses Go's confirmed re-pin and pre-change backup. A private durable operation
replays after lost replies before replacing the settings pin. A pending target
cannot silently change. It preserves every member and requires restart. It does
not perform cross-machine full-table/receipt transfer or online authority change.
Normal `up` still has control listeners and automatic refresh/announce; the
operator must stop all writers first. The local fence is not a global write lock.
`REVISION_CONFLICT` preserves the refused operation as history and requires a new
preview/confirmation under a new operation ID; ambiguous failures replay exactly.

Transitional trust bypasses OrgFS supplier/requester directory-member gates
only on the private sidecar transport. It retains document/author validation,
management permissions and sidecar admission. Zenoh ACL and complete inter-org
isolation are NOT implemented in 0.5.5.1.

Daemon `forwarding.readiness` reports independent Boolean `sidecar`, `control`,
`member`, `zenoh`, `orgfs`, plus `membershipObservedAtMs` (integer or null),
`zenohAcl:"not-implemented-0.5.5.1"` and `completeInterOrgIsolation:false`.
These are local stage facts, not end-to-end delivery. Sidecar observations are
cached, contain no private member rows, expire member leases, and are invalidated
by sidecar PID changes. Restored authorities may immediately report member=true;
non-authority restarts need a fresh roster lease. Removal completion retains the
existing `casdoorGroup` warning alongside its durable phase/result fields.

Python coverage: `tests/daemon/forwarding/membership/`, forwarding lifecycle and
InviteV2 tests. Real lease/DERP/revoke/multi-node delivery requires separate
integration evidence; mock or unit PASS is not real-network acceptance.

## Operation budgets and reply-before-reconcile (0.5.5.3)

The typed `hyprial.kernel.MEMBERSHIP_BUDGETS` table is the timeout authority
for both request-path and background membership commands:

| Operation | Seconds | Python-visible work |
| --- | ---: | --- |
| `invite-redeem` | 65 | Redeem plus roster fetch: two Go exchanges, each up to 30s |
| `member-announce`, `roster-fetch`, `member-revoke` | 35 | One Go control exchange |
| `membership-init`, `membership-status`, `invite-create`, `authority-repin` | 10 | Local command |
| Join | 105 | Shared monotonic deadline across membership and bootstrap |
| CLI join / pending autojoin / login autojoin | 120 | Outer IPC wait, including reply margin |

IPC selects the operation budget. The serial I/O actor carries an absolute
command deadline; queue residence consumes it. The wire receives the remaining
budget, with no old 31s cap. The actor allows one additional second to receive
the wire's typed failure. A command that expires in the queue is not sent later.
A running command can still finish after its caller stops waiting; a timeout
is not cancellation or rollback.

`MEMBERSHIP_SIDECAR_TIMEOUT` retains its code and adds error `data` fields:
`operation` (membership operation, or `join` for aggregate exhaustion), `leg`
(Python-visible operation, `sidecar-queue`, or bootstrap stage), and numeric
`budgetSeconds` (the effective wait, including any reduction by the shared
deadline). The message names that leg and budget. For aggregate exhaustion the
budget is 105s. Python reports `invite-redeem` for its opaque two-exchange RPC;
it does not guess which Go exchange was active. Secret-bearing request params
and remote messages are not copied into diagnostics. Other sidecar refusal
codes (`AUTHORITY_REQUIRED`, `MEMBERSHIP_NOT_INITIALIZED`, ...) remain unchanged.

CLI join emits a stage plan to stderr: redeeming invite / fetching roster,
then announcing device and bootstrapping OrgFS. This is a plan, not fabricated
live sidecar telemetry. JSON stdout remains one unchanged result/error object,
except for the additional timeout error data.

`org.invite` / `org.join` / `org.execute` / `org.remove` / `org.leave` /
`org.create` / `org.delete` reply once their OrgFS (and Casdoor) write is
durable. The forwarding reconcile that follows (`membership-status` plus slot
rebinding) runs on the daemon's `forwarding-reconcile` cadence, never on the
request thread, and each operation settles with one `org.<op>.reconcile` log
event carrying `ok`, `code` (null on success, else the failed effect's code or
exception type), `durationMs` and the operation's `org` / `user`. A pending
operation that no supervisor can reconcile logs `org.reconcile.unscheduled`.

That cadence is the only reconcile owner. The maintenance tick submits to it
too, and creates it if no supervisor existed when maintenance started. It never
reconciles inline, so at most one forwarding reconcile runs at a time, whatever
mix of ticks, org requests and join/execute bootstrap steps asks for one.
Submits that arrive while a run is active coalesce into exactly one follow-up
run. Ticks and requests take their observation times from one strictly
increasing source. So a request recorded while a run is active is never
settled by that run, even within the same millisecond. Only a run that started
after the request settles it.

The join/execute bootstrap steps `connect-inviter` and `connect-members` need
the redial finished before the next step. They submit an `org-join` request to
the cadence and wait for the run that settles it, at most 10 seconds
(`REQUEST_MEMBERSHIP_WAIT_SECONDS`). They never reconcile on the IPC thread.
If no run finishes in time, the step fails with `FORWARDING_RECONCILE_TIMEOUT`.
If that run fails, the step fails with the run's code, or
`FORWARDING_RECONCILE_FAILED` when the run has none. Only a typed `.code`
counts as the run's code. An uncoded exception (for example a bare
`RuntimeError`) always yields `FORWARDING_RECONCILE_FAILED`; its type and text
appear only in the error message, never in `code`. This differs from the
`org.<op>.reconcile` log event, whose `code` field falls back to the exception
type. Both codes are registered in `ipc_errors`. With no supervisor there is
nothing to redial: the step logs `org.reconcile.unscheduled` and continues. If
an overflow evicts a waiting step's request, that step can only time out.

Sidecar-provided transport error data is validated against the closed schema
in [sidecar-tailcat diagnostics](../sidecar-tailcat/README.md). An optional
`bytesReceived` integer is a bounded incomplete-control-response byte count,
not payload. Valid data survives org error translation and IPC framing,
including timeout and authority-unreachable errors. Invalid diagnostic objects
are discarded; a local wait timeout never invents a byte count.

The daemon IPC transport selects the typed `join-ipc` budget (110s) for
`org.join` and `org.execute`, above the 105s shared join deadline and below the
120s explicit CLI wait. Other methods retain their finite 90s transport bound.
Explicit join, pending pickup and login pickup pass that 120s wait through the
kernel client to the socket; the generic 15s `HYPRIAL_IPC_ROUNDTRIP_SECONDS`
default (or its environment override) applies only when no timeout is supplied.
A domain timeout at 105s therefore reaches the caller with its membership error
data instead of being replaced by an earlier transport timeout.

Join/execute use the remaining shared deadline for redial and slot-map actor
waits, as well as membership requests. Each of the two OrgFS joins receives the
smaller of its 30s retry allowance and the remaining shared time; retries and
sleeps do not restart the shared clock. Bootstrap checks the deadline before
and after each stage and issues no subsequent stage after exhaustion. OrgFS
facade → mesh → lifecycle propagates the optional wait bound. An expired
lifecycle waiter is removed, but its accepted effect remains owned and may
settle later. Non-join lifecycle callers have a 120s finite default backstop
and receive `unavailable` with `leg` and `budgetSeconds` on expiry. Shared join
exhaustion is translated to the membership timeout above. These are wait
budgets, not preemption of synchronous local persistence or rollback guarantees.

An expired OrgFS lifecycle waiter emits the log-only warning
`orgfs.lifecycle.wait-timeout` through the runtime logger. Its fields are
`operation` (`join`, `serve`, `checkout`, or `open-mesh`), `waitedSeconds`
(measured monotonic elapsed wait), `budgetSeconds` (allocated bound), and
`commandId` (the accepted lifecycle operation ID). It contains no space content
or invite credentials. This event is emitted for explicit bounds and the
120s default; the accepted operation may still settle after the event. In
particular, mesh-open callers that convert `unavailable` to no mesh still leave
this diagnostic event.

Reconcile request events (log-only):

| Domain / event | Level | Fields | Meaning |
| --- | --- | --- | --- |
| `org` / `org.<op>.reconcile` | info on success, warn on failure | `ok:bool`, `code:string\|null`, `durationMs:int`, `org`; `user` for invite/remove | The cadence run that covered this request finished. `<op>` is the IPC op without the `org-` prefix (`invite`, `join`, `execute`, `remove`, `leave`, `create`, `delete`). A run settles every request recorded at or before its observation time. Later requests wait for the next run. |
| `org` / `org.reconcile.unscheduled` | warn | `reason:"org-<op>"`, plus the same `org` / `user` fields | No forwarding supervisor exists. Nothing was recorded or scheduled. Every `org.*` handler reaches this through `_access_changed`. The reply is unaffected. |
| `org` / `org.reconcile.requests_overflow` | warn | `capacity:int` (64), `dropped:{reason, org, user?}` | More than 64 requests were pending, so the oldest was evicted. Only its per-operation attribution is lost: it gets no `org.<op>.reconcile` event. The reconcile itself still runs on the next cadence run, and that run covers every state change up to that point. |
| `daemon` / `forwarding.reconcile_request_deferred` | warn | `reason:"org-<op>"`, `admission:"overloaded"\|"closed"` | The cadence mailbox refused this request's tick (the `AdmissionResult` value). The request stays recorded. The maintenance tick resubmits within about one second, and that run settles the request. `closed` happens only while the daemon is stopping; in that case the request can remain unsettled. |

## One-time import hardening (0.5.5.1.1)

- Spaces that are not migration candidates are skipped, never fatal: an org whose
  `directory/org.json` has `state:"deleted"`, an org in `state/tailcat/left-orgs.json`, a space
  the local owner is no longer a member of, and a space with zero accepted device rows. Each
  skipped space logs one `membership.migration.skipped {spaceId, org, reason}` event with
  `reason` in `org-deleted | left | not-a-member | no-device-rows`. No receipt is written; the
  space is reconsidered on the next start.
- Authority is a tailcat-layer local fact. Order: the settings pin
  `forwarding.tailnet.authorities[<tailnetId>]` is used as-is; else a single owner row with an
  address; else, with several owner rows, the local device only when
  `forwarding.tailnet.authorityDefault` is `"self"` (new, default absent; only `"self"` is
  accepted). Otherwise `MIGRATION_AUTHORITY_MISSING` whose message lists candidate deviceIds (no
  addresses or keys) and the exact remediation
  `hyprial org pin-authority <spaceId> --device <deviceId> --yes`.
- The import is planned from local files (device record, settings, receipts, the membership store
  file, OrgFS rows) before the sidecar is launched; a refusal fails the start phase without a
  sidecar handshake. `daemon.start.failed` carries the error `code` (see
  `daemon-lifecycle/service-supervision.md` for `attempt`/`backoffMs`).
- Row hygiene before `membership-init` mirrors `validateStore`: owner/deviceId non-empty, both
  keys `nodekey:` + 64 lowercase hex and different, address non-empty (rejection codes
  `MISSING_OWNER`, `MISSING_DEVICE_ID`, `INVALID_KEY`, `KEYS_EQUAL`, `MISSING_ADDRESS`). Rows
  sharing any key with another deviceId are grouped transitively and only the newest `updatedAt`
  is sent; an unparsable `updatedAt` ranks oldest and no longer rejects the row. Receipt
  `diagnostics` gain `dropped: [{deviceId, keptDeviceId, reason:"DUPLICATE_KEY"}]` and
  `droppedDeviceIds`; `params` is the request actually sent.
- A failed `membership-init`/`invite-redeem` records `lastError: {code, at}` in the pending
  receipt; a later success removes it.
- `hyprial org pin-authority <spaceId> --device <deviceId> [--control-port 7448] [--yes] [--json]`
  works without a daemon (stopped or running): it opens the local OrgFS store, resolves the owner
  row's keys/address and writes only the settings pin. Errors: `SPACE_UNKNOWN`, `MEMBER_UNKNOWN`
  (candidates listed), `MEMBERSHIP_ALREADY_INITIALIZED` (table exists → `org repin`),
  `AUTHORITY_PIN_MISMATCH` (a different pin exists), `CONFIRMATION_REQUIRED` without `--yes` on a
  non-interactive stdin. Result: `{spaceId, org, owner, deviceId, controlPort, candidates,
  pinned, pinnedDeviceId, restartRequired:true}`.
- `hyprial org migration-status [--json]` (offline) lists per org space `{spaceId, org, state,
  skipReason, pinned, pinnedDeviceId, sidecarInitialized, receipt:{state, source,
  authorityDeviceId}|null, rows:{accepted, rejected, dropped}, lastError}`; rows come from the
  receipt when one exists, otherwise from a live read of the local directory.
- Python coverage: `tests/daemon/forwarding/membership/hardening/`,
  `tests/shell/cli/org/test_cli_org_authority.py`, `tests/daemon/application/startup/
  test_start_failed_fields.py`. Real-sidecar scenario: `contract/e2e/run-tailcat-membership-hardening.py`.

### Receipts (round 2, 0.5.5.1.1)

- Only a receipt with `state:"complete"` is authoritative; it is never recomputed and a missing
  store behind it is `MEMBERSHIP_NOT_INITIALIZED` (restore the backup).
- A directory-import receipt with `state:"pending"` (0.5.5.1 layout) or `"skipped"` is advisory.
  On every start `plan_migrations` recomputes the request from the current local files (skip
  rules, local-fact authority, row validation/de-duplication) before the sidecar is launched and
  rewrites the receipt: `params` become the recomputed request, the previous request is kept at
  `diagnostics.supersededParams = {params, reason:"recomputed-on-startup", previousState, at}`,
  and `lastError` is kept until a reply succeeds (then removed). The sidecar receives exactly the
  rewritten `params`.
- If the recomputed plan is a skip, the daemon emits `membership.migration.skipped` and marks the
  receipt `state:"skipped"` with `skipReason` and `skippedAt` (the file is kept, not removed, so
  history survives); a receipt whose org space no longer exists in the local store is skipped as
  `space-missing`. A skipped receipt turns back into `pending` (with `supersededParams`) when the
  space becomes a candidate again.
- A pending receipt whose tailnet the sidecar store already holds (a lost reply) is still replayed
  as-is by `operationId`; invite-redemption receipts (`redemption` key) are never recomputed.
- Authority (local fact): without a pin and without `authorityDefault:"self"`, only a sole owner
  row whose `deviceId` is this device is selected. A sole or multiple REMOTE owner row set raises
  `MIGRATION_AUTHORITY_MISSING` listing the candidate deviceIds and
  `hyprial org pin-authority <spaceId> --device <deviceId> --yes`. Member nodes therefore pin
  (offline) before they upgrade; `contract/e2e/run-tailcat-cycle-upgrade.py` does the same.
- `hyprial org migration-status` shows `receipt.state` (`pending|complete|skipped`),
  `receipt.skipReason` and `receipt.superseded` (true when `supersededParams` is present).
