"""Read-only diagnostics for legacy agent/adapter name collisions."""
import json
import sqlite3

from hyprial.identity import AgentRegistry
from hyprial.kernel import ChannelConfiguration
from hyprial.shell.impl.cli.commands.common.services import get_services


def name_collision_check() -> dict[str, object] | None:
    services = get_services()
    path = services._hyprial_home() / "channels.json"
    if not path.exists():
        return None
    try:
        channels = ChannelConfiguration.from_json(json.loads(path.read_text(encoding="utf-8")))
        names = AgentRegistry.local_names(services._state_dir() / "agents.sqlite3")
    except (OSError, ValueError, sqlite3.Error) as error:
        return {"name": "agent-adapter-name-collision", "status": "warn",
                "detail": f"Could not check local name collisions: {type(error).__name__}"}
    collisions = sorted(names & {item.name for item in channels.gateways})
    if not collisions:
        return None
    return {
        "name": "agent-adapter-name-collision",
        "status": "warn",
        "detail": "Local agent and adapter share a name: " + ", ".join(collisions)
                  + ". Recreate each adapter as <name>-feishu; doctor does not rename it.",
    }
