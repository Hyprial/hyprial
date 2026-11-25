"""Slice 6 target: receive creates routines paused and removes them on failure."""

from __future__ import annotations

from pathlib import Path

import pytest

from hyprial.daemon.impl.teleport import TeleportError
from hyprial.daemon.impl.teleport.protocol import RoutineRestore

from ..support import (
    SOURCE_URI,
    TARGET_URI,
    error_code,
    receive_request,
    staging_entries,
)
from .support import (
    HOURLY,
    NIGHTLY,
    PAYLOADS,
    WEEKLY,
    payload,
    routine_service,
)


def test_receive_creates_routines_paused_between_activate_and_start(
    tmp_path: Path,
) -> None:
    service, host, routines, _receiver, packed = routine_service(tmp_path)
    service.receive(receive_request("op-1", packed, routines=PAYLOADS))
    assert host.calls == [
        "existing",
        "land",
        "activate",
        f"create_paused:{NIGHTLY}",
        f"create_paused:{WEEKLY}",
        "start",
    ]
    assert routines.created == {NIGHTLY: False, WEEKLY: False}


def test_create_paused_gets_the_carried_yaml_and_both_uris(tmp_path: Path) -> None:
    service, _host, routines, _receiver, packed = routine_service(tmp_path)
    service.receive(receive_request("op-1", packed, routines=PAYLOADS))
    assert routines.create_args == [
        (item.yaml, SOURCE_URI, TARGET_URI) for item in PAYLOADS
    ]


def test_reply_reports_restore_enabled_per_routine(tmp_path: Path) -> None:
    service, _host, _routines, _receiver, packed = routine_service(tmp_path)
    reply = service.receive(receive_request("op-1", packed, routines=PAYLOADS))
    assert reply.routines == (
        RoutineRestore(name=NIGHTLY, restore_enabled=True),
        RoutineRestore(name=WEEKLY, restore_enabled=False),
    )
    assert reply.agent_uri == TARGET_URI
    assert service.state("op-1") == "received"


def test_repeated_receive_creates_nothing_again(tmp_path: Path) -> None:
    service, host, _routines, _receiver, packed = routine_service(tmp_path)
    request = receive_request("op-1", packed, routines=PAYLOADS)
    first = service.receive(request)
    host.calls.clear()
    assert service.receive(request) == first
    assert host.calls == []


@pytest.mark.parametrize(
    "routines",
    [
        pytest.param((payload(NIGHTLY, True),), id="missing"),
        pytest.param((*PAYLOADS, payload(HOURLY, True)), id="extra"),
        pytest.param((payload(NIGHTLY, True), payload(NIGHTLY, False)), id="duplicate"),
        pytest.param((), id="empty"),
    ],
)
def test_routines_must_match_the_names_announced_by_prepare(
    tmp_path: Path, routines
) -> None:
    service, host, created, receiver, packed = routine_service(tmp_path)
    code = error_code(service.receive, receive_request("op-1", packed, routines=routines))
    assert code == "TELEPORT_BAD_FRAME"
    assert host.calls == []
    assert created.created == {}
    assert service.state("op-1") is None
    assert staging_entries(receiver) == []


def test_receive_order_may_differ_from_prepare(tmp_path: Path) -> None:
    service, _host, routines, _receiver, packed = routine_service(tmp_path)
    reversed_payloads = tuple(reversed(PAYLOADS))
    reply = service.receive(receive_request("op-1", packed, routines=reversed_payloads))
    assert [item.name for item in reply.routines] == [WEEKLY, NIGHTLY]
    assert set(routines.created) == {NIGHTLY, WEEKLY}


def test_name_taken_after_prepare_is_a_routine_conflict(tmp_path: Path) -> None:
    service, host, routines, receiver, packed = routine_service(tmp_path)
    routines.taken.add(WEEKLY)
    code = error_code(service.receive, receive_request("op-1", packed, routines=PAYLOADS))
    assert code == "TELEPORT_ROUTINE_CONFLICT"
    assert "land" not in host.calls
    assert not [call for call in host.calls if call.startswith("create_paused")]
    assert host.landed == set() and host.running == set()
    assert service.state("op-1") is None
    assert staging_entries(receiver) == []


def test_start_failure_removes_created_routines_in_reverse_order(tmp_path: Path) -> None:
    service, host, routines, receiver, packed = routine_service(tmp_path)
    host.failures["start"] = RuntimeError("resume broke")
    code = error_code(service.receive, receive_request("op-1", packed, routines=PAYLOADS))
    assert code == "TELEPORT_STRICT_RESUME_FAILED"
    after_start = host.calls[host.calls.index("start") + 1 :]
    assert after_start == [
        "stop",
        f"routine_remove:{WEEKLY}",
        f"routine_remove:{NIGHTLY}",
        "deactivate",
        "undo_landing",
    ]
    assert routines.created == {}
    assert host.running == set() and host.landed == set()
    assert service.state("op-1") is None
    assert staging_entries(receiver) == []


@pytest.mark.parametrize(
    ("failure", "code"),
    [
        (RuntimeError("store locked"), "TELEPORT_LANDING_FAILED"),
        (TeleportError("TELEPORT_ROUTINE_CONFLICT", "raced"), "TELEPORT_ROUTINE_CONFLICT"),
    ],
    ids=["unexpected", "teleport-error"],
)
def test_create_failure_removes_the_routines_already_created(
    tmp_path: Path, failure, code: str
) -> None:
    service, host, routines, receiver, packed = routine_service(tmp_path)
    routines.failures[f"create:{WEEKLY}"] = failure
    assert (
        error_code(service.receive, receive_request("op-1", packed, routines=PAYLOADS))
        == code
    )
    assert "start" not in host.calls
    after_create = host.calls[host.calls.index(f"create_paused:{WEEKLY}") + 1 :]
    assert after_create == [f"routine_remove:{NIGHTLY}", "deactivate", "undo_landing"]
    assert routines.created == {}
    assert service.state("op-1") is None
    assert staging_entries(receiver) == []
