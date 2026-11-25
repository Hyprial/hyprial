#!/usr/bin/env python3
"""Method-registration gate for the daemon's legacy IPC dispatch.

A declared ``MethodSpec`` may be added without touching the legacy dispatch
(``MethodRegistry`` serves it).  A registered name in an executable legacy
branch is also rejected: it is a shadow route outside the declaration catalog.
A NEW literal method branch elsewhere is different: it is reachable outside
the declaration catalog.  This gate scans
the daemon implementation for method comparisons, membership tests, prefix
matchers and ``*_METHODS`` tables, resolves imported/local string constants
(including literal concatenation), freezes the pre-registry set, and fails when
dispatch grows a name that is neither frozen nor declared.  The ledger may only
shrink as C migrates methods.
"""

from __future__ import annotations

import argparse
import ast
import importlib.util
import json
import re
import subprocess
import sys
from functools import lru_cache
from pathlib import Path
from typing import Any

DEFAULT_ROOT = Path(__file__).resolve().parents[2]
ROUTER = "src/hyprial/daemon/impl/ipc/router.py"
DISPATCH_ROOT = "src/hyprial/daemon/impl"
BASELINE_RELATIVE_PATH = "scripts/ci/legacy-method-baseline.json"
BASELINE_VERSION = 1
METHOD_NAME = re.compile(r"^[a-z][A-Za-z0-9]*(?:[._-][A-Za-z0-9]+)*$")
SINGLE_WORD_METHODS = frozenset({"ping", "shutdown", "down", "hosts", "ps", "targets"})


def _module_path(root: Path, module: str) -> Path | None:
    base = root / "src" / Path(*module.split("."))
    if base.with_suffix(".py").is_file():
        return base.with_suffix(".py")
    if (base / "__init__.py").is_file():
        return base / "__init__.py"
    return None


@lru_cache(maxsize=None)
def _module_literals(root_s: str, module: str) -> dict[str, list[str]]:
    """Module-level name -> literal string values (with one-step imports)."""

    root = Path(root_s)
    path = _module_path(root, module)
    if path is None:
        return {}
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, SyntaxError):
        return {}
    literals: dict[str, list[str]] = {}
    imports: dict[str, tuple[str, str]] = {}
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and not node.level and node.module:
            for alias in node.names:
                imports[alias.asname or alias.name] = (node.module, alias.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname:
                    imports[alias.asname] = (alias.name, "")
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Name):
                    values = _resolve_in(node.value, literals, imports, root, depth=0)
                    if values:
                        literals[target.id] = values
    return literals


def _resolve_in(
    node: ast.AST,
    literals: dict[str, list[str]],
    imports: dict[str, tuple[str, str]],
    root: Path,
    *,
    depth: int,
) -> list[str] | None:
    if depth > 8:
        return None
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _resolve_in(node.left, literals, imports, root, depth=depth + 1)
        right = _resolve_in(node.right, literals, imports, root, depth=depth + 1)
        if left is None or right is None:
            return None
        return [a + b for a in left for b in right]
    if isinstance(node, ast.Name):
        if node.id in literals:
            return list(literals[node.id])
        target = imports.get(node.id)
        if target is None:
            return None
        module, symbol = target
        if symbol == "":
            return []
        values = _module_literals(str(root), module)
        if symbol in values:
            return list(values[symbol])
        facade = _facade_target(root, module, symbol)
        if facade is not None:
            return list(_module_literals(str(root), facade[0]).get(facade[1], []))
        return None
    if isinstance(node, ast.Attribute):
        if isinstance(node.value, ast.Name):
            target = imports.get(node.value.id)
            if target is not None:
                module = target[0] if target[1] == "" else f"{target[0]}.{target[1]}"
                values = _module_literals(str(root), module)
                return list(values[node.attr]) if node.attr in values else None
        return None
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        resolved = [
            _resolve_in(item, literals, imports, root, depth=depth + 1)
            for item in node.elts
        ]
        if any(item is None for item in resolved):
            return None
        return [value for item in resolved for value in item]  # type: ignore[union-attr]
    if isinstance(node, ast.Dict):
        resolved = [
            _resolve_in(key, literals, imports, root, depth=depth + 1)
            for key in node.keys
        ]
        if any(item is None for item in resolved):
            return None
        return [value for item in resolved for value in item]  # type: ignore[union-attr]
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
        constructors = {"frozenset", "set", "tuple", "list", "dict"}
        if node.func.id not in constructors or len(node.args) > 1 or node.keywords:
            return None
        if not node.args:
            return []
        return _resolve_in(node.args[0], literals, imports, root, depth=depth + 1)
    return None


