"""``hyprial agent`` administration command modules."""

from __future__ import annotations

from typing import Any

import typer

from hyprial.shell.impl.cli.commands.common.services import get_services
from hyprial.shell.impl.cli.output import confirm


agent_app = typer.Typer(
    help=(
        "Create, inspect and destroy the agents registered on this machine. "
        "An agent is an identity plus its configuration; the harness it runs "
        "on is a runtime binding, not part of the agent, so the same agent "
        "can be started on claude today and pi tomorrow."
    )
)

secret_app = typer.Typer(
    help="Manage explicit per-agent secret grants without exposing values."
)

migration_app = typer.Typer(
    help="Preflight, execute, and roll back one authorized agent-home migration.",
    no_args_is_help=True,
)

agent_keep_app = typer.Typer(
    help="Protect named agents from inactivity reports.",
)

@agent_app.command("list")
def agent_list(
    inactive_since: str | None = typer.Option(
        None,
        "--inactive-since",
        help="Show only agents with no recorded or hinted activity in this duration.",
    ),
    exclude_wf: bool = typer.Option(
        False, "--exclude-wf", help="Exclude wf-* workflow workers."
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """List this machine's agents with their real online/offline status."""
    services = get_services()

    services._execute(
        lambda: services._daemon_request(
            "agent.list",
            {
                **(
                    {"inactiveSince": inactive_since}
                    if inactive_since is not None
                    else {}
                ),
                **({"excludeWf": True} if exclude_wf else {}),
            },
        ),
        json_output=json_output,
    )

@agent_app.command("unblock")
def agent_unblock(
    name: str = typer.Argument(..., help="Blocked agent name."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Release a durable agent block so its next delivery may proceed."""
    services = get_services()

    services._execute(
        lambda: services._daemon_request("agent.unblock", {"name": name}),
        json_output=json_output,
    )

@agent_app.command("destroy")
def agent_destroy(
    name: str = typer.Argument(..., help="Actor name of the agent to destroy."),
    yes: bool = typer.Option(
        False, "--yes", help="Confirm this irreversible deletion."
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Permanently delete an agent. THIS CANNOT BE UNDONE.

    Destroying an agent stops its connectors, deletes its record, and discards
    its undelivered messages. There is no tombstone and no revival: messages
    already sent to this agent lose a resolvable recipient, and recreating the
    same name later gives you a new, empty agent -- not this one back.
    Interactive terminals may confirm after seeing the workspace inventory;
    non-interactive callers must pass --yes.
    """
    services = get_services()

    def operation() -> Any:
        services = get_services()
        if not yes:
            if json_output or not services._stdin_isatty():
                raise services.CliError(
                    "CONFIRMATION_REQUIRED",
                    f"destroying agent {name!r} from a non-interactive command "
                    "requires --yes",
                )
            preview = services._daemon_request("agent.destroy.preview", {"name": name})
            workspace = preview.get("workspace")
            if not isinstance(workspace, dict):
                raise services.CliError(
                    "INVALID_RESPONSE",
                    "agent.destroy.preview must return a workspace inventory",
                )
            files = workspace.get("files")
            size = workspace.get("bytes")
            if not isinstance(files, int) or not isinstance(size, int):
                raise services.CliError(
                    "INVALID_RESPONSE",
                    "agent.destroy.preview returned an invalid workspace inventory",
                )
            prompt = (
                f"将删除 workspace({files} 个文件、{size} 字节)，以及 agent "
                f"{name!r} 的记录、连接器和未送达消息。继续？"
            )
            if not confirm(prompt):
                raise services.CliError("CANCELLED", "agent destroy cancelled")
        return services._daemon_request("agent.destroy", {"name": name})

    services._execute(operation, json_output=json_output)
