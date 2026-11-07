"""The retired PAC CLI has no public surface; workflow owns its operations."""

from __future__ import annotations

from typer.main import get_command

from hyprial.shell.impl.cli.commands.common.root import app as _cli_root_app
import hyprial.shell.impl.cli.runtime.entry  # noqa: F401  (installs the explicit CLI composition)


def test_workflow_is_the_public_graph_surface() -> None:
    commands = get_command(_cli_root_app).commands
    assert "pac" not in commands
    assert {"workflow", "routine"} <= set(commands)


def test_workflow_has_no_legacy_pac_subtree() -> None:
    workflow = get_command(_cli_root_app).commands["workflow"].commands
    assert "pac" not in workflow
    assert {"plan", "run", "list", "inspect", "events", "reset", "worker"} <= set(workflow)
    assert "status" not in workflow
    assert "context" not in workflow


def test_migration_period_subtrees_stay_callable_but_hidden() -> None:
    workflow = get_command(_cli_root_app).commands["workflow"].commands
    assert workflow["history"].hidden is True
    assert workflow["migration"].hidden is True
