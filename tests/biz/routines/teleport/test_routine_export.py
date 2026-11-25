# Decision record (file header; no inline comments by repository rule):
# - Contract: RoutineFacade.export(*, name: str) -> dict with exactly the keys
#   name, yaml, enabled, owner. "yaml" is the stored text verbatim (the same
#   key routine.add takes), so teleport can recreate the routine byte for
#   byte; "enabled" is the current state so commit can restore it.
# - "入库时的 YAML 原文" is read as "the text the store holds now": after
#   routine set, export returns the set text, not the first add.
# - Missing routine raises RoutineServiceError("ROUTINE_NOT_FOUND"), the same
#   code routine.status uses.
"""Agent teleport slice 1: export a routine's stored YAML and enabled state."""

from __future__ import annotations

from pathlib import Path

import pytest

from hyprial.biz.impl.routine.execution.service import RoutineServiceError

from .support import ALPHA, OWNER, make_service, scheduled_yaml

ODD_YAML = (
    "# operator note: keep this comment through teleport\n"
    "version: 2\n"
    "mode:   scheduled\n"
    "name: exported-duty\n"
    "schedule: {interval: 60s}\n"
    f"actor: \"{ALPHA}\"\n"
    "task: |\n"
    "  Heartbeat.\n"
    "  Pick up work.\n"
    "on_task_timeout: {action: escalate, escalate_to: \"user:op\"}\n"
)


def test_export_returns_stored_yaml_verbatim_and_enabled_state(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    try:
        service.add(yaml_text=ODD_YAML, owner=OWNER)
        exported = service.export(name="exported-duty")
        assert exported == {
            "name": "exported-duty",
            "yaml": ODD_YAML,
            "enabled": True,
            "owner": OWNER,
        }
    finally:
        service.close()


def test_export_tracks_pause_and_resume(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    try:
        service.add(yaml_text=scheduled_yaml("duty", actor=ALPHA), owner=OWNER)
        service.pause(name="duty")
        assert service.export(name="duty")["enabled"] is False
        service.resume(name="duty")
        assert service.export(name="duty")["enabled"] is True
    finally:
        service.close()


def test_export_of_paused_add_reports_disabled(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    try:
        text = scheduled_yaml("duty", actor=ALPHA)
        service.add(yaml_text=text, owner=OWNER, enabled=False)
        assert service.export(name="duty") == {
            "name": "duty",
            "yaml": text,
            "enabled": False,
            "owner": OWNER,
        }
    finally:
        service.close()


def test_export_returns_text_after_routine_set(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    try:
        service.add(yaml_text=scheduled_yaml("duty", actor=ALPHA), owner=OWNER)
        updated = scheduled_yaml("duty", actor=ALPHA, interval="120s")
        service.set(name="duty", yaml_text=updated)
        assert service.export(name="duty")["yaml"] == updated
    finally:
        service.close()


def test_export_missing_routine_is_explicit_not_found(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    try:
        with pytest.raises(RoutineServiceError) as failure:
            service.export(name="absent")
        assert failure.value.code == "ROUTINE_NOT_FOUND"
        assert "absent" in str(failure.value)
    finally:
        service.close()


def test_export_is_read_only(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    try:
        service.add(yaml_text=scheduled_yaml("duty", actor=ALPHA), owner=OWNER)
        before = service.status(name="duty")
        service.export(name="duty")
        assert service.status(name="duty") == before
    finally:
        service.close()
