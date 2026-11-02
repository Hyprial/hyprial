"""Read-only diagnostics for legacy agent/adapter name collisions."""
import json
import sqlite3

from hyprial.kernel import ipc_errors, parse_agent_uri
from hyprial.shell.impl.cli.commands.common.services import get_services


def _agent_name(row: object) -> str | None:
    if not isinstance(row, dict):
        return None
    name = row.get("name")
    if isinstance(name, str) and name:
        return name
    actor = row.get("actor")
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
    names = AgentRegistry.local_names(services._state_dir() / "agents.sqlite3")
    return set(names), {item.name for item in channels.gateways}


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
    collisions = sorted(agent_names & adapter_names)
    if not collisions:
        return None
    return {
        "name": "agent-adapter-name-collision",
        "status": "warn",
        "detail": "Local agent and adapter share a name: " + ", ".join(collisions)
                  + ". Recreate each adapter as <name>-feishu; doctor does not rename it.",
    }
