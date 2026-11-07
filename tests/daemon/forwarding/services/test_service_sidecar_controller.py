from __future__ import annotations

import json
import queue
import threading
import time
from collections.abc import Iterator

import pytest

from hyprial.daemon.impl.forwarding import controller as controller_module

from hyprial.daemon.impl.forwarding import (
    ForwardingSidecarController,
    ForwardingSidecarError,
    ServiceControlError,
)


SERVER_PUBLIC = "nodekey:" + "a" * 64
PRIVATE_ADDRESS = "tcPrivateCanaryAddress123456789"
MAPPING = {
    "name": "api",
    "deviceId": "sub2api",
    "remotePort": 8080,
    "recordGeneration": 7,
    "localPort": 18080,
    "activeConnections": 0,
    "path": "unknown",
    "pathDetail": None,
    "observedAtMs": None,
    "lastDialState": "unknown",
    "lastDialMs": None,
    "lastError": None,
    "lastErrorAtMs": None,
}


def _controller(
    events: Iterator[dict[str, object]], *, capable: bool = True
) -> tuple[ForwardingSidecarController, list[dict[str, object]], list[str]]:
    controller = object.__new__(ForwardingSidecarController)
    controller._lock = threading.Lock()
    controller._write_lock = threading.Lock()
    controller._service_lock = threading.Lock()
    controller._closing = False
    controller._service_capable = capable
    controller._service_wire_failed = False
    controller._timeout = 3.0
    controller._service_timeout = 3.0
    writes: list[dict[str, object]] = []
    terminations: list[str] = []
    controller._send = lambda payload: writes.append(dict(payload))  # type: ignore[method-assign]
    controller._next = lambda *, deadline: next(events)  # type: ignore[method-assign]
    controller._terminate_owned_process = lambda: terminations.append("terminated")  # type: ignore[method-assign]
    return controller, writes, terminations


def _map(controller: ForwardingSidecarController) -> dict[str, object]:
    return controller.map_service(
        "api",
        "sub2api",
        PRIVATE_ADDRESS,
        SERVER_PUBLIC,
        8080,
        7,
        18080,
    )


def test_capable_v3_service_wire_uses_exact_correlated_requests() -> None:
    events: list[dict[str, object]] = []
    controller, writes, _terminations = _controller(iter(events))

    def answer(*, deadline: float) -> dict[str, object]:
        del deadline
        request = writes[-1]
        if request["op"] == "map-service":
            return {
                "v": 6,
                "event": "service-mapped",
                "requestId": request["requestId"],
                "service": dict(MAPPING),
            }
        if request["op"] == "unmap-service":
            return {
                "v": 6,
                "event": "service-unmapped",
                "requestId": request["requestId"],
                "name": "api",
                "removed": True,
            }
        return {
            "v": 6,
            "event": "service-status",
            "requestId": request["requestId"],
            "services": [dict(MAPPING)],
        }

    controller._next = answer  # type: ignore[method-assign]
    assert controller.supports_service_connect is True
    assert _map(controller) == MAPPING
    assert controller.unmap_service("api") is True
    assert controller.service_status() == {"services": [MAPPING]}

    assert [set(item) for item in writes] == [
        {
            "v",
            "op",
            "requestId",
            "name",
            "deviceId",
            "address",
            "serverPublic",
            "remotePort",
            "recordGeneration",
            "localPort",
        },
        {"v", "op", "requestId", "name"},
        {"v", "op", "requestId"},
    ]
    assert len({item["requestId"] for item in writes}) == 3


@pytest.mark.parametrize("operation", ["map", "unmap", "status"])
def test_missing_service_capability_rejects_every_control_op_without_a_write(
    operation: str,
) -> None:
    controller, writes, _terminations = _controller(iter(()), capable=False)
    with pytest.raises(ServiceControlError) as raised:
        if operation == "map":
            _map(controller)
        elif operation == "unmap":
            controller.unmap_service("api")
        else:
            controller.service_status()
    assert raised.value.code == "SERVICE_SIDECAR_UNSUPPORTED"
    assert writes == []


