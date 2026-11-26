# Local model binding storage, runtime admission and configuration

The authority is the existing desired-state SQLite database. Stage 4a composes
persisted Pi connection bindings into the common managed launch path. Stage 4b
adds three MethodSpecs projected through the existing IPC/HTTP transport and CLI;
it adds no separate transport or storage authority. GUI integration is later.

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
existing unbound native Auth behavior is unchanged. Native-auth selection belongs
to stage 4c. Public configure/status/cancel
coordination for Pi connections is described below.

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

## Public configuration (stage 4b)

`agent.model.configure`, `agent.model.status` and `agent.model.cancel` require the
trusted local operator and an owned local agent. `name` resolves to its immutable
incarnation. Optional `member` defaults to the agent URI; the actor-name alias is
normalized to that URI. A supplied member must be a harness member in that Cell.
All three have module-level MethodSpecs, closed params/results/errors and HTTP POST
projections. There is no worker permission or caller-provided trusted identity.

Configure requires `operationId`, `expectedBindingRevision` (zero for an initial
binding), `expectedCellRevision` and `configuration`. Connection configuration uses
`authKind=connection`, `harness=pi`, `mode=headless`, an exact manually supplied
`model`, `connectionRef` and `configRevision`. Native configuration uses
`authKind=native`, `nativeAuthRef`, an explicit pi/claude/codex harness, headless
mode and a manual model ID; it has no connectionRef/configRevision. The server
derives intentDigest. Other declared harness/mode
combinations refuse `MODEL_COMBINATION_UNSUPPORTED`; no defaults or alternate Key
are substituted. Secret/grant authorization precedes preparation and is repeated
when a pending operation resumes. Saving does not start a process or call a model.

Preparation atomically stores the candidate and the original Cell member's kind/model
(including absent keys). Its immutable coordination plan contains schemaVersion=1,
cellId, expectedRevision and previousIntent. Digest version=2 includes that plan.
Existing uncoordinated operations keep digest version=1 and remain readable; public
cancel refuses them with `coordination-unavailable` because compensation is unproven.
SQLite candidate_json uses a `hyprial-model-operation/v2` envelope containing
configuration and coordination. JSON operations include the closed coordination
object. Readers from stage 4a reject these new operation shapes instead of ignoring
the recovery data; no automatic downgrade is available after the first such write.

Configure performs prepare → Cell CAS → proof read → applied checkpoint → finalize.
Cell writes use the existing Cell editing service with the original incarnation ID.
The global persistence fence never spans Cell I/O. Per-Cell coordination serializes
this daemon's configure/cancel requests; external writers are checked by Cell CAS.
A failed/unknown Cell effect leaves prepared durable and launch blocked. A retry
with the same ID, digest and expected revisions resumes; a different digest conflicts.
The operation's terminal receipt is immutable even after a later configuration.

Cancel requires operationId and expectedBindingRevision. It first persists the
cancelling choice, then restores only kind/model while retaining other Cell changes.
Unknown results or conflicting member intent keep the blocker. A proof read permits
the compensated checkpoint and atomic cancelled receipt. A crash between compensation
and its checkpoint can be retried without another Cell write. Configure cannot finalize
an operation already cancelling; a finalized operation cannot be cancelled.

Status is read-only: current binding and the requested/pending operation receipt are
distinct. Cell revision and intentMatches are null when observation is unavailable.
`runtime=not-verified` never claims a running process. cleanupPending is observable;
it refuses new configure/cancel and start across every member of that incarnation.
No operation response contains Key bytes. Transport deadlines do not cancel durable
configuration writes; after a lost response, inspect status or explicitly retry the
same operation ID and payload, never a newly generated ID.

