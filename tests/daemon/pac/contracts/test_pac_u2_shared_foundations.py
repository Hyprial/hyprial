"""U2 ownership tripwires for PAC/workflow-v1 decoupling."""

from __future__ import annotations

import ast
from pathlib import Path

from hyprial.daemon.impl.dispatch.admission import DISPATCH_ROLES
from hyprial.daemon.impl.dispatch.identity import (
    DISPATCH_SERVICE_ACTOR_NAME,
    dispatch_service_actor_uri,
)

ROOT = Path(__file__).resolve().parents[4]
SRC = ROOT / "src" / "hyprial"
SHARED_MODULES = (
    SRC / "kernel" / "impl" / "primitives" / "duration.py",
    SRC / "daemon" / "impl" / "dispatch" / "admission.py",
    SRC / "daemon" / "impl" / "dispatch" / "identity.py",
    SRC / "daemon" / "impl" / "pac" / "actors" / "assignments.py",
    SRC / "daemon" / "impl" / "pac" / "storage" / "assignments.py",
    SRC / "daemon" / "impl" / "pac" / "actors" / "reconcile.py",
    SRC / "daemon" / "impl" / "inbox" / "links" / "io.py",
    SRC / "daemon" / "impl" / "pac" / "actors" / "daemon.py",
)


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def test_shared_foundations_have_no_workflow_package_import() -> None:
    offenders: list[str] = []
    for path in SHARED_MODULES:
        for node in ast.walk(_tree(path)):
            if isinstance(node, ast.ImportFrom) and node.module and (
                node.module == "hyprial.workflow"
                or node.module.startswith("hyprial.workflow.")
            ):
                offenders.append(f"{path.relative_to(ROOT)}:{node.lineno}")
            elif isinstance(node, ast.Import):
                offenders.extend(
                    f"{path.relative_to(ROOT)}:{node.lineno}"
                    for alias in node.names
                    if alias.name == "hyprial.workflow"
                    or alias.name.startswith("hyprial.workflow.")
                )
    assert offenders == []


def test_legacy_modules_no_longer_own_the_moved_definitions() -> None:
    for module in ("schema", "store", "registry", "executor", "service", "ports", "observation", "inbox_io"):
        assert not (SRC / "workflow" / f"{module}.py").exists()
    assert (SRC / "daemon" / "impl" / "pac" / "workflows" / "runtime" / "__init__.py").exists()
    assert (SRC / "daemon" / "impl" / "pac" / "storage" / "legacy.py").exists()
    assert not (SRC / "daemon" / "dispatch_gate.py").exists()


def test_dispatch_roles_and_sender_identity_are_stable_shared_contracts() -> None:
    assert DISPATCH_ROLES == frozenset({"plan", "dispatch", "review", "execute"})
    assert DISPATCH_SERVICE_ACTOR_NAME == "dispatch-coordinator"
    assert dispatch_service_actor_uri("allen", "node") == (
        "agent:allen:node:dispatch-coordinator"
    )
