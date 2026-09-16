"""Where a node looks for other nodes to connect to.

Bootstrap used to be a constant in a document: `docs/onboarding-checklist.md`
named HQ's tailnet address and every joining machine typed it in. That is a
property of one deployment, not a design -- HQ changing address means everyone
edits by hand with nothing tracking who did, and the line means nothing to
anyone else running this software.

The replacement is a peer directory the node can ask. Discovery answers one
question and returns one kind of thing:

    "what endpoints can I try to connect to right now?"

**Endpoints, deliberately -- not addresses, and not peers.** The name matters
because it is what keeps two futures open:

* The planned tsnet migration (`notes/tsnet-feasibility-verdict-2026-08-26.md`)
  puts a Go sidecar between hyprial and the network: hyprial keeps connecting to
  `tcp/127.0.0.1:<port>` while the sidecar forwards over the tailnet. After
  that, a node never sees a tailnet address at all -- what it dials is a local
  forwarding port. An interface returning "tailnet peers" would have to be
  rewritten; one returning endpoints only swaps its backend.
* Cross-tailnet follows for free. A sidecar joined to two tailnets exposes a
  local port per remote peer, and to hyprial those are simply more endpoints. Had
  this interface been spelled `list_tailnet_peers()`, cross-tailnet would have
  been designed out of it before anyone tried.

Discovery does NOT decide who may talk to whom. It reports what is reachable;
whether a connection is permitted is the transport's business (today, the
tailnet boundary).

⚠️ Note on gossip: zenoh can also discover peers by having connected peers
introduce each other, and that is deliberately NOT used here. Two reasons,
both measured rather than assumed. It only works when the introduced address
is dialable by whoever receives it -- so it does nothing when members do not
listen, and nothing after the tsnet migration where every node listens on
localhost. And it introduces peers without consulting the tailnet ACL, so
visibility would spill past the boundary that is meant to contain it. Where
there is no authoritative peer directory -- a real LAN, a single static seed --
gossip is the right tool; here we have a directory, so we ask it.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from collections.abc import Callable
from typing import Protocol, runtime_checkable

from hyprial.contracts.forwarding import DEFAULT_PEER_PORT

# A directory query runs on the daemon's startup path, so it is bounded. A
# discovery backend that hangs must degrade to "no endpoints today" rather
# than hold up a daemon that has perfectly good configured endpoints.
DISCOVERY_TIMEOUT_SECONDS = 5.0


@runtime_checkable
class EndpointDiscovery(Protocol):
    """A source of endpoints this node could try to connect to."""

    def list_reachable_endpoints(self) -> tuple[str, ...]:
        """Zenoh endpoint strings, best effort, never raising.

        Returning empty is a legitimate answer -- no directory, nothing
        online yet, the tool absent. Callers combine this with explicitly
        configured endpoints, so an empty result degrades to today's
        behaviour instead of isolating the node.
        """
        ...


@runtime_checkable
class ForwardingController(Protocol):
    """The daemon-owned protocol-v2 sidecar control surface."""

    def status(self) -> tuple[tuple[str, ...], dict[str, int]]: ...

    def map_peer(self, peer: str) -> int: ...

    def unmap_peer(self, peer: str) -> None: ...

    def close(self) -> None: ...


class ForwardingEndpoints:
    """Swap peer addresses for sidecar-reported loopback endpoints.

    ``EndpointDiscovery`` still returns endpoint strings; only the backend
    changes. The controller owns the long-lived sidecar process and this
    object owns that controller for the daemon lifetime.
    """

    def __init__(
        self,
        controller: ForwardingController,
        *,
        on_failure: Callable[[str], None] | None = None,
    ) -> None:
        self._controller = controller
        self._on_failure = on_failure

    def list_reachable_endpoints(self) -> tuple[str, ...]:
        try:
            raw_peers, mappings = self._controller.status()
            peers = tuple(sorted(set(raw_peers)))
            wanted = set(peers)
            for stale in sorted(set(mappings) - wanted):
                self._controller.unmap_peer(stale)
                mappings.pop(stale, None)
            for peer in peers:
                if peer not in mappings:
                    mappings[peer] = self._controller.map_peer(peer)
            endpoints: list[str] = []
            for peer in peers:
                port = mappings.get(peer)
                if not isinstance(port, int) or port <= 0 or port > 65535:
                    raise ValueError(
                        f"sidecar returned invalid local port for {peer}: {port!r}"
                    )
                endpoints.append(f"tcp/127.0.0.1:{port}")
            return tuple(endpoints)
        except Exception as error:  # noqa: BLE001 - discovery stays best effort
            if self._on_failure is not None:
                self._on_failure(str(error))
            return ()

    def close(self) -> None:
        self._controller.close()


class StaticEndpoints:
    """Whatever was configured explicitly -- the fallback that always works.

    Kept as a first-class backend rather than a special case: it is the only
    one available when there is no directory to ask, and it is what tests and
    isolated topologies use.
    """

    def __init__(self, endpoints: tuple[str, ...] = ()) -> None:
        self._endpoints = endpoints

    def list_reachable_endpoints(self) -> tuple[str, ...]:
        return self._endpoints


class TailscaleEndpoints:
    """Peers as the local tailscale daemon currently sees them.

    The directory is scoped to the tailnet by construction, not by a setting
    we could get wrong -- and it is subject to the tailnet ACL, so visibility
    follows the same boundary that governs whether a connection would be
    permitted at all.

    Self is absent because `tailscale status` reports it under `Self`, not
    `Peer` -- stated here because "we exclude self" would suggest a filter
    that does not exist, and someone changing the parse would not know to
    preserve it.

    ⚠️ Every online peer is offered, including ones that do not run hyprial at
    all -- infrastructure nodes, phones, a colleague's laptop. The directory
    knows who is on the tailnet, not who speaks this protocol, and nothing
    short of trying can tell the difference. That is a real cost (one doomed
    connection attempt per non-hyprial peer, retried on the transport's backoff)
    and it is accepted here rather than hidden: the alternative is a tag or
    registry that has to be maintained, which is the constant problem again
    wearing different clothes.
    """

    def __init__(
        self,
        *,
        port: int = DEFAULT_PEER_PORT,
        timeout: float = DISCOVERY_TIMEOUT_SECONDS,
        runner: object | None = None,
    ) -> None:
        self._port = port
        self._timeout = timeout
        self._runner = runner

    def list_reachable_endpoints(self) -> tuple[str, ...]:
        status = self._read_status()
        if status is None:
            return ()
        endpoints: list[str] = []
        peers = status.get("Peer")
        if not isinstance(peers, dict):
            return ()
        for peer in peers.values():
            if not isinstance(peer, dict):
                continue
            # Offline peers are skipped, not because dialing them would be
            # harmful, but because the directory already knows the answer and
            # a doomed connection attempt costs a retry budget that a genuinely
            # slow peer needs.
            if not peer.get("Online"):
                continue
            for address in peer.get("TailscaleIPs") or ():
                if not isinstance(address, str) or not address:
                    continue
                # IPv6 needs brackets in a host:port endpoint; zenoh parses
                # the locator textually and would otherwise split on the
                # address's own colons.
                host = f"[{address}]" if ":" in address else address
                endpoints.append(f"tcp/{host}:{self._port}")
        return tuple(endpoints)

    def _read_status(self) -> dict[str, object] | None:
        """The tailscale view, or None when it cannot be had.

        Every failure mode collapses to None on purpose: no tailscale
        installed, a daemon that is not running, a timeout, output that is not
        JSON. None means "this backend has nothing to say", and the caller
        still has its configured endpoints. A discovery backend that could
        raise would turn "tailscale is having a moment" into "the daemon will
        not start".
        """

        if self._runner is not None:
            try:
                return self._runner()  # type: ignore[operator]
            except Exception:  # noqa: BLE001 - a backend must not break startup
                return None
        executable = shutil.which("tailscale")
        if executable is None:
            return None
        try:
            completed = subprocess.run(
                [executable, "status", "--json"],
                capture_output=True,
                timeout=self._timeout,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if completed.returncode != 0:
            return None
        try:
            parsed = json.loads(completed.stdout)
        except (ValueError, TypeError):
            return None
        return parsed if isinstance(parsed, dict) else None


class CommandEndpoints:
    """Endpoints from a command that prints one per line.

    The escape hatch for every deployment this repository has not met: a shell
    script wrapping an internal service registry, a DNS query, a file someone
    keeps updated.  It also makes the multi-node case testable on one host --
    the well-known port is a property of the tailscale backend, not of
    discovery, and a directory free to name ports can put three nodes on one
    machine.

    Failure is silence, for the same reason as every other backend.
    """

    def __init__(
        self,
        command: tuple[str, ...],
        *,
        timeout: float = DISCOVERY_TIMEOUT_SECONDS,
    ) -> None:
        self._command = command
        self._timeout = timeout

    def list_reachable_endpoints(self) -> tuple[str, ...]:
        if not self._command:
            return ()
        try:
            completed = subprocess.run(
                list(self._command),
                capture_output=True,
                timeout=self._timeout,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return ()
        if completed.returncode != 0:
            return ()
        text = completed.stdout.decode("utf-8", "replace")
        return tuple(line.strip() for line in text.splitlines() if line.strip())


def local_tailnet_endpoint(
    *,
    port: int = DEFAULT_PEER_PORT,
    timeout: float = DISCOVERY_TIMEOUT_SECONDS,
    runner: object | None = None,
) -> str | None:
    """This node's own tailnet address as a listen endpoint, or None.

    A node that only connects out is invisible: peers can find its address in
    the directory and still have nothing to dial.  Worse, it feels perfectly
    healthy from the inside -- it sees everyone it dialed -- so "nobody can
    see me" is not observable from the machine it is true of.  That is the
    mechanism that let member-to-member blindness survive unnoticed.

    ⚠️ Deliberately the tailnet address and never `0.0.0.0`.  Binding every
    interface would put the port on whatever else the host is attached to,
    and the whole security argument here rests on the tailnet being the only
    way in.  A default that quietly widened that would be worse than no
    default at all.
    """

    status = TailscaleEndpoints(
        port=port, timeout=timeout, runner=runner
    )._read_status()
    if status is None:
        return None
    own = status.get("Self")
    if not isinstance(own, dict):
        return None
    for address in own.get("TailscaleIPs") or ():
        if not isinstance(address, str) or not address:
            continue
        host = f"[{address}]" if ":" in address else address
        return f"tcp/{host}:{port}"
    return None


def merge_endpoints(*sources: tuple[str, ...]) -> tuple[str, ...]:
    """Configured endpoints first, discovered ones after, no duplicates.

    Order is kept rather than sorted: an operator who named an endpoint
    explicitly gets it tried first, which makes a deliberately pinned peer
    behave predictably instead of racing a directory of twenty.
    """

    seen: set[str] = set()
    merged: list[str] = []
    for source in sources:
        for endpoint in source:
            if endpoint in seen:
                continue
            seen.add(endpoint)
            merged.append(endpoint)
    return tuple(merged)
