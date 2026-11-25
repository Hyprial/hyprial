"""Optional read-only projections of current OrgFS document text.

The original text and YAML fields remain ordinary author-owned data. Parsing
never validates history, approves an owner action, or writes a repaired file.
Known fields are projected only when their shape is usable; diagnostics and
the unchanged source remain available even when that optional view is absent.
"""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

import yaml

_YAML_BLOCK = re.compile(r"^```ya?ml[ \t]*\r?\n(.*?)^```", re.MULTILINE | re.DOTALL)
_HEADING = re.compile(r"^#{1,2}[ \t]+(.+?)[ \t]*$", re.MULTILINE)
_LOG_SECTION = re.compile(r"^##[ \t]+Log[ \t]*\r?\n(.*?)(?=^##[ \t]+|\Z)", re.MULTILINE | re.DOTALL)


def _epoch_ms(value: object) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if isinstance(value, datetime):
        return int(value.replace(tzinfo=value.tzinfo or UTC).timestamp() * 1000)
    if isinstance(value, date):
        return int(datetime(value.year, value.month, value.day, tzinfo=UTC).timestamp() * 1000)
    return None


@dataclass(frozen=True)
class WorkItem:
    """Source text with an optional YAML mapping, never a write authority."""

    text: str
    head: dict[Any, Any]
    title: str
    log: tuple[str, ...]
    diagnostics: tuple[str, ...] = ()

    @property
    def id(self) -> str:
        value = self.head.get("id")
        return value if isinstance(value, str) else ""

    def to_dict(self) -> dict[str, Any]:
        """Typed display fields plus unchanged source, without approval claims."""

        diagnostics = list(self.diagnostics)

        def scalar(name: str, default: Any = "") -> Any:
            value = self.head.get(name, default)
            if isinstance(value, str) or value is None and default is None:
                return value
            diagnostics.append(f"{name}: optional display field must be text")
            return default

        def strings(name: str) -> list[str]:
            value = self.head.get(name, [])
            if isinstance(value, list) and all(isinstance(item, str) for item in value):
                return list(value)
            diagnostics.append(f"{name}: optional display field must be a list of strings")
            return []

        checklist = self.head.get("checklist", [])
        if not isinstance(checklist, list):
            diagnostics.append("checklist: optional display field must be a list")
            checklist = []
        entries = []
        for index, item in enumerate(checklist):
            if isinstance(item, dict) and isinstance(item.get("text"), str) and isinstance(item.get("done"), bool):
                entries.append({"text": item["text"], "done": item["done"]})
            else:
                diagnostics.append(f"checklist[{index}]: optional display item needs text and a boolean done")
        result = {
            "id": scalar("id"), "title": self.title, "status": scalar("status"),
            "owner": scalar("owner"), "assignee": scalar("assignee", None),
            "checklist": entries, "depends": strings("depends"),
            "acceptance": scalar("acceptance"), "keywords": strings("keywords"),
            "pacs": strings("pacs"), "log": list(self.log),
            "documentText": self.text,
        }
        for name in ("opened", "closed"):
            value = self.head.get(name)
            result[name] = _epoch_ms(value)
            if value is not None and result[name] is None:
                diagnostics.append(f"{name}: optional display field is not a usable timestamp")
        result["diagnostics"] = diagnostics
        return result


def parse_work_item(text: str) -> WorkItem:
    """Read optional metadata without rejecting or rewriting the document."""

    block = _YAML_BLOCK.search(text)
    head: dict[Any, Any] = {}
    diagnostics = []
    if block is None:
        diagnostics.append("no optional YAML task metadata")
    else:
        try:
            raw = yaml.safe_load(block.group(1))
            if isinstance(raw, dict):
                head = copy.deepcopy(raw)
            else:
                diagnostics.append("optional YAML metadata is not a mapping")
        except (yaml.YAMLError, ValueError, RecursionError) as error:
            diagnostics.append(f"optional YAML metadata could not be parsed: {str(error).splitlines()[0]}")
    heading = _HEADING.search(text[:block.start()] if block is not None else text)
    title = heading.group(1) if heading else ""
    item_id = head.get("id")
    if isinstance(item_id, str) and title.startswith(item_id + " "):
        title = title[len(item_id) + 1:]
    log = _LOG_SECTION.search(text)
    return WorkItem(
        text=text, head=head, title=title or (item_id if isinstance(item_id, str) else ""),
        log=tuple(line for line in log.group(1).splitlines() if line.strip()) if log else (),
        diagnostics=tuple(diagnostics),
    )
