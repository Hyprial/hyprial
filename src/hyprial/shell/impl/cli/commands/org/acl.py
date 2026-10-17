"""``hyprial org acl``: owner-node ACL-space entry authorizations.

The ACL space (``group-NAME-acl``) admits writes only through the owner
node's entry gate: the owner's terminal, the owner's user-proxy, and agents
the owner explicitly granted here.  Grants are local owner-node state —
they never enter orgfs and never synchronize.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from datetime import datetime
from typing import Any

import typer

from hyprial.identity import org_space_name
from hyprial.kernel import ipc_errors
from hyprial.shell.impl.cli.commands.common.services import get_services
from hyprial.shell.impl.cli.commands.common.support import (
    JsonObject,
    _local_operator_identity,
    _message_claim_params,
)
from hyprial.shell.impl.cli.output import CliResult
from hyprial.shell.impl.cli.output.tables import render_table

acl_app = typer.Typer(
    help=(
        "Authorize agents to write an org's ACL space. The entry gate "
        "defaults to the owner's terminal and user-proxy; these commands "
        "manage the explicit exceptions, stored only on the owner node."
    )
)


def _acl_caller() -> JsonObject:
    """Who asks: a managed worker's own fenced claim, or the local operator."""

    worker = os.environ.get("HYPRIAL_WORKER_ACTOR")
    return _message_claim_params(worker) if worker else {"from": _local_operator_identity()}


def _validated_org(value: str) -> str:
    services = get_services()
    try:
        org_space_name(value)
    except ValueError as error:
        raise services.CliError(ipc_errors.INVALID_ARGUMENT, str(error)) from error
    return value


def _until_ms(value: str) -> int:
    services = get_services()
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise services.CliError(
            ipc_errors.INVALID_ARGUMENT,
            f"--until must be an ISO 8601 time, got {value!r}",
        ) from error
    if parsed.tzinfo is None:
        # A bare wall-clock time means the operator's local time.
        parsed = parsed.astimezone()
    return int(parsed.timestamp() * 1000)


def _grant_line(entry: Mapping[str, Any]) -> list[str]:
    def stamp(key: str) -> str:
        value = entry.get(key)
        if not isinstance(value, int):
            return "-"
        return datetime.fromtimestamp(value / 1000).astimezone().isoformat(
            timespec="seconds"
        )

    return [str(entry.get("agent", "")), stamp("grantedAtMs"), stamp("untilMs")]


@acl_app.command("grant")
def acl_grant(
    org: str = typer.Argument(..., help="Organization name (group-NAME)."),
    agent: str = typer.Argument(
        ..., help="Agent to admit: a local short name or this node's canonical agent URI."
    ),
    until: str | None = typer.Option(
        None, "--until", help="ISO 8601 expiry; a bare time is local time."
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Admit one of the owner's agents to write the org's ACL space."""
    services = get_services()

    def operation() -> JsonObject:
        params: JsonObject = {
            **_acl_caller(),
            "org": _validated_org(org),
            "agent": agent,
        }
        if until is not None:
            params["untilMs"] = _until_ms(until)
        result = services._daemon_request("org.acl.grant", params)
        if not isinstance(result, dict):
            raise services.CliError(
                "INVALID_RESPONSE", "org.acl.grant must return an object"
            )
        return {"ok": True, **result}

    services._execute(operation, json_output=json_output)


@acl_app.command("revoke")
def acl_revoke(
    org: str = typer.Argument(..., help="Organization name (group-NAME)."),
    agent: str = typer.Argument(..., help="Agent whose grant to withdraw."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Withdraw an agent's ACL-space entry grant."""
    services = get_services()

    def operation() -> JsonObject:
        result = services._daemon_request(
            "org.acl.revoke",
            {**_acl_caller(), "org": _validated_org(org), "agent": agent},
        )
        if not isinstance(result, dict):
            raise services.CliError(
                "INVALID_RESPONSE", "org.acl.revoke must return an object"
            )
        return {"ok": True, **result}

    services._execute(operation, json_output=json_output)


@acl_app.command("ls")
def acl_ls(
    org: str = typer.Argument(..., help="Organization name (group-NAME)."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """List the org's live ACL-space entry grants on this owner node."""
    services = get_services()

    def render(result: Mapping[str, Any]) -> str:
        grants = result.get("grants")
        if not isinstance(grants, list) or not grants:
            return "no grants"
        return render_table(
            None,
            ["AGENT", "GRANTED", "UNTIL"],
            [_grant_line(entry) for entry in grants if isinstance(entry, Mapping)],
        )

    def operation() -> CliResult:
        result = services._daemon_request(
            "org.acl.ls", {**_acl_caller(), "org": _validated_org(org)}
        )
        if not isinstance(result, dict) or not isinstance(result.get("grants"), list):
            raise services.CliError(
                "INVALID_RESPONSE", "org.acl.ls must return the grants array"
            )
        return CliResult({"ok": True, **result}, render=render)

    services._execute(operation, json_output=json_output)