@pytest.mark.parametrize(
    "mutate",
    [
        lambda event: event.update(v=2),
        lambda event: event.update(requestId="another-request"),
        lambda event: event["service"].update(activeConnections=True),
        lambda event: event["service"].update(deviceId="other-device"),
        lambda event: event["service"].update(remotePort=8081),
        lambda event: event["service"].update(recordGeneration=8),
        lambda event: event["service"].update(localPort=18081),
        lambda event: event["service"].pop("pathDetail"),
        lambda event: event["service"].update(lastDialMs=float("nan")),
        lambda event: event["service"].update(lastError=PRIVATE_ADDRESS),
        lambda event: event["service"].update(lastError=SERVER_PUBLIC),
    ],
)
def test_service_response_validation_is_closed_correlated_and_secret_safe(
    mutate,
) -> None:
    controller, writes, terminations = _controller(iter(()))
    controller._service_timeout = 0.05

    def answer(*, deadline: float) -> dict[str, object]:
        del deadline
        event: dict[str, object] = {
            "v": 6,
            "event": "service-mapped",
            "requestId": writes[-1]["requestId"],
            "service": dict(MAPPING),
        }
        mutate(event)
        return event

    controller._next = answer  # type: ignore[method-assign]
    with pytest.raises(ServiceControlError) as raised:
        _map(controller)
    assert raised.value.code == "SERVICE_UNAVAILABLE"
    assert PRIVATE_ADDRESS not in str(raised.value)
    assert terminations == []


def test_registered_service_failure_is_nonfatal_and_next_peer_reply_is_not_confused() -> None:
    controller, writes, terminations = _controller(iter(()))

    def answer(*, deadline: float) -> dict[str, object]:
        del deadline
        request = writes[-1]
        if request["op"] == "map-service":
            return {
                "v": 6,
                "event": "service-failed",
                "requestId": request["requestId"],
                "op": "map-service",
                "name": "api",
                "code": "PORT_IN_USE",
                "message": PRIVATE_ADDRESS,
            }
        return {"v": 5, "event": "peer-mapped", "peer": "peer-a", "slot": "/s/slot-00"}

    controller._next = answer  # type: ignore[method-assign]
    with pytest.raises(ServiceControlError) as raised:
        _map(controller)
    assert raised.value.code == "SERVICE_PORT_IN_USE"
    assert PRIVATE_ADDRESS not in str(raised.value)
    assert controller.map_peer("peer-a", "tcPeerAddress", "/s/slot-00") == "/s/slot-00"
    assert terminations == []


def test_correlated_malformed_service_reply_keeps_mesh_process_usable() -> None:
    controller, writes, terminations = _controller(iter(()))

    def answer(*, deadline: float) -> dict[str, object]:
        del deadline
        request = writes[-1]
        if request["op"] == "map-service":
            malformed = dict(MAPPING)
            malformed["activeConnections"] = True
            return {
                "v": 6,
                "event": "service-mapped",
                "requestId": request["requestId"],
                "service": malformed,
            }
        return {"v": 5, "event": "peer-mapped", "peer": "peer-a", "slot": "/s/slot-00"}

    controller._next = answer  # type: ignore[method-assign]
    with pytest.raises(ServiceControlError):
        _map(controller)
    assert terminations == []
    assert controller.map_peer("peer-a", "tcPeerAddress", "/s/slot-00") == "/s/slot-00"


def test_timed_out_service_request_isolated_from_mesh_and_late_reply() -> None:
    controller, writes, terminations = _controller(iter(()))
    calls = 0

    def answer(*, deadline: float) -> dict[str, object]:
        nonlocal calls
        del deadline
        calls += 1
        if calls == 1:
            raise ForwardingSidecarError("forwarding sidecar response timed out")
        if calls == 2:
            return {
                "v": 6,
                "event": "service-mapped",
                "requestId": writes[0]["requestId"],
                "service": dict(MAPPING),
            }
        return {"v": 5, "event": "peer-mapped", "peer": "peer-a", "slot": "/s/slot-00"}

    controller._next = answer  # type: ignore[method-assign]
    with pytest.raises(ServiceControlError) as raised:
        _map(controller)
    assert raised.value.code == "SERVICE_UNAVAILABLE"
    assert PRIVATE_ADDRESS not in str(raised.value)
    assert terminations == []
    assert controller.map_peer("peer-a", "tcPeerAddress", "/s/slot-00") == "/s/slot-00"


