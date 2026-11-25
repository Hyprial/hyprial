"""§4 failure handling: what is restored, whether abort is sent, which code."""

from __future__ import annotations

from pathlib import Path

import pytest

from hyprial.daemon.impl.teleport import TeleportError
from hyprial.daemon.impl.teleport.protocol import AbortRequest

from .fakes import (
    AGENT,
    CLEANUP_KEYS,
    OPERATION_ID,
    SOURCE_URI,
    STREAM_TOTAL,
    TARGET_NODE,
    TARGET_URI,
    UNRESOLVED_KEYS,
    expect_failure,
    rig,
)

RESTORED_AFTER_QUIESCE = [
    ("quiesce-false", "host", None, "TELEPORT_SOURCE_NOT_QUIESCED", "quiesce"),
    ("quiesce-raises", "host", "quiesce", "TELEPORT_SOURCE_NOT_QUIESCED", "quiesce"),
    ("export-raises", "host", "export", "TELEPORT_SOURCE_NOT_QUIESCED", "export"),
    ("send-incomplete", "link", "send", "TELEPORT_INCOMPLETE", "send"),
    ("send-digest", "link", "send", "TELEPORT_CHUNK_DIGEST_MISMATCH", "send"),
    ("send-unknown-op", "link", "send", "TELEPORT_UNKNOWN_OPERATION", "send"),
]


def _inject(r, side: str, step: str | None, code: str) -> None:
    if step is None:
        r.host.quiesced = False
        return
    error: BaseException
    if side == "link":
        error = TeleportError(code, f"{step} failed")
    else:
        error = RuntimeError(f"{step} failed")
    getattr(r, side).failures[step] = error


def _aborts(r) -> list[tuple]:
    return [event for event in r.events if event[0] == "abort"]


@pytest.mark.parametrize(
    "code",
    [
        "TELEPORT_WRONG_TARGET",
        "TELEPORT_OWNER_MISMATCH",
        "TELEPORT_RUNTIME_UNSUPPORTED",
        "TELEPORT_AGENT_EXISTS",
        "TELEPORT_ROUTINE_CONFLICT",
        "TELEPORT_OPERATION_CONFLICT",
    ],
)
@pytest.mark.parametrize("dry_run", [False, True])
def test_prepare_failure_reports_target_code_and_leaves_source(
    tmp_path: Path, code: str, dry_run: bool
) -> None:
    r = rig(tmp_path)
    r.link.failures["prepare"] = TeleportError(code, "refused")
    error = expect_failure(lambda: r.run(dry_run=dry_run))
    assert error.code == code
    assert r.names() == ["describe", "require_resumable", "prepare"]
    assert r.leftovers() == []


@pytest.mark.parametrize(
    ("side", "step", "code", "last"),
    [row[1:] for row in RESTORED_AFTER_QUIESCE],
    ids=[row[0] for row in RESTORED_AFTER_QUIESCE],
)
def test_failure_after_quiesce_restarts_source_and_aborts(
    tmp_path: Path, side: str, step: str | None, code: str, last: str
) -> None:
    r = rig(tmp_path)
    _inject(r, side, step, code)
    error = expect_failure(r.run)
    assert error.code == code
    names = r.names()
    assert names.index(last) < names.index("restart")
    assert "restart" in names and names.count("restart") == 1
    assert _aborts(r) and all(
        event[1] == TARGET_NODE and isinstance(event[2], AbortRequest)
        and event[2].operation_id == OPERATION_ID
        for event in _aborts(r)
    )
    assert not {"receive", "remove", "commit"} & set(names)
    assert r.leftovers() == []


@pytest.mark.parametrize(
    "code",
    [
        "TELEPORT_STRICT_RESUME_FAILED",
        "TELEPORT_LANDING_FAILED",
        "TELEPORT_DIGEST_MISMATCH",
        "TELEPORT_INCOMPLETE",
        "TELEPORT_AGENT_EXISTS",
        "TELEPORT_UNKNOWN_OPERATION",
    ],
)
def test_receive_refusal_restarts_source_without_abort(tmp_path: Path, code: str) -> None:
    r = rig(tmp_path)
    r.link.failures["receive"] = TeleportError(code, "target refused")
    error = expect_failure(r.run)
    assert error.code == code
    assert r.names()[-2:] == ["receive", "restart"]
    assert _aborts(r) == []
    assert not {"remove", "commit"} & set(r.names())
    assert r.leftovers() == []


