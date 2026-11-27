# Decision notes (service grant S3, 2026-11-26):
# - Exposes `hyprial org service grant|revoke|grants` on `service_app` without
#   any plaintext secret flag (`--secret`, `--key`, `--value`).
# - `--credential` accepts `<entryId>` or `<entryId>:<fieldName>` and fails
#   closed with `services.CliError("INVALID_ARGUMENT", ...)` on malformed values.
# - All output routes through `CliResult` / `services.runtime.execute`; human
#   views never display credential ciphertext or secret values.
# - Integration fix (2026-11-27): a grant to a target that already has an
#   effective grant rewrites it in place, so the human view says "Updated"
#   instead of "Granted" and there is no superseded list any more.
"""Service-grant CLI leaves mounted under ``hyprial org service``."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import typer

from hyprial.shell.impl.cli.commands.common.services import get_services
from hyprial.shell.impl.cli.commands.service.commands import service_app
from hyprial.shell.impl.cli.output import CliResult


def _parse_credential_option(raw: str, *, cli_error: type[BaseException]) -> dict[str, str]:
    if not raw or raw.strip() != raw:
        raise cli_error(
            "INVALID_ARGUMENT",
            "credential must be '<entryId>' or '<entryId>:<fieldName>'",
        )
    if ":" not in raw:
        return {"entryId": raw}
    parts = raw.split(":")
    if len(parts) != 2 or not parts[0] or not parts[1]:
        raise cli_error(
            "INVALID_ARGUMENT",
            "credential must be '<entryId>' or '<entryId>:<fieldName>'",
        )
    if parts[0].strip() != parts[0] or parts[1].strip() != parts[1]:
        raise cli_error(
            "INVALID_ARGUMENT",
            "credential must be '<entryId>' or '<entryId>:<fieldName>'",
        )
    return {"entryId": parts[0], "fieldName": parts[1]}


def _format_recipient(recipient: object) -> str:
    if isinstance(recipient, str):
        return recipient
    if isinstance(recipient, Mapping):
        owner = recipient.get("owner")
        device_id = recipient.get("deviceId")
        if isinstance(owner, str) and isinstance(device_id, str):
            return f"{owner}/{device_id}"
    return "-"


def _grant_status(grant: Mapping[str, Any]) -> str:
    if grant.get("revokedAtMs") is not None:
        return "revoked"
    if grant.get("effective") is False:
        return "expired"
    return "effective"


def _render_grant_row(grant: Mapping[str, Any]) -> str:
    service = grant.get("service") if isinstance(grant.get("service"), Mapping) else {}
    endpoint = f"{service.get('deviceId', '-')}:{service.get('port', '-')}"
    name = service.get("name") or "-"
    cred = "yes" if grant.get("hasCredential") else "no"
    return (
        f"{grant.get('grantId', '-')}\t"
        f"{grant.get('org', '-')}\t"
        f"{grant.get('grantor', '-')}\t"
        f"{endpoint}\t"
        f"{name}\t"
        f"{_format_recipient(grant.get('recipient'))}\t"
        f"cred={cred}\t"
        f"{_grant_status(grant)}"
    )


def _render_grant_created(data: Mapping[str, Any]) -> str:
    grant = data.get("grant")
    label = "Updated" if data.get("updated") is True else "Granted"
    if isinstance(grant, Mapping):
        return f"{label}: {_render_grant_row(grant)}"
    return f"Grant {label.lower()}."


def _render_revoke(data: Mapping[str, Any]) -> str:
    grant_id = data.get("grantId", "-")
    if data.get("revoked"):
        return f"Revoked: {grant_id}"
    return f"Not revoked (already inactive or absent): {grant_id}"


def _render_grants(data: Mapping[str, Any]) -> str:
    lines: list[str] = ["Granted:"]
    granted = data.get("granted")
    if isinstance(granted, list) and granted:
        lines.extend(
            f"  {_render_grant_row(item)}" for item in granted if isinstance(item, Mapping)
        )
    else:
        lines.append("  (none)")
    lines.append("Received:")
    received = data.get("received")
    if isinstance(received, list) and received:
        lines.extend(
            f"  {_render_grant_row(item)}" for item in received if isinstance(item, Mapping)
        )
    else:
        lines.append("  (none)")
    limited = data.get("limitedGrantors")
    if isinstance(limited, list):
        for item in limited:
            if not isinstance(item, Mapping):
                continue
            who = item.get("grantor") or "every grantor"
            lines.append(
                f"  Not read: {who} in {item.get('org', '-')} is over a read limit "
                f"({item.get('reason', '-')}); grants from it are unknown, not absent."
            )
    counts = data.get("documentCount")
    limit = data.get("documentLimit")
    if isinstance(counts, Mapping) and counts:
        lines.append("Grant documents:")
        lines.extend(f"  {org}\t{count}/{limit}" for org, count in sorted(counts.items()))
    return "\n".join(lines)


@service_app.command("grant")
def service_grant(
    port: int = typer.Argument(
        ..., min=1, max=65535, help="Locally exposed org-network TCP port."
    ),
    org: str = typer.Option(..., "--org", help="Organization name."),
    device: str = typer.Option(
        ..., "--device", help="Recipient '<owner>/<deviceId>' or '@members'."
    ),
    name: str | None = typer.Option(None, "--name", help="Optional service name."),
    protocol: str | None = typer.Option(
        None, "--protocol", help="Optional catalog protocol: tcp or http."
    ),
    note: str | None = typer.Option(None, "--note", help="Optional short usage note."),
    expires_at_ms: int | None = typer.Option(
        None,
        "--expires-at-ms",
        min=0,
        help="Optional expiration timestamp in Unix milliseconds.",
    ),
    credential: str | None = typer.Option(
        None,
        "--credential",
        metavar="ENTRY[:FIELD]",
        help="Saved user-provider entry '<entryId>' or '<entryId>:<fieldName>'.",
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Grant one locally exposed service port to an org device or ``@members``."""

    services = get_services()

    def operation() -> CliResult:
        current = get_services()
        params: dict[str, Any] = {"org": org, "port": port, "device": device}
        if expires_at_ms is not None:
            params["expiresAtMs"] = expires_at_ms
        if name is not None:
            params["name"] = name
        if protocol is not None:
            params["protocol"] = protocol
        if note is not None:
            params["note"] = note
        if credential is not None:
            params["credential"] = _parse_credential_option(
                credential, cli_error=current.CliError
            )
        result = current.runtime.request("service.grant", params)
        if not isinstance(result, dict) or not isinstance(result.get("grant"), dict):
            raise current.CliError("INVALID_RESPONSE", "service.grant must return grant")
        return CliResult({"ok": True, **result}, render=_render_grant_created)

    services.runtime.execute(operation, json_output=json_output)


