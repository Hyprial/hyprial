"""``hyprial agent`` administration command modules."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import typer

from hyprial.shell.impl.cli.commands.common.services import get_services
from hyprial.shell.impl.cli.commands.common.support import JsonObject

from hyprial.shell.impl.cli.commands.agent.admin import agent_app


@agent_app.command("create")
def agent_create(
    name: str = typer.Option(..., "--name", help="Actor name, unique on this machine."),
    cwd: Path | None = typer.Option(
        None,
        "--cwd",
        help="Default working directory (defaults to the agent home's workspace/).",
    ),
    config: Path | None = typer.Option(
        None,
        "--config",
        help=(
            "Single explicit personality config directory (C); defaults to the "
            "agent home's minimal config/."
        ),
    ),
    provider: str | None = typer.Option(
        None,
        "--provider",
        help="Model vendor preference (the squire harness/provider/model vocabulary).",
    ),
    model: str | None = typer.Option(None, "--model", help="Model id preference."),
    preferred_harness: str | None = typer.Option(
        None,
        "--preferred-harness",
        help=(
            "Default harness for 'hyprial start'. Only a default: the agent is not "
            "bound to it and may be started on any harness."
        ),
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Register a new agent on this machine.

    The name is the agent's identity within agent:<owner>:<machine>:, and only
    one agent may hold it -- creating a second agent under a name already in
    use fails, whether or not anything is currently running under it. The name
    is freed only by 'hyprial agent destroy'.

    Without --config, creation materializes a minimal explicit config in the
    agent home so claude, codex, and pi use agent-home P2. Without --cwd, the
    agent home's workspace directory is used.

    A name says nothing about a harness: an agent called 'pi-ds4' is just an
    agent called 'pi-ds4', and it can run on claude. To move an existing agent
    to another harness, start it there; that rebinds the same agent rather
    than creating a new one, so it is not a name conflict.
    """
    services = get_services()

    def operation() -> Any:
        services = get_services()
        params: JsonObject = {"name": name}
        if cwd is not None:
            params["cwd"] = str(cwd.expanduser().resolve())
        if config is not None:
            params["config"] = {
                "sources": [
                    {"path": str(config.expanduser().resolve()), "required": True}
                ],
                "discovery": "explicit-only",
            }
        if provider is not None:
            # Wire key for the model vendor, as squire spells it.
            params["provider"] = provider
        if model is not None:
            params["model"] = model
        if preferred_harness is not None:
            params["preferredHarness"] = preferred_harness
        return services._daemon_request("agent.create", params)

    services._execute(operation, json_output=json_output)

@agent_app.command("host-invite")
def agent_host_invite(
    name: str = typer.Argument(..., help="Actor name, unique on this host."),
    owner: str = typer.Option(..., "--owner", help="Visitor's owner identity, asserted by this host."),
    cwd: Path | None = typer.Option(None, "--cwd", help="Default working directory."),
    preferred_harness: str | None = typer.Option(None, "--preferred-harness", help="Preferred harness for later starts."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Register a trusted visitor's agent; does not launch or isolate a worker."""
    services = get_services()

    def operation() -> Any:
        services = get_services()
        params: JsonObject = {"name": name, "owner": owner}
        if cwd is not None:
            params["cwd"] = str(cwd.expanduser().resolve())
        if preferred_harness is not None:
            params["preferredHarness"] = preferred_harness
        return services._daemon_request("agent.host-invite", params)

    services._execute(operation, json_output=json_output)

def _create_agent_for_start(
    *,
    name: str,
    harness: str,
    runtime: str,
    cwd: Path,
    provider: str | None = None,
    model: str | None = None,
) -> JsonObject:
    """Route ``hyprial start`` through ``agent create`` before launching anything.

    Decision A5 keeps ``hyprial start`` working exactly as before but makes it go
    through agent creation, so a connector can never come up without an
    identity behind it. ``existing: reuse`` is what separates the two entry
    points: an explicit ``hyprial agent create`` on a taken name is an error, while
    ``start`` on an existing agent is the normal case -- it is starting *that*
    agent, possibly on a different harness than last time.

    Failing here also means a duplicate is refused before a TUI is spawned.
    """
    services = get_services()

    params: JsonObject = {
        "name": name,
        "existing": "reuse",
        "harness": harness,
        "runtime": runtime,
        "cwd": str(cwd),
    }
    if provider is not None:
        params["provider"] = provider
    if model is not None:
        params["model"] = model
    return services._daemon_request("agent.create", params)
