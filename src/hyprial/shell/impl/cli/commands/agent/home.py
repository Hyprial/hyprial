"""``hyprial agent`` administration command modules."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import typer

from hyprial.kernel import DaemonRequestError, ipc_errors, parse_agent_uri
from hyprial.shell.impl.cli.commands.common.services import get_services
from hyprial.shell.impl.cli.commands.common.support import JsonObject

from hyprial.shell.impl.cli.commands.agent.admin import agent_app, home_app, migration_app


@home_app.command("census")
def agent_home_census(
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Measure agent-home P2 and legacy use without changing agent state."""
    services = get_services()

    services._execute(
        lambda: services._daemon_request("agent.home-census"),
        json_output=json_output,
    )

@agent_app.command("readiness")
def agent_readiness(
    name: str = typer.Argument(..., help="Registered agent name."),
    role: str = typer.Option(
        ...,
        "--role",
        help="Explicit capability template: developer or pac.",
    ),
    harness: str = typer.Option(
        None,
        "--harness",
        help="Harness to check; defaults to the agent's preferred/last harness.",
    ),
    cwd: str = typer.Option(
        None,
        "--cwd",
        help="Workspace to check; defaults to the agent's registered cwd.",
    ),
    tools: list[str] = typer.Option(
        None,
        "--tool",
        help=(
            "Additional required tool as NAME or NAME=/absolute/path. Repeatable; "
            "only declared capabilities are checked."
        ),
    ),
    work_paths: list[str] = typer.Option(
        None,
        "--work-path",
        help="Additional required absolute work path. Repeatable.",
    ),
    forgejo_identity: bool = typer.Option(
        False,
        "--forgejo-identity",
        help=(
            "Opt in to a bounded fj whoami identity probe using the agent's own "
            "HOME/XDG. Credentials are never read, copied, or printed."
        ),
    ),
    expect_login: str = typer.Option(
        None,
        "--expect-login",
        help="Expected Forgejo login (or login@host) for the opt-in identity probe.",
    ),
    forgejo_host: str = typer.Option(
        None,
        "--forgejo-host",
        help="Declared Forgejo hostname; defaults to HYPRIAL_TEA_LOGIN.",
    ),
    timeout_seconds: float = typer.Option(
        8.0,
        "--timeout",
        min=0.1,
        max=60.0,
        help="Bound for each external probe.",
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Read-only start preflight in the actual agent HOME/XDG/PATH.

    Exits 1 when the agent is not ready.  UNKNOWN is not ready: unavailable
    permission or config introspection is never inferred as permission.  This
    command does not write config/settings, create a home, restart a service,
    or interrupt an active turn.
    """
    services = get_services()
    params: JsonObject = {
        "name": name,
        "role": role,
        "tools": list(tools or []),
        "workPaths": list(work_paths or []),
        "forgejoIdentity": forgejo_identity,
        "timeoutSeconds": timeout_seconds,
    }
    if harness is not None:
        params["harness"] = harness
    if cwd is not None:
        params["cwd"] = cwd
    if expect_login is not None:
        params["expectLogin"] = expect_login
    if forgejo_host is not None:
        params["forgejoHost"] = forgejo_host

    def operation() -> JsonObject:
        result = services._daemon_request("agent.readiness", params)
        if not isinstance(result, dict):
            raise services.CliError(
                "INVALID_RESPONSE", "agent.readiness must return an object"
            )
        # The command is a gate: not-ready is an honest failing exit status,
        # while the report keeps its own ready/ok distinction in JSON.
        return {**result, "ok": bool(result.get("ready"))}

    services._execute(operation, json_output=json_output)


@agent_app.command("ensure-home")
def agent_ensure_home(
    names: list[str] = typer.Argument(
        None,
        help=(
            "One or more registered agent names. Omit with --all-legacy to "
            "select from the census instead."
        ),
    ),
    all_legacy: bool = typer.Option(
        False,
        "--all-legacy",
        help=(
            "Select every registered agent whose home row is missing "
            "(homeProvisioned=false), excluding wf-* workers unless "
            "--include-wf is given."
        ),
    ),
    include_wf: bool = typer.Option(
        False,
        "--include-wf",
        help="Let --all-legacy select wf-* workflow workers too.",
    ),
    include_running: bool = typer.Option(
        False,
        "--include-running",
        help=(
            "Also provision agents with a live binding. By default only "
            "offline agents are provisioned and running ones are listed "
            "as deferred."
        ),
    ),
    limit: int = typer.Option(
        None,
        "--limit",
        min=1,
        help="Provision at most N agents in this run.",
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Show the selection without changing anything."
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Provision the official P2 agent home for existing (legacy) agents.

    Walks the registry's two-phase provision (0700 home tree, 0600 receipt
    mirror, durable lifecycle row) -- the same path a fresh ``agent create``
    takes, not a direct database write. Idempotent per agent: an already
    provisioned home is validated and reported as skipped.
    """
    services = get_services()

    def operation() -> Any:
        services = get_services()
        selected: list[str] = list(names or [])
        deferred: list[str] = []
        running: dict[str, bool] = {}
        if all_legacy:
            if selected:
                raise services.CliError(
                    ipc_errors.INVALID_ARGUMENT,
                    "pass either agent names or --all-legacy, not both",
                )
            census = services._daemon_request("agent.home-census")
            if not isinstance(census, dict) or not isinstance(
                census.get("agents"), list
            ):
                raise services.CliError(
                    "INVALID_RESPONSE", "agent.home-census must return agents"
                )
            live = _agent_running_status(services)
            selected = [
                str(row["actor"])
                for row in census["agents"]
                if isinstance(row, dict)
                and row.get("homeProvisioned") is False
                and (include_wf or not str(row.get("actor", "")).startswith("wf-"))
            ]
            running = {
                actor: live.get(actor, False) for actor in selected
            }
            if not include_running:
                deferred = sorted(
                    actor for actor in selected if running.get(actor)
                )
                selected = [
                    actor for actor in selected if not running.get(actor)
                ]
        if limit is not None:
            overflow = selected[limit:]
            deferred = overflow + [
                actor for actor in deferred if actor not in overflow
            ]
            selected = selected[:limit]
        if dry_run:
            return {
                "ok": True,
                "dryRun": True,
                "selected": selected,
                "deferred": deferred,
                "running": {a: v for a, v in running.items() if v},
            }
        results: list[JsonObject] = []
        failures = 0
        for actor in selected:
            try:
                outcome = services._daemon_request(
                    "agent.ensure-home", {"name": actor}
                )
            except DaemonRequestError as error:
                failures += 1
                results.append(
                    {"actor": actor, "ok": False, "error": str(error)}
                )
                continue
            results.append(
                {
                    "actor": actor,
                    "ok": True,
                    "home": outcome.get("home"),
                    "agent": outcome.get("agent"),
                }
            )
        return {
            "ok": failures == 0,
            "provisioned": sum(1 for item in results if item.get("ok")),
            "failed": failures,
            "deferred": deferred,
            "results": results,
        }

    services._execute(operation, json_output=json_output)

def _agent_running_status(services: Any) -> dict[str, bool]:
    """Actor -> has-live-binding, from the ordinary agent list read."""

    listing = services._daemon_request("agent.list", {})
    if not isinstance(listing, dict) or not isinstance(listing.get("agents"), list):
        raise services.CliError(
            "INVALID_RESPONSE", "agent.list must return agents"
        )
    status: dict[str, bool] = {}
    for row in listing["agents"]:
        if not isinstance(row, dict):
            continue
        actor = row.get("actor")
        if isinstance(actor, str):
            status[actor] = row.get("status") not in (None, "offline")
    return status

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
