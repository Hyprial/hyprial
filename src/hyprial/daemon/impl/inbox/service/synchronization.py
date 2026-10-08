"""Mutation-serialization helpers shared by every InboxService mixin.

``_synchronized`` borrows the single ``self._lock`` owned by the
``InboxService`` host (an ``_ActorMutationContext`` when the actor owns
serialization, else one ``threading.RLock``), so all mixins serialize on
exactly the same lock as the pre-split ``InboxService``.
"""
from __future__ import annotations

from collections.abc import Callable
from functools import wraps
from typing import TYPE_CHECKING, Any, TypeVar

if TYPE_CHECKING:
    from hyprial.daemon.impl.inbox.service import InboxService

_R = TypeVar("_R")


class _ActorMutationContext:
    """No-op guard used when an actor already owns mutation serialization.

    SQLite transaction blocks remain unchanged.  This context only replaces
    the legacy service-level business lock; it is deliberately private so a
    caller cannot opt out of serialization without going through the actor
    coordinator.
    """

    def __enter__(self) -> None:
        return None

    def __exit__(self, *_: object) -> None:
        return None


def _synchronized(method: Callable[..., _R]) -> Callable[..., _R]:  # noqa: UP047
    @wraps(method)
    def locked(self: InboxService, *args: Any, **kwargs: Any) -> _R:
        with self._lock:
            return method(self, *args, **kwargs)

    return locked
