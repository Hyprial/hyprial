# Conditional adapter bindings

`adapter.pin` and `adapter.unpin` optionally accept `expectedActor`.
`adapter.pins` advertises `supportsExpectedActor: true` only on daemons that
enforce this condition. GUI callers must require this explicit capability
before writing: older daemons ignore unknown parameters, so merely sending
`expectedActor` does not protect a write to an older daemon.
Omitting the field preserves the existing unconditional CLI operation.
An explicit `null` requires an unbound adapter; a canonical agent URI requires
that exact current binding. Other values produce `INVALID_ARGUMENT`.

The comparison and write share one SQLite `BEGIN IMMEDIATE` transaction,
including across processes. A mismatch produces `PIN_STATE_CONFLICT` with
`adapter`, `expectedActor`, and `currentActor`; neither the pin nor staged
legacy state is changed. The condition is checked even for a repeated target.
The existing one-to-one `PIN_CONFLICT` remains distinct.

A GUI adapter must require this field and obtain it from a fresh authoritative
`adapter.pins` read. Present the old and proposed Agent before replacing a
binding, and submit the observed old URI on confirmation. A conflict requires
a fresh read and confirmation. This is a compare-current-value condition,
not a monotonic revision: an intervening change back to the same value is
indistinguishable. Adapter lifecycle versions are unrelated.

After a lost response, read the authoritative binding before deciding the
outcome; do not automatically replay a stale confirmed move.

Verification: `uv run pytest tests/identity/agents/registry/pins tests/daemon/application/adapters/test_daemon_adapter_pin.py`.
