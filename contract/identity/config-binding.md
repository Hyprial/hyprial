# Identity-owned atomic config binding

This is an internal identity port, not a new IPC method or an authorization
grant. A daemon coordinator calls `domains.agents.bind_agent_config` from its
effect lane; the facade submits `BindAgentConfigCommand` to the identity owner.
It does not enable P2 cross-host transfer by itself.

Inputs are actor, `expected_entity_token`, config, and
`expected_current_config_digest`. Both expected values are required. The write
changes only config, never another agent field, and compares both fences in the
same SQLite write transaction. The transaction covers separate connections as
well as commands serialized by the identity mailbox.

`agent_config_digest` normalizes a config to `AgentConfig.to_json()` (or null
for no config), serializes JSON with sorted keys, ASCII escaping and compact
separators, prefixes bytes `hyprial-agent-config-v1` followed by one NUL, and
returns the lowercase SHA-256 hex digest. This identifies the config **value**,
not referenced files, a manifest, or the global projection version. Absence
has the digest of prefixed JSON null; an empty string is not a wildcard.

Rejections preserve the entire current record:

| Condition | Code |
| --- | --- |
| Different current incarnation | `AGENT_INCARNATION_MISMATCH` |
| Same incarnation, different current config value | `AGENT_CONFIG_CAS_MISMATCH` |
| Agent absent | Existing `AGENT_NOT_FOUND` |
| Missing/invalid expected fence | Existing `INVALID_ARGUMENT` |

Codes in this table describe the actor boundary. Direct registry calls raise
`ValueError` for malformed fences; the actor maps that to `INVALID_ARGUMENT`.

A caller must stop and report either conflict, without overwriting config or
destroying the current incarnation. This port performs no compensation. It
does not infer success because the desired config already matches: replay
with an obsolete expected digest still conflicts. A caller that has read the
current matching digest may issue a no-op binding.

Executable controls live in
`tests/identity/agents/registry/config_binding/test_config_binding.py` and
cover actor/facade completion, persisted state, separate-connection races,
admin writes between read and commit, both fences, and preservation of fields.

## Supported P2 config-root rebind

`hyprial agent config rebind <name> --source <absolute-private-directory>` is
the operator entry for repairing an existing P2 config root. `--dry-run`
performs the same quiescence and source validation without writing a receipt,
publishing files, or changing the binding. The corresponding compensation is
`hyprial agent config rebind <name> --rollback <receipt-id>`; rollback does not
accept `--dry-run`.

A successful JSON result has `ok`, canonical actor `name`, `dryRun`, `state`,
`receiptId`, `beforeHash`, `afterHash`, `beforeConfigDigest`,
`afterConfigDigest`, `source`, `target`, and the receipt object. Dry-run also
has `validation`; both the top-level and nested `receiptId` are null so its
candidate receipt cannot be mistaken for durable rollback authority. A
non-dry-run receipt ID names the private durable transaction directory.

New receipts and results also carry `projectionChanges`, a read-only comparison
of the current source revision's existing native projections with the selected
staging inputs. Rows contain `harness`, relative `path`, `changes` (a list), and
`action`. Changes are `unexpected`, `missing`, `content`, `mode`, `privacy`,
`directory-mode`, or `directory-privacy`. Privacy uses the existing owner/ACL
validator, including on Windows. Actions are `omit-from-new-projection`, `publish-from-staging`,
or `publish-private-directory`. An uninspectable tree produces a `.` row with
`uninspectable`; an invalid current C produces a `*` harness / `.` path row with
`current-source-invalid`. Both use `publish-from-staging`, not a claim of a
complete comparison. File bodies are never included. Older receipts return an
empty list, which does not certify their old projection's integrity. Resume and
rollback responses echo the original forward observation, not a live comparison
or a reverse-action plan.

The comparison never authorizes a write or weakens validation. Apply records
its own observation under the existing quiescence fence; dry-run does not
reserve that observation. Reusing the current private C as `--source` explicitly
republishes C under a new path and binds it. The next start derives a new native
projection; the old corrupt projection is retained untouched. Missing or invalid
staging C is still refused, and start/ensure-home never invoke rebind implicitly.

The CLI only submits `agent.config.rebind` to the daemon. Worker-origin calls
are refused. The daemon routes the request through the identity actor's
migration lease; filesystem work runs on its effect lane, and all identity or
home reads and binding CAS writes return through the actor owner. The handler
must not reacquire the process-wide identity transaction lock already held by
the running daemon.

