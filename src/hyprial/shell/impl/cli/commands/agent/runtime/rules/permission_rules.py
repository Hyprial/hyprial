"""Catch claude permission rules that are accepted but do not do what they look like.

Two documented Claude Code behaviours:

* A path rule for Write, MultiEdit, NotebookEdit or Glob never matches: file
  permissions are checked against Edit(...) and Read(...) rules only.  The tool
  name alone proves it, so these are always refused.
* For rules passed as CLI flags, ``Edit(/x/**)`` is VALID and anchors at the
  primary working directory (``<cwd>/x/**``); an absolute filesystem path needs
  a double slash, ``Edit(//x/**)``.  Rewriting a valid cwd-relative rule to the
  absolute form would silently widen it, so a single-slash Edit/Read rule is
  refused only when it almost certainly was meant as absolute: the first path
  segment is a real root directory AND no such directory exists under the cwd.
  If it exists under the cwd too, or the cwd is unknown, the rule is only
  warned about, because the check cannot tell which one the user meant.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

_RULE_FLAGS = frozenset(
    {"--allowedTools", "--allowed-tools", "--disallowedTools", "--disallowed-tools"}
)
# Tools whose path rules never match, and the rule that actually covers them.
_NEVER_MATCHED_TOOLS = {
    "Write": "Edit",
    "MultiEdit": "Edit",
    "NotebookEdit": "Edit",
    "Glob": "Read",
}
_PATH_RULE = re.compile(
    r"^(?P<tool>Edit|Read|Write|MultiEdit|NotebookEdit|Glob)\((?P<path>[^)]+)\)$"
)


def _split_rules(value: str) -> list[str]:
    rules: list[str] = []
    current: list[str] = []
    depth = 0
    for char in value:
        if char == "(":
            depth += 1
        elif char == ")":
            depth = max(0, depth - 1)
        if depth == 0 and (char == "," or char.isspace()):
            if current:
                rules.append("".join(current))
                current = []
            continue
        current.append(char)
    if current:
        rules.append("".join(current))
    return rules


def _rule_values(args: Iterable[str]) -> list[str]:
    values: list[str] = []
    in_flag = False
    for token in args:
        flag, equals, inline = token.partition("=")
        if flag in _RULE_FLAGS:
            in_flag = True
            if equals:
                values.extend(_split_rules(inline))
            continue
        if token.startswith("-"):
            in_flag = False
            continue
        if in_flag:
            values.extend(_split_rules(token))
    return values


@dataclass(frozen=True)
class RuleFindings:
    """``(rule, suggested rule)`` pairs: ``refuse`` must not start, ``warn`` may."""

    refuse: list[tuple[str, str]] = field(default_factory=list)
    warn: list[tuple[str, str]] = field(default_factory=list)


def _single_slash_first_segment(path: str) -> str | None:
    """The first segment of ``/seg/...`` when it names a real directory under ``/``."""

    if not path.startswith("/") or path.startswith("//"):
        return None
    first_segment = path[1:].split("/", 1)[0]
    if not first_segment or "*" in first_segment or not os.path.isdir("/" + first_segment):
        return None
    return first_segment


def inert_path_rules(args: Iterable[str], cwd: Path | None = None) -> RuleFindings:
    """Classify the path rules in ``args``; ``cwd`` is the agent's primary working directory.

    Depends on the host filesystem (root directories, and the cwd's contents),
    so the same rule can be classified differently on another host.
    """

    findings = RuleFindings()
    for rule in _rule_values(args):
        match = _PATH_RULE.match(rule)
        if match is None:
            continue
        tool, path = match.group("tool"), match.group("path")
        covering_tool = _NEVER_MATCHED_TOOLS.get(tool)
        if covering_tool is not None:
            # The suggestion keeps the path exactly as written: same anchoring, working tool.
            findings.refuse.append((rule, f"{covering_tool}({path})"))
            continue
        first_segment = _single_slash_first_segment(path)
        if first_segment is None:
            continue
        absolute = f"{tool}(/{path})"
        if cwd is not None and not (cwd / first_segment).exists():
            findings.refuse.append((rule, absolute))
        else:
            findings.warn.append((rule, absolute))
    return findings
