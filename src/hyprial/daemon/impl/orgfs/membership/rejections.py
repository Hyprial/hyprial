# Decision record
# - 2026-11-25 (C2, design §3.7): the key is ``(doc_id, sha256(update),
#   policy generation)``; the generation is the digest of the meta state
#   vector's canonical encoding, so any member-table or owner change makes
#   earlier rejections re-evaluable.  One memory per space, 1024 entries,
#   least recently used evicted.  Not thread-safe: it lives on the space's
#   serial executor.
"""Bounded memory of rejected remote updates."""

from __future__ import annotations

import hashlib
from collections import OrderedDict

from hyprial.daemon.impl.orgfs.membership.staged import vector_digest

REJECTION_MEMORY_CAPACITY = 1024


def policy_generation(meta_vector: bytes) -> str:
    """The admission policy generation for a meta state vector."""

    return vector_digest(meta_vector)


class RejectionMemory:
    """LRU set of ``(doc_id, sha256(update), generation)`` keys."""

    def __init__(self, capacity: int = REJECTION_MEMORY_CAPACITY) -> None:
        if capacity < 1:
            raise ValueError("capacity must be positive")
        self._capacity = capacity
        self._keys: OrderedDict[tuple[str, str, str], None] = OrderedDict()

    @staticmethod
    def _key(doc_id: str, update: bytes, generation: str) -> tuple[str, str, str]:
        return doc_id, hashlib.sha256(bytes(update)).hexdigest(), generation

    def seen(self, doc_id: str, update: bytes, generation: str) -> bool:
        """Whether this rejection is remembered; a hit refreshes it."""

        key = self._key(doc_id, update, generation)
        if key not in self._keys:
            return False
        self._keys.move_to_end(key)
        return True

    def remember(self, doc_id: str, update: bytes, generation: str) -> None:
        """Record one rejection, evicting the least recently used entry."""

        key = self._key(doc_id, update, generation)
        self._keys[key] = None
        self._keys.move_to_end(key)
        while len(self._keys) > self._capacity:
            self._keys.popitem(last=False)

    def __len__(self) -> int:
        return len(self._keys)
