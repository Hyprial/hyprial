# Decision record (provider-key contract freeze r3, docs/design/provider-key-contract-freeze-2026-11-03.md §6)
# - Thin CLI over the four user.provider.* daemon methods; the daemon is the only authority.
# - Secrets arrive only on a non-TTY stdin (one line, one trailing newline stripped,
#   1..16384 UTF-8 bytes) and are never echoed, rendered or placed in an error message.
# - Every command refuses inside a worker shell before any IPC; the daemon refuses again
#   through preauthorize.
# - logout without --yes previews the affected agents from user.provider.list and asks on
#   stderr; non-interactive or --json callers must pass --yes (CONFIRMATION_REQUIRED, as
#   the other destructive shell commands do).
# - The preview names only grants in state "active" (revoked grants are not revoked
#   again). A key absent from `keys`, or named in `invalid`, is reported as not listed
#   with its code and "affected agents unknown": the preview never claims a key it
#   cannot see affects no agent.
# - The stdin reader, worker refusal and request runner live in agent/provider/commands.py,
#   which already holds the reviewed hyprial.kernel ipc_errors edge, so this module adds no
#   new CLI->backend import (scripts/ci/import-ratchet-baseline.json may only shrink).
"""``hyprial user provider``: the operator's own model-vendor API keys."""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import typer

from hyprial.shell.impl.cli.commands.agent.provider.commands import (
    OWNER_KEY_NOT_LISTED_CODE,
    read_owner_secret,
    refuse_owner_key_in_worker_shell,
    run_owner_key_request,
)
from hyprial.shell.impl.cli.commands.common.groups import group
from hyprial.shell.impl.cli.commands.common.services import get_services
from hyprial.shell.impl.cli.output import CliResult, confirm

provider_app = group(
    help="Manage your own model-vendor API keys, sealed to your devices.",
    no_args_is_help=True,
)


def render_key(key: Mapping[str, Any]) -> str:
    """One concise line for a ProviderKeyView; never any secret material."""
    label = f" label={key.get('label')!r}" if key.get("label") else ""
    names = ",".join(key.get("environmentNames") or []) or "-"
    used = key.get("lastUsedAt")
    return (
        f"{key.get('keyId')} {key.get('provider')} {key.get('state')}{label} "
        f"env={names} dek={key.get('dekVersion') or '-'} "
        f"grants={len(key.get('grants') or [])} lastUsed={used if used is not None else 'never'}"
    )


def _render_login(data: Mapping[str, Any]) -> str:
    return "Stored key " + render_key(data.get("key") or {})


def _render_rotate(data: Mapping[str, Any]) -> str:
    return (
        "Rotated key " + render_key(data.get("key") or {})
        + f"\nReissued grants: {data.get('reissuedGrants', 0)}"
    )


def _render_list(data: Mapping[str, Any]) -> str:
    lines = [render_key(key) for key in data.get("keys") or []]
    lines += [f"invalid {row.get('keyId')} {row.get('code')}" for row in data.get("invalid") or []]
    return "\n".join(lines) if lines else "no keys"


def _render_logout(data: Mapping[str, Any]) -> str:
    if data.get("alreadyDeleted"):
        return f"Key {data.get('keyId')} was already logged out"
    revoked = data.get("revokedGrants") or []
    return f"Logged out key {data.get('keyId')}; revoked {len(revoked)} agent grant(s)"


def _agent_label(grant: Mapping[str, Any]) -> str:
    return str(grant.get("agentId") or grant.get("agent"))


def _logout_preview(services: Any, key_id: str) -> str:
    listing = services.runtime.request("user.provider.list", {"includeDeleted": True})
    keys = [key for key in listing.get("keys") or [] if key.get("keyId") == key_id]
    invalid = [row for row in listing.get("invalid") or [] if row.get("keyId") == key_id]
    if invalid or not keys:
        code = invalid[0].get("code") if invalid else OWNER_KEY_NOT_LISTED_CODE
        return f"key {key_id} is not listed ({code}); affected agents unknown"
    grants = [
        grant
        for key in keys
        for grant in key.get("grants") or []
        if grant.get("state") == "active"
    ]
    if not grants:
        return f"Logging out {key_id} revokes no agent grant."
    rows = [f"  {_agent_label(grant)} ({grant.get('grantId')}, {grant.get('state')})" for grant in grants]
    return "\n".join([f"Logging out {key_id} revokes these agent grants:", *rows])


