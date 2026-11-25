"""Isolation contract for daemons that opt into real Tailcat forwarding.

``IsolatedDaemon`` normally sets HYPRIAL_NETWORK_ISOLATED=1, which forbids the
forwarding backend. A scenario that exercises the real ``hyprial-tailcat``
sidecar (E2E-025) must drop that switch -- but only through the
``tailcat_forwarding=True`` opt-in, which replaces the blanket switch with the
narrower guarantees asserted here: loopback-only Zenoh endpoints, no peer
discovery, no gossip, no usage fetch, and (the scenario's own half) a local
labrelay DERP so even tunnel traffic never leaves loopback.
"""

from __future__ import annotations
from pathlib import Path

from typing import Any

#: The opt-in environment: forwarding allowed, everything else closed.
TAILCAT_FORWARDING_ENV = {
    "HYPRIAL_NETWORK_ISOLATED": "0",
    "HYPRIAL_USAGE_DISABLE": "1",
    "HYPRIAL_ZENOH_GOSSIP": "0",
}

#: The same opt-in with peer gossip turned ON, for the S1/v12 acceptance whose repair
#: face is "make the peers visible to each other": with gossip off a daemon reaches
#: exactly the endpoints it was told about, so a joiner that is not directly connected
#: to the holder can see neither the token nor its holder (measured 2026-10-12: an
#: active enumeration returned count=0 on all six sides).  This is a SEPARATE constant
#: rather than a default so that no scenario drifts into gossip by accident, and the
#: caller must additionally pass ``gossip=True`` to IsolatedDaemon for the guard below
#: to accept the value.
TAILCAT_FORWARDING_ENV_WITH_GOSSIP = {
    **TAILCAT_FORWARDING_ENV,
    "HYPRIAL_ZENOH_GOSSIP": "1",
}


def tailcat_forwarding_env(*, gossip: bool = False) -> dict[str, str]:
    """The forwarding opt-in's environment; gossip only when explicitly asked for.

    A copy is returned every time, so a caller cannot mutate either constant by
    editing the dict it received.
    """

    return dict(TAILCAT_FORWARDING_ENV_WITH_GOSSIP if gossip else TAILCAT_FORWARDING_ENV)


def require_tailcat_forwarding_isolation(daemon: Any) -> None:
    """The forwarding opt-in's replacement for require_network_isolated.

    Only meaningful for a daemon constructed with
    ``tailcat_forwarding=True``; every other caller gets an assertion
    failure, so the weaker check can never be taken by accident.  The
    environment half is verified from the launch dict (discovery off,
    usage off, and gossip exactly as the daemon itself declared it -- off
    unless the caller opted in with ``gossip=True``) and the effective half
    from the daemon's own ``ps``: every Zenoh listen/connect endpoint must be
    local-only (see ``endpoint_is_local_only``).  The
    Tailcat side itself is pinned to the scenario's local labrelay by
    the runner's HYPRIAL_TAILCAT_DERP; a host tailscale no longer
    exists in the product at all.
    """

    if not daemon.tailcat_forwarding:
        raise AssertionError(
            "require_tailcat_forwarding_isolation without the "
            "tailcat_forwarding=True opt-in"
        )
    # The gossip value must equal what THIS daemon declared, in either direction: a
    # caller that opted in and then lost the setting is as much a weakened isolation
    # claim as one that never opted in but carries gossip on.  Everything else stays
    # pinned to the single closed value.
    declared_gossip = "1" if getattr(daemon, "gossip", False) else "0"
    env_problems = [
        f"{name}={daemon.env.get(name)!r}"
        for name, wanted in (
            ("HYPRIAL_NETWORK_ISOLATED", "0"),
            ("HYPRIAL_PEER_DISCOVERY", "0"),
            ("HYPRIAL_USAGE_DISABLE", "1"),
            ("HYPRIAL_ZENOH_GOSSIP", declared_gossip),
        )
        if daemon.env.get(name) != wanted
    ]
    if env_problems:
        raise AssertionError(
            "forwarding isolation env was weakened after construction: "
            + ", ".join(env_problems)
        )
    status = daemon.ps()
    zenoh = status.get("zenoh") if isinstance(status.get("zenoh"), dict) else {}
    endpoints = [*zenoh.get("listen", []), *zenoh.get("connect", [])]
    remote = local_topology_violations(endpoints, getattr(daemon, "root", None))
    if remote:
        try:
            daemon.stop()
        finally:
            raise AssertionError(
                "forwarding test daemon escaped the loopback topology: "
                f"{remote}; refusing to run"
            )

# --- the loopback-topology predicate, extended 2026-10-13 by hq's ruling (path A) -------------------
# WHAT CHANGED AND WHY, so a later reader does not mistake this for a patch that makes a run pass:
# the guard's PURPOSE is "no ROUTABLE endpoint", not "only TCP".  A unix socket lives in the
# filesystem domain and is therefore MORE local than loopback TCP, not less -- under
# HYPRIAL_ZENOH_GOSSIP=1 the daemon listens on unixsock-stream slots inside the round directory,
# which is the topology the v12 configuration deliberately creates.  The predicate now accepts
# exactly two shapes: loopback TCP, and a filesystem-domain endpoint whose path is inside the
# round directory.  EVERYTHING ELSE, INCLUDING EVERY ROUTABLE ENDPOINT, IS STILL REFUSED, and the
# negative control for that is exercised by tests/tooling against this function.
_FILESYSTEM_DOMAINS = ("unixsock-stream", "unixsock", "unix")


def endpoint_is_local_only(endpoint: str, round_root: "Path | str | None") -> bool:
    """True when the endpoint cannot reach anything outside this machine.

    Accepts loopback TCP (``tcp/127.0.0.1:PORT``, ``tcp/[::1]:PORT``) and filesystem-domain
    sockets (``unixsock-stream//path``) whose path is inside *round_root*.  A missing
    *round_root* refuses filesystem endpoints rather than assuming they are fine: the check
    must be able to fail.
    """
    text = str(endpoint)
    if text.startswith(("tcp/127.0.0.1:", "tcp/[::1]:")):
        return True
    protocol, _, address = text.partition("/")
    if protocol not in _FILESYSTEM_DOMAINS:
        return False
    if round_root is None:
        return False
    # unixsock-stream//private/tmp/... arrives with a doubled leading slash.  Strip the slashes
    # and restore EXACTLY ONE, so the result is still an ABSOLUTE path: the first version of this
    # helper stripped them and left a relative path, which was then joined onto the round root and
    # therefore always landed inside it -- the negative control caught it, which is why the control
    # exists.
    candidate = Path("/" + address.lstrip("/"))
    root = Path(str(round_root))
    try:
        candidate = candidate.resolve()
        root_resolved = root.resolve()
    except OSError:
        return False
    return candidate == root_resolved or root_resolved in candidate.parents


def local_topology_violations(endpoints, round_root) -> list:
    """The endpoints that are NOT local-only.  Empty list means the topology is acceptable."""
    return [e for e in endpoints if not endpoint_is_local_only(e, round_root)]
