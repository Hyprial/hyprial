# Decision record (file header; no inline comments by repository rule):
# - Agent teleport slice 3: ``RuntimeContextActivation`` moves out of
#   ``transfer/p2/`` to ``hyprial.daemon.impl.lifecycle.primitives.activation``
#   together with the types its interface names (``ActivationError``,
#   ``ActivationReceipt``, ``ActivationPort``).  Teleport binds a landed home
#   through it without dragging in the P2 ledger, admission store, SSH runner
#   or orchestrator.
# - The old P2 module path is gone with the P2 package (slice 7), so the
#   re-export check was retired; the fence below keeps the activation free
#   of the retired ledger, store, runner and orchestrator names.
# - The import fence is checked twice: on the module's own import statements
#   (AST, the literal criterion) and on the modules a fresh interpreter loads
#   when importing it (so a re-export through another module cannot hide the
#   dependency).
"""The relocated runtime-context activation and its import fence."""

from __future__ import annotations

import ast
import importlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

NEW_MODULE = "hyprial.daemon.impl.lifecycle.primitives.activation"
FORBIDDEN_NAMES = frozenset({"journal", "AdmissionStore", "RemoteRunner", "orchestrator"})
FORBIDDEN_MODULES = (
    "hyprial.daemon.impl.transfer.p2.journal",
    "hyprial.daemon.impl.transfer.p2.store",
    "hyprial.daemon.impl.transfer.p2.orchestration.orchestrator",
    "hyprial.daemon.impl.transfer.p2.orchestration.target",
    "hyprial.daemon.impl.transfer.orchestrator",
    "hyprial.daemon.impl.transfer.execution.ssh",
)
REPO_ROOT = Path(__file__).resolve().parents[4]


def _module_source() -> tuple[Path, ast.Module]:
    module = importlib.import_module(NEW_MODULE)
    path = Path(module.__file__)
    return path, ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _imports(tree: ast.Module) -> list[tuple[str, tuple[str, ...]]]:
    found: list[tuple[str, tuple[str, ...]]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend((alias.name, ()) for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            prefix = "." * node.level + (node.module or "")
            found.append((prefix, tuple(alias.name for alias in node.names)))
    return found


def test_activation_lives_outside_the_p2_package() -> None:
    path, _tree = _module_source()

    relative = path.resolve().relative_to(REPO_ROOT / "src")
    assert "p2" not in relative.parts
    assert "transfer" not in relative.parts


def test_activation_module_imports_no_p2_ledger_store_runner_or_orchestrator() -> None:
    _path, tree = _module_source()

    for module, names in _imports(tree):
        segments = set(module.lstrip(".").split("."))
        assert not segments & FORBIDDEN_NAMES, module
        assert not set(names) & FORBIDDEN_NAMES, (module, names)
        assert not module.startswith("hyprial.daemon.impl.transfer.p2"), module


def test_importing_activation_loads_no_p2_ledger_store_runner_or_orchestrator() -> None:
    probe = (
        "import importlib, json, sys\n"
        f"importlib.import_module({NEW_MODULE!r})\n"
        f"print(json.dumps(sorted(m for m in sys.modules if m in {list(FORBIDDEN_MODULES)!r})))\n"
    )
    completed = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout.strip().splitlines()[-1]) == []


def test_package_exports_the_activation() -> None:
    from hyprial.daemon.impl.lifecycle import primitives
    from hyprial.daemon.impl.lifecycle.primitives import activation

    assert primitives.RuntimeContextActivation is activation.RuntimeContextActivation
    assert primitives.ActivationError is activation.ActivationError


class _Receipt:
    def __init__(self, path: Path) -> None:
        self.path = str(path)


class _Agent:
    def __init__(self, actor: str, home: Path, token: str) -> None:
        self.actor = actor
        self.uri = f"agent:{actor}"
        self.entity_token = token
        self.config = None
        self.cwd = str(home / "workspace")


class _Registry:
    def __init__(self, home: Path) -> None:
        self._home = home

    def require(self, actor: str) -> _Agent:
        return _Agent(actor, self._home, "landed-token")

    def get(self, actor: str) -> _Agent:
        return self.require(actor)

    def home_receipt(self, actor: str) -> _Receipt:
        return _Receipt(self._home)


def _activation(tmp_path: Path, **overrides: object):
    from hyprial.daemon.impl.lifecycle.primitives.activation import RuntimeContextActivation

    home = tmp_path / "agents" / "wk"
    (home / "config").mkdir(parents=True, exist_ok=True)
    arguments: dict[str, object] = {
        "harnesses": ("pi",),
        "tool_profile": None,
        "base_environment": {"PATH": "/usr/bin:/bin"},
        "cwd": str(tmp_path / "workspace"),
    }
    arguments.update(overrides)
    return RuntimeContextActivation(_Registry(home), **arguments)


def test_relocated_activation_refuses_a_foreign_incarnation(tmp_path: Path) -> None:
    from hyprial.daemon.impl.lifecycle.primitives.activation import ActivationError

    with pytest.raises(ActivationError) as refusal:
        _activation(tmp_path).activate("wk", "some-other-incarnation")

    assert refusal.value.code == "incarnation-drift"


def test_relocated_activation_refuses_to_undo_a_foreign_incarnation(tmp_path: Path) -> None:
    from hyprial.daemon.impl.lifecycle.primitives.activation import ActivationError

    with pytest.raises(ActivationError) as refusal:
        _activation(tmp_path).deactivate("wk", "some-other-incarnation")

    assert refusal.value.code == "incarnation-drift"


@pytest.mark.parametrize("harnesses", [(), ("pi", "pi"), ("jev",), ("agy",)])
def test_relocated_activation_validates_its_harnesses(
    tmp_path: Path, harnesses: tuple[str, ...]
) -> None:
    with pytest.raises(ValueError):
        _activation(tmp_path, harnesses=harnesses)
