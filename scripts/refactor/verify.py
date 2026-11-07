"""Staged verification for the refactor toolchain.

Checks are dispatched from the manifest's ``verification`` section; every
check is configured there and nothing is invented at run time.

Status semantics (defect 6 fix):

- ``passed``: every configured check ran and succeeded.
- ``failed``: at least one check failed (static or executed command).
- ``required-missing``: a check marked ``required: true`` could not run
  (command checks only run under ``--run-commands``). This never satisfies
  a stage.
- ``skipped``: only optional checks were skipped; reported, not a pass.
- ``no-coverage``: the stage configures no checks (or the stage is absent);
  silence is not acceptance.

Architecture gates (defect 1 fix) reuse one import-fact resolver covering
absolute imports, relative imports (any depth), ``from package import
child``, aliases, TYPE_CHECKING blocks, function-local imports, star imports
and statically-recognizable dynamic imports. Parse failures, missing roots,
unmapped product files and unresolved internal targets produce explicit
diagnostics — never a silent PASS. Unresolvable dynamic references are
reported as pending, never claimed as covered.
"""

from __future__ import annotations

import ast
import hashlib
import json
import subprocess
from collections import Counter
from pathlib import Path

from .transforms.common import ModuleIndex, ToolError

SKIPPED = "skipped"
PASSED = "passed"
FAILED = "failed"
BLOCKED = "blocked"
REQUIRED_MISSING = "required-missing"
NO_COVERAGE = "no-coverage"

REFLECT_FUNCS = ("import_module", "__import__")


def _module_of(rel: str) -> str | None:
    if not rel.endswith(".py"):
        return None
    logical = rel[len("src/") :] if rel.startswith("src/") else rel
    parts = logical[: -len(".py")].split("/")
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _layer_of(module: str, layer_of_module: dict[str, str]) -> str | None:
    best = None
    best_len = -1
    for prefix, layer in layer_of_module.items():
        if module == prefix or module.startswith(prefix + "."):
            if len(prefix) > best_len:
                best, best_len = layer, len(prefix)
    return best


def _parent_map(tree: ast.AST) -> dict[int, ast.AST]:
    parents: dict[int, ast.AST] = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parents[id(child)] = parent
    return parents


