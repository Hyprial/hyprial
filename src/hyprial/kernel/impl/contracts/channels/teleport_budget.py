# Decision record (file header; no inline comments by repository rule):
# - Every step budget of a source-side ``teleport.run`` lives in this one table
#   so the service, the link, the daemon IPC deadline and the CLI wait derive
#   from it instead of separate literals.  It sits in the kernel because the
#   IPC server must read the deadline while the teleport package may only be
#   imported by daemon composition.
# - The chunk phase is unbounded by stream size, so the service enforces
#   ``stream_seconds`` itself: it stops sending before the next chunk once the
#   window is spent and answers ``TELEPORT_INCOMPLETE``.  A chunk already in
#   flight can overrun by its full retry budget (``chunk_timeout`` x
#   ``chunk_attempts``), which ``stream_worst_case_seconds`` includes.
# - ``quiesce_export_seconds`` is a reserve for stopping the agent and building
#   the bundle; those steps have no timeout parameter of their own.
# - ``run_budget_seconds`` is the sum of all steps in their worst case.  The
#   daemon IPC deadline adds ``ipc_margin_seconds`` so the daemon always
#   answers with a ``TELEPORT_*`` verdict first; the CLI wait is strictly
#   larger than that deadline (asserted in ``tests/daemon/teleport``).
"""Dependency-free time budgets of one ``teleport.run``."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class TeleportBudget:
    default_strict_seconds: float = 60.0
    prepare_timeout: float = 10.0
    chunk_timeout: float = 15.0
    chunk_attempts: int = 3
    receive_grace: float = 60.0
    commit_timeout: float = 15.0
    commit_attempts: int = 2
    abort_timeout: float = 10.0
    routine_drain_seconds: float = 120.0
    quiesce_export_seconds: float = 60.0
    stream_seconds: float = 120.0
    run_margin_seconds: float = 20.0
    ipc_margin_seconds: float = 25.0

    @property
    def stream_worst_case_seconds(self) -> float:
        return self.stream_seconds + self.chunk_timeout * self.chunk_attempts

    @property
    def receive_seconds(self) -> float:
        return self.default_strict_seconds + self.receive_grace

    @property
    def commit_seconds(self) -> float:
        return self.commit_timeout * self.commit_attempts

    @property
    def run_budget_seconds(self) -> float:
        return (
            self.prepare_timeout
            + self.routine_drain_seconds
            + self.quiesce_export_seconds
            + self.stream_worst_case_seconds
            + self.receive_seconds
            + self.commit_seconds
            + self.abort_timeout
            + self.run_margin_seconds
        )

    @property
    def ipc_seconds(self) -> float:
        return self.run_budget_seconds + self.ipc_margin_seconds


TELEPORT_BUDGET = TeleportBudget()
