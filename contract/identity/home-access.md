# Identity-owned home access

`AgentActor.prepare_runtime_context` is an internal identity port, not a new
IPC method or a cross-host migration entry point. Callers execute it outside
an actor mailbox, for example on the P2 coordinator's EffectLane.

## Runtime incarnation admission

The optional keyword `expected_entity_token` binds preparation to the caller's
intended incarnation. Identity compares it with the current Agent when resolving
runtime preparation on its owner thread, before choosing the legacy branch or
submitting filesystem work. A mismatch raises the existing `AgentEntityConflict`
(`AGENT_VERSION_CONFLICT`); the request neither prepares the replacement's home
nor falls back to a legacy launch. An empty token is a mismatch, not a wildcard.

Omitting the keyword, or passing `None`, retains existing behavior. The check
does not replace filesystem receipt validation or the existing runtime launch
fence: a successful preparation still carries the home incarnation and resource
receipt through completion and launch. Caller-side reads of daemon projections
are not a substitute for this identity-owner admission check.

This port does not grant credentials, start a worker, or authorize source
cleanup. Its tests are component evidence for migration, not a claim that the
full P2 migration or physical two-machine acceptance has passed.

## In-process wait and receipt retention

`wait_home_access(operation_id, *, timeout)` only observes an existing call.
It does not enqueue work. A pending timeout raises `AgentHomeAccessPending`
without cancelling the accepted effect. A retained result returns its original
value (legacy runtime decisions return public `None`) or raises its original
failure. An unknown call raises a typed home-access error with disposition
`unknown-operation`; this does not establish that the filesystem work never ran.

Mailbox admission rejection (overloaded/closed), or the actor runtime's
never-begun/undelivered notification, releases the in-memory reservation without
retaining a completion. Every already-attached caller is awakened with that
rejection; a subsequent query is unknown, and an explicit submission can reserve
the ID anew. This is not cancellation of an accepted running filesystem effect,
and does not automatically retry work or bypass P2's durable recovery checks.

The internal call registry permits one submission for a retained operation id
and immutable input fingerprint. Concurrent callers attach to the same waiter;
a different actor, phase, entity token, payload content/metadata, staging path,
or runtime input is rejected with disposition `operation-fingerprint-mismatch`.

Pending custody is capped at 32 calls and never expires. At most 128 settled
results are retained, for 300 seconds from completion. Reads do not extend age;
eviction is lazy on registry access. Existing waiter references remain valid
after cache eviction. Limits are declared in `home-access-bounds.json` and
checked against the implementation defaults. The historical supervision
manifest is byte-for-byte unchanged, including its frozen metadata. There
is no new timer, thread, persistent identity store, or automatic retry policy.

## Stable submission and recovery responsibility

`prepare_runtime_context`, `materialise_landing_home` and `confirm_landing_home`
accept optional `operation_id`. Omission/None keeps uuid4 behavior; an explicit
blank or non-string ID is rejected before admission. Materialisation snapshots
the caller's file sequence into an immutable tuple before fingerprinting and
mailbox submission. The identity public surface exports `AgentHomeAccessError`,
`AgentHomeAccessPending`, `HomeOperationUnknown` and `HomeOperationConflict` for
coordinator-side handling; these are internal exceptions, not new wire codes.

The identity port owner confirmed on 2026-10-09: unknown after eviction/restart
is the wait + P2 recovery protocol's responsibility, not permanent deduplication
on the submission API. P2 first persists intent in its shared StateDatabase,
then submits. After a timeout it waits, without re-submitting pending effects.
Unknown requires reading the durable Agent row, home receipt, matched payload
and grant IDs; memory state or mere home existence is never proof of completion.
Only when durable authority establishes absent may P2 explicitly submit again,
including the same deterministic ID as **new submission**. Fingerprint conflicts
are detectable only while the original entry is pending or retained in cache.
The first batch adds no identity tombstone, ticket or new persistent authority.

Partial landing is settled against the **original expected entity token**, then
re-landed with a new sub-operation generation. A different incarnation must not
be destroyed, overwritten or replaced: report mismatch to P2 instead. Cached
results are historical operation results, not fresh authorization to launch or
clean up; consumers still apply incarnation/receipt fencing. No P2 recovery
coordinator is connected here, and no cache miss authorizes source restoration.
