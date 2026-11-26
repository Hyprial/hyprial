"""``hyprial agent`` administration command modules."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import typer

from hyprial.error import codes
from hyprial.shell.impl.cli.commands.common.services import get_services
from hyprial.shell.impl.cli.commands.common.support import JsonObject

from hyprial.shell.impl.cli.commands.agent.admin import agent_app
from hyprial.shell.impl.cli.commands.agent.provider.commands import seed_new_agent
from hyprial.shell.impl.cli.commands.agent.model import configuration_input


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
        help="Model vendor preference (the harness/provider/model vocabulary).",
    ),
    model: str | None = typer.Option(None, "--model", help="Model id preference."),
    connection: str | None = typer.Option(None, "--connection", help="Saved connection reference; disables default credentials."),
    config_revision: int | None = typer.Option(None, "--config-revision", help="Saved connection revision; required only with --connection."),
    native_auth: str | None = typer.Option(None, "--native-auth", help="Native login slot for this agent; never copies another login."),
    operation_id: str | None = typer.Option(None, "--operation-id", help="Unique model configuration operation ID."),
    preferred_harness: str | None = typer.Option(
        None,
        "--preferred-harness",
        help=(
            "Default harness for 'hyprial agent start'. Only a default: the agent is not "
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

    Credentials: a captured Claude default ('agent provider login claude
    --actor OWNER --setup-token --default') grants its token to new agents with
    --preferred-harness claude. A captured native default copies the named
    owner's login instead. PAC and lifecycle creation use the same policy.
    Legacy provider-name defaults still seed from host login in this CLI only;
    re-run provider login --default --actor OWNER to bind a Claude source for PAC.
    Explicit --connection/--native-auth disables all defaults. Supply --model,
    --preferred-harness and --operation-id. Creation does not start a process.
    A missing native login leaves a visible unconfigured agent; log in to that
    agent, then use agent model configure. Do not repeat agent create.
    """
    services = get_services()

    def operation() -> Any:
        services = get_services()
        params: JsonObject = {"name": name}
        explicit = any(value is not None for value in (connection, native_auth, config_revision, operation_id))
        if explicit:
            if provider is not None or preferred_harness is None or model is None or operation_id is None:
                raise services.CliError(codes.INVALID_ARGUMENT,
                    "Explicit model creation requires --preferred-harness, --model and --operation-id, without --provider.")
            params["modelConfiguration"] = {"operationId": operation_id,
                "configuration": configuration_input(connection=connection, config_revision=config_revision,
                    native_auth=native_auth, harness=preferred_harness, mode="headless", model=model)}
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
            params["provider"] = provider
        if model is not None and not explicit:
            params["model"] = model
        if preferred_harness is not None:
            params["preferredHarness"] = preferred_harness
        created = services.runtime.request("agent.create", params)
        if not isinstance(created, dict) or explicit:
            return created
        seed = seed_new_agent(
            services,
            name,
            created.get("providerDefault"),
            json_output=json_output,
            preferred_harness=preferred_harness,
            model_provider=provider,
        )
        return {**created, "providerSeed": seed}

    services.runtime.execute(operation, json_output=json_output)

def _create_agent_for_start(
    *,
    name: str,
    harness: str,
    runtime: str,
    cwd: Path,
    provider: str | None = None,
    model: str | None = None,
    include_resume_snapshot: bool = False,
) -> JsonObject:
    """Route ``hyprial agent start`` through ``agent create`` before launching anything.

    Decision A5 keeps ``hyprial agent start`` working exactly as before but makes it go
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
    if include_resume_snapshot:
        params["includeResumeSnapshot"] = True
    return services.runtime.request("agent.create", params)
