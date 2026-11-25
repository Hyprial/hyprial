# Decision record (file header; no inline comments by repository rule):
# - "以 paused 创建" reuses the existing internal entry
#   RoutineFacade.add(*, yaml_text, owner, enabled=False) (and the matching
#   RoutineServicePort.add(enabled=...)); no duplicate add_paused method is
#   introduced. Auto-resume lives only in the daemon's routine.add IPC
#   handler, which teleport must not go through.
# - These tests pin the guarantee teleport depends on: a paused add is
#   disabled, never dispatches (no PAC start, no source query) however many
#   due timer ticks pass, and never resumes (resume is the only facade path
#   that calls PacPort.rearm_boundary).
# - The enabled add is pinned alongside as the unchanged-behaviour baseline.
"""Agent teleport slice 1: create a routine paused, without dispatch or resume."""

from __future__ import annotations

from pathlib import Path

from .support import (
    ALPHA,
    OWNER,
    Clock,
    FakePac,
    SourceRecorder,
    make_service,
    produced_source_yaml,
    scheduled_yaml,
    wait_for,
)


def _tick(service, clock: Clock, times: int) -> None:
    for _ in range(times):
        clock.now_ms += 3_600_000
        service.submit_timer()


def test_paused_add_is_disabled_and_never_dispatches_or_resumes(tmp_path: Path) -> None:
    clock = Clock()
    pac = FakePac()
    service = make_service(tmp_path, pac=pac, clock=clock)
    try:
        added = service.add(
            yaml_text=scheduled_yaml("moved", actor=ALPHA), owner=OWNER, enabled=False
        )
        assert added["enabled"] is False
        assert service.status(name="moved")["enabled"] is False

        _tick(service, clock, 3)
        service.add(yaml_text=scheduled_yaml("probe", actor=ALPHA), owner=OWNER)
        _tick(service, clock, 1)
        wait_for(lambda: "probe" in pac.starts)

        status = service.status(name="moved")
        assert status["enabled"] is False
        assert status["inFlight"] == []
        assert "moved" not in pac.starts
        assert pac.rearm_calls == []
    finally:
        service.close()


def test_paused_source_routine_never_queries_its_source(tmp_path: Path) -> None:
    clock = Clock()
    source = SourceRecorder()
    service = make_service(tmp_path, clock=clock, source=source)
    try:
        service.add(
            yaml_text=produced_source_yaml("moved-source", produces=ALPHA),
            owner=OWNER,
            enabled=False,
        )
        _tick(service, clock, 3)
        service.add(yaml_text=scheduled_yaml("probe", actor=ALPHA), owner=OWNER)
        _tick(service, clock, 1)
        wait_for(lambda: service.status(name="probe")["inFlight"] != [])

        assert source.filters == []
        assert service.status(name="moved-source")["enabled"] is False
    finally:
        service.close()


def test_enabled_add_still_dispatches_when_due(tmp_path: Path) -> None:
    clock = Clock()
    pac = FakePac()
    service = make_service(tmp_path, pac=pac, clock=clock)
    try:
        added = service.add(yaml_text=scheduled_yaml("live", actor=ALPHA), owner=OWNER)
        assert added["enabled"] is True
        _tick(service, clock, 1)
        wait_for(lambda: "live" in pac.starts)
    finally:
        service.close()
