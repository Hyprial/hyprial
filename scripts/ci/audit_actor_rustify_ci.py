#!/usr/bin/env python3
# Decision record (CI-I, G0 item 9 / appendix I; Allen 2026-10-25, scheme ruled 2026-11-06):
# - Scope is the rustify-owned change surface, not the base branch name: a PR is
#   audited when its head branch carries a rustify prefix or its own diff touches a
#   rustify-owned path. Dev-first release-preparation sources have no audit
#   exemption: a source name does not prove that its changes were already tested.
# - Every comparison is merge-base(base, head) -> head, i.e. only the PR's own
#   change; a PR that lags its base is not blamed for what the base did meanwhile.
# - The guarded registries are checked only when the PR changes the file holding
#   them, and the whole file outside the literal must stay byte-identical.  That one
#   rule covers rebinding, mutation and assertion removal without a binding scanner.
# - The workflow step runs the base-ref copy of this file once the base has one;
#   this file and check_pr_hygiene.py may never change inside an audited PR, so the
#   gate is self-locking and must be right the first time it lands.
# - NATIVE_ORIGINS is audited as ONE inventory over its parts (resident_metadata/
#   native_origins_*.py, each one data literal NATIVE_ORIGINS_PART, merged by the frozen
#   union module NATIVE_PATH).  Steady state is the old rule per part: existing key/value
#   source text byte-identical, existing order kept, no row removed or moved between parts,
#   a key in two parts is red, a new part must be pure data and registered in registry-union.
#   The single-literal layout is relocated exactly once: relocation is accepted only when
#   the set of (key source, value source) rows is identical before and after and every part
#   keeps the old relative order; the wrapper text around the rows has no base to compare
#   against, so it is held to "docstring plus one literal assignment" and reviewed by hand.
"""Audit the closed CI-I change surface for actor-rustify.

Exit codes: 0 PASS or out of scope, 1 FAIL, 2 NOT EVALUABLE.
"""
from __future__ import annotations

import argparse
import ast
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

def _default_root() -> Path:
    script = Path(__file__).resolve()
    if script.parent.name == "ci" and script.parent.parent.name == "scripts":
        return script.parents[2]
    return Path.cwd()


ROOT = _default_root()
AUDIT_PATH = "scripts/ci/audit_actor_rustify_ci.py"
HYGIENE_PATH = "scripts/ci/check_pr_hygiene.py"
UNION_PATH = "scripts/ci/registry-union.json"
NATIVE_PATH = "tests/integration/native_harnesses/test_native_resident_tripwires.py"
HISTORY_PATH = "tests/integration/native_harnesses/resident_metadata/__init__.py"
PARTS_DIR = "tests/integration/native_harnesses/resident_metadata/"
PART_PATTERN = re.compile(r"native_origins_[a-z](?:_[a-z])?\.py\Z")
PART_SYMBOL = "NATIVE_ORIGINS_PART"
WORKFLOW_PATHS = (
    ".forgejo/workflows/tests.yml",
    ".forgejo/workflows/gate-python.yml",
    ".forgejo/workflows/gate-pr-hygiene.yml",
    ".forgejo/workflows/gate-pr-declarations.yml",
    ".forgejo/workflows/wheel-release.yml",
    ".forgejo/workflows/github-publish.yml",
    ".forgejo/workflows/gui.yml",
)
RUSTIFY_PATH_PREFIXES = ("crates/", "conformance/actor_engine/", "scripts/build/")
RUSTIFY_BRANCH_PREFIXES = ("infra-ops/rustify-", "ci/rustify-")


class AuditError(Exception):
    """A guarded file cannot be read as the closed literal shape."""


@dataclass(frozen=True)
class Literal:
    """One guarded module-level literal and the file text around it."""

    order: tuple[str, ...]
    spans: dict[str, str]
    keys: dict[str, str]
    outside: str


@dataclass(frozen=True)
class Scope:
    audited: bool
    reason: str


