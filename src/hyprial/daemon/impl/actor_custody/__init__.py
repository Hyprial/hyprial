"""Custody for the ``ActorHost`` the P4 batches inject into engine holders.

Decision record (P4 B3):
- A module outside the engine region must not construct the engine.  It takes
  cells, effect lanes and timers from an injected ``ActorHost``; this package is
  the one place that answers "who owns that host?".
- The location is deliberate.  ``daemon/impl/composition/`` already holds seven
  direct files (the structural ceiling) and its package hub reaches
  ``cells.observability.policy``, which the network isolation rule forbids for
  ``daemon.impl.network`` -- so a network module cannot import it even lazily
  from inside a function.  A neutral package carries no such edge.
- Owners are stable per component: "lark", "transport", "network", "transfer",
  "worker".  One private registry per custody instance; an injected host is
  borrowed and never shut down here.
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from hyprial.daemon.impl.composition.actors import ActorHostRegistry
    from hyprial.kernel import ActorHost


class ActorHostCustody:
    """An injected ``ActorHost``, or a privately owned one for standalone use."""

    __slots__ = ("_host", "_lock", "_owner", "_registry")

    def __init__(self, host: ActorHost | None = None, *, owner: str) -> None:
        if not owner.strip():
            raise ValueError("owner must not be blank")
        self._host = host
        self._owner = owner
        self._registry: ActorHostRegistry | None = None
        self._lock = threading.Lock()

    @property
    def owned(self) -> bool:
        """True when this custody created the host and must close it."""

        return self._registry is not None

    @property
    def host(self) -> ActorHost:
        with self._lock:
            if self._host is None:
                from hyprial.daemon.impl.composition.actors import ActorHostRegistry

                self._registry = ActorHostRegistry()
                self._host = self._registry.host_for(self._owner)
            return self._host

    def shutdown(self, timeout: float = 5.0) -> bool:
        """Close the private registry, if any.  A borrowed host is untouched."""

        with self._lock:
            registry = self._registry
        if registry is None:
            return True
        if not registry.shutdown(timeout):
            # The registry retains a host that refused to stop so a caller can
            # retry.  Dropping our reference here would turn that retry into a
            # false success: the second shutdown would return True without
            # reaching the retained host.
            return False
        with self._lock:
            if self._registry is registry:
                self._registry = None
                self._host = None
        return True


__all__ = ["ActorHostCustody"]
