"""The frame-① network delivery transport as the daemon composes it.

Decisions (cell model phase B, wire option B):

* Every network transport the daemon builds carries the sender's payload
  envelope, so the composition asks this factory instead of pairing
  ``ZenohDeliveryTransport`` and ``EnvelopeStampingTransport`` itself.
* The factory lives at the inbox package root, beside the in-memory
  transport, because it joins the link layer and the envelope layer;
  ``inbox.envelope`` stays free of the link and service imports.
"""

from __future__ import annotations

from hyprial.daemon.impl.inbox.envelope.transport import EnvelopeStampingTransport
from hyprial.daemon.impl.inbox.links.zenoh_link import ZenohDeliveryTransport
from hyprial.daemon.impl.transport import KeySpace, PresenceView, TransportSession


def stamped_network_transport(
    session: TransportSession,
    presence: PresenceView,
    *,
    keys: KeySpace | None = None,
    receipt_timeout: float = 1.0,
    origin_node: str | None = None,
) -> EnvelopeStampingTransport:
    """A Zenoh delivery transport whose frames carry the payload envelope."""

    return EnvelopeStampingTransport(
        ZenohDeliveryTransport(
            session,
            presence,
            keys=keys,
            receipt_timeout=receipt_timeout,
            origin_node=origin_node,
        )
    )


__all__ = ["stamped_network_transport"]
