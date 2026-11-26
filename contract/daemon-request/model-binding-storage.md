# Local model binding storage and runtime admission (stages 3 and 4a)

The authority is the existing desired-state SQLite database. This stage adds no
MethodSpec, daemon request, IPC wire method, HTTP route, settings writer or GUI.
Public configuration remains unavailable until stage 4b. Stage 4a composes
internally persisted Pi connection bindings into the common managed launch path.

`DesiredStateStore.model_binding_store` provides prepare/finalize/cancel, status,
agent cleanup marking/takeover/rollback and destroyed-agent purge. The caller must
derive `agent_id` from `Agent.agent_id` (immutable incarnation), resolve the stable
Cell member ID, authorize the operation, and confirm external Cell/identity effects.
The storage API never resolves display names, reads a Key or grants permissions.

## Records and versions

First prepare or cleanup marking atomically promotes the root to schema 4 with the
new row. Legacy stores remain schema 3. Schema 4 survives cancellation and empty
purge; unknown versions and unknown binding fields are rejected. JSON projection
requires three arrays: `modelBindings`, `modelBindingOperations`,
`modelAgentLifecycle`. Existing fields retain their meanings. New configuration
fields are references plus exact model/Harness/mode/member-intent digest; no secrets.

Bindings have `(agentId, memberId)`, a stable `bindingRef`, `bindingRevision`,
`configuration`, `operationId` and `phase=active`. Configuration contains authKind
`connection` with connectionRef/configRevision, or `native` with nativeAuthRef;
both contain model, harness, mode and intentDigest. Model IDs are not normalized.

Operations retain `(agentId, memberId, operationId)`, requestDigest, expectedRevision,
bindingRef, candidate, phase and resultRevision. The digest is SHA-256 of UTF-8
canonical JSON (sorted keys, compact separators, Unicode preserved) containing
version=1, agentId, memberId, expectedRevision and configuration. Terminal receipts
are immutable; resultRevision describes that operation, not the current binding.
Cleanup records are keyed by agentId, with cleanupPending.entityToken/destroyAttempt.

Successful launch rows may carry `modelBinding={bindingRef,bindingRevision}`. This
is a snapshot reference, not current configuration. It requires root schema 4.
Old schema-3 binaries refuse the whole schema-4 store; enabling it is not a
reversible downgrade. A residual legacy JSON import cannot overwrite schema 4.
Adding the nullable launch column alone leaves a legacy root at schema 3; old
readers ignore that column. It does not repeat historical ghost-row migration.

## Transaction boundaries

All conditions and mutations use one existing StateDatabase transaction. Prepare
checks cleanup → same operation digest/receipt → other prepared → expected revision
→ new-ID capacity. A partial unique index enforces one prepared per binding.
Finalize/cancel commit binding and terminal receipt together. An agent cleanup
marker blocks every member's prepare/finalize/cancel. Marker takeover, rollback and purge
compare the exact incarnation and attempt; purge deletes the three tables' entries
for that agent in one transaction. A different incarnation is untouched.
Every write validates the resulting projection before commit; invalid output rolls
back. Prepared references match an existing binding. Finalized receipts share its
reference and cannot claim a later revision. A cancelled initial attempt at revision
zero may retain a different reference from the binding created by a later attempt.

The default capacity is 10,000 retained operations per binding. A supplied limit
must be an integer from 10,000 to 2,147,483,647; it is trusted local settings input,
not a configure payload field. Invalid capacity blocks only a new ID after earlier
checks. Full capacity does not block existing-ID replay, cancel, finalize or reads.
Receipts have no expiry/eviction. Ordinary whole-document writers must carry exactly
the committed model projection, or fail; they cannot erase or revive operations.

Errors consume existing `hyprial.error.codes`: configuration-invalid (validate /
invalid-connection or operation-limit-setting), conflict (bind / revision-mismatch,
operation-digest-mismatch, operation-already-completed), incomplete (bind /
operation-in-progress, destroy-cleanup-pending), capacity-exceeded (bind /
operation-capacity). Data is a closed classification, never exception/upstream text.
Stage 4a maps incomplete configuration to HTTP 409. The existing `agent.start`
MethodSpec declares its closed phase/reason diagnostics; no new transport is added.

## Runtime admission and destruction (stage 4a)

Application writes use typed commands through the existing StatePersistenceAuthority.
An accepted cleanup write joins its original settlement before releasing the destroy
reservation. The caller cannot time out and leave an unobserved late marker.

Agent start resolves the saved binding. Lifecycle projections, desired-state launch
rows and Cell config digests retain bindingRef/bindingRevision. Restore refuses an
absent/stale snapshot. Cell facts also read the binding before the first successful
launch; the selected member's harness/model must match its recorded intent digest.
The digest includes binding revision, never grant state or Key bytes. Key rotation
and revocation do not become configuration drift or trigger a reconcile restart.
The runtime resolves the existing Secret/grant exactly once per launch, requiring
one matching authorized connection and configRevision, then rechecks binding admission.
No legacy model default or ambient credential fallback is allowed for bound launches.

This increment accepts only Pi/headless connection bindings. New native-auth bindings,
custom TUI and unsupported combinations refuse MODEL_COMBINATION_UNSUPPORTED;
existing unbound native Auth behavior is unchanged. Native-auth selection and the
public configure/status/cancel coordination belong to stage 4b.

Destroy marks cleanupPending by immutable Agent.agent_id before destructive effects,
even with zero bindings; every member's new configure/start is blocked. Failure before
entity deletion can roll back only the same live incarnation and destroy attempt.
After authoritative deletion, purge removes bindings, operation receipts and marker
in one transaction. Recovery retains markers when the registry is unreadable and
purges only proven absent incarnations; a same-name replacement is untouched.
Ordinary stop leaves bindings and receipts intact. Cleanup marking promotes schema 4
even for a legacy agent without bindings, with the downgrade refusal described above.

`agent.destroy.preview` adds modelBindingCleanup counts. The field is optional in
the wire schema for compatibility with older results; this server always emits it.
Confirmation hashes
include non-secret binding/operation state, so intervening configuration invalidates
the preview. Failed destroy and idempotent replay preserve
MODEL_CONFIGURATION_INCOMPLETE / bind / destroy-cleanup-pending and entityDeleted
(true, false, or null when registry outcome is unknown). No Key enters that result.
