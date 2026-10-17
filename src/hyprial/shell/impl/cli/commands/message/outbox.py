"""The ``hyprial outbox`` delivery-observation command group."""

from __future__ import annotations

from hyprial.shell.impl.cli.commands.common.services import get_services

import typer

outbox_app = typer.Typer(help="Inspect and prune the durable outbox.")


@outbox_app.command("list")
def outbox_list(
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """List every queued outbox entry, flagging undeliverable schemes."""
    services = get_services()

    services._execute(lambda: services._daemon_request("outbox.list"), json_output=json_output)


@outbox_app.command("prune")
def outbox_prune(
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Report what would be pruned without deleting."
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Move dead outbox entries (undeliverable scheme or expired TTL) to the DLQ."""
    services = get_services()

    services._execute(
        lambda: services._daemon_request("outbox.prune", {"dryRun": dry_run}),
        json_output=json_output,
    )
