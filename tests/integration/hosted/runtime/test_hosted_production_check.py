"""The hosted e2e runners' result follows one explicit precedence and is always reported.

run-host-invite.py main() (shared by run-capability-ledger, run-hosted-start and
run-hosted-upgrade) pings the production daemon before and after a scenario; an
unchanged pid/epoch is the only evidence the run left production untouched.
The after-ping always runs (#1004), and the result string is decided by one
fixed precedence, highest first:

1. INCONCLUSIVE: production identity changed -- both pings obtained and differ
2. INCONCLUSIVE: production check           -- either ping could not be obtained
3. FAIL                                     -- the scenario raised
4. INCONCLUSIVE: scenario did not complete  -- a BaseException (SystemExit,
   KeyboardInterrupt) stopped run() before it returned, or stopped the setup
   before run() started
5. INCONCLUSIVE: cleanup                    -- stop() failed after a passed scenario
6. PASS                                     -- only when run() actually returned

PASS is a positive completion marker, never the mere absence of error fields,
and a caught BaseException is re-raised after the report is written and printed --
with one adjustment: a SystemExit whose code is 0 or None is re-raised as
SystemExit(1) (the original kept as __cause__), because the runner must never
exit 0 on a non-PASS result.
Every error stays in its own field (productionBeforeError, productionCheckError,
cleanupError, traceback, interrupted), result.json and the stdout report are
attempted even when ledger collection or the result write itself fails
(ledgersError, resultWriteError), and run-hosted-suite.py carries the error
fields into its manifest summary.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys
from typing import Any

import pytest


RUNNER = Path(__file__).resolve().parents[4] / 'contract/e2e/run-host-invite.py'
SUITE = Path(__file__).resolve().parents[4] / 'contract/e2e/run-hosted-suite.py'
BEFORE = {"pid": 101, "epoch": "e1"}


def _load_module(path: Path, name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load(name: str) -> Any:
    return _load_module(RUNNER, name)


def _scenario(stop_error: Exception | None, run_error: Exception | None = None) -> type:
    class FakeScenario:
        scope = "fake; no daemon"

        def __init__(self, root: Path, production_socket: Path) -> None:
            self.root = root

        def run(self) -> None:
            if run_error is not None:
                raise run_error

        def stop(self) -> None:
            if stop_error is not None:
                raise stop_error

    return FakeScenario


def _run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, stop_error: Exception | None = None,
         run_error: Exception | None = None, before: Any = BEFORE, after: Any = BEFORE,
         ledger_error: Exception | None = None, write_error: Exception | None = None,
         ) -> tuple[int, dict, list[str]]:
    runner = _load(f"prod_check_{tmp_path.name}")
    root = tmp_path / "root"
    root.mkdir()
    pings: list[str] = []

    def identity(path: Path) -> dict:
        pings.append("before" if not pings else "after")
        if len(pings) == 1:
            if isinstance(before, Exception):
                raise before
            return dict(before)
        if isinstance(after, Exception):
            raise after
        return dict(after)

    monkeypatch.setattr(runner, "production_identity", identity)
    if ledger_error is not None:
        def broken_ledgers(_root: Path) -> list:
            raise ledger_error
        monkeypatch.setattr(runner, "ledger_paths", broken_ledgers)
    if write_error is not None:
        def broken_write(_path: Path, _value: Any) -> None:
            raise write_error
        monkeypatch.setattr(runner, "write_json", broken_write)
    monkeypatch.setattr(runner.tempfile, "mkdtemp", lambda **_: str(root))
    monkeypatch.setattr(sys, "argv", ["run-host-invite.py", "--production-socket", str(tmp_path / "prod.sock")])
    code = runner.main(_scenario(stop_error, run_error))
    result_path = root / "result.json"
    report = json.loads(result_path.read_text()) if result_path.exists() else {}
    return code, report, pings


def test_clean_run_passes_with_both_pings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    code, report, pings = _run(tmp_path, monkeypatch)
    assert (code, report["result"], pings) == (0, "PASS", ["before", "after"])
    assert report["productionAfter"] == report["productionBefore"] == BEFORE


def test_failed_cleanup_still_runs_the_production_check(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    code, report, pings = _run(tmp_path, monkeypatch, stop_error=RuntimeError("stop failed"))
    assert pings == ["before", "after"]
    assert report["productionAfter"] == BEFORE
    assert (code, report["result"]) == (1, "INCONCLUSIVE: cleanup")
    assert "stop failed" in report["cleanupError"]


def test_changed_production_identity_outranks_a_failed_cleanup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    code, report, _ = _run(tmp_path, monkeypatch, stop_error=RuntimeError("stop failed"),
                           after={"pid": 202, "epoch": "e2"})
    assert (code, report["result"]) == (1, "INCONCLUSIVE: production identity changed")
    assert report["productionAfter"] == {"pid": 202, "epoch": "e2"}
    assert "stop failed" in report["cleanupError"]


def test_failed_production_ping_is_reported_separately(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    code, report, _ = _run(tmp_path, monkeypatch, after=OSError("socket gone"))
    assert (code, report["result"]) == (1, "INCONCLUSIVE: production check")
    assert "socket gone" in report["productionCheckError"]
    assert "cleanupError" not in report and "productionAfter" not in report


def test_failed_before_ping_is_a_production_check_not_an_identity_change(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Nothing was compared when the before-ping failed, so 'identity changed' is a lie."""
    code, report, pings = _run(tmp_path, monkeypatch, before=OSError("socket refused"))
    assert pings == ["before", "after"], "the after-ping still always runs"
    assert (code, report["result"]) == (1, "INCONCLUSIVE: production check")
    assert "socket refused" in report["productionBeforeError"]
    assert report["productionAfter"] == BEFORE
    assert "productionBefore" not in report
    assert "traceback" not in report, "the scenario never ran: the before-ping error is not the scenario's"


