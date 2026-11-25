# Org-membership upgrade v1 (O1b, 0.5.5.3)

## Commands and authority

`upgrade.steps.status` and `upgrade.steps.run` are declared local-operator,
unary, non-HTTP MethodSpec methods. The latter accepts optional boolean `apply`
(default true); false returns `INVALID_ARGUMENT`. Unknown parameters are refused.
Both return `steps` and `pending`; status also returns `legacyArgvFiles` and
`legacyProcesses`. CLI upgrade, upgrade status, and `doctor --fix` call the
daemon. They do not open registries, profile files or replicas to run migration.
An unavailable daemon is explicit pending/warning, with no local fallback.

`org-membership-v1` is repeatable even after a done marker. A locked upgrade
runner injects the daemon's live facade, registry and profile ports. The journal
and reports use the explicit daemon `hyprial_home`, not the CLI process home.
No second business authority is created. Existing timer cleanup still receives
the operating-system home separately.

## Inventory and writes

The private journal freezes canonical users, agents and their immutable agent ids,
accepted-context digest/mappings, and a SHA-256 source digest before mutation. An inventory change fails with `source-drift`; it never
silently resets the journal. Legacy profiles, context and published OrgFS files
are not removed or rewritten.

Every known agent needs its owner's personal membership. Work-org residence is
grandfathered only from the accepted context bytes in a space with explicit org
metadata; match residents by unique name+node. Human `members`, publisher, current
org and space owner are not target guesses. Unresolved or ambiguous sources are
pending. Grandfathered rows use `origin=migrated-from-context`; personal rows use
`origin=migrated-personal`. Existing valid rows retain their original metadata.

All writes retain protected-path admission. Only a space owner can create the
corresponding membership. Foreign-owner records remain pending until projected;
they are not written by impersonation. Every represented user needs a personal
space and a visible host row; the current owner's current device must be present.

Memberships use create-only writes. Never overwrite different first-writer
values, inactive rows or tombstones.

S3a (2026-11-24) deleted the receivers routing record with `receivers.json`: its
register document format, the `receiver-first-write-conflict` and
`receiver-awaiting-sync` outcomes and the peer-witness confirmation are gone.
User entities in the inventory carry only the owner. A journal whose every
inventory entity reached `REPORTED` is compared without the retired
`receivers` key, so a node that had finished the step stays `done` on rerun.
A node whose membership step had not finished sees a new inventory digest
(`source-drift`); the manual step is in `docs/guides/migration-remove-squire.md`.

Journal the intended record before writing. After verification, missing/replaced
records or changed content revisions fail instead of re-granting rights.

## Recovery and readback

Entity checkpoints: `INVENTORY`, `PERSONAL_READY`, `MEMBERSHIPS_WRITTEN`,
`VERIFIED`, `REPORTED`. Users and agents take the relevant subset. Checkpoints
are progress, never readiness proof. Membership readback always reads OrgFS
directly, without a profile fallback. Durable fences
include node/document identity and the SHA-256 of the CRDT document update;
facade-local version counters alone are not stable across replica reopen.
The internal `TextReadSnapshot.content_revision` supplies this fence without
changing published OrgFS read response keys.

Every ordinary run writes a mode-0600 JSON report in a mode-0700 directory:
`<hyprial_home>/state/org-membership-v1/report-<id>.json`. A process crash can stop
before report publication; the next invocation resumes from persisted intent.
Report keys: `schemaVersion=1`, `operationId`, `node`, `sourceDigest` (null if
inventory unavailable), `entities`, `created`, `reused`, `conflicts`, `failed`,
`pending`, `unconfirmed`, `state`, `verified`, `reportPath`. Each entity has
`entity`, `sourceDigest`, `phase`, `targets`, `state`, `verified`. Problem entries
have `entity` and `reason`; created/reused entries have `entity` and `target`. A conflict is also a failure.
No secret exception text is copied into reports. `org.migration.report` logs only
operation/node, verified status and failed/pending counts.

The upgrade marker is `done` only for a verified report without pending/conflict/
failed/unconfirmed entries. Conflict takes state precedence, then failure,
unconfirmed, pending, and done. Conflicts are `conflict`; writes awaiting sync
are `applied_unconfirmed`; other unavailable/unverified waiting work is `pending`;
other failures and unexpected step exceptions are `failed`. All non-done states
remain listed in the runner's `pending` step-id array. Doctor includes each
step's state in its detail. A receiver-only `applied_unconfirmed` wait is
informational (`status=ok`, no action prompt), not completion. Conflicts and
other pending/failure conditions remain warnings with an action hint. Local
absence of another holder never authorizes `done`: `soleHolder` was rejected
because a cloned device's publication can still be unseen. The step remains
repeatable in all states. Result reasons are not new RPC error codes. `created`/`reused` classify
journaled records that have been observed, not transactional rollback guarantees.
A later failure does not delete earlier successful writes. Keep the journal when
reconciling drift; removing it would remove the anti-replay fence.

## Evidence boundary

Isolated unit/runtime and durable-reopen checks do not establish live Casdoor,
real-device staged upgrades or installer continuity. Those remain separate
UNKNOWN acceptance items. Administrative approval capability remains BLOCKED
until its separately authorized integration is available.
