"""``hyprial org`` membership commands: invite, join, leave, remove, pending.

People membership stays Casdoor-first and agent membership joins it in the
member slice.  The command callables live here; :mod:`.commands` registers them
(and keeps the ``org group invite`` alias on the same callable).  The two
helpers that do not move -- the org-name grammar and the invite-link reader --
are reached through that module at call time, so this
file imports no backend module and the CLI->backend ledger does not move.
"""

from __future__ import annotations

from typing import Any

import typer

from hyprial.shell.impl.cli.commands.common.services import get_services
from collections.abc import Mapping
from hyprial.shell.impl.cli.commands.common.support import JsonObject
from hyprial.shell.impl.cli.output import CliResult, progress
from hyprial.shell.impl.cli.commands.org.member.render import render_pending


def _validated_org(value: str) -> str:
    from hyprial.shell.impl.cli.commands.org import commands as org_commands

    return org_commands._validated_org(value)


def _read_link_argument(link: str) -> str:
    from hyprial.shell.impl.cli.commands.org import commands as org_commands

    return org_commands._read_link_argument(link)


def org_invite(
    org: str = typer.Argument(..., help="Organization name."),
    user: str = typer.Option(
        ..., "--user", help="Username to invite (directory rw, ACL ro)."
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Invite one user and print the invite link (§5.1).

    The link is a secret; the human output prefers the /j short link and
    keeps the long link below it.  ``--json`` passes the daemon fields
    through and adds ``short`` (null when minting degrades).
    """
    services = get_services()
    def render(result: Mapping[str, Any]) -> str:
        command = result.get("command")
        short = result.get("short")
        link = result["link"]
        lines = [short if isinstance(short, str) and short else link]
        if isinstance(short, str) and short:
            lines.append(f"长链：{link}")
        if isinstance(command, str) and command:
            lines.append(f"或在终端执行：{command}")
        lines.append("（私发，勿贴群）")
        return "\n".join(lines)

    def operation() -> CliResult:
        from hyprial.shell.impl.invites.shortlink import (
            ShortLinkUnavailable,
            shorten_invite_link,
        )

        services = get_services()
        result = services.runtime.request(
            "org.invite", {"org": _validated_org(org), "user": user}
        )
        if not isinstance(result, dict) or not isinstance(result.get("link"), str):
            raise services.CliError(
                "INVALID_RESPONSE", "org.invite must return the https invite link"
            )
        short: str | None = None
        try:
            short = shorten_invite_link(result["link"])
        except ShortLinkUnavailable as error:
            if error.notify:
                progress(
                    "org.invite.short_link_unavailable",
                    f"短链不可用：{error}",
                    json_output=json_output,
                    reason=str(error),
                )
        return CliResult({"ok": True, **result, "short": short}, render=render)

    services.runtime.execute(operation, json_output=json_output)

def _org_join_via_ipc(method: str, link: str, *, json_output: bool) -> None:
    """Body of ``org join``: every link form — https URL, ``hyprial://``
    deep link, bare token, bare ``v1.`` — is passed through untouched;
    decoding is the daemon's job (§5.1)."""
    services = get_services()

    def operation() -> JsonObject:
        services = get_services()
        from hyprial.shell.impl.invites.join import request_join

        result = request_join(
            services.runtime, _read_link_argument(link), method=method, json_output=json_output
        )
        if not isinstance(result, dict):
            raise services.CliError(
                "INVALID_RESPONSE", f"{method} must return an object"
            )
        # ``org`` rides in the daemon result; Desktop's deep-link patch
        # depends on it, so keep the passthrough verbatim.
        return {"ok": True, **result}

    services.runtime.execute(operation, json_output=json_output)

def org_join(
    link: str = typer.Argument(
        ...,
        help=(
            "The invite link — https URL, hyprial:// deep link, bare token "
            "or 'v1.<payload>' — or '-' to read it from stdin (keeps the "
            "secret out of the shell history)."
        ),
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Join an org from any invite-link form (§5.1/§5.2 path 1)."""
    _org_join_via_ipc("org.join", link, json_output=json_output)

def org_leave(
    org: str = typer.Argument(..., help="Organization name."),
    yes: bool = typer.Option(False, '--yes', help='Confirm device cutoff.'),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Leave an org: remove this device and file a leave request."""
    services = get_services()

    def operation() -> JsonObject:
        services = get_services()
        from ..removal import confirmed_removal
        result = confirmed_removal(
            services, 'org.leave', {'org': _validated_org(org)}, yes=yes
        )
        if not isinstance(result, dict):
            raise services.CliError("INVALID_RESPONSE", "org.leave must return an object")
        return {"ok": True, **result}

    services.runtime.execute(operation, json_output=json_output)

def org_remove(
    org: str = typer.Argument(..., help="Organization name."),
    user: str = typer.Argument(..., help="Username to remove from the org."),
    device: list[str] | None = typer.Option(None, '--device', help='Only revoke these active device IDs (repeatable); retain user memberships. Requires --yes. Refuses the last active device. Without this option, remove the user and all their devices.'),
    yes: bool = typer.Option(False, '--yes', help='Confirm device cutoff.'),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Remove OrgFS membership, then revoke fixed sidecar devices (owner/admin).

    Removing someone from the org also removes them from the org's Casdoor
    group, and with it from anything else that relies on that group.
    Use --device to retain those memberships while removing selected devices.
    """
    services = get_services()

    def operation() -> JsonObject:
        services = get_services()
        from ..removal import confirmed_removal
        params = {'org': _validated_org(org), 'user': user}
        if device is not None:
            if not yes:
                raise services.CliError('CONFIRMATION_REQUIRED', 'Re-run with --yes to confirm device removal.')
            params['deviceIds'] = device
        result = confirmed_removal(
            services,
            'org.remove',
            params,
            yes=yes,
        )
        if not isinstance(result, dict):
            raise services.CliError("INVALID_RESPONSE", "org.remove must return an object")
        group = result.get("casdoorGroup")
        if isinstance(group, str) and group.startswith("removed from "):
            result = {
                **result,
                "note": (
                    f"{user} was also {group}; other applications that rely "
                    "on that Casdoor group lose them too"
                ),
            }
        return {"ok": True, **result}

    services.runtime.execute(operation, json_output=json_output)

def org_pending(
    accept_all: bool = typer.Option(
        False,
        "--accept-all",
        help="Join every pending invite without asking.",
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Pick up invites bound to this account and join them (§5.3).

    A TTY without ``--accept-all`` asks per invite; a non-TTY (or
    ``--json``) invocation without ``--accept-all`` only lists what is
    pending and joins nothing.
    """
    services = get_services()

    def operation() -> JsonObject | str:
        services = get_services()
        from hyprial.shell.impl.cli.commands.login.invites import invite_confirmer
        from hyprial.shell.impl.invites.join import invite_request
        from hyprial.shell.impl.invites.pending import (
            InvitePickupError,
            run_pending,
        )

        confirm = (
            None if accept_all else invite_confirmer(services, json_output=json_output)
        )
        try:
            result = run_pending(
                services._hyprial_home(),
                accept_all=accept_all,
                confirm=confirm,
                request=invite_request(services.runtime, json_output=json_output),
            )
        except InvitePickupError as error:
            raise services.CliError(error.code, str(error)) from error
        return CliResult(result, render=render_pending)

    services.runtime.execute(operation, json_output=json_output)
