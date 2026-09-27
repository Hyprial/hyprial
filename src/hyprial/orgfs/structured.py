"""Container-level handles for structured orgfs documents.

The handle deliberately knows nothing about the facade, journal, membership,
or mesh.  Its injected commit function is the only write path, so integration
can apply the same attribution, admission, durability, and broadcast rules as
ordinary file writes.
"""

from __future__ import annotations

import base64
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
import json
from typing import Any, TypeAlias

from pycrdt import Array, Doc, Map, Text

from .api import NodeInfo, OrgFsError


_STRUCTURED_ROOT = "root"
_DELETED_ROOTS = "__orgfs_structured_deleted__"
_INTERNAL_ROOTS = frozenset({_STRUCTURED_ROOT, _DELETED_ROOTS, "__orgfs__"})
_MISSING = object()

DocMutator: TypeAlias = Callable[[Doc], None]
Commit: TypeAlias = Callable[[DocMutator], NodeInfo]
Resolver: TypeAlias = Callable[[], Any]
VersionSource: TypeAlias = Callable[[], str]


@dataclass(slots=True)
class _MutationScope:
    active: bool = True

    def require(self) -> None:
        if not self.active:
            raise OrgFsError(
                "invalid-argument",
                {"message": "document writes require OrgDoc.transact"},
            )


def _invalid(message: str, **details: object) -> OrgFsError:
    return OrgFsError("invalid-argument", {"message": message, **details})


def _converted(value: object) -> object:
    """Convert JSON-shaped Python containers to pycrdt shared containers."""

    if isinstance(value, (Map, Array, Text)):
        if value.is_integrated:
            raise _invalid("cannot insert a container owned by another document")
        return value
    if isinstance(value, Mapping):
        converted: dict[str, object] = {}
        for key, child in value.items():
            if not isinstance(key, str):
                raise _invalid("map keys must be strings")
            converted[key] = _converted(child)
        return Map(converted)
    if isinstance(value, Sequence) and not isinstance(
        value, (str, bytes, bytearray, memoryview)
    ):
        return Array([_converted(child) for child in value])
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    raise _invalid("value is not JSON-shaped", valueType=type(value).__name__)


def _plain(value: object) -> object:
    if isinstance(value, (Map, Array, Text)):
        return value.to_py()
    return value


def _wrapped(value: object, resolver: Resolver, scope: _MutationScope | None) -> object:
    if isinstance(value, Map):
        return DocMap(resolver, scope)
    if isinstance(value, Array):
        return DocList(resolver, scope)
    if isinstance(value, Text):
        return DocText(resolver, scope)
    return value


def _resolve_map(resolver: Resolver) -> Map[Any]:
    value = resolver()
    if not isinstance(value, Map):
        raise _invalid("container is no longer a map")
    return value


def _resolve_list(resolver: Resolver) -> Array[Any]:
    value = resolver()
    if not isinstance(value, Array):
        raise _invalid("container is no longer a list")
    return value


def _resolve_text(resolver: Resolver) -> Text:
    value = resolver()
    if not isinstance(value, Text):
        raise _invalid("container is no longer text")
    return value


class DocMap:
    """A zero-cache cursor over a nested pycrdt map."""

    def __init__(self, resolver: Resolver, scope: _MutationScope | None = None) -> None:
        self._resolver = resolver
        self._scope = scope

    def _write(self) -> Map[Any]:
        if self._scope is None:
            raise _invalid("document writes require OrgDoc.transact")
        self._scope.require()
        return _resolve_map(self._resolver)

    def get(self, key: str, default: object | None = None) -> object:
        value = _resolve_map(self._resolver).get(key, _MISSING)
        if value is _MISSING:
            return default
        return _wrapped(
            value,
            lambda: _resolve_map(self._resolver)[key],
            self._scope,
        )

    def keys(self) -> tuple[str, ...]:
        return tuple(sorted(str(key) for key in _resolve_map(self._resolver).keys()))

    def set(self, key: str, value: object) -> None:
        if not isinstance(key, str):
            raise _invalid("map keys must be strings")
        self._write()[key] = _converted(value)

    def delete(self, key: str) -> None:
        mapping = self._write()
        if key not in mapping:
            raise _invalid("map key does not exist", key=key)
        del mapping[key]

    def _ensure(self, key: str, expected: type[Any]) -> object:
        mapping = self._write()
        value = mapping.get(key, _MISSING)
        if value is _MISSING:
            value = expected()
            mapping[key] = value
            value = mapping[key]
        if not isinstance(value, expected):
            raise _invalid(
                "container type mismatch",
                key=key,
                expected=expected.__name__,
                actual=type(value).__name__,
            )

        def resolver() -> object:
            return _resolve_map(self._resolver)[key]

        return _wrapped(value, resolver, self._scope)

    def ensure_map(self, key: str) -> DocMap:
        return self._ensure(key, Map)  # type: ignore[return-value]

    def ensure_list(self, key: str) -> DocList:
        return self._ensure(key, Array)  # type: ignore[return-value]

    def ensure_text(self, key: str) -> DocText:
        return self._ensure(key, Text)  # type: ignore[return-value]


