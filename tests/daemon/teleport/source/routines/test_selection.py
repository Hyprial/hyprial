"""Slice 6 source: which routines travel and what prepare and receive carry."""

from __future__ import annotations

import importlib
from pathlib import Path

from hyprial.daemon.impl.teleport import TeleportError
from hyprial.daemon.impl.teleport.protocol import RoutinePayload

from ..fakes import SOURCE_URI, expect_failure, source
from .support import (
    ALL,
    ENABLED,
    HOURLY,
    LISTED,
    NIGHTLY,
    ROUTINE_EVENTS,
    WEEKLY,
    original_state,
    routine_rig,
    yaml_for,
)


def test_select_routines_keeps_every_listed_routine() -> None:
    preflight = importlib.import_module("hyprial.daemon.impl.teleport.source.preflight")
    refs = [source().RoutineRef(name, flag) for name, flag in LISTED]
    assert preflight.select_routines(refs) == tuple(refs)


def test_routines_are_selected_by_the_agents_uri(tmp_path: Path) -> None:
    r = routine_rig(tmp_path)
    r.run()
    assert r.routine_calls("routine_executed_by") == [SOURCE_URI]


def test_prepare_carries_every_migrating_routine_name(tmp_path: Path) -> None:
    r = routine_rig(tmp_path)
    r.run()
    (prepared,) = r.requests("prepare")
    assert prepared.routines == tuple(ALL)


def test_dry_run_carries_names_and_touches_no_routine(tmp_path: Path) -> None:
    r = routine_rig(tmp_path)
    result = r.run(dry_run=True)
    (prepared,) = r.requests("prepare")
    assert prepared.routines == tuple(ALL)
    assert prepared.dry_run is True
    assert result["routines"] == [{"name": n, "enabled": f} for n, f in LISTED]
    assert [n for n in r.names() if n in ROUTINE_EVENTS] == ["routine_executed_by"]


def test_result_lists_routines_with_their_original_state(tmp_path: Path) -> None:
    r = routine_rig(tmp_path)
    result = r.run()
    assert result["routines"] == [
        {"name": NIGHTLY, "enabled": True},
        {"name": WEEKLY, "enabled": False},
        {"name": HOURLY, "enabled": True},
    ]


def test_only_originally_enabled_routines_are_paused(tmp_path: Path) -> None:
    r = routine_rig(tmp_path)
    r.run()
    assert r.routine_calls("routine_pause") == ENABLED
    assert r.routine_calls("routine_resume") == []


def test_receive_carries_yaml_and_the_original_enabled_flag(tmp_path: Path) -> None:
    r = routine_rig(tmp_path)
    r.run()
    (received,) = r.requests("receive")
    assert received.routines == (
        RoutinePayload(name=NIGHTLY, yaml=yaml_for(NIGHTLY), enabled=True),
        RoutinePayload(name=WEEKLY, yaml=yaml_for(WEEKLY), enabled=False),
        RoutinePayload(name=HOURLY, yaml=yaml_for(HOURLY), enabled=True),
    )
    assert r.routine_calls("routine_export") == ALL


def test_no_routine_means_no_pause_and_no_drain_wait(tmp_path: Path) -> None:
    r = routine_rig(tmp_path)
    r.routines.listed = ()
    r.routines.enabled = {}
    result = r.run()
    assert result["routines"] == []
    assert [n for n in r.names() if n in ROUTINE_EVENTS] == ["routine_executed_by"]
    (received,) = r.requests("receive")
    assert received.routines == ()


def test_target_routine_conflict_leaves_the_source_unchanged(tmp_path: Path) -> None:
    r = routine_rig(tmp_path)
    r.link.failures["prepare"] = TeleportError("TELEPORT_ROUTINE_CONFLICT", "weekly exists")
    error = expect_failure(r.run)
    assert error.code == "TELEPORT_ROUTINE_CONFLICT"
    assert r.names() == ["describe", "require_resumable", "routine_executed_by", "prepare"]
    assert r.routines.enabled == original_state()
    assert r.leftovers() == []
