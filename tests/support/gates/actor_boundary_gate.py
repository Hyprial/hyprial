# Engine-holding rule (actor-rustify P4, prerequisites 2026-11-01 section 3):
# - Region: only src/hyprial/kernel/impl/actor_runtime/** and the explicit
#   ENGINE_COMPOSITION_ROOTS may hold the engine.  The allowlist starts empty;
#   P5 adds each host composition root by path in its own reviewed diff.
# - Held names are matched by name, not by resolved origin, so a renamed import
#   path cannot hide a construction: ActorGuardian, GenerationScheduler,
#   EffectLane (plus every subclass found in the tree and any subscripted form)
#   and the port objects create_engine, create_actor_host and ActorHost of
#   hyprial.kernel.impl.actor_runtime.port.  P8 removed ActorRuntime, the
#   compatibility facade, from this list along with the module.  ActorEngine
#   (a Protocol type) is not held.
# - Holding means a runtime import of a held name from a hyprial module
#   (absolute or relative), a call/construction of one, a subclass, a
#   getattr by constant name, a hyprial-module attribute reference, a plain
#   import of the engine package, or a local definition shadowing a held name
#   (the old PAC Protocol ActorRuntime, now PacWorkerRuntime).  Imports under
#   ``if TYPE_CHECKING:`` cannot construct and do not count; names bound from
#   non-hyprial modules (a third-party create_engine) do not count.
# - kernel/__init__.py and kernel/interface.py are ENGINE_REEXPORT_SURFACES:
#   their imports are the published kernel face and are allowed, but a
#   construction, subclass or shadow there is still a holding.
# - B1 (2026-11-11): ten holders migrated, 44 files / 71 names -> 34 / 54.
#   The two process holders remain blocked on the B5 actor construction seam.
# - P7 (2026-11-13): the last five holders (routine service/coordinator, PAC
#   graph authority, carrier/turn runtimes) take cells from an injected host.
# - P8 (2026-11-15): the holder list is empty, so its exemptions manifest and
#   the shrinking-only ceiling/compatibility machinery were deleted.  A holder
#   outside the engine region and the composition roots is a violation, full
#   stop.
"""Static actor/Pykka boundary checks used by the contract tests.

This helper deliberately parses Python instead of grepping it.  In particular,
``concurrent.futures.Future`` is not a Pykka capability and comments mentioning
``ask`` are not calls.  P8 removed the pykka backend, so any pykka import or raw
capability in src/hyprial is a violation.
"""

from __future__ import annotations

import ast
import functools
import json
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# (PY-BATCH-2026-10-01 foundation: the package moved to kernel/impl/actor_runtime;
# P8 removed its pykka backend, authority semantics unchanged.)
PYKKA_MODULE = "pykka"

# Blocking .ask() is forbidden in actor-domain code.  Only the ActorEngine
# port implementations are the sanctioned blocking site: the two P2c
# engine-port files and the P6 ABI adapter, which implements the same port
# over a reference or native ABI v1 module.  The allowlist is an exact path
# list, never a region or backend-package exemption: sibling backend modules
# stay rejected.  A symlink never earns the exemption: an allowlisted link
# could otherwise point outside the allowlist and dodge the rule.
ASK_EXEMPTION = frozenset(
    {
        Path("kernel/impl/actor_runtime/port/cell.py"),
        Path("kernel/impl/actor_runtime/port/engine.py"),
        Path("kernel/impl/actor_runtime/backends/abi.py"),
    }
)

_ACTOR_CAPABILITIES = frozenset(
    {"ActorRef", "ActorProxy", "Future", "ThreadingActor"}
)
_RAW_MAILBOX_CAPABILITIES = frozenset({"ActorInbox"})
_FORBIDDEN_CAPABILITIES = _ACTOR_CAPABILITIES | _RAW_MAILBOX_CAPABILITIES
_RAW_MAILBOX_ATTRIBUTES = frozenset({"actor_inbox", "_actor_inbox"})