def test_scenario_system_exit_is_inconclusive_not_pass_and_still_propagates(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """A BaseException from run() is not PASS: it is recorded and reported, then re-raised."""
    runner = _load(f"prod_check_interrupt_{tmp_path.name}")
    root = tmp_path / "root"
    root.mkdir()
    monkeypatch.setattr(runner, "production_identity", lambda _path: dict(BEFORE))
    monkeypatch.setattr(runner.tempfile, "mkdtemp", lambda **_: str(root))
    monkeypatch.setattr(sys, "argv", ["run-host-invite.py", "--production-socket", str(tmp_path / "prod.sock")])
    with pytest.raises(SystemExit):
        runner.main(_scenario(None, SystemExit(3)))
    report = json.loads((root / "result.json").read_text())
    assert report["result"] == "INCONCLUSIVE: scenario did not complete"
    assert "SystemExit" in report["interrupted"] and "Traceback" in report["interrupted"]
    assert report["productionAfter"] == BEFORE, "the after-ping still ran before the re-raise"
    printed = json.loads(capsys.readouterr().out)
    assert printed["result"] == report["result"]
    assert printed["interrupted"] == report["interrupted"]


@pytest.mark.parametrize("code", [0, None], ids=["zero", "none"])
def test_zero_system_exit_propagates_as_failure_not_success(
        code: int | None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A zero SystemExit deferred from run() must never exit 0 on a non-PASS result:
    it is re-raised as SystemExit(1) with the original interruption as __cause__."""
    runner = _load(f"prod_check_zero_exit_{tmp_path.name}")
    root = tmp_path / "root"
    root.mkdir()
    monkeypatch.setattr(runner, "production_identity", lambda _path: dict(BEFORE))
    monkeypatch.setattr(runner.tempfile, "mkdtemp", lambda **_: str(root))
    monkeypatch.setattr(sys, "argv", ["run-host-invite.py", "--production-socket", str(tmp_path / "prod.sock")])
    with pytest.raises(SystemExit) as raised:
        runner.main(_scenario(None, SystemExit(code)))
    assert raised.value.code == 1, "exit 0 would claim success for a non-PASS result"
    cause = raised.value.__cause__
    assert isinstance(cause, SystemExit) and cause.code == code, "the original SystemExit is the __cause__"
    report = json.loads((root / "result.json").read_text())
    assert report["result"] == "INCONCLUSIVE: scenario did not complete"
    assert not report["result"].startswith("PASS")


def test_scenario_failure_outranks_a_failed_cleanup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A FAILED scenario that also failed stop() stays FAIL; the cleanup error keeps its own field."""
    code, report, _ = _run(tmp_path, monkeypatch, run_error=RuntimeError("scenario boom"),
                           stop_error=RuntimeError("stop failed"))
    assert (code, report["result"]) == (1, "FAIL")
    assert "scenario boom" in report["traceback"]
    assert "stop failed" in report["cleanupError"]


def test_failed_ledger_collection_is_recorded_and_the_report_still_written(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    code, report, _ = _run(tmp_path, monkeypatch, ledger_error=OSError("ledger walk blew up"))
    assert (code, report["result"]) == (0, "PASS")
    assert "ledgers" not in report
    assert "ledger walk blew up" in report["ledgersError"]
    printed = json.loads(capsys.readouterr().out)
    assert printed["result"] == "PASS" and printed["ledgersError"] == report["ledgersError"]


def test_failed_result_write_still_prints_the_report(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    code, report, _ = _run(tmp_path, monkeypatch, write_error=OSError("disk full"))
    assert report == {}, "result.json could not be written..."
    printed = json.loads(capsys.readouterr().out)
    assert (code, printed["result"]) == (0, "PASS"), "...but stdout still carries the report"
    assert "disk full" in printed["resultWriteError"]
    assert "ledgers" in printed, "ledger collection is independent of the result write"


def test_suite_manifest_summary_carries_the_error_fields(
        tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    suite = _load_module(SUITE, f"hosted_suite_manifest_{tmp_path.name}")
    scenario_dir = tmp_path / "host-invite"
    scenario_dir.mkdir()
    errors = {"traceback": "scenario boom", "productionBeforeError": "before boom",
              "productionCheckError": "after boom", "cleanupError": "stop boom",
              "ledgersError": "ledgers boom", "resultWriteError": "write boom"}
    (scenario_dir / "result.json").write_text(json.dumps({
        "head": "0" * 40, "productionBefore": BEFORE, "productionAfter": BEFORE,
        "runnerSha256": "f" * 64, **errors}))
    suite.publish_evidence(tmp_path, [{"scenario": "host-invite", "result": "FAIL", "exitCode": 1}])
    summary = json.loads(capsys.readouterr().out.splitlines()[0])
    assert summary["result"] == "FAIL"
    for key, value in errors.items():
        assert summary[key] == value, f"{key} dropped from the manifest summary"
