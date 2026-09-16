"""Local-first delivery wrapper for daemon-managed actors.

Managed harnesses share the daemon's durable inbox. Sending their traffic
through the daemon's own Zenoh session cannot produce a remote receipt, so
local actors are delivered directly while every other target keeps using the
configured network transport.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import replace

from .api import DeliveryTransport, InboxMessage, ReceiveResult


class _LocalActorRegistration:
    def __init__(self, owner: LocalFirstDeliveryTransport, actor: str) -> None:
        self._owner = owner
        self._actor = actor
        self._closed = False

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._owner._unregister(self._actor)


class LocalFirstDeliveryTransport:
    """Deliver registered local actors without a same-session Zenoh round trip."""

    def __init__(
        self,
        inner: DeliveryTransport,
        *,
        origin_node: str | None = None,
        local_node_id: str | None = None,
        local_owner: str | None = None,
    ) -> None:
        self._inner = inner
        self._origin_node = origin_node
        self._local_node_id = local_node_id
        self._local_owner = local_owner
        self._lock = threading.Lock()
        self._actors: dict[str, int] = {}
        self._receiver: Callable[[InboxMessage], ReceiveResult] | None = None

    def bind_receiver(self, receiver: Callable[[InboxMessage], ReceiveResult]) -> None:
        with self._lock:
            if self._receiver is not None:
                raise RuntimeError("local delivery receiver is already bound")
            self._receiver = receiver

    def register_actor(self, actor: str) -> _LocalActorRegistration:
        with self._lock:
            self._actors[actor] = self._actors.get(actor, 0) + 1
        return _LocalActorRegistration(self, actor)

    def _unregister(self, actor: str) -> None:
        with self._lock:
            count = self._actors.get(actor, 0)
            if count <= 1:
                self._actors.pop(actor, None)
            else:
                self._actors[actor] = count - 1

    def _is_self_node_uri(self, recipient: str) -> bool:
        """A canonical URI owned by AND machined to this daemon is local.

        A self-identity four-segment URI is a local delivery object whether
        or not a connector currently claims it: registration governs
        wake/liveliness, never deliverability.  A registered-but-not-
        running local identity (an agent-created CLI persona, an offline
        durable consumer) must still receive its rows — the durable local
        inbox accepts them now and the reader collects them later.  The
        E2E-013 regression was this rule missing: replies to such
        identities were treated as remote traffic and parked in the outbox
        forever.

        BOTH the owner and the machine segment must match.  A URI whose
        owner is foreign names an identity of another owner's daemon — one
        that may share this machine — and hijacking its traffic into this
        daemon's inbox is a cross-owner misdelivery (E2E-002's ghost phase
        caught exactly that: `agent:contract:<this-node>:…` rows committed
        locally as `fetched` while belonging to nobody here).
        """

        if self._local_node_id is None or self._local_owner is None:
            return False
        from hyprial.uri import parse_agent_uri

        parsed = parse_agent_uri(recipient)
        return (
            parsed is not None
            and parsed[0] == self._local_owner
            and parsed[1] == self._local_node_id
        )

    def _local_receiver(
        self, recipient: str
    ) -> Callable[[InboxMessage], ReceiveResult] | None:
        with self._lock:
            if recipient in self._actors or self._is_self_node_uri(recipient):
                return self._receiver
            return None

    def is_online(self, recipient: str) -> bool:
        with self._lock:
            if recipient in self._actors:
                return self._receiver is not None
        if self._is_self_node_uri(recipient):
            # Deliverable is online enough for the durable inbox: the row
            # waits for its reader either way.
            return self._receiver is not None
        return self._inner.is_online(recipient)

    def deliver(self, message: InboxMessage) -> bool:
        receiver = self._local_receiver(message.recipient)
        if receiver is None:
            return self._inner.deliver(message)
        # The local path encodes no frame, so the stamp happens here: the
        # persisted row carries the same provenance a wire delivery would.
        if self._origin_node is not None and message.origin_node is None:
            message = replace(message, origin_node=self._origin_node)
        return receiver(message).acknowledged

    def confirm_fetch(self, message: InboxMessage) -> bool:
        return self._inner.confirm_fetch(message)

    def online_mailboxes(self) -> tuple[str, ...]:
        return self._inner.online_mailboxes()

    def transfer_custody(self, mailbox: str, message: InboxMessage) -> bool:
        return self._inner.transfer_custody(mailbox, message)

    def deliver_notice(self, node: str, message: InboxMessage) -> bool:
        return self._inner.deliver_notice(node, message)

    def deliver_progress(self, node: str, message: InboxMessage) -> bool:
        """Progress events share the notice path: local-first, receipt-free."""

        return self._inner.deliver_progress(node, message)
