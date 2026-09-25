"""Host capability ledger values. Recording a grant does not enforce it."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
from pathlib import Path

from hyprial.pac.errors import PacError
from hyprial.pac.principal import parse_principal

CAPABILITY_VALUES = (
    "agent-home", "see-actors", "send-to", "tool-surface",
    "channel", "shared-path", "org-context", "isolation",
)


def single_line(value: str, field: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip() or any(
        ord(char) < 32 or ord(char) == 127 for char in value
    ):
        raise ValueError(f"{field} must be a non-empty, unpadded single line")
    return value


def principal(value: str) -> None:
    try:
        parse_principal(value)
    except PacError as error:
        raise ValueError(str(error)) from error


def validate_scope(capability: str, scope: str) -> None:
    single_line(scope, "scope")
    if capability not in CAPABILITY_VALUES:
        raise ValueError("unknown capability")
    fixed = {"agent-home": ("self",), "org-context": ("accepted",),
             "isolation": ("directory", "container")}
    if capability in fixed:
        if scope not in fixed[capability]:
            raise ValueError(f"invalid {capability} scope")
        return
    try:
        value = json.loads(scope)
    except json.JSONDecodeError as error:
        raise ValueError("scope must be one JSON value") from error
    if capability == "shared-path":
        if not isinstance(value, dict) or set(value) != {"path", "mode"}:
            raise ValueError("shared-path scope needs exactly path and mode")
        path = Path(single_line(value["path"], "path"))
        if not path.is_absolute() or ".." in path.parts or value["mode"] not in ("ro", "rw"):
            raise ValueError("shared-path needs an absolute path and ro/rw mode")
        return
    if not isinstance(value, list) or not value:
        raise ValueError("scope must be a non-empty JSON array")
    for item in value:
        single_line(item, "scope item")
        if capability in ("see-actors", "send-to"):
            principal(item)


def _json(record):
    return {
        key.split("_")[0] + "".join(part.title() for part in key.split("_")[1:]): value
        for key, value in asdict(record).items()
    }


@dataclass(frozen=True, slots=True)
class CapabilityGrant:
    actor: str
    entity_token: str
    grant_id: str
    capability: str
    scope: str
    granted_by: str
    revision: int

    def __post_init__(self):
        single_line(self.grant_id, "grant_id")
        validate_scope(self.capability, self.scope)
        principal(self.granted_by)
        if type(self.revision) is not int or self.revision < 1:
            raise ValueError("revision must be a positive integer")

    def to_json(self):
        return _json(self)


@dataclass(frozen=True, slots=True)
class GrantJournalEntry:
    seq: int
    at_ms: int
    actor: str
    entity_token: str
    action: str
    grant_id: str
    capability: str | None
    scope: str | None
    by: str
    revision: int | None
    note: str | None

    def to_json(self):
        return _json(self)