def test_slow_service_reply_does_not_hold_mesh_status_operation_lock() -> None:
    controller, writes, terminations = _controller(iter(()))
    events: queue.Queue[dict[str, object]] = queue.Queue()
    controller._next = lambda *, deadline: events.get(  # type: ignore[method-assign]
        timeout=max(0.01, deadline - time.monotonic())
    )
    mapped: list[dict[str, object]] = []
    service_thread = threading.Thread(target=lambda: mapped.append(_map(controller)))
    service_thread.start()
    deadline = time.monotonic() + 1.0
    while not writes and time.monotonic() < deadline:
        service_thread.join(0.001)
    assert writes

    events.put(
        {"v": 5, "event": "peer-mapped", "peer": "peer-a", "slot": "/s/slot-00"}
    )
    assert controller.map_peer("peer-a", "tcPeerAddress", "/s/slot-00") == "/s/slot-00"
    events.put(
        {
            "v": 6,
            "event": "service-mapped",
            "requestId": writes[0]["requestId"],
            "service": dict(MAPPING),
        }
    )
    service_thread.join(1.0)
    assert not service_thread.is_alive()
    assert mapped == [MAPPING]
    assert terminations == []


def test_service_request_validation_rejects_bools_and_private_values_without_echoing() -> None:
    controller, writes, _terminations = _controller(iter(()))
    with pytest.raises(ServiceControlError) as raised:
        controller.map_service(
            "api",
            "sub2api",
            PRIVATE_ADDRESS,
            SERVER_PUBLIC,
            True,
            7,
        )
    assert raised.value.code == "SERVICE_INVALID_TARGET"
    assert PRIVATE_ADDRESS not in str(raised.value)
    assert writes == []


@pytest.mark.parametrize(
    ("correlated", "response_correlated"),
    [(False, False), (True, True), (True, False)],
)
def test_forwarding_error_is_not_consumed_by_concurrent_service_request(
    correlated: bool, response_correlated: bool,
) -> None:
    controller, writes, terminations = _controller(iter(()))
    controller._forwarding_request_ids = correlated
    controller._events = queue.Queue()

    class _LiveProcess:
        @staticmethod
        def poll():
            return None

    controller._process = _LiveProcess()
    del controller._next  # reveal the real queue reader installed on the class
    mapped: list[dict[str, object]] = []
    service_errors: list[BaseException] = []
    forwarding_errors: list[BaseException] = []

    def run_service() -> None:
        try:
            mapped.append(_map(controller))
        except BaseException as error:
            service_errors.append(error)

    def run_forwarding() -> None:
        try:
            controller.map_peer("peer-a", "tcPeerAddress", "/s/slot-00")
        except BaseException as error:
            forwarding_errors.append(error)

    service = threading.Thread(target=run_service)
    forwarding = threading.Thread(target=run_forwarding)
    service.start()
    deadline = time.monotonic() + 1.0
    while len(writes) < 1 and time.monotonic() < deadline:
        service.join(0.001)
    assert len(writes) == 1
    deadline = time.monotonic() + 1.0
    while not getattr(controller, "_response_reader", False) and time.monotonic() < deadline:
        service.join(0.001)
    assert controller._response_reader is True
    forwarding.start()
    deadline = time.monotonic() + 1.0
    while len(writes) < 2 and time.monotonic() < deadline:
        forwarding.join(0.001)
    assert len(writes) == 2
    forwarding_error = {
        "v": 6,
        "event": "error",
        "code": "PORT_IN_USE",
        "message": "busy",
    }
    if response_correlated:
        forwarding_error["requestId"] = writes[1]["requestId"]
    controller._events.put(json.dumps(forwarding_error).encode())
    forwarding.join(1.0)
    assert not forwarding.is_alive()
    controller._events.put(
        json.dumps(
            {
                "v": 6,
                "event": "service-mapped",
                "requestId": writes[0]["requestId"],
                "service": dict(MAPPING),
            }
        ).encode()
    )
    service.join(1.0)
    assert not service.is_alive()
    assert mapped == [MAPPING]
    assert service_errors == []
    assert len(forwarding_errors) == 1
    assert "PORT_IN_USE" in str(forwarding_errors[0])
    assert terminations == []
    assert list(controller._response_buffer) == []  # noqa: SLF001


