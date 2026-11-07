"""Negative controls for the test-only daemon owner/network tripwire."""

from __future__ import annotations

import threading

import pytest

from tests.support.network_owner_context import (
    DEFAULT_BOUNDARY_POINTS,
    DEFAULT_OWNER_POINTS,
    FunctionPoint,
    OwnerContextTracer,
    OwnerNetworkViolation,
)


def test_default_inventory_covers_every_owner_and_network_boundary_family() -> None:
    assert {point.label for point in DEFAULT_OWNER_POINTS} == {
        "ipc_request",
        "orgfs_space_lock",
        "inbox_writer",
        "state_writer",
    }
    assert {point.label for point in DEFAULT_BOUNDARY_POINTS} >= {
        "zenoh_transport",
        "session_remote",
        "sidecar_wire",
        "workflow_remote",
        "casdoor_api",
        "lark_api",
    }
    OwnerContextTracer()


def test_tripwire_fires_when_an_owner_calls_a_boundary() -> None:
    def black_hole() -> None:
        raise AssertionError("boundary body must not run")

    def ipc_handler() -> None:
        black_hole()

    tracer = OwnerContextTracer(
        owners=(FunctionPoint.from_callable("ipc_request", ipc_handler),),
        boundaries=(FunctionPoint.from_callable("zenoh_transport", black_hole),),
    )
    with tracer.installed(), pytest.raises(OwnerNetworkViolation) as raised:
        ipc_handler()
    assert "ipc_request" in str(raised.value)
    assert "zenoh_transport" in str(raised.value)


def test_boundary_from_a_non_owner_is_not_a_violation() -> None:
    ran: list[int] = []

    def boundary_body() -> None:
        ran.append(1)

    def plain_caller() -> None:
        boundary_body()

    tracer = OwnerContextTracer(
        owners=(FunctionPoint.from_callable("ipc_request", lambda: None),),
        boundaries=(
            FunctionPoint.from_callable("zenoh_transport", boundary_body),
        ),
    )
    with tracer.installed():
        plain_caller()
    assert ran == [1]


def test_tripwire_marks_new_owner_threads() -> None:
    violations: list[BaseException] = []

    def sidecar_wire() -> None:
        raise AssertionError("boundary body must not run")

    def state_writer() -> None:
        try:
            sidecar_wire()
        except BaseException as error:
            violations.append(error)

    tracer = OwnerContextTracer(
        owners=(FunctionPoint.from_callable("state_writer", state_writer),),
        boundaries=(FunctionPoint.from_callable("sidecar_wire", sidecar_wire),),
    )
    with tracer.installed():
        worker = threading.Thread(target=state_writer)
        worker.start()
        worker.join(timeout=2)
    assert len(violations) == 1
    assert isinstance(violations[0], OwnerNetworkViolation)


def test_tripwire_marks_an_owner_thread_that_is_already_running() -> None:
    entered = threading.Event()
    proceed = threading.Event()
    violations: list[BaseException] = []

    def lark_api() -> None:
        raise AssertionError("boundary body must not run")

    def inbox_writer() -> None:
        entered.set()
        assert proceed.wait(2)
        try:
            lark_api()
        except BaseException as error:
            violations.append(error)

    tracer = OwnerContextTracer(
        owners=(FunctionPoint.from_callable("inbox_writer", inbox_writer),),
        boundaries=(FunctionPoint.from_callable("lark_api", lark_api),),
    )
    worker = threading.Thread(target=inbox_writer)
    worker.start()
    assert entered.wait(2)
    with tracer.installed():
        proceed.set()
        worker.join(timeout=2)
    assert not worker.is_alive()
    assert len(violations) == 1
    assert isinstance(violations[0], OwnerNetworkViolation)
