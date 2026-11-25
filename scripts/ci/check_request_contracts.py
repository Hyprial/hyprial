#!/usr/bin/env python3
"""CI admission for A03's OpenAPI, import-ratchet and method-registration gates.

Ordinary PRs compare against the real base ledger.  The one-time
``intg/cli-command-surface`` -> ``dev`` container lands in a base that has no
ledger yet; that shape is admitted only when the head ledgers are byte-identical
to the accepted A03 seed recorded in ``scripts/ci/a03-contract-seed.json``.  A
missing base ledger after the seed, a changed seed, a grown ledger or an
unreadable base is always a failure, never a skip.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
SEED_RELATIVE_PATH = "scripts/ci/a03-contract-seed.json"
# Reviewed anchors.  The seed JSON is only a human-readable copy: changing it
# together with the ledgers still fails here, because these constants come
# from the reviewed gate and the accepted commit's immutable blobs.
ACCEPTED_SHA = "20269c960c98ba5def5abe65d3948a60ea8f8550"
IMPORT_LEDGER_SHA256 = "5b220a29cb1ad95f95d8b63ec2aafb746320f28287c90fd19e334236075eb4e5"
LEGACY_METHODS_SHA256 = "46357f8d4da53bc13b122c3717de8b7fe0c3db1ba5f236ea5d1f970473ef10ae"
OPENAPI_SHA256 = "8b8a83ba3e1a5365f102e242589f79bfef4489ad3d9c1540913cebed0d174a81"
REVIEWED_LEDGER_DIGESTS = {
    "importRatchet": IMPORT_LEDGER_SHA256,
    "legacyMethods": LEGACY_METHODS_SHA256,
}
IMPORT_GATE = "scripts/ci/check_import_ratchet.py"
METHOD_GATE = "scripts/ci/check_method_registration.py"
OPENAPI_GATE = "scripts/verify/dump_daemon_openapi.py"
LEDGER_KEYS = ("importRatchet", "legacyMethods")
# dev receives the A03 seed; main receives it on the first dev -> main promotion.
FIRST_SEED_BASES = frozenset({"dev", "main"})


class AdmissionError(ValueError):
    """The admission decision cannot be measured; fail closed."""


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        text=True,
        check=False,
    )


def _ref_exists(root: Path, ref: str) -> bool:
    result = _git(root, "rev-parse", "--verify", f"{ref}^{{commit}}")
    return result.returncode == 0


def _path_in_ref(root: Path, ref: str, path: str) -> bool:
    return _git(root, "cat-file", "-e", f"{ref}:{path}").returncode == 0


def _is_shallow(root: Path) -> bool:
    result = _git(root, "rev-parse", "--is-shallow-repository")
    return result.returncode == 0 and result.stdout.strip() == "true"


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _normalized_ref(ref: str) -> str:
    for prefix in ("refs/remotes/origin/", "refs/heads/", "origin/"):
        if ref.startswith(prefix):
            return ref[len(prefix) :]
    return ref


def _load_seed(
    path: Path,
    *,
    accepted_sha: str = ACCEPTED_SHA,
    ledger_digests: dict[str, str] | None = None,
    openapi_sha: str = OPENAPI_SHA256,
) -> dict[str, Any]:
    try:
        seed = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise AdmissionError(f"cannot read seed {path}: {error}") from error
    if not isinstance(seed, dict) or seed.get("version") != 1:
        raise AdmissionError("seed version must be 1")
    expected_digests = ledger_digests or REVIEWED_LEDGER_DIGESTS
    accepted = seed.get("acceptedSha")
    if accepted != accepted_sha:
        raise AdmissionError("seed acceptedSha differs from the reviewed anchor")
    for key in LEDGER_KEYS:
        entry = seed.get(key)
        if not isinstance(entry, dict):
            raise AdmissionError(f"seed {key} must be an object")
        path_value = entry.get("path")
        digest = entry.get("sha256")
        if path_value != {
            "importRatchet": "scripts/ci/import-ratchet-baseline.json",
            "legacyMethods": "scripts/ci/legacy-method-baseline.json",
        }[key]:
            raise AdmissionError(f"seed {key}.path differs from the reviewed path")
        if digest != expected_digests[key]:
            raise AdmissionError(f"seed {key}.sha256 differs from the reviewed anchor")
    openapi = seed.get("openapi")
    if not isinstance(openapi, dict) or openapi.get("sha256") != openapi_sha:
        raise AdmissionError("seed openapi.sha256 differs from the reviewed anchor")
    if openapi.get("path") != "contract/daemon-request/openapi.json":
        raise AdmissionError("seed openapi.path differs from the reviewed path")
    return seed


def _run_json_gate(root: Path, script: str, *extra: str) -> dict[str, Any]:
    result = subprocess.run(
        [sys.executable, str(root / script), "--json", *extra],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    try:
        report = json.loads(result.stdout)
    except json.JSONDecodeError:
        report = {
            "status": "FAIL",
            "violations": [
                {
                    "kind": "gate-output-invalid",
                    "script": script,
                    "returncode": result.returncode,
                    "stdout": result.stdout[-2000:],
                    "stderr": result.stderr[-2000:],
                }
            ],
        }
    return {"script": script, "returncode": result.returncode, "report": report}


def _run_openapi_gate(root: Path) -> dict[str, Any]:
    result = subprocess.run(
        [sys.executable, str(root / OPENAPI_GATE), "--check"],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    return {
        "status": "PASS" if result.returncode == 0 else "FAIL",
        "returncode": result.returncode,
        "stdout": result.stdout[-2000:],
        "stderr": result.stderr[-2000:],
    }


def _accepted_problems(
    root: Path,
    base_ref: str,
    seed: dict[str, Any],
    *,
    ledger_digests: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Verify the fixed A03 commit, its blobs, ancestry and first-seed shape."""

    problems: list[dict[str, Any]] = []
    expected_digests = ledger_digests or REVIEWED_LEDGER_DIGESTS
    accepted = str(seed["acceptedSha"])
    if not _ref_exists(root, accepted):
        return [{"kind": "accepted-commit-missing", "acceptedSha": accepted}]
    ancestor = _git(root, "merge-base", "--is-ancestor", accepted, "HEAD")
    if ancestor.returncode != 0:
        problems.append({"kind": "accepted-not-ancestor-of-head", "acceptedSha": accepted})
    already_landed = _git(root, "merge-base", "--is-ancestor", accepted, base_ref)
    if already_landed.returncode == 0:
        problems.append(
            {
                "kind": "seed-already-landed",
                "acceptedSha": accepted,
                "baseRef": base_ref,
                "detail": "base already contains the accepted A03 commit; missing ledgers are deletion, not first seed",
            }
        )
    for key in LEDGER_KEYS:
        entry = seed[key]
        blob = _git(root, "show", f"{accepted}:{entry['path']}")
        if blob.returncode != 0:
            problems.append(
                {
                    "kind": "accepted-ledger-missing",
                    "ledger": key,
                    "path": entry["path"],
                    "acceptedSha": accepted,
                }
            )
            continue
        digest = hashlib.sha256(blob.stdout.encode("utf-8")).hexdigest()
        if digest != expected_digests[key]:
            problems.append(
                {
                    "kind": "accepted-ledger-digest-mismatch",
                    "ledger": key,
                    "acceptedSha": accepted,
                    "blobSha256": digest,
                    "expected": expected_digests[key],
                }
            )
    return problems