def _module_bindings(tree: ast.Module, name: str) -> list[ast.Assign | ast.AnnAssign]:
    found: list[ast.Assign | ast.AnnAssign] = []
    for node in tree.body:
        targets = node.targets if isinstance(node, ast.Assign) else [node.target] if isinstance(node, ast.AnnAssign) else []
        if any(isinstance(target, ast.Name) and target.id == name for target in targets):
            found.append(node)
    return found


def _segment(source: str, node: ast.AST) -> str:
    text = ast.get_source_segment(source, node)
    if text is None:
        raise AuditError("literal entry has no source span")
    return text


Entries = tuple[list[str], dict[str, str], dict[str, str]]


def _dict_entries(source: str, value: ast.AST, name: str) -> Entries:
    if not isinstance(value, ast.Dict):
        raise AuditError(f"{name} is not a dict literal")
    order: list[str] = []
    spans: dict[str, str] = {}
    keys: dict[str, str] = {}
    for key, item in zip(value.keys, value.values):
        if not isinstance(key, ast.Constant) or not isinstance(key.value, str):
            raise AuditError(f"{name} has a non-string key")
        if key.value in spans:
            raise AuditError(f"duplicate {name} entry {key.value!r}")
        try:
            ast.literal_eval(item)
        except ValueError as exc:
            raise AuditError(f"{name}[{key.value!r}] is not a literal") from exc
        order.append(key.value)
        keys[key.value] = _segment(source, key)
        spans[key.value] = f"{keys[key.value]}: {_segment(source, item)}"
    return order, spans, keys


def _set_entries(source: str, value: ast.AST, name: str) -> Entries:
    if not (
        isinstance(value, ast.Call)
        and isinstance(value.func, ast.Name)
        and value.func.id == "frozenset"
        and len(value.args) <= 1
        and not value.keywords
    ):
        raise AuditError(f"{name} is not frozenset({{...}})")
    items = value.args[0].elts if value.args and isinstance(value.args[0], ast.Set) else None
    if value.args and items is None:
        raise AuditError(f"{name} is not a set literal")
    order: list[str] = []
    spans: dict[str, str] = {}
    for item in items or []:
        if not isinstance(item, ast.Constant) or not isinstance(item.value, str):
            raise AuditError(f"{name} has a non-string entry")
        if item.value in spans:
            raise AuditError(f"duplicate {name} entry {item.value!r}")
        order.append(item.value)
        spans[item.value] = _segment(source, item)
    return order, spans, dict(spans)


def parse_literal(source: str, name: str, kind: str) -> Literal:
    """Read the single module-level ``name`` literal of ``kind`` ('dict' or 'set')."""
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        raise AuditError(f"syntax error: {exc}") from exc
    bindings = _module_bindings(tree, name)
    if len(bindings) != 1:
        raise AuditError(f"expected exactly one module-level assignment {name}; found {len(bindings)}")
    node = bindings[0]
    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
    if len(targets) != 1 or node.value is None:
        raise AuditError(f"{name} assignment must bind only the guarded name")
    reader = _dict_entries if kind == "dict" else _set_entries
    order, spans, keys = reader(source, node.value, name)
    lines = source.splitlines(keepends=True)
    start = sum(len(line) for line in lines[: node.lineno - 1]) + node.col_offset
    end = sum(len(line) for line in lines[: node.end_lineno - 1]) + node.end_col_offset
    outside = source[:start] + f"<{name}>" + source[end:]
    return Literal(tuple(order), spans, keys, outside)


def _is_subsequence(base: tuple[str, ...], head: tuple[str, ...]) -> bool:
    iterator = iter(head)
    return all(any(candidate == item for candidate in iterator) for item in base)


