"""``hyprial adapter route`` commands."""

from __future__ import annotations

from hyprial.shell.impl.cli.commands.common.services import get_services

from typing import Any
from hyprial.kernel import ipc_errors
import typer

from hyprial.shell.impl.cli.commands.adapter.admin import _parse_adapter_routes
from hyprial.shell.impl.cli.commands.common.support import JsonObject
route_app = typer.Typer(
    help="Manage one adapter's outbound routes (route:<adapter>:<route>)."
)


def _route_candidates(name: str) -> list[JsonObject]:
    """Chat ids this adapter observed inbound traffic from, as CLI JSON rows.

    Reads the shared adapter database through one adapter's namespace, read-only:
    a machine that has never received an inbound event has no database, and asking
    must not create one.  Unreadable state is reported as "no candidates" rather
    than as a failure, because this feeds first-run planning, which stays useful
    for every step that has nothing to do with messaging.
    """
    services = get_services()

    from hyprial.daemon import adapter_namespace
    from hyprial.daemon import observed_chats

    try:
        rows = observed_chats(
            services._state_dir() / "adapters.sqlite3",
            adapter=adapter_namespace(name),
        )
    except Exception:  # noqa: BLE001 - an unreadable store is absence, not failure
        return []
    return [{"chatId": row.chat_id, "chatType": row.chat_type} for row in rows]


def _parse_one_route(value: str) -> Any:
    """Parse a single ``name=native_id`` pair, reusing the add-time rules."""

    return _parse_adapter_routes([value])[0]


def _route_error_code(error: Exception) -> str:
    """Map a config error to the same code ``adapter remove`` reports."""

    from hyprial.daemon import AdapterNotFoundError

    if isinstance(error, AdapterNotFoundError):
        return ipc_errors.ADAPTER_NOT_FOUND
    return getattr(error, "code", None) or ipc_errors.INVALID_ARGUMENT


def _route_operation(call: Any) -> JsonObject:
    """Run one route mutation, map its error code, then hot-reload.

    A route that exists only in the file is a route the running daemon cannot
    deliver to, so the reload belongs to the change -- but an unreachable
    daemon is not a failure: it reads the same config at its next start. This
    is the same contract as ``adapter add``.
    """
    services = get_services()

    from hyprial.kernel import PersistentConfigError

    try:
        result = call()
    except PersistentConfigError as error:
        raise services.CliError(_route_error_code(error), str(error)) from error
    try:
        result["daemonReload"] = services._daemon_request(
            "adapter.reload", {}, timeout=5.0, restore_wait=0.0
        )
    except (services.CliError, ipc_errors.TransientDaemonError):
        result["daemonReload"] = None
    return result


@route_app.command("list")
def adapter_route_list(
    name: str | None = typer.Argument(
        None, help="Configured adapter name; omit to list every adapter."
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Show the routes bound on an adapter, read from local config."""
    services = get_services()

    def operation() -> JsonObject:
        services = get_services()
        from hyprial.daemon import list_gateway_routes
        from hyprial.kernel import PersistentConfigError

        try:
            return list_gateway_routes(hyprial_home=services._hyprial_home(), name=name)
        except PersistentConfigError as error:
            raise services.CliError(_route_error_code(error), str(error)) from error

    services._execute(operation, json_output=json_output)


@route_app.command("candidates")
def adapter_route_candidates(
    name: str = typer.Argument(..., help="Configured adapter name."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """List the chat ids this adapter has already seen inbound traffic from.

    This is the machine-side answer to "which chat should a first route point
    at": the user talks to the bot once and the id is observed, instead of being
    copied by hand.  Read-only by construction -- it never creates adapter state,
    so a first-run plan may call it before any route exists.
    """
    services = get_services()

    def operation() -> JsonObject:
        services = get_services()
        return {
            "ok": True,
            "adapter": name,
            "candidates": services._route_candidates(name),
        }

    services._execute(operation, json_output=json_output, allow_missing_home=True)


@route_app.command("add")
def adapter_route_add(
    name: str = typer.Argument(..., help="Configured adapter name."),
    route: str = typer.Argument(
        ..., help="Route as name=native_chat_id (the chat/user id to send to)."
    ),
    make_default: bool = typer.Option(
        False, "--default", help="Also make this the gateway's default route."
    ),
    force: bool = typer.Option(
        False, "--force", help="Rebind a route name that already exists."
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Bind one outbound route on an existing adapter.

    Touches only ``channels.json`` -- never the App credential -- then asks a
    running daemon to reload. Use this instead of ``adapter add --force``,
    which replaces the whole gateway entry and rewrites its secret file.
    """
    services = get_services()

    def operation() -> JsonObject:
        services = get_services()
        from hyprial.daemon import add_gateway_route

        parsed = _parse_one_route(route)
        return _route_operation(
            lambda: add_gateway_route(
                hyprial_home=services._hyprial_home(),
                name=name,
                route=parsed,
                make_default=make_default,
                force=force,
            )
        )

    services._execute(operation, json_output=json_output)


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
        services = get_services()
        from hyprial.daemon import remove_gateway_route

        return _route_operation(
            lambda: remove_gateway_route(
                hyprial_home=services._hyprial_home(),
                name=name,
                route_name=route_name,
                force=force,
            )
        )

    services._execute(operation, json_output=json_output)
