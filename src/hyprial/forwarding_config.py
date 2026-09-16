"""Build non-secret daemon environment for the candidate forwarding sidecar."""

from __future__ import annotations

import ipaddress
import json
import os
from collections.abc import Mapping
from pathlib import Path
from urllib.parse import urlsplit

from hyprial.contracts.forwarding import (
    DEFAULT_PEER_PORT,
    FORWARDING_COMMAND_ENV,
    FORWARDING_UP_ENV,
)
from hyprial.network_profile import TSNET_STATE_DIRNAME, resolve_profile

FORWARDING_SIDECAR_ENV = "HYPRIAL_FORWARDING_SIDECAR"
FORWARDING_TARGET_ENV = "HYPRIAL_FORWARDING_INBOUND_TARGET"
class ForwardingConfigurationError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


def _loopback_target(raw: str) -> str:
    parsed = urlsplit(f"//{raw}")
    try:
        host = parsed.hostname
        port = parsed.port
        address = ipaddress.ip_address(host or "")
    except ValueError as error:
        raise ForwardingConfigurationError(
            "FORWARDING_TARGET_INVALID",
            f"forwarding inbound target must be an explicit loopback host:port; got {raw!r}",
        ) from error
    if not address.is_loopback or port is None or port <= 0:
        raise ForwardingConfigurationError(
            "FORWARDING_TARGET_INVALID",
            f"forwarding inbound target must be an explicit loopback host:port; got {raw!r}",
        )
    return raw


def daemon_forwarding_environment(
    hyprial_home: Path,
    environ: Mapping[str, str],
    *,
    node_id: str,
) -> dict[str, str]:
    """Return v2 sidecar launch variables, or none when not configured.

    The candidate path is explicit because publishing binaries and updating
    hyprial's 0.1.3 pin are a separate reviewed unit. No released binary is used
    as an implicit fallback.
    """

    raw_binary = environ.get(FORWARDING_SIDECAR_ENV, "").strip()
    raw_target = environ.get(FORWARDING_TARGET_ENV, "").strip()
    if not raw_binary and not raw_target:
        return {}
    if not raw_binary:
        raise ForwardingConfigurationError(
            "FORWARDING_SIDECAR_MISSING",
            f"{FORWARDING_SIDECAR_ENV} is required when forwarding is enabled",
        )
    binary = Path(raw_binary).expanduser()
    if not binary.is_file() or not os.access(binary, os.X_OK):
        raise ForwardingConfigurationError(
            "FORWARDING_SIDECAR_MISSING",
            f"candidate forwarding sidecar is not an executable file: {binary}",
        )
    if not raw_target:
        raise ForwardingConfigurationError(
            "FORWARDING_LISTEN_MISSING",
            f"{FORWARDING_TARGET_ENV} must name the fixture's explicit listener",
        )
    target = _loopback_target(raw_target)
    expected_listen = f"tcp/{target}"
    configured_listen = {
        item.strip()
        for item in environ.get("HYPRIAL_ZENOH_LISTEN", "").split(",")
        if item.strip()
    }
    if expected_listen not in configured_listen:
        raise ForwardingConfigurationError(
            "FORWARDING_LISTEN_MISSING",
            f"forwarding inbound target {target} has no matching explicit "
            "HYPRIAL_ZENOH_LISTEN endpoint",
        )

    home = Path(hyprial_home)
    state_dir = home / TSNET_STATE_DIRNAME
    if not (state_dir / "node").is_dir():
        raise ForwardingConfigurationError(
            "FORWARDING_STATE_MISSING",
            f"candidate forwarding sidecar has no joined node state under {state_dir}",
        )
    try:
        profile, _source = resolve_profile(environ, hyprial_home=home)
    except ValueError as error:
        raise ForwardingConfigurationError(
            "FORWARDING_PROFILE_INVALID", str(error)
        ) from error

    up = {
        "v": 2,
        "op": "up",
        "controlUrl": (
            ""
            if profile.control_plane_kind == "tailscale"
            else profile.control_plane_url
        ),
        "hostname": node_id.split(".", 1)[0],
        "dir": str(state_dir),
        "ephemeral": False,
        "join": profile.join,
        "authKey": None,
        "resume": True,
        "inboundTarget": target,
        "peerPort": DEFAULT_PEER_PORT,
    }
    return {
        FORWARDING_COMMAND_ENV: json.dumps(
            [str(binary), "forward"], separators=(",", ":")
        ),
        FORWARDING_UP_ENV: json.dumps(up, separators=(",", ":")),
    }