class _FileFacts:
    """Import facts of one file, resolved against the full module index."""

    def __init__(self, root: Path, rel: str, index: ModuleIndex) -> None:
        self.rel = rel
        self.parse_error: str | None = None
        self.facts: list[dict] = []
        path = root / rel
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError as exc:
            self.parse_error = str(exc)
            return
        module = _module_of(rel)
        package = (
            index.package_of_module(module, rel.endswith("__init__.py")) if module else ""
        )
        self._collect(tree, package, index)

    def _collect(self, tree: ast.AST, package: str, index: ModuleIndex) -> None:
        tc_names, tc_modules = {"TYPE_CHECKING"}, {"typing"}
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "typing":
                tc_names.update(
                    a.asname or a.name for a in node.names if a.name == "TYPE_CHECKING"
                )
            if isinstance(node, ast.Import):
                tc_modules.update(a.asname or a.name for a in node.names if a.name == "typing")
        parents = _parent_map(tree)

        # callable names statically bound to import_module/__import__, either
        # via `import importlib` (dotted use) or aliased from-imports
        reflect_bindings: set[str] = set()
        local_str_consts: dict[str, str] = {}
        ambiguous_consts: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    # `import importlib` and `import importlib as il` both
                    # bind a name whose attribute calls are import_module
                    if alias.name in ("importlib", "importlib.__init__"):
                        reflect_bindings.add(alias.asname or "importlib")
            elif isinstance(node, ast.ImportFrom) and node.module == "importlib":
                for alias in node.names:
                    if alias.name in REFLECT_FUNCS:
                        reflect_bindings.add(alias.asname or alias.name)
            elif isinstance(node, ast.Assign) and len(node.targets) == 1:
                target = node.targets[0]
                if (
                    isinstance(target, ast.Name)
                    and isinstance(node.value, ast.Constant)
                    and isinstance(node.value.value, str)
                ):
                    name = target.id
                    value = node.value.value
                    # conservative: the same name bound to DIFFERENT string
                    # values anywhere in the file makes inference ambiguous —
                    # stop rather than let an unrelated scope mask a target
                    if name in local_str_consts and local_str_consts[name] != value:
                        ambiguous_consts.add(name)
                    elif name not in local_str_consts:
                        local_str_consts[name] = value

        def in_type_checking(node: ast.AST) -> bool:
            current = node
            while id(current) in parents:
                parent = parents[id(current)]
                if isinstance(parent, ast.If) and current in parent.body:
                    test = parent.test
                    if (isinstance(test, ast.Name) and test.id in tc_names) or (
                        isinstance(test, ast.Attribute)
                        and test.attr == "TYPE_CHECKING"
                        and isinstance(test.value, ast.Name)
                        and test.value.id in tc_modules
                    ):
                        return True
                current = parent
            return False

        def function_local(node: ast.AST) -> bool:
            current = node
            while id(current) in parents:
                current = parents[id(current)]
                if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                    return True
            return False

        def context_of(node: ast.AST) -> str:
            if in_type_checking(node):
                return "type-checking"
            if function_local(node):
                return "function-local"
            return "module"

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self.facts.append(
                        {
                            "kind": "import",
                            "target": alias.name,
                            "line": node.lineno,
                            "context": context_of(node),
                        }
                    )
            elif isinstance(node, ast.ImportFrom):
                base = index.resolve_relative(package, node.level, node.module)
                context = context_of(node)
                self.facts.append(
                    {
                        "kind": "from",
                        "target": base or "",
                        "names": [a.name for a in node.names],
                        "line": node.lineno,
                        "context": context,
                    }
                )
                # `from package import child`: the child submodule is its own
                # fact when it resolves, so package-child cross-layer hops are
                # judged on the child, not lost on the unmapped parent
                for alias in node.names:
                    candidate = f"{base}.{alias.name}" if base else alias.name
                    if base and index.is_module(candidate):
                        self.facts.append(
                            {
                                "kind": "from-child",
                                "target": candidate,
                                "line": node.lineno,
                                "context": context,
                            }
                        )
            elif isinstance(node, ast.Call):
                name = (
                    node.func.id
                    if isinstance(node.func, ast.Name)
                    else node.func.attr
                    if isinstance(node.func, ast.Attribute)
                    else ""
                )
                is_reflect = (
                    isinstance(node.func, ast.Attribute) and name in REFLECT_FUNCS and reflect_bindings
                ) or (
                    # bare __import__(...) is a builtin: no binding needed
                    (isinstance(node.func, ast.Name) and name in REFLECT_FUNCS)
                    or (isinstance(node.func, ast.Name) and name in reflect_bindings)
                )
                if is_reflect and node.args:
                    arg = node.args[0]
                    value: object = None
                    resolved = False
                    if isinstance(arg, ast.Name) and arg.id in ambiguous_consts:
                        self.facts.append(
                            {
                                "kind": "dynamic-unresolved",
                                "target": None,
                                "line": node.lineno,
                                "context": context_of(node),
                                "lead": True,
                                "detail": f"constant {arg.id!r} has conflicting string "
                                "bindings in this file; manual review required",
                            }
                        )
                        continue
                    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                        value = arg.value
                        resolved = True
                    elif (
                        isinstance(arg, ast.Name)
                        and arg.id in local_str_consts
                    ):
                        value = local_str_consts[arg.id]
                        resolved = True
                    if resolved:
                        target = str(value)
                        if target.startswith("."):
                            target = index.resolve_relative(package, 1, target) or target
                        self.facts.append(
                            {
                                "kind": "dynamic",
                                "target": target,
                                "line": node.lineno,
                                "context": context_of(node),
                            }
                        )
                    else:
                        # a dynamic import whose target cannot be resolved
                        # statically is recorded as an unresolved LEAD, never
                        # dropped and never counted as coverage
                        self.facts.append(
                            {
                                "kind": "dynamic-unresolved",
                                "target": None,
                                "line": node.lineno,
                                "context": context_of(node),
                                "lead": True,
                                "detail": "dynamic import target not statically resolvable",
                            }
                        )


