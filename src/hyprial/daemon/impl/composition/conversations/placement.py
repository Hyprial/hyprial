"""Owner-home placement of conversation records (plan §2.0 "S6 placement", Q3).

Decision notes (phase A, S6):

- The record segments share the owner-home space S3 maps scope ``owner``
  to; ``CellSpaces.private_space(authority)`` is the one rule for finding or
  creating it (lowest space id wins, created only for the local author).
  ``authority()`` answers the local author with the same rule the cell
  runtime's author uses (``composition.cells.local_author``), so record ids
  stay identical while the recorder never composes the cell runtime.
- The OrgFS facade is the app's (``app._require_orgfs_runtime().facade``),
  resolved on each use so an OrgFS that is not up yet only delays records;
  tests inject an isolated facade.
- The runtime's own ``CellSpaces`` and this one share the facade's
  home-creation lock (slice HS1), so one daemon creates at most one home.
  Homes made by two devices, or by a daemon before HS1, still converge
  through S3's union-read and lowest-id-write rules, and every grant ref
  names its space, so either copy resolves.
- ``close`` (the ``conversation-recorder`` shutdown step) makes ``fs``
  raise ``RecordUnavailable`` without touching the app, so a late
  hydration or a recorder pass that outlives its join can never compose a
  new OrgFS runtime after shutdown cleared it; reads answer None and
  appends are delayed.

Decision notes (plan slice RM1):

- ``read_fs`` and ``read_spaces`` serve the record read model of
  ``conversations.*``.  A request must never compose OrgFS or create a
  space: ``read_fs`` answers the injected facade or the facade of an OrgFS
  runtime the daemon already composed (startup composes it), else raises
  ``RecordUnavailable``; ``read_spaces`` is the union of the authority's
  owner homes, never ``private_space``.

Decision notes (slice GC1):

- ``OwnerHomeGroupPlacement`` is the one ``GroupPlacement`` of the group
  log: a group of one owner lives in that owner's home, read as the union
  of homes and written to the lowest id, by the same rules as above.
"""

from __future__ import annotations

import threading
from typing import Any

from hyprial.daemon.impl.composition.cells import CellSpaces, local_author
from hyprial.daemon.impl.conversations import RecordUnavailable


class OwnerHomePlacement:
    """The OrgFS facade and owner-home space conversation records go to."""

    def __init__(self, app: Any, *, fs: Any = None) -> None:
        self._app = app
        self._fs = fs
        self._lock = threading.Lock()
        self._spaces: tuple[Any, CellSpaces] | None = None
        self._closed = False

    @property
    def closed(self) -> bool:
        return self._closed

    def close(self) -> None:
        """Refuse every later OrgFS use; idempotent."""

        self._closed = True

    def fs(self) -> Any:
        if self._closed:
            raise RecordUnavailable("closed")
        if self._fs is not None:
            return self._fs
        return self._app._require_orgfs_runtime().facade

    def _cell_spaces(self, fs: Any) -> CellSpaces:
        with self._lock:
            if self._spaces is None or self._spaces[0] is not fs:
                self._spaces = (fs, CellSpaces(fs))
            return self._spaces[1]

    def read_fs(self) -> Any:
        """The OrgFS facade for a read, without composing OrgFS."""

        if self._closed:
            raise RecordUnavailable("closed")
        if self._fs is not None:
            return self._fs
        runtime = getattr(self._app, "_orgfs_runtime", None)
        if runtime is None:
            raise RecordUnavailable("orgfs-not-composed")
        return runtime.facade

    def read_spaces(self, fs: Any, authority: str) -> tuple[str, ...]:
        """Every owner-home space id of *authority* a reader takes the union of."""

        return tuple(ref.space_id for ref in self._cell_spaces(fs).home_spaces(authority))

    def write_space(self, authority: str) -> str | None:
        """The owner-home space id records of *authority* are written to."""

        fs = self.fs()
        home = self._cell_spaces(fs).private_space(authority)
        return None if home is None else home.space_id

    def authority(self) -> str:
        """The local author records are filed under (the app's owner).

        Answered from the facade this placement holds — in production the
        app's OrgFS runtime facade, the same one the cell runtime authors
        as, so the two principals always agree.
        """

        return local_author(self._app, self.fs())


class OwnerHomeGroupPlacement:
    """The single-owner ``GroupPlacement``: *authority*'s owner-home spaces."""

    def __init__(self, homes: OwnerHomePlacement, authority: str) -> None:
        self._homes = homes
        self._authority = authority

    def fs(self) -> Any:
        return self._homes.fs()

    def write_space(self) -> str | None:
        return self._homes.write_space(self._authority)

    def read_fs(self) -> Any:
        return self._homes.read_fs()

    def read_spaces(self, fs: Any) -> tuple[str, ...]:
        return self._homes.read_spaces(fs, self._authority)


__all__ = ["OwnerHomeGroupPlacement", "OwnerHomePlacement"]
