"""The upgrade-failure record: marker file, restart detail, start failures.

R2 (owner ruling 2026-11-19): nothing is sent to any IM. These cover the
marker and the detail it carries and, structurally, the one property that
cannot be checked from inside the module: that the marker is written **only**
on failure.
"""

from __future__ import annotations

import ast
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from hyprial.daemon.impl.autoupdate import alert
from hyprial.daemon.impl.autoupdate.alert import (
    MARKER_FILENAME,
    RestartProcessObservation,
    RestartProcessState,
    StartFailureRecord,
    latest_start_failure,
    restart_failure_detail,
    start_failure_line,
    write_failure_marker,
)

def _state(tmp_path: Path) -> Path:
    state = tmp_path / "state"
    state.mkdir(parents=True)
    return state


def test_the_marker_holds_the_failure_and_nothing_about_a_send(
    tmp_path: Path,
) -> None:
    """The marker is the record; R2 sends nothing, so it has no ``alert`` key."""

    state = _state(tmp_path)
    marker = write_failure_marker(
        state_dir=state, host="box-1", summary="s", detail="d"
    )
    assert marker.name == MARKER_FILENAME
    payload = json.loads(marker.read_text("utf-8"))
    assert payload["summary"] == "s"
    assert payload["host"] == "box-1"
    assert payload["detail"] == "d"
    assert "alert" not in payload


def test_the_alert_module_has_no_sending_surface() -> None:
    """R2 (owner ruling 2026-11-19): the IM send path is deleted, not dormant."""

    for name in (
        "AlertOutcome",
        "notify_upgrade_failure",
        "notify_restore_followup",
        "notify_owner",
        "notify_upgrade_outcome",
        "record_alert_outcome",
        "prime_upgrade_alert_imports",
        "alert_text",
    ):
        assert not hasattr(alert, name), name


_PRODUCTION_START_FAILED = {
    "ts": "2026-09-13T07:17:36.765Z",
    "level": "warn",
    "component": "daemon",
    "event": "daemon.start.failed",
    "phase": "actor-runtime",
    "errorType": "DomainCommandError",
    "error": "agent …ciia-dipc already has a live connector on pi …",
}


def test_latest_start_failure_is_bounded_to_this_restart(tmp_path: Path) -> None:
    log_path = tmp_path / "daemon.jsonl"
    old = {
        **_PRODUCTION_START_FAILED,
        "ts": "2026-09-13T07:16:00.000Z",
        "phase": "ipc-server",
        "errorType": "OldFailure",
        "error": "stale failure",
    }
    log_path.write_text(
        "\n".join(
            json.dumps(item, ensure_ascii=False)
            for item in (old, _PRODUCTION_START_FAILED)
        )
        + "\n",
        "utf-8",
    )

    failure = latest_start_failure(
        log_path, started_at=datetime(2026, 9, 13, 7, 17, 26, tzinfo=UTC)
    )

    assert failure is not None
    assert failure.phase == "actor-runtime"
    assert failure.error_type == "DomainCommandError"
    assert failure.error == "agent …ciia-dipc already has a live connector on pi …"


def test_an_old_start_failure_is_not_reported_as_this_restart(tmp_path: Path) -> None:
    log_path = tmp_path / "daemon.jsonl"
    log_path.write_text(
        json.dumps(_PRODUCTION_START_FAILED, ensure_ascii=False) + "\n", "utf-8"
    )

    failure = latest_start_failure(
        log_path, started_at=datetime(2026, 9, 13, 7, 18, tzinfo=UTC)
    )

    assert failure is None


@pytest.mark.parametrize(
    ("observation", "expected"),
    [
        (
            RestartProcessObservation(44710, RestartProcessState.RUNNING),
            "pid 44710 仍在(可能在失败后的 shutdown 中)",
        ),
        (
            RestartProcessObservation(44710, RestartProcessState.EXITED),
            "pid 44710 已退出",
        ),
        (
            RestartProcessObservation(44710, RestartProcessState.UNKNOWN),
            "pid 44710 观测失败",
        ),
    ],
)
def test_restart_failure_detail_reports_each_observed_process_state(
    observation: RestartProcessObservation, expected: str
) -> None:
    detail = restart_failure_detail(
        error="daemon did not become ready within 15s",
        start_failure=None,
        process=observation,
    )

    assert expected in detail
    assert "日志里还没有 start.failed 记录" in detail
    assert not ("left running" in detail and "没有在跑的 daemon" in detail)


def test_production_start_failure_fields_are_present_in_the_complete_detail(
    tmp_path: Path,
) -> None:
    log_path = tmp_path / "daemon.jsonl"
    log_path.write_text(
        json.dumps(_PRODUCTION_START_FAILED, ensure_ascii=False) + "\n", "utf-8"
    )
    failure = latest_start_failure(
        log_path, started_at=datetime(2026, 9, 13, 7, 17, 26, tzinfo=UTC)
    )
    assert failure is not None
    detail = restart_failure_detail(
        error=(
            "daemon did not become ready within 15s; the daemon process "
            "(pid 44710) was still running when the wait ended"
        ),
        start_failure=failure,
        process=RestartProcessObservation(44710, RestartProcessState.RUNNING),
    )

    assert detail == (
        "daemon restart failed: daemon did not become ready within 15s; "
        "the daemon process (pid 44710) was still running when the wait ended\n"
        "本次 daemon.start.failed: phase=actor-runtime "
        "errorType=DomainCommandError "
        "error=agent …ciia-dipc already has a live connector on pi …\n"
        "进程状态: pid 44710 仍在(可能在失败后的 shutdown 中)"
    )