def _append_only(path: str, name: str, base: Literal, head: Literal) -> list[str]:
    errors: list[str] = []
    if base.outside != head.outside:
        errors.append(f"{path}: code outside {name} changed (assertion removal or non-registry edit)")
    if not _is_subsequence(base.order, head.order):
        errors.append(f"{path}: reordered {name} entries")
    for key in base.order:
        if key not in head.spans:
            errors.append(f"{path}: deleted {name} entry {key!r}")
        elif base.spans[key] != head.spans[key]:
            errors.append(f"{path}: changed {name} entry {key!r}")
    return errors


def is_part_path(path: str) -> bool:
    return path.startswith(PARTS_DIR) and PART_PATTERN.match(path[len(PARTS_DIR):]) is not None


@dataclass(frozen=True)
class Inventory:
    """NATIVE_ORIGINS as audited: one legacy literal in NATIVE_PATH, or the sorted parts."""

    native: str
    parts: dict[str, Literal]
    texts: dict[str, str]
    legacy: bool


def parse_inventory(native: str, parts: dict[str, str]) -> Inventory:
    if not parts:
        return Inventory(native, {NATIVE_PATH: parse_literal(native, "NATIVE_ORIGINS", "dict")}, {NATIVE_PATH: native}, True)
    try:
        tree = ast.parse(native)
    except SyntaxError as exc:
        raise AuditError(f"syntax error: {exc}") from exc
    if any(isinstance(node.value, ast.Dict) for node in _module_bindings(tree, "NATIVE_ORIGINS")):
        raise AuditError("NATIVE_ORIGINS is a dict literal in the union module although parts exist")
    ordered = dict(sorted(parts.items()))
    return Inventory(native, {path: parse_literal(text, PART_SYMBOL, "dict") for path, text in ordered.items()}, ordered, False)


def _union(inventory: Inventory) -> Literal:
    order: list[str] = []
    spans: dict[str, str] = {}
    keys: dict[str, str] = {}
    for literal in inventory.parts.values():
        for key in literal.order:
            if key not in spans:
                order.append(key)
                spans[key] = literal.spans[key]
                keys[key] = literal.keys[key]
    return Literal(tuple(order), spans, keys, inventory.native)


def _pure_data(source: str) -> bool:
    body = ast.parse(source).body
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) and isinstance(body[0].value.value, str):
        body = body[1:]
    return (
        len(body) == 1
        and isinstance(body[0], ast.Assign)
        and len(body[0].targets) == 1
        and isinstance(body[0].targets[0], ast.Name)
        and body[0].targets[0].id == PART_SYMBOL
    )


def _duplicates(head: Inventory) -> list[str]:
    seen: dict[str, str] = {}
    errors: list[str] = []
    for path, literal in head.parts.items():
        for key in literal.order:
            if key in seen and seen[key] != path:
                errors.append(f"{path}: duplicate NATIVE_ORIGINS entry {key!r} also in {seen[key]}")
            seen.setdefault(key, path)
    return errors


def _relocation(old: Literal, head: Inventory) -> list[str]:
    errors: list[str] = []
    union = _union(head)
    for key in old.order:
        if key not in union.spans:
            errors.append(f"{NATIVE_PATH}: relocation deleted NATIVE_ORIGINS entry {key!r}")
        elif old.spans[key] != union.spans[key]:
            errors.append(f"{NATIVE_PATH}: relocation changed NATIVE_ORIGINS entry {key!r}")
    for key in head_only(old.order, union.order):
        errors.append(f"{NATIVE_PATH}: relocation added NATIVE_ORIGINS entry {key!r}")
    for path, literal in head.parts.items():
        if not _is_subsequence(literal.order, old.order):
            errors.append(f"{path}: relocation reordered NATIVE_ORIGINS entries")
        if not _pure_data(head.texts[path]):
            errors.append(f"{path}: part must be a docstring plus one {PART_SYMBOL} assignment")
    return errors


