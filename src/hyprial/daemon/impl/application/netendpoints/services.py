# Decision notes (service grant S5, 2026-11-26, coordinator ruling a):
# - The worker also starts for a home that has neither a protected record nor
#   persisted connections when any org holds an effective grant addressed to
#   this device. Service grants are then its second source (derived_source):
#   entries and devices derived from the directory, never persisted here.
# - That startup question is answered only by an OrgFS runtime the daemon has
#   already built. The startup path has one by now (the delivery plane binds
#   it in the step before this one), so asking costs a bounded read there and
#   nothing is built for a home without a grant; has_effective_grant itself
#   gives up after a short budget and answers "no" on any failure. A wrong
#   "no" only delays the worker to the first service request, which starts it
#   regardless.
# - A home with a record or with connections takes exactly the path it took
#   before the question is ever asked.
"""Service-connect composition over existing daemon-owned authorities."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from pathlib import Path

from hyprial.daemon.impl.service_connect import ServiceManager
from hyprial.daemon.impl.service_grants.consumer import (
    GrantConsumer,
    device_id_of,
    has_effective_grant,
)


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
            runtime = None
            if not force:
                snapshot = state.load()
                if not snapshot.service_connections and not _access_record_present(
                    access_path
                ):
                    runtime = self._orgfs_runtime
                    if not self._held_grant(runtime):
                        return
            # The service manager owns this scheduler. It must not shut down the
            # application's shared maintenance scheduler during its earlier close.
            scheduler = _ServiceScheduler()
            try:
                manager = ServiceManager(
                    orgfs=(runtime or self._require_orgfs_runtime()).facade,
                    state=state,
                    persistence_late_result=self._state_persistence.result,
                    controller_factory=self._service_controller,
                    access_path=access_path,
                    scheduler=scheduler,
                    service_discovery=self._service_discovery,
                    actor_host=self._agent_session_domains.actor_hosts.host_for("service-connect"),
                    derived_source=self._grant_source(),
                )
            except BaseException:
                scheduler.shutdown(5.0)
                raise
            self._service_manager = manager

    def _held_grant(self, runtime) -> bool:
        """The startup answer to "is this device a grant-only recipient?"."""

        return runtime is not None and has_effective_grant(
            orgfs=runtime.facade,
            home=self.hyprial_home,
            owner=self.owner,
            device_id=device_id_of(self.hyprial_home),
        )

    def _grant_source(self) -> GrantConsumer:
        """The grants addressed to this device, as service-connect's second source."""

        return GrantConsumer(
            orgfs=self._require_orgfs_runtime().facade,
            home=self.hyprial_home,
            state_dir=self.state_dir,
            owner=self.owner,
            device_id=device_id_of(self.hyprial_home),
            logger=self._log,
        )

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