def _facade_target(root: Path, module: str, symbol: str) -> tuple[str, str] | None:
    """Resolve a daemon/identity/biz/kernel public-facade lazily mapped symbol."""

    parts = module.split(".")
    if len(parts) != 2 or parts[0] != "hyprial" or parts[1] not in {
        "kernel", "identity", "daemon", "biz",
    }:
        return None
    interface = root / "src" / parts[0] / parts[1] / "interface.py"
    if not interface.is_file():
        return None
    try:
        tree = ast.parse(interface.read_text(encoding="utf-8"), filename=str(interface))
    except (OSError, SyntaxError):
        return None
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if not any(
            isinstance(target, ast.Name) and target.id == "_FACADE_EXPORTS"
            for target in node.targets
        ):
            continue
        if not isinstance(node.value, ast.Dict):
            continue
        for key, value in zip(node.value.keys, node.value.values):
            if not isinstance(key, ast.Constant) or key.value != symbol:
                continue
            if (
                isinstance(value, (ast.Tuple, ast.List))
                and len(value.elts) == 2
                and all(isinstance(item, ast.Constant) for item in value.elts)
            ):
                return str(value.elts[0].value), str(value.elts[1].value)
    return None


def _resolve_relative(relative: str, module: str | None, level: int) -> str | None:
    if not level:
        return module
    package = relative.removeprefix("src/").removesuffix(".py").split("/")[:-1]
    try:
        return importlib.util.resolve_name("." * level + (module or ""), ".".join(package))
    except (ImportError, ValueError):
        return None


class _Resolver:
    def __init__(self, root: Path, relative: str, tree: ast.Module) -> None:
        self.root = root
        self.literals: dict[str, list[str]] = {}
        self.imports: dict[str, tuple[str, str]] = {}
        for node in tree.body:
            if isinstance(node, ast.ImportFrom):
                module = _resolve_relative(relative, node.module, node.level)
                if module is not None:
                    for alias in node.names:
                        self.imports[alias.asname or alias.name] = (module, alias.name)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.asname:
                        self.imports[alias.asname] = (alias.name, "")
            elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for target in targets:
                    if isinstance(target, ast.Name):
                        values = self.resolve(node.value)
                        if values is not None:
                            self.literals[target.id] = values

    def resolve(self, node: ast.AST) -> list[str]:
        return _resolve_in(
            node, self.literals, self.imports, self.root, depth=0
        )


REGISTRY_FILE = "src/hyprial/daemon/impl/request_catalog/registry.py"


def _reviewed_registry_comparison(relative: str, node: ast.AST) -> bool:
    """Only the real registry call sites are not literal method branches."""

    if isinstance(node, ast.Constant) and node.value is None:
        return True
    if relative == ROUTER and isinstance(node, ast.Name) and node.id == "METHOD_REGISTRY":
        return True
    return (
        relative == REGISTRY_FILE
        and isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "self"
        and node.attr in {"_bindings", "_params", "_results"}
    )


def _reviewed_registry_subscript(relative: str, node: ast.Subscript) -> bool:
    return (
        relative == REGISTRY_FILE
        and isinstance(node.value, ast.Attribute)
        and isinstance(node.value.value, ast.Name)
        and node.value.value.id == "self"
        and node.value.attr in {"_bindings", "_params", "_results"}
    )


def _mentions_method(node: ast.AST) -> bool:
    if isinstance(node, ast.Name) and node.id == "method":
        return True
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        return any(
            isinstance(item, ast.Name) and item.id == "method" for item in node.elts
        )
    return False


