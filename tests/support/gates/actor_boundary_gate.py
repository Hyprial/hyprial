"""Static actor/Pykka boundary checks used by the contract tests.

This helper deliberately parses Python instead of grepping it.  In particular,
``concurrent.futures.Future`` is not a Pykka capability and comments mentioning
``ask`` are not calls.  The only backend allowed to depend on Pykka directly is
``hyprial.kernel.impl.actor_runtime.backends.pykka``.
"""

from __future__ import annotations

import ast
import json
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# (PY-BATCH-2026-10-01 foundation: the package moved to kernel/impl/actor_runtime;
# the sanctioned backend site follows the package, authority semantics unchanged.)
PYKKA_BACKEND = Path("kernel/impl/actor_runtime/backends/pykka.py")
PYKKA_MODULE = "pykka"
BACKEND_MODULE = "hyprial.kernel.impl.actor_runtime.backends.pykka"

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


def _is_backend_module(module: str) -> bool:
    return module == BACKEND_MODULE or module.endswith(".pykka_backend")


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
    backend_modules: set[str],
    backend_types: set[str],
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
                backend_modules=backend_modules,
                backend_types=backend_types,
            )
    for child in ast.walk(annotation):
        if isinstance(child, ast.Name) and child.id in pykka_types | backend_types:
            return True
        parts = _attribute_parts(child)
        if (
            parts is not None
            and len(parts) >= 2
            and parts[-1] in _FORBIDDEN_CAPABILITIES
            and parts[0] in pykka_modules | backend_modules
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
    """Return all actor/Pykka boundary violations below ``src/hyprial``."""

    violations: list[Violation] = []
    for path in sorted(src_root.rglob("*.py")):
        relative = path.relative_to(src_root)
        label = _path_label(path, src_root)
        is_backend = relative == PYKKA_BACKEND
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))

        pykka_modules: set[str] = set()
        pykka_types: set[str] = set()
        backend_modules: set[str] = set()
        backend_types: set[str] = set()

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if _is_pykka_module(alias.name):
                        pykka_modules.add(alias.asname or alias.name.split(".")[0])
                        if not is_backend:
                            violations.append(
                                Violation(
                                    "pykka-import",
                                    label,
                                    node.lineno,
                                    f"direct import {alias.name!r} is backend-only",
                                )
                            )
                    if _is_backend_module(alias.name):
                        backend_modules.add(alias.asname or alias.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom):
                module = _module_name(node)
                if _is_pykka_module(module):
                    for alias in node.names:
                        if alias.name in _FORBIDDEN_CAPABILITIES:
                            pykka_types.add(alias.asname or alias.name)
                    if not is_backend:
                        violations.append(
                            Violation(
                                "pykka-import",
                                label,
                                node.lineno,
                                f"direct import from {module!r} is backend-only",
                            )
                        )
                elif _is_backend_module(module):
                    for alias in node.names:
                        if alias.name in _FORBIDDEN_CAPABILITIES:
                            backend_types.add(alias.asname or alias.name)
                            if not is_backend:
                                violations.append(
                                    Violation(
                                        "domain-capability",
                                        label,
                                        node.lineno,
                                        f"domain imports raw {alias.name} from the backend",
                                    )
                                )

        def leaks(annotation: ast.AST | None) -> bool:
            return _annotation_leaks_pykka(
                annotation,
                pykka_modules=pykka_modules,
                pykka_types=pykka_types,
                backend_modules=backend_modules,
                backend_types=backend_types,
            )

        for node in ast.walk(tree):
            if not is_backend and isinstance(node, ast.Call):
                if _blocking_ask(node):
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
                            "raw Pykka .proxy() access is forbidden in domain code",
                        )
                    )
            if (
                not is_backend
                and isinstance(node, ast.Attribute)
                and node.attr in _RAW_MAILBOX_ATTRIBUTES
            ):
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
                leaked = sorted(set(_literal_all(node)) & (pykka_types | backend_types))
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
                not is_backend
                and _public_assignment_targets(node)
                and parts is not None
                and len(parts) >= 2
                and parts[-1] in _FORBIDDEN_CAPABILITIES
                and parts[0] in pykka_modules | backend_modules
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
