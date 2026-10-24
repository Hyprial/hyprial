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
from collections.abc import Callable
from typing import Protocol, runtime_checkable

# A directory query runs on the daemon's startup path, so it is bounded. A
# discovery backend that hangs must degrade to "no endpoints today" rather
# than hold up a daemon that has perfectly good configured endpoints.
DISCOVERY_TIMEOUT_SECONDS = 5.0


#: The ``map-peer`` slot spec asking for one new kernel-assigned loopback
#: TCP slot (pool growth on the tcp transport); the sidecar answers with
#: the resolved ``tcp:127.0.0.1:<port>`` id.
TCP_SLOT_GROW_SPEC = "tcp:127.0.0.1:0"


def slot_locator(slot_id: str) -> str:
    """The zenoh locator dialing one slot wire id.

    A wire id is what ``map-peer.slot`` names: an absolute socket path
    (unix transport) or ``tcp:127.0.0.1:<port>`` (tcp transport, the
    Windows scheme -- zenoh has no unixsock-stream there).
    """

    if slot_id.startswith("tcp:"):
        return f"tcp/{slot_id[4:]}"
    return f"unixsock-stream/{slot_id}"


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
    """The daemon-owned protocol-v5 sidecar control surface."""

    def status(self) -> tuple[tuple[str, ...], dict[str, str]]: ...

    def map_peer(self, peer: str, address: str, slot: str) -> str: ...

    def unmap_peer(self, peer: str) -> None: ...

    def close(self) -> None: ...


