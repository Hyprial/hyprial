"""Doctor checks for route-session failures and recovery evidence."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import hyprial.shell.impl.cli.commands.doctor.commands as doctor_module


def _write_route_session_log(home: Path, entries: list[dict[str, Any]]) -> None:
    path = home / "state" / "logs" / "daemon.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(entry) for entry in entries) + "\n", encoding="utf-8")


def _route_session_ts(minutes_ago: int) -> str:
    return (
        datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    ).isoformat().replace("+00:00", "Z")


def test_route_session_doctor_check_is_ok_without_route_io_failures(tmp_path: Path) -> None:
    _write_route_session_log(
        tmp_path,
        [{"ts": _route_session_ts(5), "event": "daemon.lifecycle.operation.failed", "errorCode": "SOMETHING_ELSE"}],
    )

    check = doctor_module._route_session_doctor_check(tmp_path)

    assert check is not None
    assert check["status"] == "ok"
    assert check["metrics"]["routeIoFailed"] == 0


def test_route_session_doctor_check_fails_without_a_create_success_after(tmp_path: Path) -> None:
    _write_route_session_log(
        tmp_path,
        [
            {"ts": _route_session_ts(10), "event": "daemon.lifecycle.operation.failed", "errorCode": "ROUTE_IO_FAILED"},
            {"ts": _route_session_ts(5), "event": "daemon.lifecycle.operation.settled", "kind": "drop"},
        ],
    )

    check = doctor_module._route_session_doctor_check(tmp_path)

    assert check is not None
    assert check["status"] == "fail"
    assert check["metrics"]["routeIoFailed"] == 1
    assert check["metrics"]["createSucceededAfterLastFailure"] is False


def test_route_session_doctor_check_warns_after_a_create_success(tmp_path: Path) -> None:
    _write_route_session_log(
        tmp_path,
        [
            {"ts": _route_session_ts(10), "event": "daemon.lifecycle.operation.failed", "errorCode": "ROUTE_IO_FAILED"},
            {"ts": _route_session_ts(5), "event": "daemon.lifecycle.operation.settled", "kind": "create"},
        ],
    )

    check = doctor_module._route_session_doctor_check(tmp_path)

    assert check is not None
    assert check["status"] == "warn"
    assert check["metrics"]["routeIoFailed"] == 1
    assert check["metrics"]["createSucceededAfterLastFailure"] is True


def test_route_session_doctor_check_is_unjudged_without_a_log(tmp_path: Path) -> None:
    assert doctor_module._route_session_doctor_check(tmp_path) is None


def test_route_session_doctor_check_does_not_treat_an_error_settled_as_healed(tmp_path: Path) -> None:
    _write_route_session_log(
        tmp_path,
        [
            {"ts": _route_session_ts(10), "event": "daemon.lifecycle.operation.failed", "errorCode": "ROUTE_IO_FAILED"},
            {
                "ts": _route_session_ts(5),
                "event": "daemon.lifecycle.operation.settled",
                "kind": "create",
                "level": "error",
                "errorCode": "ROUTE_IO_FAILED",
            },
        ],
    )

    check = doctor_module._route_session_doctor_check(tmp_path)

    assert check is not None
    assert check["status"] == "fail"
    assert check["metrics"]["createSucceededAfterLastFailure"] is False


def test_route_session_doctor_check_fails_on_inbox_fetch_receipt_session_closed(tmp_path: Path) -> None:
    _write_route_session_log(
        tmp_path,
        [
            {
                "ts": _route_session_ts(10),
                "event": "inbox.fetch_receipt.publish_failed",
                "detail": "ZError: session closed",
            },
        ],
    )

    check = doctor_module._route_session_doctor_check(tmp_path)

    assert check is not None
    assert check["status"] == "fail"
    assert check["metrics"]["inboxFetchReceiptFailed"] == 1


def test_route_session_last_failure_is_the_newest_of_both_sources(tmp_path: Path) -> None:
    """A later ROUTE_IO_FAILED must win over an earlier inbox failure.

    The inbox list only exists to widen the evidence; concatenating the lists
    and taking the last element reports the inbox timestamp whenever the
    inbox list is non-empty, even if the route failure is newer.
    """

    route_ts = _route_session_ts(5)
    inbox_ts = _route_session_ts(10)
    _write_route_session_log(
        tmp_path,
        [
            {
                "ts": inbox_ts,
                "event": "inbox.fetch_receipt.publish_failed",
                "detail": "ZError: session closed",
            },
            {
                "ts": route_ts,
                "event": "daemon.lifecycle.operation.failed",
                "errorCode": "ROUTE_IO_FAILED",
            },
        ],
    )

    check = doctor_module._route_session_doctor_check(tmp_path)

    assert check is not None
    assert check["metrics"]["lastFailureTs"] == route_ts


def test_route_session_interleaved_heal_then_newer_route_failure_is_fail(
    tmp_path: Path,
) -> None:
    """A create heal is only a heal for failures before it.

    route fail -> inbox fail -> successful kind=create -> later route fail:
    the graph is blocked again (FAIL), and lastFailureTs is that last route
    failure.  This pins the downstream verdict independently of whichever
    timestamp field a future change decides to trust.
    """

    first_route_ts = _route_session_ts(20)
    inbox_ts = _route_session_ts(15)
    create_ts = _route_session_ts(10)
    last_route_ts = _route_session_ts(5)
    _write_route_session_log(
        tmp_path,
        [
            {
                "ts": first_route_ts,
                "event": "daemon.lifecycle.operation.failed",
                "errorCode": "ROUTE_IO_FAILED",
            },
            {
                "ts": inbox_ts,
                "event": "inbox.fetch_receipt.publish_failed",
                "detail": "ZError: session closed",
            },
            {
                "ts": create_ts,
                "event": "daemon.lifecycle.operation.settled",
                "kind": "create",
                "level": "info",
            },
            {
                "ts": last_route_ts,
                "event": "daemon.lifecycle.operation.failed",
                "errorCode": "ROUTE_IO_FAILED",
            },
        ],
    )

    check = doctor_module._route_session_doctor_check(tmp_path)

    assert check is not None
    assert check["status"] == "fail"
    assert check["metrics"]["createSucceededAfterLastFailure"] is False
    assert check["metrics"]["lastFailureTs"] == last_route_ts


def test_route_session_doctor_check_warns_after_an_inbox_heal(tmp_path: Path) -> None:
    _write_route_session_log(
        tmp_path,
        [
            {
                "ts": _route_session_ts(10),
                "event": "inbox.fetch_receipt.publish_failed",
                "detail": "ZError: session closed",
            },
            {"ts": _route_session_ts(5), "event": "daemon.lifecycle.operation.settled", "kind": "create", "level": "info"},
        ],
    )

    check = doctor_module._route_session_doctor_check(tmp_path)

    assert check is not None
    assert check["status"] == "warn"
    assert check["metrics"]["inboxFetchReceiptFailed"] == 1
    assert check["metrics"]["createSucceededAfterLastFailure"] is True
