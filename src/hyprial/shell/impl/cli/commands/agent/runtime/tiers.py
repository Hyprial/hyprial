"""Read recorded runtime tiers and run their single authoritative probe."""

from __future__ import annotations

import typer
from hyprial.shell.impl.cli.commands.common.groups import group

from hyprial.error import codes
from hyprial.shell.impl.cli.commands.common.services import get_services
from hyprial.shell.impl.cli.commands.common.support import JsonObject


tiers_app = group(
    help="Inspect recorded model-tier availability and probe runtime combinations.",
    invoke_without_command=True,
    # Declared no-argument behavior: bare `hyprial agent tiers` reports tiers.
    no_args_is_help=False,
)


def _parse_combo(raw: str) -> tuple[str, str | None, str]:
    services = get_services()
    parts = raw.split(":")
    if len(parts) != 3 or not parts[0] or not parts[2]:
        raise services.CliError(
            codes.INVALID_ARGUMENT,
            f"--combo {raw!r} must be harness:provider:model "
            "(provider may be empty, e.g. claude::sonnet)",
        )
    harness, provider, model = parts
    return harness, provider or None, model


@tiers_app.callback()
def agent_tiers(
    ctx: typer.Context,
    tier: str | None = typer.Option(
        None, "--tier", help="Limit the view to fast, strong, or super."
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Show static tier choices beside recorded availability; never probe."""
    if ctx.invoked_subcommand is not None:
        return
    services = get_services()

    def operation() -> JsonObject:
        from hyprial.shell.impl.cli.commands.workflow.internal.dispatch import tier_report

        try:
            return tier_report(tier)
        except ValueError as error:
            raise services.CliError(codes.INVALID_ARGUMENT, str(error)) from error

    services.runtime.execute(operation, json_output=json_output, ensure_daemon=False)


@tiers_app.command("probe")
def tiers_probe(
    owner_key: str | None = typer.Option(
        None, "--owner-key", help="Owner key whose capabilities are probed; defaults to this node's owner."
    ),
    combo: list[str] = typer.Option(
        [],
        "--combo",
        help="harness:provider:model to declare and probe; repeatable. "
        "Without --combo, every declared combination is re-probed.",
    ),
    timeout: float = typer.Option(180.0, "--timeout", help="Per-probe seconds."),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Show probe results without recording them."
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Probe combinations, classify them once, and record availability."""
    services = get_services()

    def operation() -> JsonObject:
        from hyprial.daemon import probe_capabilities

        combos = tuple(_parse_combo(raw) for raw in combo)
        try:
            return probe_capabilities(
                services._state_dir(), owner_key, combos,
                timeout=timeout, dry_run=dry_run,
            )
        except ValueError as error:
            raise services.CliError(codes.INVALID_ARGUMENT, str(error)) from error

    services.runtime.execute(operation, json_output=json_output, ensure_daemon=False)
