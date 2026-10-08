"""Where a node looks for other nodes to connect to.

Discovery answers one question and returns one kind of thing:

    "what endpoints can I try to connect to right now?"

**Endpoints, deliberately -- not addresses, and not peers.** The hyprial-tailcat
sidecar sits between hyprial and the network: hyprial keeps connecting to
``tcp/127.0.0.1:<port>`` while the sidecar forwards over Tailcat. A node never
sees a Tailcat address at all -- what it dials is a local forwarding port. The
sidecar learns *which* peers to map from the organization directory (identity
domain): ``ForwardingEndpoints`` asks its ``directory`` callback for
``deviceId -> Tailcat address``, maps what is missing and unmaps what is gone.

Discovery does NOT decide who may talk to whom. It reports what is reachable;
whether a connection is permitted is the sidecar's business (the ``allow`` set
it was brought up with).

⚠️ Note on gossip: zenoh can also discover peers by having connected peers
introduce each other, and that is deliberately NOT used here. It only works
when the introduced address is dialable by whoever receives it -- which is
never the case when every node listens on localhost behind a sidecar. Where
there is no authoritative peer directory -- a real LAN, a single static seed --
gossip is the right tool; here we have a directory, so we ask it.
"""

from __future__ import annotations

import subprocess
import time
from collections.abc import Callable, Mapping
from typing import Protocol, runtime_checkable

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
    """The daemon-owned protocol-v3 sidecar control surface."""

    def status(self) -> tuple[tuple[str, ...], dict[str, int]]: ...

    def map_peer(self, peer: str, address: str) -> int: ...

    def unmap_peer(self, peer: str) -> None: ...

    def close(self) -> None: ...


class ForwardingEndpoints:
    """Swap directory peer addresses for sidecar-reported loopback endpoints.

    ``EndpointDiscovery`` still returns endpoint strings; only the backend
    changes. The controller owns the long-lived sidecar process and this
    object owns that controller for the daemon lifetime.

    ``directory`` answers ``deviceId -> Tailcat address`` for every org member
    except this node; for members missing a local mapping the sidecar is asked
    to ``map-peer`` with that address, and mappings the directory no longer
    names are unmapped.
    """

    def __init__(
        self,
        controller: ForwardingController,
        *,
        on_failure: Callable[[str], None] | None = None,
        on_success: Callable[[], None] | None = None,
        directory: Callable[[], Mapping[str, str]] | None = None,
    ) -> None:
        self._controller = controller
        self._on_failure = on_failure
        self._on_success = on_success
        self._directory = directory if directory is not None else (lambda: {})
        # The last poll, for status/ps: who the sidecar reported and which
        # local port each maps to, or why the poll failed.  Without it
        # "which peers are we dialing?" had no answer anywhere (2026-09-24).
        self._last_poll: dict[str, object] | None = None

    @property
    def controller(self) -> ForwardingController:
        """The owned control surface; the supervisor reads ``pid`` from it."""

        return self._controller

    def list_reachable_endpoints(self) -> tuple[str, ...]:
        try:
            wanted = {
                str(peer): str(address)
                for peer, address in self._directory().items()
            }
            _reported, mappings = self._controller.status()
            for stale in sorted(set(mappings) - set(wanted)):
                self._controller.unmap_peer(stale)
                mappings.pop(stale, None)
            for peer in sorted(wanted):
                if peer not in mappings:
                    mappings[peer] = self._controller.map_peer(peer, wanted[peer])
            endpoints: list[str] = []
            for peer in sorted(wanted):
                port = mappings.get(peer)
                if not isinstance(port, int) or port <= 0 or port > 65535:
                    raise ValueError(
                        f"sidecar returned invalid local port for {peer}: {port!r}"
                    )
                endpoints.append(f"tcp/127.0.0.1:{port}")
        except Exception as error:  # noqa: BLE001 - discovery stays best effort
            self._last_poll = {
                "atMs": time.time_ns() // 1_000_000,
                "ok": False,
                "error": str(error)[:300],
                "peersReported": getattr(self._controller, "peers_reported", None),
            }
            if self._on_failure is not None:
                self._on_failure(str(error))
            return ()
        self._last_poll = {
            "atMs": time.time_ns() // 1_000_000,
            "ok": True,
            "error": None,
            "peersReported": getattr(self._controller, "peers_reported", None),
            "peers": {
                peer: f"tcp/127.0.0.1:{mappings[peer]}" for peer in sorted(wanted)
            },
        }
        # The success hook is what lets an owner tell "answered again" from
        # "never answered": a wedged child that recovers must be visible as
        # a state change, not inferred from the absence of failure events.
        if self._on_success is not None:
            self._on_success()
        return tuple(endpoints)

    @property
    def last_poll(self) -> dict[str, object] | None:
        """The most recent poll's outcome, or None before the first one."""

        return self._last_poll

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


class CommandEndpoints:
    """Endpoints from a command that prints one per line.

    The escape hatch for every deployment this repository has not met: a shell
    script wrapping an internal service registry, a DNS query, a file someone
    keeps updated.  It also makes the multi-node case testable on one host --
    a directory free to name ports can put three nodes on one machine.

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
