#!/usr/bin/env python3
"""Require a disposition for implementation-to-E2E impact, not a claim of PASS.

Snapshots and their coverage union are read from exact git objects. This tool
never executes scenarios or treats a PR declaration as execution evidence.
The required CI caller takes the checker from the base, like PR hygiene.
"""

from __future__ import annotations

import argparse
import ast
from fnmatch import fnmatchcase
import json
import os
from pathlib import Path
import re
import subprocess
import sys

REGISTRY = "contract/e2e/scenarios.json"
COVERAGE_SCHEMA = "hyprial-e2e-coverage/v1"
PROTECTED_PREFIXES = (
    "src/", "gui/", "clients/", "crates/", "sidecar/", "contract/", "packaging/",
    "scripts/build/", "scripts/ci/", "scripts/verify/", ".forgejo/",
)
PROTECTED_FILES = {
    "pyproject.toml", "uv.lock", "rust-toolchain.toml", "Cargo.toml", "Cargo.lock",
    "package.json", "package-lock.json", "Dockerfile",
}


class InvalidMapping(ValueError):
    """A missing, stale or unsafe mapping is an error, not no impact."""


def git(repo: Path, *args: str) -> bytes:
    environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    environment.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull, GIT_TERMINAL_PROMPT="0")
    result = subprocess.run(
        ["git", *args], cwd=repo, env=environment, capture_output=True, timeout=30,
    )
    if result.returncode:
        raise ValueError("git snapshot/comparison unavailable; fetch the exact base and head history")
    return result.stdout


def safe_path(value: object, *, pattern: bool = False) -> bool:
    characters = r"[A-Za-z0-9_./*\-]+" if pattern else r"[A-Za-z0-9_./\-]+"
    return (
        isinstance(value, str) and re.fullmatch(characters, value) is not None
        and not value.startswith("/") and not any(part in {"", ".", ".."} for part in value.split("/"))
        and (not pattern or not value.startswith("*"))
    )


class Snapshot:
    def __init__(self, repo: Path, revision: str):
        self.repo = repo
        self.revision = git(repo, "rev-parse", "--verify", "--end-of-options", f"{revision}^{{commit}}").decode().strip()
        entries = git(repo, "ls-tree", "-rz", "--full-tree", self.revision).split(b"\0")
        self.files = {}
        for entry in entries:
            if entry:
                description, path = entry.split(b"\t", 1)
                mode, kind, _ = description.split(b" ", 2)
                if kind == b"blob" and mode in {b"100644", b"100755"}:
                    self.files[path.decode("utf-8")] = mode
        self._text: dict[str, str] = {}

    def text(self, path: str) -> str:
        if path not in self.files:
            raise InvalidMapping(f"{path}: missing regular tracked file at {self.revision[:12]}")
        if path not in self._text:
            self._text[path] = git(self.repo, "show", f"{self.revision}:{path}").decode("utf-8")
        return self._text[path]

    def registry(self) -> dict:
        value = json.loads(self.text(REGISTRY))
        if not isinstance(value, dict) or value.get("schema") != "hyprial-e2e-scenarios/v1":
            raise InvalidMapping("unsupported E2E scenario registry schema")
        return value


def local_dependencies(snapshot: Snapshot, runner: str) -> set[str]:
    pending = [runner]
    found = set()

    def module_paths(module: str, source: str, level: int = 0) -> list[str]:
        if level:
            parent = Path(source).parent
            for _ in range(level - 1):
                parent = parent.parent
            prefix = parent.as_posix() + "/"
        elif module.startswith(("contract.e2e.", "tests.support.")):
            prefix = ""
        else:
            prefix = "contract/e2e/"
        stem = prefix + module.replace(".", "/")
        return [path for path in (stem + ".py", stem + "/__init__.py") if path in snapshot.files]

    while pending:
        source = pending.pop()
        if source in found:
            continue
        found.add(source)
        if not source.endswith(".py"):
            continue
        try:
            tree = ast.parse(snapshot.text(source), filename=source)
        except SyntaxError as error:
            raise InvalidMapping(f"{source}: cannot inspect scenario/helper dependencies") from error
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    pending.extend(module_paths(alias.name, source))
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                pending.extend(module_paths(module, source, node.level))
                for alias in node.names:
                    pending.extend(module_paths(".".join(filter(None, (module, alias.name))), source, node.level))
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                value = node.value
                if value.endswith(".py") and safe_path(value):
                    for parent in (Path(source).parent, Path("contract/e2e")):
                        candidate = (parent / value).as_posix()
                        if candidate in snapshot.files:
                            pending.append(candidate)
    return found


