# Decision record (file header; no inline comments by repository rule):
# - HQ review: a gate that is not green must say WHY. A case that cannot run
#   on this host (FREE-03 on the native backend without a free-threaded
#   interpreter) is reported explicitly unavailable with
#   `pytest.skip(f"{BLOCKED_PREFIX} ...")`, never as PASS and never as a
#   silent skip. Skips are otherwise forbidden in this suite, so a skip
#   WITHOUT the "BLOCKED:" prefix is a FAIL (AGENTS.md: skip is not a verdict).
# - Session verdict, in this order: FAIL if any test failed, errored or
#   XPASSed (strict or not), any collection error, any unprefixed skip, a
#   non-zero pytest exit status, or no test at all; otherwise BLOCKED if any
#   test was BLOCKED; otherwise PASS. Strict xfails (spec 16 known defects)
#   that fail as expected are neither failures nor blockers.
# - Exit status: PASS 0, BLOCKED 7 (EXIT_BLOCKED), FAIL pytest's own non-zero
#   status (1 when pytest itself would have returned 0, e.g. only an
#   unprefixed skip). So `0` means exactly "fully green".
# - The verdict logic is pure (`outcome_of`, `decide`) so cases/meta can test
#   it on fake reports; `VerdictPlugin` only wires it to pytest hooks. The
#   terminal section is printed from an outermost (tryfirst) terminal-summary
#   wrapper, so it comes after pytest's short test summary; `--verdict-json`
#   writes the same data as JSON.
"""Session verdict (PASS / FAIL / BLOCKED) and exit status of the conformance suite."""

from __future__ import annotations

import json
import sys
from dataclasses import asdict, dataclass, field
from typing import Any, Mapping

import pytest

BLOCKED_PREFIX = "BLOCKED:"
EXIT_PASS = 0
EXIT_FAIL = 1
EXIT_BLOCKED = 7


def _skip_reason(longrepr: Any) -> str:
    if isinstance(longrepr, tuple) and len(longrepr) == 3:
        message = str(longrepr[2])
    else:
        message = str(longrepr)
    return message[len("Skipped: "):] if message.startswith("Skipped: ") else message


def outcome_of(report: Any) -> tuple[str, str] | None:
    """Map one pytest TestReport phase to ``(outcome, reason)``; None for an uneventful phase.

    Outcomes: passed, failed, error, xfailed, xpassed, blocked, skipped (an
    unprefixed skip, which fails the session).
    """
    when, result = report.when, report.outcome
    xfail = hasattr(report, "wasxfail")
    if result == "failed":
        text = str(getattr(report, "longrepr", "") or "")
        if when == "call" and "[XPASS(strict)]" in text:
            return "xpassed", text
        return ("failed" if when == "call" else "error"), text.splitlines()[-1] if text else ""
    if result == "skipped":
        if xfail:
            return "xfailed", str(report.wasxfail)
        reason = _skip_reason(report.longrepr)
        return ("blocked" if reason.startswith(BLOCKED_PREFIX) else "skipped"), reason
    if when == "call":
        return ("xpassed", str(report.wasxfail)) if xfail else ("passed", "")
    return None


RANK = {"error": 6, "failed": 5, "xpassed": 4, "skipped": 3, "blocked": 2, "xfailed": 1, "passed": 0}


@dataclass
class Verdict:
    """The session verdict and its evidence (also the --verdict-json payload)."""

    verdict: str
    exit_status: int
    backend: str = ""
    python: str = ""
    python_executable: str = ""
    pytest_exit_status: int = 0
    passed: int = 0
    failed: int = 0
    errors: int = 0
    xfailed: int = 0
    xpassed: int = 0
    collection_errors: int = 0
    blocked: list[str] = field(default_factory=list)
    blocked_reasons: dict[str, str] = field(default_factory=dict)
    unprefixed_skips: list[str] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)