@provider_app.command("list")
def list_keys(
    provider: str | None = typer.Option(None, "--provider", help="Only keys of this model vendor."),
    include_deleted: bool = typer.Option(False, "--all", help="Include logged-out (deleted) keys."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """List your stored keys and the agents granted each; never any key value."""

    def build(_services: Any) -> dict[str, Any]:
        params: dict[str, Any] = {}
        if provider is not None:
            params["provider"] = provider
        if include_deleted:
            params["includeDeleted"] = True
        return params

    run_owner_key_request("user.provider.list", build, _render_list, json_output)


@provider_app.command("logout")
def logout(
    key_id: str = typer.Argument(..., help="Key id (pk.…) to log out."),
    yes: bool = typer.Option(False, "--yes", help="Skip the confirmation listing affected agents."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Delete a key and revoke every agent grant that uses it.

    Without --yes the affected agents are listed on stderr and you are asked to
    confirm; non-interactive and --json callers must pass --yes.
    """
    services = get_services()

    def operation() -> CliResult:
        refuse_owner_key_in_worker_shell(services)
        if not yes:
            if json_output or not services._stdin_isatty():
                raise services.CliError(
                    "CONFIRMATION_REQUIRED",
                    "logging out a key from a non-interactive command requires --yes",
                )
            preview = _logout_preview(services, key_id)
            if not confirm("Log out this key?", preview=preview):
                raise services.CliError(
                    "CONFIRMATION_REQUIRED", "Re-run with --yes after reviewing the affected agents."
                )
        result = services.runtime.request("user.provider.logout", {"keyId": key_id})
        return CliResult({"ok": True, **result}, render=_render_logout)

    services.runtime.execute(operation, json_output=json_output)


@provider_app.command("login")
def login(
    provider: str = typer.Argument(..., help="Model vendor name, e.g. openai or deepseek."),
    api_key_stdin: bool = typer.Option(
        False, "--api-key-stdin", help="Read the API key as one line from non-TTY stdin (required)."
    ),
    label: str | None = typer.Option(None, "--label", help="Optional display label for this key."),
    env: list[str] | None = typer.Option(
        None, "--env", help="Environment variable name the key is injected as; repeatable."
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Store a new API key for a model vendor, sealed to this device.

    The key is read only from stdin (--api-key-stdin) and never printed. Every
    call creates a new key; agents use it only after 'hyprial agent provider allow'.
    """

    def build(services: Any) -> dict[str, Any]:
        secret = read_owner_secret(services, requested=api_key_stdin, flag="--api-key-stdin")
        params: dict[str, Any] = {"provider": provider, "kind": "api-key", "secret": secret}
        if label is not None:
            params["label"] = label
        if env:
            params["environmentNames"] = list(env)
        return params

    run_owner_key_request("user.provider.login", build, _render_login, json_output)


@provider_app.command("rotate")
def rotate(
    key_id: str = typer.Argument(..., help="Key id (pk.…) to rotate."),
    replace_stdin: bool = typer.Option(
        False, "--replace-stdin", help="Also replace the API key with one line read from non-TTY stdin."
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Rotate a key's data-encryption key and reissue its agent grants.

    With --replace-stdin the API key itself is replaced too; the new value is
    read only from stdin and never printed.
    """

    def build(services: Any) -> dict[str, Any]:
        params: dict[str, Any] = {"keyId": key_id}
        if replace_stdin:
            params["replaceSecret"] = read_owner_secret(services, requested=True, flag="--replace-stdin")
        return params

    run_owner_key_request("user.provider.rotate", build, _render_rotate, json_output)