def _collect_facts(root: Path, config: dict) -> tuple[dict[str, _FileFacts], list[dict]]:
    """Parse every Python file under the configured roots; loud on problems."""
    roots = list(config.get("roots") or ["src"])
    problems: list[dict] = []
    files: list[str] = []
    for root_rel in roots:
        base = root / root_rel
        if not base.is_dir():
            problems.append(
                {"kind": "missing-root", "detail": f"configured root is missing: {root_rel}"}
            )
            continue
        files.extend(p.relative_to(root).as_posix() for p in sorted(base.rglob("*.py")))
    index = ModuleIndex(files)
    facts: dict[str, _FileFacts] = {}
    for rel in files:
        parsed = _FileFacts(root, rel, index)
        if parsed.parse_error is not None:
            problems.append(
                {"kind": "parse", "path": rel, "detail": parsed.parse_error}
            )
        facts[rel] = parsed
    return facts, problems


def _file_layers(facts: dict[str, _FileFacts], layer_of_module: dict[str, str]) -> dict:
    mapping = {}
    for rel in facts:
        module = _module_of(rel)
        mapping[rel] = (module, _layer_of(module, layer_of_module) if module else None)
    return mapping


def check_boundary_direction(root: Path, config: dict) -> dict:
    order = list(config.get("layer_order") or [])
    layer_of_module = dict(config.get("layer_of_module") or {})
    if not order or not layer_of_module:
        raise ToolError("boundary_direction requires layer_order and layer_of_module")
    rank = {layer: i for i, layer in enumerate(order)}
    facts, problems = _collect_facts(root, config)
    violations: list[dict] = []
    pending: list[dict] = []
    unmapped_scope = str(config.get("unmapped_scope") or "src/")
    file_layers = _file_layers(facts, layer_of_module)
    index_of_names = ModuleIndex(list(facts))
    checked = 0
    for rel, parsed in facts.items():
        module, src_layer = file_layers[rel]
        if src_layer is None and module and rel.startswith(unmapped_scope):
            problems.append(
                {
                    "kind": "unmapped-product-file",
                    "path": rel,
                    "detail": f"module {module} has no layer mapping",
                }
            )
            continue
        if src_layer is None or parsed.parse_error:
            continue
        for fact in parsed.facts:
            target = fact["target"]
            if fact["kind"] == "dynamic-unresolved":
                pending.append(
                    {"path": rel, "target": None, "line": fact["line"],
                     "lead": True,
                     "detail": fact.get("detail", "dynamic import target not statically resolvable")}
                )
                continue
            if fact["kind"] == "dynamic":
                # a statically resolved dynamic import is judged like any
                # other import: known internal reverse hops are violations
                dst_layer = _layer_of(target, layer_of_module)
                if dst_layer is not None and dst_layer != src_layer:
                    checked += 1
                    if rank[src_layer] < rank[dst_layer]:
                        violations.append(
                            {
                                "path": rel,
                                "line": fact["line"],
                                "context": fact["context"],
                                "import_kind": "dynamic",
                                "target": target,
                                "from": f"{src_layer}({module})",
                                "to": f"{dst_layer}({target})",
                            }
                        )
                continue
            if fact["kind"] == "import" and target.startswith("hyprial.") and not index_of_names.is_module(target):
                problems.append(
                    {
                        "kind": "nonexistent-internal-module",
                        "path": rel,
                        "line": fact["line"],
                        "detail": f"internal module does not exist: {target}",
                    }
                )
                continue
            if not target:
                problems.append(
                    {
                        "kind": "unresolved-target",
                        "path": rel,
                        "line": fact["line"],
                        "detail": "import target resolves to nothing",
                    }
                )
                continue
            dst_layer = _layer_of(target, layer_of_module)
            if dst_layer is None or dst_layer == src_layer:
                continue
            checked += 1
            if rank[src_layer] < rank[dst_layer]:
                violations.append(
                    {
                        "path": rel,
                        "line": fact["line"],
                        "context": fact["context"],
                        "import_kind": fact["kind"],
                        "target": target,
                        "from": f"{src_layer}({module})",
                        "to": f"{dst_layer}({target})",
                    }
                )
    violations.extend(problems)
    # unresolved dynamic targets cannot satisfy acceptance: blocked, not
    # passed and not silently pending
    status = FAILED if violations else (BLOCKED if pending else PASSED)
    return {
        "status": status,
        "violations": violations,
        "pending_dynamic_refs": pending,
        "product_files_covered": len(facts),
        "cross_layer_edges": checked,
    }


