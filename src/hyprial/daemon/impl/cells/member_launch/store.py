"""In-memory local member launch facts."""

from __future__ import annotations

from collections.abc import Iterable

from .model import MemberLaunchFact, MemberLaunchKey


class MemberLaunchStore:
    """Single-writer store for process facts indexed by member launch key."""

    def __init__(self, facts: Iterable[MemberLaunchFact] = ()) -> None:
        self._facts = {fact.key: fact for fact in facts}

    def get(self, key: MemberLaunchKey) -> MemberLaunchFact | None:
        return self._facts.get(key)

    def put(self, fact: MemberLaunchFact) -> None:
        self._facts[fact.key] = fact

    def remove(self, key: MemberLaunchKey) -> MemberLaunchFact | None:
        return self._facts.pop(key, None)

    def values(self) -> tuple[MemberLaunchFact, ...]:
        return tuple(self._facts[key] for key in sorted(self._facts))


__all__ = ["MemberLaunchStore"]
