"""Restored read-only diagnosis shares the F2 probe referee."""

from pathlib import Path

import pytest

from hyprial.daemon.impl.dispatch import matrix
from hyprial.daemon.impl.squire import probe


@pytest.mark.parametrize(
    "run",
    [probe.ProbeRun(0, ""), probe.ProbeRun(1, "rate limit"),
     probe.ProbeRun(None, "missing"), probe.ProbeRun(0, "ok", timed_out=True)],
)
def test_diagnose_uses_f2_verdict_without_writing_or_selecting(run, monkeypatch):
    monkeypatch.setattr(matrix, "active_profile", lambda: None)
    readings = matrix.diagnose("fast", runner=lambda *_: run)
    assert [reading.status for reading in readings] == [
        probe.classify_run(run)[0]
    ] * len(matrix.TIERS["fast"])
    assert matrix.resolve("fast").selected == matrix.TIERS["fast"][0]
    assert matrix.resolve("fast").readings == ()


def test_diagnose_delegates_to_referee(monkeypatch):
    calls = []
    run = probe.ProbeRun(0, "looks alive")

    def referee(observed):
        calls.append(observed)
        return "unavailable", "probe-error", None

    monkeypatch.setattr(matrix, "active_profile", lambda: None)
    monkeypatch.setattr(probe, "classify_run", referee)
    readings = matrix.diagnose("super", runner=lambda *_: run)
    assert calls == [run] * len(matrix.TIERS["super"])
    assert all(reading.status == "unavailable" for reading in readings)


def test_tiers_shell_does_not_own_profile_or_prober():
    from hyprial.shell.impl.cli.commands.agent.runtime import tiers

    source = Path(tiers.__file__).read_text()
    assert "UserProfileStore" not in source
    assert "RuntimeProber" not in source


def test_shared_tier_report_can_diagnose_without_changing_static_selection(monkeypatch):
    from hyprial.shell.impl.cli.commands.workflow.internal import dispatch

    monkeypatch.setattr(matrix, "active_profile", lambda: None)
    monkeypatch.setattr(matrix, "workflow_reminders", lambda: [])
    report = dispatch.tier_report(
        "super", probe=True, runner=lambda *_: probe.ProbeRun(1, "quota")
    )
    assert report["selection"]["super"]["model"] == matrix.TIERS["super"][0].model
    assert all(row["status"] == "unavailable" for row in report["diagnostics"]["super"])
