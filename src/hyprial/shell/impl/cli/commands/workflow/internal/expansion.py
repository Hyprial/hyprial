"""CLI input and rendering helpers for PAC workflow expansion."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Mapping


MAX_EXPANSION_BYTES = 65_536
_TARGET = re.compile(
    r"(?P<graph>[^:\s]+):(?P<placeholder>[A-Za-z][A-Za-z0-9_.-]{0,63})"
)


def parse_expansion_target(value: str) -> dict[str, str]:
    """Parse the CLI-only ``graph:placeholder`` envelope.

    This is deliberately not an address parser: a graph id may contain any
    non-whitespace non-colon characters, while the placeholder is a named
    workflow node.  Named groups keep the two fields distinct and avoid
    accidentally broadening identity URI parsing rules.
    """

    match = _TARGET.fullmatch(value)
    if match is None:
        raise ValueError(
            "--expansion-for must be graph:placeholder with a named placeholder"
        )
    return {"graphId": match.group("graph"), "nodeId": match.group("placeholder")}


def read_expansion_file(path: Path) -> str:
    """Read an expansion document without sending the origin filename."""

    try:
        payload = path.read_bytes()
    except OSError as error:
        raise ValueError(f"cannot read expansion file: {error}") from error
    if len(payload) > MAX_EXPANSION_BYTES:
        raise ValueError(
            f"expansion must be at most {MAX_EXPANSION_BYTES} UTF-8 bytes"
        )
    try:
        return payload.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError("expansion file must be valid UTF-8") from error


def render_workflow_status(data: Mapping[str, Any]) -> str:
    """Render status with a compact, human-readable child projection."""

    parent = data.get("parentGraphId")
    heading = f"workflow {data.get('graphId', data.get('runId', '?'))} {data.get('state', '?')}"
    if parent:
        heading += f" ↳ parent {parent}"
    lines = [
        heading
    ]
    for node in data.get("nodes") or []:
        if not isinstance(node, dict):
            continue
        label = f"{node.get('nodeId', '?')}:{node.get('state', '?')}"
        if node.get("kind") == "expansion" or node.get("systemOwned"):
            child = node.get("childGraphId") or "-"
            child_state = node.get("childState") or "unknown"
            label += f" ↳ child {child}:{child_state}"
            children = node.get("childNodes") or []
            if isinstance(children, list):
                states = [
                    f"{item.get('nodeId', '?')}:{item.get('state', '?')}"
                    for item in children
                    if isinstance(item, dict)
                ]
                if states:
                    label += " [" + ", ".join(states[:12]) + "]"
        lines.append("  " + label)
    return "\n".join(lines)