def decide(outcomes: Mapping[str, tuple[str, str]], pytest_exit_status: int = 0,
           collection_errors: int = 0, backend: str = "", python: str = "",
           python_executable: str = "") -> Verdict:
    """Pure verdict: ``outcomes`` maps nodeid -> (final outcome, reason)."""
    counts = {name: 0 for name in RANK}
    for outcome, _ in outcomes.values():
        counts[outcome] += 1
    reasons: list[str] = []
    for name, label in (("failed", "failed"), ("error", "errored"), ("xpassed", "XPASSed"),
                        ("skipped", "skipped without the BLOCKED: prefix")):
        if counts[name]:
            reasons.append(f"{counts[name]} test(s) {label}")
    if collection_errors:
        reasons.append(f"{collection_errors} collection error(s)")
    if not outcomes:
        reasons.append("no test ran")
    if pytest_exit_status not in (0, 1) or (pytest_exit_status == 1 and not reasons):
        reasons.append(f"pytest exit status {pytest_exit_status}")
    if reasons:
        verdict, status = "FAIL", pytest_exit_status or EXIT_FAIL
    elif counts["blocked"]:
        verdict, status = "BLOCKED", EXIT_BLOCKED
        reasons.append(f"{counts['blocked']} test(s) BLOCKED (explicitly unavailable on this host)")
    else:
        verdict, status = "PASS", EXIT_PASS
    blocked_ids = sorted(node for node, (outcome, _) in outcomes.items() if outcome == "blocked")
    return Verdict(
        verdict=verdict, exit_status=status, backend=backend, python=python,
        python_executable=python_executable, pytest_exit_status=pytest_exit_status,
        passed=counts["passed"], failed=counts["failed"], errors=counts["error"],
        xfailed=counts["xfailed"], xpassed=counts["xpassed"], collection_errors=collection_errors,
        blocked=blocked_ids, blocked_reasons={node: outcomes[node][1] for node in blocked_ids},
        unprefixed_skips=sorted(node for node, (outcome, _) in outcomes.items() if outcome == "skipped"),
        failures=sorted(node for node, (outcome, _) in outcomes.items()
                        if outcome in ("failed", "error", "xpassed")),
        reasons=reasons)


class VerdictPlugin:
    """Collects per-test outcomes, sets the exit status and prints / writes the verdict."""

    def __init__(self, config: pytest.Config) -> None:
        self.config = config
        self.outcomes: dict[str, tuple[str, str]] = {}
        self.collection_errors = 0
        self.verdict: Verdict | None = None

    def pytest_collectreport(self, report: pytest.CollectReport) -> None:
        if report.failed:
            self.collection_errors += 1

    def pytest_runtest_logreport(self, report: pytest.TestReport) -> None:
        mapped = outcome_of(report)
        if mapped is None:
            return
        previous = self.outcomes.get(report.nodeid)
        if previous is None or RANK[mapped[0]] > RANK[previous[0]]:
            self.outcomes[report.nodeid] = mapped

    @pytest.hookimpl(trylast=True)
    def pytest_sessionfinish(self, session: pytest.Session, exitstatus: int) -> None:
        self.verdict = decide(self.outcomes, int(session.exitstatus), self.collection_errors,
                              backend=str(self.config.getoption("--actor-backend", default="")),
                              python=sys.version.split()[0], python_executable=sys.executable)
        session.exitstatus = self.verdict.exit_status
        path = self.config.getoption("--verdict-json", default="")
        if path:
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(asdict(self.verdict), handle, indent=2, sort_keys=True)
                handle.write("\n")

    @pytest.hookimpl(wrapper=True, tryfirst=True)
    def pytest_terminal_summary(self, terminalreporter: Any) -> Any:
        result = yield
        verdict = self.verdict
        if verdict is not None:
            terminalreporter.section("conformance verdict")
            for node in verdict.blocked:
                reason = verdict.blocked_reasons[node][len(BLOCKED_PREFIX):].strip()
                terminalreporter.write_line(f"BLOCKED {node}: {reason}")
            for node in verdict.unprefixed_skips:
                terminalreporter.write_line(f"FORBIDDEN SKIP {node}")
            for reason in verdict.reasons:
                terminalreporter.write_line(f"- {reason}")
            terminalreporter.write_line(
                f"CONFORMANCE VERDICT: {verdict.verdict} backend={verdict.backend} "
                f"python={verdict.python} exit={verdict.exit_status}",
                bold=True, red=verdict.verdict == "FAIL", yellow=verdict.verdict == "BLOCKED",
                green=verdict.verdict == "PASS")
        return result
