"""Shared facts for the slice 7 pins: what the legacy move/transfer surface is.

Decisions:

- Legacy code is recognised by what it is, not by where it lives: a module is
  legacy when its dotted name is a P2 / ``agent_home_chain`` / SSH orchestrator
  name, or when it defines one of the legacy classes, or when it carries the
  DDL of the historical ``transfer_p2_*`` tables.  The code teleport keeps
  (bundle, landing, envelope, lifecycle primitives) may stay or move freely.
- Imports are read from the AST, including relative imports and the lazy
  ``'name': ('hyprial.x.y', 'attr')`` export tables, never from raw text.
- Each file is parsed once and only its facts (imports, legacy reasons, function
  names) are kept; the 1800 syntax trees are not cached because holding them
  cost hundreds of MB in a worker on a 4 GB CI runner (KP-073).  Checks that
  need arbitrary nodes call ``Module.parse()`` and drop the tree.
- The nine ``transfer.*`` method names are the full set the legacy router
  served on the base branch; they are spelled out because they are history.
"""

from __future__ import annotations

import ast
import functools
import re
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
SRC = ROOT / "src"
PACKAGE = SRC / "hyprial"

LEGACY_METHODS = frozenset(
    {
        "transfer.complete",
        "transfer.custody.override",
        "transfer.land",
        "transfer.plan",
        "transfer.precheck",
        "transfer.quiesce",
        "transfer.receive",
        "transfer.recipientKey",
        "transfer.resume",
    }
)

LEGACY_CLASSES = frozenset(
    {
        "RemoteRunner",
        "SshRunner",
        "AdmissionStore",
        "AdmissionActor",
        "PhaseJournal",
        "_TransferP2Mixin",
    }
)

LEGACY_DDL = re.compile(
    r"create\s+(table|(unique\s+)?index|trigger)\s+(if\s+not\s+exists\s+)?transfer_p2_",
    re.IGNORECASE,
)

TELEPORT_SEEDS = (
    "hyprial.daemon.impl.teleport",
    "hyprial.daemon.impl.lifecycle.primitives",
    "hyprial.daemon.impl.request_catalog.teleport",
    "hyprial.shell.impl.cli.commands.agent.teleport",
)

_DOTTED = re.compile(r"^hyprial(\.[A-Za-z_][A-Za-z0-9_]*)+$")


@dataclass(frozen=True)
class Module:
    """One product module's facts, read in a single pass; the syntax tree is dropped."""

    name: str
    path: Path
    is_package: bool
    imports: frozenset[str]
    reasons: tuple[str, ...]
    functions: frozenset[str]

    def parse(self) -> ast.Module:
        """Parse the module's source now; callers use it once and let it go."""

        return ast.parse(self.path.read_text(encoding="utf-8"), filename=str(self.path))


def _read_imports(tree: ast.Module, name: str, is_package: bool) -> frozenset[str]:
    package = name if is_package else name.rpartition(".")[0]
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = package.split(".")
                base = base[: len(base) - (node.level - 1)]
                stem = ".".join(base + ([node.module] if node.module else []))
            else:
                stem = node.module or ""
            names.add(stem)
            names.update(f"{stem}.{alias.name}" for alias in node.names)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            if _DOTTED.match(node.value):
                names.add(node.value)
    return frozenset(name for name in names if name.startswith("hyprial"))


def _read_reasons(tree: ast.Module) -> tuple[str, ...]:
    reasons: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name in LEGACY_CLASSES:
            reasons.append(f"defines class {node.name}")
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            if LEGACY_DDL.search(node.value):
                reasons.append("creates a transfer_p2_* table/index/trigger")
    return tuple(sorted(set(reasons)))


def _read_functions(tree: ast.Module) -> frozenset[str]:
    return frozenset(
        node.name
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    )


@functools.cache
def product_modules() -> dict[str, Module]:
    """Every ``src/hyprial`` module keyed by dotted name."""

    modules: dict[str, Module] = {}
    for path in sorted(PACKAGE.rglob("*.py")):
        parts = list(path.relative_to(SRC).with_suffix("").parts)
        is_package = parts[-1] == "__init__"
        if is_package:
            parts = parts[:-1]
        name = ".".join(parts)
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        modules[name] = Module(
            name,
            path,
            is_package,
            _read_imports(tree, name, is_package),
            _read_reasons(tree),
            _read_functions(tree),
        )
        del tree
    return modules


def legacy_name(name: str) -> bool:
    """Whether a dotted name designates legacy P2 / SSH / chain code."""

    parts = name.split(".")
    if "agent_home_chain" in parts or "transfer_p2" in parts:
        return True
    for index, part in enumerate(parts):
        if part == "transfer" and "p2" in parts[index + 1 :]:
            return True
    dotted = f".{name}."
    return ".transfer.orchestrator." in dotted or ".transfer.execution.ssh." in dotted


def legacy_content(module: Module) -> list[str]:
    """Reasons a module's body is legacy code, whatever its name."""

    return list(module.reasons)


def legacy_modules() -> dict[str, list[str]]:
    """Every product module that is legacy code, with the reasons."""

    found: dict[str, list[str]] = {}
    for name, module in product_modules().items():
        reasons = legacy_content(module)
        if legacy_name(name):
            reasons = ["legacy module name", *reasons]
        if reasons:
            found[name] = reasons
    return found


def raw_imports(module: Module) -> set[str]:
    """Dotted names a module imports or names in a lazy export table."""

    return set(module.imports)


def resolve(name: str) -> str | None:
    """The longest existing product module a dotted name lands in."""

    modules = product_modules()
    while name:
        if name in modules:
            return name
        if "." not in name:
            return None
        name = name.rpartition(".")[0]
    return None


def module_dependencies(module: Module) -> set[str]:
    """Modules ``module`` names in its imports; implicit parent packages excluded."""

    found = {target for raw in raw_imports(module) if (target := resolve(raw))}
    found.discard(module.name)
    return found


def import_closure(seeds: tuple[str, ...]) -> dict[str, str | None]:
    """Each module reachable from the seeds, mapped to the module importing it."""

    modules = product_modules()
    parent: dict[str, str | None] = {
        name: None
        for name in modules
        if any(name == seed or name.startswith(seed + ".") for seed in seeds)
    }
    pending = list(parent)
    while pending:
        current = pending.pop()
        for dependency in module_dependencies(modules[current]):
            if dependency not in parent:
                parent[dependency] = current
                pending.append(dependency)
    return parent


def chain(parent: dict[str, str | None], name: str) -> str:
    """Render the import path that reached ``name``."""

    links = [name]
    while parent.get(links[-1]) is not None:
        links.append(parent[links[-1]])
    return " <- ".join(links)
