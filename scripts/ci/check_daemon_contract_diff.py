#!/usr/bin/env python3
"""Reviewable breaking-change diff for the daemon request contract projection.

Decision record (M-03, 2026-10-31)
- ``openapi.json`` is published to the frontend, generated clients and the
  Contract Mock.  Regenerating it is not enough: a removed method, error code,
  required field, enum value or example case needs an explicit release, not a
  silent snapshot update.
- The check compares two projections (base and head) and prints every finding
  with its location.  ``Contract-Impact: <reason>`` in the PR body is the same
  declared-release mechanism the repository's existing gate uses; without it a
  BREAKING finding exits red.  Additive changes stay green.
- This is a conservative structural rule, not a full JSON-Schema subtyping
  proof.  Unknown changed schema keywords are treated as BREAKING; a reviewer
  who understands a compatible exception can declare it and record why.

wf-61d46aa513404f45a938d9e764f8dd55#implGRS3 (revGRS2 finding c): the CI gate
must not run with ``--base == --head``.  ``--base-ref REF`` diffs the head
projection against the projection at ``merge-base(REF, HEAD)`` — the real PR
fork point, so merging the target branch into a feature branch cannot produce
phantom findings.  An unresolvable merge-base (shallow history the caller did
not deepen) is an unreadable increment: the gate exits OPERATIONAL (2),
fail-closed, the same contract as check_contract_impact.py.  Unit tests of the
checker stay on explicit ``--base`` files.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_HEAD = ROOT / "contract" / "daemon-request" / "openapi.json"
PROJECTION_IN_REPO = "contract/daemon-request/openapi.json"
_DECLARE_RE = re.compile(r"^\s*Contract-Impact:\s*(?P<reason>\S.*?)\s*$", re.IGNORECASE)
_ANNOTATIONS = frozenset(
    {
        "description",
        "title",
        "deprecated",
        "readOnly",
        "writeOnly",
        "default",
        "$comment",
        "examples",
        "example",
    }
)
_META = ("owner", "mode", "mutates", "retry", "audience", "httpExposed", "confirmation")
EXIT_GREEN = 0
EXIT_RED = 1
EXIT_OPERATIONAL = 2


@dataclass(frozen=True, slots=True)
class Finding:
    severity: str
    where: str
    detail: str

    def render(self) -> str:
        return f"{self.severity} {self.where}: {self.detail}"


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _schema_findings(old: Any, new: Any, where: str) -> list[Finding]:
    findings: list[Finding] = []
    if old == new:
        return findings
    if not isinstance(old, dict) or not isinstance(new, dict):
        return [Finding("BREAKING", where, "schema shape is not an object")]
    for key in sorted(set(old) | set(new)):
        before = old.get(key)
        after = new.get(key)
        if before == after:
            continue
        child = f"{where}.{key}"
        if key in _ANNOTATIONS:
            findings.append(Finding("NOTICE", child, "annotation changed"))
            continue
        if key == "type":
            before_types = set(before) if isinstance(before, list) else {before}
            after_types = set(after) if isinstance(after, list) else {after}
            if before_types - after_types:
                findings.append(Finding("BREAKING", child, "a JSON type was removed"))
            elif after_types - before_types:
                findings.append(Finding("COMPATIBLE", child, "a JSON type was added"))
            continue
        if key in ("enum", "const"):
            before_values = before if isinstance(before, list) else [before]
            after_values = after if isinstance(after, list) else [after]
            if set(map(_canonical, before_values)) - set(map(_canonical, after_values)):
                findings.append(Finding("BREAKING", child, "an allowed value was removed"))
            elif set(map(_canonical, after_values)) - set(map(_canonical, before_values)):
                findings.append(Finding("COMPATIBLE", child, "an allowed value was added"))
            continue
        if key == "required":
            before_required = set(before) if isinstance(before, list) else set()
            after_required = set(after) if isinstance(after, list) else set()
            if after_required - before_required:
                findings.append(Finding("BREAKING", child, "a required field was added"))
            elif before_required - after_required:
                findings.append(Finding("COMPATIBLE", child, "a required field was removed"))
            continue
        if key == "properties":
            if not isinstance(before, dict) or not isinstance(after, dict):
                findings.append(Finding("BREAKING", child, "properties shape changed"))
                continue
            for name in sorted(set(before) - set(after)):
                findings.append(
                    Finding("BREAKING", f"{child}.{name}", "property was removed")
                )
            for name in sorted(set(after) - set(before)):
                findings.append(
                    Finding("COMPATIBLE", f"{child}.{name}", "property was added")
                )
            for name in sorted(set(before) & set(after)):
                findings.extend(
                    _schema_findings(before[name], after[name], f"{child}.{name}")
                )
            continue
        if key == "additionalProperties":
            if after is False and before is not False:
                findings.append(Finding("BREAKING", child, "extra properties are now rejected"))
            elif before is False and after is not False:
                findings.append(Finding("COMPATIBLE", child, "extra properties are now allowed"))
            elif isinstance(before, dict) and isinstance(after, dict):
                findings.extend(_schema_findings(before, after, child))
            elif before != after:
                findings.append(Finding("BREAKING", child, "additionalProperties changed"))
            continue
        if key in ("items", "contains", "not", "if", "then", "else", "propertyNames"):
            findings.extend(_schema_findings(before, after, child))
            continue
        if key in ("oneOf", "anyOf", "allOf", "prefixItems"):
            if not isinstance(before, list) or not isinstance(after, list):
                findings.append(Finding("BREAKING", child, "combinator shape changed"))
                continue
            before_values = {_canonical(item) for item in before}
            after_values = {_canonical(item) for item in after}
            if before_values - after_values:
                findings.append(Finding("BREAKING", child, "a schema variant was removed"))
            if after_values - before_values:
                findings.append(Finding("COMPATIBLE", child, "a schema variant was added"))
            continue
        if key == "$ref" and before != after:
            findings.append(Finding("BREAKING", child, "reference changed"))
            continue
        findings.append(Finding("BREAKING", child, "constraint changed"))
    return findings


def _methods(document: dict[str, Any]) -> dict[str, dict[str, Any]]:
    entries = document.get("x-hyprial-methods")
    if not isinstance(entries, list):
        return {}
    return {
        entry["name"]: entry
        for entry in entries
        if isinstance(entry, dict) and isinstance(entry.get("name"), str)
    }


def _paths(document: dict[str, Any]) -> dict[str, dict[str, Any]]:
    paths = document.get("paths")
    if not isinstance(paths, dict):
        return {}
    return {
        path: entry["post"]
        for path, entry in paths.items()
        if isinstance(entry, dict) and isinstance(entry.get("post"), dict)
    }


def _cases(operation: dict[str, Any]) -> dict[str, dict[str, Any]]:
    cases = operation.get("x-hyprial-examples")
    if not isinstance(cases, list):
        return {}
    return {
        case["name"]: case
        for case in cases
        if isinstance(case, dict) and isinstance(case.get("name"), str)
    }


def _compare_meta(
    name: str, before: dict[str, Any], after: dict[str, Any]
) -> list[Finding]:
    findings: list[Finding] = []
    for field in _META:
        old = before.get(field)
        new = after.get(field)
        if old == new:
            continue
        where = f"method {name}.{field}"
        if field == "httpExposed":
            if old is True and new is not True:
                findings.append(Finding("BREAKING", where, "method is no longer projected"))
            else:
                findings.append(Finding("COMPATIBLE", where, "method became projected"))
        elif field == "audience":
            old_values = set(old) if isinstance(old, list) else set()
            new_values = set(new) if isinstance(new, list) else set()
            if old_values - new_values:
                findings.append(Finding("BREAKING", where, "an audience was removed"))
            if new_values - old_values:
                findings.append(Finding("COMPATIBLE", where, "an audience was added"))
        else:
            findings.append(Finding("BREAKING", where, "execution semantics changed"))
    return findings


def _compare_errors(
    name: str, before: dict[str, Any], after: dict[str, Any]
) -> list[Finding]:
    old_errors = {
        error["code"]: error.get("dataSchema")
        for error in before.get("errors", [])
        if isinstance(error, dict) and isinstance(error.get("code"), str)
    }
    new_errors = {
        error["code"]: error.get("dataSchema")
        for error in after.get("errors", [])
        if isinstance(error, dict) and isinstance(error.get("code"), str)
    }
    findings: list[Finding] = []
    for code in sorted(set(old_errors) - set(new_errors)):
        findings.append(Finding("BREAKING", f"method {name}.errors.{code}", "error removed"))
    for code in sorted(set(new_errors) - set(old_errors)):
        findings.append(Finding("COMPATIBLE", f"method {name}.errors.{code}", "error added"))
    for code in sorted(set(old_errors) & set(new_errors)):
        findings.extend(
            _schema_findings(
                old_errors[code], new_errors[code], f"method {name}.errors.{code}.data"
            )
        )
    return findings


def _compare_examples(
    name: str, before: dict[str, Any], after: dict[str, Any]
) -> list[Finding]:
    findings: list[Finding] = []
    old_cases = _cases(before)
    new_cases = _cases(after)
    for case in sorted(set(old_cases) - set(new_cases)):
        findings.append(
            Finding("BREAKING", f"path {name}.examples.{case}", "example case removed")
        )
    for case in sorted(set(new_cases) - set(old_cases)):
        findings.append(
            Finding("COMPATIBLE", f"path {name}.examples.{case}", "example case added")
        )
    for case in sorted(set(old_cases) & set(new_cases)):
        if old_cases[case].get("kind") != new_cases[case].get("kind"):
            findings.append(
                Finding("BREAKING", f"path {name}.examples.{case}", "scenario kind changed")
            )
        elif old_cases[case] != new_cases[case]:
            findings.append(
                Finding("NOTICE", f"path {name}.examples.{case}", "fixture values changed")
            )
    old_gaps = before.get("x-hyprial-example-gaps")
    new_gaps = after.get("x-hyprial-example-gaps")
    old_gaps = old_gaps if isinstance(old_gaps, dict) else {}
    new_gaps = new_gaps if isinstance(new_gaps, dict) else {}
    for gap in sorted(set(new_gaps) - set(old_gaps)):
        findings.append(
            Finding("BREAKING", f"path {name}.gaps.{gap}", "a scenario became unavailable")
        )
    for gap in sorted(set(old_gaps) - set(new_gaps)):
        findings.append(
            Finding("COMPATIBLE", f"path {name}.gaps.{gap}", "a scenario became available")
        )
    return findings


def _compare_operation(name: str, before: dict[str, Any], after: dict[str, Any]) -> list[Finding]:
    findings: list[Finding] = []
    findings.extend(
        _schema_findings(
            before.get("paramsSchema"), after.get("paramsSchema"), f"method {name}.params"
        )
    )
    findings.extend(
        _schema_findings(
            before.get("resultSchema"), after.get("resultSchema"), f"method {name}.result"
        )
    )
    findings.extend(_compare_errors(name, before, after))
    return findings


def compare(base: dict[str, Any], head: dict[str, Any]) -> list[Finding]:
    findings: list[Finding] = []
    before_methods = _methods(base)
    after_methods = _methods(head)
    for name in sorted(set(before_methods) - set(after_methods)):
        findings.append(Finding("BREAKING", f"method {name}", "method removed"))
    for name in sorted(set(after_methods) - set(before_methods)):
        findings.append(Finding("COMPATIBLE", f"method {name}", "method added"))
    for name in sorted(set(before_methods) & set(after_methods)):
        findings.extend(_compare_meta(name, before_methods[name], after_methods[name]))
        findings.extend(
            _compare_operation(name, before_methods[name], after_methods[name])
        )
    before_paths = _paths(base)
    after_paths = _paths(head)
    for path in sorted(set(before_paths) - set(after_paths)):
        findings.append(Finding("BREAKING", f"path {path}", "HTTP path removed"))
    for path in sorted(set(after_paths) - set(before_paths)):
        findings.append(Finding("COMPATIBLE", f"path {path}", "HTTP path added"))
    for path in sorted(set(before_paths) & set(after_paths)):
        name = path.rsplit("/", 1)[-1]
        findings.extend(
            _compare_examples(name, before_paths[path], after_paths[path])
        )
    return findings


def _declaration(body: str) -> str | None:
    for line in body.splitlines():
        match = _DECLARE_RE.match(line)
        if match is not None:
            return match.group("reason")
    return None


def _load(path: Path) -> dict[str, Any]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"cannot read {path}: {error}") from error
    if not isinstance(document, dict):
        raise ValueError(f"{path} is not a JSON object")
    return document


def _git_text(*args: str) -> str:
    result = subprocess.run(
        ["git", *args], capture_output=True, check=False
    )
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", "replace").strip()
        raise ValueError(f"git {' '.join(args)} failed: {detail}")
    return result.stdout.decode("utf-8", "replace").strip()


def _base_document_from_ref(ref: str) -> dict[str, Any]:
    toplevel = Path(_git_text("rev-parse", "--show-toplevel"))
    merge_base = _git_text("merge-base", ref, "HEAD")
    present = subprocess.run(
        ["git", "-C", str(toplevel), "cat-file", "-e", f"{merge_base}:{PROJECTION_IN_REPO}"],
        capture_output=True,
        check=False,
    )
    if present.returncode != 0:
        # The fork point never published a projection (the first dev -> main
        # promotion): nothing reached consumers from that base, so every
        # method is an addition.  An unreadable merge-base still fails above.
        print(
            f"daemon-contract-diff: {merge_base} has no {PROJECTION_IN_REPO}; "
            "first publication, diffing against an empty projection",
            file=sys.stderr,
        )
        return {}
    text = _git_text("-C", str(toplevel), "show", f"{merge_base}:{PROJECTION_IN_REPO}")
    try:
        document = json.loads(text)
    except json.JSONDecodeError as error:
        raise ValueError(
            f"{merge_base}:{PROJECTION_IN_REPO} is not valid JSON: {error}"
        ) from error
    if not isinstance(document, dict):
        raise ValueError(f"{merge_base}:{PROJECTION_IN_REPO} is not a JSON object")
    return document


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    base_source = parser.add_mutually_exclusive_group(required=True)
    base_source.add_argument("--base", type=Path, help="explicit base projection file")
    base_source.add_argument(
        "--base-ref",
        help="git ref; diff against the projection at merge-base(REF, HEAD)",
    )
    parser.add_argument("--head", type=Path, default=DEFAULT_HEAD)
    parser.add_argument("--pr-body-file", type=Path, default=None)
    args = parser.parse_args(argv)
    try:
        if args.base_ref is not None:
            base = _base_document_from_ref(args.base_ref)
        else:
            base = _load(args.base)
        head = _load(args.head)
    except ValueError as error:
        print(f"daemon-contract-diff: OPERATIONAL: {error}", file=sys.stderr)
        return EXIT_OPERATIONAL
    findings = compare(base, head)
    for finding in findings:
        print(finding.render())
    breaking = [finding for finding in findings if finding.severity == "BREAKING"]
    compatible = [finding for finding in findings if finding.severity == "COMPATIBLE"]
    notices = [finding for finding in findings if finding.severity == "NOTICE"]
    print(
        f"methods: base={len(_methods(base))} head={len(_methods(head))}; "
        f"breaking={len(breaking)} compatible={len(compatible)} "
        f"notices={len(notices)}"
    )
    if not breaking:
        return EXIT_GREEN
    body = ""
    if args.pr_body_file is not None:
        try:
            body = args.pr_body_file.read_text(encoding="utf-8")
        except OSError as error:
            print(
                f"daemon-contract-diff: OPERATIONAL: cannot read PR body: {error}",
                file=sys.stderr,
            )
            return EXIT_OPERATIONAL
    reason = _declaration(body)
    if reason:
        print(f"Contract-Impact declared: {reason}")
        return EXIT_GREEN
    print(
        "BREAKING contract changes require a PR-body line "
        "'Contract-Impact: <reviewed reason>' and synchronized consumers.",
        file=sys.stderr,
    )
    return EXIT_RED


if __name__ == "__main__":
    raise SystemExit(main())
