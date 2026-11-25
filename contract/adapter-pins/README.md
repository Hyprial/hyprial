# Conditional adapter bindings (retired, I6-4)

**Retired 2026-11-23 (I6-4).** `adapter.pin`, `adapter.pins` and
`adapter.unpin` no longer exist: the pins table, the identity pin commands
and the removal pin custody are deleted, and adapter bindings are now owned
by agent cell documents. This directory is kept only as a pointer.

Replacements:

- Read a binding: `adapter.list` (its `agent` field is resolved from the
  cell adapter-routing index) or `agent.cell.get`.
- Write a binding: `agent.cell.update` (CAS on `expectedRevision`) adding or
  removing the agent cell's `role: "adapter"` member, whose
  `cell.members.<ref>.adapter` names the local adapter configuration; CLI:
  `hyprial agent cell show|apply`.
- Conditional-write semantics: the cell revision CAS (`expectedRevision`)
  replaces `expectedActor`; a stale expectation returns `INVALID_ARGUMENT`
  carrying `expected`/`current`.
- Cross-agent conflict: the same adapter declared by two agent cells is a
  routing conflict and fails closed (`agent: null`), replacing the old
  one-to-one `PIN_CONFLICT` write refusal.

Migration runbook: [docs/guides/cell-authority-migration.md](../../docs/guides/cell-authority-migration.md).
Verification: `uv run pytest tests/daemon/application/adapters/test_daemon_adapter_routing.py`.
