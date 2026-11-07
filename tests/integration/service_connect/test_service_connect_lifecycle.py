from __future__ import annotations

import logging
import threading
import time

import pytest

from hyprial.daemon.impl.application import DaemonApplication
from hyprial.daemon.impl.application.netendpoints import services as services_module
from hyprial.daemon.impl.bootstrap.discovery import ForwardingEndpoints
from hyprial.kernel import DAEMON_CLOSE_STEP_BUDGETS
from hyprial.shell.impl.daemon_wiring import (
    daemon_dependencies as _daemon_dependencies,
)
from tests.daemon.service_connect.support import FakeController, write_access
from hyprial.daemon.impl.service_connect import ServiceConnectError
from tests.support.runtime._owned_applications import (
    owned_applications as owned_applications,
)


class _ClosableFakeController(FakeController):
    def __init__(self) -> None:
        super().__init__()
        self.closed = False

    def close(self) -> None:
        self.closed = True


def _application(
    tmp_path, *, configured: bool = True
) -> tuple[DaemonApplication, _ClosableFakeController]:
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    state = home / "state"
    state.mkdir()
    controller = _ClosableFakeController()
    app = DaemonApplication(
        state_dir=state,
        socket_path=state / "daemon.sock",
        node_id="service-connect-lifecycle",
        hyprial_home=home,
        owner="tester",
        forwarding_discovery=ForwardingEndpoints(controller, slots=lambda: ()),
        **_daemon_dependencies(),
    )
    if configured:
        write_access(state / "tailcat" / "service-access.json")
    return app, controller


def test_unconfigured_startup_is_observably_inactive_until_service_ipc(
    tmp_path, caplog,
) -> None:
    app, _controller = _application(tmp_path, configured=False)
    before = {thread.ident for thread in threading.enumerate()}
    with caplog.at_level(logging.DEBUG):
        app._start_service_connect()

    assert app._service_manager is None
    assert {thread.ident for thread in threading.enumerate()} == before
    assert [
        record for record in caplog.records
        if record.getMessage().startswith("service.connect.")
    ] == []
    assert app._close_service_connect(0.0) is True

    listed = app._handle_service_connect("service.list", {})
    assert listed["services"] == []
    manager = app._service_manager
    assert manager is not None
    for _ in range(4):
        manager.reconcile()
    with pytest.raises(ServiceConnectError) as raised:
        app._handle_service_connect("service.connect", {"name": "subapi"})
    assert raised.value.code == "SERVICE_DEVICE_UNAVAILABLE"
    assert app._close_service_connect(1.0) is True


def test_present_access_record_activates_service_connect_at_startup(tmp_path) -> None:
    app, _controller = _application(tmp_path)
    before = {thread.ident for thread in threading.enumerate()}
    app._start_service_connect()
    assert app._service_manager is not None
    added = [thread for thread in threading.enumerate() if thread.ident not in before]
    assert sum(thread.name == "service-connect-scheduler" for thread in added) == 1
    assert not any(isinstance(thread, threading.Timer) for thread in added)
    assert app._close_service_connect(1.0) is True


def test_service_restore_reuses_port_and_close_precedes_shared_dependencies(
    tmp_path,
) -> None:
    app, first = _application(tmp_path)
    app._start_service_connect()
    app.handle("service.connect", {"name": "subapi"})

    assert app._close_service_connect(5.0) is True
    assert first.calls[-1] == ("unmap", "subapi")

    replacement = _ClosableFakeController()
    app._forwarding_discovery = ForwardingEndpoints(replacement, slots=lambda: ())
    app._start_service_connect()
    assert app._service_manager is not None
    app._service_manager.reconcile()

    assert replacement.calls[0][0] == "map"
    assert replacement.calls[0][1][-1] == 18080


def test_service_connect_has_named_five_second_teardown_budget() -> None:
    assert dict(DAEMON_CLOSE_STEP_BUDGETS)["service-connect"] == 5.0


def test_timed_out_service_close_retains_custody_for_retry(tmp_path) -> None:
    app, _controller = _application(tmp_path)

    class RetryClose:
        def __init__(self) -> None:
            self.outcomes = [False, True]

        def close(self, _timeout: float) -> bool:
            return self.outcomes.pop(0)

    manager = RetryClose()
    app._service_manager = manager

    assert app._close_service_connect(0.01) is False
    assert app._service_manager is manager
    assert app._close_service_connect(5.0) is True
    assert app._service_manager is None


def test_concurrent_first_service_calls_construct_and_close_exactly_one_manager(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, _controller = _application(tmp_path, configured=False)
    app._require_orgfs_runtime()
    real_manager = services_module.ServiceManager
    created = []
    factory_entered = threading.Event()

    def slow_manager(*args, **kwargs):
        factory_entered.set()
        time.sleep(0.05)
        manager = real_manager(*args, **kwargs)
        created.append(manager)
        return manager

    monkeypatch.setattr(services_module, "ServiceManager", slow_manager)
    errors: list[BaseException] = []
    threads = [
        threading.Thread(
            target=lambda: _first_list(app, errors), name=f"first-service-list-{index}"
        )
        for index in range(8)
    ]
    try:
        for thread in threads:
            thread.start()
        assert factory_entered.wait(1.0)
        for thread in threads:
            thread.join(2.0)
            assert not thread.is_alive()
        assert errors == []
        assert len(created) == 1
        assert app._close_service_connect(1.0) is True
        assert not any(
            thread.name == "service-connect-scheduler" and thread.is_alive()
            for thread in threading.enumerate()
        )
    finally:
        for manager in created:
            manager.close(1.0)


def _first_list(app: DaemonApplication, errors: list[BaseException]) -> None:
    try:
        app._handle_service_connect("service.list", {})
    except BaseException as error:
        errors.append(error)