The source must be an absolute, real directory owned by the daemon uid. Every
directory has mode `0700`; every regular file has mode `0600`. Symlinks,
special files, NUL-bearing or non-UTF-8 files, credential-bearing native
paths, secret-like paths/content, and files outside the native/shared P2
mapping are rejected.
Validation freezes one `AgentConfig` manifest and builds the native projection
for every supported harness. The receipt records separate before/after tree
SHA-256 snapshots because `agent_config_digest` identifies only normalized
binding values.

Publication copies and fsyncs complete before a same-parent rename publishes
an immutable revision under the agent home. Receipt and revision parents must
be real, daemon-owned mode-`0700` directories; linked or drifted storage fails
closed. The durable receipt fences actor,
entity token, home resource token, before/after config digests, exact tree
entries/modes/hashes, and the source and published paths. Its forward phases
are `prepared`, `published`, and `complete`; rollback additionally uses
`rolling-back`, `binding-restored`, and `rolled-back`. Repeating the same
request resumes an incomplete phase. A response lost after `complete` returns
the matching completed receipt. Rollback restores the prior binding by CAS,
then atomically renames the matching after-image into its transaction. For a
`prepared` receipt, that after-image may still be the durable staged `new` tree
or may already be the published target if the publish rename won a crash race.
The receipt remains `binding-restored` until idempotent cleanup of the retired
tree finishes, so a crash during recursive deletion resumes without reporting a
completed rollback or accepting a partial target as a valid after-image. Only
then does the receipt become `rolled-back`. Receipt, incarnation, source,
target, or CAS drift fails closed rather than overwriting unrelated state.

Every forward and rollback phase requires a quiescence proof from the identity
owner. The service rechecks after publication and immediately before each
binding CAS; rollback also rechecks before atomically retiring the published
tree. Quiescence is false when durable headless or interactive desired state,
pending session effects, any retained runtime binding, runtime launch custody,
or a destroy reservation exists; an unavailable desired-state authority is
also false. This does not use the informational `ps.connectors` projection. The
migration lease blocks conflicting launch, home, pin, destroy, and config
commands until the operation settles. `AGENT_RUNTIME_CONTEXT_STALE` still
fences lifecycle contexts: every issued runtime context is bound to the config
digest current when it is offered, so a context that was not acquired before a
successful rebind cannot launch the old C afterward.

A check followed by a separate CAS is not a proof. The binding mutation is one
owner step: the identity owner reserves the durable desired-state authority,
re-proves quiescence against the durable document (not a projection a blocked
writer has not published), and commits the guarded binding CAS before it
releases the reservation. A desired-state commit therefore either wins before
the proof -- which then fails with `AGENT_CONFIG_REBIND_NOT_QUIESCENT` -- or
lands after the CAS that the operator's proof covered. No second desired-state
store, lease file, or cross-process lock is introduced.

Rollback cleanup is exact rather than best-effort. The receipt records whether
this transaction created the revision parent (`revisionParentCreated`), and it
records that intent **before** the directory is created: an interruption in
that window cannot lose the attribution, so a resumed run still finishes the
same job and rollback removes the empty parent this transaction made. A
pre-existing `config-revisions` directory is preserved with its mode, and a
recorded pre-existing parent that has disappeared fails closed rather than
being recreated. A receipt written without the field can only preserve the
directory. Only the
expected "another revision still lives here" outcome is tolerated when that
removal is attempted; every other cleanup failure surfaces as
`AGENT_CONFIG_REBIND_CLEANUP_FAILED` while the receipt stays at
`binding-restored`, so the rollback is resumable and is never reported as
`rolled-back` before cleanup actually finished.
Rebind changes only `Agent.config`; actor URI, incarnation, home receipt,
routine state, and all unrelated fields remain unchanged.

The IPC error surface includes the existing incarnation/CAS codes and
`AGENT_CONFIG_REBIND_INVALID_SOURCE`, `AGENT_CONFIG_REBIND_INVALID_MAPPING`,
`AGENT_CONFIG_REBIND_SYMLINK`, `AGENT_CONFIG_REBIND_OWNERSHIP`,
`AGENT_CONFIG_REBIND_MODE`, `AGENT_CONFIG_REBIND_SECRET_CONTENT`,
`AGENT_CONFIG_REBIND_IN_PROGRESS`, `AGENT_CONFIG_REBIND_NO_BINDING`,
`AGENT_CONFIG_REBIND_SOURCE_DRIFT`, `AGENT_CONFIG_REBIND_RECEIPT_INVALID`,
`AGENT_CONFIG_REBIND_TARGET_DRIFT`, `AGENT_CONFIG_REBIND_NOT_QUIESCENT`, and
`AGENT_CONFIG_REBIND_CLEANUP_FAILED`.
Real daemon/headless, validation, crash-resume, rollback, stale-receipt, and CAS
controls live in `tests/integration/profiles/homes/test_p2_config_rebind.py`.
