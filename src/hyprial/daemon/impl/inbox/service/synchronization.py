"""Mutation-serialization helpers shared by every InboxService mixin.

``_synchronized`` borrows the single ``self._lock`` owned by the
``InboxService`` host (an ``_ActorMutationContext`` when the actor owns
serialization, else one ``threading.RLock``), so all mixins serialize on
exactly the same lock as the pre-split ``InboxService``.
"""
from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from functools import wraps
from typing import TYPE_CHECKING, Any, TypeVar

if TYPE_CHECKING:
    from hyprial.daemon.impl.inbox.service import InboxService

_R = TypeVar("_R")


@dataclass
class _CommittedChanges:
    """Facts from successful commits within one actor-owned operation."""

    changed: bool = False


@contextmanager
def _observe_committed_changes(service: InboxService) -> Iterator[_CommittedChanges]:
    with service._lock:
        previous = service._observed_commits
        changes = _CommittedChanges()
        service._observed_commits = changes
        try:
            yield changes
        finally:
            service._observed_commits = previous
            if previous is not None and changes.changed:
                previous.changed = True


def _record_committed_change(service: InboxService, changed: bool = True) -> None:
    """Called after a transaction commits, including before later I/O can fail."""

    observer = service._observed_commits
    if observer is not None and changed:
        observer.changed = True


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
