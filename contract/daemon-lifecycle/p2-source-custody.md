# P2 source custody (C2 draft)

Internal composition only. The cross-host P2 orchestrator drives this path for
a harness its proven-chain table carries (pi, single-machine two-daemon, fake
model: see the AT07 I4 evidence notes); every other harness is still refused
with the P0 code and mode string. No claim of full TC013–015, two-real-machine
or real-model TC012 acceptance follows from these component/boot tests.

The two P2 fact trigger points -- `transfer.receive`'s `planDigest` and
`transfer.complete`'s `chain` -- have exactly one expected caller: that
orchestrator, driving a chain it has already admitted, on a harness some
two-daemon run has proved. They are not a general IPC surface and not a licence
for an arbitrary IPC caller to drive a chain: what keeps the branch closed is
the proven-chain table (a module constant no request, environment variable or
command line can open), never a test of who the caller is. A row that is in
that table is the only thing that opens it, and the caller still has to satisfy
the source's own rows -- quiesce intent, no attention, the seven-field join, an
incarnation observed stopped.

## What the chain carries

Two facts ride the receive payload beside the identity tuple and the plan
digest, and the target derives neither of them itself:

* the operator's resolved destination working directory (``spec.cwd``, the
  same value P0's receive hands to ``create_transfer_hosted``). The target
  records it on the landed row *and* binds the runtime context to it, so the
  row and the context cannot disagree about where the worker works; and
* the source row's own ``pinnedAdapters``. A re-pin is a MOVE, so the target
  asks its pin gate first: an adapter already held by another actor refuses
  with ``TRANSFER_PIN_CONFLICT`` and names the holder, before this daemon
  records anything of its own. Only a free adapter is re-pinned onto the
  landed actor. The list is the source's snapshot, never a caller-spelled
  literal.

## What the quiesce answer adds

The `transfer.quiesce` answer's `chain` block publishes the three facts the
orchestrator cannot derive itself, and none is reachable from
`transfer.plan`:

* `sourceHome` -- the agent home this daemon actually serves, which the CLI
  exports.  A CLI resolving its own `configured_hyprial_home()` names the same
  directory only while both processes read the same environment; in a split-env
  run it would stamp the right identity over the wrong tree.
* `signingHome` -- the daemon's own home, whose Ed25519 key stamps the exported
  bundle (AT06 slice 2c).  The signature is the machine's identity -- and, since
  F2, only half of the attribution: the target also binds the identity the key
  was enrolled for against the identity the manifest claims.  A key kept inside
  the exported payload would travel to the target with the bytes it vouches for.
* `envCredentials` -- the environment-key credentials this daemon's own
  grant/incarnation gate resolved for the worker (see
  `archive/env_credentials.py`).  The export side holds no registry and must
  not open a secret file itself, so the daemon resolves each authorized grant
  and publishes the records the CLI seals into the envelope; a grant that
  cannot produce a value degrades to a `requires_reauth` record rather than
  being silently dropped.

All three are answers about this daemon, not caller-supplied values, and all
are published only on the proven P2 branch of the quiesce the caller already
asked for.

## Attributing a received bundle

An exported bundle carries a detached signature beside its manifest, and a
landing refuses a bundle it cannot attribute.  The anchor is the *target's*
own offline file, so nothing about this needs the source to be online.

* `<bundle>/signature.json` -- a sibling of `manifest.json`, not a field inside
  it, so the existing seal is untouched.  It signs the sealed
  `manifest_digest` under a domain separator, and when the bundle declares a
  credential envelope it also binds that envelope's header digest: the same
  digest cannot be replayed under a different envelope.  The signing key is the
  exporting machine's own Ed25519 key, never the tailcat device identity, and
  the key file never travels inside the bundle.
