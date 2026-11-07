"""Conservative, read-only CI acquisition inventory and cache coverage policy.

Public compatibility facade. Scanner state and parsing live in focused modules.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import asdict
import json
from pathlib import Path
import re
import shlex

import tests.support.ci.ci_workflow_policy as workflows
from tests.support.ci_download.types import (
    CACHE_ENV,
    Context,
    Diagnostic,
    Inventory,
    Site,
    condition,
    expand,
    families,
    js_close,
    js_split,
    js_tokens,
    matrix_jobs,
)
from tests.support.ci._ci_download_scanner import Scanner

MODE = "audit"  # D5 flips this after coverage and runner contracts land.

__all__ = [
    "CACHE_ENV",
    "Context",
    "Diagnostic",
    "Inventory",
    "MODE",
    "Scanner",
    "Site",
    "audit",
    "condition",
    "covered",
    "expand",
    "families",
    "image_disposition_errors",
    "js_close",
    "js_split",
    "js_tokens",
    "load_manifest",
    "matrix_jobs",
    "validate_manifest",
    "violations",
    "workflows",
]


def load_manifest(root: Path) -> dict:
    path = root / ".forgejo/ci-downloads.json"
    return (
        json.loads(path.read_text())
        if path.is_file()
        else {
            "version": 1,
            "coverage": [],
            "exceptions": [],
            "edges": [],
            "runner_contracts": [],
        }
    )


def validate_manifest(manifest: dict, inventory: Inventory) -> list[str]:
    """Fail closed on dead declarations, broad selectors and absent evidence."""
    errors = []
    if manifest.get("version") != 1:
        errors.append("manifest version must be 1")
    allowed = {"version", "coverage", "exceptions", "edges", "runner_contracts"}
    for category in ("coverage", "exceptions", "edges", "runner_contracts"):
        if not isinstance(manifest.get(category, []), list) or any(
            not isinstance(e, dict) for e in manifest.get(category, [])
        ):
            return [f"{category} must be an array of objects"]
    if set(manifest) - allowed:
        errors.append("unknown manifest fields")
    known = {s.fingerprint: s for s in inventory.sites}
    seen = set()
    for category in ("coverage", "exceptions"):
        for entry in manifest.get(category, []):
            fp = entry.get("fingerprint", "")
            if not re.fullmatch(r"[0-9a-f]{64}", fp):
                errors.append(f"{category}: exact fingerprint required (no globs)")
            if fp not in known:
                errors.append(f"{category}: dead entry {fp}")
            if fp in seen:
                errors.append(f"duplicate entry {fp}")
            seen.add(fp)
            required = (
                ("reason", "purpose", "owner", "review")
                if category == "exceptions"
                else (
                    "key",
                    "path",
                    "family",
                    "prepare_step",
                    "mechanism",
                    "runner_contract",
                )
            )
            for field in required:
                if not isinstance(entry.get(field), str) or not entry[field].strip():
                    errors.append(f"{category}: nonempty {field} required")
            site = known.get(fp)
            if site and "command" in entry and entry["command"] != site.command:
                errors.append(f"{category}: command does not match fingerprint")
            for field in ("workflow", "job", "file", "function", "ordinal"):
                if field not in entry or (
                    site and entry[field] != getattr(site, field)
                ):
                    errors.append(f"{category}: exact {field} required")
            if category == "exceptions" and entry.get("reason", "").strip().lower() in {
                "url absent",
                "already installed",
                "retry exists",
                "small",
                "continue-on-error",
                "*",
                "?",
            }:
                errors.append("invalid cache exception reason")
            if category == "coverage" and (
                re.search(r"hashFiles\(\s*([\"'])\s*\1\s*\)", entry.get("key", ""))
                or re.fullmatch(r"\$\{\{\s*github.run_id\s*}}", entry.get("key", ""))
            ):
                errors.append("empty hash or run-only cache key")
    return errors


def covered(site: Site, entry: dict, inventory: Inventory, manifest: dict) -> bool:
    if (
        entry.get("family") != site.family
        or site.step_index < 0
        or site.family == "ACTION"
    ):
        return False
    steps = inventory.steps.get((site.workflow, site.job), [])
    index = next(
        (i for i, s in enumerate(steps) if s.get("id") == entry.get("prepare_step")), -1
    )
    if index < 0 or index >= site.step_index:
        return False
    step = steps[index]
    cond = condition(step.get("if"))
    if cond in {"false", "0"} or (
        cond and cond not in {"true", "success()", site.condition}
    ):
        return False
    if step.get("continue-on-error"):
        return False
    key = entry.get("key", "")
    path = entry.get("path", "")
    if not key.strip() or not path.strip():
        return False
    contracts = {c.get("id"): c for c in manifest.get("runner_contracts", [])}
    contract = contracts.get(entry.get("runner_contract"), {})
    if (
        contract.get("verified") is not True
        or not contract.get("evidence")
        or contract.get("mechanism") != entry.get("mechanism")
    ):
        return False
    if (
        contract.get("workflow") != site.workflow
        or contract.get("job") != site.job
        or path not in contract.get("paths", [])
    ):
        return False
    if site.builder != bool(contract.get("builder", False)):
        return False
    mechanism = entry.get("mechanism")
    if mechanism == "actions/cache":
        if not str(step.get("uses", "")).startswith(
            ("actions/cache@", "actions/cache/restore@")
        ):
            return False
        settings = step.get("with", {})
        if (
            settings.get("key") != key
            or path not in str(settings.get("path", "")).splitlines()
        ):
            return False
    elif mechanism == "restore.cjs":
        if (
            "restore.cjs" not in step.get("run", "")
            or step.get("env", {}).get("INPUT_KEY") != key
            or step.get("env", {}).get("INPUT_PATH") != path
        ):
            return False
    elif mechanism == "persistent":
        if (
            not step.get("run")
            or contract.get("prepare_command") != step["run"]
            or contract.get("key") != key
        ):
            return False
    else:
        return False
    variable = CACHE_ENV.get(site.family)
    actual_path = (
        expand(dict(site.env).get(variable, ""), dict(site.env)) if variable else ""
    )
    if variable is None:
        return False
    if actual_path != expand(path, dict(site.env)):
        seed = entry.get("seed_copy", {})
        seed_index = next(
            (i for i, s in enumerate(steps) if s.get("id") == seed.get("step")), -1
        )
        if not index < seed_index < site.step_index or seed.get("path") != actual_path:
            return False
        seed_step = steps[seed_index]
        if condition(seed_step.get("if")) not in {
            "",
            "true",
            "success()",
            site.condition,
        } or seed_step.get("continue-on-error"):
            return False
        try:
            copy_argv = shlex.split(seed_step.get("run", ""))
        except ValueError:
            return False
        if copy_argv != ["cp", "-a", path.rstrip("/") + "/.", actual_path]:
            return False
    if any(
        flag in site.command
        for flag in ("--no-cache", "--cache-dir", "--cache ", "--store-dir")
    ):
        return False  # A command-level override needs a separate proven contract.
    return True


def image_disposition_errors(
    inventory: Inventory, original: set[tuple], retired: set[tuple]
) -> list[str]:
    """Account real pre-job acquisitions exactly; retired identities are not sites."""
    errors = []
    if retired - original:
        errors.append("retired image identity absent from original inventory")
    actual = Counter(
        (site.workflow, site.job, site.command)
        for site in inventory.sites
        if site.function == "implicit-image"
    )
    expected = original - retired
    if expected - actual.keys():
        errors.append("missing retained image contexts")
    if actual.keys() - expected:
        errors.append("unaccounted current image contexts")
    if any(count != 1 for count in actual.values()):
        errors.append("duplicate image contexts")
    return errors


def audit(inventory: Inventory, manifest: dict) -> dict:
    errors = validate_manifest(manifest, inventory)
    exceptions = {
        e.get("fingerprint"): e
        for e in manifest.get("exceptions", [])
        if isinstance(e, dict)
    }
    coverage = {
        e.get("fingerprint"): e
        for e in manifest.get("coverage", [])
        if isinstance(e, dict)
    }
    uncovered = []
    for site in inventory.sites:
        if not errors and site.fingerprint in exceptions:
            continue
        if not errors and covered(
            site, coverage.get(site.fingerprint, {}), inventory, manifest
        ):
            continue
        uncovered.append(site)
    return {
        "mode": MODE,
        "direct_sites": len(inventory.lexical(True)),
        "transitive_sites": len(inventory.lexical(False)),
        "implicit_image_contexts": sum(
            s.function == "implicit-image" for s in inventory.sites
        ),
        "uncovered_by_family": dict(
            sorted(Counter(s.family for s in uncovered).items())
        ),
        "uncovered_count": len(uncovered),
        "uncovered": [asdict(s) for s in uncovered],
        "diagnostics": [asdict(d) for d in inventory.diagnostics],
        "manifest_errors": errors,
    }


def violations(inventory: Inventory, manifest: dict) -> list[str]:
    report = audit(inventory, manifest)
    return [
        *report["manifest_errors"],
        *(
            f"{d['workflow']}::{d['job']} {d['file']}:{d['line']}: {d['message']}"
            for d in report["diagnostics"]
        ),
        *(
            f"{s['workflow']}::{s['job']}::{s['step']} {s['file']}:{s['line']} {s['family']}: {s['command']} via {s['call_chain']}"
            for s in report["uncovered"]
        ),
    ]