def _face_allowlist(root: Path, face: str, index: ModuleIndex) -> set[str] | None:
    """Approved export set of a face: its __all__ when the face file exists.

    Returns None when no allowlist can be established (missing file or no
    __all__): symbol-level imports from that face then count as BLOCKED, not
    passed — an unauditable face must not silently accept private names.
    """
    path = index.path_of(face)
    if path is None:
        return None
    try:
        tree = ast.parse((root / path).read_text(encoding="utf-8", errors="replace"))
    except SyntaxError:
        return None
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "__all__" for t in node.targets
        ):
            if isinstance(node.value, (ast.List, ast.Tuple, ast.Set)):
                names = set()
                for element in node.value.elts:
                    if isinstance(element, ast.Constant) and isinstance(element.value, str):
                        names.add(element.value)
                return names
            return None
    return None


def check_public_face_gate(root: Path, config: dict) -> dict:
    layer_of_module = dict(config.get("layer_of_module") or {})
    faces = set(config.get("public_faces") or [])
    if not layer_of_module:
        raise ToolError("public_face_gate requires layer_of_module")
    facts, problems = _collect_facts(root, config)
    violations: list[dict] = []
    blocked: list[dict] = []
    pending: list[dict] = []
    file_layers = _file_layers(facts, layer_of_module)
    index = ModuleIndex(list(facts))
    allowlists: dict[str, set[str] | None] = {}
    for face in faces:
        if face in index.module_paths or any(m == face for m in index.module_paths):
            allowlists[face] = _face_allowlist(root, face, index)
    for rel, parsed in facts.items():
        module, src_layer = file_layers[rel]
        if src_layer is None or parsed.parse_error:
            continue
        for fact in parsed.facts:
            target = fact["target"]
            if fact["kind"] == "dynamic-unresolved":
                pending.append(
                    {"path": rel, "target": None, "line": fact["line"],
                     "lead": True,
                     "detail": fact.get("detail", "dynamic import target not statically resolvable")}
                )
                continue
            if fact["kind"] == "dynamic":
                dst_layer = _layer_of(target, layer_of_module)
                if dst_layer is not None and dst_layer != src_layer and target not in faces:
                    violations.append(
                        {
                            "path": rel,
                            "line": fact["line"],
                            "import": target,
                            "import_kind": "dynamic",
                            "detail": "cross-layer dynamic import must target an approved face",
                        }
                    )
                continue
            if fact["kind"] == "import" and target.startswith("hyprial.") and not index.is_module(target):
                problems.append(
                    {
                        "kind": "nonexistent-internal-module",
                        "path": rel,
                        "line": fact["line"],
                        "detail": f"internal module does not exist: {target}",
                    }
                )
                continue
            if not target:
                problems.append(
                    {
                        "kind": "unresolved-target",
                        "path": rel,
                        "line": fact["line"],
                        "detail": "import target resolves to nothing",
                    }
                )
                continue
            dst_layer = _layer_of(target, layer_of_module)
            if dst_layer is None or dst_layer == src_layer:
                continue  # same-layer internal use is free
            if target not in faces:
                # cross-layer access must target an approved face exactly: a
                # registered face does NOT whitelist its own submodules
                violations.append(
                    {
                        "path": rel,
                        "line": fact["line"],
                        "import": target,
                        "context": fact["context"],
                        "detail": (
                            "cross-layer import must target an approved public face, "
                            f"not its submodules; approved: {sorted(faces)}"
                        ),
                    }
                )
                continue
            # `from <face> import ...`: every imported symbol must be in the
            # face's audited export allowlist
            names = fact.get("names") or []
            if "*" in names:
                violations.append(
                    {
                        "path": rel,
                        "line": fact["line"],
                        "import": target,
                        "detail": "star import from a public face is not auditable",
                    }
                )
                continue
            if not names:
                continue
            allowlist = allowlists.get(target)
            if allowlist is None:
                blocked.append(
                    {
                        "path": rel,
                        "line": fact["line"],
                        "import": target,
                        "names": names,
                        "detail": (
                            f"face {target} has no audited __all__; symbol-level import "
                            "cannot be proven allowlisted"
                        ),
                    }
                )
                continue
            unlisted = sorted(set(names) - allowlist)
            if unlisted:
                violations.append(
                    {
                        "path": rel,
                        "line": fact["line"],
                        "import": target,
                        "names": unlisted,
                        "detail": (
                            f"symbols not in the approved export allowlist of {target}: "
                            f"{unlisted}"
                        ),
                    }
                )
    violations.extend(problems)
    status = FAILED if violations else (BLOCKED if blocked or pending else PASSED)
    return {
        "status": status,
        "violations": violations,
        "blocked": blocked,
        "pending_dynamic_refs": pending,
        "product_files_covered": len(facts),
    }


