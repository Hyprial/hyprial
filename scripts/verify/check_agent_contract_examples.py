#!/usr/bin/env python3
"""Three-state gate for the five Agent request-method examples.

Decision record (M-03, 2026-10-31)
- This script reads only the committed business OpenAPI projection.  It does
  not import the daemon, start a server, read a home or open a socket, so the
  mock/example artifact can be checked on a machine with no runtime.
- PASS means all five methods are present and each has success, a legal
  empty/unconfigured state or an explicit reason why it has none, a true
  schema-invalid parameters case, and a schema-valid handler/domain error or an
  explicit reason.  BLOCKED
  means the accepted M-01/M-02 declarations are not in this projection yet;
  it is never reported as PASS.  FAIL means the projection has a defect.
- The sensitive-material scan is part of FAIL, not a separate advisory: a real
  token in an example is a publication bug.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
CONTRACT_ROOT = ROOT / "contract" / "daemon-request"
if str(CONTRACT_ROOT) not in sys.path:
    sys.path.insert(0, str(CONTRACT_ROOT))

from contract_mock.sensitive import scan_value  # noqa: E402

DEFAULT_OPENAPI = CONTRACT_ROOT / "openapi.json"
REQUIRED_METHODS = (
    "agent.list",
    "agent.secret.list",
    "agent.provider.default",
    "agent.provider.login",
    "agent.provider.logout",
)
EXIT_PASS = 0
EXIT_FAIL = 1
EXIT_BLOCKED = 2


@dataclass
class Report:
    verdict: str
    missing_methods: list[str] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)
    sensitive: list[str] = field(default_factory=list)
    examples: dict[str, int] = field(default_factory=dict)

    def lines(self) -> list[str]:
        lines = [
            f"verdict: {self.verdict}",
            f"required methods: {len(REQUIRED_METHODS)}",
            f"missing methods: {len(self.missing_methods)}",
            f"example cases: {sum(self.examples.values())}",
            f"failures: {len(self.failures)}",
            f"sensitive findings: {len(self.sensitive)}",
        ]
        for method in self.missing_methods:
            lines.append(f"MISSING {method}")
        for failure in self.failures:
            lines.append(f"FAIL {failure}")
        for finding in self.sensitive:
            lines.append(f"SENSITIVE {finding}")
        return lines


def _operation(document: dict[str, Any], method: str) -> dict[str, Any] | None:
    for entry in document.get("x-hyprial-methods", []):
        if entry.get("name") == method and (
            not entry.get("httpExposed")
            or (entry.get("httpBinding") or {}).get("readOnlyParamsSchema") is not None
        ):
            return entry
    paths = document.get("paths")
    if not isinstance(paths, dict):
        return None
    path = paths.get(f"/daemon/request/{method}")
    if not isinstance(path, dict):
        return None
    operation = path.get("post")
    return operation if isinstance(operation, dict) else None


def evaluate(
    document: dict[str, Any], required: tuple[str, ...] = REQUIRED_METHODS
) -> Report:
    report = Report(verdict="BLOCKED")
    for method in required:
        operation = _operation(document, method)
        if operation is None:
            report.missing_methods.append(method)
            continue
        examples = operation.get("x-hyprial-examples")
        if not isinstance(examples, list) or not examples:
            report.failures.append(f"{method}: no x-hyprial-examples")
            report.examples[method] = 0
            continue
        report.examples[method] = len(examples)
        gaps = operation.get("x-hyprial-example-gaps")
        gaps = gaps if isinstance(gaps, dict) else {}
        kinds = {
            case.get("kind")
            for case in examples
            if isinstance(case, dict) and isinstance(case.get("kind"), str)
        }
        if "success" not in kinds:
            report.failures.append(f"{method}: no success example")
        if not (kinds & {"empty", "unconfigured"}) and not gaps.get("empty"):
            report.failures.append(
                f"{method}: no empty/unconfigured example or empty_not_applicable reason"
            )
        if "invalid-params" not in kinds:
            report.failures.append(f"{method}: no invalid-params example")
        if not (kinds & {"domain-error", "semantic-param-error"}) and not gaps.get(
            "domainErrors"
        ):
            report.failures.append(
                f"{method}: no domain-error/semantic-param-error example or "
                "domain_errors_not_applicable reason"
            )
    material: dict[str, Any] = {}
    paths = document.get("paths")
    if isinstance(paths, dict):
        for path, entry in paths.items():
            if not isinstance(entry, dict):
                continue
            operation = entry.get("post")
            if isinstance(operation, dict) and isinstance(
                operation.get("x-hyprial-examples"), list
            ):
                material[path] = operation["x-hyprial-examples"]
    material["declarations"] = [
        entry.get("x-hyprial-examples", [])
        for entry in document.get("x-hyprial-methods", [])
    ]
    report.sensitive.extend(scan_value(material, "examples"))
    if report.failures or report.sensitive:
        report.verdict = "FAIL"
    elif report.missing_methods:
        report.verdict = "BLOCKED"
    else:
        report.verdict = "PASS"
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--openapi", type=Path, default=DEFAULT_OPENAPI)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        document = json.loads(args.openapi.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        print(f"BLOCKED cannot read {args.openapi}: {error}", file=sys.stderr)
        return EXIT_BLOCKED
    if not isinstance(document, dict):
        print(f"FAIL {args.openapi} is not a JSON object", file=sys.stderr)
        return EXIT_FAIL
    report = evaluate(document)
    if args.json:
        print(
            json.dumps(
                {
                    "verdict": report.verdict,
                    "missingMethods": report.missing_methods,
                    "failures": report.failures,
                    "sensitive": report.sensitive,
                    "examples": report.examples,
                },
                sort_keys=True,
            )
        )
    else:
        for line in report.lines():
            print(line)
        if report.verdict == "BLOCKED":
            print(
                "BLOCKED: the accepted M-01/M-02 declarations are not in this "
                "projection; this is not a pass.",
                file=sys.stderr,
            )
    if report.verdict == "PASS":
        return EXIT_PASS
    if report.verdict == "FAIL":
        return EXIT_FAIL
    return EXIT_BLOCKED


if __name__ == "__main__":
    raise SystemExit(main())
