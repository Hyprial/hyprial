#!/usr/bin/env python3
"""Validate the documented E2E scenario inventory."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
REQUIRED_IDS = {
    "E2E-001",
    "E2E-002",
    "E2E-003",
    "E2E-004",
    "E2E-005",
    "E2E-006",
    "E2E-007",
    "E2E-008",
    "E2E-009",
    "E2E-010",
    "E2E-011",
    "E2E-012",
    "E2E-013",
    "E2E-015",
    "E2E-016",
    "E2E-017",
    "E2E-018",
    "E2E-020",
    "E2E-021",
    "E2E-024",
    "E2E-025",
    "E2E-028",
    "E2E-029",
    "E2E-030",
    "E2E-031",
    "E2E-032",
}


def main() -> int:
    document = json.loads((ROOT / "scenarios.json").read_text(encoding="utf-8"))
    scenarios = document.get("scenarios")
    if not isinstance(scenarios, list):
        raise TypeError("scenarios must be an array")
    ids = {scenario.get("id") for scenario in scenarios if isinstance(scenario, dict)}
    if ids != REQUIRED_IDS:
        raise AssertionError(
            "scenario IDs must be exactly E2E-001 through E2E-013, E2E-015 through E2E-018, E2E-020, E2E-021, and E2E-024 through E2E-032 (E2E-023 is reserved for the routine-binding scenario); "
            f"missing {sorted(REQUIRED_IDS - ids)}; "
            f"unexpected {sorted(ids - REQUIRED_IDS)}"
        )
    # Keep the release inventory closed: E2E-019 remains intentionally absent.
    # Every later addition needs its explicit baseline decision, runner,
    # document, gate wiring, and ID set in the same change.
    # E2E-022 (public install + self-hosted tailnet) retired with the tsnet
    # sidecar (Tailcat cutover): it depended on Headscale pre-auth join and
    # the pin-verified public tsnet binary, both gone.
    import re as _re

    for scenario_id in ids:
        if not isinstance(scenario_id, str) or not _re.fullmatch(r"E2E-\d{3}", scenario_id):
            raise AssertionError(f"scenario id has an invalid shape: {scenario_id!r}")
    if len(ids) != len(scenarios):
        raise AssertionError("duplicate scenario id present")
    for scenario in scenarios:
        path = ROOT / scenario["document"]
        if not path.is_file():
            raise AssertionError(f"missing scenario document: {path.name}")
        text = path.read_text(encoding="utf-8")
        for heading in ("## Preconditions", "## Steps", "## Assertions"):
            if heading not in text:
                raise AssertionError(f"{path.name} is missing {heading}")
        assertions = scenario.get("assertions")
        if not isinstance(assertions, list) or not assertions:
            raise AssertionError(f"{scenario['id']} needs declarative assertions")
        if scenario.get("real_environment_required") and "[REAL ENV]" not in text:
            raise AssertionError(f"{path.name} must mark real-environment lines")
        if scenario.get("executable"):
            runner = ROOT / scenario.get("runner", "")
            if not runner.is_file() or not os.access(runner, os.X_OK):
                raise AssertionError(
                    f"{scenario['id']} needs an executable runner: {runner.name}"
                )
            if "[REAL ENV]" in text and not scenario.get("real_environment_required"):
                raise AssertionError(
                    f"{path.name} is isolated automation and must not require REAL ENV"
                )
            runner_text = runner.read_text(encoding="utf-8")
            if "SCENARIO =" in runner_text and f'SCENARIO = "{scenario["id"]}"' not in runner_text:
                raise AssertionError(
                    f"{runner.name} SCENARIO does not match {scenario['id']}"
                )
    print(f"RESULT PASS {len(scenarios)} documented E2E scenarios")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (AssertionError, TypeError, json.JSONDecodeError) as error:
        print(f"RESULT FAIL {error}")
        sys.exit(1)
