"""Service-connect composition over existing daemon-owned authorities."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from pathlib import Path

from hyprial.daemon.impl.service_connect import ServiceManager


class _ServiceScheduler:
    """One long-lived cadence thread, created only for an active manager."""

    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._scheduled: dict[str, tuple[float, int, Callable[[int], None]]] = {}
        self._closed = False
        self._thread = threading.Thread(
            target=self._run,
            daemon=True,
            name="service-connect-scheduler",
        )
        self._thread.start()

    def schedule(
        self,
        key: str,
        generation: int,
        delay: float,
        callback: Callable[[int], None],
    ) -> bool:
        with self._condition:
            if self._closed:
                return False
            self._scheduled[key] = (
                time.monotonic() + max(0.0, delay),
                generation,
                callback,
            )
            self._condition.notify()
            return True

    def cancel(self, key: str) -> None:
        with self._condition:
            self._scheduled.pop(key, None)
            self._condition.notify()

    def shutdown(self, timeout: float) -> bool:
        with self._condition:
            self._closed = True
            self._scheduled.clear()
            self._condition.notify_all()
        self._thread.join(max(0.0, timeout))
        return not self._thread.is_alive()

    def _run(self) -> None:
        while True:
            with self._condition:
                while not self._closed and not self._scheduled:
                    self._condition.wait()
                if self._closed:
                    return
                key, scheduled = min(
                    self._scheduled.items(), key=lambda item: item[1][0]
                )
                deadline, generation, callback = scheduled
                remaining = deadline - time.monotonic()
                if remaining > 0:
                    self._condition.wait(remaining)
                    continue
                if self._scheduled.get(key) != scheduled:
                    continue
                self._scheduled.pop(key, None)
            try:
                callback(generation)
            except BaseException:
                # The callback owns diagnostics and rescheduling. One bad
                # reconcile must not create replacement scheduler threads.
                continue


def _access_record_present(path: Path) -> bool:
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    except OSError:
        # An unreadable or otherwise invalid present record activates the
        # fail-closed manager so the operator gets one transition diagnostic.
        return True
    return True


class _ServiceConnectOpsMixin:
    """Compose and expose one service manager without owning its dependencies."""

    def _service_controller(self) -> object | None:
        backend = self._forwarding_backend()
        return None if backend is None else getattr(backend, "controller", None)

    def _start_service_connect(self, *, force: bool = False) -> None:
        if self._service_manager is not None:
            return
        with self._service_manager_lock:
            if self._service_manager is not None:
                return
            state = getattr(
                self._state_persistence,
                "settled_desired",
                self.desired_state,
            )
            access_path = (
                self.hyprial_home / "state" / "tailcat" / "service-access.json"
            )
            if not force:
                snapshot = state.load()
                if not snapshot.service_connections and not _access_record_present(
                    access_path
                ):
                    return
            # The service manager owns this scheduler. It must not shut down the
            # application's shared maintenance scheduler during its earlier close.
            scheduler = _ServiceScheduler()
            try:
                manager = ServiceManager(
                    orgfs=self._require_orgfs_runtime().facade,
                    state=state,
                    persistence_late_result=self._state_persistence.result,
                    controller_factory=self._service_controller,
                    access_path=access_path,
                    scheduler=scheduler,
                )
            except BaseException:
                scheduler.shutdown(5.0)
                raise
            self._service_manager = manager

    def _handle_service_connect(
        self, method: str, params: dict[str, object]
    ) -> dict[str, object]:
        manager = self._service_manager
        if manager is None:
            self._start_service_connect(force=True)
            with self._service_manager_lock:
                manager = self._service_manager
        assert manager is not None
        return manager.handle(method, params)

    def _close_service_connect(self, timeout: float) -> bool:
        with self._service_manager_lock:
            manager = self._service_manager
            if manager is None:
                return True
            # Fence new IPC acquisition before waiting for the manager's accepted
            # persistence/control work and private scheduler to drain. Retain the
            # manager itself until close confirms success: a timeout is
            # observational and must not discard the only retry/custody handle.
            closed = manager.close(timeout)
            if closed and self._service_manager is manager:
                self._service_manager = None
            return closed


__all__ = ["_ServiceConnectOpsMixin"]