@pytest.mark.parametrize(
    "failure",
    [
        TeleportError("TELEPORT_UNRESOLVED", "no reply to receive"),
        RuntimeError("transport broke mid-receive"),
    ],
    ids=["unresolved", "unexpected"],
)
def test_unresolved_receive_keeps_source_stopped(tmp_path: Path, failure) -> None:
    r = rig(tmp_path)
    r.link.failures["receive"] = failure
    error = expect_failure(r.run)
    assert error.code == "TELEPORT_UNRESOLVED"
    assert r.names()[-1] == "receive"
    assert not {"restart", "abort", "remove", "commit"} & set(r.names())
    assert [c for c in r.routines.calls if c[0] in {"resume", "remove"}] == []
    assert r.leftovers() == []


def test_unresolved_result_carries_exactly_the_manual_check_data(tmp_path: Path) -> None:
    r = rig(tmp_path)
    r.link.failures["receive"] = TeleportError("TELEPORT_UNRESOLVED", "silent")
    data = expect_failure(r.run).data
    assert set(data) == UNRESOLVED_KEYS
    assert {key: data[key] for key in UNRESOLVED_KEYS - {"manualCheck"}} == {
        "operationId": OPERATION_ID,
        "targetNode": TARGET_NODE,
        "agent": AGENT,
        "sourceAgentUri": SOURCE_URI,
        "bytesSent": STREAM_TOTAL,
        "sourceState": "stopped",
        "pausedRoutines": [],
    }
    assert isinstance(data["manualCheck"], str) and data["manualCheck"].strip()


def test_source_cleanup_failure_skips_commit(tmp_path: Path) -> None:
    r = rig(tmp_path)
    r.host.failures["remove"] = RuntimeError("home busy")
    error = expect_failure(r.run)
    assert error.code == "TELEPORT_SOURCE_CLEANUP_FAILED"
    assert r.names()[-1] == "remove"
    assert not {"commit", "abort", "restart"} & set(r.names())
    assert set(error.data) == CLEANUP_KEYS
    assert error.data == {
        "operationId": OPERATION_ID,
        "agent": AGENT,
        "targetNode": TARGET_NODE,
        "targetAgentUri": TARGET_URI,
        "pausedRoutines": [],
    }
    assert TARGET_URI in error.message
    assert r.leftovers() == []


@pytest.mark.parametrize(
    "failure",
    [
        TeleportError("TELEPORT_UNKNOWN_OPERATION", "target restarted"),
        TeleportError("TELEPORT_UNRESOLVED", "no reply to commit"),
    ],
)
def test_commit_failure_is_still_success_with_a_warning(tmp_path: Path, failure) -> None:
    r = rig(tmp_path)
    r.link.failures["commit"] = failure
    result = r.run()
    assert result["ok"] is True
    assert result["to"] == {"node": TARGET_NODE, "uri": TARGET_URI}
    assert result["commit"] == {"ok": False, "pausedRoutines": []}
    assert not {"restart", "abort"} & set(r.names())
    assert r.leftovers() == []


def test_failed_restart_does_not_mask_the_original_code(tmp_path: Path) -> None:
    r = rig(tmp_path)
    r.host.quiesced = False
    r.host.failures["restart"] = RuntimeError("cannot start")
    error = expect_failure(r.run)
    assert error.code == "TELEPORT_SOURCE_NOT_QUIESCED"
    assert _aborts(r)


def test_failed_abort_does_not_mask_the_original_code(tmp_path: Path) -> None:
    r = rig(tmp_path)
    r.link.abort_ok = False
    r.link.failures["send"] = TeleportError("TELEPORT_INCOMPLETE", "silence")
    error = expect_failure(r.run)
    assert error.code == "TELEPORT_INCOMPLETE"
    assert "restart" in r.names()
