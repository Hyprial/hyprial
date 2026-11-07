"""Strict manifest validation for the staged refactor toolchain.

The validator is the authoritative gate used by plan/apply; the JSON Schema
document (``manifest.schema.json``) mirrors it for external tooling. It
checks structure, cross-field consistency, per-kind requirements, approval
status semantics and cross-rule conflicts (duplicate rule ids, conflicting
facade exports, unknown stage verification references).
"""

from __future__ import annotations

import json
from pathlib import Path

from .transforms.common import STAGES, ToolError

REQUIRED_COMMON = ("rule_id", "kind", "stage", "status", "expected_matches", "reason")

KIND_FIELDS = {
    "import_module": ("old_module", "new_module"),
    "import_symbol": ("old_module", "new_module", "old_symbol", "new_symbol"),
    "symbol_extract": ("source_path", "source_sha256", "symbols", "target_path", "dependencies"),
    "class_member_extract": (
        "source_path",
        "source_sha256",
        "class_name",
        "symbols",
        "new_class_name",
        "target_path",
        "dependencies",
        "wiring",
    ),
    "facade_export": ("facade_path", "facade_module", "provider_module", "symbol"),
    "reference_rule": ("source_path", "format", "operations"),
    "layout_move": ("moves",),
}

REFERENCE_OPS = {
    "module_string",
    "parents_depth_shift",
    "json_field",
    "json_entry_path",
    "toml_key",
    "text_span",
}
REFERENCE_OP_FIELDS = {
    "module_string": ("old", "new"),
    "parents_depth_shift": ("delta",),
    "json_field": ("field", "old", "new"),
    "json_entry_path": ("match_path", "new_path"),
    "toml_key": ("key", "old", "new"),
    "text_span": ("old", "new"),
}
DEP_PROVISIONS = {"import", "companion", "wrapper_param", "field", "host", "class_attr"}
CHECK_NAMES = {
    "boundary_direction",
    "public_face_gate",
    "authority_manifests",
    "resource_present",
    "command",
}