def _accept(value: str, *, router: bool) -> bool:
    if not METHOD_NAME.fullmatch(value):
        return False
    return router or "." in value or value in SINGLE_WORD_METHODS


def _enclosing_if(parents: dict[ast.AST, ast.AST], node: ast.AST) -> ast.If | None:
    current = node
    while current in parents:
        current = parents[current]
        if isinstance(current, ast.If):
            if any(item is node for item in ast.walk(current.test)):
                return current
            return None
    return None


def _is_registry_invoke(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "invoke"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "METHOD_REGISTRY"
    )


def _is_policy_value(node: ast.AST | None) -> bool:
    return isinstance(node, (ast.Name, ast.Compare, ast.BoolOp, ast.UnaryOp)) or (
        isinstance(node, ast.Constant) and isinstance(node.value, bool)
    )


def _branch_nodes(branch: ast.If) -> list[ast.AST]:
    nodes: list[ast.AST] = []

    def visit(item: ast.AST) -> None:
        if isinstance(
            item, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)
        ):
            return
        nodes.append(item)
        for child in ast.iter_child_nodes(item):
            visit(child)

    for item in branch.body:
        visit(item)
    return nodes


def _assigned_values(branch: ast.If) -> dict[str, ast.AST]:
    values: dict[str, ast.AST] = {}
    for item in _branch_nodes(branch):
        if isinstance(item, ast.Assign) and len(item.targets) == 1:
            target = item.targets[0]
            if isinstance(target, ast.Name):
                values[target.id] = item.value
        elif isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
            values[item.target.id] = item.value
    return values


def _return_value(value: ast.AST | None, assigned: dict[str, ast.AST]) -> ast.AST | None:
    seen: set[str] = set()
    while isinstance(value, ast.Name) and value.id in assigned and value.id not in seen:
        seen.add(value.id)
        value = assigned[value.id]
    return value


def _excludes_registry(node: ast.AST) -> bool:
    """Recognize a mandatory negative registry membership condition."""

    if isinstance(node, ast.BoolOp) and isinstance(node.op, ast.And):
        return any(_excludes_registry(value) for value in node.values)
    return (
        isinstance(node, ast.Compare)
        and isinstance(node.left, ast.Name)
        and node.left.id == "method"
        and len(node.ops) == 1
        and isinstance(node.ops[0], ast.NotIn)
        and len(node.comparators) == 1
        and isinstance(node.comparators[0], ast.Name)
        and node.comparators[0].id == "METHOD_REGISTRY"
    )


def _registry_dispatch_precedes(
    parents: dict[ast.AST, ast.AST], branch: ast.If
) -> bool:
    """Recognize a complete registry return in an earlier function-level branch."""

    parent = parents.get(branch)
    if not isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return False
    for statement in parent.body:
        if statement is branch:
            return False
        if not isinstance(statement, ast.If) or statement.orelse:
            continue
        test = statement.test
        if not (
            isinstance(test, ast.Compare)
            and isinstance(test.left, ast.Name)
            and test.left.id == "method"
            and len(test.ops) == 1
            and isinstance(test.ops[0], ast.In)
            and len(test.comparators) == 1
            and isinstance(test.comparators[0], ast.Name)
            and test.comparators[0].id == "METHOD_REGISTRY"
            and len(statement.body) == 1
            and isinstance(statement.body[0], ast.Return)
        ):
            continue
        value = statement.body[0].value
        if (
            _is_registry_invoke(value)
            and len(value.args) >= 2
            and isinstance(value.args[1], ast.Name)
            and value.args[1].id == "method"
        ):
            return True
    return False


