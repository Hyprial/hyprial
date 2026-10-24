"""Shared helpers for ``hyprial message status``."""

from __future__ import annotations

from hyprial.shell.impl.cli.commands.common.services import get_services

from typing import Any
import typer

from hyprial.shell.impl.cli.commands.common.support import _message_claim_params

delivery_app = typer.Typer(help="Ask what actually happened to messages you sent.")


@delivery_app.command("status")
def delivery_status(
    source: str = typer.Option(..., "--from", help="Local sending actor."),
    message_id: str | None = typer.Option(
        None, "--message-id", help="One message; omit for every recorded outcome."
    ),
    timeout: float | None = typer.Option(
        None, "--timeout", help="Seconds to wait on other holders (default 2)."
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Show the persisted terminal state of messages you sent.

    Every persisted outcome is ``fetched`` (the recipient took it) or
    ``expired`` (the holder TTL elapsed).  Holder response health remains in
    ``diagnostics`` and never changes that terminal outcome.  ``pending`` means
    a holder still has it and no verdict exists yet; ``unknown`` means no
    reachable holder has a record.
    """
    services = get_services()

    def operation() -> Any:
        services = get_services()
        params = _message_claim_params(source)
        if message_id is not None:
            params["messageId"] = message_id
        if timeout is not None:
            params["timeoutSeconds"] = timeout
        return services._daemon_request("message.status", params)

    services._execute(operation, json_output=json_output)
