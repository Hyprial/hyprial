#!/usr/bin/env python3
"""Reject new unbounded blocking calls in daemon-side Python code.

The gate scans daemon, business, and identity code with the standard-library
AST.  Existing debt is recorded in ``unbounded-waits-baseline.json`` and is an
only-out ratchet: entries match normalized calls rather than unstable source
lines, while new calls fail and repaired entries remain red until deleted.

An intentional exception must be declared in the first 20 physical lines of
its file, with a reason, for example::

    # hyprial-unbounded-wait-allow: join - shutdown has already closed all producers

The declaration applies only to the named method in that file.  Inline
suppression is not recognized, and an unused declaration is stale and red.
"""

from __future__ import annotations

import argparse
import ast
from collections import defaultdict, deque
import json
import re
import sys
from pathlib import Path
from typing import Any


SCAN_ROOTS = (
    "src/hyprial/daemon",
    "src/hyprial/biz",
    "src/hyprial/identity",
)
BASELINE_RELATIVE_PATH = "scripts/ci/unbounded-waits-baseline.json"
BASELINE_VERSION = 2
BLOCKING_METHODS = frozenset({"wait", "get", "result", "join", "acquire"})
HEADER_LINES = 20
HEADER_PREFIX = "# hyprial-unbounded-wait-allow:"
HEADER_PATTERN = re.compile(
    r"^\s*#\s*hyprial-unbounded-wait-allow:\s*"
    r"(?P<method>wait|get|result|join|acquire)\s+-\s+(?P<reason>\S(?:.*\S)?)\s*$"
)
BASELINE_FIELDS = frozenset({"path", "line", "method", "call", "reason"})


class GateFormatError(ValueError):
    """A policy input is malformed, so the gate must fail closed."""


def _literal_unbounded(node: ast.AST) -> bool:
    """Return true for literal values that retain an infinite wait."""

    try:
        value = ast.literal_eval(node)
    except (ValueError, TypeError):
        return False
    return (value is None or value == -1) and not isinstance(value, bool)


def _keyword(call: ast.Call, *names: str) -> ast.AST | None:
    for item in call.keywords:
        if item.arg in names:
            return item.value
    return None


def _literal_false(node: ast.AST | None) -> bool:
    return isinstance(node, ast.Constant) and node.value is False


def _has_bounded_timeout(call: ast.Call, method: str) -> bool:
    timeout = _keyword(call, "timeout")
    if timeout is not None:
        return not _literal_unbounded(timeout)

    if method in {"wait", "result", "join"}:
        return bool(call.args) and not _literal_unbounded(call.args[0])

    if method == "acquire":
        blocking = _keyword(call, "blocking", "block")
        if _literal_false(blocking) or (call.args and _literal_false(call.args[0])):
            return True
        return len(call.args) >= 2 and not _literal_unbounded(call.args[1])

    if method == "get":
        blocking = _keyword(call, "block", "blocking")
        if _literal_false(blocking):
            return True
        if call.args and blocking is None:
            # Queue.get(False) is nonblocking; Queue.get(True) is an explicit
            # unbounded wait.  Two positional arguments are (block, timeout).
            # Otherwise preserve mapping.get(key), which is not a queue read.
            positional_block = call.args[0]
            if _literal_false(positional_block):
                return True
            if len(call.args) >= 2:
                if isinstance(positional_block, ast.Constant) and isinstance(
                    positional_block.value, bool
                ):
                    return not _literal_unbounded(call.args[1])
                return True
            if isinstance(positional_block, ast.Constant):
                return positional_block.value is not True
            return True
        return False

    return False


def _call_text(call: ast.Call) -> str:
    try:
        return ast.unparse(call)
    except Exception:
        return f".{getattr(call.func, 'attr', '?')}(...)"


