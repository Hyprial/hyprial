"""Detect duplicate live instances of this node identity on the mesh.

Two daemons sharing one node identity (the 2026-09-14 jjkysy-dev incident:
a copied home ran a second daemon for 34 hours) previously declared
byte-identical liveliness keys, so no observer could tell one instance from
two and nothing alerted.  This component declares a generation-bearing
liveliness token (``liveliness/daemon/<node>/<generation>``) and watches the
same node's wildcard: a token carrying a *different* generation is another
live process serving this node identity, so both sides emit
``daemon.identity.duplicate_instance`` at error level.

Detection and alarm only -- no automatic remediation (no killing, no forced
offline).  De-duplication is by peer generation for this process's lifetime:
one alert per distinct peer generation, not one per liveliness event.  A
same-generation reconnect re-declares the very key this process already
discounts, so it never alerts.

Coverage limit, stated plainly: a peer running a version that predates this
token declares nothing under ``liveliness/daemon/`` and is therefore
invisible here.  That case is covered on one machine by the startup
copied-home detection in ``home_guard``; across machines it is not covered.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any

from hyprial.transport.api import Registration, TransportSample, TransportSession
from hyprial.transport.keys import KeySpace

DUPLICATE_INSTANCE_EVENT = "daemon.identity.duplicate_instance"

Logger = Callable[..., None]


class DuplicateInstanceWatch:
    """Declare this generation's token; alert on foreign generations."""

    def __init__(
        self,
        session: TransportSession,
        node_id: str,
        generation: str,
        *,
        keys: KeySpace | None = None,
        logger: Logger | None = None,
    ) -> None:
        if not node_id:
            raise ValueError("node_id must not be empty")
        if not generation:
            raise ValueError("generation must not be empty")
        self._keys = keys or KeySpace()
        self._node_id = node_id
        self._generation = generation
        self._logger = logger
        self._lock = threading.Lock()
        self._reported: set[str] = set()
        # Declare before observing: history=True replays the current token
        # set (including this one), and the generation-equality discount in
        # _on_sample is what keeps our own declaration from alerting.
        self._token: Registration = session.declare_liveliness(
            self._keys.daemon_liveliness(node_id, generation)
        )
        try:
            self._observation: Registration = session.observe_liveliness(
                self._keys.daemon_liveliness_for_node(node_id),
                self._on_sample,
                history=True,
            )
        except BaseException:
            self._token.close()
            raise

    def _on_sample(self, sample: TransportSample) -> None:
        if sample.kind == "delete":
            # A departing peer is not a duplicate; the generation stays in
            # _reported so a flapping peer does not re-alert on every
            # reconnect of the same process.
            return
        peer_generation = self._keys.decode_identity(sample.key.rsplit("/", 1)[-1])
        if peer_generation == self._generation:
            # Our own token (history replay) or a same-process reconnect:
            # same node identity AND same generation is not a duplicate.
            return
        with self._lock:
            if peer_generation in self._reported:
                return
            self._reported.add(peer_generation)
        self._log(
            "error",
            DUPLICATE_INSTANCE_EVENT,
            nodeId=self._node_id,
            generation=self._generation,
            peerGeneration=peer_generation,
            detail=(
                "another live daemon generation serves this node identity; "
                "detection and alarm only, no automatic remediation"
            ),
        )

    def _log(self, level: str, event: str, **fields: Any) -> None:
        if self._logger is None:
            return
        try:
            self._logger(level, event, **fields)
        except Exception:  # noqa: BLE001 - telemetry must not break the watch
            pass

    @property
    def duplicates(self) -> tuple[str, ...]:
        """Every peer generation this process has alerted on, sorted."""

        with self._lock:
            return tuple(sorted(self._reported))

    def status_payload(self) -> dict[str, Any]:
        peers = list(self.duplicates)
        return {"active": bool(peers), "meshPeerGenerations": peers}

    def close(self) -> None:
        self._observation.close()
        self._token.close()