def _branch_classification(
    parents: dict[ast.AST, ast.AST], node: ast.AST, *, router: bool
) -> str | None:
    if not router:
        return None
    branch = _enclosing_if(parents, node)
    if branch is None or _excludes_registry(branch.test):
        return None
    if _registry_dispatch_precedes(parents, branch):
        return None
    current = node
    while current is not branch.test:
        parent = parents.get(current)
        if parent is None:
            return None
        if isinstance(parent, ast.UnaryOp) and isinstance(parent.op, ast.Not):
            return None
        current = parent
    assigned = _assigned_values(branch)
    returns = [item for item in _branch_nodes(branch) if isinstance(item, ast.Return)]
    if not returns and not any(isinstance(item, ast.Raise) for item in _branch_nodes(branch)):
        return None
    unresolved = False
    for item in returns:
        value = _return_value(item.value, assigned)
        if _is_registry_invoke(value):
            continue
        if value is None:
            continue
        if _is_policy_value(value):
            if isinstance(value, ast.Name) and value.id not in assigned:
                unresolved = True
            continue
        return "shadow"
    if any(isinstance(item, ast.Raise) for item in _branch_nodes(branch)):
        return "shadow"
    return "unsupported" if unresolved else None


def _positive_comparison(node: ast.Compare) -> bool:
    return all(isinstance(operator, (ast.Eq, ast.In)) for operator in node.ops)


def _shadow_matches_registry(shadow: dict[str, str], registry: set[str]) -> bool:
    method = shadow.get("method")
    if method is not None:
        return method in registry
    prefix = shadow.get("prefix")
    return prefix is not None and any(name.startswith(prefix) for name in registry)


def collect(root: Path) -> dict[str, Any]:
    names: set[str] = set()
    prefixes: set[str] = set()
    unsupported: set[str] = set()
    shadows: list[dict[str, str]] = []
    for path in sorted((root / DISPATCH_ROOT).rglob("*.py")):
        relative = path.relative_to(root).as_posix()
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=relative)
        except (OSError, SyntaxError):
            unsupported.add(f"unreadable:{relative}")
            continue
        resolver = _Resolver(root, relative, tree)
        parents = {
            child: parent
            for parent in ast.walk(tree)
            for child in ast.iter_child_nodes(parent)
        }
        router = relative == ROUTER
        local_names: set[str] = set()
        local_prefixes: set[str] = set()
        for node in tree.body:
            if not isinstance(node, ast.Assign):
                continue
            if not any(
                isinstance(target, ast.Name) and "METHOD" in target.id.upper()
                for target in node.targets
            ):
                continue
            values = resolver.resolve(node.value)
            if values is None:
                unsupported.add(f"unresolved-method-table:{relative}")
            else:
                for value in values:
                    if _accept(value, router=router):
                        local_names.add(value)
        tables = {
            target.id
            for node in tree.body
            if isinstance(node, ast.Assign)
            for target in node.targets
            if isinstance(target, ast.Name) and "METHOD" in target.id.upper()
        }
        for node in ast.walk(tree):
            if isinstance(node, ast.Compare):
                operands = [node.left, *node.comparators]
                if any(_mentions_method(item) for item in operands):
                    for side in operands:
                        if isinstance(side, ast.Name) and side.id == "method":
                            continue
                        values = resolver.resolve(side)
                        if values is None:
                            if _reviewed_registry_comparison(relative, side):
                                values = []
                            else:
                                unsupported.add(f"unresolved-method-comparison:{relative}")
                                continue
                        for value in values:
                            if _accept(value, router=router):
                                local_names.add(value)
                                if _positive_comparison(node):
                                    classification = _branch_classification(
                                        parents, node, router=router
                                    )
                                    if classification == "shadow":
                                        shadows.append(
                                            {
                                                "method": value,
                                                "form": f"comparison:{relative}:{node.lineno}",
                                            }
                                        )
                                    elif classification == "unsupported":
                                        unsupported.add(
                                            f"unresolved-route-return:{relative}:{node.lineno}"
                                        )
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "startswith"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "method"
            ):
                arguments = resolver.resolve(node.args[0]) if node.args else None
                if arguments is None:
                    unsupported.add(f"unresolved-method-prefix:{relative}")
                else:
                    for value in arguments:
                        if value.endswith(".") and METHOD_NAME.fullmatch(value[:-1]):
                            local_prefixes.add(value)
                            classification = _branch_classification(
                                parents, node, router=router
                            )
                            if classification == "shadow":
                                shadows.append(
                                    {
                                        "prefix": value,
                                        "form": f"prefix:{relative}:{node.lineno}",
                                    }
                                )
                            elif classification == "unsupported":
                                unsupported.add(
                                    f"unresolved-route-return:{relative}:{node.lineno}"
                                )
            if not (router or local_names or local_prefixes):
                continue
            if (
                isinstance(node, ast.Subscript)
                and not isinstance(node.slice, ast.Slice)
                and (_mentions_method(node.value) or _mentions_method(node.slice))
            ):
                table_lookup = (
                    isinstance(node.value, ast.Name)
                    and node.value.id in tables
                    and _mentions_method(node.slice)
                )
                if not table_lookup and not _reviewed_registry_subscript(relative, node):
                    unsupported.add(f"subscript-dispatch:{relative}")
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in {"get", "__getitem__"}
                and any(_mentions_method(argument) for argument in node.args)
            ):
                table_lookup = (
                    isinstance(node.func.value, ast.Name)
                    and node.func.value.id in tables
                )
                if not table_lookup:
                    unsupported.add(f"mapping-dispatch:{relative}")
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "getattr"
                and len(node.args) >= 2
                and isinstance(node.args[1], ast.Name)
                and node.args[1].id == "method"
            ):
                unsupported.add(f"getattr-dispatch:{relative}")
            if isinstance(node, ast.Match) and _mentions_method(node.subject):
                unsupported.add(f"match-dispatch:{relative}")
        names.update(local_names)
        prefixes.update(local_prefixes)
    return {
        "methods": sorted(names),
        "prefixes": sorted(prefixes),
        "unsupported": sorted(unsupported),
        "shadows": shadows,
    }