def coverage(snapshot: Snapshot, value: dict) -> dict[str, dict]:
    if value.get("coverage_schema") != COVERAGE_SCHEMA:
        raise InvalidMapping("missing or unsupported E2E coverage_schema")
    rows = value.get("scenarios")
    if not isinstance(rows, list) or not rows:
        raise InvalidMapping("missing or empty scenario mapping")
    result = {}
    for row in rows:
        if not isinstance(row, dict):
            raise InvalidMapping("scenario mapping must be an object")
        identifier = row.get("id")
        if not isinstance(identifier, str) or not re.fullmatch(r"E2E-\d{3}", identifier) or identifier in result:
            raise InvalidMapping("invalid or duplicate scenario id")
        own = set()
        for key in ("runner", "document"):
            name = row.get(key)
            if not safe_path(name):
                raise InvalidMapping(f"{identifier}: unsafe or missing {key}")
            path = f"contract/e2e/{name}"
            if path not in snapshot.files:
                raise InvalidMapping(f"{identifier}: missing regular tracked {key} {path}")
            own.add(path)
        patterns = row.get("covers")
        if not isinstance(patterns, list) or not patterns or len(set(map(str, patterns))) != len(patterns):
            raise InvalidMapping(f"{identifier}: missing, empty or duplicate covers")
        dependencies = local_dependencies(snapshot, f"contract/e2e/{row['runner']}") | own
        for pattern in patterns:
            if not safe_path(pattern, pattern=True):
                raise InvalidMapping(f"{identifier}: unsafe covers pattern")
            matches = {path for path in snapshot.files if fnmatchcase(path, pattern)}
            if not matches:
                raise InvalidMapping(f"{identifier}: stale covers pattern {pattern}")
            dependencies.update(path for path in matches if path.startswith("contract/e2e/") and path != REGISTRY)
        result[identifier] = {"row": row, "patterns": patterns, "dependencies": dependencies}
    return result


def visible_lines(body: str) -> list[str]:
    body = re.sub(r"<!--.*?-->", "", body, flags=re.DOTALL)
    lines = []
    fence = None
    for line in body.splitlines():
        stripped = line.strip()
        marker = re.match(r"^(`{3,}|~{3,})", stripped)
        if marker:
            character = marker[1][0]
            if fence is None:
                fence = character
            elif character == fence:
                fence = None
            continue
        if fence is None:
            lines.append(stripped)
    return lines


def declarations(body: str) -> tuple[dict, dict, list[str]]:
    scenarios, unknown, errors = {}, {}, []
    for line in visible_lines(body):
        if line.startswith("E2E-Impact:"):
            matched = re.fullmatch(r"E2E-Impact:\s+(E2E-\d{3})\s+(updated|unchanged|not-applicable):\s*(.*)", line)
            if not matched:
                errors.append("malformed E2E-Impact declaration")
                continue
            identifier, action, reason = matched.groups()
            if identifier in scenarios:
                errors.append(f"{identifier}: duplicate E2E-Impact declaration")
            scenarios[identifier] = (action, reason)
        elif line.startswith("E2E-Unknown:"):
            path, separator, reason = line.removeprefix("E2E-Unknown:").strip().partition(": ")
            if not separator or not safe_path(path):
                errors.append("malformed E2E-Unknown declaration; use an exact repository path")
                continue
            if path in unknown:
                errors.append(f"{path}: duplicate E2E-Unknown declaration")
            unknown[path] = reason
    return scenarios, unknown, errors


def review_reason(reason: str) -> bool:
    marker = "; requires-review"
    return reason.endswith(marker) and len(reason.removesuffix(marker).strip()) >= 8


def unchanged_reason(reason: str) -> bool:
    matched = re.fullmatch(r"reason=(.+?);\s*validation=(.+)", reason)
    if not matched:
        return False
    rationale, validation = (part.strip() for part in matched.groups())
    return (
        len(rationale) >= 8 and len(validation) >= 12
        and not re.match(r"(?i)^(?:PASS|FAIL|BLOCKED|SKIP|https?://)\b", validation)
    )


