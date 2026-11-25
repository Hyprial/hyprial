"""prepare on the target: the six ordered checks, dry run and the reply."""

from __future__ import annotations

from pathlib import Path

import pytest

from .support import (
    AGENT,
    NODE,
    PUBLIC_KEY,
    FakeHost,
    FakeRoutines,
    chunks,
    error_code,
    make_service,
    packed_bundle,
    prepare_request,
    staging_entries,
)

CHECKS = (
    "TELEPORT_WRONG_TARGET",
    "TELEPORT_OWNER_MISMATCH",
    "TELEPORT_RUNTIME_UNSUPPORTED",
    "TELEPORT_AGENT_EXISTS",
    "TELEPORT_ROUTINE_CONFLICT",
    "TELEPORT_OPERATION_CONFLICT",
)


def test_prepare_registers_operation_and_returns_public_key(tmp_path: Path) -> None:
    service, host, receiver = make_service(tmp_path)
    reply = service.prepare(prepare_request("op-1"))
    assert reply.operation_id == "op-1"
    assert reply.target_node == NODE
    assert reply.recipient_public_key == PUBLIC_KEY
    assert len(reply.recipient_public_key) == 32
    assert reply.dry_run is False
    assert service.state("op-1") == "prepared"
    assert service.node_id == NODE
    assert (Path(receiver.root) / "op-1").is_dir()
    assert host.calls == []


def test_dry_run_checks_but_leaves_no_staging_and_no_state(tmp_path: Path) -> None:
    service, _host, receiver = make_service(tmp_path)
    reply = service.prepare(prepare_request("op-dry", dry_run=True))
    assert reply.dry_run is True
    assert reply.recipient_public_key == PUBLIC_KEY
    assert service.state("op-dry") is None
    assert staging_entries(receiver) == []
    first = chunks("op-dry", packed_bundle(tmp_path))[0]
    assert error_code(service.chunk, first) == "TELEPORT_UNKNOWN_OPERATION"
    again = service.prepare(prepare_request("op-dry", dry_run=True))
    assert again.dry_run is True
    real = service.prepare(prepare_request("op-dry"))
    assert real.dry_run is False
    assert service.state("op-dry") == "prepared"


def test_dry_run_still_refuses_an_existing_agent(tmp_path: Path) -> None:
    service, _host, receiver = make_service(tmp_path, FakeHost(existing={AGENT}))
    code = error_code(service.prepare, prepare_request("op-dry", dry_run=True))
    assert code == "TELEPORT_AGENT_EXISTS"
    assert staging_entries(receiver) == []


@pytest.mark.parametrize("first", range(len(CHECKS)), ids=CHECKS)
def test_checks_run_in_plan_order(tmp_path: Path, first: int) -> None:
    host = FakeHost()
    routines = FakeRoutines()
    service, _host, receiver = make_service(tmp_path, host, routines=routines)
    service.prepare(prepare_request("op-1", routines=("nightly",)))
    overrides: dict = {"routines": ("nightly",)}
    if first <= 0:
        overrides["target_node"] = "node-elsewhere"
    if first <= 1:
        overrides["owner"] = "owner-other"
    if first <= 2:
        host.supported.clear()
    if first <= 3:
        host.existing.add(AGENT)
    if first <= 4:
        routines.taken.add("nightly")
    for dry_run in (True, False):
        code = error_code(service.prepare, prepare_request("op-1", dry_run=dry_run, **overrides))
        assert code == CHECKS[first]
    assert service.state("op-1") == "prepared"
    assert staging_entries(receiver) == ["op-1"]


@pytest.mark.parametrize(
    ("overrides", "host_change", "code"),
    [
        ({"target_node": "node-elsewhere"}, None, "TELEPORT_WRONG_TARGET"),
        ({"owner": "owner-other"}, None, "TELEPORT_OWNER_MISMATCH"),
        ({}, "unsupported", "TELEPORT_RUNTIME_UNSUPPORTED"),
        ({"harness": "lark"}, "anything", "TELEPORT_RUNTIME_UNSUPPORTED"),
        ({}, "exists", "TELEPORT_AGENT_EXISTS"),
    ],
)
def test_each_refusal_leaves_no_operation(
    tmp_path: Path, overrides: dict, host_change: str | None, code: str
) -> None:
    host = FakeHost()
    if host_change == "unsupported":
        host.supported.discard("claude")
    if host_change == "anything":
        host.supported.add("lark")
    if host_change == "exists":
        host.existing.add(AGENT)
    service, _host, receiver = make_service(tmp_path, host)
    assert error_code(service.prepare, prepare_request("op-x", **overrides)) == code
    assert service.state("op-x") is None
    assert staging_entries(receiver) == []


def test_routine_conflict_consults_the_routines_port(tmp_path: Path) -> None:
    routines = FakeRoutines(taken={"weekly"})
    service, _host, receiver = make_service(tmp_path, routines=routines)
    code = error_code(
        service.prepare, prepare_request("op-r", routines=("nightly", "weekly"))
    )
    assert code == "TELEPORT_ROUTINE_CONFLICT"
    assert "existing" in routines.calls
    assert service.state("op-r") is None
    assert staging_entries(receiver) == []


def test_repeated_prepare_is_operation_conflict(tmp_path: Path) -> None:
    service, _host, receiver = make_service(tmp_path)
    service.prepare(prepare_request("op-1"))
    assert error_code(service.prepare, prepare_request("op-1")) == "TELEPORT_OPERATION_CONFLICT"
    assert service.state("op-1") == "prepared"
    assert staging_entries(receiver) == ["op-1"]


def test_unsafe_operation_id_is_bad_frame(tmp_path: Path) -> None:
    service, _host, receiver = make_service(tmp_path)
    assert error_code(service.prepare, prepare_request("..")) == "TELEPORT_BAD_FRAME"
    assert staging_entries(receiver) == []