* `<home>/secrets/transfer/trusted-source-keys.json` (0700/0600) -- the set of
  source keys this target will accept, one record per `(keyId, actor)` pair.
  Resolution is local: no network, no source machine, no user-profile lookup in
  this slice.  A missing store is an empty store, never a wildcard, so a target
  that enrolled nobody refuses every signature instead of trusting anybody.
* Attribution is a double bind (F2).  An enrollment is not a bare key: it names
  the identity that key speaks for, `(actor, sourceMachine)`, which is the
  record's own identity and the thing a re-registration must agree on.  A
  landing therefore requires **both** that the signature verifies under an
  enrolled key **and** that the bundle's sealed manifest claims that same
  identity.  One key enrolled for one actor never vouches for a bundle that
  lands as another: without the second half, the receipt names the actor the
  store was told about while the landing adopts the actor the manifest names.
  The attestation a receipt records is taken from the sealed manifest -- the
  claim that landed -- and is equal to the enrolled identity by construction.
* Refusals happen after the bundle's own seal has been checked and before any
  byte is written, and they are named: `bundle_signature_missing`,
  `bundle_signature_malformed`, `bundle_signature_unknown_key` (no such key in
  the store), `bundle_signature_invalid` (the signature does not verify) and
  `bundle_signature_identity_mismatch` (the key verifies, but the sealed
  manifest claims an actor/source machine the key is not enrolled for; the
  refusal names the manifest's claim and the key id, never the enrolled
  identity).  The bundle's `manifest_digest` remains the first integrity gate;
  the signature is the attribution gate on top of it.

The operator surface is three commands against this target's configured home,
all offline:

* `hyprial agent transfer trust-source-add --actor <actor> --source-machine <machine>
  --public-key <key|file>` -- enroll one source key.  `--public-key` takes a path
  to the source's key record, a file holding only the base64 key, or the base64
  text itself; `--key-id` asserts the exact id if the operator wants that
  checked.  `--actor` names who the key is enrolled *for*, and a landing
  enforces it against the bundle's own sealed claim -- enrolling a key here is
  the target side of the double bind, not a label.  Re-enrolling the same key
  for the same actor and machine is a no-op; the same key id for different
  material or identity is refused (`trust_store_key_conflict`).
* `hyprial agent transfer trust-source-list` -- print the store.  It does not create
  one.
* `hyprial agent transfer trust-source-retire --key-id <id> [--retired-at <iso>]` --
  record retirement.  Retirement is a record, never a delete: the line stays
  for the audit, and a retired key still resolves.  Whether a bundle signed past
  its `retiredAt` lands is the store's `retirementPolicy` (`warn` by default,
  `refuse` on demand) -- a store field, deliberately not a CLI switch and not a
  bypass.  Retiring a key that was never enrolled is refused
  (`trust_store_unknown_key`).

## A reservation that recorded no step

A refused second preparation names the holder, and the two retentions are not
the same.  A preparation with a step is resolved through its own audited
`transfer.custody.override`: the successor reaps the slot once *every* step of
that attempt records the override.  A preparation that recorded no step has no
audited override to mark -- `apply_override` answers `UNKNOWN` -- so the
refusal says plainly that there is no automatic release path for it, and does
not send the operator to a door that is not there.  An unreadable step is
neither case and fails closed with `TRANSFER_CUSTODY_UNAVAILABLE`.

## Start admission

`HoldStartsForTransfer` binds actor, expected entity token, operation ID and
generation. The harness actor owns both this decision and all native start
admission. `StartsHeld` returns held / entity-mismatch / unavailable, plus the
correlations drained before acknowledgment. Same binding attaches/replays;
another operation/generation conflicts. A positive different token retires the
old incarnation's derived hold. Query errors never retire it.

New starts of a held incarnation return `TRANSFER_STARTS_HELD`; inability to
resolve a held incarnation returns `TRANSFER_CUSTODY_UNAVAILABLE`. Both are
typed internal port errors registered in kernel IPC errors. `StartsHeld` alone
does not prove the worker is stopped: existing lifecycle deactivation and its
authoritative receipt must follow. Caller timeout does not cancel native start
custody. A late process must be registered in the runtime, or positively stopped,
before it counts as drained. Failed late cleanup remains unresolved.