def assess(repo: Path, base: str, head: str, body: str, plan_only: bool = False) -> tuple[dict, int]:
    report = {"schema": "hyprial-e2e-impact/v1", "execution": "NOT_EVALUATED", "errors": [], "review_required": []}
    try:
        baseline, candidate = Snapshot(repo, base), Snapshot(repo, head)
        merge_base = git(repo, "merge-base", baseline.revision, candidate.revision).decode().strip()
        report.update(base=baseline.revision, head=candidate.revision, merge_base=merge_base)
        base_value, head_value = baseline.registry(), candidate.registry()
        if base_value.get("coverage_schema") != COVERAGE_SCHEMA:
            report.update(status="BOOTSTRAP_REVIEW_REQUIRED", errors=[
                "base has no adopted E2E coverage schema; land reviewed checker/metadata before enabling the base-owned CI step",
            ])
            return report, 2
        before, after = coverage(baseline, base_value), coverage(candidate, head_value)
        changed = set(git(repo, "diff", "--no-ext-diff", "--no-textconv", "--no-renames", "--name-only", "-z",
                          merge_base, candidate.revision, "--").decode().split("\0")) - {""}
        affected, covered = {}, set()
        for identifier in sorted(before.keys() | after.keys()):
            entries = [mapping[identifier] for mapping in (before, after) if identifier in mapping]
            dependencies = set().union(*(entry["dependencies"] for entry in entries))
            patterns = {pattern for entry in entries for pattern in entry["patterns"]}
            paths = {path for path in changed if path in dependencies or any(fnmatchcase(path, pattern) for pattern in patterns)}
            covered.update(paths)
            mapping_changed = before.get(identifier, {}).get("row") != after.get(identifier, {}).get("row")
            if mapping_changed and REGISTRY in changed:
                paths.add(REGISTRY)
                covered.add(REGISTRY)
            if paths:
                affected[identifier] = {"paths": sorted(paths), "scenario_changes": sorted(changed & dependencies)}
        unknown = sorted(path for path in changed - covered if path.startswith(PROTECTED_PREFIXES) or path in PROTECTED_FILES)
        report.update(changed=sorted(changed), affected=affected, unknown=unknown)
        if plan_only:
            report["status"] = "impact-plan"
            return report, 0
        items, dispositions, errors = declarations(body)
        for identifier in affected:
            if identifier not in items:
                errors.append(f"{identifier}: missing E2E-Impact disposition")
        for identifier, (action, reason) in items.items():
            impact = affected.get(identifier, {"scenario_changes": []})
            if action == "updated" and (not reason.strip() or not impact["scenario_changes"] or identifier not in after):
                errors.append(f"{identifier}: updated requires a corresponding runner/document/helper change, not only covers")
            elif action == "unchanged" and not unchanged_reason(reason):
                errors.append(f"{identifier}: unchanged requires reason=<specific contract judgment>; validation=<original scenario record or concrete plan>, not a verdict/URL")
            elif action == "not-applicable":
                if not review_reason(reason):
                    errors.append(f"{identifier}: not-applicable requires a concrete reason ending '; requires-review'")
                else:
                    report["review_required"].append(identifier)
        for identifier in items.keys() - (before.keys() | after.keys()):
            errors.append(f"{identifier}: declaration names no registered scenario")
        for path in unknown:
            if not review_reason(dispositions.get(path, "")):
                errors.append(f"{path}: missing E2E-Unknown exact-path reason ending '; requires-review'")
            else:
                report["review_required"].append(path)
        for path in dispositions.keys() - set(unknown):
            errors.append(f"{path}: E2E-Unknown does not name an unmapped changed product/protocol path")
        report.update(status="declarations-required" if errors else "declaration-complete", errors=errors,
                      dispositions={key: value[0] for key, value in sorted(items.items())})
        return report, 1 if errors else 0
    except (InvalidMapping, json.JSONDecodeError, UnicodeError) as error:
        report.update(status="invalid-mapping", errors=[str(error)])
        return report, 1
    except (ValueError, OSError, subprocess.TimeoutExpired) as error:
        report.update(status="operational-error", errors=[str(error) if isinstance(error, ValueError) else type(error).__name__])
        return report, 2


def validate(repo: Path, head: str = "HEAD") -> tuple[dict, int]:
    """Check that the scenario registry's assumptions still hold at one commit.

    This is the periodic-E2E form of the check: it compares nothing and reads no
    base, because the question is not "what changed" but "is the mapping still
    true" — every runner/document exists, every covers pattern still matches a
    tracked file, and every declared dependency still parses.
    """

    report = {
        "schema": "hyprial-e2e-impact/v1",
        "mode": "validate-only",
        "execution": "NOT_EVALUATED",
        "errors": [],
    }
    try:
        candidate = Snapshot(repo, head)
        report["head"] = candidate.revision
        value = candidate.registry()
        mapping = coverage(candidate, value)
    except InvalidMapping as error:
        report.update(status="assumptions-invalid", errors=[str(error)])
        return report, 1
    except (json.JSONDecodeError, UnicodeError) as error:
        # A damaged registry is an invalid mapping, not a tooling failure: the
        # assumptions cannot be read, so they cannot be claimed to hold.
        report.update(status="assumptions-invalid", errors=[f"{REGISTRY}: {error}"])
        return report, 1
    except (ValueError, OSError, subprocess.TimeoutExpired) as error:
        report.update(status="operational-error", errors=[str(error)])
        return report, 2
    report.update(
        status="assumptions-hold",
        scenarios=len(mapping),
        coverage_schema=value.get("coverage_schema"),
    )
    return report, 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--base")
    parser.add_argument("--head", default="HEAD")
    parser.add_argument(
        "--validate-only",
        action="store_true",
        help="check the scenario assumptions at --head only (periodic E2E form)",
    )
    parser.add_argument("--pr-body-file", type=Path)
    parser.add_argument("--plan-only", action="store_true", help="list impact without validating dispositions or execution")
    args = parser.parse_args(argv)
    if args.validate_only:
        report, code = validate(args.repo, args.head or "HEAD")
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
        return code
    if not args.base:
        print(json.dumps({"status": "operational-error", "execution": "NOT_EVALUATED",
                          "errors": ["--base is required unless --validate-only"]}))
        return 2
    try:
        body = args.pr_body_file.read_text(encoding="utf-8") if args.pr_body_file else ""
    except OSError:
        print(json.dumps({"status": "operational-error", "execution": "NOT_EVALUATED", "errors": ["cannot read PR body file"]}))
        return 2
    report, code = assess(args.repo, args.base, args.head, body, args.plan_only)
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return code


if __name__ == "__main__":
    sys.exit(main())
