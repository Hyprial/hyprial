# Local model binding storage (implementation stage 3)

The authority is the existing desired-state SQLite database. This stage adds no
MethodSpec, daemon request, IPC wire method, HTTP route, settings writer or GUI.
Production admission for new model bindings remains closed until stage 4.

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

## Transaction boundaries

All conditions and mutations use one existing StateDatabase transaction. Prepare
checks cleanup → same operation digest/receipt → other prepared → expected revision
→ new-ID capacity. A partial unique index enforces one prepared per binding.
Finalize/cancel commit binding and terminal receipt together. An agent cleanup
marker blocks every member's prepare/finalize. Marker takeover, rollback and purge
compare the exact incarnation and attempt; purge deletes the three tables' entries
for that agent in one transaction. A different incarnation is untouched.

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
Transport mapping and Cell/destroy recovery are stage 4, not delivered here.
