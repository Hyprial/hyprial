from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess

from ..transforms.common import STAGES


def _stages_arg(args) -> list[str]:
    if getattr(args, "all", False) or getattr(args, "stage", None) is None:
        return list(STAGES)
    return [args.stage]


def _uncovered_references(
    manifest: dict, root: Path, rule_entries: list[dict], covered: set[str]
) -> tuple[list[dict], bool]:
    """(uncovered rows, evidence_blocked).

    A configured-but-missing/unreadable C0 references file BLOCKS coverage
    claims: the plan must not present an empty uncovered list as clearance.
    """
    evidence = manifest.get("evidence") or {}
    rel_path = evidence.get("references_path")
    if not rel_path:
        # no evidence source configured at all: coverage cannot be claimed
        return [], True
    path = root / rel_path
    if not path.is_file():
        return [], True
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return [], True
    needles = []
    for rule in manifest["rules"]:
        old_module = rule.get("old_module")
        if old_module:
            needles.append(old_module)
        for move in rule.get("moves") or []:
            needles.append(str(move.get("from")))
    if not needles:
        return []
    uncovered: list[dict] = []
    for row in data.get("python_references", []):
        value = str(row.get("value", ""))
        if not any(n in value or value in n for n in needles):
            continue
        if row.get("path") in covered:
            continue
        uncovered.append(
            {
                "kind": row.get("kind"),
                "path": row.get("path"),
                "line": row.get("line"),
                "value": value[:120],
            }
        )
    for row in data.get("tracked_text_path_references", []):
        value = str(row.get("value", ""))
        if not any(n == value or value.startswith(n) for n in needles):
            continue
        if row.get("path") in covered:
            continue
        uncovered.append(
            {
                "kind": "text-path",
                "path": row.get("path"),
                "line": row.get("line"),
                "value": value[:120],
            }
        )
    return uncovered[:500], False


def _materialize_moves(root: Path, ws, materialized: dict) -> None:
    for move in ws.moves():
        dst = root / move.dst
        dst.parent.mkdir(parents=True, exist_ok=True)
        src = root / move.src
        if src.exists():
            src.rename(dst)
        # else: the source only ever existed in the workspace overlay (created
        # by an earlier rule, e.g. a generated facade, then moved by a later
        # one); move() re-keyed the overlay entry to dst, so _write_output_files
        # lands its content at the destination.
        materialized["moves"].append({"src": move.src, "dst": move.dst})


def _write_output_files(root: Path, ws, materialized: dict) -> None:
    for rel, data in sorted(ws._written.items()):
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".refactor-tmp")
        tmp.write_bytes(data)
        old_mode = path.stat().st_mode if path.exists() else None
        os.replace(tmp, path)
        if old_mode is not None:
            path.chmod(old_mode)
        materialized["writes"].append(rel)


def _owned_live_outputs(root: Path, state: dict | None) -> set[str]:
    """Manifest-owned files from recorded completions that still exist.

    A prior stage's outputs (e.g. an extracted module) are part of the
    pipeline's workspace even while untracked; arbitrary untracked files
    never are.
    """
    owned: set[str] = set()
    if not state:
        return owned
    for record in state.get("stages", {}).values():
        for rule in record.get("rules", {}).values():
            for path in rule.get("paths", {}):
                if (root / path).is_file():
                    owned.add(path)
    return owned


def _dirty_tracked_paths(root: Path) -> set[str]:
    """Tracked paths with uncommitted changes, parsed from -z output.

    Porcelain v1 -z records are NUL-separated; a rename carries the new path
    first and the old path as an extra field, so both are reported.
    """
    raw = subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "status",
            "--porcelain=v1",
            "-z",
            "--untracked-files=no",
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    fields = raw.split("\0")
    dirty: set[str] = set()
    i = 0
    while i < len(fields):
        record = fields[i]
        if not record:
            i += 1
            continue
        status = record[:2]
        path = record[3:]
        dirty.add(path)
        if "R" in status or "C" in status:
            if i + 1 < len(fields):
                dirty.add(fields[i + 1])
            i += 1
        i += 1
    return dirty


def _touched_paths(manifest: dict) -> set[str]:
    touched: set[str] = set()
    for rule in manifest["rules"]:
        for key in ("source_path", "target_path", "facade_path"):
            if rule.get(key):
                touched.add(str(rule[key]))
        for move in rule.get("moves") or []:
            touched.add(str(move["from"]))
            touched.add(str(move["to"]))
        for spec in rule.get("manifest_updates") or []:
            touched.add(str(spec["path"]))
        for spec in rule.get("reference_updates") or []:
            touched.add(str(spec["source_path"]))
        for spec in rule.get("operations") or []:
            if rule.get("source_path"):
                touched.add(str(rule["source_path"]))
    return touched


def status_report(root: Path, state_dir_name: str, exit_ok: int) -> int:
    base = root / state_dir_name
    result: dict = {"schema_version": 1, "manifests": {}}
    if base.is_dir():
        for manifest_dir in sorted(base.iterdir()):
            if not manifest_dir.is_dir():
                continue
            entry: dict = {"state_dir": str(manifest_dir)}
            try:
                state = json.loads(
                    (manifest_dir / "stages.json").read_text(encoding="utf-8")
                )
                entry["manifest_sha256"] = state.get("manifest_sha256")
                entry["baseline_commit"] = state.get("baseline_commit")
                entry["stages"] = {
                    stage: {
                        "status": record.get("status"),
                        "completed_at": record.get("completed_at"),
                        "rules": {
                            rule_id: r["status"]
                            for rule_id, r in record.get("rules", {}).items()
                        },
                    }
                    for stage, record in state.get("stages", {}).items()
                }
            except (OSError, json.JSONDecodeError) as exc:
                entry["corrupt"] = str(exc)
            failure = manifest_dir / "last-failure.json"
            if failure.exists():
                try:
                    entry["last_failure"] = json.loads(
                        failure.read_text(encoding="utf-8")
                    ).get("at")
                except (OSError, json.JSONDecodeError):
                    entry["last_failure"] = "unreadable"
            result["manifests"][manifest_dir.name] = entry
    print(json.dumps(result, sort_keys=True))
    return exit_ok
