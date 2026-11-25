"""Slice 6 source: pause and drain before the home is touched (§4 row 2)."""

from __future__ import annotations

from pathlib import Path

import pytest

from ..fakes import OPERATION_ID, expect_failure
from .support import (
    ALL,
    DEFAULT_DRAIN_TIMEOUT,
    ENABLED,
    HOURLY,
    NIGHTLY,
    WEEKLY,
    original_state,
    routine_rig,
    service_module,
)

HOME_TOUCHING = {"quiesce", "export", "restart", "send", "receive", "remove", "commit"}


def test_default_drain_timeout_is_120_seconds() -> None:
    assert service_module().ROUTINE_DRAIN_TIMEOUT_SECONDS == DEFAULT_DRAIN_TIMEOUT


def test_pause_and_drain_run_after_prepare_and_before_quiesce(tmp_path: Path) -> None:
    r = routine_rig(tmp_path)
    r.run()
    prepare = r.index("prepare")
    pauses = [r.index("routine_pause", name) for name in ENABLED]
    wait = r.index("routine_wait")
    exports = [r.index("routine_export", name) for name in ALL]
    quiesce = r.index("quiesce")
    assert prepare < min(pauses)
    assert pauses == sorted(pauses)
    assert max(pauses) < wait < min(exports)
    assert max(exports) < quiesce


def test_drain_waits_for_every_migrating_routine_with_the_default_timeout(
    tmp_path: Path,
) -> None:
    r = routine_rig(tmp_path)
    r.run()
    waits = [event for event in r.events if event[0] == "routine_wait"]
    assert waits == [("routine_wait", tuple(ALL), DEFAULT_DRAIN_TIMEOUT)]


def test_drain_timeout_is_configurable_per_service(tmp_path: Path) -> None:
    r = routine_rig(tmp_path, drain_timeout_seconds=7.5)
    r.run()
    waits = [event for event in r.events if event[0] == "routine_wait"]
    assert waits == [("routine_wait", tuple(ALL), 7.5)]


def _assert_restored_before_home(r, error, code: str) -> None:
    assert error.code == code
    assert error.data is None
    assert not HOME_TOUCHING & set(r.names())
    assert r.routines.enabled == original_state()
    assert WEEKLY not in r.routine_calls("routine_pause")
    assert WEEKLY not in r.routine_calls("routine_resume")
    assert r.routine_calls("routine_remove") == []
    aborts = r.requests("abort")
    assert [request.operation_id for request in aborts] == [OPERATION_ID]
    assert r.leftovers() == []


def test_in_flight_timeout_restores_routines_and_aborts(tmp_path: Path) -> None:
    r = routine_rig(tmp_path)
    r.routines.busy = [HOURLY]
    error = expect_failure(r.run)
    _assert_restored_before_home(r, error, "TELEPORT_IN_FLIGHT_TIMEOUT")
    assert r.routine_calls("routine_resume") == ENABLED
    assert HOURLY in error.message
    assert r.index("routine_wait") < r.index("routine_resume", NIGHTLY)


def test_drain_wait_that_raises_is_an_in_flight_timeout(tmp_path: Path) -> None:
    r = routine_rig(tmp_path)
    r.routines.failures["wait"] = RuntimeError("routine runtime closing")
    error = expect_failure(r.run)
    _assert_restored_before_home(r, error, "TELEPORT_IN_FLIGHT_TIMEOUT")
    assert r.routine_calls("routine_resume") == ENABLED


def test_pause_failure_resumes_what_was_paused(tmp_path: Path) -> None:
    r = routine_rig(tmp_path)
    r.routines.failures[f"pause:{HOURLY}"] = RuntimeError("store locked")
    error = expect_failure(r.run)
    _assert_restored_before_home(r, error, "TELEPORT_SOURCE_NOT_QUIESCED")
    assert r.routine_calls("routine_resume") == [NIGHTLY]
    assert "routine_wait" not in r.names()


@pytest.mark.parametrize("failing", [NIGHTLY, WEEKLY])
def test_unreadable_definition_fails_before_the_home(tmp_path: Path, failing: str) -> None:
    r = routine_rig(tmp_path)
    r.routines.failures[f"export:{failing}"] = RuntimeError("no such routine")
    error = expect_failure(r.run)
    _assert_restored_before_home(r, error, "TELEPORT_SOURCE_NOT_QUIESCED")
    assert r.routine_calls("routine_resume") == ENABLED