class DocList:
    """A zero-cache cursor over a pycrdt array."""

    def __init__(self, resolver: Resolver, scope: _MutationScope | None = None) -> None:
        self._resolver = resolver
        self._scope = scope

    def _write(self) -> Array[Any]:
        if self._scope is None:
            raise _invalid("document writes require OrgDoc.transact")
        self._scope.require()
        return _resolve_list(self._resolver)

    def get(self, index: int) -> object:
        try:
            value = _resolve_list(self._resolver)[index]
        except (IndexError, RuntimeError) as exc:
            raise _invalid("list index is out of range", index=index) from exc
        return _wrapped(
            value,
            lambda: _resolve_list(self._resolver)[index],
            self._scope,
        )

    def len(self) -> int:
        return len(_resolve_list(self._resolver))

    def __len__(self) -> int:
        return self.len()

    def insert(self, index: int, value: object) -> None:
        array = self._write()
        if index < 0 or index > len(array):
            raise _invalid("list index is out of range", index=index)
        array.insert(index, _converted(value))

    def delete(self, index: int) -> None:
        array = self._write()
        if index < 0 or index >= len(array):
            raise _invalid("list index is out of range", index=index)
        del array[index]


class DocText:
    """A zero-cache cursor over pycrdt text."""

    def __init__(self, resolver: Resolver, scope: _MutationScope | None = None) -> None:
        self._resolver = resolver
        self._scope = scope

    def _write(self) -> Text:
        if self._scope is None:
            raise _invalid("document writes require OrgDoc.transact")
        self._scope.require()
        return _resolve_text(self._resolver)

    def str(self) -> str:
        return _resolve_text(self._resolver).to_py() or ""

    def insert(self, index: int, value: str) -> None:
        if not isinstance(value, str):
            raise _invalid("text insert value must be a string")
        text = self._write()
        if index < 0 or index > len(text):
            raise _invalid("text index is out of range", index=index)
        text.insert(index, value)

    def delete(self, index: int, length: int) -> None:
        text = self._write()
        if index < 0 or length < 0 or index + length > len(text):
            raise _invalid("text range is out of bounds", index=index, length=length)
        if length:
            del text[index : index + length]


