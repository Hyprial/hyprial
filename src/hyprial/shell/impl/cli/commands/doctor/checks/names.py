"""Read-only diagnostics for legacy agent/adapter name collisions.

Decision: a bootstrap refusal code makes the check NOT-EVALUABLE only when the
daemon's probed phase is ``bootstrap`` (the ``daemon status`` pattern).  Dev
tolerated nothing but ``DaemonUnavailableError`` (offline read); a serving
daemon's refusal (``CALLER_NOT_AUTHORIZED``, ``METHOD_NOT_FOUND``), invalid
response, internal error or restoring state, and a failed probe, still abort
doctor loudly rather than become a warn with a wrong explanation.

Decision (PR 2941 second review): the owner's user agent (reserved name
``hyprial-user``, marked by the ``template: user`` capability fact) is not
a collision.  It has no harness and no desired-state row and is never
started or stopped by name, so it cannot cause the adapter/worker confusion
this check exists for.  Online, ``agent.list`` already leaves it out;
offline, the registry read drops the names of marked records.  An ordinary
agent that holds the name is still reported in both modes.  ``agent.list``
rows carry the local name as a bare ``actor``; it counts as the name.
"""
import json
import sqlite3

from hyprial.kernel import ipc_errors, parse_agent_uri
from hyprial.shell.impl.cli.commands.common.services import get_services
from hyprial.shell.impl.cli.commands.daemon.commands import (
    BOOTSTRAP_REFUSAL_CODES,
    confirmed_bootstrap_refusal,
)


USER_AGENT_MARKER = ("template", "user")
"""The capability fact of the owner's user agent; ``agent.list`` hides its record."""


def _agent_name(row: object) -> str | None:
    if not isinstance(row, dict):
        return None
    name = row.get("name")
    if isinstance(name, str) and name:
        return name
    actor = row.get("actor")
    if isinstance(actor, str) and actor and ":" not in actor:
        return actor
    if isinstance(actor, str) and actor:
        parsed = parse_agent_uri(actor)
        return parsed[2] if parsed is not None else None
    return None


def _online_names(services) -> tuple[set[str], set[str]]:
    agents = services.runtime.request("agent.list", {})
    adapters = services.runtime.request("adapter.list", {})
    if not isinstance(agents, dict) or not isinstance(agents.get("agents"), list):
        raise services.CliError("INVALID_RESPONSE", "agent.list must return agents")
    if not isinstance(adapters, dict) or not isinstance(adapters.get("adapters"), list):
        raise services.CliError("INVALID_RESPONSE", "adapter.list must return adapters")
    agent_names = {name for row in agents["agents"] if (name := _agent_name(row))}
    adapter_names = {
        row["name"]
        for row in adapters["adapters"]
        if isinstance(row, dict) and isinstance(row.get("name"), str) and row["name"]
    }
    return agent_names, adapter_names


def _offline_names(services) -> tuple[set[str], set[str]]:
    """B06 offline-only read; callers must have observed DAEMON_UNAVAILABLE."""

    from hyprial.identity import AgentRegistry
    from hyprial.kernel import ChannelConfiguration

    path = services._hyprial_home() / "channels.json"
    if not path.exists():
        return set(), set()
    channels = ChannelConfiguration.from_json(json.loads(path.read_text(encoding="utf-8")))
    database = services._state_dir() / "agents.sqlite3"
    names = AgentRegistry.local_names(database)
    hidden = AgentRegistry.local_names_with_capability(database, *USER_AGENT_MARKER)
    return set(names) - set(hidden), {item.name for item in channels.gateways}


def name_collision_check() -> dict[str, object] | None:
    services = get_services()
    try:
        agent_names, adapter_names = _online_names(services)
    except ipc_errors.DaemonUnavailableError:
        try:
            agent_names, adapter_names = _offline_names(services)
        except (OSError, ValueError, sqlite3.Error) as offline_error:
            return {"name": "agent-adapter-name-collision", "status": "warn",
                    "detail": f"Could not check local name collisions: {type(offline_error).__name__}"}
    except services.CliError as refusal:
        if refusal.code not in BOOTSTRAP_REFUSAL_CODES:
            raise
        confirmed_bootstrap_refusal(services, refusal)
        return {"name": "agent-adapter-name-collision", "status": "warn",
                "detail": "NOT-EVALUABLE: the daemon refused to list agents and adapters "
                          f"({refusal.code}); a daemon without an account (bootstrap) cannot"}
    collisions = sorted(agent_names & adapter_names)
    if not collisions:
        return None
    return {
        "name": "agent-adapter-name-collision",
        "status": "warn",
        "detail": "Local agent and adapter share a name: " + ", ".join(collisions)
                  + ". Recreate each adapter as <name>-feishu; doctor does not rename it.",
    }
