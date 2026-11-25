"""Seams of the conversations package (plan §2.0 boundary rules).

Network, the inbox engine, delivery and threads never import conversations.
Conversations reads delivery through the facade ports only: it imports no
inbox engine, application, network or composition module, so the recorder
and ``ConversationBodies`` stay testable over plain ports.
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
IMPL = ROOT / "src" / "hyprial" / "daemon" / "impl"
CONVERSATIONS = "hyprial.daemon.impl.conversations"
NO_CONVERSATIONS = ("network", "inbox", "delivery", "threads", "cells", "orgfs")
CONVERSATIONS_FORBIDDEN = tuple(
    f"hyprial.daemon.impl.{name}"
    for name in ("inbox", "application", "network", "composition", "links", "threads")
)


def _imports(path: Path) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text("utf-8"))):
        if isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
        elif isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
    return found


def test_the_package_has_its_faces() -> None:
    names = {path.relative_to(IMPL / "conversations").as_posix()
             for path in (IMPL / "conversations").rglob("*.py")}
    assert {"__init__.py", "bodies.py", "record/recorder.py"} <= names


def test_no_lower_package_imports_conversations() -> None:
    offenders = {
        str(path.relative_to(IMPL)): sorted(
            module for module in _imports(path) if module.startswith(CONVERSATIONS)
        )
        for name in NO_CONVERSATIONS
        for path in (IMPL / name).rglob("*.py")
    }
    assert {name: bad for name, bad in offenders.items() if bad} == {}


def test_conversations_imports_no_engine_or_composition() -> None:
    offenders = {
        str(path.relative_to(IMPL)): sorted(
            module for module in _imports(path) if module.startswith(CONVERSATIONS_FORBIDDEN)
        )
        for path in (IMPL / "conversations").rglob("*.py")
    }
    assert {name: bad for name, bad in offenders.items() if bad} == {}