def _import_ledger_edges(document: object) -> dict[tuple[str, str, str | None], int]:
    if not isinstance(document, dict) or document.get("version") != 1:
        raise ValueError("import ledger version must be 1")
    rows = document.get("edges")
    if not isinstance(rows, list):
        raise ValueError("import ledger must contain an edges array")
    edges: dict[tuple[str, str, str | None], int] = {}
    for index, row in enumerate(rows):
        if not isinstance(row, dict) or set(row) != {"path", "module", "symbol", "count"}:
            raise ValueError(
                f"import ledger edge #{index} must contain exactly path/module/symbol/count"
            )
        path = row["path"]
        module = row["module"]
        symbol = row["symbol"]
        count = row["count"]
        if not isinstance(path, str) or not path or not isinstance(module, str) or not module:
            raise ValueError(f"import ledger edge #{index} path/module must be non-empty strings")
        if symbol is not None and (not isinstance(symbol, str) or not symbol):
            raise ValueError(f"import ledger edge #{index} symbol must be null or a non-empty string")
        if isinstance(count, bool) or not isinstance(count, int) or count <= 0:
            raise ValueError(f"import ledger edge #{index} count must be a positive integer")
        key = (path, module, symbol)
        if key in edges:
            raise ValueError(f"import ledger edge #{index} duplicates {key}")
        edges[key] = count
    return edges


