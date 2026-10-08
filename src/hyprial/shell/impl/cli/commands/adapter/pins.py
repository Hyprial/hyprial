"""``hyprial adapter pin/unpin/pins`` harness pinning."""

from __future__ import annotations

from hyprial.shell.impl.cli.commands.common.services import get_services

import typer

from hyprial.shell.impl.cli.commands.adapter.admin import adapter_app
@adapter_app.command("pin")
def adapter_pin(
    adapter: str = typer.Argument(..., help="Configured adapter name."),
    actor: str = typer.Argument(
        ..., help="Agent name (or canonical agent:<owner>:<machine>:<actor> URI)."
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Bind this adapter's inbound messages to one agent on this machine.

    Adapter and agent bind one-to-one: an adapter routes to exactly one agent,
    and an agent can be pinned by at most one adapter (pin the target
    elsewhere first requires 'hyprial adapter unpin' on its current adapter). The
    agent must already exist ('hyprial agent create' / 'hyprial start'); the stored
    value is always its canonical URI. The pin lives on the agent's record and
    disappears with 'hyprial agent destroy'. Inbound-only: replies travel with
    each message's own correlation, never by reverse lookup of this pin.
    """
    services = get_services()

    services._execute(
        lambda: services._daemon_request("adapter.pin", {"name": adapter, "actor": actor}),
        json_output=json_output,
    )


@adapter_app.command("unpin")
def adapter_unpin(
    adapter: str = typer.Argument(...),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Remove an adapter's receiver pin from its agent's record."""
    services = get_services()

    services._execute(
        lambda: services._daemon_request("adapter.unpin", {"name": adapter}),
        json_output=json_output,
    )


@adapter_app.command("pins")
def adapter_pins(
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """List adapter receiver pins (adapter -> canonical agent URI)."""
    services = get_services()

    services._execute(lambda: services._daemon_request("adapter.pins", {}), json_output=json_output)
