# Daemon death path

The home lease remains fail-closed on ownership-record loss or renewal I/O
failure. This change does not enable TTL-only self-fencing in production.

## Fence diagnostics

`homeLease.fenceReason` in `daemon.stopping` and the home-lease status projection
retains `ownership-record-lost` as its prefix. File failures append a colon and
one diagnostic token:

- `OSError[ENOSPC]` for a full disk on heartbeat write;
- `OSError[EIO]` for a failed record read;
- `record-absent` when no usable record is present;
- `record-mismatch` when a record belongs to another owner.

Other failures use the exception class name, with a symbolic errno (or its
numeric value when unknown) when available. Exception messages and paths are
not included. Invalid JSON, invalid text, and non-object JSON remain unusable
records (`record-absent`), as before; they are not claims about file absence.

An authority configured with a logger emits one `warn` event,
`daemon.home_lease.fenced`, on its first fence. The daemon wires this logger.
Fields are `fenceReason`, `generation`, and `heartbeatAgeSeconds` (number or
null). A logger exception must not prevent the ownership-lost stop callback.
Existing reasons such as `heartbeat-deadline` retain their meaning.

## Exit guarantee

Only a daemon that owns process exit arms the exit backstop. Before `_close()`,
`run()` arms `TEARDOWN_BUDGETED_SECONDS`. After close (including a raised close),
it replaces that pending deadline with `DAEMON_EXIT_BACKSTOP_SECONDS` before
final cleanup. A replacement cancels the pending timer; an expiry already
committed to exit cannot be withdrawn. Embedded daemons never force host exit.

On expiry the existing `daemon.exit.forced` record reports `afterSeconds` as the
active timer's budget. The process exits with code 75, bypassing interpreter
thread joins. This now covers a blocked close as well as a blocked final join.
A clean exit does not wait for the timer. This is a forced-exit bound, not a
promise that unbudgeted close operations can complete inside the window.

## Executable coverage

- `tests/daemon/home/test_home_lease_failure_diagnostics.py`
- `tests/daemon/application/supervision/test_daemon_exit_backstop.py`
- `tests/tooling/isolation/test_exit_independence.py`

These use disposable state, injected file errors, and owned child processes.
They do not claim live disk-exhaustion or production restart acceptance.