def _inventory_errors(base: Inventory, head: Inventory) -> list[str]:
    if base.legacy and head.legacy:
        return _append_only(NATIVE_PATH, "NATIVE_ORIGINS", base.parts[NATIVE_PATH], head.parts[NATIVE_PATH])
    if head.legacy:
        return [f"{NATIVE_PATH}: NATIVE_ORIGINS moved out of its parts back into one literal"]
    errors = _duplicates(head)
    if base.legacy:
        return errors + _relocation(base.parts[NATIVE_PATH], head)
    if base.native != head.native:
        errors.append(f"{NATIVE_PATH}: code outside NATIVE_ORIGINS changed (assertion removal or non-registry edit)")
    for path in sorted(set(base.parts) | set(head.parts)):
        old, new = base.parts.get(path), head.parts.get(path)
        if new is None:
            errors += [f"{path}: deleted NATIVE_ORIGINS entry {key!r} (part removed)" for key in old.order]
        elif old is None:
            if not _pure_data(head.texts[path]):
                errors.append(f"{path}: part must be a docstring plus one {PART_SYMBOL} assignment")
        else:
            errors += _append_only(path, "NATIVE_ORIGINS", old, new)
    return errors


def audit_registries(
    native_base: str,
    native_head: str,
    history_base: str,
    history_head: str,
    parts_base: dict[str, str] | None = None,
    parts_head: dict[str, str] | None = None,
) -> list[str]:
    """NATIVE_ORIGINS append-only over its parts; HISTORICAL_ROWS gains only byte-identical retired live rows."""
    try:
        inv_base = parse_inventory(native_base, parts_base or {})
        inv_head = parse_inventory(native_head, parts_head or {})
        hist_base = parse_literal(history_base, "HISTORICAL_ROWS", "set")
        hist_head = parse_literal(history_head, "HISTORICAL_ROWS", "set")
    except AuditError as exc:
        return [f"registry parse failure: {exc}"]
    live_base, live_head = _union(inv_base), _union(inv_head)
    errors = _inventory_errors(inv_base, inv_head)
    errors += _append_only(HISTORY_PATH, "HISTORICAL_ROWS", hist_base, hist_head)
    for key in head_only(live_base.order, live_head.order):
        if key in hist_base.spans:
            errors.append(f"{NATIVE_PATH}: live entry {key!r} reuses a historical row")
    for key in head_only(hist_base.order, hist_head.order):
        if key not in live_base.spans or key not in live_head.spans:
            errors.append(f"{HISTORY_PATH}: historical row {key!r} has no unchanged live NATIVE_ORIGINS row")
        elif hist_head.keys[key] != live_base.keys[key]:
            errors.append(f"{HISTORY_PATH}: historical move {key!r} is not byte-identical")
    return errors


def head_only(base: tuple[str, ...], head: tuple[str, ...]) -> list[str]:
    known = set(base)
    return [key for key in head if key not in known]


def _is_legacy_entry(entry: object) -> bool:
    return isinstance(entry, dict) and entry.get("path") == NATIVE_PATH and entry.get("symbol") == "NATIVE_ORIGINS"


def audit_registry_config(base: str, head: str, parts: tuple[str, ...] = ()) -> list[str]:
    try:
        old, new = json.loads(base), json.loads(head)
        old_entries, new_entries = old["registries"], new["registries"]
        if not isinstance(old_entries, list) or not isinstance(new_entries, list):
            raise ValueError("registries is not a list")
    except (ValueError, KeyError, TypeError) as exc:
        return [f"{UNION_PATH}: parse failure: {exc}"]
    errors: list[str] = []
    if {k: v for k, v in old.items() if k != "registries"} != {k: v for k, v in new.items() if k != "registries"}:
        errors.append(f"{UNION_PATH}: non-registry policy changed")
    offset = 0
    for index, entry in enumerate(old_entries):
        pos = index + offset
        if pos >= len(new_entries):
            errors.append(f"{UNION_PATH}: deleted registry entry {entry!r}")
        elif new_entries[pos] == entry:
            continue
        elif _is_legacy_entry(entry) and parts and _covers_parts(entry, new_entries[pos : pos + len(parts)], parts):
            offset += len(parts) - 1
        else:
            errors.append(f"{UNION_PATH}: changed/reordered entry {entry!r} -> {new_entries[pos]!r}")
            break
    return errors


