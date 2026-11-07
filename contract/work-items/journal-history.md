# Durable work-item history and repair references

Work-item validity is replayed from the existing OrgFS document journal. The
process-local facade history is neither a source of historical identities nor
evidence that a document was created. Reads never backfill journal rows or
repair items automatically.

The retained chain must be nonempty and contain its creation record. A first
writer sequence of zero alone is insufficient: both the final record's state
vector and the state vector of the actual replayed document must equal the
durable document frontier. The replayed text must also match the inspected
snapshot. Missing causal history, mismatched heads and missing creation remain
`WORK_ITEM_INVALID` with `historyIncomplete: true`. Only a committed purge
retirement permits an "after purge" reason; unreadable journals are reported
as unavailable.

Each retained row has an opaque reference with the representation
`journal:<physicalDocId>:<writer>:<sequence>`. The document, writer and sequence
are the durable commit key. References resolve against that document's retained
journal; a different or nonexistent row cannot substitute for the actual
invalid head or latest validating row.

`work.repair` writes `invalidJournalRef` and `restoredJournalRef` in its repair
owner action and local verification ledger. Its log line has the form
`- <timestamp> <principal>: repair <invalidJournalRef> -> <restoredJournalRef>`
with the existing optional unverified-operator suffix. Exact repair recognition
uses these references and the exact restored content, including its owner
actions and log. The invalid reference identifies the most recent invalid row;
the restored reference identifies the most recent validating row.

The returned `version` and write `expectVersion` remain the current facade
snapshot token. They are used only for present-state comparison and stale-write
retry. They are not durable history references. The local ledger preserves
`producedVersion` as that diagnostic snapshot token and adds `producedJournalRef`
for the actual committed action row. Verification matches the durable reference
along with space, item, action and caller. If the committed action cannot be
matched to the current journal head, no local proof is minted; the landed write
remains readable and its action is unverified.

Compatibility is read-only. Parsers retain legacy repair owner actions with
`invalidVersion` / `restoredVersion`, and old ledger rows remain readable. Those
process-local aliases are not reinterpreted as journal references and do not
authorize exact repair or local verification. A legacy repair chain therefore
requires an explicit owner-authorized `work.repair` from a retained validating
row to regain validity. Missing retained evidence still refuses repair. There
is no automatic migration, relabeling, bulk repair or rewrite of old rows.

The two R7 assertions that required repair logs and ledger references to equal
facade tokens are updated to require the corresponding durable row references.
Keeping their former expectations would preserve the restart defect: facade
revision/clock aliases are not persisted or resolvable after hydration.
