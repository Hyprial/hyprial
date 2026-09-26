"""Status projection and bounded TCP diagnostics for tailnet peers."""

from __future__ import annotations

import errno
import socket
from datetime import UTC, datetime
from typing import Any, Mapping

from hyprial.contracts.forwarding import DEFAULT_PEER_PORT


# Registered in ``tests/supervision_exemptions.json``. This bounds one raw TCP
# connect from the on-demand doctor command; it starts no lifecycle or retry.
PEER_CONNECT_START_TIMEOUT_SECONDS = 2.0


def _nullable_string(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _peer_name(peer: Mapping[str, object]) -> str | None:
    host_name = _nullable_string(peer.get("HostName"))
    if host_name is not None:
        return host_name
    dns_name = _nullable_string(peer.get("DNSName"))
    return dns_name.rstrip(".") if dns_name is not None else None


def _last_handshake_age(value: object, *, now: datetime) -> float | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        instant = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if instant.year <= 1:
        return None
    if instant.tzinfo is None:
        instant = instant.replace(tzinfo=UTC)
    return max(0.0, (now - instant.astimezone(UTC)).total_seconds())


def _listen_address(addresses: list[str]) -> str | None:
    if not addresses:
        return None
    host = addresses[0]
    rendered = f"[{host}]" if ":" in host else host
    return f"{rendered}:{DEFAULT_PEER_PORT}"


def tailnet_status_projection(
    status: Mapping[str, object] | None,
    *,
    now: datetime | None = None,
) -> dict[str, object]:
    """Project only observed Tailscale status fields into the ``ps`` wire."""

    observed_at = now or datetime.now(UTC)
    raw_self = status.get("Self") if isinstance(status, Mapping) else None
    self_status = raw_self if isinstance(raw_self, Mapping) else {}
    raw_peers = status.get("Peer") if isinstance(status, Mapping) else None
    peer_values = raw_peers.values() if isinstance(raw_peers, Mapping) else ()
    peers: list[dict[str, Any]] = []
    for raw_peer in peer_values:
        if not isinstance(raw_peer, Mapping):
            continue
        name = _peer_name(raw_peer)
        if name is None:
            continue
        raw_addresses = raw_peer.get("TailscaleIPs")
        addresses = (
            [item for item in raw_addresses if isinstance(item, str) and item]
            if isinstance(raw_addresses, list)
            else []
        )
        current_address = raw_peer.get("CurAddr")
        path = (
            "direct"
            if isinstance(current_address, str) and current_address
            else "relayed"
            if current_address == ""
            else None
        )
        online = raw_peer.get("Online")
        peers.append(
            {
                "name": name,
                "dnsName": (
                    raw_peer["DNSName"].rstrip(".")
                    if isinstance(raw_peer.get("DNSName"), str) and raw_peer["DNSName"]
                    else None
                ),
                "addresses": addresses,
                "listenAddress": _listen_address(addresses),
                "online": online if isinstance(online, bool) else None,
                "path": path,
                "homeDerp": _nullable_string(raw_peer.get("Relay")),
                "lastHandshakeAgeS": _last_handshake_age(
                    raw_peer.get("LastHandshake"), now=observed_at
                ),
            }
        )
    peers.sort(key=lambda item: str(item["name"]))
    return {
        "self": {"homeDerp": _nullable_string(self_status.get("Relay"))},
        "peers": peers,
    }


#: Connect errors meaning "no route to that address from here" -- a missing
#: tailnet route (e.g. utun gone), not an unanswered SYN.
_UNREACHABLE_ERRNOS = frozenset({errno.EHOSTUNREACH, errno.ENETUNREACH})
_TIMEOUT_ERRNOS = frozenset({errno.ETIMEDOUT, errno.EAGAIN})


def tcp_probe(
    host: str,
    port: int,
    *,
    timeout: float = PEER_CONNECT_START_TIMEOUT_SECONDS,
) -> dict[str, object]:
    """Run one raw TCP connect and keep each failure mechanism distinct.

    ``refused``: the host answered and nothing listens.  ``timeout``: the SYN
    went unanswered.  ``unreachable``: no route from here.  ``error``:
    anything else (name resolution, EADDRNOTAVAIL, ...), with its errno.  Each
    has a different remedy, so none is folded into another (infra-ops review
    of #851: labelling them all timeout recreates the old "closed" lumping).
    ``socket`` does not consult HTTP proxy environment variables.
    """

    try:
        with socket.create_connection((host, port), timeout=timeout):
            return {"result": "open", "errno": None}
    except socket.gaierror as error:
        return {"result": "error", "errno": error.errno}
    except TimeoutError:
        return {"result": "timeout", "errno": None}
    except OSError as error:
        error_number = error.errno
        if error_number == errno.ECONNREFUSED:
            return {"result": "refused", "errno": error_number}
        if error_number in _UNREACHABLE_ERRNOS:
            return {"result": "unreachable", "errno": error_number}
        if error_number in _TIMEOUT_ERRNOS:
            return {"result": "timeout", "errno": error_number}
        return {"result": "error", "errno": error_number}


def classify_peer_reachability(
    peer: Mapping[str, object], probe: Mapping[str, object]
) -> dict[str, object] | None:
    """Turn status plus one probe into a PR-1 peer verdict, if any."""

    result = probe.get("result")
    if result == "open":
        return None
    verdict: str | None = None
    if result == "refused":
        verdict = "refused"
    elif result == "timeout" and peer.get("online") is True:
        verdict = "data_plane_down"
    elif result == "unreachable":
        verdict = "no_route"
    elif result == "error":
        verdict = "probe_error"
    if verdict is None:
        return None
    return {
        "peer": peer.get("name"),
        "verdict": verdict,
        "probe": {
            "result": result,
            "errno": probe.get("errno"),
        },
        "path": peer.get("path"),
        "homeDerp": peer.get("homeDerp"),
        "lastHandshakeAgeS": peer.get("lastHandshakeAgeS"),
    }
