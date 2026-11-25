# Decisions (actor-rustify P7):
# - One process is allowed exactly one ActorHostRegistry, created by the
#   process entry (daemon / offline management) or lazily by a standalone
#   custody fallback.  Every other module must take an injected host.
# - The allowlist is exact and per-file: a new private registry anywhere else
#   fails closed until the author registers it here with a reason.
# - RED evidence is mechanical: a mutation adds a registry construction to a
#   non-allowlisted production module and the gate must report it.
"""Only documented entries and standalone custody fallbacks may own a registry."""

from __future__ import annotations

import ast
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
SRC = ROOT / "src" / "hyprial"

# relative path -> expected number of direct ActorHostRegistry() calls.
EXPECTED_REGISTRIES: dict[str, int] = {
    # The daemon process entry: one engine for every daemon owner label.
    "daemon/impl/application/__init__.py": 1,
    # The offline management CLI process entry (flock-held, no daemon).
    "daemon/impl/operations/management.py": 1,
    # AgentSessionDomains falls back only when a standalone caller injects none;
    # the daemon path always passes the entry registry.
    "daemon/impl/composition/domains.py": 1,
    # Standalone custody fallbacks (docs/CLI/tests), never the daemon path.
    "daemon/impl/actor_custody/__init__.py": 1,
    "daemon/impl/orgfs/composition/vocabulary.py": 1,
}


def _registry_constructions(root: Path) -> dict[str, int]:
    found: dict[str, int] = {}
    for path in root.rglob("*.py"):
        counts = 0
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = (
                func.id
                if isinstance(func, ast.Name)
                else func.attr
                if isinstance(func, ast.Attribute)
                else None
            )
            if name == "ActorHostRegistry":
                counts += 1
        if counts:
            found[str(path.relative_to(root))] = counts
    return found


def test_only_entries_and_standalone_custody_create_a_registry() -> None:
    assert _registry_constructions(SRC) == EXPECTED_REGISTRIES


def test_mutation_new_private_registry_is_red(tmp_path: Path) -> None:
    tree = tmp_path / "hyprial"
    shutil.copytree(SRC, tree, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    mutant = tree / "daemon" / "impl" / "leaked_owner.py"
    mutant.write_text(
        "from hyprial.daemon.impl.composition.actors import ActorHostRegistry\n"
        "\n"
        "registry = ActorHostRegistry()\n",
        encoding="utf-8",
    )
    assert _registry_constructions(tree) == {
        **EXPECTED_REGISTRIES,
        "daemon/impl/leaked_owner.py": 1,
    }


def test_mutation_attribute_style_construction_is_red(tmp_path: Path) -> None:
    tree = tmp_path / "hyprial"
    shutil.copytree(SRC, tree, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    mutant = tree / "biz" / "impl" / "leaked_owner.py"
    mutant.write_text(
        "from hyprial.daemon.impl.composition import actors\n"
        "\n"
        "registry = actors.ActorHostRegistry()\n",
        encoding="utf-8",
    )
    assert _registry_constructions(tree) == {
        **EXPECTED_REGISTRIES,
        "biz/impl/leaked_owner.py": 1,
    }
