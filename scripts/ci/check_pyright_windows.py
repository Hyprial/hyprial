#!/usr/bin/env python3
"""Incremental pyright gate: no new Windows-missing stdlib attributes (only-out).

Owner 2026-11-21 (W1B): the AST gate misses POSIX-only attributes reached
through types; pyright's typeshed knows which ``os``/``signal``/``fcntl``/
``socket``/``select``/``pwd``/``termios``/``resource`` members do not exist on
Windows.  The repository keeps no pyright configuration or lock entry: pyright
is fetched pinned (``uvx --from pyright==PYRIGHT_VERSION``), so ``pyproject.toml``
and ``uv.lock`` stay untouched.

Only Python files the change touches under ``src/hyprial`` (added or modified
between the base and ``HEAD``; the kernel ``PLATFORM_SEAMS`` of
``check_windows_portability.py`` and the vendored GUI excluded) are checked, with
``--pythonplatform Windows``.  Only ``reportAttributeAccessIssue`` diagnostics of
the form ``"X" is not a known attribute of module "M"`` with ``M`` (root) in
``MODULES`` count.  The same files are checked in a snapshot of the base
(``git archive``), and the comparison is a count map keyed by ``(path, module,
attribute)``: a key whose count rises is red, lines may move freely, a fixed
finding just leaves.  The base is ``merge-base(--base-ref, HEAD)`` when the
history has it, else ``--base-ref`` itself (reported as ``baseMode``).  A file
that is new or renamed counts from zero.  Pyright failing to run, or exiting
with a fatal code (2+), fails closed.

Guards pyright cannot narrow (``hasattr(os, "fork")``, ``try/except
AttributeError``) still report; only the rise is red, so existing guarded code
does not block, and new code should use a kernel seam instead.
"""

from __future__ import annotations

import argparse
import io
import json
import re
import shlex
import subprocess
import sys
import tarfile
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any

try:
    from check_windows_portability import PLATFORM_SEAMS as PLATFORM_SEAMS, in_seam
except ModuleNotFoundError:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from check_windows_portability import PLATFORM_SEAMS as PLATFORM_SEAMS, in_seam

PYRIGHT_VERSION = "1.1.407"
DEFAULT_PYRIGHT = f"uvx --from pyright=={PYRIGHT_VERSION} pyright"
SCAN_ROOT = "src/hyprial/"
EXCLUDED_PARTS = frozenset({"_gui", "__pycache__"})
MODULES = frozenset({"os", "signal", "fcntl", "socket", "select", "pwd", "termios", "resource"})
RULE = "reportAttributeAccessIssue"
MESSAGE = re.compile(r'^"(?P<attr>[^"]+)" is not a known attribute of module "(?P<module>[\w.]+)"')
FATAL_EXIT = 2


class GateError(RuntimeError):
    """The comparison cannot be made: fail closed, never skip."""


def _git(root: Path, *args: str, binary: bool = False) -> subprocess.CompletedProcess[Any]:
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=not binary, check=False)


def resolve_base(root: Path, ref: str) -> tuple[str, str]:
    """``(commit, mode)``: the merge-base when reachable, else the ref's own commit."""

    merge_base = _git(root, "merge-base", ref, "HEAD")
    if merge_base.returncode == 0 and merge_base.stdout.strip():
        return merge_base.stdout.strip(), "merge-base"
    commit = _git(root, "rev-parse", "--verify", "-q", f"{ref}^{{commit}}")
    if commit.returncode != 0:
        raise GateError(f"base ref {ref!r} is not available (fetch it first)")
    return commit.stdout.strip(), "base-tip"


def checked(relative: str) -> bool:
    return (
        relative.startswith(SCAN_ROOT) and relative.endswith(".py") and not in_seam(relative)
        and not EXCLUDED_PARTS.intersection(relative.split("/"))
    )


def changed_files(root: Path, base: str) -> list[str]:
    """Added or modified product files between ``base`` and ``HEAD`` (renames count as added)."""

    diff = _git(root, "diff", "--name-only", "--no-renames", "--diff-filter=AM", base, "HEAD", "--", SCAN_ROOT)
    if diff.returncode != 0:
        raise GateError(f"git diff {base}..HEAD failed: {diff.stderr.strip()}")
    return sorted(path for path in diff.stdout.splitlines() if checked(path) and (root / path).is_file())


def files_in(root: Path, commit: str, files: list[str]) -> list[str]:
    if not files:
        return []
    listed = _git(root, "ls-tree", "-r", "--name-only", commit, "--", *files)
    if listed.returncode != 0:
        raise GateError(f"git ls-tree {commit} failed: {listed.stderr.strip()}")
    return sorted(set(listed.stdout.splitlines()) & set(files))


def snapshot(root: Path, commit: str, destination: Path) -> None:
    """The base's ``src/hyprial`` tree, so its imports resolve against itself."""

    archive = _git(root, "archive", "--format=tar", commit, "--", SCAN_ROOT.rstrip("/"), binary=True)
    if archive.returncode != 0:
        raise GateError(f"git archive {commit} failed: {archive.stderr.decode(errors='replace').strip()}")
    with tarfile.open(fileobj=io.BytesIO(archive.stdout)) as tar:
        tar.extractall(destination, filter="data")