def _covers_parts(legacy: dict, replacement: list, parts: tuple[str, ...]) -> bool:
    wanted = [(path, PART_SYMBOL, legacy.get("kind")) for path in sorted(parts)]
    got = [(e.get("path"), e.get("symbol"), e.get("kind")) if isinstance(e, dict) else None for e in replacement]
    return got == wanted


def audit_parts_registered(config: str, parts: tuple[str, ...]) -> list[str]:
    try:
        registered = {(e["path"], e["symbol"]) for e in json.loads(config)["registries"]}
    except (ValueError, KeyError, TypeError) as exc:
        return [f"{UNION_PATH}: parse failure: {exc}"]
    return [f"{UNION_PATH}: part {path} is not registered" for path in parts if (path, PART_SYMBOL) not in registered]


def additions_only(diff: str, path: str) -> list[str]:
    return [
        f"{path}: removed workflow line {line[1:].strip()!r}"
        for line in diff.splitlines()
        if line.startswith("-") and not line.startswith("---")
    ]


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)


def _show(cwd: Path, ref: str, path: str) -> str | None:
    result = _git(cwd, "show", f"{ref}:{path}")
    return result.stdout if result.returncode == 0 else None


def _parts(cwd: Path, ref: str) -> dict[str, str]:
    listing = _git(cwd, "ls-tree", "-r", "--name-only", ref, "--", PARTS_DIR)
    if listing.returncode != 0:
        raise AuditError(f"git ls-tree failed: {listing.stderr.strip()}")
    found: dict[str, str] = {}
    for path in filter(is_part_path, listing.stdout.splitlines()):
        text = _show(cwd, ref, path)
        if text is None:
            raise AuditError(f"{path}: part cannot be read at {ref}")
        found[path] = text
    return found


