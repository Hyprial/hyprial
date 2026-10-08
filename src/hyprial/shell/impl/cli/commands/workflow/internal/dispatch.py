"""``hyprial dispatch matrix``."""

from __future__ import annotations

from hyprial.shell.impl.cli.commands.common.services import get_services

from hyprial.kernel import ipc_errors
import typer

from hyprial.shell.impl.cli.commands.common.support import JsonObject
dispatch_app = typer.Typer(
    help="Dispatch capability matrix (read-only).", no_args_is_help=True
)


@dispatch_app.command("matrix")
def dispatch_matrix(
    tier: str | None = typer.Option(
        None, "--tier", help="fast, strong, or super."
    ),
    probe: bool = typer.Option(
        False, "--probe", help="Diagnose each candidate; never selects."
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Inspect choices, optionally diagnosing; never launch or update a profile."""
    services = get_services()

    def operation() -> JsonObject:
        services = get_services()
        from hyprial.daemon import (
            candidate_json,
            diagnose,
            resolve,
            workflow_reminders,
        )
        from hyprial.kernel import TIERS

        if tier is not None and tier not in TIERS:
            raise services.CliError(
                ipc_errors.INVALID_ARGUMENT,
                "tier must be fast, strong, or super",
            )
        tiers = (tier,) if tier is not None else tuple(TIERS)
        document: JsonObject = {
            "ok": True,
            "reminders": workflow_reminders(),
            "tiers": {
                name: [candidate_json(candidate) for candidate in TIERS[name]]
                for name in tiers
            },
            # The static selection, reported so a caller reading this command
            # sees the same answer dispatch would use.  It is not derived from
            # the diagnostics below and carries no readings.
            "selection": {
                name: candidate_json(resolve(name).selected) for name in tiers
            },
        }
        if probe:
            # Explicit human diagnosis only.  Read-only by construction: the
            # readings are reported here and consumed nowhere else.
            document["diagnostics"] = {
                name: [reading.to_json() for reading in diagnose(name)]
                for name in tiers
            }
        return document

    services._execute(operation, json_output=json_output)