# These suffixes discover the old ownership entry points independently of the
# exemption file.  That makes the manifest exact: a new legacy owner is an
# unmanifested regression, while a removed owner leaves a stale entry that must
# be deleted.  The allowed direction is therefore mechanically shrinking-only.
# (PY-BATCH-2026-10-01 foundation: keys follow the five-module layout; the
# legacy-entry-point semantics per file are unchanged.)
_LEGACY_DOMAIN_FILES: dict[Path, tuple[str, ...]] = {
    Path("daemon/impl/adapters/lark/runtime/runtime.py"): ("Supervisor",),
    Path("identity/impl/agents/registry/_core.py"): ("Registry",),
    Path("daemon/impl/application/__init__.py"): ("Application",),
    Path("daemon/impl/runtime/bridge.py"): ("Runtime",),
    Path("daemon/impl/inbox/service/__init__.py"): ("Service",),
    Path("biz/impl/routine/execution/service.py"): ("Service",),
    Path("biz/impl/workflow/service.py"): ("Service",),
}


@dataclass(frozen=True, order=True)
class Violation:
    """One source-level actor-boundary violation."""

    rule: str
    path: str
    line: int
    detail: str

    def render(self) -> str:
        return f"{self.rule}: {self.path}:{self.line}: {self.detail}"


def _module_name(node: ast.ImportFrom) -> str:
    return node.module or ""


def _is_pykka_module(module: str) -> bool:
    return module == PYKKA_MODULE or module.startswith(f"{PYKKA_MODULE}.")


def _path_label(path: Path, src_root: Path) -> str:
    return str(Path("src/hyprial") / path.relative_to(src_root))


def _attribute_parts(node: ast.AST) -> tuple[str, ...] | None:
    parts: list[str] = []
    current = node
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if not isinstance(current, ast.Name):
        return None
    parts.append(current.id)
    return tuple(reversed(parts))


def _string_annotation(annotation: ast.Constant) -> ast.AST | None:
    if not isinstance(annotation.value, str):
        return None
    try:
        return ast.parse(annotation.value, mode="eval").body
    except SyntaxError:
        return None


def _annotation_leaks_pykka(
    annotation: ast.AST | None,
    *,
    pykka_modules: set[str],
    pykka_types: set[str],
) -> bool:
    if annotation is None:
        return False
    if isinstance(annotation, ast.Constant):
        parsed = _string_annotation(annotation)
        if parsed is not None:
            return _annotation_leaks_pykka(
                parsed,
                pykka_modules=pykka_modules,
                pykka_types=pykka_types,
            )
    for child in ast.walk(annotation):
        if isinstance(child, ast.Name) and child.id in pykka_types:
            return True
        parts = _attribute_parts(child)
        if (
            parts is not None
            and len(parts) >= 2
            and parts[-1] in _FORBIDDEN_CAPABILITIES
            and parts[0] in pykka_modules
        ):
            return True
    return False


