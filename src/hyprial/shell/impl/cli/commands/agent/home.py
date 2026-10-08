"""``hyprial agent`` administration command modules."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import typer

from hyprial.kernel import ipc_errors, parse_agent_uri
from hyprial.shell.impl.cli.commands.common.services import get_services
from hyprial.shell.impl.cli.commands.common.support import JsonObject

from hyprial.shell.impl.cli.commands.agent.admin import agent_app, migration_app


@agent_app.command("home-census")
def agent_home_census(
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Measure agent-home P2 and legacy use without changing agent state."""
    services = get_services()

    services._execute(
        lambda: services._daemon_request("agent.home-census"),
        json_output=json_output,
    )

def _migration_manifest(path: Path) -> JsonObject:
    services = get_services()
    try:
        value = json.loads(path.expanduser().read_text(encoding="utf-8"))
    except OSError as error:
        raise services.CliError(
            ipc_errors.INVALID_ARGUMENT,
            f"cannot read migration manifest {path}: {error}",
        ) from error
    except json.JSONDecodeError as error:
        raise services.CliError(
            ipc_errors.INVALID_ARGUMENT,
            f"migration manifest {path} is not valid JSON: {error}",
        ) from error
    if not isinstance(value, dict):
        raise services.CliError(
            ipc_errors.INVALID_ARGUMENT,
            "migration manifest must contain a JSON object",
        )
    return value

_MIGRATION_ROUTINE_BINDING_FIELDS = (
    "owner",
    "actor",
    "produces",
    "target",
    "targetUri",
    "coordinator",
    "coordinatorUri",
)

def _routine_binding_matches_agent(value: object, requested: str) -> bool:
    if not isinstance(value, str):
        return False
    if value == requested:
        return True
    requested_uri = parse_agent_uri(requested)
    candidate_uri = parse_agent_uri(value)
    if requested_uri is not None:
        return candidate_uri == requested_uri
    return candidate_uri is not None and candidate_uri[2] == requested

def _guard_agent_migration_routines(agent: str, *, json_output: bool) -> None:
    services = get_services()
    result = services._daemon_request(
        "routine.list",
        {"all": True, **services._routine_identity()},
    )
    if not isinstance(result, dict) or not isinstance(result.get("routines"), list):
        raise services.CliError("INVALID_RESPONSE", "routine.list must return routines")

    active: list[str] = []
    for routine in result["routines"]:
        if not isinstance(routine, dict):
            raise services.CliError("INVALID_RESPONSE", "routine.list routines must be objects")
        if routine.get("enabled") is not True:
            continue
        if any(
            _routine_binding_matches_agent(routine.get(field), agent)
            for field in _MIGRATION_ROUTINE_BINDING_FIELDS
        ):
            name = routine.get("name")
            active.append(name if isinstance(name, str) and name else "<unnamed>")
    if active:
        names = ", ".join(active)
        raise services.CliError(
            "ROUTINE_ACTIVE",
            f"enabled routine(s) {names} are bound to agent {agent!r}; "
            "pause them with `hyprial routine pause <name>` and resume after "
            "the migration",
        )

@migration_app.command("preflight")
def agent_migrate_preflight(
    agent: str = typer.Argument(
        ...,
        help=(
            "Registered agent name (resident agents: pause their routines and "
            "stop them first)."
        ),
    ),
    manifest: Path = typer.Option(
        ...,
        "--manifest",
        help=(
            "JSON manifest containing the authorization window, exact source "
            "entries, and required support keys."
        ),
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Validate and privately persist one immutable migration plan."""
    services = get_services()

    def operation() -> Any:
        services = get_services()
        _guard_agent_migration_routines(agent, json_output=json_output)
        return services._daemon_request(
            "agent.migrate.preflight",
            {"agent": agent, "manifest": _migration_manifest(manifest)},
        )

    services._execute(operation, json_output=json_output)

@migration_app.command("execute")
def agent_migrate_execute(
    agent: str = typer.Argument(
        ...,
        help=(
            "Registered agent name (resident agents: pause their routines and "
            "stop them first)."
        ),
    ),
    migration_id: str = typer.Option(
        ..., "--migration-id", help="Migration id returned by preflight."
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Execute a daemon-persisted preflight plan."""
    services = get_services()

    def operation() -> Any:
        services = get_services()
        _guard_agent_migration_routines(agent, json_output=json_output)
        return services._daemon_request(
            "agent.migrate.execute",
            {"agent": agent, "migrationId": migration_id},
        )

    services._execute(operation, json_output=json_output)

@migration_app.command("rollback")
def agent_migrate_rollback(
    agent: str = typer.Argument(
        ...,
        help=(
            "Registered agent name (resident agents: pause their routines and "
            "stop them first)."
        ),
    ),
    migration_id: str = typer.Option(
        ..., "--migration-id", help="Completed migration id to roll back."
    ),
    window_id: str = typer.Option(
        ..., "--window-id", help="Fresh operator authorization window id."
    ),
    responsible_owner: str = typer.Option(
        ...,
        "--responsible-owner",
        help="Owner responsible for this rollback and any follow-up.",
    ),
    expires_at_ms: int = typer.Option(
        ...,
        "--expires-at-ms",
        help="Authorization deadline as Unix epoch milliseconds.",
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Roll back a completed plan under a fresh authorization window."""
    services = get_services()

    def operation() -> Any:
        services = get_services()
        _guard_agent_migration_routines(agent, json_output=json_output)
        return services._daemon_request(
            "agent.migrate.rollback",
            {
                "agent": agent,
                "migrationId": migration_id,
                "authorizationWindow": {
                    "windowId": window_id,
                    "responsibleOwner": responsible_owner,
                    "expiresAtMs": expires_at_ms,
                },
            },
        )

    services._execute(operation, json_output=json_output)
