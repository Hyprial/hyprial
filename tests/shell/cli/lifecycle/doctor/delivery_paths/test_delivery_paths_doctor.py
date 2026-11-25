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
    assert "route" not in check["detail"]
    assert check["action"]["command"] == "hyprial message probe-delivery-paths"


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
            for family in ("agent", "user")
        },
    })
    assert delivery_paths_doctor_check(tmp_path)["status"] == "ok"

    write({
        "epoch": time.time(),
        "per_family": {
            "agent": {"accepted": True},
            "user": {"accepted": False},
        },
    })
    failed = delivery_paths_doctor_check(tmp_path)
    assert failed["status"] == "fail"
    assert "user" in failed["detail"]
    assert "route" not in failed["detail"]
    assert "route" not in failed["action"]["command"]

    write({
        "epoch": time.time(),
        "per_family": {
            "agent": {"accepted": True},
            "user": {"accepted": True},
            "route": {"accepted": False, "target": None, "error": "left by an older probe"},
        },
    })
    older = delivery_paths_doctor_check(tmp_path)
    assert older["status"] == "ok"
    assert "routeConfigured" not in older.get("metrics", {})

    write({
        "epoch": time.time(),
        "per_family": {"agent": {"accepted": True}},
    })
    missing_user = delivery_paths_doctor_check(tmp_path)
    assert missing_user["status"] == "fail"
    assert "user" in missing_user["detail"]

    write({"epoch": time.time() - 90_000, "per_family": {}})
    stale = delivery_paths_doctor_check(tmp_path)
    assert stale["status"] == "warn"
    assert "NOT-EVALUABLE" in stale["detail"]
    assert "stale_probe" in stale["detail"]
