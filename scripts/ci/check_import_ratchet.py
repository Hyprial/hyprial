#!/usr/bin/env python3
"""Exact CLI→backend import ratchet for the A migration window.

The CLI may consume reviewed protocol vocabulary and other individually
reviewed exceptions from backend public faces, but it must not keep adding
business-helper imports.  This gate records one exact
entry per ``(CLI file, imported module, imported symbol)`` triple with an
occurrence count.  Entries may only leave or shrink; a new triple, a higher
count, a lower count not reflected in the ledger, or a backend→CLI reverse
import fails the run.

Coverage includes absolute/relative imports, aliases, function-local imports
and literal ``importlib.import_module`` / ``__import__`` calls.  Dynamic names
that cannot be resolved statically are not silently allowed by this script;
they remain review leads for the module-boundary gate.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import importlib.util
import json
import subprocess
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULT_ROOT = Path(__file__).resolve().parents[2]
CLI_ROOT = "src/hyprial/shell/impl/cli"
BACKEND_ROOTS = (
    "src/hyprial/kernel",
    "src/hyprial/identity",
    "src/hyprial/daemon",
    "src/hyprial/biz",
)
BACKEND_DOMAINS = frozenset({"kernel", "identity", "daemon", "biz"})
BASELINE_RELATIVE_PATH = "scripts/ci/import-ratchet-baseline.json"
BASELINE_VERSION = 1
EXCEPTIONS_RELATIVE_PATH = "scripts/ci/import-ratchet-exceptions.json"
EXCEPTIONS_VERSION = 1
# Reviewed digest of the exact bootstrap/offline/protocol/CLI input-output exception manifest.
# Updating the manifest without updating this anchor fails closed.
REVIEWED_EXCEPTION_DIGEST = "ab5af63c88a8b6f2040ff7881a0de3e880ee19d1a75f6f613da6f77dd0a7a010"
EXCEPTION_CATEGORIES = frozenset({"bootstrap", "offline-maintenance", "protocol", "cli-input-output"})
EXCEPTION_FIELDS = frozenset({"path", "module", "symbol", "count", "category", "condition"})
ENTRY_FIELDS = frozenset({"path", "module", "symbol", "count"})

#: The three public faces use ``importlib.import_module(module_path)`` over
#: their literal ``_FACADE_EXPORTS`` table (verified separately by the module
#: boundary gate).  These exact paths may have a non-literal dynamic call;
#: every other unresolved dynamic import is a hard violation.
REVIEWED_DYNAMIC_IMPORT_PATHS = frozenset(
    {
        "src/hyprial/biz/interface.py",
        "src/hyprial/daemon/interface.py",
        "src/hyprial/identity/interface.py",
    }
)


@dataclass(frozen=True, order=True)
class Edge:
    path: str
    module: str
    symbol: str | None

    def key(self) -> dict[str, Any]:
        return {"path": self.path, "module": self.module, "symbol": self.symbol}


def _package(relative: str) -> str:
    parts = relative.removeprefix("src/").removesuffix(".py").split("/")
    return ".".join(parts[:-1])


def _resolve(relative: str, module: str | None, level: int) -> str | None:
    if not level:
        return module
    try:
        return importlib.util.resolve_name("." * level + (module or ""), _package(relative))
    except (ImportError, ValueError):
        return None


def _backend_target(module: str | None, symbol: str | None) -> tuple[str, str | None] | None:
    if not module:
        return None
    if module == "hyprial" and symbol in BACKEND_DOMAINS:
        return f"hyprial.{symbol}", None
    parts = module.split(".")
    if len(parts) >= 2 and parts[0] == "hyprial" and parts[1] in BACKEND_DOMAINS:
        # Internal ``*.impl`` penetration is a backend dependency even when the
        # module-boundary gate already rejects the direct import.
        return module, symbol
    return None


def _dynamic_targets(tree: ast.AST) -> tuple[list[str], list[str]]:
    names: dict[str, str] = {"__import__": "__import__"}
    modules: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in {"importlib", "builtins", "runpy"}:
                    modules[alias.asname or alias.name] = alias.name
        elif isinstance(node, ast.ImportFrom) and node.module in {"importlib", "builtins", "runpy"}:
            for alias in node.names:
                if alias.name in {"import_module", "__import__", "run_module", "run_path"}:
                    names[alias.asname or alias.name] = alias.name
    targets: list[str] = []
    unresolved: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        callable_name = names.get(node.func.id) if isinstance(node.func, ast.Name) else None
        if isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name):
            owner = modules.get(node.func.value.id)
            if (owner == "importlib" and node.func.attr == "import_module") or (
                owner == "builtins" and node.func.attr == "__import__"
            ) or (owner == "runpy" and node.func.attr in {"run_module", "run_path"}):
                callable_name = node.func.attr
        if callable_name is None or not node.args:
            continue
        first = node.args[0]
        if not isinstance(first, ast.Constant) or not isinstance(first.value, str):
            unresolved.append("<non-literal>")
            continue
        value = first.value
        package = next(
            (
                keyword.value.value
                for keyword in node.keywords
                if keyword.arg == "package"
                and isinstance(keyword.value, ast.Constant)
                and isinstance(keyword.value.value, str)
            ),
            None,
        )
        if value.startswith("."):
            if package is None:
                unresolved.append(repr(value))
                continue
            try:
                value = importlib.util.resolve_name(value, package)
            except (ImportError, ValueError):
                unresolved.append(repr(value))
                continue
        targets.append(value)
    return targets, unresolved


def _scan_imports(
    relative: str, tree: ast.AST
) -> tuple[list[tuple[str, str | None]], list[str]]:
    found: list[tuple[str, str | None]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            module = _resolve(relative, node.module, node.level)
            for alias in node.names:
                found.append((module or "", "*" if alias.name == "*" else alias.name))
        elif isinstance(node, ast.Import):
            for alias in node.names:
                found.append((alias.name, None))
    targets, unresolved = _dynamic_targets(tree)
    for target in targets:
        found.append((target, None))
    return found, unresolved


def _is_type_checking(test: ast.AST) -> bool:
    return (isinstance(test, ast.Name) and test.id == "TYPE_CHECKING") or (
        isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"
    )


def _build_reexports(root: Path) -> dict[str, dict[str, tuple[str, str | None]]]:
    """Module-level shell re-exports resolved through arbitrary shell hops.

    Pass one records every module-level ``ImportFrom`` alias (shell or
    backend).  Pass two resolves an alias chain until it reaches a backend
    domain symbol; cycles terminate without a false negative.  A CLI import of
    such a local name is a backend dependency even though its ``ImportFrom``
    names a shell module.
    """

    raw: dict[str, dict[str, tuple[str, str | None]]] = {}
    for path in sorted((root / "src/hyprial/shell").rglob("*.py")):
        relative = path.relative_to(root).as_posix()
        package = _package(relative)
        module_name = package if path.name == "__init__.py" else f"{package}.{path.stem}"
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=relative)
        except (OSError, SyntaxError):
            continue
        entries = list(tree.body)
        for node in tree.body:
            if isinstance(node, ast.If) and _is_type_checking(node.test):
                entries.extend(node.body)
        names: dict[str, tuple[str, str | None]] = {}
        for node in entries:
            if isinstance(node, ast.ImportFrom):
                target_module = _resolve(relative, node.module, node.level)
                if not target_module:
                    continue
                for alias in node.names:
                    names[alias.asname or alias.name] = (target_module, alias.name)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.asname:
                        names[alias.asname] = (alias.name, None)
        if names:
            raw[module_name] = names

    def resolve(
        module: str,
        symbol: str | None,
        seen: frozenset[tuple[str, str | None]],
    ) -> tuple[str, str | None] | None:
        key = (module, symbol)
        if key in seen:
            return None
        direct = _backend_target(module, symbol)
        if direct is not None:
            return direct
        target = raw.get(module, {}).get(symbol or "")
        if target is None:
            return None
        return resolve(target[0], target[1], seen | {key})

    reexports: dict[str, dict[str, tuple[str, str | None]]] = {}
    for module_name, names in raw.items():
        resolved: dict[str, tuple[str, str | None]] = {}
        for local, (target_module, target_symbol) in names.items():
            backend = resolve(target_module, target_symbol, frozenset({(module_name, local)}))
            if backend is not None:
                resolved[local] = backend
        if resolved:
            reexports[module_name] = resolved
    return reexports

def scan(
    root: Path,
) -> tuple[Counter[Edge], list[dict[str, Any]], set[Edge], list[dict[str, Any]]]:
    """Count exact CLI→backend edges and collect reverse/dynamic failures."""

    counts: Counter[Edge] = Counter()
    reexported: set[Edge] = set()
    reexports = _build_reexports(root)
    reverse: list[dict[str, Any]] = []
    unresolved: list[dict[str, Any]] = []
    cli_root = root / CLI_ROOT
    for path in sorted(cli_root.rglob("*.py")):
        relative = path.relative_to(root).as_posix()
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=relative)
        except (OSError, SyntaxError) as error:
            reverse.append({"kind": "unreadable-cli-source", "path": relative, "detail": str(error)})
            continue
        imports, dynamic_unresolved = _scan_imports(relative, tree)
        if relative not in REVIEWED_DYNAMIC_IMPORT_PATHS:
            for value in dynamic_unresolved:
                unresolved.append(
                    {"kind": "unresolved-dynamic-import", "path": relative, "target": value}
                )
        for module, symbol in imports:
            target = _backend_target(module, symbol)
            if target is not None:
                counts[Edge(relative, target[0], target[1])] += 1
                continue
            exported = reexports.get(module, {}).get(symbol or "")
            if exported is not None:
                edge = Edge(relative, exported[0], exported[1])
                counts[edge] += 1
                reexported.add(edge)
    for base in BACKEND_ROOTS:
        for path in sorted((root / base).rglob("*.py")):
            relative = path.relative_to(root).as_posix()
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"), filename=relative)
            except (OSError, SyntaxError) as error:
                reverse.append({"kind": "unreadable-backend-source", "path": relative, "detail": str(error)})
                continue
            imports, dynamic_unresolved = _scan_imports(relative, tree)
            if relative not in REVIEWED_DYNAMIC_IMPORT_PATHS:
                for value in dynamic_unresolved:
                    unresolved.append(
                        {"kind": "unresolved-dynamic-import", "path": relative, "target": value}
                    )
            for module, symbol in imports:
                target = _resolve(relative, module, 0) if module else None
                if target == "hyprial" and symbol in {"shell", "cli"}:
                    reverse.append(
                        {
                            "kind": "reverse-backend-import",
                            "path": relative,
                            "module": f"hyprial.{symbol}",
                            "symbol": None,
                        }
                    )
                    continue
                if target and (target == "hyprial.shell" or target.startswith("hyprial.shell.")):
                    reverse.append(
                        {
                            "kind": "reverse-backend-import",
                            "path": relative,
                            "module": target,
                            "symbol": symbol,
                        }
                    )
                if target and target in {"hyprial.cli"}:
                    reverse.append(
                        {
                            "kind": "reverse-backend-import",
                            "path": relative,
                            "module": target,
                            "symbol": symbol,
                        }
                    )
    return counts, reverse, reexported, unresolved




def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_exceptions(path: Path) -> dict[Edge, int]:
    """Read the exact, reviewed exception manifest (empty when absent)."""

    if not path.exists():
        return {}
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"{path}: cannot read exception manifest: {error}") from error
    if not isinstance(document, dict) or document.get("version") != EXCEPTIONS_VERSION:
        raise ValueError(f"{path}: exception manifest version must be {EXCEPTIONS_VERSION}")
    raw_entries = document.get("entries")
    if not isinstance(raw_entries, list):
        raise ValueError(f"{path}: entries must be a JSON array")
    exceptions: dict[Edge, int] = {}
    for index, raw in enumerate(raw_entries):
        where = f"{path}: exception #{index}"
        if not isinstance(raw, dict) or set(raw) != EXCEPTION_FIELDS:
            raise ValueError(f"{where}: fields must be exactly {sorted(EXCEPTION_FIELDS)}")
        if raw["category"] not in EXCEPTION_CATEGORIES:
            raise ValueError(f"{where}: category must be one of {sorted(EXCEPTION_CATEGORIES)}")
        if not all(isinstance(raw[name], str) and raw[name] for name in ("path", "module", "condition")):
            raise ValueError(f"{where}: path/module/condition must be non-empty strings")
        symbol = raw["symbol"]
        if symbol is not None and (not isinstance(symbol, str) or not symbol):
            raise ValueError(f"{where}: symbol must be null or a non-empty string")
        count = raw["count"]
        if isinstance(count, bool) or not isinstance(count, int) or count <= 0:
            raise ValueError(f"{where}: count must be a positive integer")
        edge = Edge(raw["path"], raw["module"], symbol)
        if edge in exceptions:
            raise ValueError(f"{where}: duplicate exception edge {edge}")
        exceptions[edge] = count
    return exceptions


def _exception_problems(
    current: dict[Edge, int], exceptions: dict[Edge, int], baseline: dict[Edge, int]
) -> list[dict[str, Any]]:
    problems: list[dict[str, Any]] = []
    for edge, count in sorted(exceptions.items()):
        if edge in baseline:
            problems.append(
                {"kind": "exception-overlaps-baseline", **_entry(edge, count), "baseline": baseline[edge]}
            )
        observed = current.get(edge, 0)
        if observed == 0:
            problems.append({"kind": "exception-unused", **_entry(edge, count)})
        elif observed != count:
            problems.append(
                {"kind": "exception-count-mismatch", **_entry(edge, observed), "exception": count}
            )
    return problems


def _without_exceptions(
    current: dict[Edge, int], exceptions: dict[Edge, int], baseline: dict[Edge, int]
) -> dict[Edge, int]:
    return {
        edge: count
        for edge, count in current.items()
        if exceptions.get(edge) != count or edge in baseline
    }


def _entry(edge: Edge, count: int) -> dict[str, Any]:
    return {"path": edge.path, "module": edge.module, "symbol": edge.symbol, "count": count}


def _baseline_edges(document: Any) -> dict[Edge, int]:
    if not isinstance(document, dict) or document.get("version") != BASELINE_VERSION:
        raise ValueError("baseline version must be 1")
    raw = document.get("edges")
    if not isinstance(raw, list):
        raise ValueError("baseline edges must be a list")
    edges: dict[Edge, int] = {}
    for index, entry in enumerate(raw):
        if not isinstance(entry, dict) or set(entry) != ENTRY_FIELDS:
            raise ValueError(f"baseline entry #{index} has unexpected fields")
        path, module, symbol, count = (
            entry["path"],
            entry["module"],
            entry["symbol"],
            entry["count"],
        )
        if not isinstance(path, str) or not path or not isinstance(module, str) or not module:
            raise ValueError(f"baseline entry #{index} needs path and module strings")
        if symbol is not None and not isinstance(symbol, str):
            raise ValueError(f"baseline entry #{index}: symbol must be a string or null")
        if isinstance(count, bool) or not isinstance(count, int) or count < 1:
            raise ValueError(f"baseline entry #{index}: count must be a positive integer")
        edge = Edge(path, module, symbol)
        if edge in edges:
            raise ValueError(f"baseline entry #{index}: duplicate edge {edge}")
        edges[edge] = count
    return edges


def load_baseline(path: Path) -> dict[Edge, int]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise ValueError(f"missing baseline {path}") from error
    return _baseline_edges(document)


def load_git_baseline(root: Path, ref: str) -> dict[Edge, int]:
    """Read the ledger from an independent base ref; missing base fails closed."""

    result = subprocess.run(
        ["git", "-C", str(root), "show", f"{ref}:{BASELINE_RELATIVE_PATH}"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise ValueError(f"cannot read {BASELINE_RELATIVE_PATH} from {ref!r}: {result.stderr.strip()}")
    try:
        return _baseline_edges(json.loads(result.stdout))
    except json.JSONDecodeError as error:
        raise ValueError(f"base ledger from {ref!r} is not JSON: {error}") from error


def _base_growth(
    current: dict[Edge, int],
    baseline: dict[Edge, int],
    base: dict[Edge, int],
) -> list[dict[str, Any]]:
    violations: list[dict[str, Any]] = []
    for edge, count in sorted(current.items()):
        limit = base.get(edge)
        if limit is None:
            violations.append({"kind": "base-import-edge-growth", **_entry(edge, count)})
        elif count > limit:
            violations.append(
                {"kind": "base-import-edge-growth", **_entry(edge, count), "base": limit}
            )
    for edge, count in sorted(baseline.items()):
        limit = base.get(edge)
        if limit is None:
            violations.append({"kind": "ledger-growth", **_entry(edge, count)})
        elif count > limit:
            violations.append(
                {"kind": "ledger-growth", **_entry(edge, count), "base": limit}
            )
    return violations


def check(root: Path, baseline_path: Path, *, base_ref: str | None = None) -> dict[str, Any]:
    current, reverse, reexported, unresolved = scan(root)
    try:
        baseline = load_baseline(baseline_path)
    except ValueError as error:
        return {"status": "FAIL", "violations": [{"kind": "invalid-baseline", "detail": str(error)}]}
    exceptions_path = root / EXCEPTIONS_RELATIVE_PATH
    try:
        exceptions = load_exceptions(exceptions_path)
    except ValueError as error:
        return {"status": "FAIL", "violations": [{"kind": "invalid-exceptions", "detail": str(error)}]}
    if exceptions_path.exists() and REVIEWED_EXCEPTION_DIGEST:
        digest = _digest(exceptions_path)
        if digest != REVIEWED_EXCEPTION_DIGEST:
            return {
                "status": "FAIL",
                "violations": [
                    {
                        "kind": "exception-digest-mismatch",
                        "expected": REVIEWED_EXCEPTION_DIGEST,
                        "actual": digest,
                    }
                ],
            }
    violations: list[dict[str, Any]] = _exception_problems(dict(current), exceptions, baseline)
    effective = _without_exceptions(dict(current), exceptions, baseline)
    if base_ref is not None:
        try:
            base = load_git_baseline(root, base_ref)
        except ValueError as error:
            return {"status": "FAIL", "violations": [{"kind": "missing-base-ledger", "detail": str(error)}]}
        violations.extend(_base_growth(effective, baseline, base))
    for edge, count in sorted(effective.items()):
        previous = baseline.get(edge)
        if previous is None:
            kind = "cli-reexport-bypass" if edge in reexported else "new-cli-backend-import"
            violations.append({"kind": kind, **_entry(edge, count)})
        elif count > previous:
            violations.append(
                {
                    "kind": "cli-backend-import-grew",
                    **_entry(edge, count),
                    "baseline": previous,
                }
            )
        elif count < previous:
            violations.append(
                {
                    "kind": "stale-baseline-entry",
                    **_entry(edge, count),
                    "baseline": previous,
                }
            )
    for edge, previous in sorted(baseline.items()):
        if edge not in current:
            violations.append(
                {
                    "kind": "stale-baseline-entry",
                    **_entry(edge, 0),
                    "baseline": previous,
                }
            )
    violations.extend(reverse)
    violations.extend(unresolved)
    return {
        "status": "FAIL" if violations else "PASS",
        "cliEdges": sum(current.values()),
        "exceptionEdges": sum(exceptions.values()),
        "baselineEdges": sum(baseline.values()),
        "violations": violations,
    }


def write_baseline(root: Path, baseline_path: Path) -> dict[str, Any]:
    current, reverse, reexported, unresolved = scan(root)
    try:
        exceptions = load_exceptions(root / EXCEPTIONS_RELATIVE_PATH)
    except ValueError as error:
        return {"status": "FAIL", "violations": [{"kind": "invalid-exceptions", "detail": str(error)}]}
    try:
        previous = load_baseline(baseline_path) if baseline_path.exists() else {}
    except ValueError as error:
        return {"status": "FAIL", "violations": [{"kind": "invalid-baseline", "detail": str(error)}]}
    current = _without_exceptions(current, exceptions, previous)
    if unresolved:
        return {
            "status": "FAIL",
            "violations": [{"kind": "unresolved-dynamic-import-blocked", **row} for row in unresolved],
        }
    if reverse:
        return {
            "status": "FAIL",
            "violations": [{"kind": "reverse-backend-import-blocked", **row} for row in reverse],
        }
    if baseline_path.exists():
        try:
            previous = load_baseline(baseline_path)
        except ValueError as error:
            return {"status": "FAIL", "violations": [{"kind": "invalid-baseline", "detail": str(error)}]}
        violations = []
        for edge, count in sorted(current.items()):
            old = previous.get(edge)
            if old is None:
                kind = "cli-reexport-bypass" if edge in reexported else "new-cli-backend-import"
                violations.append({"kind": kind, **_entry(edge, count)})
            elif count > old:
                violations.append(
                    {"kind": "cli-backend-import-grew", **_entry(edge, count), "baseline": old}
                )
        if violations:
            return {"status": "FAIL", "violations": violations}
    document = {
        "version": BASELINE_VERSION,
        "policy": (
            "Exact CLI -> daemon/biz/identity import triples with occurrence "
            "counts. Entries may only leave or shrink; updating this file is a "
            "reviewed decrease, never a new permit."
        ),
        "edges": [_entry(edge, count) for edge, count in sorted(current.items())],
    }
    baseline_path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "{",
        f'  "version": {document["version"]},',
        f'  "policy": {json.dumps(document["policy"])},',
        '  "edges": [',
    ]
    for index, entry in enumerate(document["edges"]):
        comma = "," if index + 1 < len(document["edges"]) else ""
        lines.append("    " + json.dumps(entry, separators=(",", ":")) + comma)
    lines.extend(["  ]", "}"])
    baseline_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {
        "status": "PASS",
        "wrote": str(baseline_path),
        "edges": len(document["edges"]),
        "occurrences": sum(current.values()),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument(
        "--baseline",
        type=Path,
        default=None,
        help=f"baseline path (default: {BASELINE_RELATIVE_PATH})",
    )
    parser.add_argument(
        "--write-baseline",
        action="store_true",
        help="rewrite only after a verified shrink; new/grown edges are refused",
    )
    parser.add_argument(
        "--base-ref",
        default=None,
        help="independent git ref whose ledger is the growth ceiling; missing means FAIL",
    )
    parser.add_argument("--json", action="store_true", help="emit the full report")
    args = parser.parse_args(argv)
    root = args.root.resolve()
    baseline = args.baseline or root / BASELINE_RELATIVE_PATH
    if args.write_baseline:
        report = write_baseline(root, baseline)
    else:
        report = check(root, baseline, base_ref=args.base_ref)
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(f"{report['status']} import ratchet")
        for violation in report.get("violations", []):
            print(json.dumps(violation, sort_keys=True))
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
