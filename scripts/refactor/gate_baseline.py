#!/usr/bin/env python3
"""Capture existing static authority manifest validation before any relocation."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import sys
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    root, output = args.root.resolve(strict=True), args.output.resolve()
    if output.exists() or (
        output.is_relative_to(root)
        and not output.is_relative_to(root / "docs" / "evidence")
    ):
        parser.error("output must be a new evidence file or outside the checkout")
    rows = []
    for module_name, manifest_name, validator_name in (
        (
            "actor_boundary_gate",
            "actor_boundary_exemptions",
            "validate_legacy_manifest",
        ),
        (
            "supervision_boundary_gate",
            "supervision_exemptions",
            "validate_supervision_manifest",
        ),
        ("inbox_authority_gate", "inbox_authority_exemptions", "validate_manifest"),
    ):
        path = root / "tests" / f"{module_name}.py"
        spec = importlib.util.spec_from_file_location(module_name, path)
        if spec is None or spec.loader is None:
            raise RuntimeError(f"cannot load static gate: {path}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
        manifest = root / "tests" / f"{manifest_name}.json"
        data = manifest.read_bytes()
        errors = getattr(module, validator_name)(root / "src" / "hyprial", manifest)
        rows.append(
            {
                "gate": module_name,
                "gate_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "manifest": manifest.relative_to(root).as_posix(),
                "manifest_sha256": hashlib.sha256(data).hexdigest(),
                "exemptions": len(json.loads(data)["exemptions"]),
                "errors": errors,
            }
        )
    result = {
        "schema_version": 1,
        "checks": rows,
        "limitation": "Only the three existing static manifest validators ran; this is not a full test suite result.",
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    print(
        json.dumps(
            {
                row["gate"]: {
                    "exemptions": row["exemptions"],
                    "errors": len(row["errors"]),
                }
                for row in rows
            },
            sort_keys=True,
        )
    )
    return int(any(row["errors"] for row in rows))


if __name__ == "__main__":
    raise SystemExit(main())
