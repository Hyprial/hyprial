"""The daemon binds its own application to every owner-key consumer it wires.

Building the full transport graph needs a live daemon; these checks pin the
two production call sites instead: each passes ``app=self`` so the owner-key
authority (behind its gate) can see the daemon that owns the launch or export.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

import hyprial

SRC = Path(hyprial.__file__).resolve().parent

CALL_SITES = (
    ("daemon/impl/application/wiring/transport.py", "compose_daemon_worker_launch", "self"),
    ("daemon/impl/teleport/wiring/source_export.py", "env_credentials_for_quiesce", "app"),
)


def _keywords(path: Path, function: str) -> list[dict[str, str]]:
    found = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == function:
            found.append({kw.arg: ast.unparse(kw.value) for kw in node.keywords})
    return found


@pytest.mark.parametrize(("relative", "function", "application"), CALL_SITES)
def test_the_production_call_site_passes_the_daemon_application(relative, function, application):
    calls = _keywords(SRC / relative, function)
    assert calls, f"{function} is no longer called from {relative}"
    assert all(call.get("app") == application for call in calls)
