"""``hyprial agent`` administration command modules."""

from __future__ import annotations

import sys
from typing import Any

import typer

from hyprial.kernel import ipc_errors
from hyprial.shell.impl.cli.commands.common.services import get_services

from hyprial.shell.impl.cli.commands.agent.admin import secret_app


@secret_app.command("provider-" + "write")
def agent_secret_entry_write(
    entry_id: str = typer.Argument(..., help="Secret entry id."),
    field_name: str = typer.Option(..., "--field", help="JSON field name."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Write one model-vendor secret from a non-TTY stdin stream."""
    services = get_services()

    def operation() -> Any:
        services = get_services()
        if sys.stdin.isatty():
            raise services.CliError(
                "SECRET_REQUIRED",
                "secret write refuses a TTY; pipe exactly one secret value on stdin",
            )
        value = sys.stdin.read()
        if value.endswith("\n"):
            value = value[:-1]
        if not value or "\n" in value or "\r" in value:
            raise services.CliError(
                ipc_errors.INVALID_ARGUMENT,
                "secret write requires one non-empty line on stdin",
            )
        return services._daemon_request(
            "agent.secret." + "provider-write",
            {"entryId": entry_id, "fieldName": field_name, "value": value},
        )

    services._execute(operation, json_output=json_output)

@secret_app.command("grant")
def agent_secret_grant(
    actor: str = typer.Argument(..., help="Agent instance name."),
    grant_id: str = typer.Option(..., "--grant-id", help="Stable grant id."),
    source: str = typer.Option(..., "--source", help="user-" + "provider or agent-private"),
    entry_id: str = typer.Option(..., "--entry-id", help="Secret entry id."),
    field_name: str = typer.Option(..., "--field-name", help="Model-vendor field name."),
    environment_name: list[str] = typer.Option(
        ..., "--environment-name", help="Approved environment variable name; repeatable."
    ),
    revision: int = typer.Option(..., "--revision", help="Positive grant revision."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Grant one named secret entry to one current agent incarnation."""
    services = get_services()

    services._execute(
        lambda: services._daemon_request(
            "agent.secret.grant",
            {
                "actor": actor,
                "grantId": grant_id,
                "source": source,
                "entryId": entry_id,
                "fieldName": field_name,
                "environmentNames": environment_name,
                "revision": revision,
            },
        ),
        json_output=json_output,
    )

@secret_app.command("list")
def agent_secret_list(
    actor: str | None = typer.Option(None, "--actor", help="Filter by agent instance."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """List non-secret grant metadata."""
    services = get_services()

    services._execute(
        lambda: services._daemon_request(
            "agent.secret.list", {**({"actor": actor} if actor else {})}
        ),
        json_output=json_output,
    )

@secret_app.command("revoke")
def agent_secret_revoke(
    actor: str = typer.Argument(..., help="Agent instance name."),
    grant_id: str = typer.Argument(..., help="Grant id."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Revoke one grant; the underlying secret entry remains intact."""
    services = get_services()

    services._execute(
        lambda: services._daemon_request(
            "agent.secret.revoke", {"actor": actor, "grantId": grant_id}
        ),
        json_output=json_output,
    )