Bounds reuse the harness runtime's configuration: hold entries use the restore
eligibility capacity (10,000); all pending hold observers together use mailbox
capacity (default 128); unsettled native starts/late cleanup use the existing
I/O slot multiplier (4 × start admission width). No added thread or timer.

`ReleaseStartsHold` is internal, with actor/entity/operation/generation required
to match the active hold exactly. Missing/mismatched holds, changed incarnation
or unfinished drain return `TRANSFER_CONFLICT`; unavailable identity returns
`TRANSFER_CUSTODY_UNAVAILABLE`. Invalid fields return `INVALID_ARGUMENT`.
These refusals leave the hold intact. Successful release emits
`StartsHoldReleased` without starting/stopping a worker or altering durable
custody. It creates no retained replay record: a lost ack is an unknown outcome
for the future coordinator to reconcile, never a reason to clear eligibility.

The owner-approved caller order is authentication, durable journal audit,
durable structured alert, needs-attention, exact hold release, acknowledgement,
then eligibility unsuppression. That order is wired on the existing
owner-authenticated IPC admission (`transfer.custody.override`): the caller must
name the daemon owner as `principal`; a mismatch is refused before any audit
row. There is no boolean force flag and no public P2 CLI; the audit receipt is
the command's journal row plus its durable operator alert. A persistent
override keeps the row `held` with `reason=overridden` and principal/time: boot,
wake and reconcile permit starts while ordinary P2 progression stays halted.
This prevents reboot/reconcile from undoing an authorized override. An override
that completes releases only the exact derived hold; custody is never deleted
and only a terminal receipt contract retires it.

## Journal observation

Source custody is a readonly projection populated by the short P2 EffectLane.
Mailbox reads do no SQLite or remote work. Missing projection is unavailable;
positive absence is clear; a quiesce-pending intent is unavailable until the
hold/stop protocol establishes its result; a quiesced record is held. Existing
internal journal callers remain responsible for verifying receipt authority.

The same validated snapshot carries operation/generation references for
rebuilding volatile harness holds. Hydration replays these holds through the
harness owner with the current canonical actor and original incarnation. It
requires an exact positive acknowledgement; failed replay remains unavailable
and never deletes custody. Query plus replay share the existing total 10-second
budget. Filtering a source out of the boot batch alone is not sufficient: its
rebuilt hold also rejects later starts through the harness owner.

`transfer_p2_revision` is metadata in the shared StateDatabase, updated in the
same transaction as admission/step mutations. Snapshot reads use one SQLite
read transaction. Projection `journal_version` is this durable revision;
`version` is a distinct request/publication sequence used to reject stale
completion. A fresh read after I/O recovery may have the same journal revision.

Boot classification and dormant wake hydrate through the P2 lane. Activity
policy (including degraded policy) cannot publish an unsuppress over held or
unavailable custody. Projection capacity is 10,000; query observer timeout is
10 seconds; P2 authority teardown uses the daemon's 5 second drain budget.

Explicit lifecycle start also hydrates before clearing activity suppression:
held returns `TRANSFER_STARTS_HELD`, unknown/unavailable returns
`TRANSFER_CUSTODY_UNAVAILABLE`. This caller-side observation is not the atomic
start fence; the harness owner still enforces its hold. A positive absence
allows the existing start path, including same-incarnation desired-state ABA.

## Outstanding in this draft