def _blocking_calls(tree: ast.AST, relative: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        method = node.func.attr
        if method not in BLOCKING_METHODS or _has_bounded_timeout(node, method):
            continue
        rows.append(
            {
                "path": relative,
                "line": node.lineno,
                "column": node.col_offset + 1,
                "method": method,
                "call": _call_text(node),
            }
        )
    return sorted(rows, key=lambda row: (row["line"], row["column"], row["method"]))


def _header_allowlist(text: str, relative: str) -> tuple[list[dict[str, Any]], list[str]]:
    entries: list[dict[str, Any]] = []
    errors: list[str] = []
    seen: set[str] = set()
    for line_number, line in enumerate(text.splitlines()[:HEADER_LINES], start=1):
        if "hyprial-unbounded-wait-allow:" not in line:
            continue
        match = HEADER_PATTERN.fullmatch(line)
        if match is None:
            errors.append(
                f"{relative}:{line_number}: malformed header allowlist; expected "
                f"'{HEADER_PREFIX} <method> - <reason>'"
            )
            continue
        method = match.group("method")
        if method in seen:
            errors.append(f"{relative}:{line_number}: duplicate header allowlist for {method}")
            continue
        seen.add(method)
        entries.append(
            {
                "path": relative,
                "line": line_number,
                "method": method,
                "reason": match.group("reason"),
            }
        )
    return entries, errors


def _parse_baseline_entry(raw: object, index: int) -> dict[str, Any]:
    where = f"baseline entry #{index}"
    if not isinstance(raw, dict):
        raise GateFormatError(f"{where} must be an object")
    unknown = sorted(set(raw) - BASELINE_FIELDS)
    missing = sorted(BASELINE_FIELDS - set(raw))
    if unknown or missing:
        raise GateFormatError(f"{where}: unknown={unknown}, missing={missing}")
    path = raw["path"]
    line = raw["line"]
    method = raw["method"]
    call = raw["call"]
    reason = raw["reason"]
    if (
        not isinstance(path, str)
        or not path
        or path.startswith("/")
        or ".." in Path(path).parts
    ):
        raise GateFormatError(f"{where}: invalid repository-relative path {path!r}")
    if isinstance(line, bool) or not isinstance(line, int) or line < 1:
        raise GateFormatError(f"{where}: line must be a positive integer")
    if method not in BLOCKING_METHODS:
        raise GateFormatError(f"{where}: unsupported method {method!r}")
    if not isinstance(call, str) or not call.strip():
        raise GateFormatError(f"{where}: call must be non-empty")
    try:
        call_node = ast.parse(call, mode="eval").body
    except SyntaxError as error:
        raise GateFormatError(f"{where}: call must be a valid expression") from error
    if (
        not isinstance(call_node, ast.Call)
        or not isinstance(call_node.func, ast.Attribute)
        or call_node.func.attr != method
    ):
        raise GateFormatError(f"{where}: call must invoke method {method!r}")
    if not isinstance(reason, str) or not reason.strip():
        raise GateFormatError(f"{where}: reason must be non-empty")
    return {
        "path": path,
        "line": line,
        "method": method,
        "call": _call_text(call_node),
        "reason": reason,
    }


def load_baseline(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise GateFormatError(f"{path}: cannot read valid JSON: {error}") from error
    if not isinstance(document, dict) or set(document) != {"version", "entries"}:
        raise GateFormatError(f"{path}: expected only version and entries")
    if document["version"] != BASELINE_VERSION:
        raise GateFormatError(
            f"{path}: version must be {BASELINE_VERSION}, got {document['version']!r}"
        )
    if not isinstance(document["entries"], list):
        raise GateFormatError(f"{path}: entries must be an array")
    entries = [
        _parse_baseline_entry(raw, index)
        for index, raw in enumerate(document["entries"])
    ]
    seen: set[tuple[str, int, str, str]] = set()
    for entry in entries:
        key = (entry["path"], entry["line"], entry["method"], entry["call"])
        if key in seen:
            raise GateFormatError(f"{path}: duplicate row {key}")
        seen.add(key)
    return entries


def _python_files(root: Path) -> tuple[list[Path], list[str]]:
    paths: list[Path] = []
    errors: list[str] = []
    for relative in SCAN_ROOTS:
        scan_root = root / relative
        if not scan_root.is_dir() or scan_root.is_symlink():
            errors.append(f"missing or invalid scan root: {relative}")
            continue
        for path in scan_root.rglob("*.py"):
            if "__pycache__" not in path.parts and not path.is_symlink():
                paths.append(path)
    return sorted(paths), errors


def scan(
    root: Path,
    baseline: Path | bool | None = None,
) -> dict[str, Any]:
    """Scan one repository tree and apply header and baseline exceptions."""

    root = root.resolve()
    files, root_errors = _python_files(root)
    raw_rows: list[dict[str, Any]] = []
    parse_errors: list[str] = []
    header_errors: list[str] = []
    header_allowed: list[dict[str, Any]] = []
    stale_headers: list[dict[str, Any]] = []

    for path in files:
        relative = path.relative_to(root).as_posix()
        try:
            text = path.read_text(encoding="utf-8")
            tree = ast.parse(text, filename=relative)
        except (OSError, UnicodeError, SyntaxError) as error:
            parse_errors.append(f"{relative}: {error}")
            continue
        rows = _blocking_calls(tree, relative)
        allowances, errors = _header_allowlist(text, relative)
        header_errors.extend(errors)
        allowed_methods = {entry["method"] for entry in allowances}
        used_methods = {row["method"] for row in rows if row["method"] in allowed_methods}
        for entry in allowances:
            if entry["method"] in used_methods:
                matches = sum(1 for row in rows if row["method"] == entry["method"])
                header_allowed.append({**entry, "matches": matches})
            else:
                stale_headers.append(entry)
        raw_rows.extend(row for row in rows if row["method"] not in allowed_methods)

    baseline_entries: list[dict[str, Any]] = []
    baseline_errors: list[str] = []
    if baseline is not False:
        baseline_path = (
            baseline.resolve()
            if isinstance(baseline, Path)
            else root / BASELINE_RELATIVE_PATH
        )
        try:
            baseline_entries = load_baseline(baseline_path)
        except GateFormatError as error:
            baseline_errors.append(str(error))
    else:
        baseline_path = None

    def baseline_key(row: dict[str, Any]) -> tuple[str, str, str]:
        return row["path"], row["method"], row["call"]

    entries_by_key: dict[
        tuple[str, str, str], deque[dict[str, Any]]
    ] = defaultdict(deque)
    for entry in baseline_entries:
        entries_by_key[baseline_key(entry)].append(entry)

    if baseline_errors:
        violations = raw_rows
        grandfathered: list[dict[str, Any]] = []
        stale_baseline: list[dict[str, Any]] = []
    else:
        violations = []
        grandfathered = []
        for row in raw_rows:
            entries = entries_by_key[baseline_key(row)]
            if entries:
                entry = entries.popleft()
                grandfathered.append(
                    {
                        **row,
                        "reason": entry["reason"],
                        "baselineLine": entry["line"],
                    }
                )
            else:
                violations.append(row)
        remaining_entry_ids = {
            id(entry) for entries in entries_by_key.values() for entry in entries
        }
        stale_baseline = [
            entry for entry in baseline_entries if id(entry) in remaining_entry_ids
        ]

    status = "FAIL" if any(
        (
            violations,
            stale_baseline,
            stale_headers,
            parse_errors,
            header_errors,
            baseline_errors,
            root_errors,
        )
    ) else "PASS"
    return {
        "status": status,
        "scanRoots": list(SCAN_ROOTS),
        "filesChecked": len(files),
        "callsChecked": len(raw_rows) + sum(row["matches"] for row in header_allowed),
        "violations": violations,
        "grandfathered": grandfathered,
        "headerAllowed": header_allowed,
        "staleBaseline": stale_baseline,
        "staleHeaderAllowlist": stale_headers,
        "parseErrors": parse_errors,
        "headerErrors": header_errors,
        "baselineErrors": baseline_errors,
        "rootErrors": root_errors,
        "baselinePath": str(baseline_path) if baseline_path is not None else None,
        "summary": {
            "failing": len(violations),
            "grandfathered": len(grandfathered),
            "headerAllowed": sum(row["matches"] for row in header_allowed),
            "staleBaseline": len(stale_baseline),
            "staleHeaderAllowlist": len(stale_headers),
            "errors": len(parse_errors)
            + len(header_errors)
            + len(baseline_errors)
            + len(root_errors),
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--no-baseline",
        action="store_true",
        help="report every finding; intended only for deliberate baseline generation",
    )
    args = parser.parse_args(argv)
    selected_baseline: Path | bool | None
    selected_baseline = False if args.no_baseline else args.baseline
    report = scan(args.root, baseline=selected_baseline)
    text = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8")
    print(text, end="")
    if report["status"] != "PASS":
        print(
            "unbounded-waits: "
            f"failing={report['summary']['failing']} "
            f"grandfathered={report['summary']['grandfathered']} "
            f"stale_baseline={report['summary']['staleBaseline']} "
            f"stale_headers={report['summary']['staleHeaderAllowlist']} "
            f"errors={report['summary']['errors']}",
            file=sys.stderr,
        )
    return int(report["status"] != "PASS")


if __name__ == "__main__":
    raise SystemExit(main())
