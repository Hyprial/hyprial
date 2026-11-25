"""``hyprial agent`` administration command modules."""

from __future__ import annotations


import typer

from hyprial.error import codes
from hyprial.shell.impl.cli.commands.common.services import get_services

from hyprial.shell.impl.cli.commands.agent.admin import agent_app


@agent_app.command("grant")
def agent_grant(
    actor: str = typer.Argument(..., help="Agent instance name; also the caller for check/visibility."),
    verb: str = typer.Argument(
        "add",
        help="add (default), list, revoke, check, or visibility.",
    ),
    grant_id: str | None = typer.Argument(
        None, help="Grant id, required by revoke."
    ),
    capability: str | None = typer.Option(
        None,
        "--capability",
        help="Capability name from the grant schema; for check, a frozen capability.",
    ),
    scope: list[str] | None = typer.Option(
        None,
        "--scope",
        help="Capability scope; for add a single line (arrays/paths use JSON), repeatable for check.",
    ),
    new_grant_id: str | None = typer.Option(
        None, "--grant-id", help="Stable id for add; defaults to a new UUID."
    ),
    revision: int = typer.Option(1, "--revision", help="Must increase when updating an existing id."),
    audit: bool = typer.Option(
        False, "--audit", help="With list: show history, including destroyed incarnations."
    ),
    resource: str | None = typer.Option(
        None,
        "--resource",
        help="With check: tool name, path, isolation mode, or agent-home URI.",
    ),
    caller: str | None = typer.Option(
        None,
        "--caller",
        help="With check/visibility: caller principal URI; omit for the host-local operator.",
    ),
    hosted: bool = typer.Option(
        False, "--hosted", help="With check/visibility: treat the caller as a hosted visitor."
    ),
    unverified: bool = typer.Option(
        False, "--unverified", help="With check/visibility: mark the identity unverified."
    ),
    write: bool = typer.Option(
        False, "--write", help="With check: for shared-path, check write access instead of read."
    ),
    see_actors: str | None = typer.Option(
        None,
        "--see-actors",
        help="With visibility: see-actors scope as a JSON array of principal URIs.",
    ),
    send_to: str | None = typer.Option(
        None,
        "--send-to",
        help="With visibility: send-to scope as a JSON array of principal URIs.",
    ),
    target: list[str] | None = typer.Option(
        None,
        "--target",
        help="With visibility: candidate target URI to decide (repeatable).",
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Record, inspect, revoke, or reason about a capability grant.

    ``add`` records a capability grant: see-actors and send-to are enforced on
    each applicable request, while other capabilities are recorded for their
    owning entry points. ``list`` reads the actor's active records (or its
    append-only audit history with --audit) and ``revoke`` removes one.

    ``check`` evaluates a what-if capability decision from CLI arguments: it
    does not read the grant ledger, and the result names the entry point that
    owns the capability (AT10).

    ``visibility`` evaluates what-if see-actors / send-to decisions from CLI
    arguments: it does not read the grant ledger (AT09 decision core).
    """
    services = get_services()
    if verb == "add":
        services.runtime.execute(
            lambda: services.runtime.request("agent.grant", {
                "actor": actor,
                "capability": _required(capability, "--capability"),
                "scope": _single_scope(scope),
                "grantId": new_grant_id if new_grant_id is not None else str(services.uuid4()),
                "revision": revision,
            }), json_output=json_output,
        )
        return
    if verb == "list":
        services.runtime.execute(
            lambda: services.runtime.request("agent.grants", {
                "actor": actor, "audit": audit,
            }), json_output=json_output,
        )
        return
    if verb == "revoke":
        services.runtime.execute(
            lambda: services.runtime.request(
                "agent.revoke",
                {"actor": actor, "grantId": _required(grant_id, "grant id")},
            ),
            json_output=json_output,
        )
        return
    if verb == "check":
        _run_check(
            actor=actor,
            capability=capability,
            resource=resource,
            caller=caller,
            hosted=hosted,
            unverified=unverified,
            write=write,
            scope=scope,
            revision=revision,
            json_output=json_output,
        )
        return
    if verb == "visibility":
        _run_visibility(
            actor=actor,
            caller=caller,
            hosted=hosted,
            unverified=unverified,
            see_actors=see_actors,
            send_to=send_to,
            target=target,
            json_output=json_output,
        )
        return
    raise services.CliError(
        codes.INVALID_ARGUMENT,
        f"unknown verb {verb!r}; use add, list, revoke, check, or visibility",
    )


def _required(value: str | None, label: str) -> str:
    if value is None:
        raise get_services().CliError(
            codes.INVALID_ARGUMENT, f"{label} is required for this verb"
        )
    return value


def _single_scope(scope: list[str] | None) -> str:
    services = get_services()
    if not scope:
        raise services.CliError(
            codes.INVALID_ARGUMENT, "--scope is required for add"
        )
    if len(scope) != 1:
        raise services.CliError(
            codes.INVALID_ARGUMENT, "add records one grant, so pass exactly one --scope"
        )
    return scope[0]


def _simulation_request(method: str, scenario: dict[str, object]) -> dict[str, object]:
    services = get_services()
    try:
        return services.runtime.request(method, {"scenario": scenario})
    except services.CliError as error:
        if error.code in (codes.ENFORCEMENT_ERROR, codes.VISIBILITY_ERROR):
            raise ValueError(str(error)) from None
        scope_codes = {
            codes.ENFORCEMENT_SCOPE_INVALID: "enforcement_scope_invalid",
            codes.VISIBILITY_SCOPE_INVALID: "visibility_scope_invalid",
        }
        if error.code in scope_codes:
            raise services.CliError(scope_codes[error.code], str(error)) from None
        raise


def _run_check(
    *, actor: str, capability: str | None, resource: str | None, caller: str | None,
    hosted: bool, unverified: bool, write: bool, scope: list[str] | None,
    revision: int, json_output: bool,
) -> None:
    del actor
    services = get_services()

    def operation() -> dict[str, object]:
        return _simulation_request("agent.grant.check", {
            "caller": caller, "hosted": hosted, "unverified": unverified,
            "capability": _required(capability, "--capability"),
            "resource": _required(resource, "--resource"),
            "scope": scope or [], "revision": revision, "write": write,
        })

    services.runtime.execute(operation, json_output=json_output)


def _run_visibility(
    *, actor: str, caller: str | None, hosted: bool, unverified: bool,
    see_actors: str | None, send_to: str | None, target: list[str] | None,
    json_output: bool,
) -> None:
    del actor
    services = get_services()
    services.runtime.execute(lambda: _simulation_request("agent.grant.visibility", {
        "caller": caller, "hosted": hosted, "unverified": unverified,
        "seeActors": see_actors, "sendTo": send_to, "targets": target or [],
    }), json_output=json_output)


@agent_app.command("grants")
def agent_grants(
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """List every capability record on this machine.

    This is the global view only; one actor's records and its audit history
    are read with `agent grant <ACTOR> list [--audit]`.
    """
    services = get_services()
    services.runtime.execute(
        lambda: services.runtime.request("agent.grants", {}),
        json_output=json_output,
    )
