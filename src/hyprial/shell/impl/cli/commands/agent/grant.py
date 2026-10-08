"""``hyprial agent`` administration command modules."""

from __future__ import annotations


import typer

from hyprial.shell.impl.cli.commands.common.services import get_services

from hyprial.shell.impl.cli.commands.agent.admin import agent_app
from hyprial.shell.impl.cli.commands.common.root import app


@agent_app.command("grant")
def agent_grant(
    actor: str = typer.Argument(..., help="Agent instance name."),
    capability: str = typer.Option(..., "--capability", help="Capability name from the grant schema."),
    scope: str = typer.Option(..., "--scope", help="Single-line capability scope; arrays/paths use JSON."),
    grant_id: str | None = typer.Option(None, "--grant-id", help="Stable id; defaults to a new UUID."),
    revision: int = typer.Option(1, "--revision", help="Must increase when updating an existing id."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Record a capability grant. Recording does not enforce runtime permissions."""
    services = get_services()
    services._execute(
        lambda: services._daemon_request("agent.grant", {
            "actor": actor, "capability": capability, "scope": scope,
            "grantId": grant_id if grant_id is not None else str(services.uuid4()), "revision": revision,
        }), json_output=json_output,
    )

@agent_app.command("revoke")
def agent_revoke(
    actor: str = typer.Argument(..., help="Agent instance name."),
    grant_id: str = typer.Argument(..., help="Grant id returned by agent grant."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Remove an active capability record and retain its audit history."""
    services = get_services()
    services._execute(
        lambda: services._daemon_request("agent.revoke", {"actor": actor, "grantId": grant_id}),
        json_output=json_output,
    )

@agent_app.command("grants")
def agent_grants(
    actor: str | None = typer.Argument(None, help="Agent name; required with --audit."),
    audit: bool = typer.Option(False, "--audit", help="Show history, including destroyed incarnations."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """List capability records, or an agent's append-only audit history."""
    services = get_services()
    services._execute(
        lambda: services._daemon_request("agent.grants", {
            **({"actor": actor} if actor is not None else {}), "audit": audit,
        }), json_output=json_output,
    )

@app.command("grants-check")
def grants_check(
    capability: str = typer.Option(
        ...,
        "--capability",
        help="Frozen capability: tool-surface, channel, shared-path, org-context, isolation, agent-home.",
    ),
    resource: str = typer.Option(
        ...,
        "--resource",
        help="What is being used: tool name, channel, path, isolation mode, or agent-home URI.",
    ),
    caller: str | None = typer.Option(
        None,
        "--caller",
        help="Caller principal URI; omit for the host-local operator (unrestricted).",
    ),
    hosted: bool = typer.Option(
        False, "--hosted", help="Treat the caller as a hosted visitor."
    ),
    unverified: bool = typer.Option(
        False, "--unverified", help="Mark the identity unverified; the verdict is a refusal."
    ),
    write: bool = typer.Option(
        False, "--write", help="For shared-path: check write access instead of read."
    ),
    scope: list[str] | None = typer.Option(
        None, "--scope", help="Ledger scope JSON for one grant record (repeatable)."
    ),
    revision: int = typer.Option(
        1, "--revision", help="Revision stamped on the --scope records."
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Decide one capability use and name the entry point that owns it (AT10)."""
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
                            capability=capability,
                            scope=raw,
                            revision=revision,
                        )
                    )
            if capability == "shared-path":
                verdict = check_shared_path(
                    resource, write=write, caller=identity, records=records
                )
            else:
                verdict = explain(capability, resource, caller=identity, records=records)
        except EnforcementError as error:
            raise services.CliError(error.code, str(error)) from error
        payload = verdict.as_dict()
        payload["entryPoints"] = dict(CAPABILITY_ENTRY_POINTS)
        return payload

    services._execute(operation, json_output=json_output)

@app.command("grants-visibility")
def grants_visibility(
    caller: str | None = typer.Option(
        None,
        "--caller",
        help="Caller principal URI; omit for the host-local operator (unrestricted).",
    ),
    hosted: bool = typer.Option(
        False,
        "--hosted",
        help="Treat the caller as a hosted visitor (no grant means self only).",
    ),
    unverified: bool = typer.Option(
        False,
        "--unverified",
        help="Mark the identity as unverified; every target is then refused.",
    ),
    see_actors: str | None = typer.Option(
        None,
        "--see-actors",
        help="see-actors scope as a JSON array of principal URIs.",
    ),
    send_to: str | None = typer.Option(
        None,
        "--send-to",
        help="send-to scope as a JSON array of principal URIs.",
    ),
    target: list[str] | None = typer.Option(
        None,
        "--target",
        help="Candidate target URI to decide (repeatable).",
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Decide see-actors / send-to for a verified caller (AT09 decision core)."""
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
                    GrantRecord(actor=caller, capability=SEE_ACTORS, scope=see_actors)
                )
            if send_to is not None and caller is not None:
                grants.append(
                    GrantRecord(actor=caller, capability=SEND_TO, scope=send_to)
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
