"""Parse and serialize the Markdown organization-context format.

The human-readable Markdown and the fenced YAML block are one document.  A
parsed instance retains its exact source bytes so serialization is lossless.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

import yaml
from yaml.constructor import ConstructorError
from yaml.nodes import MappingNode


_YAML_FENCE = re.compile(
    r"^```ya?ml[ \t]*\n(?P<yaml>.*?)^```[ \t]*(?:\n|$)",
    re.MULTILINE | re.DOTALL | re.IGNORECASE,
)
_PUBLISHER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_REQUIRED_TOP_LEVEL = {
    "meta": dict,
    "lines": list,
    "members": list,
    "routing": list,
    "norms": dict,
    "residents": list,
}


class _UniqueKeyLoader(yaml.SafeLoader):
    """Safe YAML loader that refuses ambiguous duplicate mapping keys."""


def _construct_unique_mapping(
    loader: _UniqueKeyLoader, node: yaml.Node, deep: bool = False
) -> dict[object, object]:
    if not isinstance(node, MappingNode):
        raise ConstructorError(None, None, "expected a mapping node", node.start_mark)
    loader.flatten_mapping(node)
    result: dict[object, object] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicate = key in result
        except TypeError as error:
            raise ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                "found an unhashable mapping key",
                key_node.start_mark,
            ) from error
        if duplicate:
            raise ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                f"found duplicate mapping key {key!r}",
                key_node.start_mark,
            )
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


_UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_unique_mapping
)


class OrgDocumentError(ValueError):
    """The document cannot safely participate in the org-context protocol."""


@dataclass(frozen=True, slots=True)
class OrgMeta:
    version: int
    issued_at: date
    publisher: str

    def to_json(self) -> dict[str, object]:
        return {
            "version": self.version,
            "issuedAt": self.issued_at.isoformat(),
            "publisher": self.publisher,
        }


@dataclass(frozen=True, slots=True)
class OrgDocument:
    """A validated document plus its exact source bytes."""

    source: str
    data: dict[str, Any]
    meta: OrgMeta
    markdown: str


def _validate_record(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise OrgDocumentError(f"{label} must be an object with string keys")
    return value


def _validate_meta(value: object) -> OrgMeta:
    meta = _validate_record(value, "meta")
    version = meta.get("version")
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise OrgDocumentError("meta.version must be a positive integer")

    raw_issued = meta.get("issued_at")
    if isinstance(raw_issued, datetime):
        raise OrgDocumentError("meta.issued_at must be a YYYY-MM-DD date")
    if isinstance(raw_issued, date):
        issued_at = raw_issued
    elif isinstance(raw_issued, str):
        try:
            issued_at = date.fromisoformat(raw_issued)
        except ValueError as error:
            raise OrgDocumentError(
                "meta.issued_at must be a YYYY-MM-DD date"
            ) from error
    else:
        raise OrgDocumentError("meta.issued_at must be a YYYY-MM-DD date")

    publisher = meta.get("publisher")
    if not isinstance(publisher, str) or not _PUBLISHER.fullmatch(publisher):
        raise OrgDocumentError(
            "meta.publisher must be a safe non-empty username"
        )
    # meta.signature is a retired field: pre-unsigning documents carry it,
    # so parse tolerates and ignores it; new documents never write it.  No
    # data migration -- the field costs nothing left in place.
    return OrgMeta(
        version=version,
        issued_at=issued_at,
        publisher=publisher,
    )


def _required_string(record: dict[str, Any], key: str, label: str) -> None:
    value = record.get(key)
    if not isinstance(value, str) or not value:
        raise OrgDocumentError(f"{label}.{key} must be a non-empty string")


def _optional_string(record: dict[str, Any], key: str, label: str) -> None:
    if key in record:
        _required_string(record, key, label)


def _optional_string_list(record: dict[str, Any], key: str, label: str) -> None:
    if key not in record:
        return
    value = record[key]
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item for item in value
    ):
        raise OrgDocumentError(f"{label}.{key} must be an array of strings")


def _validate_schema(data: dict[str, Any]) -> None:
    for collection in ("lines", "members", "routing", "residents"):
        for index, value in enumerate(data[collection]):
            _validate_record(value, f"{collection}[{index}]")

    for index, line in enumerate(data["lines"]):
        label = f"lines[{index}]"
        _required_string(line, "line", label)
        _required_string(line, "owner", label)
        for key in ("coordinator", "board", "watcher"):
            _optional_string(line, key, label)
        for key in ("repos", "boards"):
            _optional_string_list(line, key, label)

    for index, member in enumerate(data["members"]):
        label = f"members[{index}]"
        _required_string(member, "username", label)
        _required_string(member, "display", label)
        for key in ("role", "home_node", "squire", "status"):
            _optional_string(member, key, label)
        _optional_string_list(member, "machines", label)

    for index, route in enumerate(data["routing"]):
        label = f"routing[{index}]"
        _required_string(route, "kind", label)
        _required_string(route, "to", label)
        _optional_string(route, "scope", label)

    norms = _validate_record(data["norms"], "norms")
    autonomy = norms.get("autonomy")
    if not isinstance(autonomy, list) or any(
        not isinstance(item, str) or not item for item in autonomy
    ):
        raise OrgDocumentError("norms.autonomy must be an array of strings")
    _required_string(norms, "escalation", "norms")
    _required_string(norms, "etiquette", "norms")

    for index, resident in enumerate(data["residents"]):
        label = f"residents[{index}]"
        for key in ("name", "node", "role"):
            _required_string(resident, key, label)
        _optional_string(resident, "serves", label)


def parse_document(source: str) -> OrgDocument:
    """Parse and validate exactly one fenced YAML segment from Markdown."""

    if not isinstance(source, str):
        raise TypeError("org-context source must be text")
    matches = list(_YAML_FENCE.finditer(source))
    if len(matches) != 1:
        raise OrgDocumentError(
            "org-context must contain exactly one fenced YAML block"
        )
    match = matches[0]
    yaml_text = match.group("yaml")
    try:
        raw = yaml.load(yaml_text, Loader=_UniqueKeyLoader)
    except yaml.YAMLError as error:
        raise OrgDocumentError(f"org-context YAML is invalid: {error}") from error
    data = _validate_record(raw, "org-context YAML")
    for key, expected_type in _REQUIRED_TOP_LEVEL.items():
        value = data.get(key)
        if not isinstance(value, expected_type):
            label = "an object" if expected_type is dict else "an array"
            raise OrgDocumentError(f"{key} must be {label}")
    _validate_schema(data)

    markdown = source[: match.start()] + source[match.end() :]
    if not markdown.strip():
        raise OrgDocumentError("org-context Markdown body must not be empty")
    meta = _validate_meta(data["meta"])
    return OrgDocument(
        source=source,
        data=data,
        meta=meta,
        markdown=markdown,
    )


def serialize_document(document: OrgDocument) -> str:
    """Serialize without rewriting any signed Markdown or YAML bytes."""

    if not isinstance(document, OrgDocument):
        raise TypeError("document must be an OrgDocument")
    return document.source


def document_summary(document: OrgDocument) -> dict[str, object]:
    """Return a small operator-facing summary without inventing authority."""

    title = next(
        (
            line.lstrip("#").strip()
            for line in document.markdown.splitlines()
            if line.startswith("#") and line.lstrip("#").strip()
        ),
        None,
    )
    return {
        "title": title,
        "lines": len(document.data["lines"]),
        "members": len(document.data["members"]),
        "routes": len(document.data["routing"]),
        "residents": len(document.data["residents"]),
    }
