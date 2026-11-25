#!/usr/bin/env python3
"""Aggregate per-scenario exit codes into one controlled-gate verdict.

Each scenario step of e2e-macos-credentialed.yml writes the runner's exit code to
``<results-dir>/<SCENARIO>.code`` (0=PASS, 1=FAIL, 2=BLOCKED, 3=NOT-RUN
because the preflight refused, 4=AWAITING-USER with bounded resume evidence,
5=MACHINE-BUSY because another E2E run held this host's lock).
This script prints the matrix — FAIL and
BLOCKED/NOT-RUN on separate lines, so an absent or environment-blocked
scenario is never indistinguishable from a pass — and exits nonzero unless
every
*gating* scenario recorded 0. E2E-009 is not read here: it needs no
credential, so its record lives in the isolated gate (run-isolated-gate.py,
e2e-macos-isolated.yml), whose own exit fails the job when that scenario
fails. Absence is not PASS: a missing gating record is a gate failure of its
own.

Non-gating scenarios (currently none — E2E-008 was promoted after the
channel-contract fix proved the smoke catches real regressions) are
recorded and printed with the same absence-is-not-PASS discipline — a
missing record is printed as its own line, never silently dropped — but
they never flip the verdict.
"""

from __future__ import annotations

import sys
from pathlib import Path

REQUIRED_SCENARIOS = (
    "E2E-004",
    "E2E-006",
    "E2E-008",
    "E2E-012",
    "E2E-013",
    "E2E-025",
)

# No non-gating scenarios remain.  E2E-008 was promoted to gating after the
# CC >=2.1.227 channel-contract fix (#94/#197): its smoke caught that real
# regression from the very first run while three wrong attributions waved it
# away — the non-gating exemption only taught everyone to ignore a correct
# alarm.  Standing rule (set 2026-08-19): if this check ever goes red again,
# a matching production signature means an incident gets filed; it must
# never be waived back to non-gating to buy quiet.
NON_GATING_SCENARIOS: tuple[str, ...] = ()

STATUS_BY_CODE = {
    0: "PASS",
    1: "FAIL",
    2: "BLOCKED",
    3: "NOT-RUN (preflight refused)",
    4: "AWAITING-USER (bounded attended handoff)",
    # Deliberately its own code rather than folded into BLOCKED: "never
    # started because another E2E run held the machine" and "ran and hit an
    # environmental block" are different facts about a release candidate, and
    # a reader who cannot tell them apart will chase the wrong one.
    5: "MACHINE-BUSY (another E2E run held the machine lock; never started)",
}


def render_matrix(results_dir: Path) -> tuple[list[str], bool]:
    """Return (matrix lines, all-passed) for the recorded scenario codes.

    ``all-passed`` reflects gating scenarios only; non-gating smoke results
    are rendered with a ``(non-gating …)`` annotation and never move it.
    """

    lines: list[str] = []
    all_passed = True
    for scenario in REQUIRED_SCENARIOS:
        path = results_dir / f"{scenario}.code"
        if not path.is_file():
            lines.append(f"{scenario}: MISSING RECORD (absence is not PASS)")
            all_passed = False
            continue
        raw = path.read_text(encoding="utf-8").strip()
        try:
            code = int(raw)
        except ValueError:
            lines.append(f"{scenario}: UNREADABLE RECORD {raw!r}")
            all_passed = False
            continue
        status = STATUS_BY_CODE.get(code, f"FAIL (unexpected exit {code})")
        lines.append(f"{scenario}: {status}")
        if code != 0:
            all_passed = False
    for scenario in NON_GATING_SCENARIOS:
        path = results_dir / f"{scenario}.code"
        if not path.is_file():
            lines.append(
                f"{scenario}: MISSING RECORD (non-gating smoke; absence is "
                "not PASS, and not verdict-blocking)"
            )
            continue
        raw = path.read_text(encoding="utf-8").strip()
        try:
            code = int(raw)
        except ValueError:
            lines.append(
                f"{scenario}: UNREADABLE RECORD {raw!r} (non-gating smoke)"
            )
            continue
        status = STATUS_BY_CODE.get(code, f"FAIL (unexpected exit {code})")
        lines.append(f"{scenario}: {status} (non-gating smoke)")
    return lines, all_passed


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(f"usage: {argv[0]} <results-dir>", file=sys.stderr)
        return 2
    lines, all_passed = render_matrix(Path(argv[1]))
    print("controlled gate matrix (FAIL and BLOCKED/NOT-RUN are never green):")
    for line in lines:
        print(f"  {line}")
    if all_passed:
        print("VERDICT PASS: every gating scenario recorded PASS")
        return 0
    print("VERDICT FAIL: at least one gating scenario did not record PASS")
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
