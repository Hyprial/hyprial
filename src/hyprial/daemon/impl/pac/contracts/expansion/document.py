"""Bounded, canonical parsing for authored expansion documents."""

from __future__ import annotations

from hashlib import sha256
import json
import math
from typing import Any

import yaml
from yaml.events import AliasEvent

from hyprial.identity import PAC_EXPANSION_INVALID, PacError

MAX_EXPANSION_TEXT = 65_536
MAX_DOCUMENT_DEPTH = 64


def _invalid(field: str, reason: str, message: str) -> None:
    raise PacError(PAC_EXPANSION_INVALID, message, {"field": field, "reason": reason})


class _Loader(yaml.SafeLoader):
    """SafeLoader with aliases and pathological nesting disabled."""

    depth = 0

    def compose_node(self, parent: Any, index: Any) -> yaml.Node:
        if self.check_event(AliasEvent):
            _invalid("document", "syntax", "YAML aliases are not permitted")
        self.depth += 1
        if self.depth > MAX_DOCUMENT_DEPTH:
            self.depth -= 1
            _invalid("document", "syntax", "expansion document is too deep")
        try:
            return super().compose_node(parent, index)
        finally:
            self.depth -= 1


def _mapping(loader: _Loader, node: yaml.MappingNode, deep: bool = False) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if not isinstance(key, str):
            _invalid("document", "syntax", "document mapping keys must be strings")
        if key in result:
            _invalid("document", "syntax", f"duplicate key {key!r}")
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


_Loader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping)


def _json_value(value: Any, field: str = "document") -> None:
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            _invalid(field, "syntax", "non-finite numbers are not JSON values")
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _json_value(item, f"{field}[{index}]")
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                _invalid(field, "syntax", "document mapping keys must be strings")
            _json_value(item, f"{field}.{key}")
        return
    _invalid(field, "syntax", f"{field} contains a non-JSON value")


def parse_expansion_document(raw: str) -> dict[str, Any]:
    """Parse one YAML/JSON document into bounded JSON-compatible values."""

    if not isinstance(raw, str):
        _invalid("expansion", "syntax", "expansion must be text")
    try:
        if len(raw.encode("utf-8")) > MAX_EXPANSION_TEXT:
            _invalid("expansion", "transport-size", "expansion exceeds 65536 UTF-8 bytes")
    except UnicodeEncodeError:
        _invalid("expansion", "syntax", "expansion is not valid UTF-8")
    loader = _Loader(raw)
    try:
        document = loader.get_single_data()
    except PacError:
        raise
    except (yaml.YAMLError, RecursionError, MemoryError) as error:
        _invalid("document", "syntax", f"invalid expansion document: {error}")
    finally:
        loader.dispose()
    _json_value(document)
    if not isinstance(document, dict):
        _invalid("document", "syntax", "expansion root must be an object")
    return document


def expansion_digest(raw: str) -> str:
    """Hash canonical parsed bytes, never authored formatting or policy facts."""

    document = parse_expansion_document(raw)
    canonical = json.dumps(
        document,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return sha256(canonical).hexdigest()


__all__ = ["MAX_EXPANSION_TEXT", "expansion_digest", "parse_expansion_document"]
