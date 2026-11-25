"""Immutable-id segment grammar has one source of truth.

Decision: scan source ASTs one file at a time instead of retaining a full AST
dictionary, removing the shard-2 4 GiB cgroup peak-memory spike.
"""

from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[4]
SOURCE = ROOT / "src" / "hyprial"
URI_MODULE = SOURCE / "kernel" / "impl" / "primitives" / "uri.py"
PREDICATES = {
    "is_user_id_segment",
    "is_device_id_segment",
    "is_agent_id_segment",
}
AGGREGATE_PREDICATE = "is_identity_id_segment"
GATE_FILES = {
    SOURCE / "shell" / "impl" / "login" / "flow.py",
    SOURCE / "daemon" / "impl" / "configuration" / "identity.py",
    SOURCE / "identity" / "impl" / "device" / "models.py",
    SOURCE / "daemon" / "impl" / "network" / "tailcat" / "keys.py",
    SOURCE / "identity" / "impl" / "agents" / "registry" / "_core.py",
    SOURCE / "identity" / "impl" / "users" / "store" / "_store.py",
    SOURCE / "daemon" / "impl" / "pac" / "graphs" / "edits.py",
    SOURCE / "daemon" / "impl" / "bootstrap" / "adapter_registration.py",
}
PRIVATE_PREFIXES = {"u.", "d.", "a."}
PRIVATE_REGEX_FRAGMENTS = ("ou_", "[a-z2-7]{26}", "[0-9a-f]{32}")


def test_id_shape_predicates_are_defined_only_in_the_kernel_uri_module() -> None:
    definitions: dict[str, list[Path]] = {name: [] for name in PREDICATES}
    for path in sorted(SOURCE.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.name in definitions:
                    definitions[node.name].append(path)
    assert definitions == {name: [URI_MODULE] for name in PREDICATES}


def test_no_other_source_module_hard_codes_the_old_prefix_tuple() -> None:
    offenders = []
    for path in sorted(SOURCE.rglob("*.py")):
        if path == URI_MODULE:
            continue
        text = path.read_text(encoding="utf-8")
        if '("u.", "d.", "a.")' in text or "('u.', 'd.', 'a.')" in text:
            offenders.append(path.relative_to(ROOT).as_posix())
    assert offenders == []


def _imports_uri_predicates(tree: ast.AST) -> bool:
    names = PREDICATES | {AGGREGATE_PREDICATE, "uri"}
    modules = {
        "hyprial.kernel",
        "hyprial.kernel.interface",
        "hyprial.kernel.impl.primitives",
        "hyprial.kernel.impl.primitives.uri",
    }
    return any(
        isinstance(node, ast.ImportFrom)
        and node.module in modules
        and any(alias.name in names for alias in node.names)
        for node in ast.walk(tree)
    )


def _literal_strings(node: ast.AST) -> set[str]:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return {node.value}
    if isinstance(node, (ast.List, ast.Set, ast.Tuple)):
        return set().union(*(_literal_strings(item) for item in node.elts))
    return set()


def _private_id_shape_checks(path: Path, tree: ast.AST) -> list[str]:
    offenders = []
    relative = path.relative_to(ROOT).as_posix()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        if isinstance(node.func, ast.Attribute) and node.func.attr == "startswith":
            prefixes = _literal_strings(node.args[0]) & PRIVATE_PREFIXES
            if prefixes:
                offenders.append(
                    f"{relative}:{node.lineno}: startswith {sorted(prefixes)!r}"
                )
        is_re_call = (
            isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "re"
            and node.func.attr in {"compile", "fullmatch", "match", "search"}
        )
        if is_re_call:
            for pattern in _literal_strings(node.args[0]):
                fragments = [
                    fragment
                    for fragment in PRIVATE_REGEX_FRAGMENTS
                    if fragment in pattern
                ]
                if fragments:
                    offenders.append(
                        f"{relative}:{node.lineno}: private id regex {fragments!r}"
                    )
    return offenders


def test_gate_modules_do_not_reimplement_private_id_shape_checks() -> None:
    seen = set()
    offenders = []
    for path in sorted(SOURCE.rglob("*.py")):
        seen.add(path)
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        if path == URI_MODULE:
            continue
        if path in GATE_FILES or _imports_uri_predicates(tree):
            offenders.extend(_private_id_shape_checks(path, tree))
    assert not (missing := sorted(GATE_FILES - seen)), missing
    assert offenders == []