def _entry_identity(entry: dict) -> tuple:
    """Type-preserving complete-record identity.

    Canonical JSON keeps JSON types distinct (integer 1 differs from the
    string "1") and field order irrelevant. Actor entries carry
    path/symbol/reason; supervision adds kind/owner/card; inbox entries
    identify by path/owner/operation(+reason). Comparing whole records as a
    multiset preserves duplicates and policy metadata.
    """
    return json.dumps(entry, sort_keys=True, ensure_ascii=False)


def check_authority_manifests(root: Path, config: dict) -> dict:
    problems: list[dict] = []
    total_entries = 0
    for manifest_cfg in config.get("manifests") or []:
        rel = manifest_cfg["path"]
        path = root / rel
        if not path.is_file():
            problems.append({"path": rel, "error": "manifest file missing"})
            continue
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            problems.append({"path": rel, "error": f"invalid JSON: {exc}"})
            continue
        entries = (
            document
            if isinstance(document, list)
            else document.get("exemptions", document.get("entries", []))
        )
        if not isinstance(entries, list):
            problems.append({"path": rel, "error": "no entry list found"})
            continue
        total_entries += len(entries)
        expected = manifest_cfg.get("expected_entries")
        if expected is not None and len(entries) != expected:
            problems.append(
                {"path": rel, "error": f"expected {expected} entries, found {len(entries)}"}
            )
        preserved_from = manifest_cfg.get("preserved_from")
        pin = manifest_cfg.get("pin_sha256")
        if not preserved_from:
            problems.append(
                {"path": rel, "error": "preserved_from baseline comparison is required"}
            )
            continue
        if not pin:
            problems.append(
                {
                    "path": rel,
                    "error": "pin_sha256 of the baseline copy is required; an unpinned "
                    "baseline could be edited alongside the manifest",
                }
            )
            continue
        base_path = root / preserved_from
        if not base_path.is_file():
            problems.append({"path": rel, "error": f"baseline copy {preserved_from} missing"})
            continue
        baseline_bytes = base_path.read_bytes()
        if hashlib.sha256(baseline_bytes).hexdigest() != pin:
            problems.append(
                {
                    "path": rel,
                    "error": f"baseline copy {preserved_from} does not match its pinned "
                    "sha256; restore the pinned baseline or re-pin after review",
                }
            )
            continue
        try:
            baseline = json.loads(baseline_bytes.decode("utf-8"))
        except json.JSONDecodeError as exc:
            problems.append({"path": rel, "error": f"baseline JSON invalid: {exc}"})
            continue
        base_entries = (
            baseline
            if isinstance(baseline, list)
            else baseline.get("exemptions", baseline.get("entries", []))
        )
        allowed_new_to_old = {
            m["to"]: m["from"] for m in manifest_cfg.get("allowed_moves") or []
        }
        # multiset comparison of complete records; approved moves rewrite the
        # path field first so identity fields must match exactly, duplicates
        # keep their multiplicity, and a copied entry cannot mask a deletion
        adjusted = []
        for entry in entries:
            item = {**entry}
            old_path = allowed_new_to_old.get(item.get("path"))
            if old_path is not None:
                item["path"] = old_path
            adjusted.append(item)
        current_counts = Counter(_entry_identity(e) for e in adjusted)
        baseline_counts = Counter(_entry_identity(e) for e in base_entries)
        # top-level schema/policy/other metadata must stay identical; only
        # the exemptions list may differ, and only via approved path moves
        baseline_meta = {
            k: v for k, v in baseline.items() if k not in ("exemptions", "entries")
        } if isinstance(baseline, dict) else {}
        current_meta = {
            k: v for k, v in document.items() if k not in ("exemptions", "entries")
        } if isinstance(document, dict) else {}
        if json.dumps(baseline_meta, sort_keys=True, ensure_ascii=False) != json.dumps(
            current_meta, sort_keys=True, ensure_ascii=False
        ):
            problems.append(
                {
                    "path": rel,
                    "error": "top-level schema/policy metadata changed; only the "
                    "entry list may differ, and only through approved path moves",
                }
            )
        for identity, count in (baseline_counts - current_counts).items():
            problems.append(
                {
                    "path": rel,
                    "error": f"entry removed or altered ({count}x): "
                    + str(identity)[:200],
                }
            )
        for identity, count in (current_counts - baseline_counts).items():
            problems.append(
                {
                    "path": rel,
                    "error": f"entry added or duplicated ({count}x): "
                    + str(identity)[:200],
                }
            )
    return {
        "status": PASSED if not problems else FAILED,
        "problems": problems,
        "total_entries": total_entries,
    }