def validate_manifest(manifest: dict, require_approved: bool = False) -> list[str]:
    """Return a list of validation errors; empty means valid."""
    errors: list[str] = []

    def err(message: str) -> None:
        errors.append(message)

    if not isinstance(manifest, dict):
        return ["manifest must be a JSON object"]
    if manifest.get("schema_version") != 1:
        err("schema_version must be 1")
    commit = manifest.get("baseline_commit")
    if not isinstance(commit, str) or len(commit) != 40 or any(
        c not in "0123456789abcdef" for c in commit
    ):
        err("baseline_commit must be a 40-char lowercase hex sha")
    rules = manifest.get("rules")
    if not isinstance(rules, list) or not rules:
        err("rules must be a non-empty array")
        return errors

    seen_ids: set[str] = set()
    rule_stages: set[str] = set()
    facade_exports: dict[str, tuple[str, str]] = {}
    for index, rule in enumerate(rules):
        where = f"rules[{index}]"
        if not isinstance(rule, dict):
            err(f"{where}: rule must be an object")
            continue
        rule_id = rule.get("rule_id")
        where = f"rules[{index}]({rule_id})"
        for field in REQUIRED_COMMON:
            if field not in rule:
                err(f"{where}: missing required field {field!r}")
        kind = rule.get("kind")
        if kind not in KIND_FIELDS:
            err(f"{where}: unknown kind {kind!r}")
            continue
        stage = rule.get("stage")
        if stage not in STAGES:
            err(f"{where}: stage must be one of {', '.join(STAGES)}")
        else:
            rule_stages.add(stage)
        status = rule.get("status")
        if status not in ("draft", "approved"):
            err(f"{where}: status must be 'draft' or 'approved'")
        if require_approved and status != "approved":
            err(f"{where}: rule is not approved (status={status!r})")
        if not isinstance(rule.get("reason"), str) or not rule["reason"].strip():
            err(f"{where}: reason must be a non-empty string")
        expected = rule.get("expected_matches")
        if not isinstance(expected, int) or isinstance(expected, bool) or expected < 0:
            err(f"{where}: expected_matches must be a non-negative integer")
        if kind in ("import_module", "import_symbol") and expected == 0:
            err(f"{where}: import rules must expect at least one site")
        if kind == "symbol_extract" and expected != len(rule.get("symbols") or []):
            err(f"{where}: symbol_extract expected_matches must equal len(symbols)")
        if kind == "class_member_extract" and expected != len(rule.get("symbols") or []):
            err(f"{where}: class_member_extract expected_matches must equal len(symbols)")
        if rule_id in seen_ids:
            err(f"{where}: duplicate rule_id")
        seen_ids.add(rule_id)
        for field in KIND_FIELDS[kind]:
            if field not in rule:
                err(f"{where}: kind {kind} requires field {field!r}")
        if kind == "import_module":
            if rule.get("old_symbol") not in (None,) or rule.get("new_symbol") not in (None,):
                err(f"{where}: import_module must not set old_symbol/new_symbol")
            if rule.get("old_module") == rule.get("new_module"):
                err(f"{where}: old_module equals new_module")
        if kind == "import_symbol" and rule.get("old_symbol") != rule.get("new_symbol"):
            if expected is None:
                pass
        if kind in ("symbol_extract", "class_member_extract"):
            if not isinstance(rule.get("symbols"), list) or not rule["symbols"]:
                err(f"{where}: symbols must be a non-empty list")
            if not isinstance(rule.get("dependencies"), list):
                err(f"{where}: dependencies must be a list (possibly empty)")
            for dep in rule.get("dependencies") or []:
                if not isinstance(dep, dict) or "name" not in dep or "provision" not in dep:
                    err(f"{where}: dependency entries need name and provision")
                    continue
                if dep["provision"] not in DEP_PROVISIONS:
                    err(f"{where}: dependency {dep.get('name')!r} has unknown provision")
        if kind == "class_member_extract":
            wiring = rule.get("wiring")
            if not isinstance(wiring, dict) or "method" not in wiring or "code" not in wiring:
                err(f"{where}: wiring must provide method and code")
        if kind == "facade_export":
            symbol = rule.get("symbol")
            previous = facade_exports.get(symbol)
            if previous is not None and previous != (
                rule.get("facade_path"),
                rule.get("provider_module"),
            ):
                err(
                    f"{where}: symbol {symbol!r} exported from conflicting providers "
                    f"{previous[1]!r} and {rule.get('provider_module')!r}"
                )
            facade_exports[symbol] = (rule.get("facade_path"), rule.get("provider_module"))
        if kind == "reference_rule":
            fmt = rule.get("format")
            if fmt not in ("python", "json", "toml", "text"):
                err(f"{where}: unknown format {fmt!r}")
            ops = rule.get("operations")
            if not isinstance(ops, list) or not ops:
                err(f"{where}: operations must be a non-empty list")
            else:
                total = 0
                for op in ops:
                    if not isinstance(op, dict) or op.get("op") not in REFERENCE_OPS:
                        err(f"{where}: unknown reference op {op!r}")
                        continue
                    name = op["op"]
                    if (fmt == "python") and name not in ("module_string", "parents_depth_shift"):
                        err(f"{where}: op {name!r} unsupported for python files")
                    if fmt == "json" and name not in ("json_field", "json_entry_path", "text_span"):
                        err(f"{where}: op {name!r} unsupported for json files")
                    if fmt == "toml" and name != "toml_key":
                        err(f"{where}: op {name!r} unsupported for toml files")
                    if fmt == "text" and name != "text_span":
                        err(f"{where}: op {name!r} unsupported for text files")
                    for field in REFERENCE_OP_FIELDS[name]:
                        if field not in op:
                            err(f"{where}: op {name!r} requires field {field!r}")
                    count = op.get("count", 1)
                    if not isinstance(count, int) or count < 0:
                        err(f"{where}: op {name!r} count must be a non-negative integer")
                    total += count
                if expected != total:
                    err(
                        f"{where}: expected_matches ({expected}) must equal the sum of "
                        f"operation counts ({total})"
                    )
        if kind == "layout_move":
            moves = rule.get("moves")
            if not isinstance(moves, list) or not moves:
                err(f"{where}: moves must be a non-empty list")
            else:
                for move in moves:
                    if not isinstance(move, dict) or "from" not in move or "to" not in move:
                        err(f"{where}: each move needs from/to")
            if expected != len(moves or []):
                err(f"{where}: layout_move expected_matches must equal len(moves)")

    verification = manifest.get("verification")
    if verification is not None:
        if not isinstance(verification, dict):
            err("verification must be an object keyed by stage")
        else:
            for key, checks in verification.items():
                if key != "always" and key not in STAGES:
                    err(f"verification: unknown stage key {key!r}")
                if not isinstance(checks, list):
                    err(f"verification[{key}]: must be a list")
                    continue
                for check in checks:
                    if not isinstance(check, dict) or check.get("check") not in CHECK_NAMES:
                        err(f"verification[{key}]: unknown check {check!r}")

    architecture = manifest.get("architecture")
    if architecture is not None:
        if not isinstance(architecture, dict) or not isinstance(
            architecture.get("layer_order"), list
        ):
            err("architecture.layer_order must be a list")
        else:
            order = architecture["layer_order"]
            mapping = architecture.get("layer_of_module") or {}
            for module, layer in mapping.items():
                if layer not in order:
                    err(f"architecture: module {module!r} maps to unknown layer {layer!r}")
    return errors


def load_and_validate(path: Path, require_approved: bool = False) -> dict:
    try:
        manifest = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ToolError(f"cannot read manifest {path}: {exc}") from exc
    errors = validate_manifest(manifest, require_approved=require_approved)
    if errors:
        raise ToolError(
            "manifest validation failed:\n" + "\n".join(f"- {e}" for e in errors)
        )
    return manifest


def main(argv: list[str] | None = None) -> int:
    import argparse
    import sys

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument(
        "--require-approved",
        action="store_true",
        help="fail unless every rule has status 'approved'",
    )
    args = parser.parse_args(argv)
    try:
        load_and_validate(args.manifest, require_approved=args.require_approved)
    except ToolError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print("manifest OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
