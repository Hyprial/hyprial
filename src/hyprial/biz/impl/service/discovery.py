"""Auto-discovery of installed services shipped under this package.

One subdirectory with a ``service.yaml`` is one installed service.  The
declaration uses the same fields as OrgFS catalog documents and is validated
by the same catalog entry schema -- no second format.  Failures are explicit:
an invalid declaration raises :class:`ServiceDiscoveryError` naming the
directory and the offending field.  An empty (or absent) service directory is
a legal empty state.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from yaml.constructor import ConstructorError
from yaml.nodes import MappingNode

from hyprial.daemon import CatalogEntry

__all__ = ["ServiceDiscoveryError", "discover_installed_services"]


class ServiceDiscoveryError(RuntimeError):
    """An installed-service declaration is invalid (names directory + field)."""


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


def _service_root() -> Path:
    return Path(__file__).resolve().parent


def discover_installed_services(
    root: Path | None = None,
) -> tuple[CatalogEntry, ...]:
    """Load every installed service declaration under ``biz/impl/service/``.

    Each subdirectory must carry a ``service.yaml`` declaration; a subdirectory
    without one is reported, not skipped.  Same-name duplicates across the
    directory are reported.  Entries come back sorted by name.
    """

    base = _service_root() if root is None else root
    if not base.is_dir():
        # An absent service directory is the same legal empty state as an
        # empty one (the module docstring's promise; iterdir would otherwise
        # turn "nothing installed yet" into FileNotFoundError).
        return ()
    entries: list[CatalogEntry] = []
    seen: set[str] = set()
    for child in sorted(base.iterdir()):
        if not child.is_dir() or child.name.startswith("_"):
            continue
        declaration = child / "service.yaml"
        if not declaration.is_file():
            raise ServiceDiscoveryError(
                f"installed service directory {child.name}/ has no service.yaml"
            )
        entry = _load_declaration(declaration)
        if entry.name in seen:
            raise ServiceDiscoveryError(
                f"duplicate installed service name {entry.name!r}"
            )
        seen.add(entry.name)
        entries.append(entry)
    return tuple(entries)


def _load_declaration(declaration: Path) -> CatalogEntry:
    try:
        payload: Any = yaml.load(
            declaration.read_text(), Loader=_UniqueKeyLoader
        )
    except (yaml.YAMLError, ConstructorError) as error:
        raise ServiceDiscoveryError(
            f"installed service declaration {declaration} is not valid YAML: {error}"
        ) from error
    if not isinstance(payload, dict):
        raise ServiceDiscoveryError(
            f"installed service declaration {declaration} must be a mapping"
        )
    # Name the offending fields before the schema does its generic rejection:
    # "is invalid" without the names made an operator diff the file against
    # the schema by hand (the split spec's field-naming requirement).
    problems = CatalogEntry.field_problems(payload)
    if problems:
        raise ServiceDiscoveryError(
            f"installed service declaration {declaration} is invalid: "
            + "; ".join(problems)
        )
    try:
        return CatalogEntry.from_json(payload)
    except Exception as error:
        raise ServiceDiscoveryError(
            f"installed service declaration {declaration} is invalid: {error}"
        ) from error
