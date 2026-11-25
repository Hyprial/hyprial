# Decision record (2026-11-24, owner request on PR #3142):
# - Three production incidents came from read paths that each flattened the
#   whole meta document on every call: a 24.7k-document space took 592 s to
#   open because ``retired()`` flattened every root once per document file.
# - The live meta document is only ever changed by ``Doc.apply_update``; local
#   commits mutate a separate working document first.  ``MetaDoc`` counts those
#   calls and the count is the primary invalidation token.
# - A pycrdt document observer was tried first and rejected: its Subscription
#   object is bound to the thread that created it and panics when another
#   thread drops it, and a store is used from several threads.
# - The state vector alone is not a valid token: a deletion leaves it
#   unchanged.  It is kept as a second token so that an insert or overwrite
#   made without ``apply_update`` is still seen.  ``Doc.get(name, type=Map)``
#   can add a root name without any update, so the root names are a third.
# - A document that is not a ``MetaDoc`` is never cached.
# - The read model is a cache of the document, never an authority: it persists
#   nothing and can be dropped at any time.
# - Cached values are shared between callers and must be treated as read-only.
"""Bounded reads of one space's live meta document."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol, TypeVar, runtime_checkable

from pycrdt import Doc

from hyprial.daemon.impl.orgfs.storage.store.vocabulary import (
    RetirementRecord,
    _doc_root,
    _retirement_record,
)


T = TypeVar("T")


@runtime_checkable
class SpaceReader(Protocol):
    """Read surface of a space whose cost does not follow the journal.

    Every method answers from the meta read model: after the first call that
    follows a meta change, a call costs one dictionary lookup plus the size of
    its own answer.  No method decodes a journal envelope.
    """

    def active_tree_doc_id(self) -> str | None: ...

    def retired(self, doc_id: str) -> RetirementRecord | None: ...

    def retirement_records(self) -> tuple[RetirementRecord, ...]: ...

    def purge_listed(self, sha: str) -> bool: ...

    def purge_projection(self) -> frozenset[str]: ...

    def purge_list_entries(self) -> dict[str, str]: ...

    def snapshot_point(self, doc_id: str) -> bytes | None: ...

    def writer_attributions(self) -> dict[str, tuple[str, ...]]: ...


class MetaDoc(Doc):
    """The live meta document; ``applied`` moves around every applied update."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.applied = 0
        super().__init__(*args, **kwargs)

    def apply_update(self, update: bytes) -> None:
        self.applied += 1
        try:
            super().apply_update(update)
        finally:
            self.applied += 1


class MetaReadModel:
    """Plain views of the live meta roots, rebuilt only after meta changes.

    ``flattens`` counts root rebuilds so cost tests can assert on work done
    instead of on wall time.
    """

    def __init__(self) -> None:
        self._doc: Doc | None = None
        self._token: tuple[Any, ...] | None = None
        self._roots: dict[str, Any] = {}
        self._derived: dict[str, Any] = {}
        self.flattens = 0

    def close(self) -> None:
        """Drop the bound document and every cached view."""

        self._doc = None
        self._token = None
        self._roots = {}
        self._derived = {}

    def root(self, doc: Doc | None, name: str) -> Any:
        """Return root ``name`` as ``_doc_roots(doc).get(name)`` would.

        One token check while the meta is unchanged; one flatten of that root
        otherwise.
        """

        if doc is None:
            return None
        token = self._bind(doc)
        if token is not None and name in self._roots:
            return self._roots[name]
        value = _doc_root(doc, name)
        self.flattens += 1
        if token is not None and token == self._token_of(doc):
            self._roots[name] = value
        return value

    def derived(self, doc: Doc | None, key: str, build: Callable[[], T]) -> T:
        """Return ``build()`` memoized until the meta changes."""

        if doc is None:
            return build()
        token = self._bind(doc)
        if token is not None and key in self._derived:
            return self._derived[key]
        value = build()
        if token is not None and token == self._token_of(doc):
            self._derived[key] = value
        return value

    def retirements(self, doc: Doc | None) -> dict[str, RetirementRecord]:
        """Return every well-formed retirement record keyed by retired doc id."""

        def build() -> dict[str, RetirementRecord]:
            raw = self.root(doc, "retirements")
            records: dict[str, RetirementRecord] = {}
            for doc_id in sorted(raw if isinstance(raw, dict) else ()):
                record = _retirement_record(str(doc_id), raw[doc_id])
                if record is not None:
                    records[str(doc_id)] = record
            return records

        return self.derived(doc, "retirements", build)

    @staticmethod
    def _token_of(doc: Doc) -> tuple[Any, ...] | None:
        if not isinstance(doc, MetaDoc):
            return None
        names = tuple(sorted(str(key) for key in doc.keys()))
        return (doc.applied, doc.get_state(), names)

    def _bind(self, doc: Doc) -> tuple[Any, ...] | None:
        token = self._token_of(doc)
        if doc is not self._doc or token != self._token:
            self._doc = doc
            self._token = token
            self._roots = {}
            self._derived = {}
        return token
