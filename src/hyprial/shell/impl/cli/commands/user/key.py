"""Thin ``hyprial user key`` CLI. Lifecycle mutations explicitly await U2."""
from __future__ import annotations

import typer

from hyprial.shell.impl.cli.commands.agent.provider.commands import refuse_owner_key_in_worker_shell

from hyprial.shell.impl.cli.commands.common.groups import group
from hyprial.shell.impl.cli.commands.common.services import get_services
from hyprial.shell.impl.cli.output import CliResult

key_app = group(
    help="Inspect local user signing keys; binding lifecycle is not yet available.",
    no_args_is_help=True,
)


def _run(method: str, params: dict, json_output: bool) -> None:
    services = get_services()

    def operation() -> CliResult:
        refuse_owner_key_in_worker_shell(services, command_label="user key")
        result = services.runtime.request(method, params)
        return CliResult(
            {"ok": True, **result},
            render=lambda data: (
                f"User key: {data.get('state', 'unavailable')} "
                f"({data.get('keyId') or 'none'})"
            ),
        )

    services.runtime.execute(operation, json_output=json_output)


@key_app.command("status")
def status(json_output: bool = typer.Option(False, "--json", help="Emit JSON only.")):
    """Show public local key metadata; never return private key material."""
    _run("userKey.status", {}, json_output)


@key_app.command("renew")
def renew(json_output: bool = typer.Option(False, "--json", help="Emit JSON only.")):
    """Refuse until trusted binding issuance and publication are available."""
    _run("userKey.renew", {}, json_output)


@key_app.command("rotate")
def rotate(json_output: bool = typer.Option(False, "--json", help="Emit JSON only.")):
    """Refuse until trusted issuance, re-signing and revocation are available."""
    _run("userKey.rotate", {}, json_output)


@key_app.command("revoke")
def revoke(device: str = typer.Option(..., "--device", help="Device uid whose key is revoked."),
           json_output: bool = typer.Option(False, "--json", help="Emit JSON only.")):
    """Refuse until verified per-device revocation is available."""
    _run("userKey.revoke", {"deviceUid": device}, json_output)
