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
#: The one person-name predicate for the org-context format.  A publisher
#: is a person named by their login: the legacy bare short name
#: (``allenwoods``) or the tailnet login shape (``lin.yilun@hyprial.com``,
#: ``jjkysy@github``) -- the same owner spelling a PAC ``user:<owner>``
#: principal stores verbatim.  There is deliberately no second copy of this
#: judgment: the repo's other owner boundaries either accept more than a
#: curated document should (the login/URI owner grammar is only "non-empty,
#: no ':'"), or less (OWNER_NAME_PATTERN / ACTOR_NAME_PATTERN reject ``@``).
#: Hygiene is unchanged from the pre-``@`` publisher grammar: no whitespace,
#: ``':'``, ``'/'``, control characters or leading punctuation, ``@`` at most
#: once, and the 128-character cap still bounds the whole name -- local part
#: and domain together, so the old ceiling is not widened by the domain.
_PERSON_NAME = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._-]*(?:@[A-Za-z0-9][A-Za-z0-9._-]*)?$"
)
_PERSON_NAME_MAX_LENGTH = 128


def is_person_name(value: object) -> bool:
    """Judge one candidate as a person name (a login-shaped owner)."""

    return (
        isinstance(value, str)
        and len(value) <= _PERSON_NAME_MAX_LENGTH
        and _PERSON_NAME.fullmatch(value) is not None
    )

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


def _construct_timestamp(loader: _UniqueKeyLoader, node: yaml.Node) -> object:
    """Construct a timestamp, turning an impossible date into a located error.

    PyYAML resolves ``2026-99-99`` to the timestamp tag and then builds a
    ``date``; the construction raises a bare ``ValueError`` with no mark, so
    a caller cannot name the line.  Re-raise it as a ``ConstructorError`` at
    the node, which the one validation entry point already locates.
    """

    try:
        return yaml.SafeLoader.construct_yaml_timestamp(loader, node)
    except (ValueError, OverflowError) as error:
        raise ConstructorError(
            "while constructing a timestamp",
            node.start_mark,
            str(error),
            node.start_mark,
        ) from error


_UniqueKeyLoader.add_constructor(
    "tag:yaml.org,2002:timestamp", _construct_timestamp
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
    if not is_person_name(publisher):
        raise OrgDocumentError(
            "meta.publisher must be a person name: a short login or an "
            "owner@domain tailnet login (no whitespace, ':', '/', or control "
            "characters; at most 128 characters)"
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


@dataclass(frozen=True, slots=True)
class DocumentIssue:
    """Where and why one document failed validation (1-based file position)."""

    line: int
    column: int
    reason: str

    def to_json(self) -> dict[str, object]:
        return {"line": self.line, "column": self.column, "reason": self.reason}


@dataclass(frozen=True, slots=True)
class DocumentValidation:
    """The single result of validating one org-context document.

    ``ok`` is true only together with a fully validated ``document``.  On
    failure ``issue`` names the file line, column and reason.  Validation never
    raises for malformed input: it is the one entry point a hand-edit
    pre-flight and every in-code writer share.
    """

    ok: bool
    document: OrgDocument | None = None
    issue: DocumentIssue | None = None


def _fenced_block_line(source: str, match: re.Match[str]) -> int:
    return source.count("\n", 0, match.start("yaml")) + 1


def _yaml_failure_issue(
    source: str, match: re.Match[str], error: yaml.YAMLError
) -> DocumentIssue:
    block_line = _fenced_block_line(source, match)
    mark = getattr(error, "problem_mark", None)
    if mark is None:
        line, column = block_line, 1
    else:
        line = block_line + int(mark.line)
        column = int(mark.column) + 1
    context = getattr(error, "context", None)
    problem = getattr(error, "problem", None)
    if problem and context and str(context) != str(problem):
        reason = f"{context}: {problem}"
    else:
        reason = problem or context or "invalid YAML"
    return DocumentIssue(line, column, str(reason))


def validate_document(source: object) -> DocumentValidation:
    """Validate the structured part of one org-context document.

    This is the single validation function for the org-context format.  It
    returns a :class:`DocumentValidation` with a 1-based file line, column and
    reason on failure; it never raises for malformed input, and an invalid
    document is never returned as ``document``.  ``parse_document`` delegates
    here, and every in-code writer calls this before it writes.
    """

    if not isinstance(source, str):
        return DocumentValidation(
            ok=False,
            issue=DocumentIssue(1, 1, "org-context source must be text"),
        )
    matches = list(_YAML_FENCE.finditer(source))
    if len(matches) != 1:
        if matches:
            line = source.count("\n", 0, matches[1].start()) + 1
        else:
            line = 1
        return DocumentValidation(
            ok=False,
            issue=DocumentIssue(
                line, 1, "org-context must contain exactly one fenced YAML block"
            ),
        )
    match = matches[0]
    yaml_text = match.group("yaml")
    try:
        raw = yaml.load(yaml_text, Loader=_UniqueKeyLoader)
    except yaml.YAMLError as error:
        return DocumentValidation(ok=False, issue=_yaml_failure_issue(source, match, error))
    except (ValueError, TypeError, OverflowError) as error:
        # Belt and braces: a constructor that raises outside the YAML error
        # hierarchy must still be a located result, never an exception.
        return DocumentValidation(
            ok=False,
            issue=DocumentIssue(
                _fenced_block_line(source, match), 1, f"invalid YAML value: {error}"
            ),
        )
    block_line = _fenced_block_line(source, match)
    try:
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
    except OrgDocumentError as error:
        return DocumentValidation(ok=False, issue=DocumentIssue(block_line, 1, str(error)))
    return DocumentValidation(
        ok=True,
        document=OrgDocument(source=source, data=data, meta=meta, markdown=markdown),
    )


def parse_document(source: str) -> OrgDocument:
    """Parse and validate exactly one fenced YAML segment from Markdown."""

    result = validate_document(source)
    if not result.ok or result.document is None:
        issue = result.issue or DocumentIssue(1, 1, "org-context is invalid")
        raise OrgDocumentError(f"{issue.reason} (line {issue.line}, column {issue.column})")
    return result.document


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
