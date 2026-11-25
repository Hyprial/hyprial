"""Daemon-side caller scoping for PAC overview list commands."""

from __future__ import annotations

import threading

from hyprial.daemon.impl.application import DaemonApplication
from hyprial.shell.impl.cli.commands.common import support as _cli_support
import hyprial.shell.impl.cli.runtime.entry  # noqa: F401  (installs the explicit CLI composition)


CALLER = "agent:test:node:caller"
OTHER = "agent:test:node:other"


def _routine_row(name: str, owner: str, actor: str, outcomes: list[str]) -> dict:
    """One routine projection row with the real producer's required keys."""

    return {
        "name": name,
        "owner": owner,
        "enabled": True,
        "nextDueMs": 1_000,
        "sourceErrorStreak": 0,
        "outcomes": outcomes,
        "registrationId": None,
        "mode": "source",
        "role": "dispatch",
        "actor": actor,
        "actorOwnership": "external",
        "launch": None,
        "overlap": None,
        "missedPeriods": None,
        "inFlight": [],
        "lastDispatchAtMs": None,
        "lastOutcome": None,
    }


def _run_row(graph_id: str, sender: str) -> dict:
    """One workflow.list run row with the real producer's required keys."""

    return {
        "runId": graph_id,
        "graphId": graph_id,
        "backend": "pac",
        "name": "overview",
        "state": "running",
        "sender": sender,
        "createdAtMs": 1_000,
        "closedAtMs": None,
        "onFailure": "terminate",
        "routineName": None,
        "lastProgressAtMs": None,
        "currentNode": None,
    }


class _Workflows:
    def __init__(self) -> None:
        self.viewers: list[str | None] = []

    def list(self, *, limit: int, viewer: str | None):
        self.viewers.append(viewer)
        rows = [
            _run_row("wf-caller", CALLER),
            _run_row("wf-other", OTHER),
        ]
        return {
            "runs": rows
            if viewer is None
            else [row for row in rows if row["sender"] == viewer]
        }

    def routine_last_dispatches(self) -> dict[str, int]:
        return {"mine": 100, "delegated": 150, "theirs": 200}


class _Routines:
    def list(self):
        return {
            "routines": [
                _routine_row("mine", CALLER, CALLER, ["done"]),
                _routine_row("theirs", OTHER, OTHER, []),
                _routine_row("delegated", OTHER, CALLER, ["escalated"]),
            ]
        }


def _app() -> tuple[DaemonApplication, _Workflows]:
    app = object.__new__(DaemonApplication)
    workflows = _Workflows()

    def caller(params):
        return str(params["actor"])

    app.owner = "test"
    app._restore_done = threading.Event()
    app._restore_done.set()
    app._inbox = object()
    app._harnesses = object()
    app._presence = object()
    app._workflow_service = workflows
    app._routine_service = _Routines()
    app._workflow_caller = caller
    return app, workflows


def test_workflow_list_all_exposes_other_sender_without_changing_default() -> None:
    app, workflows = _app()

    default = app.handle("workflow.list", {"actor": CALLER})
    all_callers = app.handle("workflow.list", {"actor": CALLER, "all": True})
    owner_default = app.handle("workflow.list", {"actor": "user:test"})

    assert [row["graphId"] for row in default["runs"]] == ["wf-caller"]
    assert [row["graphId"] for row in all_callers["runs"]] == [
        "wf-caller",
        "wf-other",
    ]
    assert owner_default == all_callers
    assert workflows.viewers == [CALLER, None, None]


def test_routine_list_scopes_agents_and_adds_latest_activity() -> None:
    app, _workflows = _app()

    default = app.handle("routine.list", {"actor": CALLER})
    all_callers = app.handle("routine.list", {"actor": CALLER, "all": True})
    owner_default = app.handle("routine.list", {"actor": "user:test"})

    assert [row["name"] for row in default["routines"]] == ["mine", "delegated"]
    assert [row["name"] for row in all_callers["routines"]] == [
        "mine",
        "theirs",
        "delegated",
    ]
    assert owner_default == all_callers
    assert default["routines"][0]["lastDispatchAtMs"] == 100
    assert default["routines"][0]["lastOutcome"] == "done"
    assert all_callers["routines"][1]["lastOutcome"] is None


def test_plain_text_output_is_not_hard_wrapped_at_80_columns(capsys) -> None:
    """Rich wraps at 80 when stdout is not a TTY; a table line must stay whole."""

    line = "NAME" + " " * 90 + "LAST RESULT  NEXT    IN-FLIGHT"
    _cli_support._emit(line, json_output=False)
    assert capsys.readouterr().out == line + "\n"


def test_a_disabled_routine_shows_paused_not_an_overdue_time() -> None:

    text = _cli_support._render_routine_list(
        [{"name": "c1", "owner": "agent:o:m:kanban", "mode": "source", "enabled": False,
          "nextDueMs": 1_000, "inFlight": []}],
        all_callers=False, now_ms=600_000,
    )
    row = text.splitlines()[2]
    assert "paused" in row and "ago" not in row
