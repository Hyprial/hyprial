# Decision record (provider-key contract freeze r3, docs/design/provider-key-contract-freeze-2026-11-03.md §5-§6)
# - allow/revoke/status are thin calls to the agent.provider.* owner-key methods.
# - The CLI option is --actor, but the IPC param is always "agent": an "actor" key makes
#   the router treat the caller as an agent, and preauthorize refuses it.
# - Worker shells are refused before any IPC with the same rule as `user provider`.
# - revoke does no controlled restart in this phase; a running agent is reported by the
#   daemon as restart.state "not-supported" and the CLI warns on stderr.
"""``hyprial agent provider allow|revoke|status``: grant your own keys to agents."""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import typer

from hyprial.shell.impl.cli.commands.agent.provider.commands import (
    provider_app,
    refuse_owner_key_in_worker_shell,
    run_owner_key_request,
)
from hyprial.shell.impl.cli.commands.common.services import get_services
from hyprial.shell.impl.cli.output import CliResult, warn


def _render_grant(grant: Mapping[str, Any]) -> str:
    names = ",".join(grant.get("environmentNames") or []) or "-"
    return (
        f"{grant.get('actor')} {grant.get('grantId')} {grant.get('state')} "
        f"key={grant.get('keyId') or '-'} env={names} seq={grant.get('seq')} "
        f"expiresAt={grant.get('expiresAt') if grant.get('expiresAt') is not None else '-'}"
    )


def _render_allow(data: Mapping[str, Any]) -> str:
    verb = "Allowed" if data.get("changed") else "Already allowed"
    return f"{verb}: " + _render_grant(data.get("grant") or {})


def _render_revoke(data: Mapping[str, Any]) -> str:
    verb = "Revoked" if data.get("changed") else "Already revoked"
    return f"{verb}: " + _render_grant(data.get("grant") or {})


def _render_status(data: Mapping[str, Any]) -> str:
    lines = [f"{data.get('actor')} ({data.get('agentId')})"]
    for slot in data.get("grants") or []:
        code = f" {slot.get('code')}" if slot.get("code") else ""
        lines.append(
            f"  {slot.get('grantId')} {slot.get('provider')} {slot.get('state')}{code} "
            f"key={slot.get('keyId') or '-'} unsealable={'yes' if slot.get('unsealable') else 'no'}"
        )
    if len(lines) == 1:
        lines.append("  no grants")
    return "\n".join(lines)


@provider_app.command("allow")
def agent_provider_allow(
    actor: str = typer.Option(..., "--actor", help="Agent name, a.<id> or agent URI to grant to."),
    provider: str = typer.Option(..., "--provider", help="Model vendor whose key is granted."),
    key_id: str | None = typer.Option(
        None, "--key", help="Key id (pk.…); optional when you hold one active key for the vendor."
    ),
    env: list[str] | None = typer.Option(
        None, "--env", help="Environment variable name the key is injected as; repeatable."
    ),
    replace: bool = typer.Option(
        False, "--replace", help="Replace a grant of a different key the agent already holds."
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Let an agent use one of your stored keys; re-running is idempotent.

    The key value is never read or shown here. Revoke with 'hyprial agent
    provider revoke --actor <agent> --provider <vendor>'.
    """

    def build(_services: Any) -> dict[str, Any]:
        params: dict[str, Any] = {"agent": actor, "provider": provider}
        if key_id is not None:
            params["keyId"] = key_id
        if env:
            params["environmentNames"] = list(env)
        if replace:
            params["replace"] = True
        return params

    run_owner_key_request("agent.provider.allow", build, _render_allow, json_output)


@provider_app.command("revoke")
def agent_provider_revoke(
    actor: str = typer.Option(..., "--actor", help="Agent name, a.<id> or agent URI to revoke from."),
    provider: str = typer.Option(..., "--provider", help="Model vendor whose grant is revoked."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Revoke an agent's grant for one model vendor; re-running is idempotent.

    A running agent keeps the key it already loaded until it restarts; the
    command warns on stderr when that applies.
    """
    services = get_services()

    def operation() -> CliResult:
        refuse_owner_key_in_worker_shell(services)
        result = services.runtime.request("agent.provider.revoke", {"agent": actor, "provider": provider})
        if (result.get("restart") or {}).get("state") == "not-supported":
            warn(
                f"agent {actor} is running and keeps the revoked key until it restarts; "
                "restart it to drop the key now",
                json_output=json_output,
            )
        return CliResult({"ok": True, **result}, render=_render_revoke)

    services.runtime.execute(operation, json_output=json_output)


@provider_app.command("status")
def agent_provider_status(
    actor: str = typer.Option(..., "--actor", help="Agent name, a.<id> or agent URI to inspect."),
    provider: str | None = typer.Option(None, "--provider", help="Only this model vendor's slot."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Show an agent's key grants and whether this device can unseal them."""

    def build(_services: Any) -> dict[str, Any]:
        params: dict[str, Any] = {"agent": actor}
        if provider is not None:
            params["provider"] = provider
        return params

    run_owner_key_request("agent.provider.status", build, _render_status, json_output)
