"""The ``hyprial message outbox`` delivery-observation command group.

One outbox view: ``message outbox`` lists every queued entry (optionally
filtered to one actor's own sends) and ``message outbox prune`` moves dead
entries to the DLQ.  The per-actor view reuses the outbox-list serializer
(plan item F7/A7), so the two readers never drift.
"""

from __future__ import annotations

from hyprial.shell.impl.cli.commands.common.services import get_services

from typing import Any
import typer


outbox_app = typer.Typer(
    help="Inspect and prune the durable outbox.", invoke_without_command=True
)


@outbox_app.callback()
def message_outbox(
    ctx: typer.Context,
    actor: str | None = typer.Option(
        None, "--actor", help="Only this local actor's queued sends."
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """List every queued outbox entry, flagging undeliverable schemes."""
    if ctx.invoked_subcommand is not None:
        return
    services = get_services()

    def operation() -> Any:
        if actor is None:
            return services._daemon_request("outbox.list")
        return services._daemon_request(
            "message.query", {"from": actor, "view": "outbox"}
        )

    services._execute(operation, json_output=json_output)


@outbox_app.command("prune")
def message_outbox_prune(
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
