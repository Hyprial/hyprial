"""``hyprial doctor`` delivery-paths check: reads the explicit probe file only."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import hyprial.shell.impl.cli.runtime.entry  # noqa: F401  (installs the explicit CLI composition)
from tests.shell.cli.lifecycle.doctor.doctor_support import (
    checks as _checks,
    doctor as _doctor,
    running_ps as _running_ps,
)


def test_delivery_paths_doctor_warns_without_a_probe(
    monkeypatch: Any, tmp_path: Path,
) -> None:
    """A fresh install has no probe yet: warn with the way forward, never fail
    (owner 2026-11-06), so the check alone cannot make doctor red."""
    monkeypatch.setenv("HYPRIAL_HOME", str(tmp_path))
    result = _doctor(monkeypatch, _running_ps(None), delivery_paths_ok=False)
    check = _checks(result)["delivery-paths"]
    assert check["status"] == "warn"
    assert "NOT-EVALUABLE" in check["detail"]
    assert "route:" in check["detail"]
    assert "hyprial adapter route add" in check["detail"]
    assert "hyprial adapter route add" in check["action"]["command"]


def test_delivery_paths_doctor_reads_the_epoch_and_family_verdicts(
    tmp_path: Path,
) -> None:
    from hyprial.shell.impl.cli.commands.doctor.checks.delivery_paths import (
        delivery_paths_doctor_check,
    )

    state = tmp_path / "state"
    state.mkdir()
    def write(payload: dict[str, object]) -> None:
        (state / "delivery-paths-probe.json").write_text(
            json.dumps(payload), encoding="utf-8"
        )

    write({"epoch": time.time(), "per_family": {}})
    assert delivery_paths_doctor_check(tmp_path)["status"] == "fail"

    write({
        "epoch": time.time(),
        "per_family": {
            family: {"accepted": True, "target": f"{family}:x"}
            for family in ("agent", "user", "route")
        },
    })
    assert delivery_paths_doctor_check(tmp_path)["status"] == "ok"

    write({
        "epoch": time.time(),
        "per_family": {
            "agent": {"accepted": True},
            "user": {"accepted": True},
            "route": {"accepted": False},
        },
    })
    failed = delivery_paths_doctor_check(tmp_path)
    assert failed["status"] == "fail"
    assert "route" in failed["detail"]
    assert "hyprial adapter route add" in failed["action"]["command"]

    # no --route given: the probe records an explicit null target -> optional, warn
    write({
        "epoch": time.time(),
        "per_family": {
            "agent": {"accepted": True, "target": "agent:x"},
            "user": {"accepted": True, "target": "user:x"},
            "route": {"accepted": False, "target": None, "error": "route target not supplied"},
        },
    })
    unconfigured = delivery_paths_doctor_check(tmp_path)
    assert unconfigured["status"] == "warn"
    assert unconfigured["metrics"]["routeConfigured"] is False
    assert "hyprial adapter route add" in unconfigured["detail"]

    # route not configured AND user failed -> still fail, naming only user
    write({
        "epoch": time.time(),
        "per_family": {
            "agent": {"accepted": True, "target": "agent:x"},
            "user": {"accepted": False, "target": "user:x"},
            "route": {"accepted": False, "target": None},
        },
    })
    user_failed = delivery_paths_doctor_check(tmp_path)
    assert user_failed["status"] == "fail"
    assert "user" in user_failed["detail"] and "route" not in user_failed["detail"].split(";")[0]

    write({"epoch": time.time() - 90_000, "per_family": {}})
    stale = delivery_paths_doctor_check(tmp_path)
    assert stale["status"] == "warn"
    assert "NOT-EVALUABLE" in stale["detail"]
    assert "stale_probe" in stale["detail"]
