# Synthetic pre-host-invite registry database (pinned at bd6eb960)

Generated with `AgentRegistry` from the clean, read-only
`bd6eb9601ec3ee54b788d71ac7dfa3e0864fea72` source, not the code under test.
`provenance.json` records the registry source hash and fixture hashes.
(Renamed from `host-invite-bd6eb960` when the host-invite feature was
deleted, 2026-11-21; the bytes are unchanged.)

The old build created one native and one transfer-hosted agent, configured
the native agent, recorded sessions, pinned synthetic adapters, and granted
references to synthetic provider entries. No credential values are included.
Two synthetic lifecycle receipts were inserted using the old table schema.
The SQL is that database's `iterdump`; `home.json` contains its home directories
and receipt mirrors. All rows and all fields are compared after the real
DaemonApplication constructor opens the database: they must equal exactly what
the UUID addressing P4 open documents (`after_p4` in
`contract/e2e/run-hosted-registry.py`; id-form `agents.uri`, stamped `agent_id`,
`agentId`/`displayName` on the agent-record payloads, an `id-form` freeze of
each old name), and nothing else may change.

Only `__HOME__` and `__UID__` are replaced to place the receipts in the isolated
test directory under the current test uid. The frozen CHECK has two values and
entity_token is already NOT NULL, so the open only adds the additive columns
and never rewrites the table (the CHECK-upgrade rebuild was deleted with
host-invite).
