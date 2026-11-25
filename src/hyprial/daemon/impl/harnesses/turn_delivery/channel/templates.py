"""Packaged worker-channel permission templates.

Decision notes (I6-5a, ruling §13.1.5):

- The template is data the agent-cell registrar seeds into a claude member's
  ``cell.members.<m>.allow``/``deny`` when the document declares neither
  (``member_rules``).  The launch reads the cell document only; the template
  is never a launch-time default.  ``harness_tools`` stays channel wiring:
  the bridge tool names a worker channel always registers.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from importlib.resources import files

_PACKAGE = "hyprial.daemon.impl.harnesses.turn_delivery.channel"
_VERSION = 1
_TEMPLATE_KEYS = {
    "version",
    "name",
    "harnessTools",
    "allowedTools",
    "disallowedTools",
}


@dataclass(frozen=True, slots=True)
class WorkerChannelTemplate:
    """One data-defined harness and CLI policy for a managed worker."""

    name: str
    harness_tools: tuple[str, ...]
    allowed_tools: tuple[str, ...]
    disallowed_tools: tuple[str, ...]

    def bridge_tools(self) -> tuple[str, ...]:
        """The harness-bridge MCP tool names of this channel's registered tools."""

        return tuple(f"mcp__harness-bridge__{tool}" for tool in self.harness_tools)

    def member_rules(self) -> tuple[tuple[str, ...], tuple[str, ...]]:
        """The ``(allow, deny)`` rules the registrar seeds into a cell member."""

        return self.bridge_tools() + self.allowed_tools, self.disallowed_tools


def _strings(raw: object, field: str) -> tuple[str, ...]:
    if (
        not isinstance(raw, list)
        or any(not isinstance(item, str) or not item for item in raw)
        or len(set(raw)) != len(raw)
        or not raw
    ):
        raise ValueError(f"worker channel template {field} must be a list of non-empty strings")
    return tuple(raw)


def load_worker_channel_template(name: str) -> WorkerChannelTemplate:
    """Load a packaged worker-channel policy by template name."""

    if not isinstance(name, str) or not name or "/" in name or "\\" in name:
        raise ValueError(f"invalid worker channel template name {name!r}")
    resource = files(_PACKAGE).joinpath("templates").joinpath(f"{name}.json")
    try:
        raw = json.loads(resource.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ValueError(f"worker channel template {name!r} is not available: {error}") from error
    if not isinstance(raw, dict):
        raise ValueError(f"worker channel template {name!r} must be an object")
    if set(raw) != _TEMPLATE_KEYS:
        raise ValueError(f"worker channel template {name!r} has invalid top-level keys")
    if raw.get("version") != _VERSION or raw.get("name") != name:
        raise ValueError(f"worker channel template {name!r} has an invalid version or name")
    return WorkerChannelTemplate(
        name=name,
        harness_tools=_strings(raw.get("harnessTools"), "harnessTools"),
        allowed_tools=_strings(raw.get("allowedTools"), "allowedTools"),
        disallowed_tools=_strings(raw.get("disallowedTools"), "disallowedTools"),
    )