def test_the_marker_carries_the_same_start_failure_line_the_detail_does(
    tmp_path: Path,
) -> None:
    """Marker and failure detail must not tell the same event in different amounts.

    Before this the alert named the cause and the marker did not -- and the
    marker is the half that survives on an unattended machine, so the one
    record a reader finds was the one without the cause. Both are rendered by
    `start_failure_line`, so the shared text is asserted, not a copy of it.
    """

    state = _state(tmp_path)
    failure = StartFailureRecord(
        timestamp=datetime(2026, 9, 13, 7, 17, 36, 765000, tzinfo=UTC),
        phase="actor-runtime",
        error_type="DomainCommandError",
        error="no agent named 'ciia-dipc' on this machine",
    )
    marker = write_failure_marker(
        state_dir=state,
        host="box-1",
        summary="s",
        detail=(
            "daemon restart failed: daemon did not become ready within 15s\n"
            f"{start_failure_line(failure)}"
        ),
        start_failure=failure,
    )

    payload = json.loads(marker.read_text("utf-8"))
    assert payload["detail"] == (
        "daemon restart failed: daemon did not become ready within 15s\n"
        "本次 daemon.start.failed: phase=actor-runtime "
        "errorType=DomainCommandError "
        "error=no agent named 'ciia-dipc' on this machine"
    )
    assert payload["startFailure"] == {
        "phase": "actor-runtime",
        "errorType": "DomainCommandError",
        "error": "no agent named 'ciia-dipc' on this machine",
    }


def test_a_marker_with_no_start_failure_states_it_instead_of_an_empty_object(
    tmp_path: Path,
) -> None:
    """`{}` would be present and unreadable; the sentence is the honest form.

    Both halves of the rule are asserted: the sentence is in `detail`, and the
    structured field is a written-out `null` -- not `{}`, and not omitted, so
    that "we looked and found nothing" cannot be read as "this field never
    existed".
    """

    state = _state(tmp_path)
    marker = write_failure_marker(
        state_dir=state,
        host="box-1",
        summary="s",
        detail=(
            "daemon restart failed: daemon did not become ready within 15s\n"
            f"{start_failure_line(None)}"
        ),
        start_failure=None,
    )

    payload = json.loads(marker.read_text("utf-8"))
    assert "本次启动: 日志里还没有 start.failed 记录" in payload["detail"]
    assert payload["startFailure"] is None
    assert payload["startFailure"] != {}


def test_a_marker_with_no_start_failure_dimension_omits_the_key(
    tmp_path: Path,
) -> None:
    """Omitted is not null: null asserts a search, omission does not.

    The survivor marker (old daemon torn down but the process still running) is
    not a start failure at all, so the question does not apply. Writing `null`
    there would read as "we looked for this launch's daemon.start.failed and
    found none" -- a claim about a search the survivor branch never runs -- and
    it would be the only place in the file where that null had no "no record"
    sentence beside it. The key is absent instead, and so is the sentence.

    This is also why the parameter's default is a sentinel rather than `None`:
    a caller who names nothing has not answered the question.
    """

    state = _state(tmp_path)
    marker = write_failure_marker(
        state_dir=state,
        host="box-1",
        summary="s",
        detail="daemon teardown completed but the old process did not exit",
    )

    payload = json.loads(marker.read_text("utf-8"))
    assert "startFailure" not in payload
    assert "本次启动: 日志里还没有 start.failed 记录" not in payload["detail"]


# ---------------------------------------------------------------------------
# ⭐ The control group. It cannot live inside the module, so it lives here.
# ---------------------------------------------------------------------------


def _native_upgrade_source() -> str:
    from hyprial.shell.impl.cli.commands.common import pending_restart as owner
    from hyprial.shell.impl.cli.commands.common.services import get_services
    if owner.get_services is not get_services:
        raise RuntimeError("Upgrade service getter is not the canonical native binding")
    source = Path(owner.__file__).read_text("utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id == "services"
                                               for target in node.targets):
            if not (isinstance(node.value, ast.Call) and isinstance(node.value.func, ast.Name)
                    and node.value.func.id == "get_services" and not node.value.args and not node.value.keywords):
                raise RuntimeError("Noncanonical lexical services assignment in the upgrade provider")
    return source


def _native_marker_call_name(call: ast.Call) -> str | None:
    target = call.func
    if isinstance(target, ast.Name):
        return target.id
    if (isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name)
            and target.value.id == "services"
            and target.attr == "write_failure_marker"):
        return target.attr
    return None


def test_the_marker_is_written_only_from_the_failure_handler() -> None:
    """Success must not leave a failure marker.

    What distinguishes "written on failure" from "written on everything" is
    *where it is called from*, and that is checked structurally: every call
    site must be lexically inside an `except` handler.
    """

    tree = ast.parse(_native_upgrade_source())
    handlers = [n for n in ast.walk(tree) if isinstance(n, ast.ExceptHandler)]
    guarded: set[int] = set()
    for handler in handlers:
        for node in ast.walk(handler):
            if isinstance(node, ast.Call):
                name = _native_marker_call_name(node)
                if name == "write_failure_marker":
                    guarded.add(node.lineno)

    all_calls: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = _native_marker_call_name(node)
            if name == "write_failure_marker":
                all_calls.add(node.lineno)

    assert all_calls, "the failure marker is not wired in at all"
    assert all_calls == guarded, (
        f"marker writes outside an except handler at lines "
        f"{sorted(all_calls - guarded)} -- a successful upgrade would record a failure"
    )


