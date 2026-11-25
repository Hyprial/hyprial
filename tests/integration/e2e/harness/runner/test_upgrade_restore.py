"""A restore-gated ps read waits on light ping; it never invents empty state."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from contract.e2e._upgrade_continuity import installation


class Clock:
    now = 0.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def test_ps_waits_for_restore_before_reading_full_state() -> None:
    clock = Clock()
    calls: list[str] = []
    snapshot = {"pendingMessages": [{"messageId": "must-not-disappear"}]}

    def ipc(state: Path, method: str, params: dict[str, Any]) -> dict[str, Any]:
        calls.append(method)
        if method == "ping":
            return {"restorePending": clock.now < .4}
        assert clock.now >= .4
        assert method == "ps"
        return snapshot

    assert installation.daemon_ps({"ipc": ipc, "time": clock}, Path("/tmp/hr-upgrade-restore/state"), timeout=1) is snapshot
    assert calls == ["ping", "ping", "ping", "ps"]


def test_restore_timeout_is_failure_not_empty_success() -> None:
    clock = Clock()
    def ipc(state: Path, method: str, params: dict[str, Any]) -> dict[str, Any]:
        assert method == "ping", "heavy ps must not cross the restore gate"
        return {"restorePending": True}

    with pytest.raises(AssertionError, match="restore did not settle"):
        installation.daemon_ps({"ipc": ipc, "time": clock}, Path("/tmp/hr-upgrade-restore/state"), timeout=.4)
    assert clock.now == .4


def test_unrelated_ipc_error_is_not_swallowed() -> None:
    def ipc(*args: Any) -> dict[str, Any]:
        raise AssertionError("unrelated failure")

    with pytest.raises(AssertionError, match="unrelated failure"):
        installation.daemon_ps({"ipc": ipc, "time": Clock()}, Path("/tmp/hr-upgrade-restore/state"))


def test_non_object_ps_is_failure_not_empty_success() -> None:
    def ipc(state: Path, method: str, params: dict[str, Any]) -> object:
        return {} if method == "ping" else []

    with pytest.raises(AssertionError, match="no object result"):
        installation.daemon_ps({"ipc": ipc, "time": Clock()}, Path("/tmp/hr-upgrade-restore/state"))


def test_entry_wires_ps_to_the_restore_aware_helper(monkeypatch: pytest.MonkeyPatch) -> None:
    # Reuse the same path-loaded entry seam as the diagnostics tests.
    import runpy
    root = Path(__file__).resolve().parents[5]
    entry = runpy.run_path(str(root / "contract/e2e/run-upgrade-continuity.py"))
    seen = []
    def replacement(dependencies, state):
        seen.append(state)
        return {"daemon": {"running": True}}
    monkeypatch.setattr(installation, "daemon_ps", replacement)
    entry["daemon_ps"].__globals__["_installation_impl"] = installation
    assert entry["daemon_ps"]({"HARNESS_STATE_DIR": "/tmp/hr-upgrade-restore/state"}) == {"daemon": {"running": True}}
    assert seen == [Path("/tmp/hr-upgrade-restore/state")]
