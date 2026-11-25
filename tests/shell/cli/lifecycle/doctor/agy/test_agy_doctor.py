"""Tests for the Antigravity (agy) doctor check."""

from __future__ import annotations

import shutil
from typing import Any

from hyprial.shell.impl.cli.commands.doctor.checks.agy import (
    agy_doctor_check,
    agy_doctor_checks,
)


def test_agy_doctor_check_omitted_without_agy_connectors() -> None:
    assert agy_doctor_check({"connectors": []}) is None
    assert agy_doctor_checks({"connectors": [{"runtime": "codex"}]}) == []


def test_agy_doctor_check_reports_ok_or_fail_from_path(monkeypatch: Any) -> None:
    ps = {"connectors": [{"name": "a1", "runtime": "agy"}]}

    monkeypatch.setattr(shutil, "which", lambda name: "/fake/bin/agy" if name == "agy" else None)
    ok_row = agy_doctor_check(ps)
    assert ok_row is not None
    assert ok_row["status"] == "ok"
    assert "/fake/bin/agy" in str(ok_row["detail"])

    monkeypatch.setattr(shutil, "which", lambda _name: None)
    fail_row = agy_doctor_check(ps)
    assert fail_row is not None
    assert fail_row["status"] == "fail"
    assert "not found" in str(fail_row["detail"])