class _RootMap:
    """Logical root map, with P1's top-level ``text`` container kept visible."""

    def __init__(self, document: Doc, scope: _MutationScope | None = None) -> None:
        self._document = document
        self._scope = scope

    def _require(self) -> None:
        if self._scope is None:
            raise _invalid("document writes require OrgDoc.transact")
        self._scope.require()

    def _direct(self, key: str) -> object:
        deleted = self._deleted_roots(create=False)
        if (
            key in _INTERNAL_ROOTS
            or key not in self._document
            or (deleted is not None and bool(deleted.get(key, False)))
        ):
            return _MISSING
        if key == "text":
            return self._document.get(key, type=Text)
        return self._document[key]

    def _storage(self, *, create: bool) -> Map[Any] | None:
        if _STRUCTURED_ROOT not in self._document:
            if not create:
                return None
            self._document[_STRUCTURED_ROOT] = Map()
        value = self._document.get(_STRUCTURED_ROOT, type=Map)
        if not isinstance(value, Map):
            raise _invalid("structured root has the wrong container type")
        return value

    def _deleted_roots(self, *, create: bool) -> Map[Any] | None:
        if _DELETED_ROOTS not in self._document:
            if not create:
                return None
            self._document[_DELETED_ROOTS] = Map()
        value = self._document.get(_DELETED_ROOTS, type=Map)
        if not isinstance(value, Map):
            raise _invalid("deleted-root registry has the wrong container type")
        return value

    def _restore_direct(self, key: str) -> object:
        if key not in self._document:
            return _MISSING
        deleted = self._deleted_roots(create=False)
        if deleted is not None and key in deleted:
            del deleted[key]
        if key == "text":
            return self._document.get(key, type=Text)
        return self._document[key]

    def _value(self, key: str) -> object:
        direct = self._direct(key)
        if direct is not _MISSING:
            return direct
        storage = self._storage(create=False)
        return _MISSING if storage is None else storage.get(key, _MISSING)

    def _resolver(self, key: str) -> Resolver:
        def resolve() -> object:
            value = self._value(key)
            if value is _MISSING:
                raise _invalid("map key no longer exists", key=key)
            return value

        return resolve

    def get(self, key: str, default: object | None = None) -> object:
        value = self._value(key)
        if value is _MISSING:
            return default
        return _wrapped(value, self._resolver(key), self._scope)

    def keys(self) -> tuple[str, ...]:
        keys = {str(key) for key in self._document.keys() if key not in _INTERNAL_ROOTS}
        deleted = self._deleted_roots(create=False)
        if deleted is not None:
            keys.difference_update(str(key) for key in deleted.keys())
        storage = self._storage(create=False)
        if storage is not None:
            keys.update(str(key) for key in storage.keys())
        return tuple(sorted(keys))

    def set(self, key: str, value: object) -> None:
        self._require()
        if not isinstance(key, str):
            raise _invalid("map keys must be strings")
        direct = self._direct(key)
        if direct is _MISSING and key in self._document:
            direct = self._restore_direct(key)
        if direct is not _MISSING:
            self._replace_direct(key, direct, value)
            return
        storage = self._storage(create=True)
        assert storage is not None
        storage[key] = _converted(value)

    def _replace_direct(self, key: str, current: object, value: object) -> None:
        if isinstance(current, Text) and isinstance(value, str):
            current.clear()
            if value:
                current.insert(0, value)
            return
        if isinstance(current, Map) and isinstance(value, Mapping):
            current.clear()
            for child_key, child in value.items():
                if not isinstance(child_key, str):
                    raise _invalid("map keys must be strings")
                current[child_key] = _converted(child)
            return
        if (
            isinstance(current, Array)
            and isinstance(value, Sequence)
            and not isinstance(value, (str, bytes, bytearray, memoryview))
        ):
            current.clear()
            current.extend([_converted(child) for child in value])
            return
        raise _invalid("cannot replace a document root with a different type", key=key)

    def delete(self, key: str) -> None:
        self._require()
        direct = self._direct(key)
        if isinstance(direct, (Map, Array, Text)):
            direct.clear()
            deleted = self._deleted_roots(create=True)
            assert deleted is not None
            deleted[key] = True
            return
        storage = self._storage(create=False)
        if storage is None or key not in storage:
            raise _invalid("map key does not exist", key=key)
        del storage[key]

    def _ensure(self, key: str, expected: type[Any]) -> object:
        self._require()
        value = self._value(key)
        if value is _MISSING and key in self._document:
            value = self._restore_direct(key)
        if value is _MISSING:
            storage = self._storage(create=True)
            assert storage is not None
            storage[key] = expected()
            value = storage[key]
        if not isinstance(value, expected):
            raise _invalid(
                "container type mismatch",
                key=key,
                expected=expected.__name__,
                actual=type(value).__name__,
            )
        return _wrapped(value, self._resolver(key), self._scope)

    def ensure_map(self, key: str) -> DocMap:
        return self._ensure(key, Map)  # type: ignore[return-value]

    def ensure_list(self, key: str) -> DocList:
        return self._ensure(key, Array)  # type: ignore[return-value]

    def ensure_text(self, key: str) -> DocText:
        return self._ensure(key, Text)  # type: ignore[return-value]


class StructuredOrgDoc:
    """Public structured document handle backed by an injected commit seam."""

    def __init__(
        self,
        document: Any,
        doc_id: str,
        commit: Commit,
        *,
        version: VersionSource | None = None,
    ) -> None:
        self._document = document
        self._doc = (
            document.doc
            if isinstance(getattr(document, "doc", None), Doc)
            else document
        )
        if not isinstance(self._doc, Doc):
            raise TypeError("StructuredOrgDoc requires a pycrdt Doc")
        self._doc_id = str(doc_id)
        self._commit = commit
        self._version = version

    def doc_id(self) -> str:
        return self._doc_id

    def snapshot_json(self) -> str:
        value: dict[str, object] = {}
        deleted = (
            self._doc.get(_DELETED_ROOTS, type=Map)
            if _DELETED_ROOTS in self._doc
            else None
        )
        deleted_keys = (
            {str(key) for key in deleted.keys()} if isinstance(deleted, Map) else set()
        )
        storage = (
            self._doc.get(_STRUCTURED_ROOT, type=Map)
            if _STRUCTURED_ROOT in self._doc
            else None
        )
        if isinstance(storage, Map):
            value.update(storage.to_py() or {})
        for key in self._doc.keys():
            if key in _INTERNAL_ROOTS or str(key) in deleted_keys:
                continue
            root_value = (
                self._doc.get(str(key), type=Text)
                if key == "text"
                else self._doc[str(key)]
            )
            value[str(key)] = _plain(root_value)
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    def version(self) -> str:
        if self._version is not None:
            return self._version()
        return base64.b64encode(self._doc.get_state()).decode("ascii")

    def root(self) -> _RootMap:
        return _RootMap(self._doc)

    def transact(self, mutate: Callable[[_RootMap], None]) -> NodeInfo:
        if not callable(mutate):
            raise _invalid("transact requires a callable")

        def operation(document: Doc) -> None:
            scope = _MutationScope()
            try:
                mutate(_RootMap(document, scope))
            finally:
                scope.active = False

        return self._commit(operation)


__all__ = ["DocList", "DocMap", "DocText", "StructuredOrgDoc"]