def check_resource_present(root: Path, config: dict) -> dict:
    missing = []
    for rel in config.get("paths") or []:
        path = root / rel
        if not path.exists():
            missing.append(rel)
    return {"status": PASSED if not missing else FAILED, "missing": missing}


def check_command(root: Path, config: dict, run_commands: bool) -> dict:
    required = bool(config.get("required"))
    if not run_commands:
        return {
            "status": SKIPPED,
            "required": required,
            "detail": "command checks are disabled; pass --run-commands to execute",
            "argv": config.get("argv"),
        }
    argv = [str(a) for a in config.get("argv") or []]
    if not argv:
        return {"status": FAILED, "required": required, "detail": "command check has no argv"}
    cwd = root / str(config.get("cwd") or ".")
    timeout = float(config.get("timeout_s") or 600)
    expect_exit = int(config.get("expect_exit", 0))
    try:
        completed = subprocess.run(
            argv, cwd=str(cwd), capture_output=True, text=True, timeout=timeout
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {
            "status": FAILED,
            "required": required,
            "detail": f"command failed to run: {exc}",
            "argv": argv,
        }
    ok = completed.returncode == expect_exit
    return {
        "status": PASSED if ok else FAILED,
        "required": required,
        "argv": argv,
        "exit_code": completed.returncode,
        "stdout_tail": completed.stdout[-4000:],
        "stderr_tail": completed.stderr[-4000:],
    }


STATIC_CHECKS = {
    "boundary_direction": lambda root, cfg, run: check_boundary_direction(root, cfg),
    "public_face_gate": lambda root, cfg, run: check_public_face_gate(root, cfg),
    "authority_manifests": lambda root, cfg, run: check_authority_manifests(root, cfg),
    "resource_present": lambda root, cfg, run: check_resource_present(root, cfg),
    "command": lambda root, cfg, run: check_command(root, cfg, run),
}


def run_stage_verification(
    root: Path, manifest: dict, stage: str | None, run_commands: bool
) -> dict:
    verification = manifest.get("verification") or {}
    report: dict = {"stages": {}, "status": PASSED}
    stages = [stage] if stage else sorted(verification)
    total_checks = 0
    for key in stages:
        checks = verification.get(key, [])
        results = []
        for config in checks:
            total_checks += 1
            name = config.get("check")
            required = bool(config.get("required"))
            entry = {"check": name, "name": config.get("name") or name, "required": required}
            fn = STATIC_CHECKS.get(name)
            if fn is None:
                entry["status"] = FAILED
                entry["detail"] = f"unknown check {name!r}"
            else:
                try:
                    entry.update(fn(root, config, run_commands))
                except ToolError as exc:
                    entry["status"] = FAILED
                    entry["detail"] = str(exc)
            results.append(entry)
        if not results:
            stage_status = NO_COVERAGE
        elif any(r["status"] == FAILED for r in results):
            stage_status = FAILED
        elif any(r["status"] == SKIPPED and r.get("required") for r in results):
            stage_status = REQUIRED_MISSING
        elif any(r["status"] == SKIPPED for r in results):
            stage_status = SKIPPED
        else:
            stage_status = PASSED
        report["stages"][key] = {"status": stage_status, "results": results}
    stage_statuses = [value["status"] for value in report["stages"].values()]
    if FAILED in stage_statuses:
        report["status"] = FAILED
    elif REQUIRED_MISSING in stage_statuses:
        report["status"] = REQUIRED_MISSING
    elif not stages or total_checks == 0:
        report["status"] = NO_COVERAGE
    elif SKIPPED in stage_statuses:
        report["status"] = SKIPPED
    else:
        report["status"] = PASSED
    return report


__all__ = [
    "FAILED",
    "NO_COVERAGE",
    "PASSED",
    "REQUIRED_MISSING",
    "SKIPPED",
    "run_stage_verification",
]
