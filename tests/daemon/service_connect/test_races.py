from __future__ import annotations

import threading
import time

import pytest

from hyprial.daemon.impl.service_connect.models import CatalogEntry
from hyprial.daemon.impl.service_connect.io import ControllerCall
from hyprial.daemon.impl.service_connect import ServiceManager




from .support import FakeController, FakeStatePort, ManualScheduler, write_access

CATALOG_ENTRY_PAYLOAD = CatalogEntry.from_json(
    {
        "name": "subapi",
        "deviceId": "sub2api",
        "remotePort": 8080,
        "protocol": "http",
        "localPort": 18080,
        "usage": "claude-code-baseurl",
        "env": {"ANTHROPIC_BASE_URL": "http://127.0.0.1:{port}"},
        "auth": "sub2api API key; agents request grant sub2api-key through hq",
        "guide": "Export the printed environment manually. Existing streams break on restart.\n",
    }
)


class _BoundedLock:
    """Expose the old inversion as a bounded error instead of a leaked deadlock."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.contended = threading.Event()

    def __enter__(self):
        if not self._lock.acquire(blocking=False):
            self.contended.set()
            if not self._lock.acquire(timeout=0.5):
                raise AssertionError("catalog lock acquisition deadlocked")
        return self

    def __exit__(self, *_args) -> None:
        self._lock.release()


def _manager(tmp_path, controller, state):
    access = tmp_path / "state" / "tailcat" / "service-access.json"
    write_access(access)
    return ServiceManager(
        orgfs=None,
        state=state,
        persistence_late_result=None,
        controller_factory=lambda: controller,
        access_path=access,
        scheduler=ManualScheduler(),
        service_discovery=lambda: (CATALOG_ENTRY_PAYLOAD,),
    )


def test_disconnect_during_accepted_map_cannot_resurrect_intent(tmp_path) -> None:
    controller = FakeController()
    controller.block_map = True
    state = FakeStatePort()
    manager = _manager(tmp_path, controller, state)
    connected: list[object] = []
    disconnected: list[object] = []
    connect_thread = threading.Thread(
        target=lambda: connected.append(manager.handle("service.connect", {"name": "subapi"}))
    )
    disconnect_thread = threading.Thread(
        target=lambda: disconnected.append(
            manager.handle("service.disconnect", {"name": "subapi"})
        )
    )
    try:
        connect_thread.start()
        assert controller.map_entered.wait(1.0)
        disconnect_thread.start()
        controller.map_release.set()
        connect_thread.join(1.0)
        disconnect_thread.join(1.0)
        assert not connect_thread.is_alive()
        assert not disconnect_thread.is_alive()
        assert connected[0]["service"]["state"] == "listening"
        assert disconnected == [{"name": "subapi", "removed": True}]
        assert state.snapshot.service_connections == ()
        assert "subapi" not in controller.services
        assert [call[0] for call in controller.calls] == ["map", "unmap"]
    finally:
        controller.map_release.set()
        manager.close(1.0)


def test_reconcile_closes_mapping_when_protected_record_disappears(tmp_path) -> None:
    controller = FakeController()
    state = FakeStatePort()
    manager = _manager(tmp_path, controller, state)
    try:
        manager.handle("service.connect", {"name": "subapi"})
        manager.access_path.unlink()
        manager.reconcile()
        assert "subapi" not in controller.services
        assert state.snapshot.service_connections[0].name == "subapi"
        listed = manager.handle("service.list", {})
        assert listed["services"][0]["state"] == "failed"
    finally:
        manager.close(1.0)


def test_close_retry_drains_a_real_blocking_effect_lane(tmp_path) -> None:
    controller = FakeController()
    controller.block_map = True
    state = FakeStatePort()
    manager = _manager(tmp_path, controller, state)
    close_calls: list[float] = []
    real_io_close = manager._io.close  # noqa: SLF001 - retry regression seam

    def observed_io_close(timeout: float) -> bool:
        close_calls.append(timeout)
        return real_io_close(timeout)

    manager._io.close = observed_io_close  # type: ignore[method-assign]  # noqa: SLF001
    effects: list[object] = []
    effect_thread = threading.Thread(
        target=lambda: effects.append(
            manager._io.control(  # noqa: SLF001 - drain-custody regression seam
                controller,
                "map_service",
                ("subapi", "sub2api", "tc-private", "nodekey:" + "1" * 64, 8080, 7, 18080),
                2.0,
            )
        )
    )
    effect_thread.start()
    assert controller.map_entered.wait(1.0)

    # Mutation contract: replacing the retry path with ``if self._closed:
    # return True`` makes this second assertion fail because the first close
    # fences admission before the effect lane has drained.
    assert manager.close(0.01) is False
    assert len(close_calls) == 1
    controller.map_release.set()
    effect_thread.join(1.0)
    assert not effect_thread.is_alive()
    assert manager.close(1.0) is True
    assert len(close_calls) == 2
    assert effects
    try:
        manager.handle("service.list", {})
    except RuntimeError as error:
        assert str(error) == "service manager is closed"
    else:
        raise AssertionError("closed service manager accepted work")


def test_controller_call_and_effect_request_repr_hide_private_args() -> None:
    from hyprial.kernel import EffectRequest

    address = "tcPrivateAddressCanaryForRepr"
    call = ControllerCall(object(), "map_service", ("subapi", address))

    assert address not in repr(call)
    assert address not in repr(EffectRequest("operation", 1, call))


@pytest.mark.parametrize(
    ("method", "params"),
    [
        ("service.list", {}),
        ("service.guide", {"name": "subapi"}),
        ("service.connect", {"name": "subapi"}),
    ],
)
def test_catalog_io_never_inverts_manager_lock_with_reconcile(
    tmp_path, method: str, params: dict[str, str]
) -> None:
    manager = _manager(tmp_path, FakeController(), FakeStatePort())
    catalog_lock = _BoundedLock()
    manager._catalog_lock = catalog_lock  # noqa: SLF001 - exact lock-order seam
    catalog_read_entered = threading.Event()
    release_catalog_read = threading.Event()
    original_read = manager._io.read_catalog  # noqa: SLF001
    calls = 0

    def interleaved_read(registry, timeout):
        nonlocal calls
        calls += 1
        if calls == 1:
            catalog_read_entered.set()
            assert release_catalog_read.wait(1.0)
        return original_read(registry, timeout)

    manager._io.read_catalog = interleaved_read  # type: ignore[method-assign]  # noqa: SLF001
    errors: list[BaseException] = []

    def run(call) -> None:
        try:
            call()
        except BaseException as error:
            errors.append(error)

    reconcile = threading.Thread(target=lambda: run(manager.reconcile))
    ipc = threading.Thread(target=lambda: run(lambda: manager.handle(method, params)))
    try:
        reconcile.start()
        assert catalog_read_entered.wait(1.0)
        ipc.start()
        assert catalog_lock.contended.wait(1.0)
        release_catalog_read.set()
        reconcile.join(1.0)
        ipc.join(1.0)
        assert not reconcile.is_alive()
        assert not ipc.is_alive()
        assert errors == []
    finally:
        release_catalog_read.set()
        reconcile.join(1.0)
        ipc.join(1.0)
        manager.close(1.0)


def test_close_returns_false_within_deadline_when_manager_lock_is_held(tmp_path) -> None:
    manager = _manager(tmp_path, FakeController(), FakeStatePort())
    held = threading.Event()
    release = threading.Event()

    def hold_manager_lock() -> None:
        with manager._lock:  # noqa: SLF001 - close deadline regression seam
            held.set()
            release.wait(2.0)

    holder = threading.Thread(target=hold_manager_lock)
    holder.start()
    try:
        assert held.wait(1.0)
        started = time.monotonic()
        assert manager.close(0.05) is False
        assert time.monotonic() - started < 0.25
    finally:
        release.set()
        holder.join(1.0)
        assert not holder.is_alive()
        manager.close(1.0)


def test_close_fences_admission_before_blocked_reconcile_drains(tmp_path) -> None:
    controller = FakeController()
    controller.block_map = True
    state = FakeStatePort()
    state.set_service_connection("subapi", 18080)
    manager = _manager(tmp_path, controller, state)
    reconcile_errors: list[BaseException] = []
    admission_errors: list[BaseException] = []

    def run_reconcile() -> None:
        try:
            manager.reconcile()
        except BaseException as error:
            reconcile_errors.append(error)

    def run_admission() -> None:
        try:
            manager.handle("service.list", {})
        except BaseException as error:
            admission_errors.append(error)

    reconcile = threading.Thread(target=run_reconcile)
    admission = threading.Thread(target=run_admission)
    reconcile.start()
    assert controller.map_entered.wait(1.0)
    assert manager.close(0.05) is False
    admission.start()
    admission.join(0.2)
    admission_refused_without_lock = not admission.is_alive()

    controller.map_release.set()
    reconcile.join(1.0)
    admission.join(1.0)
    assert not reconcile.is_alive()
    assert not admission.is_alive()
    assert admission_refused_without_lock
    assert len(admission_errors) == 1
    assert isinstance(admission_errors[0], RuntimeError)
    assert str(admission_errors[0]) == "service manager is closed"
    assert reconcile_errors == []
    assert manager.close(1.0) is True