def _public_function(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    return not node.name.startswith("_")


def _function_annotations(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
) -> Iterable[ast.AST | None]:
    yield node.returns
    for argument in (
        *node.args.posonlyargs,
        *node.args.args,
        *node.args.kwonlyargs,
    ):
        yield argument.annotation
    if node.args.vararg is not None:
        yield node.args.vararg.annotation
    if node.args.kwarg is not None:
        yield node.args.kwarg.annotation


def _blocking_ask(call: ast.Call) -> bool:
    if not isinstance(call.func, ast.Attribute) or call.func.attr != "ask":
        return False
    for keyword in call.keywords:
        if keyword.arg != "block":
            continue
        return not (
            isinstance(keyword.value, ast.Constant) and keyword.value.value is False
        )
    return True


def _literal_all(node: ast.Assign | ast.AnnAssign) -> tuple[str, ...]:
    value = node.value
    if not isinstance(value, (ast.List, ast.Tuple, ast.Set)):
        return ()
    return tuple(
        item.value
        for item in value.elts
        if isinstance(item, ast.Constant) and isinstance(item.value, str)
    )


def _assignment_is_all(node: ast.Assign | ast.AnnAssign) -> bool:
    if isinstance(node, ast.AnnAssign):
        return isinstance(node.target, ast.Name) and node.target.id == "__all__"
    return any(isinstance(target, ast.Name) and target.id == "__all__" for target in node.targets)


def _public_assignment_targets(node: ast.Assign | ast.AnnAssign) -> tuple[str, ...]:
    targets: Iterable[ast.expr]
    if isinstance(node, ast.AnnAssign):
        targets = (node.target,)
    else:
        targets = node.targets
    return tuple(
        target.id
        for target in targets
        if isinstance(target, ast.Name) and not target.id.startswith("_")
    )


def _public_api_nodes(
    tree: ast.Module,
) -> Iterable[ast.FunctionDef | ast.AsyncFunctionDef | ast.AnnAssign]:
    """Yield annotations that are reachable through normal public Python APIs.

    A public-looking method on a private implementation class is not a public
    ``hyprial`` API.  Module callables/variables and methods/variables on public
    classes are.  Nested function locals are never part of that surface.
    """

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if _public_function(node):
                yield node
        elif isinstance(node, ast.AnnAssign):
            if _public_assignment_targets(node):
                yield node
        elif isinstance(node, ast.ClassDef) and not node.name.startswith("_"):
            for member in node.body:
                if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if _public_function(member):
                        yield member
                elif isinstance(member, ast.AnnAssign):
                    if _public_assignment_targets(member):
                        yield member


def scan_actor_boundaries(src_root: Path) -> list[Violation]:
    """Return all actor/Pykka boundary violations below ``src/hyprial``.

    P8 removed the pykka backend, so no file may import pykka or use a raw
    Pykka capability; every engine holding outside the engine region and the
    composition roots is an ``engine-holding`` violation.
    """

    violations: list[Violation] = engine_holding_violations(src_root)
    for path in sorted(src_root.rglob("*.py")):
        relative = path.relative_to(src_root)
        label = _path_label(path, src_root)
        ask_exempt = relative in ASK_EXEMPTION and not path.is_symlink()
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))

        pykka_modules: set[str] = set()
        pykka_types: set[str] = set()

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if _is_pykka_module(alias.name):
                        pykka_modules.add(alias.asname or alias.name.split(".")[0])
                        violations.append(
                            Violation(
                                "pykka-import",
                                label,
                                node.lineno,
                                f"direct import {alias.name!r} is forbidden",
                            )
                        )
            elif isinstance(node, ast.ImportFrom):
                module = _module_name(node)
                if _is_pykka_module(module):
                    for alias in node.names:
                        if alias.name in _FORBIDDEN_CAPABILITIES:
                            pykka_types.add(alias.asname or alias.name)
                    violations.append(
                        Violation(
                            "pykka-import",
                            label,
                            node.lineno,
                            f"direct import from {module!r} is forbidden",
                        )
                    )

        def leaks(annotation: ast.AST | None) -> bool:
            return _annotation_leaks_pykka(
                annotation,
                pykka_modules=pykka_modules,
                pykka_types=pykka_types,
            )

        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                if _blocking_ask(node) and not ask_exempt:
                    violations.append(
                        Violation(
                            "blocking-ask",
                            label,
                            node.lineno,
                            "blocking .ask() is forbidden in actor-domain code",
                        )
                    )
                if isinstance(node.func, ast.Attribute) and node.func.attr == "proxy":
                    violations.append(
                        Violation(
                            "domain-capability",
                            label,
                            node.lineno,
                            "raw Pykka .proxy() access is forbidden",
                        )
                    )
            if isinstance(node, ast.Attribute) and node.attr in _RAW_MAILBOX_ATTRIBUTES:
                violations.append(
                    Violation(
                        "domain-capability",
                        label,
                        node.lineno,
                        f"raw Pykka mailbox attribute {node.attr!r} is forbidden",
                    )
                )
        for node in _public_api_nodes(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if any(map(leaks, _function_annotations(node))):
                    violations.append(
                        Violation(
                            "public-api-leak",
                            label,
                            node.lineno,
                            f"public callable {node.name!r} exposes a Pykka type",
                        )
                    )
            elif leaks(node.annotation):
                targets = _public_assignment_targets(node)
                violations.append(
                    Violation(
                        "public-api-leak",
                        label,
                        node.lineno,
                        f"public annotation for {', '.join(targets)} exposes a Pykka type",
                    )
                )

        for node in tree.body:
            if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                continue
            if _assignment_is_all(node):
                leaked = sorted(set(_literal_all(node)) & pykka_types)
                if leaked:
                    violations.append(
                        Violation(
                            "public-api-leak",
                            label,
                            node.lineno,
                            f"__all__ exports Pykka capability names: {', '.join(leaked)}",
                        )
                    )
            value = node.value
            parts = _attribute_parts(value) if value is not None else None
            if (
                _public_assignment_targets(node)
                and parts is not None
                and len(parts) >= 2
                and parts[-1] in _FORBIDDEN_CAPABILITIES
                and parts[0] in pykka_modules
            ):
                violations.append(
                    Violation(
                        "domain-capability",
                        label,
                        node.lineno,
                        "domain exports a raw Pykka capability",
                    )
                )

    return sorted(set(violations))


def _legacy_symbols(src_root: Path) -> set[tuple[str, str]]:
    symbols: set[tuple[str, str]] = set()
    for relative, suffixes in _LEGACY_DOMAIN_FILES.items():
        path = src_root / relative
        if not path.exists():
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in tree.body:
            if (
                isinstance(node, ast.ClassDef)
                and not node.name.startswith("_")
                and node.name.endswith(suffixes)
            ):
                symbols.add((str(Path("src/hyprial") / relative), node.name))
    return symbols


def validate_legacy_manifest(src_root: Path, manifest_path: Path) -> list[str]:
    """Validate exact, shrinking-only legacy ownership exemptions."""

    raw: Any = json.loads(manifest_path.read_text(encoding="utf-8"))
    errors: list[str] = []
    if not isinstance(raw, dict) or raw.get("schema_version") != 1:
        return ["manifest schema_version must be exactly 1"]
    if raw.get("policy") != "shrinking-only":
        errors.append("manifest policy must be exactly 'shrinking-only'")

    entries = raw.get("exemptions")
    if not isinstance(entries, list):
        return errors + ["manifest exemptions must be a list"]

    manifested: set[tuple[str, str]] = set()
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            errors.append(f"exemptions[{index}] must be an object")
            continue
        path = entry.get("path")
        symbol = entry.get("symbol")
        reason = entry.get("reason")
        if not all(isinstance(value, str) and value.strip() for value in (path, symbol, reason)):
            errors.append(f"exemptions[{index}] needs non-empty path, symbol, and reason")
            continue
        key = (path, symbol)
        if key in manifested:
            errors.append(f"duplicate manifest exemption: {path}:{symbol}")
        manifested.add(key)

    actual = _legacy_symbols(src_root)
    for relative, suffixes in _LEGACY_DOMAIN_FILES.items():
        path = src_root / relative
        if not path.is_file():
            continue
        tree = ast.parse(path.read_text(encoding='utf-8'))
        counts = Counter(node.name for node in tree.body if isinstance(node, ast.ClassDef)
                         and not node.name.startswith('_') and node.name.endswith(suffixes))
        for symbol, count in counts.items():
            if count != 1:
                errors.append(f'non-unique legacy authority definition: src/hyprial/{relative}:{symbol} ({count})')
    for path, symbol in sorted(actual - manifested):
        errors.append(f"unmanifested legacy cutover symbol: {path}:{symbol}")
    for path, symbol in sorted(manifested - actual):
        errors.append(
            f"stale legacy exemption must shrink now: {path}:{symbol}"
        )
    return errors


ENGINE_HOLDING_RULE = "engine-holding"
ENGINE_REGION = Path("kernel/impl/actor_runtime")
ENGINE_PACKAGE = "hyprial.kernel.impl.actor_runtime"
ENGINE_PORT_MODULE = "hyprial.kernel.impl.actor_runtime.port"
ENGINE_PORT_NAMES = frozenset({"create_engine", "create_actor_host", "ActorHost"})
ENGINE_HELD_NAMES = (
    frozenset({"ActorGuardian", "GenerationScheduler", "EffectLane"})
    | ENGINE_PORT_NAMES
)
ENGINE_REEXPORT_SURFACES = frozenset({Path("kernel/__init__.py"), Path("kernel/interface.py")})
ENGINE_COMPOSITION_ROOTS: frozenset[Path] = frozenset(
    {Path("daemon/impl/composition/actors/__init__.py")}
)


@dataclass(frozen=True, order=True)
class EngineHolding:
    """One place where a module outside the engine region holds the engine."""

    path: str
    line: int
    name: str
    how: str

    def render(self) -> str:
        return f"{ENGINE_HOLDING_RULE}: {self.path}:{self.line}: {self.how} {self.name}"


def _callee(node: ast.AST) -> tuple[str | None, ast.AST | None]:
    """Return the terminal name of a callee or base and the object it hangs off."""

    while isinstance(node, ast.Subscript):
        node = node.value
    if isinstance(node, ast.Name):
        return node.id, None
    if isinstance(node, ast.Attribute):
        return node.attr, node.value
    return None, None


def _root_name(node: ast.AST | None) -> str | None:
    while isinstance(node, (ast.Attribute, ast.Subscript, ast.Call)):
        node = node.func if isinstance(node, ast.Call) else node.value
    return node.id if isinstance(node, ast.Name) else None


def _is_type_checking_test(test: ast.expr) -> bool:
    name, _ = _callee(test)
    return name == "TYPE_CHECKING"


def _type_checking_only(tree: ast.Module) -> set[int]:
    """Ids of nodes that only execute under ``if TYPE_CHECKING:``."""

    guarded: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.If) and _is_type_checking_test(node.test):
            for statement in node.body:
                guarded.update(id(child) for child in ast.walk(statement))
    return guarded


