# Internal P2 local intent journal (C prerequisite slice)

This daemon-internal journal is not IPC, operator admission, a process launcher,
or a daemon restore implementation. It is stored in the existing shared
StateDatabase, under the P2 single-writer ownership convention. No application
caller is wired by this slice. Later P2 authority commands must call its short
mutations on their existing database effect lane and publish custody/status
facts to the mailbox; long external waits never enter that lane or mailbox.

## Intent and completion

`AdmissionStore.journal.begin(operation_id, step, fingerprint, actor)` reserves
one local step transactionally against the admitted binding. Source accepts only
quiesce; target accepts land followed by activate. It returns immutable
`StepStart(state, execute)`. Only the first successful begin returns execute=true.
Identical replay returns the stored pending/result with execute=false; it is not
permission to repeat filesystem/lifecycle/remote work. Changed actor/fingerprint
rejects. New steps require live admission and the prior completed phase.

A source intent retains the original incarnation; a target land result records
its new incarnation, and activation must retain it. Each generation preserves
its own intent and result. Beginning activation does not overwrite the land
receipt. A single transaction writes each change. There is no distributed or
identity-filesystem atomicity claim.

`finish(operation_id, expected=StepState, receipt, entity_token)` compares the
entire pending record and validates the incarnation before recording completion.
The exact same completion replays; altered receipt/intent rejects. The receipt
string is an internal reference, not a signature or proof by itself. The future
coordinator MUST verify the authoritative local receipt before this call. No
remote ACTIVE receipt, strict-resume proof, or target grant is inferred here.

Expiry/revocation cannot authorize a new effect. An already admitted effect's
verified completion may still be recorded after either; custody remains held.
Unknown outcomes retain pending intent. The current slice has no reconciliation
executor, abort/complete terminal release, or destroy-and-rebuild command.
A new generation here means the next approved local step; rebuilding a target
requires the later fenced recovery contract and must not reuse these results.

## Custody and audit

`custody(actor, expected_entity_token)` returns a read-only source custody fact.
Absent matching records returns unblocked; read/corrupt/binding failures raise,
never return unblocked. A replacement incarnation is not suppressed by an old
record. This is not wired to restore/dormant wake in this slice. Lifecycle owner
must confirm that seam, including per-entity delayed startup/alert/bounded retry
on read failure and terminal suppression retirement.

`cancel_prepared` now rejects if any step intent exists, including revoked,
attention-marked or corrupt step records. Revoke then cancel cannot free a source
that may already have stopped or shipped data. Only preparation without effects
can still release custody. There is deliberately no post-intent unlock API yet.

`attention` atomically records event ID, principal, action, timestamp and
operation/generation, and marks the step needs_attention without deleting
custody or performing an override. Replayed event IDs require identical content.
The caller must already have authenticated/authorized the explicit override;
plain strings do not authorize it. The production alert sink/manual command is
not wired. Until reconciliation is implemented, attention blocks advancement
and ordinary completion, retaining uncertainty rather than falsely reporting
safe release.

## Schema and failure scope

New binding JSON explicitly records _schema_version=1; original A encodings
without that field remain version 1. Malformed/unknown schema returns internal
corrupt-record, not unknown-operation and not a new empty record. The new journal
has version 1 rows. New internal reasons phase-conflict/corrupt-record are not
published kernel wire error codes; future IPC mapping requires contract work.

## Evidence boundary

Process tests use separate real Python processes, SQLite, pipe barriers and
SIGKILL at synthetic effect boundaries. They test persistent intent, exclusive
custody, retained land state and audit, not actual worker liveness or daemon
startup suppression. No sleeping to manufacture a race, no external network,
no model authentication, and no production state. TC013/014/015 protocol
prerequisites are exercised; their real daemon acceptance remains NOT_RUN.
TC012 remains BLOCKED on physical isolation and dedicated model credentials.
The formal orchestrator P2 rejection remains in place.
