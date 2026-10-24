"""PAC workflow CLI command module."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import typer

from hyprial.kernel import ipc_errors
from hyprial.shell.impl.cli.commands.common.services import get_services
from hyprial.shell.impl.cli.output import CliResult, render_generic, warn


from hyprial.shell.impl.cli.commands.workflow.internal.worktrees import (
    _render_worktree_repository,
    collect_worktree_reports,
)
from hyprial.shell.impl.cli.commands.workflow.run import workflow_app


@workflow_app.command("gc")
def workflow_gc(
    repos: list[Path] | None = typer.Argument(
        None,
        metavar="[REPO]...",
        help="Repository paths for the read-only worktree advice (default: current directory).",
    ),
    base: str | None = typer.Option(
        None,
        "--base",
        help="Merged means reachable from this ref (default: any of origin/HEAD, origin/dev, origin/main that exist).",
    ),
    show_all: bool = typer.Option(
        False, "--all", help="Show every worktree in the human worktree advice."
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Preview the next bounded worker removal set and the worktree advice.",
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Preview workflow GC; the only read-only entry, it never reclaims."""
    services = get_services()

    def operation() -> CliResult:
        services = get_services()
        if not dry_run:
            raise services.CliError(
                ipc_errors.INVALID_ARGUMENT,
                "PAC GC is daemon-owned; pass --dry-run to preview it",
            )
        preview = services._daemon_request("pac.gc.preview", {})
        if not isinstance(preview, dict):
            raise services.CliError("INVALID_RESPONSE", "pac.gc.preview must return an object")
        try:
            results = collect_worktree_reports(repos, base=base)
        except services.CliError as error:
            # The preview itself must survive a default cwd outside any git
            # repository; an explicit repository argument still errors, and
            # a real git failure inside a repository is not hidden.
            if repos or error.code != ipc_errors.INVALID_ARGUMENT:
                raise
            warn(f"worktree advice skipped: {error}", json_output=json_output)
            results = []

        def render(_data: Any) -> str:
            lines = [render_generic(preview)]
            lines.extend(
                _render_worktree_repository(result, show_all=show_all)
                for result in results
            )
            return "\n\n".join(lines)

        return CliResult({"ok": True, **preview, "worktrees": results}, render=render)

    services._execute(operation, json_output=json_output)
