"""Exposure target/peer-key address validation and exposure normalization policy."""

from __future__ import annotations
import ipaddress
import os
import stat
from collections.abc import Mapping
from pathlib import Path


FORWARD_PROTOCOL_VERSION = 3


_PROXY_KEYS = frozenset(
    {
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "NO_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
        "no_proxy",
    }
)


def _normalize_exposure(exposure: Mapping[str, object]) -> dict[str, object]:
    port = exposure.get("port")
    target = exposure.get("target")
    proxy_protocol = exposure.get("proxyProtocol")
    if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
        raise ForwardingSidecarError("exposure port must be between 1 and 65535")
    if not isinstance(target, str) or not target:
        raise ForwardingSidecarError("exposure target must be a non-empty string")
    # Python-side canonical value is "none"; protocol v3 writes "" on the
    # wire (see controller.expose).  Both are accepted here so a persisted
    # entry and a raw wire event normalize to the same record.
    if proxy_protocol not in {"v2", "none", ""}:
        raise ForwardingSidecarError("proxy protocol must be v2 or none")
    # Any local process can reach a loopback TCP port and forge a PROXY v2
    # header, so an identity-carrying exposure needs a unix socket in a
    # private directory.  The sidecar enforces the same rule.
    if proxy_protocol == "v2" and not target.startswith("unix:"):
        raise ForwardingSidecarError(
            "proxy protocol v2 requires a unix: target in a private (0700) directory"
        )
    return {
        "port": port,
        "target": target,
        # Canonicalize the wire form "" back to the Python-side "none".
        "proxyProtocol": "none" if proxy_protocol == "" else proxy_protocol,
    }


def validate_unix_exposure_target(target: str) -> str:
    """Require a private, current-user directory for a unix socket target."""

    if not target.startswith("unix:"):
        return target
    path = Path(target.removeprefix("unix:"))
    if not path.is_absolute():
        raise ForwardingSidecarError("unix exposure target must be absolute")
    try:
        info = path.parent.stat()
    except OSError as error:
        raise ForwardingSidecarError(
            f"unix exposure target directory is unavailable: {path.parent}"
        ) from error
    expected_owner = getattr(os, "geteuid", lambda: info.st_uid)()
    if not stat.S_ISDIR(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o700:
        raise ForwardingSidecarError(
            f"unix exposure target directory must have mode 0700: {path.parent}"
        )
    if info.st_uid != expected_owner:
        raise ForwardingSidecarError(
            f"unix exposure target directory must be owned by the current user: {path.parent}"
        )
    return target


def validate_exposure_target(target: str) -> str:
    if target.startswith("unix:"):
        return validate_unix_exposure_target(target)
    if not target.startswith("tcp:"):
        raise ForwardingSidecarError("exposure target must start with unix: or tcp:")
    address = target.removeprefix("tcp:")
    try:
        if address.startswith("["):
            host, separator, raw_port = address[1:].partition("]:")
            if not separator:
                raise ValueError
        else:
            host, separator, raw_port = address.rpartition(":")
            if not separator:
                raise ValueError
        parsed = ipaddress.ip_address(host)
        port = int(raw_port)
    except ValueError as error:
        raise ForwardingSidecarError(
            "tcp exposure target must be an explicit loopback IP:port"
        ) from error
    if not parsed.is_loopback or not 1 <= port <= 65535:
        raise ForwardingSidecarError(
            "tcp exposure target must be an explicit loopback IP:port"
        )
    return target


def validate_peer_key_address(address: str) -> str:
    """The ``addr`` argument of the v3 ``peer-key`` op (was v2 ``whois``)."""

    try:
        if address.startswith("["):
            host, separator, raw_port = address[1:].partition("]:")
            if not separator:
                raise ValueError
        else:
            host, separator, raw_port = address.rpartition(":")
            if not separator:
                raise ValueError
        ipaddress.ip_address(host)
        port = int(raw_port)
    except ValueError as error:
        raise ForwardingSidecarError("addr must be an IP:port string") from error
    if not 1 <= port <= 65535:
        raise ForwardingSidecarError("addr must be an IP:port string")
    return address


class ForwardingSidecarError(RuntimeError):
    pass
