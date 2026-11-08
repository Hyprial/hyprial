"""KNOWN_FAILURES absorbs only the recorded failure, and never a pass."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
from typing import Any

import pytest


SUITE = Path(__file__).resolve().parents[4] / "contract/e2e/run-hosted-suite.py"


@pytest.fixture
def suite(monkeypatch: pytest.MonkeyPatch) -> Any:
    spec = importlib.util.spec_from_file_location("hosted_suite_known", SUITE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    monkeypatch.setattr(sys, "path", list(sys.path))
    spec.loader.exec_module(module)
    return module


def _row(result: str, code: int) -> dict[str, Any]:
    return {"scenario": "hosted-upgrade", "exitCode": code, "result": result}


def _track_synthetic_gap(suite: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(
        suite.KNOWN_FAILURES,
        "hosted-upgrade",
        {
            "signature": "MEMBERSHIP_NOT_INITIALIZED",
            "reason": "classifier fixture",
            "tracking": "https://code.hyprial.com/HyprialOS/harness-bridge/pulls/2539",
        },
    )


def test_recorded_signature_is_known_fail_not_red(suite: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    _track_synthetic_gap(suite, monkeypatch)
    row = _row("FAIL", 1)
    report = {"traceback": "AssertionError: org create returned no space id: "
                           "{'ok': False, 'code': 'MEMBERSHIP_NOT_INITIALIZED'}"}
    assert suite.classify_known_failure("hosted-upgrade", row, report) is False
    assert row["result"] == "KNOWN-FAIL"
    assert row["knownFailure"]["tracking"].endswith("/pulls/2539")


def test_other_failure_of_a_listed_scenario_stays_red(suite: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    _track_synthetic_gap(suite, monkeypatch)
    row = _row("FAIL", 1)
    assert suite.classify_known_failure("hosted-upgrade", row, {"traceback": "rollback mismatch"}) is True
    assert row["result"] == "FAIL"


@pytest.mark.parametrize("result", ["INCONCLUSIVE", "NOT_RUN"])
def test_non_fail_outcomes_are_never_absorbed(
    suite: Any, result: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _track_synthetic_gap(suite, monkeypatch)
    report = {"traceback": "MEMBERSHIP_NOT_INITIALIZED"}
    assert suite.classify_known_failure("hosted-upgrade", _row(result, 1), report) is True


def test_unexpected_pass_is_red_until_the_entry_is_removed(
    suite: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _track_synthetic_gap(suite, monkeypatch)
    row = _row("PASS", 0)
    assert suite.classify_known_failure("hosted-upgrade", row, {}) is True
    assert row["result"] == "UNEXPECTED-PASS"


def test_unlisted_scenarios_keep_the_plain_rule(suite: Any) -> None:
    assert suite.classify_known_failure("host-invite", _row("PASS", 0), {}) is False
    assert suite.classify_known_failure(
        "host-invite", _row("FAIL", 1), {"traceback": "MEMBERSHIP_NOT_INITIALIZED"}
    ) is True


def test_only_the_tracked_gap_is_listed(suite: Any) -> None:
    assert set(suite.KNOWN_FAILURES) == set()
