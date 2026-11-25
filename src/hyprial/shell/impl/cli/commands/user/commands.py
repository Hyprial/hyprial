"""``hyprial user`` commands over the daemon's name-only person registry.

R3 of the user-to-IM binding removal (design #2950 8.5, owner ruling
2026-11-19): an IM sender is the person who owns that IM account and hyprial
does not judge who that is, so there is no ``bind``, ``unbind``, ``whois`` or
``bindings`` here and ``list`` lists people, not bindings.  ``add``, ``show``
and ``list`` read and write ``identity.users.add|list``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import typer
from hyprial.shell.impl.cli.commands.common.groups import group

from hyprial.error import codes
from hyprial.shell.impl.cli.commands.common.services import get_services
from hyprial.shell.impl.cli.commands.common.support import JsonObject
from hyprial.shell.impl.cli.output import CliResult, scalar_text
from hyprial.shell.impl.cli.output.tables import render_table


_USER_COLUMNS = ("userKey", "kind", "owner", "displayName", "nickname", "realName")

user_app = group(
    help=(
        "Keep the local registry of people: members (with a hyprial owner) "
        "and guests. Guests never grant authorization."
    )
)


def _confirmer(confirmed_by: str) -> str:
    who = confirmed_by.strip()
    if not who:
        raise get_services().CliError(
            codes.INVALID_ARGUMENT,
            "--confirmed-by must name the person or coordinator confirming this change",
        )
    return who


def _mapping(value: object, *, method: str) -> JsonObject:
    if not isinstance(value, Mapping):
        raise get_services().CliError(
            "INVALID_RESPONSE", f"{method} result must be an object"
        )
    return dict(value)


def _rows(value: object, *, method: str, keys: Sequence[str]) -> list[JsonObject]:
    candidate = (
        next((value[key] for key in keys if key in value), None)
        if isinstance(value, Mapping)
        else None
    )
    if not isinstance(candidate, list) or not all(
        isinstance(row, Mapping) for row in candidate
    ):
        raise get_services().CliError(
            "INVALID_RESPONSE", f"{method} result must contain object rows"
        )
    return [dict(row) for row in candidate]


def _render_users(data: Mapping[str, Any]) -> str:
    cells = [
        [scalar_text(row.get(column)) for column in _USER_COLUMNS]
        for row in data["rows"]
    ]
    return render_table(None, list(_USER_COLUMNS), cells)


@user_app.command("list")
def user_list(
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """List local members and guests; JSON output uses {"rows": [...]}."""

    def operation() -> CliResult:
        result = get_services().runtime.request("identity.users.list", {})
        return CliResult(
            {"rows": _rows(result, method="identity.users.list", keys=("users",))},
            render=_render_users,
        )

    get_services().runtime.execute(operation, json_output=json_output)


@user_app.command("add")
def user_add(
    kind: str = typer.Option(
        ..., "--kind", help="member (has a hyprial owner) or guest."
    ),
    confirmed_by: str = typer.Option(
        ..., "--confirmed-by", help="Person or coordinator confirming the user."
    ),
    owner: str | None = typer.Option(
        None, "--owner", help="The member's hyprial owner; forbidden for guests."
    ),
    nickname: str | None = typer.Option(None, "--nickname", help="Nickname."),
    real_name: str | None = typer.Option(None, "--real-name", help="Real name."),
    display_name: str | None = typer.Option(
        None, "--display-name", help="Name shown to agents."
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Add one local member or guest; the daemon generates the user key."""

    def operation() -> CliResult:
        if kind == "member" and not owner:
            raise get_services().CliError(
                codes.INVALID_ARGUMENT, "a member needs --owner"
            )
        if kind == "guest" and not display_name:
            raise get_services().CliError(
                codes.INVALID_ARGUMENT, "a guest needs --display-name"
            )
        params: JsonObject = {
            "kind": kind,
            "confirmedBy": _confirmer(confirmed_by),
        }
        for name, value in (
            ("owner", owner),
            ("nickname", nickname),
            ("realName", real_name),
            ("displayName", display_name),
        ):
            if value is not None:
                params[name] = value
        result = _mapping(
            get_services().runtime.request("identity.users.add", params),
            method="identity.users.add",
        )
        record = result.get("user")
        if not isinstance(record, Mapping):
            raise get_services().CliError(
                "INVALID_RESPONSE", "identity.users.add returned no user record"
            )
        return CliResult(
            {"user": dict(record)},
            render=lambda data: (
                f"added {scalar_text(data['user'].get('kind'))} "
                f"{scalar_text(data['user'].get('userKey') or data['user'].get('user'))}"
            ),
        )

    get_services().runtime.execute(operation, json_output=json_output)


@user_app.command("show")
def user_show(
    user: str = typer.Argument(..., help="Local user key."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Show one local member or guest through the daemon authority."""

    def operation() -> JsonObject:
        result = get_services().runtime.request(
            "identity.users.list", {"user": user}
        )
        rows = _rows(
            result, method="identity.users.list", keys=("users",)
        )
        matches = [
            row
            for row in rows
            if row.get("userKey", row.get("user")) == user
        ]
        if not matches:
            raise get_services().CliError("USER_NOT_FOUND", "user not found")
        return matches[0]

    get_services().runtime.execute(operation, json_output=json_output)
