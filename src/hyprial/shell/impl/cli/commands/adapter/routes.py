"""``hyprial adapter route`` commands.

Decision F1a (2026-11-23): every subcommand is a presentation of the daemon's
``adapter.routes.*`` requests; the IPC-only ``adapter.route-config.*`` twin is
retired.  ``route add`` no longer rebinds an existing name: a taken name is
refused and the caller removes it first, so a failed write never loses the
route.  ``route remove --force`` maps to ``clearDefault``.

Decision F1a review (2026-11-24, coordinator ruling on P1-1): each command sends
exactly one daemon request, written in its own operation so the
request-architecture gate sees it.  Writes omit the optional
``expectedRevision``; the daemon still refuses a taken, unbound or default route
under its write lease.  ``route list`` requires an adapter name, because no
daemon request returns every adapter's routes and a per-adapter loop is
request orchestration the CLI must not own.
"""

from __future__ import annotations

from typing import NoReturn

from hyprial.shell.impl.cli.commands.common.services import get_services

from hyprial.error import codes
import typer
from hyprial.shell.impl.cli.commands.common.groups import group

from hyprial.shell.impl.cli.commands.common.support import JsonObject
route_app = group(
    help="Manage one adapter's outbound routes (named chats of that adapter)."
)


def _parse_one_route(value: str) -> tuple[str, str]:
    name, separator, native_id = value.partition("=")
    if not separator or not name or not native_id:
        raise get_services().CliError(codes.INVALID_ARGUMENT, f"--route {value!r} must be name=native_id")
    return name, native_id


def _restate_refusal(error: Exception, name: str, route: str) -> NoReturn:
    """Re-raise a daemon route refusal in CLI terms; other errors pass through unchanged."""

    services = get_services()
    code = getattr(error, "code", None)
    if code == "ROUTE_EXISTS":
        message = (f"route {route!r} already exists on adapter {name!r}; run "
                   f"'hyprial adapter route remove {name} {route}' first, then add it again")
    elif code == "ROUTE_NOT_FOUND":
        message = (f"route {route!r} is not bound on adapter {name!r}; run "
                   f"'hyprial adapter route list {name}' to see the bound routes")
    elif code == "ROUTE_IN_USE":
        message = (f"route {route!r} is the default route of adapter {name!r}; "
                   "pass --force to remove it and clear the default")
    elif code == "ADAPTER_CONFIG_CONFLICT":
        message = f"adapter {name!r} changed while the route was being written; run the command again"
    else:
        raise error
    raise services.CliError(code, message, getattr(error, "data", None)) from None


@route_app.command("list")
def adapter_route_list(
    name: str = typer.Argument(..., help="Configured adapter name."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Show the routes bound on one adapter, as the daemon reports them."""
    services = get_services()

    def operation() -> JsonObject:
        listed = services.runtime.request("adapter.routes.list", {"name": name})
        return {"ok": True, "adapters": [listed["adapter"]]}

    services.runtime.execute(operation, json_output=json_output)


@route_app.command("candidates")
def adapter_route_candidates(
    name: str = typer.Argument(..., help="Configured adapter name."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """List the chat ids this adapter has already seen inbound traffic from.

    This is the machine-side answer to "which chat should a first route point
    at": the user talks to the bot once and the id is observed, instead of being
    copied by hand.  Read-only by construction -- it never creates adapter state.
    The adapter must be configured.
    """
    services = get_services()

    def operation() -> JsonObject:
        return services.runtime.request("adapter.routes.candidates", {"name": name})

    services.runtime.execute(operation, json_output=json_output, allow_missing_home=True)


@route_app.command("add")
def adapter_route_add(
    name: str = typer.Argument(..., help="Configured adapter name."),
    route: str = typer.Argument(
        ..., help="Route as name=native_chat_id (the chat/user id to send to)."
    ),
    make_default: bool = typer.Option(
        False, "--default", help="Also make this the gateway's default route."
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Bind one outbound route on an existing adapter.

    Touches only ``channels.json`` -- never the App credential -- and the daemon
    reloads the adapter. A route name that is already bound is refused; remove
    it first to point it at another chat.
    """
    services = get_services()

    def operation() -> JsonObject:
        route_name, native_id = _parse_one_route(route)
        try:
            return services.runtime.request("adapter.routes.add", {
                "name": name, "route": route_name, "chatId": native_id, "makeDefault": make_default,
            })
        except services.CliError as error:
            _restate_refusal(error, name, route_name)

    services.runtime.execute(operation, json_output=json_output)


@route_app.command("remove")
def adapter_route_remove(
    name: str = typer.Argument(..., help="Configured adapter name."),
    route_name: str = typer.Argument(..., help="Route name to unbind."),
    force: bool = typer.Option(
        False,
        "--force",
        help="Remove even when it is the default route (clears the default).",
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Unbind one outbound route from an adapter."""
    services = get_services()

    def operation() -> JsonObject:
        try:
            return services.runtime.request("adapter.routes.remove", {
                "name": name, "route": route_name, "clearDefault": force,
            })
        except services.CliError as error:
            _restate_refusal(error, name, route_name)

    services.runtime.execute(operation, json_output=json_output)