def _seed_subset_problems(
    root: Path, accepted: str, entry: dict[str, Any], head_path: Path
) -> list[dict[str, Any]]:
    """First-dev head ledger may only be a no-growth subset of the accepted blob."""

    try:
        accepted_doc = json.loads(
            _git(root, "show", f"{accepted}:{entry['path']}").stdout
        )
        head_doc = json.loads(head_path.read_text(encoding="utf-8"))
        accepted_edges = _import_ledger_edges(accepted_doc)
        head_edges = _import_ledger_edges(head_doc)
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as error:
        return [{"kind": "seed-head-invalid-ledger", "detail": str(error)}]
    problems: list[dict[str, Any]] = []
    for key, count in sorted(head_edges.items()):
        previous = accepted_edges.get(key)
        if previous is None:
            problems.append({"kind": "seed-head-import-growth", "path": key[0], "module": key[1], "symbol": key[2], "count": count})
        elif count > previous:
            problems.append({"kind": "seed-head-import-growth", "path": key[0], "module": key[1], "symbol": key[2], "count": count, "accepted": previous})
    return problems


def _seed_admission(
    root: Path,
    *,
    base_ref: str,
    base_branch: str,
    allow_first_seed: bool,
    seed_path: Path,
    accepted_sha: str = ACCEPTED_SHA,
    ledger_digests: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Decide the ledger mode without running the three contract gates."""

    root = root.resolve()
    if not _ref_exists(root, base_ref):
        return {
            "status": "FAIL",
            "baseRef": base_ref,
            "invalidBase": True,
            "modes": {},
            "violations": [{"kind": "invalid-base-ref", "baseRef": base_ref}],
        }
    expected_digests = ledger_digests or REVIEWED_LEDGER_DIGESTS
    seed = _load_seed(
        seed_path,
        accepted_sha=accepted_sha,
        ledger_digests=expected_digests,
    )
    base_ledgers = {
        key: _path_in_ref(root, base_ref, str(seed[key]["path"]))
        for key in LEDGER_KEYS
    }
    first_seed_shape = not any(base_ledgers.values())
    seed_allowed = allow_first_seed and first_seed_shape and base_branch in FIRST_SEED_BASES
    violations: list[dict[str, Any]] = []
    if seed_allowed:
        if _is_shallow(root):
            violations.append(
                {
                    "kind": "ancestry-unproven-in-shallow",
                    "detail": (
                        "first-seed admission needs a complete history; the CI "
                        "step unshallows only when a dev or main base has no ledgers"
                    ),
                }
            )
        else:
            violations.extend(
                _accepted_problems(root, base_ref, seed, ledger_digests=expected_digests)
            )
    ledgers: dict[str, Any] = {}
    modes: dict[str, str | None] = {}
    for key in LEDGER_KEYS:
        entry = seed[key]
        path = root / str(entry["path"])
        try:
            actual_digest = _digest(path)
        except OSError as error:
            violations.append(
                {
                    "kind": "missing-head-ledger",
                    "ledger": key,
                    "path": str(entry["path"]),
                    "detail": str(error),
                }
            )
            modes[key] = None
            continue
        ledger = {
            "path": entry["path"],
            "basePresent": base_ledgers[key],
            "headSha256": actual_digest,
            "seedSha256": entry["sha256"],
            "matchesSeed": actual_digest == entry["sha256"],
            "firstSeedUsed": False,
        }
        if base_ledgers[key]:
            modes[key] = "base"
        elif not seed_allowed:
            violations.append(
                {
                    "kind": "missing-base-ledger",
                    "ledger": key,
                    "baseRef": base_ref,
                    "detail": (
                        "first-seed admission is limited to a dev or main base whose "
                        "head ledger exactly matches the accepted A03 seed"
                    ),
                }
            )
            modes[key] = None
        elif key == "importRatchet" and _ref_exists(root, str(seed["acceptedSha"])):
            subset_problems = _seed_subset_problems(
                root, str(seed["acceptedSha"]), entry, path
            )
            if subset_problems:
                violations.extend({**problem, "ledger": key} for problem in subset_problems)
                modes[key] = None
            else:
                ledger["firstSeedUsed"] = True
                ledger["firstSeedSubset"] = True
                modes[key] = "head"
        elif actual_digest != entry["sha256"]:
            violations.append(
                {
                    "kind": "seed-digest-mismatch",
                    "ledger": key,
                    "headSha256": actual_digest,
                    "seedSha256": entry["sha256"],
                }
            )
            modes[key] = None
        else:
            ledger["firstSeedUsed"] = True
            modes[key] = "head"
        ledgers[key] = ledger
    return {
        "status": "FAIL" if violations else "PASS",
        "baseRef": base_ref,
        "baseBranch": base_branch,
        "acceptedSha": seed["acceptedSha"],
        "acceptedSource": seed.get("acceptedSource"),
        "seedShaPresent": _ref_exists(root, str(seed["acceptedSha"])),
        "allowFirstSeed": allow_first_seed,
        "firstSeedShape": first_seed_shape,
        "firstSeedAllowed": seed_allowed,
        "ledgers": ledgers,
        "modes": modes,
        "violations": violations,
    }


def check(
    root: Path,
    *,
    base_ref: str,
    base_branch: str,
    allow_first_seed: bool,
    seed_path: Path,
) -> dict[str, Any]:
    decision = _seed_admission(
        root,
        base_ref=base_ref,
        base_branch=base_branch,
        allow_first_seed=allow_first_seed,
        seed_path=seed_path,
    )
    if decision.pop("invalidBase", False):
        return decision
    modes = decision.pop("modes")
    violations = list(decision["violations"])
    gates: dict[str, Any] = {"openapi": _run_openapi_gate(root)}
    for key in LEDGER_KEYS:
        mode = modes.get(key)
        if mode is None:
            continue
        extra = ("--base-ref", base_ref) if mode == "base" else ()
        gate = _run_json_gate(
            root,
            IMPORT_GATE if key == "importRatchet" else METHOD_GATE,
            *extra,
        )
        gates[key] = gate
        report = gate["report"]
        if gate["returncode"] != 0 or report.get("status") != "PASS":
            violations.append(
                {
                    "kind": "gate-failed",
                    "ledger": key,
                    "violations": report.get("violations", []),
                }
            )
    if gates["openapi"]["status"] != "PASS":
        violations.append(
            {
                "kind": "gate-failed",
                "gate": "openapi",
                "stderr": gates["openapi"].get("stderr", ""),
            }
        )
    decision["status"] = "FAIL" if violations else "PASS"
    decision["gates"] = gates
    decision["violations"] = violations
    return decision


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--base-ref", required=True)
    parser.add_argument(
        "--base-branch",
        default=None,
        help="logical base branch (default: strip origin/ from --base-ref)",
    )
    parser.add_argument("--allow-first-seed", action="store_true")
    parser.add_argument(
        "--seed-file",
        type=Path,
        default=None,
        help=f"accepted A03 seed (default: {SEED_RELATIVE_PATH})",
    )
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    root = args.root.resolve()
    seed = args.seed_file or root / SEED_RELATIVE_PATH
    try:
        report = check(
            root,
            base_ref=args.base_ref,
            base_branch=args.base_branch or _normalized_ref(args.base_ref),
            allow_first_seed=args.allow_first_seed,
            seed_path=seed,
        )
    except AdmissionError as error:
        report = {"status": "FAIL", "violations": [{"kind": "invalid-seed", "detail": str(error)}]}
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(f"{report['status']} request contract gate ({args.base_ref})")
        for violation in report.get("violations", []):
            print(json.dumps(violation, sort_keys=True))
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
