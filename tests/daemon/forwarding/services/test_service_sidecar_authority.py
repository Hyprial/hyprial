from __future__ import annotations

import threading

import pytest

from hyprial.daemon.impl.forwarding import (
    ForwardingControllerAuthority,
    ServiceControlError,
)

from .test_service_sidecar_controller import MAPPING, PRIVATE_ADDRESS, SERVER_PUBLIC


def _map(owner: ForwardingControllerAuthority) -> dict[str, object]:
    return owner.map_service(
        "api",
        "sub2api",
        PRIVATE_ADDRESS,
        SERVER_PUBLIC,
        8080,
        7,
        18080,
    )


class _Controller:
    pid = 321
    peers_reported = True
    supports_service_connect = True

    def __init__(self) -> None:
        self.calls: list[str] = []

    def map_service(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        self.calls.append("map")
        return dict(MAPPING)

    def unmap_service(self, name: str) -> bool:
        self.calls.append(f"unmap:{name}")
        return True

    def service_status(self) -> dict[str, object]:
        self.calls.append("service-status")
        return {"services": [dict(MAPPING)]}

    def close(self) -> None:
        self.calls.append("close")


def test_native_authority_exposes_capability_and_delegates_service_operations() -> None:
    controller = _Controller()
    owner = ForwardingControllerAuthority(controller)
    assert owner.pid == 321
    assert owner.supports_service_connect is True
    assert _map(owner) == MAPPING
    assert owner.unmap_service("api") is True
    assert owner.service_status() == {"services": [MAPPING]}
    owner.close()
    assert controller.calls == ["map", "unmap:api", "service-status", "close"]


def test_native_authority_refuses_all_service_calls_when_capability_is_absent() -> None:
    controller = _Controller()
    controller.supports_service_connect = False
    owner = ForwardingControllerAuthority(controller)
    for call in (_map, lambda value: value.unmap_service("api"), lambda value: value.service_status()):
        with pytest.raises(ServiceControlError) as raised:
            call(owner)
        assert raised.value.code == "SERVICE_SIDECAR_UNSUPPORTED"
    assert controller.calls == []
    owner.close()


def test_accepted_timeout_retains_custody_and_orders_disconnect_after_completion() -> None:
    entered = threading.Event()
    release = threading.Event()
    settled = threading.Event()

    class Controller(_Controller):
        def map_service(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            self.calls.append("map-enter")
            entered.set()
            assert release.wait(2)
            self.calls.append("map-settled")
            settled.set()
            return dict(MAPPING)

    controller = Controller()
    owner = ForwardingControllerAuthority(controller, timeout=0.05)
    errors: list[BaseException] = []
    worker = threading.Thread(target=lambda: _capture_error(errors, lambda: _map(owner)))
    worker.start()
    assert entered.wait(1)
    worker.join(1)
    assert len(errors) == 1
    assert isinstance(errors[0], TimeoutError)

    disconnected: list[bool] = []
    follower = threading.Thread(target=lambda: disconnected.append(owner.unmap_service("api")))
    follower.start()
    assert not settled.is_set()
    release.set()
    follower.join(2)
    assert disconnected == [True]
    assert controller.calls == ["map-enter", "map-settled", "unmap:api"]
    owner.close()
    assert controller.calls == ["map-enter", "map-settled", "unmap:api", "close"]


def _capture_error(errors: list[BaseException], call) -> None:  # type: ignore[no-untyped-def]
    try:
        call()
    except BaseException as error:
        errors.append(error)


def test_authority_capacity_is_enforced_while_admitted_result_is_pending() -> None:
    entered = threading.Event()
    release = threading.Event()

    class Controller(_Controller):
        def map_service(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            entered.set()
            assert release.wait(2)
            return dict(MAPPING)

    owner = ForwardingControllerAuthority(Controller(), capacity=1, timeout=1.0)
    worker = threading.Thread(target=lambda: _map(owner))
    worker.start()
    assert entered.wait(1)
    with pytest.raises(ServiceControlError, match="overloaded"):
        owner.service_status()
    release.set()
    worker.join(2)
    owner.close()


def test_authority_sanitizes_unexpected_controller_error_and_replacement_is_fresh() -> None:
    class Broken(_Controller):
        def map_service(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            raise RuntimeError(PRIVATE_ADDRESS)

    first = ForwardingControllerAuthority(Broken())
    with pytest.raises(ServiceControlError) as raised:
        _map(first)
    assert raised.value.code == "SERVICE_UNAVAILABLE"
    assert PRIVATE_ADDRESS not in str(raised.value)
    first.close()

    replacement = ForwardingControllerAuthority(_Controller())
    assert _map(replacement) == MAPPING
    replacement.close()
