#!/usr/bin/env python3
"""Choose and shard the public GitHub-hosted pytest suite.

The public workflow has one plan interface.  Pull requests may select tests
from a verified two-parent merge diff, while every other event and every
uncertain input runs the complete tracked test set.  Product source and
environment-sensitive scripts deliberately use the full suite.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import re
import subprocess
import sys
from collections import defaultdict
from collections.abc import Iterable
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[2]
SMALL_SELECTION_SHARD1_MAX = 16
MAX_SHARDS = 16
_FULL_FILES = frozenset(
    {
        ".github/workflows/gate.yml",
        ".github/workflows/nightly-full.yml",
        "scripts/ci/pytest_impact.py",
        "scripts/ci/docs_only.py",
        "scripts/git_environment.py",
        "pyproject.toml",
        "uv.lock",
        "tests/conftest.py",
        "tests/__init__.py",
    }
)
_FULL_PREFIXES = (".github/actions/", "tests/support/", "tests/fixtures/")
_ALLOWED_TEST_TOP_DIRS = frozenset({"kernel", "identity", "daemon", "biz", "shell", "integration", "tooling", "scripts"})
ALL_PYTHON_GUARDS = (
    "tests/kernel/language/test_exception_handler_bindings.py",
    "tests/daemon/harnesses/actors/test_harness_restore_admission.py",
    "tests/shell/compatibility/test_unreachable_surface_retirement.py",
    "tests/tooling/architecture/rename/test_hyprial_rename_contract.py",
    "tests/tooling/isolation/windows/test_windows_suite_harness.py",
    "tests/tooling/refactor/scanning/test_c0_scan.py",
)

try:
    from scripts.ci import docs_only
    from scripts.git_environment import hardened_git_environment
except ModuleNotFoundError:
    sys.path.insert(0, str(ROOT))
    from scripts.ci import docs_only
    from scripts.git_environment import hardened_git_environment


def full(reason: str) -> dict[str, object]:
    return {"mode": "full", "kind": "full", "tests": ["tests/"], "groups": [], "reasons": [reason]}


def git(root: Path, *args: str) -> str:
    environment = hardened_git_environment(os.environ, include_home=True, propagate_proxy=True)
    return subprocess.run(
        ["git", *args],
        cwd=root,
        env=environment,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=60,
    ).stdout


def tracked_test_files(root: Path) -> list[str]:
    raw = git(root, "ls-files", "-z", "--", "tests/**/test_*.py", "tests/test_*.py", "tests/**/*_test.py", "tests/*_test.py")
    paths = [path for path in raw.split("\0") if path]
    verified: list[str] = []
    for path in paths:
        if not _valid_test_path(path):
            raise ValueError("git returned an invalid test path")
        target = root / path
        if target.is_symlink() or not target.is_file():
            raise ValueError("git returned a non-regular test path")
        verified.append(path)
    return sorted(set(verified), key=lambda value: value.encode())


def _valid_test_path(path: str) -> bool:
    if not isinstance(path, str) or not path.startswith("tests/") or "\\" in path:
        return False
    parsed = PurePosixPath(path)
    return parsed.as_posix() == path and not any(part in {"", ".", ".."} for part in parsed.parts)


def _module_name(path: str) -> str:
    return path[:-3].replace("/", ".")


def _is_test_module(path: str) -> bool:
    name = PurePosixPath(path).name
    return name.startswith("test_") and name.endswith(".py") or name.endswith("_test.py")


def _read_test_sources(root: Path, files: Iterable[str]) -> dict[str, tuple[ast.Module, str]]:
    parsed: dict[str, tuple[ast.Module, str]] = {}
    for path in files:
        try:
            text = (root / path).read_text(encoding="utf-8")
            tree = ast.parse(text, filename=path)
        except (OSError, SyntaxError, UnicodeError) as error:
            raise ValueError("tests tree cannot be safely parsed") from error
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            function = node.func
            dynamic = False
            if isinstance(function, ast.Name) and function.id in {"__import__", "import_module", "run_module", "run_path"}:
                dynamic = not node.args or not isinstance(node.args[0], ast.Constant) or not isinstance(node.args[0].value, str)
            if isinstance(function, ast.Attribute) and function.attr in {"import_module", "run_module", "run_path"}:
                dynamic = not node.args or not isinstance(node.args[0], ast.Constant) or not isinstance(node.args[0].value, str)
            if dynamic:
                raise ValueError("dynamic test import cannot be safely resolved")
        parsed[_module_name(path)] = (tree, text)
    return parsed


def _import_names(tree: ast.Module, module: str) -> set[str]:
    found: set[str] = set()
    package = module.rpartition(".")[0]
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                prefix = package
                for _ in range(node.level - 1):
                    prefix = prefix.rpartition(".")[0]
                base = ".".join(item for item in (prefix, node.module or "") if item)
            else:
                base = node.module or ""
            found.add(base)
            found.update(f"{base}.{alias.name}" for alias in node.names if base)
    return found


def tracked_python_files(root: Path) -> list[str]:
    raw = git(root, "ls-files", "-z", "--", "tests/**/*.py", "tests/*.py")
    paths = [path for path in raw.split("\0") if path]
    verified: list[str] = []
    for path in paths:
        if not _valid_test_path(path):
            raise ValueError("git returned an invalid test source path")
        target = root / path
        if target.is_symlink() or not target.is_file():
            raise ValueError("git returned a non-regular test source path")
        verified.append(path)
    return sorted(set(verified), key=lambda value: value.encode())


def _reverse_references(root: Path, changed: Iterable[str]) -> dict[str, set[str]]:
    files = tracked_python_files(root)
    parsed = _read_test_sources(root, files)
    by_module = {_module_name(path): path for path in files}
    references: dict[str, set[str]] = defaultdict(set)
    for path in set(files).union(changed):
        name = PurePosixPath(path)
        references[name.name].add(path)
        references[path].add(path)
        if path.endswith(".py"):
            references[name.stem].add(path)
            module = _module_name(path)
            references[module].add(path)
            if name.stem == "__init__":
                references[module.removesuffix(".__init__")].add(path)
    reverse: dict[str, set[str]] = defaultdict(set)
    for module, (tree, text) in parsed.items():
        source = by_module[module]
        names = _import_names(tree, module)
        names.update(re.findall(r"[\w./-]+", text))
        for name in names:
            for target in references.get(name, ()):
                if target != source:
                    reverse[target].add(source)
    for path in docs_only.regular_files(root, "tests"):
        if path.suffix == ".py":
            continue
        text = path.read_bytes()
        for target in changed:
            if target.encode() in text or PurePosixPath(target).name.encode() in text:
                relative = path.relative_to(root).as_posix()
                if relative != target:
                    reverse[target].add(relative)
    return reverse


def _dependency_closure(seeds: set[str], reverse: dict[str, set[str]]) -> set[str]:
    selected = set(seeds)
    pending = list(seeds)
    while pending:
        current = pending.pop()
        for referrer in reverse.get(current, ()):
            if referrer not in selected:
                selected.add(referrer)
                pending.append(referrer)
    return selected


def standalone_test_modules(root: Path, candidates: list[str]) -> set[str]:
    """Return changed test modules with no reverse test-tree references."""

    if not candidates:
        return set()
    try:
        reverse = _reverse_references(root, tracked_test_files(root))
    except (OSError, ValueError, subprocess.SubprocessError):
        return set()
    return {path for path in candidates if path not in reverse}


def _docs_selection(root: Path, paths: list[str]) -> tuple[set[str], list[str]]:
    docs = [path for path in paths if path.startswith("docs/") and path.endswith(".md")]
    if not docs:
        return set(), []
    try:
        result = docs_only.classify_paths(docs, root=root)
    except (OSError, ValueError, UnicodeError):
        return set(), ["documentation reference scan unavailable"]
    if not result.docs_only:
        return set(), [result.reason]
    return {"tests/tooling/"}, []


def _test_selection(root: Path, paths: list[str], available: list[str]) -> tuple[set[str], list[str]]:
    changed_tests = [path for path in paths if path.startswith("tests/")]
    if not changed_tests:
        return set(), []
    top_dirs = {PurePosixPath(path).parts[1] for path in changed_tests if len(PurePosixPath(path).parts) > 1}
    if not top_dirs <= _ALLOWED_TEST_TOP_DIRS:
        return set(), ["test path is outside the selectable test domains"]
    reverse = _reverse_references(root, changed_tests)
    seeds: set[str] = set()
    for path in changed_tests:
        if path in available and _is_test_module(path) and not reverse.get(path):
            seeds.add(path)
            continue
        if path not in available or not _is_test_module(path):
            seeds.add(path)
        top = PurePosixPath(path).parts[1]
        seeds.update(candidate for candidate in available if PurePosixPath(candidate).parts[1] == top)
    return _dependency_closure(seeds, reverse), []


def _guard_selection(root: Path, paths: list[str], selected: set[str]) -> tuple[set[str], list[str]]:
    if not any(path.endswith(".py") for path in paths):
        return selected, []
    missing = [path for path in ALL_PYTHON_GUARDS if not (root / path).is_file() or (root / path).is_symlink()]
    if missing:
        return set(), [f"Python guard missing: {missing[0]}"]
    return selected | set(ALL_PYTHON_GUARDS), []


def plan_paths(root: Path, paths: Iterable[str], target: str = "dev") -> dict[str, object]:
    changed = list(paths)
    if target == "main" or target.startswith("release/"):
        return full("release gate: main and release targets run full validation")
    try:
        available = tracked_test_files(root)
        if not available:
            return full("tracked test set is empty")
        if any(path in _FULL_FILES or path.startswith(_FULL_PREFIXES) or path.startswith("src/") for path in changed):
            return full("product, fixture, or selector infrastructure changed")
        if any(path.startswith("scripts/") and not path.startswith("scripts/ci/") for path in changed):
            return full("environment script changed")
        if any(path == "scripts/git_environment.py" for path in changed):
            return full("git environment changed")
        selected: set[str] = set()
        reasons: list[str] = []
        docs_selected, docs_reasons = _docs_selection(root, changed)
        if docs_reasons:
            return full(docs_reasons[0])
        selected.update(docs_selected)
        if docs_selected:
            reasons.append("unreferenced documentation maps to tests/tooling")
        test_selected, test_reasons = _test_selection(root, changed, available)
        if test_reasons:
            return full(test_reasons[0])
        selected.update(test_selected)
        if test_selected:
            reasons.append("test-tree change with reverse dependency closure")
        for path in changed:
            if path.startswith("docs/") and path.endswith(".md"):
                continue
            if path.startswith(".github/") or path.startswith("scripts/ci/"):
                if path not in {"scripts/ci/pytest_impact.py", "scripts/ci/docs_only.py"}:
                    selected.update(path for path in available if path.startswith("tests/tooling/"))
                    reasons.append("CI change maps to tests/tooling")
                continue
            if path.startswith("tests/"):
                continue
            return full(f"unmapped change: {path}")
        selected, guard_reasons = _guard_selection(root, changed, selected)
        if guard_reasons:
            return full(guard_reasons[0])
        selected_paths = {
            path
            for candidate in selected
            for path in available
            if candidate == path or candidate.endswith("/") and path.startswith(candidate)
        }
        tests = sorted(selected_paths, key=lambda value: value.encode())
        if not tests:
            return full("selection produced no tracked test files")
        unique_reasons = list(dict.fromkeys(reasons))
        return {
            "mode": "selected",
            "kind": "docs-only" if docs_selected and not test_selected and all(path.startswith("docs/") and path.endswith(".md") for path in changed) else "selected",
            "tests": tests,
            "groups": sorted({path.split("/", 2)[1] for path in tests}),
            "reasons": unique_reasons or ["selected test files"],
            "changedCount": len(changed),
        }
    except (OSError, ValueError, subprocess.SubprocessError, UnicodeError):
        return full("comparison unavailable: fail closed to full suite")


def plan(root: Path, event: object, event_name: str) -> dict[str, object]:
    if event_name != "pull_request":
        return full("non-PR event: full validation")
    if not isinstance(event, dict):
        return full("invalid event payload: full validation")
    pull_request = event.get("pull_request")
    if not isinstance(pull_request, dict):
        return full("missing pull request: full validation")
    base = pull_request.get("base")
    if not isinstance(base, dict):
        return full("missing pull request base: full validation")
    target = base.get("ref")
    if not isinstance(target, str) or not target:
        return full("missing PR base ref: full validation")
    try:
        comparison = docs_only.pull_request_diff(root, event, event_name)
        decision = plan_paths(root, comparison.paths, target)
        if decision["mode"] == "selected":
            decision.update(
                parent1=comparison.parent_sha,
                head=comparison.head_sha,
                mergeCommit=comparison.merge_sha,
            )
        return decision
    except (OSError, ValueError, subprocess.SubprocessError, UnicodeError):
        return full("comparison unavailable: fail closed to full suite")


def parse_shard(value: str) -> tuple[int, int]:
    match = re.fullmatch(r"([1-9][0-9]*)/([1-9][0-9]*)", value)
    if not match or not 1 <= int(match[1]) <= int(match[2]) <= MAX_SHARDS:
        raise argparse.ArgumentTypeError("--emit-shard expects K/N with 1 <= K <= N <= 16")
    return int(match[1]), int(match[2])


def _shard_paths(all_selected: list[str], index: int, count: int, mode: str) -> list[str]:
    if mode == "selected" and len(all_selected) <= SMALL_SELECTION_SHARD1_MAX:
        return all_selected if index == 1 else []
    return [path for position, path in enumerate(all_selected) if position % count == index - 1]


def _selected_paths(decision: dict[str, object]) -> list[str]:
    selected = decision.get("tests")
    if not isinstance(selected, list) or not all(isinstance(path, str) for path in selected):
        raise ValueError("selected plan has no valid test paths")
    return selected


def _write_meta(
    path: Path, decision: dict[str, object], index: int, total: int, shard_count: int, digest: str
) -> None:
    values = {
        "PLAN_MODE": str(decision["mode"]),
        "PLAN_KIND": str(decision["kind"]),
        "PLAN_TOTAL": str(total),
        "SHARD_INDEX": str(index),
        "SHARD_COUNT": str(shard_count),
        "PLAN_DIGEST": digest,
    }
    if any(not re.fullmatch(r"[A-Za-z0-9_-]+", value) for value in values.values()):
        raise ValueError("invalid pytest shard metadata")
    path.write_text("".join(f"{key}={value}\n" for key, value in values.items()), encoding="utf-8")


def emit_shard(root: Path, decision: dict[str, object], index: int, count: int, output: Path, meta: Path) -> int:
    if decision["mode"] == "full":
        all_selected = tracked_test_files(root)
    else:
        all_selected = sorted(set(_selected_paths(decision)), key=lambda value: value.encode())
    if not all_selected:
        raise ValueError("selected test set is empty")
    shard_paths = _shard_paths(all_selected, index, count, str(decision["mode"]))
    if decision["mode"] == "full" and not shard_paths:
        raise ValueError("full shard is empty")
    digest = hashlib.sha256(json.dumps(all_selected, separators=(",", ":")).encode()).hexdigest()
    output.write_text("".join(f"{path}\n" for path in shard_paths), encoding="utf-8")
    _write_meta(meta, decision, index, len(all_selected), len(shard_paths), digest)
    report = {
        "mode": decision["mode"],
        "kind": decision["kind"],
        "total": len(all_selected),
        "shard": index,
        "shards": count,
        "shardCount": len(shard_paths),
        "planDigest": digest,
    }
    print("PYTEST_SHARD " + json.dumps(report, sort_keys=True), flush=True)
    if summary := os.environ.get("GITHUB_STEP_SUMMARY"):
        with Path(summary).open("a", encoding="utf-8") as stream:
            stream.write("\n### Python test selection\n\n```json\n" + json.dumps(report, indent=2) + "\n```\n")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan-only", action="store_true")
    parser.add_argument("--emit-shard", type=parse_shard)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--meta", type=Path)
    args = parser.parse_args(argv)
    try:
        event_path = os.environ.get("GITHUB_EVENT_PATH")
        event = json.loads(Path(event_path).read_text(encoding="utf-8")) if event_path else {}
    except (OSError, ValueError, TypeError):
        event = {}
    decision = plan(ROOT, event, os.environ.get("GITHUB_EVENT_NAME", ""))
    print("PYTEST_IMPACT " + json.dumps(decision, sort_keys=True), flush=True)
    if summary := os.environ.get("GITHUB_STEP_SUMMARY"):
        with Path(summary).open("a", encoding="utf-8") as stream:
            stream.write("\n### Python test selection\n\n```json\n" + json.dumps(decision, indent=2) + "\n```\n")
    if args.plan_only:
        return 0
    if args.emit_shard is None or args.out is None or args.meta is None:
        parser.error("--emit-shard requires --out and --meta")
    index, count = args.emit_shard
    try:
        return emit_shard(ROOT, decision, index, count, args.out, args.meta)
    except (OSError, ValueError, subprocess.SubprocessError):
        print("Unable to emit a valid pytest shard", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
