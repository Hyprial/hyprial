"""Bounded mount metadata, serialized by the composition view's read boundary."""

from collections import OrderedDict
from dataclasses import dataclass
from hashlib import sha256

from hyprial.kernel import LifecycleTarget
from hyprial.daemon.impl.lifecycle.observation import LifecycleObservationOwner


@dataclass(slots=True)
class CellMount:
    entity_token: str  # Private identity fence, never part of CellSnapshot.
    diagnostic_incarnation: str
    target: LifecycleTarget
    owner: LifecycleObservationOwner | None = None
    source: tuple[str, str] | None = None
    binding_revision: int | None = None

    def detach(self) -> None:
        if self.owner is not None:
            self.owner.unbind(self.target)
        self.owner = None
        self.source = None
        self.binding_revision = None

    def attach(self, owner: LifecycleObservationOwner, key: str, epoch: str,
               *, binding_revision: int | None) -> None:
        # An unchanged producer can belong to a new Agent binding. Release only
        # its diagnostic target so old cursors resync and subscribers close,
        # even when no reader observed the interval between those bindings.
        if self.owner is not owner or self.binding_revision != binding_revision:
            self.detach()
        try:
            # Rebind even an unchanged tuple: the source owner's bounded store
            # can have evicted it. bind/current and publish share the owner lock.
            owner.bind(self.target, key, epoch)
        except BaseException:
            owner.unbind(self.target)
            self.detach()
            raise
        self.owner, self.source = owner, (key, epoch)
        self.binding_revision = binding_revision


class CellMounts:
    def __init__(self, host: str, capacity: int) -> None:
        if capacity < 1:
            raise ValueError("cell capacity must be positive")
        self.host, self.capacity = host, capacity
        self.rows: OrderedDict[str, CellMount] = OrderedDict()

    def get(self, actor: str, entity_token: str) -> CellMount:
        row = self.rows.get(actor)
        if row is not None and row.entity_token != entity_token:
            self.remove(actor)
            row = None
        if row is None:
            if len(self.rows) >= self.capacity:
                self.remove(next(iter(self.rows)))
            # A domain-separated digest of the high-entropy entity token is a
            # diagnostic ID, never an authority token. It survives cache eviction
            # and daemon reconstruction; monitor epochs still force resync.
            incarnation = sha256(f"cell-diagnostic-v1\0{actor}\0{entity_token}".encode()).hexdigest()
            row = CellMount(entity_token, incarnation,
                            LifecycleTarget(self.host, "harness.lifecycle", f"{actor}/{incarnation}"))
            self.rows[actor] = row
        self.rows.move_to_end(actor)
        return row

    def remove(self, actor: str) -> None:
        row = self.rows.pop(actor, None)
        if row is not None:
            row.detach()

    def close(self) -> None:
        for actor in tuple(self.rows):
            self.remove(actor)
