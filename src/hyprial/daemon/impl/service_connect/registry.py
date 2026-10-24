"""Trusted public service catalog loading with explicit fallback provenance."""

from __future__ import annotations

import time
from collections.abc import Callable
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
                None, None, f"duplicate catalog key {key!r}", key_node.start_mark
            )
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


_UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_unique_mapping
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
        discovered: Callable[[], tuple[CatalogEntry, ...]] | None = None,
    ) -> None:
        self._orgfs = orgfs
        self.space_id = space_id
        self.owner = owner
        self._cached = cached
        self._now_ms = now_ms or (lambda: int(time.time() * 1000))
        self._discovered = discovered

    def load(self) -> RegistrySnapshot:
        error: str | None = None
        overrides: tuple[CatalogEntry, ...] = ()
        cache: ServiceRegistry | None = None
        # Source labels follow the BRANCH (orgfs success / cache fallback /
        # discovery-only), not the entry count: an empty OrgFS space is still
        # an OrgFS read.
        source = "discovery"
        if self.space_id is not None and self.owner is not None:
            source = "orgfs"
            try:
                overrides = self._read_orgfs()
            except ServiceConnectError as orgfs_error:
                cache = self._matching_cache()
                error = str(orgfs_error)
                source = "discovery"
                if cache is not None:
                    source = "cache"
                    overrides = tuple(
                        _catalog_entry(entry) for entry in cache.entries
                    )
            else:
                cache = ServiceRegistry(
                    1,
                    self.space_id,
                    self.owner,
                    self._now_ms(),
                    tuple(_desired_entry(entry) for entry in overrides),
                )
        discovered = self._discovered_services()
        return self._merge_with_discovery(
            discovered,
            overrides,
            source=source,
            error=error,
            cache=cache,
        )

    def _discovered_services(self) -> tuple[CatalogEntry, ...]:
        discovery = self._discovered
        return discovery() if discovery is not None else ()

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

    def _merge_with_discovery(
        self,
        discovered: tuple[CatalogEntry, ...],
        overrides: tuple[CatalogEntry, ...],
        *,
        source: str,
        error: str | None,
        cache: ServiceRegistry | None,
    ) -> RegistrySnapshot:
        # Load order (spec: service module split): OrgFS documents first, then
        # shipped service declarations; a same-name conflict is won by the
        # explicit OrgFS entry.  The winner rule is fixed, so the observable
        # signal is the conflict list itself -- without it a shadowed shipped
        # declaration is silent (the review's ② c).
        entries = {entry.name: entry for entry in discovered}
        explicit = {entry.name: entry for entry in overrides}
        conflicts = tuple(sorted(entries.keys() & explicit.keys()))
        entries.update(explicit)
        snapshot_cache = cache
        return RegistrySnapshot(
            tuple(entries[key] for key in sorted(entries)),
            source,
            self.space_id,
            self.owner,
            error,
            snapshot_cache,
            conflicts,
        )
