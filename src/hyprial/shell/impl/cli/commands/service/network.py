"""Network-facing leaves of ``hyprial service`` (R7 §2.13).

These four leaves were ``hyprial host expose|unexpose|exposures|peer-key``
(originally ``hyprial network …``): they describe what this machine offers on
the organization network, which is the serving half of the ``service`` group.
``peer-key`` is renamed ``whois`` at the CLI spelling; the daemon IPC method
stays ``network.peerKey``.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import typer

from hyprial.shell.impl.cli.commands.common.services import get_services
from hyprial.shell.impl.cli.commands.common.support import JsonObject
from hyprial.shell.impl.cli.commands.service.commands import service_app
from hyprial.shell.impl.cli.output import CliResult


@service_app.command("expose")
def service_expose(
    port: int = typer.Argument(..., min=1, max=65535, help="Org-network TCP port."),
    target: str = typer.Option(
        ..., "--target", help="Local unix:/absolute/path or tcp:127.0.0.1:port target."
    ),
    proxy_protocol: str = typer.Option(
        "v2", "--proxy-protocol", help="Identity preface: v2 or none."
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Publish one local service on the org network with caller identity.

    The exposure carries PROXY protocol v2 with the caller's node key (TLV
    0xE0), so the service behind it can resolve which organization member connected.
    """
    services = get_services()

    def operation() -> JsonObject:
        services = get_services()
        result = services._daemon_request(
            "network.expose",
            {"port": port, "target": target, "proxyProtocol": proxy_protocol},
        )
        if not isinstance(result, dict) or not isinstance(result.get("exposure"), dict):
            raise services.CliError("INVALID_RESPONSE", "network.expose must return exposure")
        return {"ok": True, **result}

    services._execute(operation, json_output=json_output)


@service_app.command("unexpose")
def service_unexpose(
    port: int = typer.Argument(..., min=1, max=65535, help="Org-network TCP port."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Remove one desired and active published service."""
    services = get_services()

    def operation() -> JsonObject:
        services = get_services()
        result = services._daemon_request("network.unexpose", {"port": port})
        if not isinstance(result, dict) or result.get("port") != port:
            raise services.CliError("INVALID_RESPONSE", "network.unexpose must return port")
        return {"ok": True, **result}

    services._execute(operation, json_output=json_output)


@service_app.command("exposures")
def service_exposures(
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """List the services this machine publishes on the org network."""
    services = get_services()

    def render(result: Mapping[str, Any]) -> str:
        rows = result["exposures"]
        lines = [
            f"{item['port']}\t{item['target']}\tproxy={item['proxyProtocol']}"
            for item in rows
            if isinstance(item, dict)
        ]
        rejected = result.get("rejected")
        for item in rejected if isinstance(rejected, list) else []:
            exposure = item.get("exposure") if isinstance(item, dict) else None
            port = exposure.get("port") if isinstance(exposure, dict) else "?"
            reason = item.get("reason") if isinstance(item, dict) else ""
            lines.append(f"{port}\tNOT SERVED\t{reason}")
        return "\n".join(lines) if lines else "No service exposures."

    def operation() -> CliResult:
        services = get_services()
        result = services._daemon_request("network.exposures")
        if not isinstance(result, dict) or not isinstance(result.get("exposures"), list):
            raise services.CliError("INVALID_RESPONSE", "network.exposures must return exposures")
        return CliResult({"ok": True, **result}, render=render)

    services._execute(operation, json_output=json_output)


@service_app.command("whois")
def service_whois(
    address: str = typer.Argument(..., metavar="IP:PORT", help="Observed org-network source address."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Resolve a connection address to the peer's node key.

    Renamed from ``network peer-key``; protocol v3 replaced the tsnet
    LocalAPI ``whois`` with the sidecar's ``Server.PeerKey`` for an address an
    exposed service observed.
    """
    services = get_services()

    def operation() -> JsonObject:
        services = get_services()
        result = services._daemon_request("network.peerKey", {"addr": address})
        if not isinstance(result, dict) or not isinstance(result.get("found"), bool):
            raise services.CliError("INVALID_RESPONSE", "network.peerKey must return found")
        return {"ok": True, **result}

    services._execute(operation, json_output=json_output)


__all__ = ["service_expose", "service_exposures", "service_unexpose", "service_whois"]
