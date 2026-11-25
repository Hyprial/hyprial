from __future__ import annotations

from scripts.verify.verify_installed_gui import BLOCKED, FAIL, PASS, Step, verdict


def test_verdict_passes_only_when_all_steps_pass() -> None:
    assert verdict([Step("build", "PASS", "ok"), Step("browser", "PASS", "ok")]) == ("PASS", PASS)


def test_verdict_reports_first_failure_before_blocked_steps() -> None:
    line, code = verdict([Step("build", "PASS", "ok"), Step("browser", "FAIL", "501"), Step("lifecycle", "BLOCKED", "unknown")])
    assert line == "FAIL browser 501"
    assert code == FAIL


def test_verdict_reports_blocked_when_no_step_failed() -> None:
    line, code = verdict([Step("build", "BLOCKED", "native tool missing")])
    assert line == "BLOCKED build native tool missing"
    assert code == BLOCKED