class ForwardingEndpoints:
    """The fixed slot pool as an ``EndpointDiscovery`` (design §5).

    ``EndpointDiscovery`` still returns endpoint strings; only the backend
    changes. The controller owns the long-lived sidecar process and this
    object owns that controller for the daemon lifetime.

    The endpoint answer is the FIXED slot set -- independent of membership,
    of the directory, and even of whether the sidecar currently answers --
    so the zenoh connect set never changes while members come and go. The
    fixed pool is the only way forwarding dials (there is no rollback to
    per-peer loopback ports). Mapping is not discovery's job: the slot
    reconcile (and the S2b peer-access actor) calls :meth:`bind` /
    :meth:`unbind`, and the poll here only keeps the health hooks and
    ``last_poll`` fed.
    """

    def __init__(
        self,
        controller: ForwardingController,
        *,
        on_failure: Callable[[str], None] | None = None,
        on_success: Callable[[], None] | None = None,
        slots: Callable[[], tuple[str, ...]],
        slot_transport: str = "unix",
        on_slots_reported: Callable[[tuple[str, ...]], None] | None = None,
    ) -> None:
        self._controller = controller
        self._on_failure = on_failure
        self._on_success = on_success
        self._slots = slots
        self._slot_transport = slot_transport
        self._on_slots_reported = on_slots_reported
        # What this incarnation already asked the sidecar to map
        # (label -> (address, slot)): makes bind() idempotent per sidecar
        # process, and dies with the backend so a relaunched sidecar is
        # re-told everything.
        self._bound: dict[str, tuple[str, int]] = {}
        # tcp transport only: slot index -> wire id.  Ports are kernel-
        # assigned per sidecar incarnation, so this table is seeded from
        # the sidecar-reported ``up`` list and extended by growth binds;
        # like ``_bound`` it dies with the backend.
        self._slot_ids: dict[int, str] = {}
        # The last poll, for status/ps: which slot each mapped peer dials,
        # or why the poll failed.  Without it "which peers are we dialing?"
        # had no answer anywhere (2026-09-24).
        self._last_poll: dict[str, object] | None = None

    @property
    def controller(self) -> ForwardingController:
        """The owned control surface; the supervisor reads ``pid`` from it."""

        return self._controller

    @property
    def slot_mode(self) -> bool:
        """True: the fixed slot pool is the only backend shape."""

        return True

    def list_reachable_endpoints(self) -> tuple[str, ...]:
        return self._slot_endpoints()

    def _slot_endpoints(self) -> tuple[str, ...]:
        """The fixed slot set, with the status poll kept for health only.

        Unix transport: the endpoints are returned even when the poll
        fails -- a slot's socket path outlives any sidecar incarnation,
        and zenoh redialing a momentarily dead socket is harmless; that
        indifference is the whole point of the fixed pool.  TCP transport
        (the Windows scheme): the set is whatever this incarnation
        reported at ``up`` plus growth binds, memoized so a failed poll
        keeps the last known set rather than emptying the connect set.
        """

        endpoints = tuple(self.slot_locators().values())
        try:
            self._controller.status()
        except Exception as error:  # noqa: BLE001 - health stays best effort
            self._last_poll = {
                "atMs": time.time_ns() // 1_000_000,
                "ok": False,
                "error": str(error)[:300],
                "peersReported": getattr(self._controller, "peers_reported", None),
            }
            if self._on_failure is not None:
                self._on_failure(str(error))
            return endpoints
        by_index = self.slot_locators()
        self._last_poll = {
            "atMs": time.time_ns() // 1_000_000,
            "ok": True,
            "error": None,
            "peersReported": getattr(self._controller, "peers_reported", None),
            "connectionTelemetry": getattr(
                self._controller, "connection_telemetry", None
            ),
            "derpMapURL": getattr(self._controller, "derp_map_url", None),
            "derpMapSource": getattr(self._controller, "derp_map_source", None),
            "peers": {
                label: by_index[slot]
                for label, (_address, slot) in sorted(self._bound.items())
                if slot in by_index
            },
        }
        if self._on_success is not None:
            self._on_success()
        return endpoints

    def slot_locators(self) -> dict[int, str]:
        """Slot index -> zenoh locator, for this backend's transport.

        Unix: derived statically from the ``slots`` callable (the store's
        fixed set, growth included).  TCP: the sidecar-reported ids of
        this incarnation plus growth binds; empty until the sidecar's
        ``up`` answer has been seen.
        """

        if self._slot_transport == "tcp":
            self._seed_slot_ids()
            return {
                index: slot_locator(slot_id)
                for index, slot_id in sorted(self._slot_ids.items())
            }
        return dict(enumerate(self._slots()))

    def _seed_slot_ids(self) -> None:
        """Populate the tcp index table from the sidecar's ``up`` report."""

        if self._slot_ids:
            return
        reported = getattr(self._controller, "slots_reported", None)
        if isinstance(reported, (list, tuple)) and all(
            isinstance(slot_id, str) for slot_id in reported
        ):
            slot_ids = tuple(reported)
            self._slot_ids = dict(enumerate(slot_ids))
            if self._on_slots_reported is not None:
                self._on_slots_reported(slot_ids)

    def _slot_wire_id(self, slot: int) -> str:
        """What ``map-peer.slot`` names for one slot index.

        Unix: the socket path, stripped from the static locator (the
        store grew before any growth bind, so the index is always
        covered).  TCP: the incarnation's reported id, or the grow spec
        asking the sidecar to open a new kernel-assigned listener.
        """

        if self._slot_transport == "tcp":
            self._seed_slot_ids()
            return self._slot_ids.get(slot, TCP_SLOT_GROW_SPEC)
        endpoints = tuple(self._slots())
        if slot >= len(endpoints):
            raise ValueError(f"slot {slot} is beyond the fixed set of {len(endpoints)}")
        locator = endpoints[slot]
        prefix = "unixsock-stream/"
        if not locator.startswith(prefix):
            raise ValueError(f"slot {slot} locator is not a unix slot: {locator!r}")
        return locator[len(prefix):]

    def bind(self, peer: str, address: str, slot: int) -> bool:
        """``map-peer`` onto a slot; True when a wire call was made.

        Idempotent per sidecar incarnation: re-asserting the same
        ``(address, slot)`` is free, a changed address on the same slot is
        the §2 rebind (the sidecar drops the old splice and the dial
        reaches the new peer), and a relaunched sidecar starts with an
        empty memo so everything is re-told.
        """

        if self._bound.get(peer) == (address, slot):
            return False
        result = self._controller.map_peer(peer, address, self._slot_wire_id(slot))
        if self._slot_transport == "tcp":
            # A growth bind's answer is the new slot's resolved id; a
            # rebind echoes the known one.  Either way the table holds
            # the truth for this incarnation.
            self._slot_ids[slot] = result
            if self._on_slots_reported is not None:
                self._on_slots_reported(
                    tuple(self._slot_ids[index] for index in sorted(self._slot_ids))
                )
        self._bound[peer] = (address, slot)
        return True

    def unbind(self, peer: str) -> None:
        """``unmap-peer`` and forget; the slot returns to hold mode."""

        self._controller.unmap_peer(peer)
        self._bound.pop(peer, None)

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