def windows_attribute_findings(report: dict[str, Any], cwd: Path) -> list[dict[str, Any]]:
    """The counted subset of one pyright ``--outputjson`` report, paths relative to ``cwd``."""

    findings = []
    for diagnostic in report.get("generalDiagnostics", []):
        match = MESSAGE.match(diagnostic.get("message", ""))
        if diagnostic.get("rule") != RULE or not match or match["module"].split(".")[0] not in MODULES:
            continue
        path = Path(diagnostic.get("file", ""))
        relative = (path.relative_to(cwd) if path.is_absolute() else path).as_posix()
        findings.append(
            {"path": relative, "module": match["module"], "attribute": match["attr"],
             "line": diagnostic.get("range", {}).get("start", {}).get("line", -1) + 1}
        )
    return findings


def run_pyright(command: str, cwd: Path, files: list[str], python: str) -> list[dict[str, Any]]:
    if not files:
        return []
    argv = [*shlex.split(command), "--outputjson", "--pythonplatform", "Windows", "--pythonversion", "3.12",
            "--pythonpath", python, *files]
    try:
        completed = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, check=False)
    except OSError as error:
        raise GateError(f"cannot run pyright ({command}): {error}") from error
    if completed.returncode >= FATAL_EXIT:
        raise GateError(f"pyright exited {completed.returncode}: {(completed.stderr or completed.stdout)[-2000:]}")
    try:
        report = json.loads(completed.stdout)
    except ValueError as error:
        raise GateError(f"pyright output is not JSON: {completed.stdout[-2000:]}") from error
    if not isinstance(report, dict):
        raise GateError(f"pyright output is not a JSON report: {completed.stdout[-2000:]}")
    return windows_attribute_findings(report, cwd.resolve())


def compare(base: list[dict[str, Any]], head: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keys ``(path, module, attribute)`` whose count rose from base to head."""

    def key(row: dict[str, Any]) -> tuple[str, str, str]:
        return row["path"], row["module"], row["attribute"]

    before, after = Counter(map(key, base)), Counter(map(key, head))
    return [
        {"path": k[0], "symbol": f"{k[1]}.{k[2]}", "count": count, "base": before.get(k, 0),
         "lines": sorted(row["line"] for row in head if key(row) == k)}
        for k, count in sorted(after.items()) if count > before.get(k, 0)
    ]


def check(root: Path, base_ref: str, pyright: str = DEFAULT_PYRIGHT, python: str = sys.executable,
          files: list[str] | None = None) -> dict[str, Any]:
    root = root.resolve()
    report: dict[str, Any] = {"status": "FAIL", "baseRef": base_ref, "files": [], "newFindings": [], "errors": []}
    try:
        base, mode = resolve_base(root, base_ref)
        report.update(base=base, baseMode=mode)
        targets = sorted(path for path in files if checked(path)) if files is not None else changed_files(root, base)
        report["files"] = targets
        head = run_pyright(pyright, root, targets, python)
        in_base = files_in(root, base, targets)
        before: list[dict[str, Any]] = []
        if in_base:
            with tempfile.TemporaryDirectory(prefix="pyright-windows-base-") as directory:
                snapshot(root, base, Path(directory))
                before = run_pyright(pyright, Path(directory), in_base, python)
        report.update(headFindings=len(head), baseFindings=len(before), newFindings=compare(before, head))
    except GateError as error:
        report["errors"].append(str(error))
    report["status"] = "FAIL" if report["errors"] or report["newFindings"] else "PASS"
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--base-ref", required=True, help="git ref of the PR base")
    parser.add_argument("--pyright", default=DEFAULT_PYRIGHT, help=f"pyright command (default: {DEFAULT_PYRIGHT})")
    parser.add_argument("--python", default=sys.executable, help="interpreter whose site-packages pyright resolves")
    parser.add_argument("--files", nargs="*", help="check these files instead of the change set")
    parser.add_argument("--json", action="store_true", help="print the full JSON report")
    args = parser.parse_args(argv)
    report = check(args.root, args.base_ref, args.pyright, args.python, args.files)
    if args.json:
        print(json.dumps(report, indent=2))
        return 0 if report["status"] == "PASS" else 1
    print(
        f"pyright-windows gate: {report['status']} base={report.get('base', '?')} ({report.get('baseMode', '?')}) "
        f"files={len(report['files'])} head={report.get('headFindings', 0)} base_findings={report.get('baseFindings', 0)} "
        f"new={len(report['newFindings'])}"
    )
    for row in report["newFindings"]:
        print(f"  NEW {row['path']} {row['symbol']} ({row['count']} > {row['base']}) lines {row['lines']}; "
              "use a kernel seam or a cross-platform API instead")
    for error in report["errors"]:
        print(f"  ERROR {error}")
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
