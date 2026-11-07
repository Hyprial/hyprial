#!/usr/bin/env python3
"""Measure parser coverage and LibCST round-trip fidelity without importing product code."""

from __future__ import annotations

import argparse
import ast
import hashlib
import io
import json
import subprocess
import sys
import tokenize
from collections import Counter
from importlib.metadata import version
from pathlib import Path


def scope(path: str) -> str:
    if path.startswith("src/hyprial/"):
        return "product"
    if path.startswith(("notes/", "docs/notes/")):
        return "history"
    return path.split("/", 1)[0]


def probe(root: Path, compare_bowler: bool = False) -> dict[str, object]:
    import libcst

    bowler_parser = None
    if compare_bowler:
        from fissix.refactor import RefactoringTool

        # Bowler Query(python_version=3), its default, passes this option.
        bowler_parser = RefactoringTool([], options={"print_function": True})
    raw = subprocess.check_output(["git", "-C", str(root), "ls-files", "-z"])
    paths = sorted({p for p in raw.decode().split("\0") if p.endswith(".py")})
    rows: list[dict[str, object]] = []
    for relative in paths:
        path = root / relative
        row: dict[str, object] = {"path": relative, "scope": scope(relative)}
        if not path.is_file() or path.is_symlink():
            row["input_error"] = "missing, non-regular, or symlink input"
            rows.append(row)
            continue
        data = path.read_bytes()
        row["sha256"] = hashlib.sha256(data).hexdigest()
        try:
            encoding, _ = tokenize.detect_encoding(io.BytesIO(data).readline)
            text = data.decode(encoding)
        except (SyntaxError, UnicodeError) as error:
            row["input_error"] = f"{type(error).__name__}: {error}"
            rows.append(row)
            continue
        for name in ("ast_py312", "libcst"):
            try:
                if name == "ast_py312":
                    ast.parse(text, filename=relative, feature_version=(3, 12))
                    row[name] = "ok"
                else:
                    generated = libcst.parse_module(data).bytes
                    row[name] = "ok" if generated == data else "round_trip_mismatch"
            except Exception as error:
                row[name] = (
                    f"{type(error).__name__}: {str(error).splitlines()[0][:160]}"
                )
        if bowler_parser is not None:
            try:
                # The Bowler/Fissix parser requires a terminal newline.
                bowler_parser.refactor_string(
                    text if text.endswith("\n") else text + "\n", relative
                )
                row["bowler_fissix"] = "ok"
            except Exception as error:
                row["bowler_fissix"] = (
                    f"{type(error).__name__}: {str(error).splitlines()[0][:160]}"
                )
        rows.append(row)
    parsers = ["ast_py312", "libcst"] + (["bowler_fissix"] if compare_bowler else [])
    summaries: dict[str, dict[str, object]] = {}
    for name in parsers:
        successes = Counter(str(r["scope"]) for r in rows if r.get(name) == "ok")
        failures = Counter(str(r["scope"]) for r in rows if r.get(name) != "ok")
        summaries[name] = {
            "ok": sum(successes.values()),
            "failed": sum(failures.values()),
            "ok_by_scope": dict(sorted(successes.items())),
            "failed_by_scope": dict(sorted(failures.items())),
        }
    versions = {"libcst": version("libcst")}
    if compare_bowler:
        versions.update({name: version(name) for name in ("bowler", "fissix")})
    return {
        "schema_version": 1,
        "head": subprocess.check_output(
            ["git", "-C", str(root), "rev-parse", "HEAD"], text=True
        ).strip(),
        "python": sys.version.split()[0],
        "versions": versions,
        "bowler_options": {"python_version": 3, "print_function": True}
        if compare_bowler
        else None,
        "python_files": len(paths),
        "summary": summaries,
        "files": rows,
        "limitations": [
            "Parsing and byte round-trip checks do not establish transformation correctness.",
            "Historical Python inputs may intentionally contain invalid or retired syntax.",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--compare-bowler", action="store_true")
    args = parser.parse_args()
    root = args.root.resolve(strict=True)
    output = args.output.resolve()
    if (
        output == root
        or output.is_relative_to(root / ".git")
        or (
            output.is_relative_to(root)
            and not output.is_relative_to(root / "docs" / "evidence")
        )
    ):
        parser.error("output must be outside the checkout or under docs/evidence")
    if output.exists():
        parser.error("output already exists; choose a new evidence file")
    result = probe(root, args.compare_bowler)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    )
    print(
        json.dumps(
            {k: result[k] for k in ("python_files", "versions", "summary")},
            sort_keys=True,
        )
    )
    return int(
        any(
            r["scope"] == "product" and r.get("libcst") != "ok" for r in result["files"]
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
