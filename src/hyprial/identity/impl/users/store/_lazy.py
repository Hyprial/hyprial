from __future__ import annotations
from ._store import UserStore

from collections.abc import Callable
from pathlib import Path
import threading

from ._base import (
    Ambiguous,
    ResolvedUser,
)


class LazyUserStore:
    """The read side a long-running process holds: opened on first use.

    A Lark worker lives for days, while ``hyprial user add`` creates
    ``users.sqlite3`` whenever an operator (or the coordinator) first runs it.
    Opening only at worker start would make the first binding on a machine
    invisible until an adapter restart; opening eagerly would create the file
    on machines that never use it.  So: look for the file on each lookup
    until it exists, open it once, then keep it.

    A store that exists but cannot be opened must not break inbound
    delivery -- naming the sender is bookkeeping beside it, exactly like the
    identities lookup it precedes.  The failure is reported once through
    ``on_open_failure`` and the lookup answers ``None``, so the caller falls
    back to #818's identities-only resolution.
    """

    def __init__(
        self,
        path: Path,
        *,
        on_open_failure: Callable[[Exception], None] | None = None,
    ) -> None:
        self._path = path
        self._on_open_failure = on_open_failure
        self._store: UserStore | None = None
        self._failure_reported = False
        self._lock = threading.Lock()

    def _opened(self) -> UserStore | None:
        with self._lock:
            if self._store is not None:
                return self._store
            if not self._path.is_file():
                return None
            try:
                self._store = UserStore(self._path)
            except (NameError, ImportError):
                raise
            except Exception as error:  # noqa: BLE001 - never break inbound
                if not self._failure_reported and self._on_open_failure is not None:
                    self._failure_reported = True
                    self._on_open_failure(error)
                return None
            return self._store

    def resolve_account(
        self, adapter: str, open_id: str, union_id: str | None = None
    ) -> ResolvedUser | Ambiguous | None:
        store = self._opened()
        if store is None:
            return None
        return store.resolve_account(adapter, open_id, union_id)

    def close(self) -> None:
        with self._lock:
            if self._store is not None:
                self._store.close()
                self._store = None
