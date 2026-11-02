"""Read-only cell port. A missing cell never asserts process death."""

from typing import Protocol, runtime_checkable

from hyprial.kernel import ObservationBatch, ObservationSubscription
from .model import CellSnapshot


@runtime_checkable
class CellObservationPort(Protocol):
    def snapshot(self, actor: str) -> CellSnapshot | None: ...

    def observe(self, actor: str, *, after_revision: int | None = None,
                epoch: str | None = None, limit: int = 128, wait_ms: int = 0,
                observer_id: str | None = None) -> ObservationBatch | None: ...

    def open(self, actor: str) -> ObservationSubscription | None: ...

    def cancel(self, observer_id: str) -> None: ...
