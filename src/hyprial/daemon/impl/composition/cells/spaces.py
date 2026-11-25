"""Map cell scopes to OrgFS spaces: org directory spaces and owner homes.

Decision notes (phase A, S3):

- Scope ``org`` with a name maps to that org's one directory space (by its
  ``group-*`` name; an ambiguous name maps to none).  Without a name it maps
  to every live org directory space, excluding orgs this node has left.
- Scope ``owner`` maps to the writer's owner-home space (plan Q3): a
  space owned by the writer whose metadata carries
  ``kind = hyprial-owner-home``.  The tag cannot carry ``owner`` (OrgFS core
  metadata is immutable); the owner is the space's own ``owner``.  When two
  devices of one owner each created a home, readers take the union and
  writers use the lowest space id, so both converge on one writer target.
- ``private_space`` creates the home only for the local OrgFS author, and
  only when a write needs it; reading never creates a space.
- Only spaces named ``hyprial-home`` are probed for the tag, so org
  directory spaces never cost a metadata read.  Positive home tags are
  remembered per space id; an untagged ``hyprial-home`` is re-checked each
  call because the tag is written just after creation.
- A crash between ``create_space`` and the tag write leaves an untagged
  ``hyprial-home`` owned by the author; ``private_space`` tags and adopts
  it (lowest space id first) instead of creating another.

Decision notes (cell model OC1a, contract OC0 §1.4.7):

- ``write_spaces(scope, writer)``: the second argument is the local OrgFS
  writer ``user:<username>``, never a cell authority (``user:u.<sub>``).
  OrgFS space owners are OrgFS principals, so an authority finds no home
  and creates none (it is not the facade's author): the cell stays dirty.
  The ``org`` scope ignores the argument.
- ``home_spaces(owner)`` and ``private_space(owner)`` keep their names and
  behaviour: ``owner`` is an OrgFS space owner principal (the local writer
  or another device's OrgFS principal), not a cell authority.  The
  conversation placement passes the local OrgFS author positionally.

Decision notes (slice HS1):

- One daemon has several ``CellSpaces`` over one facade (the cell runtime's
  and the conversation placement's), each racing to create the first home.
  Creation is serialised per OrgFS facade: a lock kept in a weak map keyed
  by the facade object, so every instance over that facade shares it and
  applications with their own facades (several coexist in one test
  process) never contend.  The facade is the narrowest object both paths
  already share; the app is not, since tests hand a facade straight in.
- The lock is taken only after the unlocked lookup found no home and only
  for the local author.  Inside it the home lookup runs again, then
  adoption or creation, then the tag, so a second thread finds the tagged
  home the first one made.
- The wait is bounded by ``HOME_CREATE_WAIT_S``; on timeout ``private_space``
  raises ``TimeoutError`` instead of creating unlocked.  Both writers
  already treat an exception as "retry later" (the runtime keeps the cell
  dirty, the record segment is delayed).
- Homes from two devices of one owner are not prevented: each device has its
  own facade.  The union read and lowest-id write above still converge them.
"""

from __future__ import annotations

import threading
import weakref
from collections.abc import Callable, Iterable
from typing import Any

from hyprial.daemon.impl.cells.model import CellScope
from hyprial.daemon.impl.cells.ports import SpaceRef
from hyprial.daemon.impl.orgfs.api import OrgFsError
from hyprial.identity import org_from_space_name

OWNER_HOME_KIND = "hyprial-owner-home"
OWNER_HOME_NAME = "hyprial-home"
HOME_CREATE_WAIT_S = 10.0

_HOME_LOCKS: weakref.WeakKeyDictionary[Any, threading.Lock] = weakref.WeakKeyDictionary()
_HOME_LOCKS_GUARD = threading.Lock()


def _home_lock(fs: Any) -> threading.Lock:
    """The one home-creation lock of the OrgFS facade *fs*."""

    with _HOME_LOCKS_GUARD:
        lock = _HOME_LOCKS.get(fs)
        if lock is None:
            lock = threading.Lock()
            _HOME_LOCKS[fs] = lock
        return lock


class CellSpaces:
    """The ``CellSpacesPort`` over one OrgFS facade."""

    def __init__(
        self,
        fs: Any,
        *,
        left_orgs: Callable[[], Iterable[str]] = lambda: (),
    ) -> None:
        self._fs = fs
        self._left_orgs = left_orgs
        self._homes: set[str] = set()

    def read_spaces(self) -> tuple[SpaceRef, ...]:
        refs = list(self.org_spaces().values())
        refs.extend(self.home_spaces())
        return tuple(sorted(set(refs), key=lambda ref: ref.space_id))

    def write_spaces(self, scope: CellScope, writer: str) -> tuple[SpaceRef, ...]:
        """Where a cell of *scope* is written by the local OrgFS *writer*."""

        if scope.kind == "owner":
            home = self.private_space(writer)
            return () if home is None else (home,)
        orgs = self.org_spaces()
        if scope.org is None:
            return tuple(orgs[org] for org in sorted(orgs))
        chosen = orgs.get(scope.org)
        return () if chosen is None else (chosen,)

    def org_spaces(self) -> dict[str, SpaceRef]:
        """Live org name -> its one directory space."""

        left = set(self._left_orgs())
        seen: dict[str, list[Any]] = {}
        for info in self._fs.spaces():
            org = org_from_space_name(info.name)
            if org is None or org in left:
                continue
            seen.setdefault(org, []).append(info)
        return {
            org: SpaceRef(infos[0].space_id, infos[0].owner, "org", org)
            for org, infos in seen.items()
            if len(infos) == 1
        }

    def home_spaces(self, owner: str | None = None) -> tuple[SpaceRef, ...]:
        """Every tagged owner-home space (of *owner* when given)."""

        homes: list[SpaceRef] = []
        for info in self._fs.spaces():
            if owner is not None and info.owner != owner:
                continue
            if info.name != OWNER_HOME_NAME:
                continue
            if info.space_id not in self._homes:
                try:
                    meta = self._fs.space_meta(info.space_id)
                except OrgFsError:
                    continue
                if meta.get("kind") != OWNER_HOME_KIND:
                    continue
                self._homes.add(info.space_id)
            homes.append(SpaceRef(info.space_id, info.owner, "owner-home"))
        return tuple(sorted(homes, key=lambda ref: ref.space_id))

    def private_space(self, owner: str) -> SpaceRef | None:
        """The owner-home write target; created for the local author on demand."""

        homes = self.home_spaces(owner)
        if homes:
            return homes[0]
        if owner != getattr(self._fs, "author", None):
            return None
        lock = _home_lock(self._fs)
        if not lock.acquire(timeout=HOME_CREATE_WAIT_S):
            raise TimeoutError("owner home creation is busy")
        try:
            return self._find_or_create_home(owner)
        finally:
            lock.release()

    def _find_or_create_home(self, owner: str) -> SpaceRef:
        homes = self.home_spaces(owner)
        if homes:
            return homes[0]
        orphans = sorted(
            (info for info in self._fs.spaces()
             if info.owner == owner and info.name == OWNER_HOME_NAME),
            key=lambda info: info.space_id,
        )
        info = orphans[0] if orphans else self._fs.create_space(OWNER_HOME_NAME)
        self._fs.update_space_meta(info.space_id, {"kind": OWNER_HOME_KIND})
        self._homes.add(info.space_id)
        return SpaceRef(info.space_id, info.owner, "owner-home")


__all__ = ["CellSpaces", "HOME_CREATE_WAIT_S", "OWNER_HOME_KIND", "OWNER_HOME_NAME"]
