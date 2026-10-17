"""Immutable custody observations; publication is owned by the P2 mailbox."""

from dataclasses import dataclass, replace
import threading
from typing import Literal

from .model import identifier


@dataclass(frozen=True, slots=True)
class CustodyQuery:
    actor: str
    expected_entity_token: str

    def __post_init__(self):
        identifier(self.actor)
        identifier(self.expected_entity_token)


@dataclass(frozen=True, slots=True)
class SourceHold:
    operation_id: str
    generation: int


@dataclass(frozen=True, slots=True)
class SourceCustody:
    actor: str
    entity_token: str
    state: Literal["clear", "held", "unavailable"] = "unavailable"
    operations: tuple[str, ...] = ()
    reason: str = "not-hydrated"
    journal_version: int | None = None
    # Publication sequence, distinct from durable journal revision. A successful
    # reread after an I/O failure is fresh even when the journal did not change.
    version: int = 0
    # Derived from the same validated snapshot; not a new durable authority.
    holds: tuple[SourceHold, ...] = ()
    # True only when every outstanding custody step carries a durable
    # authenticated-override audit row. The state stays "held"/"unavailable":
    # the override permits this entity to start, it never deletes custody and
    # never resumes regular migration progress.
    overridden: bool = False

    @property
    def blocks_start(self) -> bool:
        """Fail closed: unknown/held custody suppresses unless overridden."""

        if self.state == "clear":
            return False
        return not self.overridden


class CustodyProjection:
    def __init__(self, *, capacity: int = 10_000):
        self._capacity = capacity
        self._lock = threading.Lock()
        self._version = 0
        self._values: dict[CustodyQuery, SourceCustody] = {}

    def read(self, query: CustodyQuery) -> SourceCustody:
        with self._lock:
            return self._values.get(query, SourceCustody(query.actor, query.expected_entity_token))

    def publish(self, value: SourceCustody, *, sequence: int) -> SourceCustody:
        query = CustodyQuery(value.actor, value.entity_token)
        with self._lock:
            previous = self._values.get(query)
            if previous is not None and previous.version >= sequence:
                return previous
            self._version = max(self._version, sequence)
            if query not in self._values and len(self._values) >= self._capacity:
                return SourceCustody(query.actor, query.expected_entity_token,
                                     reason="projection-capacity", version=self._version)
            value = replace(value, version=sequence)
            self._values[query] = value
            return value