`hyprial config set modelBinding.operationLimit 20000` writes the existing private
settings.json atomically and offline, preserving other settings. Default=10000;
valid range=10000..2147483647. It affects the next new operation. Invalid/unreadable
settings refuse new IDs; replay/cancel/finalize of existing operations remain usable.
The offline writer has a single exact import exception in both CLI architecture
gates; the shrink-only baselines are unchanged. Its count, path and public symbol
are pinned, and extra imports remain violations. No configure/status/cancel business
operation is exempted from daemon requests.
Capacity refusal maps to HTTP 409 with bind/operation-capacity. Additional closed
diagnostics include bind/cell-revision-mismatch, cell-intent-mismatch,
agent-incarnation-changed, state-unreadable and cell-outcome-unknown; see MethodSpec
for the authoritative per-code reason lists.


## Native target and explicit creation

`agent.provider.auth.list` retains its legacy result when targetsVersion is absent.
Opting into targetsVersion=1 adds metadata-only targets to each row, each containing
nativeAuthRef, target and kind. References have the exact form
`native:v1:<harness>:<oauth|api-key>:<target>` and identify a slot inside the selected
Agent incarnation, not an account fingerprint or authority shared across Agents.
OAuth refresh preserves the slot; logout makes it unavailable; an explicit login
can repopulate it. Unreadable/ambiguous stores and command/env-derived Keys refuse.
Claude selects anthropic/oauth; Codex selects openai/oauth or openai/api-key; Pi
selects an explicitly listed target. Login inventory is not an upstream test.

Every new native binding is reauthorized before configure and launch. Competing
model credential grants and routed grants refuse; unrelated tool secrets remain.
No native launch args or interactive mode are accepted in this slice. Pi native
bindings require SDK 1.2.0: its physical model must match the exact target and ID;
unknown IDs refuse without aliases or fallback. Native getAuth rechecks the slot
and prevents ambient environment/API-Key overrides. The receipt and reservation
rules apply equally to native and custom bindings. Real vendor calls, other Pi
native SDK versions and custom TUI are not established by isolated process tests.
Claude/Codex explicit native bindings refuse a nonempty session_ref with
MODEL_RESUME_MISMATCH (resume/binding-changed): this slice has no equivalent
immutable session receipt for them. Use a new session; legacy unbound resumes are
unchanged. This is an intentional fail-closed capability boundary.

`agent.create` accepts an optional closed modelConfiguration object containing
operationId and configuration. This operator-only path rejects simultaneous legacy
harness/provider/model/runtime/existing inputs. It creates an identity with the
persistent capability modelConfigurationRequired=true, suppresses captured-Claude
and CLI default seeding, and then uses the same coordinator as agent.model.configure.
Connection creation grants the exact saved field through the existing Secret/grant
authority with an incarnation fence; it never duplicates Key storage. Native creation
does not import another Agent/operator login.

Creation and configuration are separate commits. If configuration fails after
identity creation, MODEL_CONFIGURATION_INCOMPLETE reports bind/configuration-required
with agentId, actor, operationId and a sanitized causeCode. The identity remains
visible and start/restore/reconcile refuse until a binding is finalized. Read status,
complete explicit login/grant authorization and use agent.model.configure for that
identity; do not retry agent.create or silently choose a default. Existing schema-4
binding downgrade refusal remains; the capability-only incomplete identity does not
establish safe operation under an older binary that does not enforce this marker.

CLI create adds --connection/--config-revision or --native-auth, together with
--preferred-harness, --model and --operation-id. Configure accepts the same exclusive
credential selectors. Existing CLI requests without these new flags keep legacy
behavior. Saving configuration never starts the Agent or calls a model.

`agent.secret.provider-test` remains the connection probe: exact endpoint, Key and
manual model ID, with its existing cancellation and timeout contract. It is not
native-login validation or proof of a successful harness launch. No second Auth
probe method is introduced. Native login inventory, connection-probe results and
runtime-start observations remain distinct.

The offline operationLimit setting is nested JSON (`modelBinding.operationLimit`
in CLI notation means {"modelBinding":{"operationLimit":20000}}); a literal dotted
JSON key is invalid and blocks new IDs, without blocking existing replay/cancel.
