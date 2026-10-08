"""``hyprial network`` tailnet exposure commands."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from hyprial.shell.impl.cli.commands.common.services import get_services

import typer

from hyprial.shell.impl.cli.commands.common.support import JsonObject
from hyprial.shell.impl.cli.output import CliResult
network_app = typer.Typer(
    help="Expose loopback services to Tailcat peers and inspect peer identity."
)


@network_app.command("expose")
def network_expose(
    port: int = typer.Argument(..., min=1, max=65535, help="Tailnet TCP port."),
    target: str = typer.Option(
        ..., "--target", help="Local unix:/absolute/path or tcp:127.0.0.1:port target."
    ),
    proxy_protocol: str = typer.Option(
        "v2", "--proxy-protocol", help="Identity preface: v2 or none."
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Persist and activate one inbound tailnet service exposure."""
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


@network_app.command("unexpose")
def network_unexpose(
    port: int = typer.Argument(..., min=1, max=65535, help="Tailnet TCP port."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Remove one desired and active inbound exposure."""
    services = get_services()

    def operation() -> JsonObject:
        services = get_services()
        result = services._daemon_request("network.unexpose", {"port": port})
        if not isinstance(result, dict) or result.get("port") != port:
            raise services.CliError("INVALID_RESPONSE", "network.unexpose must return port")
        return {"ok": True, **result}

    services._execute(operation, json_output=json_output)


@network_app.command("exposures")
def network_exposures(
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """List the daemon's persistent desired inbound exposures."""
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
        return "\n".join(lines) if lines else "No network exposures."

    def operation() -> CliResult:
        services = get_services()
        result = services._daemon_request("network.exposures")
        if not isinstance(result, dict) or not isinstance(result.get("exposures"), list):
            raise services.CliError("INVALID_RESPONSE", "network.exposures must return exposures")
        return CliResult({"ok": True, **result}, render=render)

    services._execute(operation, json_output=json_output)


@network_app.command("peer-key")
def network_peer_key(
    address: str = typer.Argument(..., metavar="IP:PORT", help="Tailcat source address."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Resolve a Tailcat connection address to the peer's node key.

    Protocol v3 replaced ``network whois`` (tsnet LocalAPI) with
    ``peer-key``: the sidecar answers from ``Server.PeerKey`` for an
    address an exposed service observed.
    """
    services = get_services()

    def operation() -> JsonObject:
        services = get_services()
        result = services._daemon_request("network.peerKey", {"addr": address})
        if not isinstance(result, dict) or not isinstance(result.get("found"), bool):
            raise services.CliError("INVALID_RESPONSE", "network.peerKey must return found")
        return {"ok": True, **result}

    services._execute(operation, json_output=json_output)
