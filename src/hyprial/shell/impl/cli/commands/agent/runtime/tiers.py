"""Read recorded runtime tiers and run their single authoritative probe."""

from __future__ import annotations

import typer

from hyprial.kernel import ipc_errors
from hyprial.shell.impl.cli.commands.common.services import get_services
from hyprial.shell.impl.cli.commands.common.support import JsonObject


tiers_app = typer.Typer(
    help="Inspect recorded model-tier availability and probe runtime combinations.",
    invoke_without_command=True,
)


def _parse_combo(raw: str) -> tuple[str, str | None, str]:
    services = get_services()
    parts = raw.split(":")
    if len(parts) != 3 or not parts[0] or not parts[2]:
        raise services.CliError(
            ipc_errors.INVALID_ARGUMENT,
            f"--combo {raw!r} must be harness:provider:model "
            "(provider may be empty, e.g. claude::sonnet)",
        )
    harness, provider, model = parts
    return harness, provider or None, model


def _delivery_agent_live(agent: str | None) -> bool:
    services = get_services()
    if agent is None:
        return False
    try:
        status = services._daemon_request("ps", {})
    except ipc_errors.DaemonUnavailableError:
        return False
    connectors = status.get("connectors")
    if not isinstance(connectors, list):
        return False
    return any(
        isinstance(candidate, dict)
        and candidate.get("runtime") == "user-proxy"
        and candidate.get("name") == agent
        and candidate.get("running") is True
        for candidate in connectors
    )


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
            raise services.CliError(ipc_errors.INVALID_ARGUMENT, str(error)) from error

    services._execute(operation, json_output=json_output)


@tiers_app.command("probe")
def tiers_probe(
    owner_key: str | None = typer.Option(
        None, "--owner-key", help="Profile owner key; required with multiple users."
    ),
    combo: list[str] = typer.Option(
        [],
        "--combo",
        help="harness:provider:model to declare and probe; repeatable. "
        "Without --combo, every declared combination is re-probed.",
    ),
    timeout: float = typer.Option(180.0, "--timeout", help="Per-probe seconds."),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Show probe results without writing the profile."
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Probe combinations, classify them once, and record availability."""
    services = get_services()

    def operation() -> JsonObject:
        from hyprial.daemon import probe_profile
        from hyprial.identity import UserProfileError

        combos = tuple(_parse_combo(raw) for raw in combo)
        try:
            document = probe_profile(
                services._state_dir(), owner_key, combos,
                timeout=timeout, dry_run=dry_run,
            )
        except UserProfileError as error:
            raise services.CliError("NOT_FOUND", str(error)) from error
        except ValueError as error:
            raise services.CliError(ipc_errors.INVALID_ARGUMENT, str(error)) from error
        profile_json = document["profile"]
        profile_json["deliveryAgentLive"] = _delivery_agent_live(
            profile_json.get("deliveryAgent")
        )
        return document

    services._execute(operation, json_output=json_output)
