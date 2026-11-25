"""Slice 6 source: delete migrated routines after success; commit warnings."""

from __future__ import annotations

from pathlib import Path

import pytest

from hyprial.daemon.impl.teleport import TeleportError
from hyprial.daemon.impl.teleport.protocol import RoutineOutcome

from ..fakes import CLEANUP_KEYS, TARGET_NODE, TARGET_URI, expect_failure
from .support import (
    ALL,
    ENABLED,
    HOURLY,
    NIGHTLY,
    WEEKLY,
    routine_rig,
)


def test_success_deletes_every_migrated_routine_before_the_agent(tmp_path: Path) -> None:
    r = routine_rig(tmp_path)
    result = r.run()
    assert r.routine_calls("routine_remove") == ALL
    assert r.routines.enabled == {}
    assert r.index("receive") < r.index("routine_remove", NIGHTLY)
    assert r.index("routine_remove", HOURLY) < r.index("remove")
    assert r.index("remove") < r.index("commit")
    assert r.routine_calls("routine_resume") == []
    assert result["to"] == {"node": TARGET_NODE, "uri": TARGET_URI}
    assert result["commit"] == {"ok": True, "pausedRoutines": []}


def test_agent_removal_failure_skips_commit_and_lists_paused_routines(
    tmp_path: Path,
) -> None:
    r = routine_rig(tmp_path)
    r.host.failures["remove"] = RuntimeError("home busy")
    error = expect_failure(r.run)
    assert error.code == "TELEPORT_SOURCE_CLEANUP_FAILED"
    assert set(error.data) == CLEANUP_KEYS
    assert error.data["pausedRoutines"] == ENABLED
    assert not {"commit", "abort", "restart"} & set(r.names())
    assert r.routine_calls("routine_resume") == []


def test_routine_removal_failure_stops_cleanup_without_commit(tmp_path: Path) -> None:
    r = routine_rig(tmp_path)
    r.routines.failures[f"remove:{WEEKLY}"] = RuntimeError("store locked")
    error = expect_failure(r.run)
    assert error.code == "TELEPORT_SOURCE_CLEANUP_FAILED"
    assert error.data["pausedRoutines"] == ENABLED
    assert WEEKLY in error.message
    assert r.routine_calls("routine_remove") == [NIGHTLY, WEEKLY]
    assert not {"remove", "commit", "abort", "restart"} & set(r.names())
    assert r.routine_calls("routine_resume") == []


def test_a_disabled_routine_left_paused_is_not_a_commit_warning(tmp_path: Path) -> None:
    r = routine_rig(tmp_path)
    r.link.commit_outcomes = (
        RoutineOutcome(name=NIGHTLY, enabled=True, resumed=True),
        RoutineOutcome(name=WEEKLY, enabled=False, resumed=False),
        RoutineOutcome(name=HOURLY, enabled=True, resumed=True),
    )
    assert r.run()["commit"] == {"ok": True, "pausedRoutines": []}


def test_a_failed_resume_on_the_target_is_a_commit_warning(tmp_path: Path) -> None:
    r = routine_rig(tmp_path)
    r.link.commit_outcomes = (
        RoutineOutcome(name=NIGHTLY, enabled=True, resumed=True),
        RoutineOutcome(name=WEEKLY, enabled=False, resumed=False),
        RoutineOutcome(name=HOURLY, enabled=True, resumed=False),
    )
    result = r.run()
    assert result["ok"] is True
    assert result["commit"] == {"ok": False, "pausedRoutines": [HOURLY]}


@pytest.mark.parametrize(
    "failure",
    [
        TeleportError("TELEPORT_UNKNOWN_OPERATION", "target restarted"),
        TeleportError("TELEPORT_UNRESOLVED", "no reply to commit"),
    ],
)
def test_commit_without_reply_lists_the_routines_left_paused(
    tmp_path: Path, failure
) -> None:
    r = routine_rig(tmp_path)
    r.link.failures["commit"] = failure
    result = r.run()
    assert result["ok"] is True
    assert result["commit"] == {"ok": False, "pausedRoutines": ENABLED}
    assert r.routine_calls("routine_remove") == ALL
