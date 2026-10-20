"""Endpoint-role and lifetime diagnostics for native Zenoh link events."""

from __future__ import annotations

import threading
import time
from collections.abc import Mapping
from typing import Any


def _locator_port(locator: str) -> int | None:
    try:
        return int(locator.split("?", 1)[0].rsplit(":", 1)[1])
    except (IndexError, ValueError):
        return None


class ZenohLinkDiagnosticsMixin:
    _diagnostic_guard: threading.Lock
    _link_opened_at: dict[
        tuple[str, str, str], tuple[float, str | None, int | None, int | None]
    ]
    _endpoint_peers: dict[str, str]
    _peer_endpoint_history: dict[str, str]
    _map_peer_remap_count: int
    _zenoh_link_delete_count: int

    def set_endpoint_peers(self, peers: Mapping[str, str]) -> None:
        """Associate current sidecar map endpoints with Hyprial peer nodes."""

        current = {str(endpoint): str(peer) for peer, endpoint in peers.items()}
        remaps: list[tuple[str, str, str, int]] = []
        with self._diagnostic_guard:
            for endpoint, peer in current.items():
                previous = self._peer_endpoint_history.get(peer)
                if previous is not None and previous != endpoint:
                    self._map_peer_remap_count += 1
                    remaps.append(
                        (peer, previous, endpoint, self._map_peer_remap_count)
                    )
                self._peer_endpoint_history[peer] = endpoint
            self._endpoint_peers = current
        for peer, previous, endpoint, count in remaps:
            self._emit_diagnostic(
                "zenoh.forwarding.map_peer_remap",
                "zenoh.forwarding.map_peer_remap peer=%s previous=%s current=%s count=%s",
                peer,
                previous,
                endpoint,
                count,
                peerNode=peer,
                previousEndpoint=previous,
                currentEndpoint=endpoint,
                mapPeerRemapCount=count,
            )

    def _queue_link_event(self, event: Any) -> None:
        """Copy one link event off Zenoh's callback thread."""

        link = event.link
        kind = str(event.kind).rsplit(".", 1)[-1].lower()
        zid = str(link.zid)
        src = str(link.src)
        dst = str(link.dst)
        key = (zid, src, dst)
        with self._diagnostic_guard:
            endpoint = src if src in self._endpoint_peers else dst
            peer_node = self._endpoint_peers.get(endpoint)
            peer_map_port = _locator_port(endpoint) if peer_node is not None else None
            other = dst if endpoint == src else src
            local_ephemeral_port = (
                _locator_port(other) if peer_node is not None else None
            )
            if kind == "put":
                self._link_opened_at[key] = (
                    time.monotonic(),
                    peer_node,
                    local_ephemeral_port,
                    peer_map_port,
                )
                delete_fields: dict[str, object] = {}
            else:
                opened = self._link_opened_at.pop(key, None)
                if opened is not None:
                    (
                        opened_at,
                        peer_node,
                        local_ephemeral_port,
                        peer_map_port,
                    ) = opened
                else:
                    opened_at = None
                self._zenoh_link_delete_count += 1
                native_reason = getattr(event, "reason", None)
                delete_fields = {
                    "reason": str(native_reason)
                    if native_reason
                    else "native-cause-unavailable",
                    "ageMs": (
                        max(0, int((time.monotonic() - opened_at) * 1000))
                        if opened_at is not None
                        else None
                    ),
                    "zenohLinkDeleteCount": self._zenoh_link_delete_count,
                    "mapPeerRemapCount": self._map_peer_remap_count,
                }
        self._offer_diagnostic(
            (
                "link",
                {
                    "kind": kind,
                    "zid": zid,
                    "src": src,
                    "dst": dst,
                    "peerNode": peer_node,
                    "localEphemeralPort": local_ephemeral_port,
                    "peerMapPort": peer_map_port,
                    **delete_fields,
                },
            )
        )