def _module_of(relative: Path) -> list[str]:
    parts = ["hyprial", *relative.with_suffix("").parts]
    if parts[-1] == "__init__":
        parts.pop()
    return parts


def _absolute_from(node: ast.ImportFrom, relative: Path) -> str | None:
    """Resolve an ``ImportFrom`` module to an absolute dotted name, or ``None`` if foreign."""

    if node.level == 0:
        module = node.module or ""
        if module == "hyprial" or module.startswith("hyprial."):
            return module
        return None
    package = _module_of(relative)
    if relative.name != "__init__.py":
        package = package[:-1]
    if node.level > 1:
        package = package[: len(package) - (node.level - 1)]
    return ".".join([*package, *([node.module] if node.module else [])])


def _in_engine_package(module: str) -> bool:
    return module == ENGINE_PACKAGE or module.startswith(f"{ENGINE_PACKAGE}.")


@dataclass(frozen=True)
class _Source:
    relative: Path
    label: str
    text: str

    def mentions(self, names: Iterable[str]) -> bool:
        return "actor_runtime" in self.text or any(name in self.text for name in names)

    def tree(self) -> ast.Module:
        return _parse_cached(self.label, self.text)


@functools.lru_cache(maxsize=4096)
def _parse_cached(label: str, text: str) -> ast.Module:
    return ast.parse(text, filename=label)


