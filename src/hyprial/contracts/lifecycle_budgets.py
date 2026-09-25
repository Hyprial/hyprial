"""Lifecycle operation timeouts, shared by the daemon and the CLI.

These live in the contract layer, not in ``hyprial.daemon.lifecycle_manager``,
because the CLI needs them to size its ``hyprial down`` IPC wait and importing the
daemon package from the CLI startup path adds ~1.2 s to every invocation
(guarded by ``test_importing_the_cli_does_not_drag_in_the_daemon_package``).
The composition rule is the point: the three waits are derived from one
deadline, never chosen independently.  Card 104164aa (c).
"""

from __future__ import annotations

#: How long a lifecycle operation may keep re-driving an unresolvable forward
#: step before the manager gives up.  The manager's per-step wait
#: (``completion_timeout``, 2 s) is deliberately short, and a healthy-slow
#: effect (a codex session start measures 9-43 s) exceeds it constantly, so the
#: re-drive *count* cannot tell slow from stuck -- only this operation-level
#: budget can.  On expiry the operation is put to the terminal ``FAILED`` state
#: (never ``COMPENSATING``: compensating a stop that cannot complete just hangs
#: the same way in reverse), which stops the re-drive and lets ``hyprial down``
#: return a coded failure instead of an IPC timeout.
LIFECYCLE_OPERATION_DEADLINE_SECONDS = 70.0

#: The daemon-side wait for an operation to settle
#: (``_run_lifecycle_operation``) must OUTLAST the operation deadline so it
#: receives the terminal ``FAILED`` the manager writes, rather than timing out
#: itself; the CLI's IPC wait must in turn outlast the daemon-side wait so the
#: caller receives that coded failure instead of an IPC timeout.  Both are
#: derived from the deadline, never chosen independently.
LIFECYCLE_WAIT_MARGIN_SECONDS = 10.0
LIFECYCLE_IPC_MARGIN_SECONDS = 10.0

#: The diagnostic sampler is deliberately not a new daemon-start budget.  It
#: runs only after the unchanged readiness wait has failed, and bounds the one
#: macOS ``ps`` process used to read child CPU time.  Linux uses one ``/proc``
#: read and does not consume this allowance.
PROCESS_CPU_PROBE_TIMEOUT_SECONDS = 1.0
