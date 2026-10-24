"""``hyprial agent`` administration command modules."""

from __future__ import annotations


import typer

from hyprial.kernel import ipc_errors
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
        help="With check: tool name, channel, path, isolation mode, or agent-home URI.",
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
        services._execute(
            lambda: services._daemon_request("agent.grant", {
                "actor": actor,
                "capability": _required(capability, "--capability"),
                "scope": _single_scope(scope),
                "grantId": new_grant_id if new_grant_id is not None else str(services.uuid4()),
                "revision": revision,
            }), json_output=json_output,
        )
        return
    if verb == "list":
        services._execute(
            lambda: services._daemon_request("agent.grants", {
                "actor": actor, "audit": audit,
            }), json_output=json_output,
        )
        return
    if verb == "revoke":
        services._execute(
            lambda: services._daemon_request(
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
        ipc_errors.INVALID_ARGUMENT,
        f"unknown verb {verb!r}; use add, list, revoke, check, or visibility",
    )


def _required(value: str | None, label: str) -> str:
    if value is None:
        raise get_services().CliError(
            ipc_errors.INVALID_ARGUMENT, f"{label} is required for this verb"
        )
    return value


def _single_scope(scope: list[str] | None) -> str:
    services = get_services()
    if not scope:
        raise services.CliError(
            ipc_errors.INVALID_ARGUMENT, "--scope is required for add"
        )
    if len(scope) != 1:
        raise services.CliError(
            ipc_errors.INVALID_ARGUMENT, "add records one grant, so pass exactly one --scope"
        )
    return scope[0]


def _run_check(
    *,
    actor: str,
    capability: str | None,
    resource: str | None,
    caller: str | None,
    hosted: bool,
    unverified: bool,
    write: bool,
    scope: list[str] | None,
    revision: int,
    json_output: bool,
) -> None:
    del actor  # the what-if identity is --caller; ACTOR names the grant subject
    services = get_services()

    from hyprial.identity import (
        CAPABILITY_ENTRY_POINTS,
        CapabilityRecord,
        EnforcementError,
        Principal,
        check_shared_path,
        explain,
    )

    def operation() -> dict[str, object]:
        services = get_services()
        capability_name = _required(capability, "--capability")
        resource_name = _required(resource, "--resource")
        identity = (
            None
            if caller is None
            else Principal(caller, hosted=hosted, verified=not unverified)
        )
        records: list[CapabilityRecord] = []
        try:
            if caller is not None:
                for raw in scope or ():
                    records.append(
                        CapabilityRecord(
                            actor=caller,
                            capability=capability_name,
                            scope=raw,
                            revision=revision,
                        )
                    )
            if capability_name == "shared-path":
                verdict = check_shared_path(
                    resource_name, write=write, caller=identity, records=records
                )
            else:
                verdict = explain(
                    capability_name, resource_name, caller=identity, records=records
                )
        except EnforcementError as error:
            raise services.CliError(error.code, str(error)) from error
        payload = verdict.as_dict()
        payload["entryPoints"] = dict(CAPABILITY_ENTRY_POINTS)
        return payload

    services._execute(operation, json_output=json_output)


def _run_visibility(
    *,
    actor: str,
    caller: str | None,
    hosted: bool,
    unverified: bool,
    see_actors: str | None,
    send_to: str | None,
    target: list[str] | None,
    json_output: bool,
) -> None:
    del actor  # the what-if identity is --caller; ACTOR names the grant subject
    services = get_services()

    from hyprial.identity import (
        SEE_ACTORS,
        SEND_TO,
        CallerIdentity,
        GrantRecord,
        VisibilityError,
        explain_target,
        may_send,
        visible_targets,
    )

    def operation() -> dict[str, object]:
        services = get_services()
        identity = (
            None
            if caller is None
            else CallerIdentity(caller, hosted=hosted, verified=not unverified)
        )
        grants: list[GrantRecord] = []
        try:
            if see_actors is not None and caller is not None:
                grants.append(
                    GrantRecord(
                        actor=caller, capability=SEE_ACTORS, scope=see_actors
                    )
                )
            if send_to is not None and caller is not None:
                grants.append(
                    GrantRecord(
                        actor=caller, capability=SEND_TO, scope=send_to
                    )
                )
            rows = [
                {"targetUri": uri, "targetKind": "candidate"}
                for uri in (target or ())
            ]
            decisions = [
                {
                    "target": uri,
                    "seeActors": explain_target(
                        uri, capability=SEE_ACTORS, caller=identity, grants=grants
                    ).as_dict(),
                    "sendTo": may_send(
                        uri, caller=identity, grants=grants
                    ).as_dict(),
                }
                for uri in (target or ())
            ]
            visible = [
                row["targetUri"]
                for row in visible_targets(rows, caller=identity, grants=grants)
            ]
        except VisibilityError as error:
            raise services.CliError(error.code, str(error)) from error
        return {
            "caller": caller,
            "hosted": hosted,
            "verified": not unverified,
            "decisions": decisions,
            "visible": visible,
        }

    services._execute(operation, json_output=json_output)


@agent_app.command("grants")
def agent_grants(
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """List every capability record on this machine.

    This is the global view only; one actor's records and its audit history
    are read with `agent grant <ACTOR> list [--audit]`.
    """
    services = get_services()
    services._execute(
        lambda: services._daemon_request("agent.grants", {}),
        json_output=json_output,
    )