def _read_sources(src_root: Path) -> list[_Source]:
    return [
        _Source(path.relative_to(src_root), _path_label(path, src_root), path.read_text(encoding="utf-8"))
        for path in sorted(src_root.rglob("*.py"))
    ]


def engine_held_names(sources: Iterable[_Source]) -> frozenset[str]:
    """The engine names plus, transitively, every class that subclasses one of them.

    Every holding form spells a held name or the engine package in the source
    text, so only files that mention one are parsed.
    """

    pending = list(sources)
    names = set(ENGINE_HELD_NAMES)
    grew = True
    while grew:
        grew = False
        for source in pending:
            if not source.mentions(names):
                continue
            for node in ast.walk(source.tree()):
                if not isinstance(node, ast.ClassDef) or node.name in names:
                    continue
                if any(_callee(base)[0] in names for base in node.bases):
                    names.add(node.name)
                    grew = True
    return frozenset(names)


def _file_engine_holdings(
    relative: Path,
    label: str,
    tree: ast.Module,
    names: frozenset[str],
    *,
    reexport_surface: bool,
) -> set[EngineHolding]:
    guarded = _type_checking_only(tree)
    foreign: set[str] = set()
    hyprial_modules: set[str] = set()
    holdings: set[EngineHolding] = set()

    def hold(node: ast.AST, name: str, how: str) -> None:
        holdings.add(EngineHolding(label, getattr(node, "lineno", 0), name, how))

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                bound = alias.asname or alias.name.split(".")[0]
                if alias.name == "hyprial" or alias.name.startswith("hyprial."):
                    hyprial_modules.add(bound)
                    if _in_engine_package(alias.name) and not reexport_surface and id(node) not in guarded:
                        hold(node, alias.name, "import")
                else:
                    foreign.add(bound)
        elif isinstance(node, ast.ImportFrom):
            module = _absolute_from(node, relative)
            if module is None:
                foreign.update(alias.asname or alias.name for alias in node.names)
                continue
            hyprial_modules.update(
                alias.asname or alias.name for alias in node.names if alias.name not in names
            )
            if reexport_surface or id(node) in guarded:
                continue
            for alias in node.names:
                if alias.name == "*" and _in_engine_package(module):
                    hold(node, f"{module}.*", "import")
                elif alias.name in names:
                    hold(node, alias.name, "import")
                elif not _in_engine_package(module) and _in_engine_package(f"{module}.{alias.name}"):
                    hold(node, f"{module}.{alias.name}", "import")

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name, base = _callee(node.func)
            if name in names:
                if base is None and name in foreign:
                    continue
                if base is not None and _root_name(base) in foreign:
                    continue
                hold(node, name, "construct")
            elif (
                name == "getattr"
                and base is None
                and len(node.args) >= 2
                and isinstance(node.args[1], ast.Constant)
                and node.args[1].value in names
            ):
                hold(node, str(node.args[1].value), "getattr")
        elif isinstance(node, ast.ClassDef):
            held_bases = [base for base in map(lambda item: _callee(item)[0], node.bases) if base in names]
            if node.name in names:
                hold(node, node.name, "subclass" if held_bases else "shadow")
            for base_name in held_bases:
                hold(node, base_name, "subclass")
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name in names:
                hold(node, node.name, "shadow")
        elif (
            isinstance(node, ast.Attribute)
            and node.attr in names
            and _root_name(node.value) in hyprial_modules
        ):
            hold(node, node.attr, "reference")
    return holdings


