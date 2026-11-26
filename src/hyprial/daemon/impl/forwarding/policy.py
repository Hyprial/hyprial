# Decision notes (service grant S4-py, 2026-11-26):
# - An exposure's admission is explicit on protocol v7: "allow" admits every
#   member of the tailnet, "keys" admits exactly the listed peer public keys
#   and an empty list admits nobody. The daemon states which one it means on
#   every expose; there is no default.
# - The wire shape is validated here, before anything is written to the
#   sidecar, with the same rules the Go parser applies (canonical nodekeys,
#   no duplicates, no zero key, at most 4096). A request the sidecar would
#   reject is refused without a round trip.
# - The "exposed" reply never carries the key list, only a count in "keys"
#   mode. The caller's set is confirmed by that count, which is why the reply
#   check compares modes and counts and never expects keys back.
"""Forwarding validation, typed errors and membership operation wait policy."""

from __future__ import annotations
import ipaddress
import re
import socket
import stat
from collections.abc import Mapping, Sequence
from pathlib import Path

from hyprial.daemon.impl.network.tailcat.binary import SIDECAR_PROTOCOL_VERSION
from hyprial.kernel import (
    MEMBERSHIP_BUDGETS,
    loopback_inbound_supported,
    private_metadata_violation,
)


FORWARD_PROTOCOL_VERSION = SIDECAR_PROTOCOL_VERSION


_UNSUPPORTED_PROXY_KEYS = frozenset(
    {
        "ALL_PROXY",
        "all_proxy",
    }
)


def _child_environment(environ: Mapping[str, str]) -> dict[str, str]:
    return {
        key: value
        for key, value in environ.items()
        if not key.startswith("TS_") and key not in _UNSUPPORTED_PROXY_KEYS
    }


def _normalize_exposure(exposure: Mapping[str, object]) -> dict[str, object]:
    """Validate an exposure record; protocol v7 writes canonical ``"none"`` as ``""`` on the wire."""
    port = exposure.get("port")
    target = exposure.get("target")
    proxy_protocol = exposure.get("proxyProtocol")
    if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
        raise ForwardingSidecarError("exposure port must be between 1 and 65535")
    if not isinstance(target, str) or not target:
        raise ForwardingSidecarError("exposure target must be a non-empty string")
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


ADMISSION_ALLOW = "allow"
ADMISSION_KEYS = "keys"
MAX_ADMISSION_KEYS = 4096
_ADMISSION_MODES = frozenset({ADMISSION_ALLOW, ADMISSION_KEYS})
_CANONICAL_NODEKEY = re.compile(r"nodekey:[0-9a-f]{64}")
_ZERO_NODEKEY = "nodekey:" + "0" * 64


def normalize_admission(admission: object, keys: object) -> tuple[str, list[str]]:
    """Validate an exposure's admission; return it in wire order.

    ``"allow"`` takes no keys. ``"keys"`` takes a list of canonical peer
    public keys without duplicates, possibly empty (admit nobody).
    """

    if admission not in _ADMISSION_MODES:
        raise ForwardingSidecarError("exposure admission must be allow or keys")
    if admission == ADMISSION_ALLOW:
        if keys:
            raise ForwardingSidecarError("exposure admission allow carries no keys")
        return ADMISSION_ALLOW, []
    if not isinstance(keys, Sequence) or isinstance(keys, (str, bytes)):
        raise ForwardingSidecarError("exposure admission keys must be a list")
    if len(keys) > MAX_ADMISSION_KEYS:
        raise ForwardingSidecarError("exposure admission keys exceed the sidecar limit")
    for entry in keys:
        if (
            not isinstance(entry, str)
            or entry == _ZERO_NODEKEY
            or not _CANONICAL_NODEKEY.fullmatch(entry)
        ):
            raise ForwardingSidecarError("exposure admission keys must be canonical nodekeys")
    if len(set(keys)) != len(keys):
        raise ForwardingSidecarError("exposure admission keys must be unique")
    return ADMISSION_KEYS, list(keys)


def exposure_wire_request(
    normalized: Mapping[str, object], admission: str, keys: Sequence[str]
) -> dict[str, object]:
    """The ``expose`` request for a normalized exposure and its admission."""

    mode, ordered = normalize_admission(admission, keys)
    wire: dict[str, object] = dict(normalized, admission=mode)
    if wire["proxyProtocol"] == "none":
        wire["proxyProtocol"] = ""
    if mode == ADMISSION_KEYS:
        wire["keys"] = ordered
    return wire


