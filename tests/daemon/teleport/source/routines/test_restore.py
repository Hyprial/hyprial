"""Slice 6 source: later failures restore routines; UNRESOLVED keeps them paused."""

from __future__ import annotations

from pathlib import Path

import pytest

from hyprial.daemon.impl.teleport import TeleportError

from ..fakes import expect_failure
from .support import (
    ENABLED,
    HOURLY,
    NIGHTLY,
    WEEKLY,
    original_state,
    routine_rig,
)

LATER_FAILURES = [
    pytest.param("quiesced", None, "TELEPORT_SOURCE_NOT_QUIESCED", id="no-quiescence-proof"),
    pytest.param(
        "host:quiesce", RuntimeError("stop timed out"), "TELEPORT_SOURCE_NOT_QUIESCED",
        id="quiesce-raises",
    ),
    pytest.param(
        "host:export", RuntimeError("tree changed"), "TELEPORT_SOURCE_NOT_QUIESCED",
        id="export",
    ),
    pytest.param(
        "link:send", TeleportError("TELEPORT_INCOMPLETE", "chunk lost"), "TELEPORT_INCOMPLETE",
        id="chunk",
    ),
    pytest.param(
        "link:receive",
        TeleportError("TELEPORT_STRICT_RESUME_FAILED", "session differs"),
        "TELEPORT_STRICT_RESUME_FAILED",
        id="receive-refused",
    ),
    pytest.param(
        "link:receive",
        TeleportError("TELEPORT_ROUTINE_CONFLICT", "taken after prepare"),
        "TELEPORT_ROUTINE_CONFLICT",
        id="receive-routine-conflict",
    ),
]


def _inject(r, where: str, failure: BaseException | None) -> None:
    if where == "quiesced":
        r.host.quiesced = False
        return
    side, step = where.split(":")
    target = r.host if side == "host" else r.link
    target.failures[step] = failure


@pytest.mark.parametrize(("where", "failure", "code"), LATER_FAILURES)
def test_later_failure_restores_the_original_routine_state(
    tmp_path: Path, where: str, failure, code: str
) -> None:
    r = routine_rig(tmp_path)
    _inject(r, where, failure)
    error = expect_failure(r.run)
    assert error.code == code
    assert r.routines.enabled == original_state()
    assert r.routine_calls("routine_resume") == ENABLED
    assert WEEKLY not in r.routine_calls("routine_resume")
    assert r.routine_calls("routine_remove") == []
    assert r.index("restart") < r.index("routine_resume", NIGHTLY)
    assert "commit" not in r.names()
    assert r.leftovers() == []


def test_a_failing_resume_keeps_the_code_and_names_the_routine(tmp_path: Path) -> None:
    r = routine_rig(tmp_path)
    r.link.failures["send"] = TeleportError("TELEPORT_INCOMPLETE", "chunk lost")
    r.routines.failures[f"resume:{NIGHTLY}"] = RuntimeError("store locked")
    error = expect_failure(r.run)
    assert error.code == "TELEPORT_INCOMPLETE"
    assert NIGHTLY in error.message
    assert r.routine_calls("routine_resume") == ENABLED
    assert r.routines.enabled[HOURLY] is True


@pytest.mark.parametrize(
    "failure",
    [
        TeleportError("TELEPORT_UNRESOLVED", "no reply to receive"),
        RuntimeError("transport broke mid-receive"),
    ],
    ids=["unresolved", "unexpected"],
)
def test_unresolved_keeps_routines_paused_and_lists_them(tmp_path: Path, failure) -> None:
    r = routine_rig(tmp_path)
    r.link.failures["receive"] = failure
    error = expect_failure(r.run)
    assert error.code == "TELEPORT_UNRESOLVED"
    assert error.data["pausedRoutines"] == ENABLED
    assert r.routine_calls("routine_resume") == []
    assert r.routine_calls("routine_remove") == []
    assert r.routines.enabled == {NIGHTLY: False, WEEKLY: False, HOURLY: False}
    assert not {"restart", "abort", "commit"} & set(r.names())