def _read_baseline(path: Path) -> dict[str, list[str]]:
    document = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict) or document.get("version") != BASELINE_VERSION:
        raise ValueError("legacy method baseline version must be 1")
    methods = document.get("methods")
    prefixes = document.get("prefixes")
    if not isinstance(methods, list) or not all(isinstance(item, str) for item in methods):
        raise ValueError("legacy method baseline methods must be a string list")
    if not isinstance(prefixes, list) or not all(isinstance(item, str) for item in prefixes):
        raise ValueError("legacy method baseline prefixes must be a string list")
    return {"methods": methods, "prefixes": prefixes}


def load_git_baseline(root: Path, ref: str) -> dict[str, list[str]]:
    result = subprocess.run(
        ["git", "-C", str(root), "show", f"{ref}:{BASELINE_RELATIVE_PATH}"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise ValueError(
            f"cannot read {BASELINE_RELATIVE_PATH} from {ref!r}: {result.stderr.strip()}"
        )
    try:
        document = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise ValueError(f"base ledger from {ref!r} is not JSON: {error}") from error
    if not isinstance(document, dict) or document.get("version") != BASELINE_VERSION:
        raise ValueError("base ledger version must be 1")
    return {"methods": document.get("methods", []), "prefixes": document.get("prefixes", [])}


def check(
    root: Path,
    baseline_path: Path,
    *,
    registry_names: set[str] | None = None,
    base_ref: str | None = None,
) -> dict[str, Any]:
    current = collect(root)
    registry = registry_names or set()
    try:
        baseline = _read_baseline(baseline_path)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        return {"status": "FAIL", "violations": [{"kind": "invalid-baseline", "detail": str(error)}]}
    if base_ref is not None:
        try:
            base = load_git_baseline(root, base_ref)
        except ValueError as error:
            return {"status": "FAIL", "violations": [{"kind": "missing-base-ledger", "detail": str(error)}]}
        allowed_base = set(base["methods"]) | registry
        base_violations = [
            *(
                {"kind": "base-router-method-growth", "method": name}
                for name in current["methods"]
                if name not in allowed_base
            ),
            *(
                {"kind": "base-router-prefix-growth", "prefix": prefix}
                for prefix in current["prefixes"]
                if prefix not in base["prefixes"]
            ),
            *(
                {"kind": "base-ledger-growth", "method": name}
                for name in baseline["methods"]
                if name not in set(base["methods"])
            ),
        ]
        if base_violations:
            return {
                "status": "FAIL",
                "routerMethods": len(current["methods"]),
                "registryMethods": len(registry),
                "violations": base_violations,
            }
    allowed = set(baseline["methods"]) | registry
    violations: list[dict[str, Any]] = []
    for name in current["methods"]:
        if name not in allowed:
            violations.append({"kind": "unregistered-router-method", "method": name})
    registry_methods = set(registry)
    for shadow in current["shadows"]:
        if _shadow_matches_registry(shadow, registry_methods):
            violations.append({"kind": "registered-legacy-method-shadow", **shadow})
    for prefix in current["prefixes"]:
        if prefix not in baseline["prefixes"]:
            violations.append({"kind": "new-router-method-prefix", "prefix": prefix})
    for name in baseline["methods"]:
        if name not in current["methods"] and name not in registry:
            violations.append({"kind": "stale-router-method-entry", "method": name})
    for prefix in baseline["prefixes"]:
        if prefix not in current["prefixes"]:
            violations.append({"kind": "stale-router-prefix-entry", "prefix": prefix})
    for form in current["unsupported"]:
        violations.append({"kind": "unsupported-router-method-dispatch", "form": form})
    return {
        "status": "FAIL" if violations else "PASS",
        "routerMethods": len(current["methods"]),
        "registryMethods": len(registry),
        "violations": violations,
    }


def write_baseline(
    root: Path, baseline_path: Path, *, registry_names: set[str] | None = None
) -> dict[str, Any]:
    current = collect(root)
    registry = set(registry_names or ())
    if current["unsupported"]:
        return {
            "status": "FAIL",
            "violations": [
                {"kind": "unsupported-router-method-dispatch", "form": form}
                for form in current["unsupported"]
            ],
        }
    shadows = [
        {"kind": "registered-legacy-method-shadow", **shadow}
        for shadow in current["shadows"]
        if _shadow_matches_registry(shadow, registry)
    ]
    if shadows:
        return {"status": "FAIL", "violations": shadows}
    if baseline_path.exists():
        try:
            previous = _read_baseline(baseline_path)
        except (OSError, ValueError, json.JSONDecodeError) as error:
            return {"status": "FAIL", "violations": [{"kind": "invalid-baseline", "detail": str(error)}]}
        allowed = set(previous["methods"]) | set(registry_names or ())
        added = [name for name in current["methods"] if name not in allowed]
        new_prefixes = [item for item in current["prefixes"] if item not in previous["prefixes"]]
        if added or new_prefixes:
            return {
                "status": "FAIL",
                "violations": [
                    *({"kind": "unregistered-router-method", "method": name} for name in added),
                    *({"kind": "new-router-method-prefix", "prefix": item} for item in new_prefixes),
                ],
            }
    document = {
        "version": BASELINE_VERSION,
        "policy": (
            "Frozen literal method names/prefixes of the daemon IPC dispatch at "
            "A-03 (comparisons, membership, prefixes, *_METHODS tables, resolved "
            "string constants). New entries are declarations (MethodSpec) or "
            "rejected; this ledger may only shrink as families migrate."
        ),
        **current,
    }
    baseline_path.parent.mkdir(parents=True, exist_ok=True)
    baseline_path.write_text(
        json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return {
        "status": "PASS",
        "wrote": str(baseline_path),
        "methods": len(current["methods"]),
        "prefixes": len(current["prefixes"]),
    }


def _registry_names(root: Path) -> set[str]:
    sys.path.insert(0, str(root / "src"))
    from hyprial.daemon.impl.request_catalog.registry import METHOD_REGISTRY

    return set(METHOD_REGISTRY.names)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--baseline", type=Path, default=None)
    parser.add_argument("--write-baseline", action="store_true")
    parser.add_argument(
        "--base-ref",
        default=None,
        help="independent git ref whose ledger is the growth ceiling; missing means FAIL",
    )
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    root = args.root.resolve()
    baseline = args.baseline or root / BASELINE_RELATIVE_PATH
    registry = _registry_names(root)
    report = (
        write_baseline(root, baseline, registry_names=registry)
        if args.write_baseline
        else check(root, baseline, registry_names=registry, base_ref=args.base_ref)
    )
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(f"{report['status']} method registration")
        for violation in report.get("violations", []):
            print(json.dumps(violation, sort_keys=True))
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