def confirmed_admission(event: Mapping[str, object], wire: Mapping[str, object]) -> bool:
    """Whether the ``exposed`` reply confirms exactly the request ``wire``.

    The reply echoes the exposure and the mode and, in ``"keys"`` mode, the
    number of keys it took. It never carries the keys themselves.
    """

    if _normalize_exposure(event) != _normalize_exposure(wire):
        return False
    if event.get("admission") != wire.get("admission"):
        return False
    if wire.get("admission") == ADMISSION_ALLOW:
        return "keyCount" not in event
    count = event.get("keyCount")
    return type(count) is int and count == len(wire.get("keys", ()))


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
    if not stat.S_ISDIR(info.st_mode):
        raise ForwardingSidecarError(
            f"unix exposure target directory must have mode 0700: {path.parent}"
        )
    try:
        violation = private_metadata_violation(path.parent, info, mode=0o700)
    except OSError as error:
        raise ForwardingSidecarError(
            f"unix exposure target directory privacy cannot be inspected: {path.parent}"
        ) from error
    if violation == "unsafe-mode":
        raise ForwardingSidecarError(
            f"unix exposure target directory must have mode 0700: {path.parent}"
        )
    if violation == "wrong-owner":
        raise ForwardingSidecarError(
            f"unix exposure target directory must be owned by the current user: {path.parent}"
        )
    if violation is not None:
        raise ForwardingSidecarError(
            f"unix exposure target directory must be account-private "
            f"({violation}): {path.parent}"
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


_INBOUND_TARGET_EXPECTATION = (
    "inbound target must be unix:<absolute-path> or tcp:127.0.0.1:<port>"
)


def _loopback_tcp_port(address: str) -> int:
    host, separator, raw_port = address.partition(":")
    if (
        not separator
        or host != "127.0.0.1"
        or not raw_port.isascii()
        or not raw_port.isdigit()
    ):
        raise ForwardingSidecarError(_INBOUND_TARGET_EXPECTATION)
    port = int(raw_port)
    if not 1 <= port <= 65535:
        raise ForwardingSidecarError(_INBOUND_TARGET_EXPECTATION)
    return port


def validate_inbound_target(target: str) -> str:
    """Accept only a private unix socket or explicit loopback TCP target."""

    if target.startswith("unix:"):
        return validate_unix_exposure_target(target)
    if target.startswith("tcp:"):
        _loopback_tcp_port(target.removeprefix("tcp:"))
        return target
    raise ForwardingSidecarError(_INBOUND_TARGET_EXPECTATION)


def inbound_target_from_listener(listener: str) -> str:
    """Convert a managed zenoh listener into its sidecar inbound target."""

    if listener.startswith("unixsock-stream/"):
        path = listener.removeprefix("unixsock-stream/")
        if not path or not Path(path).is_absolute():
            raise ForwardingSidecarError(_INBOUND_TARGET_EXPECTATION)
        return "unix:" + path
    if listener.startswith("tcp/"):
        address = listener.removeprefix("tcp/")
        _loopback_tcp_port(address)
        return "tcp:" + address
    raise ForwardingSidecarError(_INBOUND_TARGET_EXPECTATION)


def inbound_listener_from_target(target: str) -> str:
    """Convert a managed sidecar inbound target into its zenoh listener."""

    validate_inbound_target(target)
    if target.startswith("unix:"):
        return "unixsock-stream/" + target.removeprefix("unix:")
    return "tcp/" + target.removeprefix("tcp:")


def is_private_inbound_listener(listener: str) -> bool:
    """Whether a listener stays on the private unix or loopback TCP channel."""

    try:
        inbound_target_from_listener(listener)
    except ForwardingSidecarError:
        return False
    return True


def validate_managed_inbound_target(target: str) -> str:
    """Validate the managed channel grammar, then its platform capability."""

    validate_inbound_target(target)
    if target.startswith("tcp:") and not loopback_inbound_supported():
        raise ForwardingSidecarError(
            "managed inbound TCP is only supported on Windows; "
            "use unix:<absolute-path>"
        )
    return target


def is_managed_private_inbound_listener(listener: str) -> bool:
    """Whether a listener passes managed-channel grammar and platform gates."""

    if not is_private_inbound_listener(listener):
        return False
    return not listener.startswith("tcp/") or loopback_inbound_supported()


def reserve_loopback_listener() -> str:
    """Reserve one kernel-assigned loopback port as a zenoh listener."""

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return f"tcp/127.0.0.1:{probe.getsockname()[1]}"


def validate_peer_key_address(address: str) -> str:
    """Validate the ``addr`` argument of the ``peer-key`` operation."""

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


def parse_derp_map(event: dict[str, object]) -> tuple[str | None, str | None]:
    """Return the sidecar-reported DERP map URL and source, if any."""

    raw_url = event.get("derpMapURL")
    raw_source = event.get("derpMapSource")
    return (
        raw_url if isinstance(raw_url, str) else None,
        raw_source if isinstance(raw_source, str) else None,
    )


class ForwardingSidecarError(RuntimeError):
    def __init__(self, message: str, *, code: str = "FORWARD_PROTOCOL", details: dict | None = None) -> None:
        self.code = code
        self.details = details or {}
        super().__init__(message)


REQUEST_MEMBERSHIP_WAIT_SECONDS = MEMBERSHIP_BUDGETS["membership-status"].seconds


def membership_wait_seconds(op: str, requested: float | None) -> float:
    """Use the operation budget unless the caller supplies a tighter bound."""

    allowance = MEMBERSHIP_BUDGETS[op].seconds
    return allowance if requested is None else min(float(requested), allowance)
