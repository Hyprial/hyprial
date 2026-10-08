"""Bounded TCP diagnostics for mesh peers.

The tailnet status projection that used to live here was deleted with the
tailnet cutover (2026-10-03): there is no host tailscale to project any
more.  What remains is the one mechanism-independent probe (``tcp_probe``)
and the verdict classifier the doctor command feeds it.
"""

from __future__ import annotations

import errno
import socket
from typing import Mapping


# Registered in ``tests/supervision_exemptions.json``. This bounds one raw TCP
# connect from the on-demand doctor command; it starts no lifecycle or retry.
PEER_CONNECT_START_TIMEOUT_SECONDS = 2.0


#: Connect errors meaning "no route to that address from here" -- a missing
#: route, not an unanswered SYN.
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
