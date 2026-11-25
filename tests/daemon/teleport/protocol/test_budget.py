"""The teleport run budget chain: steps < run budget < daemon IPC < CLI wait."""

from __future__ import annotations

from hyprial.daemon.impl.ipc import server
from hyprial.kernel import TELEPORT_BUDGET as budget
from hyprial.shell.impl.cli.commands.agent.teleport import TELEPORT_IPC_TIMEOUT_SECONDS


def test_ipc_deadline_covers_the_sum_of_the_step_budgets() -> None:
    assert budget.run_budget_seconds >= (
        budget.prepare_timeout
        + budget.routine_drain_seconds
        + budget.quiesce_export_seconds
        + budget.stream_worst_case_seconds
        + budget.receive_seconds
        + budget.commit_seconds
        + budget.abort_timeout
    )
    assert budget.ipc_seconds > budget.run_budget_seconds
    assert budget.ipc_seconds > server._IPC_REQUEST_TIMEOUT


def test_cli_wait_is_strictly_above_the_daemon_deadline() -> None:
    assert TELEPORT_IPC_TIMEOUT_SECONDS > budget.ipc_seconds
