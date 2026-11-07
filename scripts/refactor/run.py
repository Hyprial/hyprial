"""CLI orchestrator for the staged refactor toolchain.

Commands:

- ``plan``: simulate the requested stages cumulatively without touching the
  repository; writes a report (rule outcomes, per-stage match counts,
  complete moves, cumulative unified diff, uncovered references, affected
  consumers/gates, pending decisions, apply token) into a new output
  directory.
- ``apply``: execute the requested stage(s) in an isolated checkout. Only
  approved rules, a matching baseline HEAD, clean manifest-touched paths and
  the explicit ``--confirm`` token printed by ``plan`` are accepted. Stage
  state, rule logs and input/output hashes are recorded under
  ``.refactor-state/``. Failures exit non-zero and preserve state.
- ``verify``: run the manifest-configured checks for a stage; command checks
  only run with ``--run-commands``.
- ``status``: print recorded stage state.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

if __package__ in (None, ""):  # direct execution: python scripts/refactor/run.py
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    __package__ = "scripts.refactor"

from .cli_support.plan_state import (
    _dirty_tracked_paths,
    _materialize_moves,
    _owned_live_outputs,
    _stages_arg,
    _touched_paths,
    _uncovered_references,
    _write_output_files,
    status_report as _status_report,
)  # noqa: E402
from . import verify as verify_module  # noqa: E402
from .transforms import facades, imports, layout, references, symbols  # noqa: E402
from .transforms.common import (  # noqa: E402
    Diagnostic,
    Rule,
    RuleOutcome,
    STAGES,
    ToolError,
    Workspace,
    sha256_hex,
    stable_write,
    unified_diff,
)
from .transforms.parsing import TransformContext  # noqa: E402
from . import validate_manifest as validator  # noqa: E402

STATE_DIR = ".refactor-state"
APPLY_ENGINES = {
    "import_module": imports.apply_rule,
    "import_symbol": imports.apply_rule,
    "symbol_extract": symbols.apply_rule,
    "class_member_extract": symbols.apply_rule,
    "facade_export": facades.apply_rule,
    "reference_rule": references.apply_rule,
    "layout_move": layout.apply_rule,
}
PASSED = verify_module.PASSED
FAILED = verify_module.FAILED
EXIT_OK = 0
EXIT_MANIFEST = 2
EXIT_UNAPPROVED = 3
EXIT_BASELINE = 4
EXIT_DRIFT = 5
EXIT_CONFIRM = 6
EXIT_RULES = 7
EXIT_WRITE = 8
EXIT_VERIFY = 9
EXIT_STATE = 10


def git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True
    )
    if result.returncode != 0:
        raise ToolError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    # rstrip only: porcelain status lines start with a status column (" M path")
    return result.stdout.rstrip()


def tracked_files(root: Path) -> list[str]:
    raw = subprocess.check_output(["git", "-C", str(root), "ls-files", "-z"])
    return sorted(p.decode("utf-8", "surrogateescape") for p in raw.split(b"\0") if p)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def manifest_sha256(manifest: dict) -> str:
    return sha256_hex(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True).encode("utf-8")
    )


def tool_code_hash() -> str:
    """Stable executable identity fingerprint.

    Included: every executed Python source under scripts/refactor (excluding
    generated previews/reports), the manifest JSON Schema, the tool's
    pyproject.toml and its uv.lock (actual dependency pins). Excluded:
    generated reports, previews, caches, state and documentation.
    """
    base = Path(__file__).resolve().parent
    parts = []
    for path in sorted(base.rglob("*")):
        rel = path.relative_to(base)
        if not path.is_file():
            continue
        rel_posix = rel.as_posix()
        if rel_posix.startswith(("preview/", ".venv/", ".ruff_cache/")):
            continue
        if "__pycache__" in path.parts:
            continue
        if path.suffix == ".py":
            parts.append(f"{rel_posix}:{sha256_hex(path.read_bytes())}")
        elif rel_posix in ("manifest.schema.json", "pyproject.toml", "uv.lock"):
            parts.append(f"{rel_posix}:{sha256_hex(path.read_bytes())}")
    return sha256_hex("\n".join(parts).encode("utf-8"))


def apply_token(baseline_commit: str, manifest_hash: str, root: Path) -> str:
    payload = (
        f"{baseline_commit}\n{manifest_hash}\n{root.resolve()}\n{tool_code_hash()}"
    )
    return sha256_hex(payload.encode("utf-8"))


def rules_for(manifest: dict, stages: list[str]) -> list[dict]:
    return [r for r in manifest["rules"] if r["stage"] in stages]


def _rule_outcome(rule_data: dict, ctx: TransformContext) -> RuleOutcome:
    engine = APPLY_ENGINES.get(rule_data["kind"])
    if engine is None:
        return RuleOutcome(
            rule_id=rule_data["rule_id"],
            kind=rule_data["kind"],
            stage=rule_data["stage"],
            status="refused",
            diagnostics=[
                Diagnostic(
                    "UNSUPPORTED-KIND", f"no engine for kind {rule_data['kind']!r}"
                )
            ],
        )
    return engine(Rule(rule_data), ctx)


def simulate(
    manifest: dict,
    ctx: TransformContext,
    stages: list[str],
    state: dict | None = None,
    apply_mode: bool = False,
) -> list[dict]:
    """Run rules stage by stage against the shared simulated workspace."""
    completed = {}
    if state:
        completed = state.get("stages", {})
    stage_reports = []
    for stage in [s for s in STAGES if s in stages]:
        entries = []
        for rule_data in manifest["rules"]:
            if rule_data["stage"] != stage:
                continue
            rule_state = (
                completed.get(stage, {}).get("rules", {}).get(rule_data["rule_id"])
                if completed
                else None
            )
            try:
                outcome = _rule_outcome(rule_data, ctx)
            except ToolError as exc:
                outcome = RuleOutcome(
                    rule_id=rule_data["rule_id"],
                    kind=rule_data["kind"],
                    stage=stage,
                    status="refused",
                    diagnostics=[Diagnostic("ENGINE-ERROR", str(exc))],
                )
            except Exception as exc:  # noqa: BLE001 - convert crashes into refusals
                outcome = RuleOutcome(
                    rule_id=rule_data["rule_id"],
                    kind=rule_data["kind"],
                    stage=stage,
                    status="refused",
                    diagnostics=[
                        Diagnostic("ENGINE-CRASH", f"{type(exc).__name__}: {exc}")
                    ],
                )
            if apply_mode and rule_state and outcome.status == "applied":
                outcome.status = "refused"
                outcome.diagnostics.append(
                    Diagnostic(
                        "STAGE-DRIFT",
                        "rule was recorded completed but the tree no longer satisfies it "
                        "(unexpected source change after apply)",
                    )
                )
            if (
                apply_mode
                and rule_state
                and outcome.status == "refused"
                and rule_state.get("status") in ("applied", "already_done")
            ):
                # stage lineage: when every recorded output of this rule was
                # superseded by a later approved stage, the engine's "no more
                # old form" refusal is the EXPECTED zero-modification replay,
                # not a drift. Tampering with the live (latest) version is
                # still caught by the recorded-output consistency check.
                recorded_paths = set((rule_state.get("paths") or {}).keys())
                superseded_paths = set(
                    (rule_state.get("superseded_paths") or {}).keys()
                )
                # after supersession `paths` is empty and everything lives in
                # superseded_paths; the rule is fully absorbed by a later stage
                fully_superseded = bool(superseded_paths) and (
                    recorded_paths <= superseded_paths or not recorded_paths
                )
                if fully_superseded:
                    outcome.status = "already_done"
                    outcome.matches = 0
                    outcome.diagnostics = [
                        d
                        for d in outcome.diagnostics
                        if d.severity != "error" or d.code != "EXPECTED-MATCHES"
                    ]
            entry = {
                "rule_id": outcome.rule_id,
                "kind": outcome.kind,
                "stage": stage,
                "status": outcome.status,
                "expected_matches": outcome.expected_matches,
                "matches": outcome.matches,
                "new_form_count": outcome.new_form_count,
                "changed_paths": outcome.changed_paths,
                "diagnostics": [
                    {
                        "code": d.code,
                        "message": d.message,
                        "path": d.path,
                        "line": d.line,
                        "severity": d.severity,
                    }
                    for d in outcome.diagnostics
                ],
                "detail": outcome.detail,
            }
            if rule_data["status"] != "approved":
                entry["approval"] = rule_data["status"]
            entries.append(entry)
        stage_reports.append({"stage": stage, "rules": entries})
    return stage_reports


def _hash_tree(root: Path, files: list[str]) -> dict[str, str]:
    result = {}
    for rel in files:
        path = root / rel
        if path.is_file() and not path.is_symlink():
            result[rel] = sha256_hex(path.read_bytes())
    return result


def _reject_output(root: Path, output: Path) -> None:
    resolved = output.resolve()
    root_resolved = root.resolve()
    git_dir = (root_resolved / ".git").resolve()
    if resolved == git_dir or git_dir in resolved.parents:
        raise ToolError(f"output directory must be outside .git: {resolved}")
    if resolved == root_resolved:
        raise ToolError("output directory must not be the repository root")
    if resolved.is_relative_to(root_resolved) and not resolved.is_relative_to(
        root_resolved / "docs" / "evidence"
    ):
        raise ToolError(
            "in-repository output is restricted to docs/evidence; use a temp directory"
        )
    if resolved.exists() and (not resolved.is_dir() or any(resolved.iterdir())):
        raise ToolError(f"output must be a new or empty directory: {resolved}")


def cmd_plan(args) -> int:
    root = args.root.resolve()
    manifest = validator.load_and_validate(args.manifest)
    manifest_hash = manifest_sha256(manifest)
    stages = _stages_arg(args)
    output = args.output.resolve() if args.output else None
    if output is not None:
        _reject_output(root, output)
    files = tracked_files(root)
    manifest_hash_for_state = manifest_hash
    try:
        plan_state = _load_state(_state_dir(root, manifest_hash_for_state))
    except ToolError:
        plan_state = None
    owned = _owned_live_outputs(root, plan_state)
    files = sorted(set(files) | owned)
    ws = Workspace(root, files)
    ctx = TransformContext(ws)

    stage_reports = simulate(manifest, ctx, stages)
    all_rules = [entry for report in stage_reports for entry in report["rules"]]

    moves = [dataclasses.asdict(m) for m in ws.moves()]
    diff_files = []
    dirty = sorted(set(ws._written) | set(ws._deleted))
    for rel in dirty:
        original = (root / rel).read_bytes() if (root / rel).is_file() else b""
        current = b"" if rel in ws._deleted else ws.read(rel)
        diff = unified_diff(rel, original, current)
        if diff:
            diff_files.append({"path": rel, "diff": diff})

    changed_paths = sorted({p for e in all_rules for p in e["changed_paths"]})
    uncovered, evidence_blocked = _uncovered_references(
        manifest, root, all_rules, set(changed_paths) | set(dirty)
    )
    pending = [
        {
            "rule_id": e["rule_id"],
            "stage": e["stage"],
            "kind": e["kind"],
            "status": "draft",
        }
        for e in all_rules
        if e.get("approval") == "draft"
    ]

    refused = [e for e in all_rules if e["status"] == "refused"]
    gates = sorted(
        {
            c.get("check")
            for checks in (manifest.get("verification") or {}).values()
            for c in checks
        }
    )
    authority_paths = sorted(
        {
            spec["path"]
            for rule in manifest["rules"]
            for spec in (rule.get("manifest_updates") or [])
        }
    )
    executable = not refused and not pending and not evidence_blocked
    root_head = _head_or_none(root)
    baseline_match = root_head == manifest["baseline_commit"]
    token = apply_token(manifest["baseline_commit"], manifest_hash, root)
    post_stage_hashes = {}
    cumulative: set[str] = set()
    for report_stage in stage_reports:
        stage_name = report_stage["stage"]
        for entry in report_stage["rules"]:
            cumulative.update(entry["changed_paths"])
        hashes = {}
        for rel in sorted(cumulative):
            try:
                hashes[rel] = sha256_hex(ws.read(rel))
            except ToolError:
                hashes[rel] = None
        post_stage_hashes[stage_name] = hashes
    blocked = [f"{e['rule_id']}: {e['diagnostics'][0]['code']}" for e in refused] + [
        f"{p['rule_id']}: draft" for p in pending
    ]
    if not baseline_match:
        blocked.append(
            f"baseline mismatch: root HEAD {root_head} != manifest baseline "
            f"{manifest['baseline_commit']}"
        )
        executable = False
    if evidence_blocked:
        blocked.append(
            "uncovered-references evidence is configured but missing/unreadable; "
            "coverage cannot be claimed without it"
        )
    report = {
        "schema_version": 1,
        "command": "plan",
        "manifest_path": str(args.manifest),
        "manifest_sha256": manifest_hash,
        "tool_code_sha256": tool_code_hash(),
        "baseline_match": baseline_match,
        "post_stage_hashes": post_stage_hashes,
        "baseline_commit": manifest["baseline_commit"],
        "root": str(root),
        "root_head": _head_or_none(root),
        "stages_requested": stages,
        "stages": stage_reports,
        "moves": moves,
        "changed_paths": changed_paths,
        "diff_files": diff_files,
        "uncovered_references": uncovered,
        "uncovered_evidence_blocked": evidence_blocked,
        "pending_decisions": pending,
        "affected_gates": gates,
        "affected_authority_manifests": authority_paths,
        "executable": executable,
        "blocked_reasons": blocked,
        "apply_token": token,
        "tool_note": "plan is read-only; apply additionally requires an approved manifest",
    }
    if output is not None:
        output.mkdir(parents=True, exist_ok=True)
        stable_write(output / "plan.json", report)
        (output / "diff.patch").write_text(
            "".join(d["diff"] for d in diff_files), encoding="utf-8"
        )
    print(
        json.dumps(
            {
                "plan": str(output) if output else None,
                "stages": stages,
                "applied": sum(1 for e in all_rules if e["status"] == "applied"),
                "already_done": sum(
                    1 for e in all_rules if e["status"] == "already_done"
                ),
                "refused": len(refused),
                "pending": len(pending),
                "changed_paths": len(changed_paths),
                "moves": len(moves),
                "diff_files": len(diff_files),
                "executable": executable,
            },
            sort_keys=True,
        )
    )
    return EXIT_OK if executable else EXIT_RULES


def _head_or_none(root: Path) -> str | None:
    try:
        return git(root, "rev-parse", "HEAD")
    except ToolError:
        return None


def _state_dir(root: Path, manifest_hash: str) -> Path:
    return root / STATE_DIR / f"m-{manifest_hash[:12]}"


def _load_state(state_dir: Path) -> dict | None:
    path = state_dir / "stages.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ToolError(f"state file {path} is corrupt: {exc}")


def _write_state(state_dir: Path, state: dict) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    tmp = state_dir / "stages.json.tmp"
    tmp.write_bytes(stable_json(state))
    os.replace(tmp, state_dir / "stages.json")


def stable_json(data: dict) -> bytes:
    return (
        json.dumps(data, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    ).encode("utf-8")


def cmd_apply(args) -> int:
    root = args.root.resolve()
    manifest = validator.load_and_validate(args.manifest)
    manifest_hash = manifest_sha256(manifest)
    stages = _stages_arg(args)
    unapproved = [
        r["rule_id"] for r in rules_for(manifest, stages) if r["status"] != "approved"
    ]
    if unapproved:
        print(
            json.dumps({"error": "unapproved-rules", "rules": unapproved}),
            file=sys.stderr,
        )
        return EXIT_UNAPPROVED
    state_dir = _state_dir(root, manifest_hash)
    try:
        state = _load_state(state_dir)
    except ToolError:
        print(
            json.dumps({"error": "state corrupt", "state_dir": str(state_dir)}),
            file=sys.stderr,
        )
        return EXIT_STATE

    head = _head_or_none(root)
    if head != manifest["baseline_commit"]:
        print(
            json.dumps(
                {
                    "error": "baseline-mismatch",
                    "head": head,
                    "baseline_commit": manifest["baseline_commit"],
                }
            ),
            file=sys.stderr,
        )
        return EXIT_BASELINE

    token = apply_token(manifest["baseline_commit"], manifest_hash, root)
    if not args.confirm or args.confirm != token:
        print(
            json.dumps(
                {
                    "error": "confirm-mismatch",
                    "hint": "run plan and pass its apply_token via --confirm; the token "
                    "binds baseline commit, manifest content, the exact checkout root and "
                    "the tool code (tool_code_sha256), so any of those changing invalidates it",
                }
            ),
            file=sys.stderr,
        )
        return EXIT_CONFIRM

    files = tracked_files(root)
    owned = _owned_live_outputs(root, state)
    files = sorted(set(files) | owned)
    before_hashes = _hash_tree(root, files)
    ws = Workspace(root, files)
    ctx = TransformContext(ws)

    prior_stages = state.get("stages", {}) if state else {}

    # a recorded completion must still match its exact recorded outputs;
    # "some migrated form exists" is never sufficient on its own. This runs
    # before simulation so tampering/deletion is diagnosed even when the
    # engine would classify the rule differently.
    for stage_name, record in prior_stages.items():
        if stage_name not in stages:
            continue
        for rule_id, rule_record in record.get("rules", {}).items():
            for path, digest in (rule_record.get("paths") or {}).items():
                if not (root / path).is_file():
                    print(
                        json.dumps(
                            {
                                "error": "already-done-mismatch",
                                "rule": rule_id,
                                "path": path,
                                "detail": "recorded output was deleted after apply",
                            }
                        ),
                        file=sys.stderr,
                    )
                    return EXIT_DRIFT
                if sha256_hex((root / path).read_bytes()) != digest:
                    print(
                        json.dumps(
                            {
                                "error": "already-done-mismatch",
                                "rule": rule_id,
                                "path": path,
                                "detail": "recorded output was modified after apply",
                            }
                        ),
                        file=sys.stderr,
                    )
                    return EXIT_DRIFT

    # simulate first: the full read/write/move set, not just the manifest's
    # explicit fields, defines which files the run may legitimately touch
    stage_reports = simulate(manifest, ctx, stages, state=state, apply_mode=True)
    all_rules = [entry for report in stage_reports for entry in report["rules"]]

    recorded_outputs: dict[str, str] = {}
    recorded_inputs: dict[str, str] = {}
    for record in prior_stages.values():
        recorded_outputs.update(record.get("outputs", {}))
        recorded_inputs.update(record.get("inputs", {}))

    touched = _touched_paths(manifest)
    for rule_report in stage_reports:
        for entry in rule_report["rules"]:
            touched.update(entry["changed_paths"])
            for diagnostic in entry["diagnostics"]:
                if diagnostic["path"]:
                    touched.add(diagnostic["path"])
    touched.update(ws.read_paths)
    touched.update(ws._written)
    for move in ws.moves():
        touched.add(move.src)
        touched.add(move.dst)
    dirty = {p for p in _dirty_tracked_paths(root) if p in touched and p in set(files)}
    if dirty and state:
        # paths this manifest already wrote are foreign drift only if their
        # content no longer matches the recorded post-apply output hashes;
        # sources of completed moves are expected to be gone
        own = {
            p
            for p in dirty
            if (
                recorded_outputs.get(p)
                and (root / p).is_file()
                and sha256_hex((root / p).read_bytes()) == recorded_outputs[p]
            )
            or (p in recorded_inputs and not (root / p).is_file())
        }
        dirty -= own
    if dirty:
        print(
            json.dumps(
                {
                    "error": "dirty-inputs",
                    "paths": sorted(dirty),
                    "hint": "files this run would read or write have uncommitted changes",
                }
            ),
            file=sys.stderr,
        )
        return EXIT_DRIFT

    # hash pins from the manifest must hold for rules not already completed
    # (completed rules are guarded by the recorded-output consistency check)
    for rule in manifest["rules"]:
        if rule["stage"] not in stages:
            continue
        prior = (
            prior_stages.get(rule["stage"], {}).get("rules", {}).get(rule["rule_id"])
        )
        if prior and prior.get("status") in ("applied", "already_done"):
            continue
        source_path = rule.get("source_path")
        source_sha = rule.get("source_sha256")
        if source_path and source_sha:
            path = root / str(source_path)
            if not path.is_file() or sha256_hex(path.read_bytes()) != source_sha:
                print(
                    json.dumps(
                        {
                            "error": "input-drift",
                            "rule": rule["rule_id"],
                            "path": source_path,
                        }
                    ),
                    file=sys.stderr,
                )
                return EXIT_DRIFT

    refused = [e for e in all_rules if e["status"] == "refused"]
    if refused:
        failure = {
            "schema_version": 1,
            "command": "apply",
            "status": "failed",
            "at": now_iso(),
            "manifest_sha256": manifest_hash,
            "stages": stage_reports,
        }
        state_dir.mkdir(parents=True, exist_ok=True)
        stable_write(state_dir / "last-failure.json", failure)
        print(
            json.dumps(
                {
                    "applied": 0,
                    "already_done": sum(
                        1 for e in all_rules if e["status"] == "already_done"
                    ),
                    "refused": len(refused),
                    "first_refusal": refused[0]["diagnostics"][0]["code"]
                    if refused[0]["diagnostics"]
                    else "unknown",
                }
            )
        )
        return EXIT_RULES

    # an untracked file already occupying a write target must never be
    # overwritten silently
    for rel in ws._written:
        if rel not in set(files) and (root / rel).exists():
            print(
                json.dumps(
                    {
                        "error": "untracked-target",
                        "path": rel,
                        "hint": "an untracked file already exists at a write target",
                    }
                ),
                file=sys.stderr,
            )
            return EXIT_DRIFT

    # materialize: moves first, then content writes. The two phases are
    # separate functions so tests can inject failures at each boundary.
    materialized: dict = {"moves": [], "writes": []}
    try:
        _materialize_moves(root, ws, materialized)
        _write_output_files(root, ws, materialized)
    except OSError as exc:
        print(
            json.dumps({"error": "write-failure", "detail": str(exc)}), file=sys.stderr
        )
        failure = {
            "schema_version": 1,
            "command": "apply",
            "status": "failed",
            "phase": "materialize",
            "at": now_iso(),
            "manifest_sha256": manifest_hash,
            "materialized": materialized,
            "detail": str(exc),
        }
        state_dir.mkdir(parents=True, exist_ok=True)
        stable_write(state_dir / "last-failure.json", failure)
        return EXIT_WRITE

    after_hashes = _hash_tree(root, files + [p for p in ws._written if p not in files])
    stage_state = state.get("stages", {}) if state else {}
    for report in stage_reports:
        stage = report["stage"]
        changed_here = sorted({p for e in report["rules"] for p in e["changed_paths"]})
        rules_view = {}
        for e in report["rules"]:
            rule_record = (
                prior_stages.get(stage, {}).get("rules", {}).get(e["rule_id"], {})
            )
            prior_paths = dict(rule_record.get("paths", {}))
            rule_paths = dict(prior_paths)
            for p in e["changed_paths"]:
                if after_hashes.get(p) and (root / p).exists():
                    rule_paths[p] = after_hashes[p]
            # lineage history must survive replays: a zero-modification
            # replay rewrites this record and would otherwise forget which
            # paths a later stage already superseded
            superseded = dict(rule_record.get("superseded_paths", {}))
            rules_view[e["rule_id"]] = {
                "status": e["status"],
                "matches": e["matches"],
                "expected_matches": e["expected_matches"],
                "changed_paths": e["changed_paths"],
                "paths": rule_paths,
                "superseded_paths": superseded,
            }
        # stage lineage: a path rewritten by THIS stage supersedes earlier
        # stages' recorded versions; history is kept, validation follows the
        # live lineage only
        this_stage_paths = set(changed_here)
        for prior_name, prior_record in stage_state.items():
            for rule_id, rule_record in prior_record.get("rules", {}).items():
                overlap = set(rule_record.get("paths", {})) & this_stage_paths
                if overlap:
                    superseded = rule_record.setdefault("superseded_paths", {})
                    for path in overlap:
                        superseded[path] = {
                            "sha256": rule_record["paths"].pop(path),
                            "superseded_by": f"{stage}",
                        }
        prior_stage_record = prior_stages.get(stage, {})
        merged_inputs = dict(prior_stage_record.get("inputs", {}))
        merged_outputs = dict(prior_stage_record.get("outputs", {}))
        for p in changed_here:
            if before_hashes.get(p):
                merged_inputs[p] = before_hashes[p]
            if after_hashes.get(p) and (root / p).exists():
                merged_outputs[p] = after_hashes[p]
        record = {
            "status": "applied",
            "applied_at": now_iso(),
            "manifest_sha256": manifest_hash,
            "tool_code_sha256": tool_code_hash(),
            "rules": rules_view,
            "inputs": merged_inputs,
            "outputs": merged_outputs,
        }
        stage_state[stage] = record
    new_state = {
        "schema_version": 1,
        "manifest_sha256": manifest_hash,
        "baseline_commit": manifest["baseline_commit"],
        "stages": stage_state,
    }
    if state is None:
        state_dir.mkdir(parents=True, exist_ok=True)
        stable_write(state_dir / "manifest.snapshot.json", manifest)
    try:
        _write_state(state_dir, new_state)
    except OSError as exc:
        print(
            json.dumps({"error": "state-write-failure", "detail": str(exc)}),
            file=sys.stderr,
        )
        return EXIT_WRITE

    modifications = sum(1 for e in all_rules if e["status"] == "applied")
    already = sum(1 for e in all_rules if e["status"] == "already_done")
    print(
        json.dumps(
            {
                "applied": modifications,
                "already_done": already,
                "modifications": len(ws._written) if modifications else 0,
                "refused": 0,
                "state_dir": str(state_dir),
            },
            sort_keys=True,
        )
    )
    return EXIT_OK


def _verify_preflight(root: Path, manifest: dict, stage: str | None) -> dict:
    """Mandatory acceptance preflight, before any configured gate runs.

    Checks root/baseline identity, that the requested stages were actually
    applied, that the recorded tool fingerprint still matches, and that every
    non-superseded recorded output hash (or deletion, for move sources) still
    holds. A bad state fails closed; configured checks can never mask it.
    """
    results: list[dict] = []
    head = _head_or_none(root)
    results.append(
        {
            "check": "root-baseline-identity",
            "status": PASSED if head == manifest["baseline_commit"] else FAILED,
            "detail": f"HEAD {head} vs baseline {manifest['baseline_commit']}",
        }
    )
    manifest_hash = manifest_sha256(manifest)
    state = _load_state(_state_dir(root, manifest_hash))
    results.append(
        {
            "check": "applied-state-present",
            "status": PASSED if state else FAILED,
            "detail": "no applied stage state for this manifest" if not state else "",
        }
    )
    if state:
        current_tool = tool_code_hash()
        requested = [stage] if stage else sorted(state.get("stages", {}))
        for stage_name in requested:
            record = state.get("stages", {}).get(stage_name)
            if not record:
                results.append(
                    {
                        "check": f"stage-applied:{stage_name}",
                        "status": FAILED,
                        "detail": "stage has no applied record; acceptance requires an applied migration",
                    }
                )
                continue
            if record.get("status") != "applied":
                results.append(
                    {
                        "check": f"stage-applied:{stage_name}",
                        "status": FAILED,
                        "detail": f"stage status is {record.get('status')!r}, not applied",
                    }
                )
                continue
            if record.get("tool_code_sha256") != current_tool:
                results.append(
                    {
                        "check": f"tool-identity:{stage_name}",
                        "status": FAILED,
                        "detail": "tool code changed since apply; re-run apply and verify "
                        "with the reviewed tool version",
                    }
                )
            for rule_id, rule_record in record.get("rules", {}).items():
                for path, digest in (rule_record.get("paths") or {}).items():
                    if not (root / path).is_file():
                        results.append(
                            {
                                "check": f"output-hash:{rule_id}",
                                "status": FAILED,
                                "path": path,
                                "detail": "recorded output was deleted",
                            }
                        )
                    elif sha256_hex((root / path).read_bytes()) != digest:
                        results.append(
                            {
                                "check": f"output-hash:{rule_id}",
                                "status": FAILED,
                                "path": path,
                                "detail": "recorded output was modified after apply",
                            }
                        )
    failed = [r for r in results if r["status"] == FAILED]
    return {
        "status": FAILED if failed else PASSED,
        "results": results,
    }


def cmd_verify(args) -> int:
    root = args.root.resolve()
    manifest = validator.load_and_validate(args.manifest)
    preflight = _verify_preflight(root, manifest, args.stage)
    report = verify_module.run_stage_verification(
        root, manifest, args.stage, run_commands=args.run_commands
    )
    report["stages"]["_preflight"] = preflight
    if preflight["status"] == FAILED:
        report["status"] = verify_module.FAILED
    if report["status"] == verify_module.PASSED:
        # record verification per stage: an applied stage is not accepted
        # until a passing verify marks it
        manifest_hash = manifest_sha256(manifest)
        state_dir = _state_dir(root, manifest_hash)
        try:
            state = _load_state(state_dir)
        except ToolError:
            state = None
        if state is not None:
            changed = False
            for stage_name in report["stages"]:
                record = state.get("stages", {}).get(stage_name)
                if record and record.get("status") == "applied":
                    record["verified_at"] = now_iso()
                    record["verification"] = "passed"
                    changed = True
            if changed:
                _write_state(state_dir, state)
    if args.output:
        stable_write(Path(args.output), report)
    print(
        json.dumps(
            {
                "status": report["status"],
                "stages": {
                    key: value["status"] for key, value in report["stages"].items()
                },
            },
            sort_keys=True,
        )
    )
    return EXIT_OK if report["status"] == verify_module.PASSED else EXIT_VERIFY


def cmd_status(args) -> int:
    root = args.root.resolve()
    return _status_report(root, STATE_DIR, EXIT_OK)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="scripts.refactor.run", description="Staged deep-module refactor toolchain"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    plan_p = sub.add_parser("plan", help="read-only cumulative preview (default)")
    plan_p.add_argument("--manifest", type=Path, required=True)
    plan_p.add_argument("--root", type=Path, default=Path.cwd())
    plan_p.add_argument("--stage", choices=STAGES)
    plan_p.add_argument(
        "--all", action="store_true", help="simulate S1..S6 cumulatively"
    )
    plan_p.add_argument(
        "--output", type=Path, help="directory for plan.json + diff.patch"
    )
    plan_p.set_defaults(func=cmd_plan)

    apply_p = sub.add_parser("apply", help="execute a stage in an isolated checkout")
    apply_p.add_argument("--manifest", type=Path, required=True)
    apply_p.add_argument("--root", type=Path, default=Path.cwd())
    apply_p.add_argument("--stage", choices=STAGES)
    apply_p.add_argument("--all", action="store_true")
    apply_p.add_argument("--confirm", required=True, help="apply_token printed by plan")
    apply_p.set_defaults(func=cmd_apply)

    verify_p = sub.add_parser("verify", help="run manifest-configured verification")
    verify_p.add_argument("--manifest", type=Path, required=True)
    verify_p.add_argument("--root", type=Path, default=Path.cwd())
    verify_p.add_argument("--stage", choices=STAGES)
    verify_p.add_argument("--run-commands", action="store_true")
    verify_p.add_argument("--output", type=Path)
    verify_p.set_defaults(func=cmd_verify)

    status_p = sub.add_parser("status", help="print recorded stage state")
    status_p.add_argument("--root", type=Path, default=Path.cwd())
    status_p.set_defaults(func=cmd_status)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except ToolError as exc:
        print(json.dumps({"error": "tool", "detail": str(exc)}), file=sys.stderr)
        return EXIT_MANIFEST


if __name__ == "__main__":
    raise SystemExit(main())
