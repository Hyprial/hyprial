#!/usr/bin/env python3
"""Node net-API gate: connect only through the shared local-IPC module.

Owner rule (2026-11-19): the Node side of hyprial's local daemon IPC only
CONNECTS, and it does so through exactly one shared implementation,
``src/hyprial/daemon/impl/harnesses/pi/resources/pi_local_ipc.ts``
(``resolveLocalIpcEndpoint`` / ``connectLocalIpc``).  Listening is always the
Python kernel wrapper's job.  Therefore ``net.createConnection``,
``net.connect`` and ``net.createServer`` -- through ANY binding of
``node:net`` (namespace import, default import, ``require``, dynamic
``import()``, or a from-imported name) -- are red in every other
``.ts`` / ``.mjs`` / ``.js`` / ``.cjs`` file under ``src/hyprial``.

There is no baseline: a hit is always red, fixed by moving the call into the
shared module (connect) or back into Python (listen).  The vendored GUI
payload (``src/hyprial/_gui``) is generated data, not first-party code.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

SCAN_ROOTS = ("src/hyprial",)
SUFFIXES = frozenset({".ts", ".mjs", ".js", ".cjs"})
EXCLUDED_PARTS = frozenset({"_gui", "__pycache__", "node_modules"})
SHARED_MODULE = "src/hyprial/daemon/impl/harnesses/pi/resources/pi_local_ipc.ts"
FORBIDDEN_MEMBERS = ("createConnection", "connect", "createServer")

NAMESPACE_BINDINGS = tuple(
    re.compile(pattern)
    for pattern in (
        r'import\s+\*\s+as\s+(\w+)\s+from\s+["\']node:net["\']',
        r'import\s+(\w+)\s*,\s*\{[^}]*\}\s*from\s+["\']node:net["\']',
        r'import\s+(\w+)\s+from\s+["\']node:net["\']',
        r'\b(?:const|let|var)\s+(\w+)\s*=\s*await\s+import\(\s*["\']node:net["\']\s*\)',
        r'\b(?:const|let|var)\s+(\w+)\s*=\s*require\(\s*["\']node:net["\']\s*\)',
    )
)
NAMED_IMPORT = re.compile(r'import\s*\{([^}]*)\}\s*from\s+["\']node:net["\']')
INLINE_REQUIRE = re.compile(
    r'require\(\s*["\']node:net["\']\s*\)\s*\.\s*(\w+)'
)


def _line_of(source: str, offset: int) -> int:
    return source.count("\n", 0, offset) + 1


def scan_source(source: str, relative: str) -> list[dict[str, Any]]:
    """Forbidden net-API occurrences in one file; the shared module is exempt."""

    if relative == SHARED_MODULE:
        return []
    namespaces = {
        match.group(1)
        for pattern in NAMESPACE_BINDINGS
        for match in pattern.finditer(source)
    }
    findings: list[dict[str, Any]] = []
    if namespaces:
        dotted = re.compile(
            r"\b(" + "|".join(re.escape(name) for name in namespaces) + r")\s*\.\s*("
            + "|".join(FORBIDDEN_MEMBERS)
            + r")\b"
        )
        for match in dotted.finditer(source):
            findings.append(
                {
                    "path": relative,
                    "line": _line_of(source, match.start()),
                    "symbol": f"{match.group(1)}.{match.group(2)}",
                }
            )
    for match in INLINE_REQUIRE.finditer(source):
        if match.group(1) in FORBIDDEN_MEMBERS:
            findings.append(
                {
                    "path": relative,
                    "line": _line_of(source, match.start()),
                    "symbol": f"require('node:net').{match.group(1)}",
                }
            )
    for match in NAMED_IMPORT.finditer(source):
        clause, line = match.group(1), _line_of(source, match.start())
        for spec in clause.split(","):
            names = re.split(r"\s+as\s+", spec.strip())
            imported = names[0].strip().strip('"\'')
            local = names[-1].strip() if len(names) > 1 else imported
            if imported in FORBIDDEN_MEMBERS:
                findings.append(
                    {"path": relative, "line": line, "symbol": f"import {{{imported} as {local}}}"}
                )
                calls = re.compile(r"\b" + re.escape(local) + r"\s*\(")
                for call in calls.finditer(source):
                    call_line = _line_of(source, call.start())
                    if call_line != line:
                        findings.append(
                            {"path": relative, "line": call_line, "symbol": f"{local}()"}
                        )
    return findings


def _script_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for scan_root in SCAN_ROOTS:
        base = root / scan_root
        if not base.is_dir():
            continue
        for suffix in SUFFIXES:
            files.extend(base.rglob(f"*{suffix}"))
    return sorted(
        path
        for path in files
        if not EXCLUDED_PARTS.intersection(path.relative_to(root).parts)
    )


def scan(root: Path) -> dict[str, Any]:
    root = root.resolve()
    findings: list[dict[str, Any]] = []
    errors: list[str] = []
    files = _script_files(root)
    for path in files:
        relative = path.relative_to(root).as_posix()
        try:
            findings.extend(scan_source(path.read_text(encoding="utf-8"), relative))
        except (OSError, UnicodeError) as error:
            errors.append(f"{relative}: {error}")
    return {
        "status": "FAIL" if findings or errors else "PASS",
        "scanRoots": list(SCAN_ROOTS),
        "filesChecked": len(files),
        "findings": findings,
        "errors": errors,
        "summary": {
            "findings": len(findings),
            "byFile": {
                file: sum(1 for row in findings if row["path"] == file)
                for file in sorted({row["path"] for row in findings})
            },
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--json", action="store_true", help="print the full JSON report")
    args = parser.parse_args(argv)
    report = scan(args.root)
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(
            f"node-net-apis gate: {report['status']} files={report['filesChecked']} "
            f"findings={report['summary']['findings']}"
        )
        for row in report["findings"]:
            print(
                f"  {row['path']}:{row['line']} {row['symbol']}; use the shared module "
                f"{SHARED_MODULE} (connect) or the Python kernel wrapper (listen)"
            )
        for error in report["errors"]:
            print(f"  ERROR {error}")
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
