from __future__ import annotations

import ast
import importlib.util
import re
import subprocess
from collections import Counter, defaultdict
from pathlib import Path

REPO_SCOPES = (
    "src/hyprial",
    "tests",
    "scripts",
    "contract",
    "app",
    "src/gui",
    "notes",
)

SRC_PREFIX = "src/hyprial/"

MAX_SNIPPET = 180


def resolve_from(
    node: ast.ImportFrom, current: str, package: str, modules: dict[str, str]
) -> tuple[str | None, str | None]:
    try:
        base = (
            importlib.util.resolve_name("." * node.level + (node.module or ""), package)
            if node.level
            else (node.module or "")
        )
    except (ImportError, ValueError):
        return None, None
    if node.module:
        return base, base if base in modules else None
    # `from . import child`: prefer a real package child module to a package symbol.
    candidates = [(base + "." + a.name) for a in node.names if a.name != "*"]
    found = next((candidate for candidate in candidates if candidate in modules), None)
    return base, found


def scope_name(node: ast.AST) -> str:
    p = node
    while p is not None:
        if isinstance(p, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            return "function-local"
        p = getattr(p, "_parent", None)
    return "module"


def annotate_parents(tree: ast.AST) -> None:
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            child._parent = parent


def type_checking_names(tree: ast.AST) -> tuple[set[str], set[str]]:
    names, modules = {"TYPE_CHECKING"}, {"typing"}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "typing":
            names.update(
                a.asname or a.name for a in node.names if a.name == "TYPE_CHECKING"
            )
        if isinstance(node, ast.Import):
            modules.update(a.asname or a.name for a in node.names if a.name == "typing")
    return names, modules


def in_type_checking(node: ast.AST, names: set[str], modules: set[str]) -> bool:
    p = node
    while p is not None:
        parent = getattr(p, "_parent", None)
        if isinstance(parent, ast.If) and p in parent.body:
            test = parent.test
            if (isinstance(test, ast.Name) and test.id in names) or (
                isinstance(test, ast.Attribute)
                and test.attr == "TYPE_CHECKING"
                and isinstance(test.value, ast.Name)
                and test.value.id in modules
            ):
                return True
        p = parent
    return False


def snippet(text: str) -> str:
    text = " ".join(text.split())
    return text[:MAX_SNIPPET]


def evidence_line(source: str, line: int, rows: list[str] | None = None) -> str:
    rows = rows if rows is not None else source.splitlines()
    return snippet(rows[line - 1]) if 0 < line <= len(rows) else ""


def git(root: Path, *args: str) -> str:
    return (
        subprocess.check_output(["git", "-C", str(root), *args], stderr=subprocess.PIPE)
        .decode("utf-8", "replace")
        .strip()
    )


def _package_for_unindexed(rel: str) -> str:
    parts = rel[:-3].split("/")
    if parts[-1] == "__init__":
        parts.pop()
    else:
        parts.pop()
    return ".".join(parts)


def resource_only_directories(files: list[str]) -> list[str]:
    directories = sorted(
        {p.rsplit("/", 1)[0] for p in files if p.startswith(SRC_PREFIX) and "/" in p}
    )
    result = []
    for directory in directories:
        descendants = [p for p in files if p.startswith(directory + "/")]
        python_impls = [
            p
            for p in descendants
            if p.endswith(".py") and Path(p).name != "__init__.py"
        ]
        if (
            descendants
            and any(not p.endswith(".py") for p in descendants)
            and not python_impls
        ):
            result.append(directory)
    return sorted(set(result))


def strongly_connected(graph: dict[str, set[str]]) -> list[list[str]]:
    index = 0
    stack = []
    onstack = set()
    indexes = {}
    low = {}
    result = []

    def visit(v: str) -> None:
        nonlocal index
        indexes[v] = low[v] = index
        index += 1
        stack.append(v)
        onstack.add(v)
        for w in sorted(graph.get(v, ())):
            if w not in indexes:
                visit(w)
                low[v] = min(low[v], low[w])
            elif w in onstack:
                low[v] = min(low[v], indexes[w])
        if low[v] == indexes[v]:
            comp = []
            while True:
                w = stack.pop()
                onstack.remove(w)
                comp.append(w)
                if w == v:
                    break
            result.append(sorted(comp))

    for v in sorted(graph):
        if v not in indexes:
            visit(v)
    return sorted(result)


def qualified_name(node: ast.AST) -> str:
    parts = []
    current = node
    while current is not None:
        if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            parts.append(current.name)
        current = getattr(current, "_parent", None)
    return ".".join(reversed(parts))


def dotted_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = dotted_name(node.value)
        return f"{prefix}.{node.attr}" if prefix else None
    return None


def scan_references(
    root: Path,
    files: list[str],
    py_records: dict,
    modules: dict,
    mapping_for_mod: dict,
    import_records: list,
) -> tuple[list, list, dict, dict]:
    refs, textrefs = [], []
    exports = {
        "status": "partial-static-observations",
        "all_declarations": [],
        "package_reexports": [],
        "ports_imports": [],
        "definitions": [],
        "caller_references": [],
    }
    extraction = {
        "status": "static-observations-only",
        "cli_registrations": [],
        "daemon_application_methods": [],
    }
    path_pattern = re.compile(
        r"(?:src/hyprial/)?(?:[A-Za-z0-9_./-]+\.py|docs/notes/[A-Za-z0-9_./-]+|docs/[A-Za-z0-9_./-]+\.md|notes/[A-Za-z0-9_./-]+)"
    )
    for rel, (source, tree) in py_records.items():
        source_lines = source.splitlines()
        module = next((mod for mod, path in modules.items() if path == rel), None)
        package = (
            module
            if rel.endswith("/__init__.py")
            else (module.rpartition(".")[0] if module else _package_for_unindexed(rel))
        )
        for n in ast.walk(tree):
            if isinstance(n, (ast.List, ast.Tuple)):
                for index, item in enumerate(n.elts[:-1]):
                    if isinstance(item, ast.Constant) and item.value == "-m":
                        target = n.elts[index + 1]
                        refs.append(
                            {
                                "kind": "argv-module-entry",
                                "path": rel,
                                "line": item.lineno,
                                "value": target.value
                                if isinstance(target, ast.Constant)
                                and isinstance(target.value, str)
                                else "<dynamic>",
                                "expression": snippet(ast.unparse(target)),
                            }
                        )
            if isinstance(n, ast.Constant) and isinstance(n.value, str):
                value = n.value
                if any(
                    term in value
                    for term in ("hyprial.", "src/hyprial", "docs/", "notes/")
                ) or value in {"-m", "importlib.resources"}:
                    refs.append(
                        {
                            "kind": "argv-module-flag"
                            if value == "-m"
                            else "string-constant",
                            "path": rel,
                            "line": n.lineno,
                            "value": snippet(value),
                        }
                    )
                if value.startswith(".") and package:
                    try:
                        candidate = importlib.util.resolve_name(value, package)
                    except (ImportError, ValueError):
                        candidate = None
                    if candidate in modules:
                        refs.append(
                            {
                                "kind": "relative-module-string",
                                "path": rel,
                                "line": n.lineno,
                                "value": value,
                                "resolved_target": candidate,
                            }
                        )
            if isinstance(n, ast.Call):
                fname = (
                    n.func.id
                    if isinstance(n.func, ast.Name)
                    else n.func.attr
                    if isinstance(n.func, ast.Attribute)
                    else ""
                )
                first = n.args[0] if n.args else None
                arg = (
                    first.value
                    if isinstance(first, ast.Constant) and isinstance(first.value, str)
                    else None
                )
                if fname in {"import_module", "__import__"}:
                    refs.append(
                        {
                            "kind": fname,
                            "path": rel,
                            "line": n.lineno,
                            "value": arg or "<dynamic>",
                            "relative_package": package
                            if arg and arg.startswith(".")
                            else None,
                        }
                    )
                if fname == "files":
                    refs.append(
                        {
                            "kind": "resources.files",
                            "path": rel,
                            "line": n.lineno,
                            "value": arg,
                        }
                    )
                if fname == "add_typer" or fname == "command" or fname == "callback":
                    if rel.endswith("cli.py") or "/cli_commands/" in rel:
                        extraction["cli_registrations"].append(
                            {
                                "path": rel,
                                "line": n.lineno,
                                "kind": fname,
                                "source": evidence_line(source, n.lineno, source_lines),
                            }
                        )
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                if rel.endswith("/__init__.py") and not rel.startswith(SRC_PREFIX):
                    exports["definitions"].append(
                        {
                            "path": rel,
                            "name": n.name,
                            "qualified_name": qualified_name(n),
                            "kind": type(n).__name__,
                            "line": n.lineno,
                        }
                    )
                if isinstance(
                    n, (ast.FunctionDef, ast.AsyncFunctionDef)
                ) and rel.startswith(SRC_PREFIX):
                    args = ast.unparse(n.args)
                    result_annotation = (
                        f" -> {ast.unparse(n.returns)}" if n.returns else ""
                    )
                    prefix = (
                        "async def" if isinstance(n, ast.AsyncFunctionDef) else "def"
                    )
                    exports["definitions"].append(
                        {
                            "path": rel,
                            "name": n.name,
                            "kind": type(n).__name__,
                            "line": n.lineno,
                            "qualified_name": qualified_name(n),
                            "signature": f"{prefix} {n.name}({args}){result_annotation}",
                        }
                    )
                if isinstance(n, ast.ClassDef) and rel.startswith(SRC_PREFIX):
                    bases = [ast.unparse(b) for b in n.bases] + [
                        f"{kw.arg}={ast.unparse(kw.value)}" for kw in n.keywords
                    ]
                    exports["definitions"].append(
                        {
                            "path": rel,
                            "name": n.name,
                            "kind": "ClassDef",
                            "line": n.lineno,
                            "qualified_name": qualified_name(n),
                            "signature": f"class {n.name}({', '.join(bases)})"
                            if bases
                            else f"class {n.name}",
                        }
                    )
                if isinstance(n, ast.ClassDef) and n.name == "DaemonApplication":
                    for method in n.body:
                        if isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef)):
                            reads, writes, calls = Counter(), Counter(), Counter()
                            for child in ast.walk(method):
                                if (
                                    isinstance(child, ast.Attribute)
                                    and isinstance(child.value, ast.Name)
                                    and child.value.id == "self"
                                ):
                                    if isinstance(child.ctx, ast.Store):
                                        writes[child.attr] += 1
                                    else:
                                        reads[child.attr] += 1
                                    if isinstance(
                                        getattr(child, "_parent", None), ast.AugAssign
                                    ):
                                        reads[child.attr] += 1
                                if (
                                    isinstance(child, ast.Call)
                                    and isinstance(child.func, ast.Attribute)
                                    and isinstance(child.func.value, ast.Name)
                                    and child.func.value.id == "self"
                                ):
                                    calls[child.func.attr] += 1
                            extraction["daemon_application_methods"].append(
                                {
                                    "path": rel,
                                    "name": method.name,
                                    "line": method.lineno,
                                    "self_reads": dict(sorted(reads.items())),
                                    "self_writes": dict(sorted(writes.items())),
                                    "self_calls": dict(sorted(calls.items())),
                                }
                            )
            if isinstance(n, ast.ImportFrom) and rel.endswith("/__init__.py"):
                exports["package_reexports"].append(
                    {
                        "path": rel,
                        "line": n.lineno,
                        "module": n.module,
                        "level": n.level,
                        "names": [a.name for a in n.names],
                    }
                )
            if isinstance(n, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "__all__" for t in n.targets
            ):
                vals = (
                    [
                        e.value
                        for e in n.value.elts
                        if isinstance(e, ast.Constant) and isinstance(e.value, str)
                    ]
                    if isinstance(n.value, (ast.List, ast.Tuple, ast.Set))
                    else []
                )
                exports["all_declarations"].append(
                    {"path": rel, "line": n.lineno, "names": vals}
                )
            if (
                isinstance(n, (ast.Attribute, ast.Name))
                and getattr(n, "id", getattr(n, "attr", "")) == "__file__"
            ):
                refs.append(
                    {
                        "kind": "__file__-path-arithmetic",
                        "path": rel,
                        "line": n.lineno,
                        "value": evidence_line(source, n.lineno, source_lines),
                    }
                )
    port_sites = {}
    for item in import_records:
        target = item["target"]
        if (
            target.endswith("_ports")
            or target.endswith(".ports")
            or ".ports." in target
            or target.endswith("ports.py")
        ):
            port_sites.setdefault(
                item["site"],
                {
                    "site": item["site"],
                    "path": item["path"],
                    "line": item["line"],
                    "target": target,
                    "symbol": item["symbol"],
                },
            )
    exports["ports_imports"] = [port_sites[k] for k in sorted(port_sites)]
    exports["ports_target_references"] = [
        row for row in import_records if row["site"] in port_sites
    ]
    source_port_sites = {
        k: v for k, v in port_sites.items() if v["path"].startswith(SRC_PREFIX)
    }
    exports["ports_import_site_count"] = len(port_sites)
    exports["source_ports_import_site_count"] = len(source_port_sites)
    exports["source_ports_module_count"] = len(
        {v["target"] for v in source_port_sites.values()}
    )
    exports["source_ports_consumer_sites"] = [
        source_port_sites[k] for k in sorted(source_port_sites)
    ]
    for rel in files:
        if rel.endswith(".py"):
            continue
        path = root / rel
        try:
            data = path.read_bytes()
        except OSError:
            continue
        if b"\0" in data[:8192]:
            continue
        content = data.decode("utf-8", "replace")
        for lineno, line in enumerate(content.splitlines(), 1):
            for match in path_pattern.finditer(line):
                value = match.group(0)
                textrefs.append(
                    {
                        "path": rel,
                        "line": lineno,
                        "value": value,
                        "classification": "historical"
                        if any(
                            x in rel.lower()
                            for x in (
                                "notes/",
                                "archive",
                                "archived",
                                "handoff",
                                "evidence",
                            )
                        )
                        else "active-or-unknown",
                        "evidence": snippet(line),
                    }
                )
    imported = defaultdict(list)
    for row in import_records:
        if not row["resolved"] or row["symbol"] == "*":
            continue
        if row["symbol"]:
            binding = row["alias"] or row["symbol"]
            target = f"{row['target']}.{row['symbol']}"
        elif row["kind"] == "ImportFrom":
            binding = row["alias"] or row["target"].rsplit(".", 1)[-1]
            target = row["target"]
        else:
            binding = row["alias"] or row["target"]
            target = row["target"]
        imported[row["path"]].append((binding, target, row["site"]))
    caller_refs = []
    for rel, (_, tree) in py_records.items():
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                name = dotted_name(node.func)
                if name:
                    for binding, target, site in imported[rel]:
                        if name == binding or name.startswith(binding + "."):
                            caller_refs.append(
                                {
                                    "path": rel,
                                    "line": node.lineno,
                                    "target_name": target + name[len(binding) :],
                                    "import_site": site,
                                    "resolution": "import-binding-syntactic-candidate-not-scope-verified",
                                }
                            )
    exports["caller_references"] = sorted(
        caller_refs, key=lambda x: (x["path"], x["line"], x["target_name"])
    )
    exports["limitations"] = [
        "Function and class signatures are syntax observations, not a complete API contract.",
        "Static caller resolution and overload/descriptor semantics are not inferred.",
        "Call candidates use imported spellings; shadowing, rebinding and function-local visibility require LibCST metadata before any rewrite.",
    ]
    extraction["limitations"] = [
        "Self attribute observations do not fully model mutations through aliases, subscripts, descriptors, or method side effects."
    ]
    return (
        sorted(refs, key=lambda x: (x["path"], x["line"], x["kind"])),
        sorted(textrefs, key=lambda x: (x["path"], x["line"], x["value"])),
        exports,
        extraction,
    )