@service_app.command("revoke")
def service_revoke(
    grant_id: str = typer.Argument(
        ..., metavar="GRANT_ID", help="Service grant identifier (sg.<26 base32 chars>)."
    ),
    org: str = typer.Option(..., "--org", help="Organization name."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Revoke one service grant authored by the local owner."""

    services = get_services()

    def operation() -> CliResult:
        current = get_services()
        result = current.runtime.request(
            "service.revoke", {"org": org, "grantId": grant_id}
        )
        if not isinstance(result, dict) or not isinstance(result.get("revoked"), bool):
            raise current.CliError(
                "INVALID_RESPONSE", "service.revoke must return revoked"
            )
        return CliResult({"ok": True, **result}, render=_render_revoke)

    services.runtime.execute(operation, json_output=json_output)


@service_app.command("grants")
def service_grants(
    org: str | None = typer.Option(None, "--org", help="Filter to one organization."),
    port: int | None = typer.Option(
        None, "--port", min=1, max=65535, help="Filter to one remote service port."
    ),
    include_revoked: bool = typer.Option(
        False, "--include-revoked", help="Include revoked and expired grants."
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """List service grants authored by or targeting this device (read-only)."""

    services = get_services()

    def operation() -> CliResult:
        current = get_services()
        params: dict[str, Any] = {}
        if org is not None:
            params["org"] = org
        if port is not None:
            params["port"] = port
        if include_revoked:
            params["includeRevoked"] = True
        result = current.runtime.request("service.grants", params)
        if (
            not isinstance(result, dict)
            or not isinstance(result.get("granted"), list)
            or not isinstance(result.get("received"), list)
        ):
            raise current.CliError(
                "INVALID_RESPONSE", "service.grants must return granted and received"
            )
        return CliResult({"ok": True, **result}, render=_render_grants)

    services.runtime.execute(operation, json_output=json_output)


__all__ = ["service_grant", "service_grants", "service_revoke"]