def _commit(cwd: Path, ref: str) -> str | None:
    result = _git(cwd, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
    return result.stdout.strip() if result.returncode == 0 else None


def resolve_base_ref(base_ref: str, cwd: Path = ROOT) -> str | None:
    """Resolve ``origin/<BASE_REF>`` first, then the local branch."""
    raw = base_ref.strip()
    if not raw:
        return None
    bare = raw.removeprefix("origin/")
    for candidate in (f"origin/{bare}", bare):
        if _commit(cwd, candidate):
            return candidate
    return None


def changed_paths(cwd: Path, merge_base: str, head: str) -> list[str]:
    result = _git(cwd, "diff", "--name-only", "--no-renames", merge_base, head)
    if result.returncode != 0:
        raise AuditError(f"git diff failed: {result.stderr.strip()}")
    return [line for line in result.stdout.splitlines() if line]


def decide_scope(head_ref_name: str, paths: list[str]) -> Scope:
    name = head_ref_name.strip()
    if name.startswith(RUSTIFY_BRANCH_PREFIXES):
        return Scope(True, f"rustify head branch {name!r}")
    owned = sorted(path for path in paths if path.startswith(RUSTIFY_PATH_PREFIXES))
    if owned:
        return Scope(True, f"rustify-owned paths changed ({len(owned)}), first {owned[0]!r}")
    return Scope(False, "no rustify head prefix and no rustify-owned path changed")


def _unchanged(cwd: Path, merge_base: str, head: str, path: str, why: str) -> list[str]:
    before, after = _show(cwd, merge_base, path), _show(cwd, head, path)
    if before is not None and before != after:
        return [f"{path}: {why}"]
    return []


def audit_change_surface(cwd: Path, merge_base: str, head: str, paths: list[str]) -> list[str]:
    errors: list[str] = []
    touched = set(paths)
    parts_head: dict[str, str] = {}
    for path in sorted(touched):
        if path.startswith(PARTS_DIR) and path != HISTORY_PATH and not is_part_path(path) and _git(cwd, "cat-file", "-e", f"{head}:{path}").returncode == 0:
            errors.append(f"{path}: only registered native_origins part .py files and the package __init__.py may live under {PARTS_DIR}")
    if NATIVE_PATH in touched or HISTORY_PATH in touched or any(path.startswith(PARTS_DIR) for path in touched):
        sources = [_show(cwd, ref, path) for path in (NATIVE_PATH, HISTORY_PATH) for ref in (merge_base, head)]
        if any(source is None for source in sources):
            errors.append(f"{NATIVE_PATH} / {HISTORY_PATH}: guarded registry file missing or deleted")
        else:
            native_base, native_head, history_base, history_head = sources
            parts_base, parts_head = _parts(cwd, merge_base), _parts(cwd, head)
            errors += audit_registries(native_base, native_head, history_base, history_head, parts_base, parts_head)
    if UNION_PATH in touched or parts_head:
        union_base, union_head = _show(cwd, merge_base, UNION_PATH), _show(cwd, head, UNION_PATH)
        if union_base is None or union_head is None:
            errors.append(f"{UNION_PATH}: guarded file missing or deleted")
        else:
            if UNION_PATH in touched:
                errors += audit_registry_config(union_base, union_head, tuple(sorted(parts_head)))
            errors += audit_parts_registered(union_head, tuple(sorted(parts_head)))
    errors += _unchanged(cwd, merge_base, head, HYGIENE_PATH, "check_pr_hygiene.py, union algorithm and TREE_LEVEL_TESTS must be unchanged")
    errors += _unchanged(cwd, merge_base, head, AUDIT_PATH, "the CI-I audit must be byte-for-byte unchanged")
    for path in WORKFLOW_PATHS:
        if path in touched:
            diff = _git(cwd, "diff", "--unified=0", "--no-renames", merge_base, head, "--", path)
            if diff.returncode != 0:
                errors.append(f"NOT EVALUABLE: git diff {path} failed: {diff.stderr.strip()}")
            else:
                errors += additions_only(diff.stdout, path)
    return errors


def run(base_ref: str, head: str, head_ref_name: str, cwd: Path = ROOT) -> tuple[Scope | None, list[str]]:
    resolved = resolve_base_ref(base_ref, cwd)
    if resolved is None:
        return None, [f"NOT EVALUABLE: base ref {base_ref!r} could not be resolved"]
    head_commit = _commit(cwd, head)
    if head_commit is None:
        return None, [f"NOT EVALUABLE: head ref {head!r} could not be resolved"]
    merge_base = _git(cwd, "merge-base", resolved, head_commit)
    if merge_base.returncode != 0 or not merge_base.stdout.strip():
        return None, [f"NOT EVALUABLE: no merge-base between {resolved!r} and {head!r}"]
    base_commit = merge_base.stdout.strip()
    try:
        paths = changed_paths(cwd, base_commit, head_commit)
    except AuditError as exc:
        return None, [f"NOT EVALUABLE: {exc}"]
    scope = decide_scope(head_ref_name, paths)
    if not scope.audited:
        return scope, []
    return scope, audit_change_surface(cwd, base_commit, head_commit, paths)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-ref", required=True)
    parser.add_argument("--head", default="HEAD")
    parser.add_argument("--head-ref-name", required=True)
    parser.add_argument("--root", type=Path, default=ROOT)
    args = parser.parse_args(argv)
    scope, errors = run(args.base_ref, args.head, args.head_ref_name, args.root.resolve())
    for error in errors:
        print(f"FAIL: {error}", file=sys.stderr)
    if any(error.startswith("NOT EVALUABLE:") for error in errors):
        return 2
    if errors:
        return 1
    assert scope is not None
    verdict = "PASS" if scope.audited else "OUT OF SCOPE"
    print(f"{verdict}: CI-I audit base={args.base_ref} head={args.head} ({scope.reason})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
