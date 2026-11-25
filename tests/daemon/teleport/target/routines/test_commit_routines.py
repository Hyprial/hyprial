"""Slice 6 target: commit resumes only what was enabled; prepare without a port."""

from __future__ import annotations

from pathlib import Path

from hyprial.daemon.impl.teleport.protocol import CommitRequest, RoutineOutcome

from ..support import error_code, make_service, prepare_request, receive_request, staging_entries
from .support import (
    HOURLY,
    NIGHTLY,
    PAYLOADS,
    WEEKLY,
    payload,
    routine_service,
)

THREE = (payload(NIGHTLY, True), payload(WEEKLY, False), payload(HOURLY, True))


def _received(tmp_path: Path, payloads=PAYLOADS):
    names = tuple(item.name for item in payloads)
    service, host, routines, receiver, packed = routine_service(tmp_path, names=names)
    service.receive(receive_request("op-1", packed, routines=payloads))
    host.calls.clear()
    return service, host, routines, receiver


def test_without_commit_every_created_routine_stays_paused(tmp_path: Path) -> None:
    _service, host, routines, _receiver = _received(tmp_path)
    assert routines.created == {NIGHTLY: False, WEEKLY: False}
    assert host.calls == []


def test_commit_resumes_exactly_the_routines_to_restore(tmp_path: Path) -> None:
    service, host, routines, receiver = _received(tmp_path, THREE)
    reply = service.commit(CommitRequest("op-1"))
    assert host.calls == [f"resume:{NIGHTLY}", f"resume:{HOURLY}"]
    assert reply.routines == (
        RoutineOutcome(name=NIGHTLY, enabled=True, resumed=True),
        RoutineOutcome(name=WEEKLY, enabled=False, resumed=False),
        RoutineOutcome(name=HOURLY, enabled=True, resumed=True),
    )
    assert routines.created == {NIGHTLY: True, WEEKLY: False, HOURLY: True}
    assert service.state("op-1") == "committed"
    assert staging_entries(receiver) == []


def test_a_failing_resume_is_reported_and_the_rest_still_resume(tmp_path: Path) -> None:
    service, host, routines, _receiver = _received(tmp_path, THREE)
    routines.failures[f"resume:{NIGHTLY}"] = RuntimeError("store locked")
    reply = service.commit(CommitRequest("op-1"))
    assert host.calls == [f"resume:{NIGHTLY}", f"resume:{HOURLY}"]
    assert reply.routines == (
        RoutineOutcome(name=NIGHTLY, enabled=True, resumed=False),
        RoutineOutcome(name=WEEKLY, enabled=False, resumed=False),
        RoutineOutcome(name=HOURLY, enabled=True, resumed=True),
    )
    assert service.state("op-1") == "committed"


def test_repeated_commit_is_idempotent(tmp_path: Path) -> None:
    service, host, _routines, _receiver = _received(tmp_path, THREE)
    first = service.commit(CommitRequest("op-1"))
    host.calls.clear()
    assert service.commit(CommitRequest("op-1")) == first
    assert host.calls == []


def test_routines_without_a_routines_port_are_unsupported(tmp_path: Path) -> None:
    service, _host, receiver = make_service(tmp_path)
    for dry_run in (True, False):
        code = error_code(
            service.prepare, prepare_request("op-1", routines=(NIGHTLY,), dry_run=dry_run)
        )
        assert code == "TELEPORT_RUNTIME_UNSUPPORTED"
    assert service.state("op-1") is None
    assert staging_entries(receiver) == []
    service.prepare(prepare_request("op-2"))
    assert service.state("op-2") == "prepared"