Explicit authenticated override and bounded pending quiesce-effect recovery
are implemented; the complete daemon restart acceptance matrix and the real
TC013-015 orchestration acceptance remain incomplete. Bounded fleet
hydration, version-aware deferred retry and automatic reconcile custody guards
are implemented as described below. The internal first-execution quiesce wrapper and owner-side token
propagation are implemented below; their composition test uses controlled
process/network edges and is not daemon crash/restart acceptance.
No new unlock, force boolean, independent identity store or supervision
exemption is introduced. TC012 remains BLOCKED on isolation and dedicated
credentials. The original two boot tests arrange the journal offline. Additional
tests now drive the real internal quiesce helper inside an isolated daemon,
pause at deterministic pipe barriers, SIGKILL it, and restart through the
ordinary entry point. They cover before-intent, after-intent and after-quiesce
source windows, plus a concurrent second source operation while the first is
pending. The test-only admission driver and fake Pi model do not validate the
public migration entry point, authenticated override, target TC015, or TC012.

## Lifecycle envelope (C2 work in progress)

`LifecycleSpec.expected_entity_token` and
`LifecycleMutationRequest.expected_entity_token` default to None (legacy
behavior), reject blank/non-string values, and remain distinct from the
compensation `expected_resource_token`. The lifecycle operation JSON persists
`expectedEntityToken` only when present; recovery and deadline requests carry
it forward.

Identity bind/release checks the expected incarnation inside the same write
transaction as the mutation, raising `AGENT_INCARNATION_MISMATCH` on conflict.
The existing identity lifecycle receipt stores the expected entity token;
a replay cannot change a legacy receipt into a fenced one. Old receipt rows
upgrade to NULL without replacing their compensation token or provenance.
This extends existing receipt metadata, not a second identity custody store.

Harness, session and route owners now compare a non-None token through their
cache-only identity port at their own admission boundary. Missing wiring is
`LIFECYCLE_INCARNATION_UNSUPPORTED`; an unreadable projection is
`TRANSFER_CUSTODY_UNAVAILABLE`; a different token is
`AGENT_INCARNATION_MISMATCH`. Harness also rechecks after asynchronous desired
state persistence and on deadline settlement. Harness/session durable receipt
metadata retains the expected token; replay cannot change it. Route retains
its existing whole-request fingerprint and bounded in-memory receipts.
Identity lifecycle create/destroy remains explicitly unsupported for a fenced
request; normal deactivate does neither. None preserves the old path.

The harness hold index maps a canonical actor URI and its local worker name
to the same slot; admission still resolves the original actor and compares
its incarnation. Release additionally requires the original actor spelling,
operation and generation, so parsing an unrelated URI never grants release.
This closes the URI/local-name gap for both new starts and in-flight drain.

These owner tests and manager envelope tests do not enable the public
coordinator or authenticated override.


## Internal quiesce execution (C2 draft)

SourceIntent/SourceFinish go through the short journal authority lane. The
source binding ID and expected incarnation must agree; no identity DB writes.
Intent/result also publish the corresponding local-actor custody observation.
QuiesceEffect runs in SourceRunner's separate EffectLane: durable intent first,
exact positive hold/drain receipt second, then existing lifecycle deactivate
under `transfer-quiesce:{operation_id}:{generation}` with the entity fence.
Only a matching COMPLETED receipt permits journal completion. Pending replay,
failed/mismatched receipt, refused admission or deadline stays unknown, retains
custody and never automatically repeats or compensates an effect.

A durable pending row left by a crashed effect is reconciled on the existing
restore cadence (boot classification and restore-wake scan), never by a new
timer. `_reconcile_transfer_quiesce` reads at most 32 pending intents per pass
through the P2 authority, and closes one only when two existing owners agree:
the lifecycle manager returns a COMPLETED stop receipt whose `operation_id`
equals `transfer-quiesce:{operation}:{generation}`, and the harness owner
positively acknowledges the derived hold for the same incarnation, operation
and generation. Both are consumed through their existing ports; the caller
sends no new start/stop and never compensates. A missing receipt, a receipt for
another operation, a refused or mismatched hold, a changed incarnation or a
failed completion all leave the intent pending and the entity fail-closed
suppressed. Completion is idempotent: a closed row is no longer pending, and a
vanished intent refuses rather than fabricating one.