def test_response_buffer_has_count_and_age_bounds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(controller_module, "_RESPONSE_BUFFER_CAPACITY", 8)
    monkeypatch.setattr(controller_module, "_RESPONSE_BUFFER_MAX_AGE_SECONDS", 0.01)
    controller, _writes, _terminations = _controller(iter(()))
    target = {"v": 5, "event": "target", "requestId": "target-1"}
    noise = [
        {"v": 5, "event": "state", "requestId": f"noise-{index}"}
        for index in range(20)
    ]
    controller._next = lambda *, deadline: next(events)  # type: ignore[method-assign]
    events = iter([*noise, target])

    assert controller._await_response(  # noqa: SLF001
        lambda event: event.get("requestId") == "target-1",
        deadline=time.monotonic() + 1.0,
    ) == target
    assert len(controller._response_buffer) <= 8  # noqa: SLF001

    time.sleep(0.02)
    target_2 = {"v": 5, "event": "target", "requestId": "target-2"}
    events = iter([target_2])
    assert controller._await_response(  # noqa: SLF001
        lambda event: event.get("requestId") == "target-2",
        deadline=time.monotonic() + 1.0,
    ) == target_2
    assert list(controller._response_buffer) == []  # noqa: SLF001


def test_expired_late_service_reply_cannot_fail_the_next_service_request() -> None:
    controller, writes, terminations = _controller(iter(()))
    controller._stale_service_requests = {"expired-request": time.monotonic() - 3600}
    calls = 0

    def answer(*, deadline: float) -> dict[str, object]:
        nonlocal calls
        del deadline
        calls += 1
        if calls == 1:
            return {
                "v": 6,
                "event": "service-failed",
                "requestId": "expired-request",
                "op": "map-service",
                "name": "api",
                "code": "PORT_IN_USE",
                "message": "late",
            }
        return {
            "v": 6,
            "event": "service-mapped",
            "requestId": writes[-1]["requestId"],
            "service": dict(MAPPING),
        }

    controller._next = answer  # type: ignore[method-assign]
    assert _map(controller) == MAPPING
    assert terminations == []
    assert controller._dropped_service_responses == 1  # noqa: SLF001
    assert list(controller._response_buffer) == []  # noqa: SLF001


def test_stale_service_request_tracking_has_count_and_age_bounds() -> None:
    controller, _writes, _terminations = _controller(iter(()))
    controller._next = lambda *, deadline: (_ for _ in ()).throw(  # type: ignore[method-assign]
        ForwardingSidecarError("forwarding sidecar response timed out")
    )
    for _ in range(80):
        with pytest.raises(ServiceControlError):
            _map(controller)
    assert len(controller._stale_service_requests) <= 64
    controller._stale_service_requests["expired"] = time.monotonic() - 3600
    with pytest.raises(ServiceControlError):
        _map(controller)
    assert "expired" not in controller._stale_service_requests


def test_broken_shared_stream_still_terminates_the_sidecar_once() -> None:
    # Exact-ID matching drops stray replies (review 962 m4); a genuinely broken
    # stream must still trip the restart fence exactly once.
    controller, _writes, terminations = _controller(iter(()))

    def broken(*, deadline: float) -> dict[str, object]:
        del deadline
        raise ForwardingSidecarError("forwarding sidecar stream is not valid JSON")

    controller._next = broken  # type: ignore[method-assign]
    with pytest.raises(ServiceControlError) as raised:
        _map(controller)
    assert raised.value.wire_failed is True
    assert terminations == ["terminated"]
