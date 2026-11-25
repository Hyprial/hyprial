#!/usr/bin/env python3
"""Fail CI when product/test modules exceed 7 files or code exceeds 1000 lines.

Module = a source-bearing directory below the product and test roots. Count all
direct regular files (including documentation/data) once the directory contains
code; data-only directories are resources, not modules. Third-party and generated
dependencies are not first-party product code. There are no per-file exceptions.
The retired GUI roots (src/gui, app/src, app/src-tauri) were removed from the
gate in Z-7 A3: none exists in the tree, and the live GUI root gui/ is not a
product root of this gate.
Active script/contract/sidecar collections additionally receive the line check;
independent CLI entrypoint collections are not product module directories.
Sidecar directories containing Go source are Go packages, not CLI collections,
and also receive the direct-file limit (including tests, docs and data).

The known violations that predate the gate on the base branch are named in a
baseline file (``scripts/ci/structural-baseline.json``); only the DELTA is red.
The baseline polarity is fixed: entries may only LEAVE.  A repaired violation
leaves a stale entry that is itself red until it is deleted, an entry carries an
expiry date after which it stops grandfathering, and a new violation is red
because no entry covers it.  A violation introduced after the baseline was
recorded (for example the 1004-line ``contract/e2e/_daemon_runner.py`` added by
#1510) has no entry, so it is red: the baseline never absorbs a new violation.
A malformed baseline fails closed and loudly.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, timedelta
from pathlib import Path

PRODUCT_ROOTS = ("src/hyprial",)
TEST_ROOTS = ("tests",)
OPTIONAL_PRODUCT_ROOTS = ("clients/native",)
OPTIONAL_CODE_COLLECTION_ROOTS = ("scripts", "contract", "sidecar")
CODE_SUFFIXES = {".py", ".pyi", ".mjs", ".cjs", ".js", ".ts", ".tsx", ".jsx", ".css", ".scss", ".html", ".sh", ".ps1", ".rs", ".go"}
EXCLUDED_DIRECTORIES = {".git", ".venv", "node_modules", "__pycache__"}
# Known dependency/release payloads, not arbitrary names that can hide new code.
EXCLUDED_PATHS = {
    "src/hyprial/_gui",
    "clients/native/target",
    "clients/native/src-tauri/target",
    "clients/native/src-tauri/gen",
    "clients/native/src-tauri/runtime",
}
HISTORICAL_PATHS = {"scripts/archived"}
MAX_MODULE_FILES = 7
MAX_CODE_LINES = 1000

# --- baseline (only-out ratchet exemptions) ----------------------------------
#
# TWO violations predate this gate on dev, so pr-hygiene's merge-tree rule
# (which runs this script) was permanently RED and a NEW violation was
# indistinguishable from the known ones.  The baseline names the known
# violations so that only the DELTA is red.  #1510's 1004-line
# contract/e2e/_daemon_runner.py is exactly such a NEW violation: it has no
# entry and MUST stay reported red, never absorbed into this file.
#
# Polarity, fixed and not negotiable: entries may only LEAVE.  Adding an entry
# is a review decision recorded in the diff, never a silent escape; once the
# underlying violation is repaired the stale entry is red until it is deleted;
# an entry also expires, so nothing is grandfathered forever by default.
BASELINE_RELATIVE_PATH = "scripts/ci/structural-baseline.json"
BASELINE_VERSION = 1
BASELINE_DEFAULT_DAYS = 14
# Only violations that carry a measured `actual` and a `limit` can be
# grandfathered: a missing root or a symlink is a structural defect, not a
# budget to spend.
BASELINE_KINDS = frozenset({"code-line-count", "module-file-count"})
BASELINE_ENTRY_FIELDS = frozenset(
    {"kind", "path", "actual", "limit", "fix_pr", "owner", "recorded", "expires", "reason"}
)


class BaselineFormatError(ValueError):
    """The baseline file cannot be read or validated: fail closed, never skip."""


def _baseline_date(value: object, field: str, where: str) -> date:
    if not isinstance(value, str):
        raise BaselineFormatError(
            f"{where}: {field} must be an ISO date string (e.g. 2026-10-30), got {value!r}"
        )
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise BaselineFormatError(f"{where}: {field}={value!r} is not an ISO date: {error}") from error


def _parse_baseline_entry(raw: object, index: int) -> dict:
    where = f"baseline entry #{index}"
    if not isinstance(raw, dict):
        raise BaselineFormatError(f"{where} must be a JSON object, got {type(raw).__name__}")
    unknown = sorted(set(raw) - BASELINE_ENTRY_FIELDS)
    if unknown:
        raise BaselineFormatError(
            f"{where}: unknown field(s) {unknown}; allowed {sorted(BASELINE_ENTRY_FIELDS)}"
        )
    kind = raw.get("kind")
    if kind not in BASELINE_KINDS:
        raise BaselineFormatError(f"{where}: kind must be one of {sorted(BASELINE_KINDS)}, got {kind!r}")
    path = raw.get("path")
    if not isinstance(path, str) or not path or path.startswith("/") or ".." in Path(path).parts:
        raise BaselineFormatError(
            f"{where}: path must be a non-empty repository-relative path, got {path!r}"
        )
    actual = raw.get("actual")
    limit = raw.get("limit")
    for name, value in (("actual", actual), ("limit", limit)):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise BaselineFormatError(f"{where}: {name} must be a non-negative integer, got {value!r}")
    if actual <= limit:
        raise BaselineFormatError(
            f"{where}: actual={actual} is not above limit={limit}; a baseline entry only grandfathers an existing violation"
        )
    fix_pr = raw.get("fix_pr")
    owner = raw.get("owner")
    if fix_pr is not None and (not isinstance(fix_pr, str) or not fix_pr.strip()):
        raise BaselineFormatError(f"{where}: fix_pr must be null or a non-empty string, got {fix_pr!r}")
    if owner is not None and (not isinstance(owner, str) or not owner.strip()):
        raise BaselineFormatError(f"{where}: owner must be null or a non-empty string, got {owner!r}")
    if fix_pr is None and owner is None:
        raise BaselineFormatError(f"{where}: fix_pr is null, so owner is required")
    reason = raw.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        raise BaselineFormatError(f"{where}: reason must be a non-empty string")
    recorded = raw.get("recorded")
    recorded_date = _baseline_date(recorded, "recorded", where) if recorded is not None else None
    expires = raw.get("expires")
    if expires is not None:
        expires_date = _baseline_date(expires, "expires", where)
    elif recorded_date is not None:
        expires_date = recorded_date + timedelta(days=BASELINE_DEFAULT_DAYS)
    else:
        raise BaselineFormatError(
            f"{where}: needs expires (ISO date) or recorded (defaults to recorded + {BASELINE_DEFAULT_DAYS} days)"
        )
    return {
        "kind": kind,
        "path": path,
        "actual": actual,
        "limit": limit,
        "fix_pr": fix_pr,
        "owner": owner,
        "recorded": recorded_date,
        "expires": expires_date,
        "reason": reason,
    }


def load_baseline(path: Path) -> list[dict]:
    """Read and validate the baseline.  A missing file is an empty baseline."""

    if not path.exists():
        return []
    if path.is_dir():
        raise BaselineFormatError(f"{path}: baseline path is a directory, not a file")
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as error:
        raise BaselineFormatError(f"{path}: cannot read baseline: {error}") from error
    try:
        document = json.loads(text)
    except json.JSONDecodeError as error:
        raise BaselineFormatError(f"{path}: not valid JSON: {error}") from error
    if not isinstance(document, dict):
        raise BaselineFormatError(f"{path}: top level must be a JSON object")
    unknown = sorted(set(document) - {"version", "polarity", "entries"})
    if unknown:
        raise BaselineFormatError(f"{path}: unknown top-level field(s) {unknown}")
    if document.get("version") != BASELINE_VERSION:
        raise BaselineFormatError(
            f"{path}: version must be {BASELINE_VERSION}, got {document.get('version')!r}"
        )
    raw_entries = document.get("entries")
    if not isinstance(raw_entries, list):
        raise BaselineFormatError(f"{path}: entries must be a JSON array")
    entries = [_parse_baseline_entry(raw, index) for index, raw in enumerate(raw_entries)]
    seen: set[tuple[str, str]] = set()
    for entry in entries:
        key = (entry["kind"], entry["path"])
        if key in seen:
            raise BaselineFormatError(f"{path}: duplicate entry for {key[0]} {key[1]}")
        seen.add(key)
    return entries


def _stale_message(entry: dict, issues: list[str]) -> str:
    parts = []
    if "repaired" in issues:
        parts.append(
            "the violation this entry grandfathers no longer exists; delete the entry "
            "(the baseline may only shrink)"
        )
    if "expired" in issues:
        parts.append(
            f"the entry expired {entry['expires'].isoformat()}; re-record it deliberately or fix the violation"
        )
    return f"{entry['kind']} {entry['path']}: " + "; ".join(parts)


def apply_baseline(violations: list[dict], entries: list[dict], today: date) -> tuple[list[dict], list[dict], list[dict]]:
    """Split measured violations into failing, grandfathered and stale entries.

    ``failing`` is the exit-code input: a new violation, a violation worse than
    its recorded actual, or a violation whose entry expired.  ``stale`` is the
    only-out ratchet: an entry whose violation was repaired, and/or an entry
    past its expiry.
    """

    failing: list[dict] = []
    grandfathered: list[dict] = []
    stale: list[dict] = []
    by_key = {(entry["kind"], entry["path"]): entry for entry in entries}
    matched: set[tuple[str, str]] = set()
    for violation in violations:
        key = (violation["kind"], violation["path"])
        entry = by_key.get(key)
        if entry is None:
            failing.append(violation)
            continue
        matched.add(key)
        if entry["expires"] < today:
            failing.append({**violation, "baselineExpired": entry["expires"].isoformat()})
            continue
        if violation["actual"] <= entry["actual"]:
            grandfathered.append(
                {
                    **violation,
                    "recorded": entry["actual"],
                    "fix_pr": entry["fix_pr"],
                    "owner": entry["owner"],
                    "expires": entry["expires"].isoformat(),
                    "reason": entry["reason"],
                }
            )
        else:
            failing.append({**violation, "baselineActual": entry["actual"]})
    for entry in entries:
        key = (entry["kind"], entry["path"])
        issues = []
        if key not in matched:
            issues.append("repaired")
        if entry["expires"] < today:
            issues.append("expired")
        if issues:
            stale.append(
                {
                    "kind": entry["kind"],
                    "path": entry["path"],
                    "actual": entry["actual"],
                    "limit": entry["limit"],
                    "expires": entry["expires"].isoformat(),
                    "issues": issues,
                    "message": _stale_message(entry, issues),
                }
            )
    return failing, grandfathered, stale


def _scan_tree(root: Path) -> dict:
    violations: list[dict] = []
    files_checked = 0
    modules_checked = 0
    max_lines = 0
    max_files = 0

    def visit(directory: Path, *, module_limits: bool = True, go_package_limits: bool = False) -> None:
        nonlocal files_checked, modules_checked, max_lines, max_files
        entries = sorted(directory.iterdir())
        for entry in entries:
            if entry.is_symlink():
                violations.append({"kind": "source-symlink", "path": entry.relative_to(root).as_posix()})
        files = [entry for entry in entries if not entry.is_symlink() and entry.is_file()]
        code = [entry for entry in files if entry.suffix in CODE_SUFFIXES]
        is_module = module_limits or (go_package_limits and any(path.suffix == ".go" for path in code))
        if code and is_module:
            modules_checked += 1
            max_files = max(max_files, len(files))
            if len(files) > MAX_MODULE_FILES:
                violations.append({"kind": "module-file-count", "path": directory.relative_to(root).as_posix(), "actual": len(files), "limit": MAX_MODULE_FILES})
        for path in code:
            files_checked += 1
            lines = len(path.read_bytes().splitlines())
            max_lines = max(max_lines, lines)
            if lines > MAX_CODE_LINES:
                violations.append({"kind": "code-line-count", "path": path.relative_to(root).as_posix(), "actual": lines, "limit": MAX_CODE_LINES})
        for entry in entries:
            if not entry.is_symlink() and entry.is_dir() and entry.name not in EXCLUDED_DIRECTORIES and entry.relative_to(root).as_posix() not in EXCLUDED_PATHS | HISTORICAL_PATHS:
                visit(entry, module_limits=module_limits, go_package_limits=go_package_limits)

    for name in PRODUCT_ROOTS:
        path = root / name
        if not path.is_dir() or path.is_symlink():
            violations.append({"kind": "missing-product-root", "path": name})
        else:
            visit(path)
    for name in TEST_ROOTS:
        path = root / name
        if not path.is_dir() or path.is_symlink():
            violations.append({"kind": "missing-test-root", "path": name})
        else:
            visit(path)
    for name in OPTIONAL_PRODUCT_ROOTS:
        path = root / name
        if path.is_symlink():
            violations.append({"kind": "source-symlink", "path": name})
        elif path.exists():
            if not path.is_dir():
                violations.append({"kind": "invalid-product-root", "path": name})
            else:
                visit(path)
    for name in OPTIONAL_CODE_COLLECTION_ROOTS:
        path = root / name
        if path.is_symlink():
            violations.append({"kind": "source-symlink", "path": name})
        elif path.exists():
            if not path.is_dir():
                violations.append({"kind": "invalid-code-collection-root", "path": name})
            else:
                visit(path, module_limits=False, go_package_limits=name == "sidecar")
    if not files_checked:
        violations.append({"kind": "zero-code-coverage", "path": "."})
    return {"status": "FAIL" if violations else "PASS", "productRoots": PRODUCT_ROOTS, "testRoots": TEST_ROOTS, "optionalProductRoots": OPTIONAL_PRODUCT_ROOTS, "optionalLineOnlyRoots": OPTIONAL_CODE_COLLECTION_ROOTS, "goPackageRoots": ("sidecar",), "filesChecked": files_checked, "modulesChecked": modules_checked, "maxCodeLines": max_lines, "maxDirectFiles": max_files, "limits": {"moduleFiles": MAX_MODULE_FILES, "codeLines": MAX_CODE_LINES}, "excludedDependencyDirectories": sorted(EXCLUDED_DIRECTORIES), "excludedPayloadPaths": sorted(EXCLUDED_PATHS), "historicalPaths": sorted(HISTORICAL_PATHS), "violations": violations}


def scan(root: Path, baseline: Path | None = None, today: date | None = None) -> dict:
    """Structural scan plus the only-out baseline judgement.

    ``violations`` keeps only the FAILING items (new, worse than recorded, or
    expired), so an existing consumer that reads it never sees a grandfathered
    entry.  ``grandfathered``, ``stale_baseline`` and ``baselineErrors`` carry
    the ratchet's own verdicts.  A malformed baseline fails closed: it
    grandfathers nothing and every measured violation is reported.
    """

    report = _scan_tree(root)
    resolved = baseline if baseline is not None else root / BASELINE_RELATIVE_PATH
    entries: list[dict] = []
    baseline_errors: list[str] = []
    try:
        entries = load_baseline(resolved)
    except BaselineFormatError as error:
        baseline_errors.append(str(error))
    if baseline_errors:
        failing = list(report["violations"])
        grandfathered: list[dict] = []
        stale: list[dict] = []
    else:
        failing, grandfathered, stale = apply_baseline(
            report["violations"], entries, today or date.today()
        )
    report["violations"] = failing
    report["grandfathered"] = grandfathered
    report["stale_baseline"] = stale
    report["baselineErrors"] = baseline_errors
    # The counters keep the two populations apart on purpose: the exit code and
    # the first number are the NEW/failing items, while grandfathered is the
    # existing debt the baseline still carries.  Never collapse them into one
    # total.
    report["summary"] = {
        "failing": len(failing),
        "grandfathered": len(grandfathered),
        "staleBaseline": len(stale),
        "baselineErrors": len(baseline_errors),
    }
    report["baselinePath"] = str(resolved) if resolved.exists() else None
    report["baselineEntries"] = len(entries)
    report["status"] = "FAIL" if failing or stale or baseline_errors else "PASS"
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--baseline", type=Path, help=f"baseline file (default: <root>/{BASELINE_RELATIVE_PATH})")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    baseline = args.baseline.resolve() if args.baseline else None
    report = scan(args.root.resolve(), baseline=baseline)
    text = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text)
    print(text, end="")
    if report["violations"] or report["stale_baseline"] or report["baselineErrors"]:
        print(
            "structural-limits: "
            f"failing={len(report['violations'])} "
            f"grandfathered={len(report['grandfathered'])} "
            f"stale_baseline={len(report['stale_baseline'])} "
            f"baseline_errors={len(report['baselineErrors'])}",
            file=sys.stderr,
        )
    for error in report["baselineErrors"]:
        print(f"structural-limits baseline error: {error}", file=sys.stderr)
    return int(report["status"] != "PASS")


if __name__ == "__main__":
    raise SystemExit(main())
