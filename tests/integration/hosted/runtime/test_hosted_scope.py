"""Decision A: authorization has one implemented entry, no setup backdoor."""
from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]


def test_only_the_identity_actor_calls_hosting_authority() -> None:
    calls = []
    commands = []
    reserved = []
    for path in (ROOT / "src").rglob("*.py"):
        tree = ast.parse(path.read_text())
        relative = str(path.relative_to(ROOT / "src"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                if isinstance(node.func, ast.Attribute) and node.func.attr == "create_transfer_hosted":
                    calls.append(relative)
                if isinstance(node.func, ast.Name) and node.func.id == "CreateTransferHostedAgentCommand":
                    commands.append(relative)
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and "squire-container" in node.value:
                reserved.append((relative, node.value))
    assert sorted(calls) == ["hyprial/identity/impl/agents/actor/_generation_dispatch.py"]
    assert commands == ["hyprial/daemon/impl/composition/views.py"]
    assert reserved == []
