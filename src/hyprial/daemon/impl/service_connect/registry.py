"""Trusted public service catalog loading with explicit fallback provenance."""

from __future__ import annotations

import time
from pathlib import PurePosixPath

import yaml
from yaml.constructor import ConstructorError
from yaml.nodes import MappingNode

from hyprial.daemon.impl.desired_state import ServiceRegistry, ServiceRegistryEntry
from hyprial.kernel import ipc_errors

from .models import CatalogEntry, RegistrySnapshot, ServiceConnectError, invalid_registry


_FRONT_FIELDS = frozenset(
    {
        "name",
        "deviceId",
        "remotePort",
        "protocol",
        "localPort",
        "usage",
        "env",
        "auth",
    }
)


class _UniqueKeyLoader(yaml.SafeLoader):
    pass


def _construct_unique_mapping(
    loader: _UniqueKeyLoader, node: yaml.Node, deep: bool = False
) -> dict[object, object]:
    if not isinstance(node, MappingNode):
        raise ConstructorError(None, None, "expected a mapping", node.start_mark)
    loader.flatten_mapping(node)
    result: dict[object, object] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicate = key in result
        except TypeError as error:
            raise ConstructorError(
                None, None, "unhashable catalog key", key_node.start_mark
            ) from error
        if duplicate:
            raise ConstructorError(
                None, None, "duplicate catalog key", key_node.start_mark
            )
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


_UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_unique_mapping
)


BUILTIN_API = CatalogEntry.from_json(
    {
        "name": "api",
        "deviceId": "sub2api",
        "remotePort": 8080,
        "protocol": "http",
        "localPort": 18080,
        "usage": "claude-code-baseurl",
        "env": {"ANTHROPIC_BASE_URL": "http://127.0.0.1:{port}"},
        "auth": "sub2api API key; agents request grant sub2api-key through hq",
        "guide": "Export the printed environment manually. Existing streams break on restart.\n",
    }
)


def parse_catalog_document(filename: str, text: str) -> CatalogEntry:
    """Parse one root markdown document without reflecting invalid input."""

    try:
        path = PurePosixPath(filename)
        if (
            path.name != filename
            or path.suffix != ".md"
            or not text.startswith("---\n")
        ):
            raise invalid_registry()
        marker = text.find("\n---\n", 4)
        if marker < 0:
            raise invalid_registry()
        header_text = text[4:marker]
        guide = text[marker + 5 :]
        header = yaml.load(header_text, Loader=_UniqueKeyLoader)
        if not isinstance(header, dict) or set(header) != _FRONT_FIELDS:
            raise invalid_registry()
        value = dict(header)
        value["guide"] = guide
        entry = CatalogEntry.from_json(value)
        if path.stem != entry.name:
            raise invalid_registry()
        return entry
    except ServiceConnectError:
        raise
    except (TypeError, ValueError, yaml.YAMLError) as error:
        raise invalid_registry() from error


def _desired_entry(entry: CatalogEntry) -> ServiceRegistryEntry:
    return ServiceRegistryEntry.from_json(entry.to_json())


def _catalog_entry(entry: ServiceRegistryEntry) -> CatalogEntry:
    return CatalogEntry.from_json(entry.to_json())


class CatalogRegistry:
    """Read one whole pinned root snapshot, or use a matching last-good cache."""

    def __init__(
        self,
        orgfs: object | None,
        *,
        space_id: str | None,
        owner: str | None,
        cached: ServiceRegistry | None = None,
        now_ms=None,
    ) -> None:
        self._orgfs = orgfs
        self.space_id = space_id
        self.owner = owner
        self._cached = cached
        self._now_ms = now_ms or (lambda: int(time.time() * 1000))

    def load(self) -> RegistrySnapshot:
        if self.space_id is None or self.owner is None:
            return self._builtin(None)
        try:
            overrides = self._read_orgfs()
        except ServiceConnectError as error:
            cache = self._matching_cache()
            if cache is not None:
                return self._merge(
                    tuple(_catalog_entry(entry) for entry in cache.entries),
                    source="cache",
                    error=str(error),
                    cache=cache,
                )
            return self._builtin(str(error))
        cache = ServiceRegistry(
            1,
            self.space_id,
            self.owner,
            self._now_ms(),
            tuple(_desired_entry(entry) for entry in overrides),
        )
        return self._merge(overrides, source="orgfs", error=None, cache=cache)

    def _read_orgfs(self) -> tuple[CatalogEntry, ...]:
        if self._orgfs is None:
            raise ServiceConnectError(
                ipc_errors.SERVICE_REGISTRY_INVALID,
                "service catalog is unavailable",
            )
        try:
            spaces = self._orgfs.spaces()
            space = next(
                (value for value in spaces if value.space_id == self.space_id), None
            )
            if space is None or space.owner != self.owner:
                raise ServiceConnectError(
                    ipc_errors.SERVICE_REGISTRY_INVALID,
                    "service catalog is unavailable",
                )
            nodes = self._orgfs.listdir(self.space_id, "id:root")
            entries: list[CatalogEntry] = []
            for node in nodes:
                if node.deleted:
                    continue
                if node.kind != "doc" or node.name_conflict:
                    raise invalid_registry()
                snapshot = self._orgfs.read_text_snapshot(
                    self.space_id, f"id:{node.node_id}"
                )
                if snapshot.node.node_id != node.node_id or snapshot.node.path != node.name:
                    raise invalid_registry()
                entries.append(parse_catalog_document(node.name, snapshot.content))
            if len({entry.name for entry in entries}) != len(entries):
                raise invalid_registry()
            return tuple(sorted(entries, key=lambda entry: entry.name))
        except ServiceConnectError:
            raise
        except BaseException as error:
            raise ServiceConnectError(
                ipc_errors.SERVICE_REGISTRY_INVALID,
                "service catalog is unavailable",
            ) from error

    def _matching_cache(self) -> ServiceRegistry | None:
        cache = self._cached
        if (
            cache is not None
            and cache.space_id == self.space_id
            and cache.owner == self.owner
        ):
            return cache
        return None

    def _builtin(self, error: str | None) -> RegistrySnapshot:
        return RegistrySnapshot(
            (BUILTIN_API,), "builtin", self.space_id, self.owner, error, None
        )

    def _merge(
        self,
        overrides: tuple[CatalogEntry, ...],
        *,
        source: str,
        error: str | None,
        cache: ServiceRegistry,
    ) -> RegistrySnapshot:
        entries = {BUILTIN_API.name: BUILTIN_API}
        entries.update({entry.name: entry for entry in overrides})
        return RegistrySnapshot(
            tuple(entries[key] for key in sorted(entries)),
            source,
            self.space_id,
            self.owner,
            error,
            cache,
        )