Budget declaration is p2-source-bounds.json: total 70 seconds, 32 pending
observers, one effect worker. Journal status remains memory-only and independent
of those waits. Same in-flight request attaches; different fingerprint rejects.
A volatile execution generation prevents old completion retries from settling a
new submission; accepted completion remains in EffectLane custody until ack.
Shutdown drains source effects before lifecycle dependencies and the journal.
This is internal composition only, not new IPC admission or full crash recovery.

### Deferred restore observation (C2 draft)

Boot custody hydration shares a 10-second fleet budget, including hold replay;
it is not 10 seconds per worker. A timed-out accepted read is retained and
reattached, never cancelled or resubmitted while unresolved. The existing
restore-wake cadence observes at most 32 deferred entities per tick with the
same shared budget, at most three automatic read retries per entity/spec.
No new timer, process launcher, or durable authority is introduced. Memory
tracking is bounded at 10,000 entries; overflow/exhaustion is reported and does
not release suppression. Deferred entries rotate to prevent starvation.

Only an observation with a publication version strictly newer than the deferred
version can permit reclassification; absent identity, changed incarnation or
changed desired generation retires that observation without publishing an
allow. A newer clear fact reapplies the existing activity policy, which may
still suppress the worker. The existing lifecycle owner receives a stable resume-wake operation for a
freshly allowed deferred spec. After acceptance, later ticks only query that
same receipt (zero observer wait); no repeated submission is triggered by a
pending result. The lifecycle owner's follow-up decision selects a distinct RESUME kind:
agent.bind → harness.ensure → optional session.register → route.managed.ensure.
Its inverse receipts remain those of the existing owners. It never creates or
destroys the Agent and never ensures/drops the preserved persona route. Both
custody-deferred and activity-dormant wake use RESUME with the current entity
token. CREATE retains its explicit unsupported-fence rejection; no mode bit or
new process launcher is introduced.

Operation IDs distinguish kinds: dormant RESUME uses
`resume-wake:{actor}:{entity}:{disposition}:{desired_generation}`, while
custody-deferred RESUME uses `resume-wake:{actor}:{entity}:custody:{desired_generation}`.
Legacy `restore-wake:` CREATE receipts are historical observations only, never
submitted as RESUME or used as its acceptance receipt. A still-running or
compensating legacy operation defers the wake. A terminal legacy receipt is
followed by a fresh harness-owner native observation using the existing bounded
port, not a cached status read alone. `RefreshHarnessProjectionsCommand` only
republishes the cached projection and has no native-read switch. The restore
path is the sole holder of the native-observation capability: composition mints
a `RuntimeFactsGrant` from the daemon-owned `RuntimeFactsAuthority` and sends
`ObserveHarnessRuntimeFactsCommand`; the harness owner validates the grant's
provenance and rejects anything it did not mint with
`HARNESS_RUNTIME_OBSERVATION_UNAUTHORIZED`. On acceptance it joins ProcessOwner
facts on the existing I/O lane, then validates the captured fleet, incarnation,
generation and process token in the harness mailbox before publishing. It
performs no session-ref persistence and can never persist by construction. If the completed legacy operation
has a currently running runtime, no operation is submitted or replayed. If no
runtime is running, a new `resume-wake:` RESUME can be submitted. Lookup failure,
receipt ID mismatch, failed observation, or running runtime with a non-completed
legacy outcome retains the disposition and submits nothing. Positive absence
of the legacy receipt follows the ordinary RESUME path. Its native admission
still consumes the entity token at the owner; liveness observation is not a lease.

After retries are exhausted, newer facts published by the authority can still
be consumed; stale or unavailable facts never unlock. Automatic native restart
also consumes the current incarnation's pure-memory custody projection and
fails closed on unknown or query exceptions, independently of the legacy
fail-open activity-policy callback. Explicit starts continue through the
hydration and atomic owner-held admission paths.