def scan_engine_holdings(
    src_root: Path,
    *,
    composition_roots: frozenset[Path] = ENGINE_COMPOSITION_ROOTS,
    reexport_surfaces: frozenset[Path] = ENGINE_REEXPORT_SURFACES,
) -> list[EngineHolding]:
    """Every engine holding outside the engine region and the composition roots."""

    sources = _read_sources(src_root)
    names = engine_held_names(sources)
    holdings: set[EngineHolding] = set()
    for source in sources:
        if source.relative.is_relative_to(ENGINE_REGION) or source.relative in composition_roots:
            continue
        if not source.mentions(names):
            continue
        holdings |= _file_engine_holdings(
            source.relative,
            source.label,
            source.tree(),
            names,
            reexport_surface=source.relative in reexport_surfaces,
        )
    return sorted(holdings)


def engine_holders(
    src_root: Path,
    *,
    composition_roots: frozenset[Path] = ENGINE_COMPOSITION_ROOTS,
    reexport_surfaces: frozenset[Path] = ENGINE_REEXPORT_SURFACES,
) -> dict[str, frozenset[str]]:
    """Map each holder file label to the engine names it holds."""

    holders: dict[str, set[str]] = {}
    for holding in scan_engine_holdings(
        src_root,
        composition_roots=composition_roots,
        reexport_surfaces=reexport_surfaces,
    ):
        holders.setdefault(holding.path, set()).add(holding.name)
    return {path: frozenset(held) for path, held in holders.items()}


def engine_holding_violations(
    src_root: Path,
    *,
    composition_roots: frozenset[Path] = ENGINE_COMPOSITION_ROOTS,
    reexport_surfaces: frozenset[Path] = ENGINE_REEXPORT_SURFACES,
) -> list[Violation]:
    """``engine-holding`` violations: every holding outside the allowlists."""

    return sorted(
        {
            Violation(ENGINE_HOLDING_RULE, holding.path, holding.line, f"{holding.how} {holding.name}")
            for holding in scan_engine_holdings(
                src_root,
                composition_roots=composition_roots,
                reexport_surfaces=reexport_surfaces,
            )
        }
    )


def validate_engine_allowlists(
    src_root: Path,
    *,
    composition_roots: frozenset[Path] = ENGINE_COMPOSITION_ROOTS,
    reexport_surfaces: frozenset[Path] = ENGINE_REEXPORT_SURFACES,
) -> list[str]:
    """Each allowlisted composition root and re-export surface must exist."""

    errors = []
    for kind, paths in (("composition root", composition_roots), ("re-export surface", reexport_surfaces)):
        for relative in sorted(paths):
            if relative.is_relative_to(ENGINE_REGION):
                errors.append(f"engine {kind} {relative} is inside the engine region already")
            elif not (src_root / relative).is_file():
                errors.append(f"stale engine {kind} allowlist entry: src/hyprial/{relative}")
    return errors
