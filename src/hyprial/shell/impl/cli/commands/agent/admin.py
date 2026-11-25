"""``hyprial agent`` administration command modules."""

from __future__ import annotations

from typing import Any

import typer
from hyprial.shell.impl.cli.commands.common.groups import group

from hyprial.shell.impl.cli.commands.common.services import get_services
from hyprial.shell.impl.cli.output import CliResult, confirm
from hyprial.shell.impl.cli.output.status import render_agent_list, render_agent_row


agent_app = group(
    help=(
        "Create, inspect and destroy the agents registered on this machine. "
        "An agent is an identity plus its configuration; the harness it runs "
        "on is a runtime binding, not part of the agent, so the same agent "
        "can be started on claude today and pi tomorrow."
    )
)

secret_app = group(
    help="Manage explicit per-agent secret grants without exposing values."
)

migration_app = group(
    help="Preflight, execute, and roll back one authorized agent-home migration.",
    no_args_is_help=True,
)

home_app = group(
    help="Inspect and migrate the agent home directories on this machine.",
    no_args_is_help=True,
)

config_app = group(
    help="Validate, atomically replace, or roll back an existing P2 config root.",
    no_args_is_help=True,
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
    include_user_agent: bool = typer.Option(
        False,
        "--include-user-agent",
        help="Also list this machine's user agent (your own agent, no harness).",
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """List local agents, presence and scoped cell run diagnostics.

    Missing runState means unsupported, not idle. Permission coverage=false
    means the carrier cannot report permission waits, not that none exist.
    """
    services = get_services()

    def operation() -> CliResult:
        result = services.runtime.request(
            "agent.list",
            {
                **(
                    {"inactiveSince": inactive_since}
                    if inactive_since is not None
                    else {}
                ),
                **({"excludeWf": True} if exclude_wf else {}),
                **({"includeUserAgent": True} if include_user_agent else {}),
            },
        )
        if (
            not isinstance(result, dict)
            or not isinstance(result.get("agents"), list)
            or not all(isinstance(row, dict) for row in result["agents"])
        ):
            raise services.CliError(
                "INVALID_RESPONSE", "daemon agent.list result must contain agent rows"
            )
        return CliResult(result, render=render_agent_list)

    services.runtime.execute(operation, json_output=json_output)

@agent_app.command("unblock")
def agent_unblock(
    name: str = typer.Argument(..., help="Blocked agent name."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Release a durable agent block so its next delivery may proceed."""
    services = get_services()

    services.runtime.execute(
        lambda: services.runtime.request("agent.unblock", {"name": name}),
        json_output=json_output,
    )

@agent_app.command("rename")
def agent_rename(
    name: str = typer.Argument(..., help="The agent: name, display name, a.<id> or agent URI."),
    display_name: str = typer.Argument(..., help="The new display name."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Change an agent's display name; its id, address, home and grants stay."""
    services = get_services()

    def operation() -> CliResult:
        result = services.runtime.request(
            "agent.rename", {"name": name, "displayName": display_name}
        )
        if not isinstance(result, dict) or not isinstance(result.get("agent"), dict):
            raise services.CliError(
                "INVALID_RESPONSE", "daemon agent.rename result must contain the agent row"
            )
        return CliResult(result, render=render_agent_row)

    services.runtime.execute(operation, json_output=json_output)

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
        if not yes and (json_output or not services._stdin_isatty()):
            raise services.CliError(
                "CONFIRMATION_REQUIRED",
                f"destroying agent {name!r} from a non-interactive command requires --yes",
            )
        preview = services.runtime.request("agent.destroy.preview", {"name": name})
        operation_id = preview.get("operationId")
        workspace = preview.get("workspace")
        if not isinstance(operation_id, str) or not operation_id or not isinstance(workspace, dict):
            raise services.CliError("INVALID_RESPONSE", "deletion preview must return operationId and workspace")
        files = workspace.get("files")
        size = workspace.get("bytes")
        if type(files) is not int or type(size) is not int or files < 0 or size < 0:
            raise services.CliError("INVALID_RESPONSE", "deletion preview returned an invalid workspace inventory")
        if not yes:
            prompt = (
                f"将删除 workspace({files} 个文件、{size} 字节)，以及 agent "
                f"{name!r} 的记录、连接器和未送达消息。继续？"
            )
            if not confirm(prompt):
                raise services.CliError("CANCELLED", "agent destroy cancelled")
        return services.runtime.request("agent.destroy", {"operationId": operation_id})

    services.runtime.execute(operation, json_output=json_output)
