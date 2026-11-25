# Org membership records (post-OC4 / J10 / J21 / J22)

Shapes and rules for the membership storage foundation after personal-org
removal. Frozen historical fixtures:
`contract/fixtures/org-membership/records.json` and `admission.json`.
Rulings: `CONTRACT-OC0-v4.md` §5, §8.1 (J10, J21, J22).

## Work-only organizations and local owner storage

- Only `work` organizations (`group-<slug>`) exist. A node with zero joined
  organizations has a legal empty `org.list` (`orgs: []`) and no synthetic
  default or personal organization is created.
- Every agent may belong to zero or more work organizations; leaving the final
  organization is valid and leaves the agent unassigned.
- Owner-scoped protected records (model credentials, grants, and owner-store
  documents) live exclusively on the local machine under `<HYPRIAL_HOME>/state/`
  with private `0700` directories and `0600` files, never in OrgFS.

## Agent membership

- Path `agents/<owner>/<agentId>.json`. `agentId` is the agent's immutable
  id (`a.` + 26 lowercase base32 characters, kernel `_AGENT_ID_SEGMENT`,
  derived by the identity registry `Agent.agent_id`), never its URI or a
  mutable name: a move or rename keeps it, a same-name rebuild gets a new one
  and inherits nothing (R9).
- Record: `schemaVersion`, `agent`, `owner`, `agentId`, `role` (`member`),
  `state` (`requested` | `approved` | `removed`), `addedBy`, `addedAt`, plus
  `removedBy`/`removedAt` for a tombstone. Optional migration `origin` is
  `migrated-from-context` or `migrated-personal`; it does not grant write authority.
  Old records without `origin` remain valid. Other unknown fields are refused.
- The row's `agent` URI must name its `owner`; a mismatch is refused, not
  normalized.
- Writes are compare-and-set on the document version. Deletion is a tombstone;
  an `approved` row is a membership, everything else is inert.
- Projections have three outcomes: `ok` with the active rows, or `unavailable`
  with a reason. An unreadable space is never reported as an empty `ok`
  projection, so a caller cannot write "no members" over an unknown state.

## Receivers (removed, S3a 2026-11-24)

`receivers.json`, `org.receivers.get` and `org.receivers.set` are deleted. The
path `receivers.json` is an ordinary OrgFS document path with no protected
section; no daemon code reads an old one.

## Admission

Agent membership records are protected partitions in work orgs: the replicated
document id encodes the space, the section and the path principal, so every
replica decides from `(doc id, author)` alone, never from the body.

| section | work org |
|---|---|
| `agents` | author must be the space owner; the agent's owner alone is refused (no self-approval, R2) |

## Current organization and work defaults (O2a)

- `org.use {org}` and `org.use {clear:true}` are mutually exclusive. Only an
  active organization can be selected. The daemon atomically writes
  `state/org/current.json` under its own home; it never writes this selection
  to OrgFS or changes membership, admission, or delivery policy.
- `org.resolve {org?, spaceId?}` selects explicit org, then current org, then
  the unique active org. No candidate or several candidates returns
  `ORG_REQUIRED` with sorted `data.candidates`.
  Unknown, malformed, or inactive explicit targets return `INVALID_ARGUMENT`.
  0.5.5.3 is observe-only (design #2078): a `spaceId` not governed by an
  active organization passes through unchanged with an observe notice and a
  null `org` instead of being refused; enforcement moves to 0.5.5.4.
- Candidates have a valid org name, a nonempty space ID, and state `active`.
  Left, deleted, creating, partial, sync-problem and legacy-active rows cannot
  be selected. On the next context/work request an unavailable current value
  is durably cleared before fallback, even with an explicit target.
  Results and domain errors carry `notices` (errors under `data`); the CLI
  renders them on stderr, including in JSON mode. An unreadable local settings
  file returns `DAEMON_NOT_READY` without selecting a fallback.
- `org.list` emits `kind=work`, `current` per row, and top-level
  `notices`; it retains `orgs`, `leftOrgs` and the nullable `authorityDevice`
  on every row. An empty `orgs: []` list is legal and normal. It remains a
  read-only HTTP method. An unavailable current selection is shown as
  unselected with a notice without changing local settings; a subsequent
  context command or work shortcut calls `org.resolve` to perform the durable
  repair.
- A change logs `org.current.changed` with `previous`, `current` and
  `reason=explicit|unavailable`. Setting the same value produces no change.
- Work documents now follow [ordinary OrgFS](../work-items/README.md).
  All `work.*` MethodSpecs are removed. The CLI retains only `org work ls`
  and `org work show <filename>`: they call `org.resolve` followed by
  `orgfs.ls` on `work` or `orgfs.read` on `work/<filename>`. These return
  ordinary OrgFS results, not work-item heads or approval classifications.
  A client-side organization selection is not authorization; ordinary OrgFS
  permissions and request identity rules still apply.
- Unknown explicit targets use `INVALID_ARGUMENT`; ambiguous selection uses
  `ORG_REQUIRED`. OrgFS errors retain their own structured details and frozen
  vocabulary. No work-specific validation or repair error is introduced by
  these shortcuts.
- `org.use` and `org.resolve` remain local (`http_exposed=false`) and are marked
  mutating because stale-current cleanup can write local state, with
  conservative `retry=never`. The subsequent `orgfs.ls/read` operations are
  ordinary reads and do not mutate the work documents.
- Overview resolves the org through `org.resolve` and reads current OrgFS text.
  Association collect/finish and their asked/declined state machine are retired.
  Work content, including unknown fields and old owner-action entries, is not
  migrated or deleted, and a local ledger is not an authority for that content.
- Frozen examples: `contract/fixtures/org-membership/defaults.json`.

## Not closed by this slice

- Live multi-device Casdoor binding and recovery require separate environment
  acceptance; isolated binding/recovery tests do not establish that result.
- Migration (`org-membership-v1`) is specified in [migration.md](migration.md).
  Member commands, invitations and admission observe mode belong to O2b/O2c/O3.
