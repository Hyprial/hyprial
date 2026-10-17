"""The ``hyprial link`` subcommands.

Every subcommand crosses the versioned IPC boundary: the read-only queries
(``list``, ``stats``) and the operator mutations (``add``, ``drop``) are the
daemon's decisions, never the shell's.  Method names come from the daemon's
public face; ``--from`` accepts a registered alias or a canonical URI.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import typer

from hyprial.shell.impl.cli.commands.common.services import get_services
from hyprial.shell.impl.cli.commands.common.support import (
    JsonObject,
    _message_claim_params,
)
from hyprial.shell.impl.cli.commands.link.render import (
    render_link_change,
    render_link_list,
    render_link_stats,
)
from hyprial.shell.impl.cli.commands.message.send import _validate_from_identity
from hyprial.shell.impl.cli.output import CliResult

link_app = typer.Typer(
    help="Inspect and manage this node's send links.", no_args_is_help=True
)


def _result(result: Any, render: Any) -> CliResult:
    # The daemon result is authoritative; ``ok`` stays whatever it said.
    data = dict(result) if isinstance(result, Mapping) else {"result": result}
    return CliResult({"ok": True, **data}, render=render)


@link_app.command("list")
def link_list(
    source: str = typer.Option(
        ...,
        "--from",
        help="Local actor whose links to inspect.",
        callback=_validate_from_identity,
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """List this actor's links, its window and its owner link."""
    services = get_services()

    def operation() -> Any:
        from hyprial.daemon import IPC_LINK_LIST

        services = get_services()
        return _result(
            services._daemon_request(IPC_LINK_LIST, _message_claim_params(source)),
            render_link_list,
        )

    services._execute(operation, json_output=json_output)


@link_app.command("stats")
def link_stats(
    source: str = typer.Option(
        ...,
        "--from",
        help="Local actor whose window to inspect.",
        callback=_validate_from_identity,
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Show this actor's window controller readings and per-link estimates."""
    services = get_services()

    def operation() -> Any:
        from hyprial.daemon import IPC_LINK_STATS

        services = get_services()
        return _result(
            services._daemon_request(IPC_LINK_STATS, _message_claim_params(source)),
            render_link_stats,
        )

    services._execute(operation, json_output=json_output)


@link_app.command("add")
def link_add(
    peer: str = typer.Argument(..., help="Peer actor URI to reference."),
    source: str = typer.Option(
        ...,
        "--from",
        help="Local actor, or user:<owner> with --on-behalf-of.",
        callback=_validate_from_identity,
    ),
    on_behalf_of: str | None = typer.Option(
        None,
        "--on-behalf-of",
        help="Local agent the operator acts for; never the wire sender.",
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Write a one-sided reference to <peer> without sending anything."""
    services = get_services()

    def operation() -> Any:
        from hyprial.daemon import IPC_LINK_ADD

        services = get_services()
        params: JsonObject = {
            **_message_claim_params(source, on_behalf_of=on_behalf_of),
            "peer": peer,
        }
        return _result(
            services._daemon_request(IPC_LINK_ADD, params), render_link_change
        )

    services._execute(operation, json_output=json_output)


@link_app.command("drop")
def link_drop(
    peer: str = typer.Argument(..., help="Peer actor URI to reclaim."),
    source: str = typer.Option(
        ...,
        "--from",
        help="Local actor, or user:<owner> with --on-behalf-of.",
        callback=_validate_from_identity,
    ),
    on_behalf_of: str | None = typer.Option(
        None,
        "--on-behalf-of",
        help="Local agent the operator acts for; never the wire sender.",
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Reclaim the link to <peer> and drain its pending requests."""
    services = get_services()

    def operation() -> Any:
        from hyprial.daemon import IPC_LINK_DROP

        services = get_services()
        params: JsonObject = {
            **_message_claim_params(source, on_behalf_of=on_behalf_of),
            "peer": peer,
        }
        return _result(
            services._daemon_request(IPC_LINK_DROP, params), render_link_change
        )

    services._execute(operation, json_output=json_output)
