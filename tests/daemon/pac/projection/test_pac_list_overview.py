"""Daemon-side caller scoping for PAC overview list commands."""

from __future__ import annotations

import threading

from hyprial.daemon.impl.application import DaemonApplication
from hyprial.shell.impl.cli.commands.common import support as _cli_support
import hyprial.shell.impl.cli.runtime.entry  # noqa: F401  (installs the explicit CLI composition)


CALLER = "agent:test:node:caller"
OTHER = "agent:test:node:other"


class _Workflows:
    def __init__(self) -> None:
        self.viewers: list[str | None] = []

    def list(self, *, limit: int, viewer: str | None):
        self.viewers.append(viewer)
        rows = [
            {"graphId": "wf-caller", "sender": CALLER},
            {"graphId": "wf-other", "sender": OTHER},
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
                {
                    "name": "mine",
                    "owner": CALLER,
                    "actor": CALLER,
                    "outcomes": ["done"],
                },
                {
                    "name": "theirs",
                    "owner": OTHER,
                    "actor": OTHER,
                    "outcomes": [],
                },
                {
                    "name": "delegated",
                    "owner": OTHER,
                    "actor": CALLER,
                    "outcomes": ["escalated"],
                },
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
