"""One forwarding reconcile at a time, whichever source asks (review 0d910d1d2 P1).

The maintenance tick and explicit org requests both trigger the forwarding
reconcile (membership read + slot rebinding through the serial sidecar I/O
owner).  They must share one owner -- the ``forwarding-reconcile`` cadence --
including when maintenance started before any supervisor existed and one
appeared later: an inline maintenance reconcile beside a request-created
cadence ran two reconciles concurrently.

The fake carries the real maintenance and forwarding mixin methods; only the
reconcile body is replaced by a probe that measures overlap.
"""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace
from typing import Any

from hyprial.daemon.impl.application.messaging.maintenance import _MaintenanceMixin
from hyprial.daemon.impl.application.netendpoints.forwarding import _ForwardingOpsMixin


class _App(_ForwardingOpsMixin):
    def __init__(self) -> None:
        self.logs: list[tuple[str, str, str, dict[str, Any]]] = []
        self.stop_event = threading.Event()
        self._forwarding_supervisor: object | None = None
        self._forwarding_cadence = None
        self._forwarding_reconcile_requests = self._new_forwarding_reconcile_requests()
        self._runtime = object()
        self._maintenance_watchdog = SimpleNamespace(phase=lambda *_args: None)
        self._status_refresh_due = self._profile_refresh_due = float("inf")
        self._restore_policy_refresh_due = float("inf")
        self._dispatch_cadence = None
        self._workflow_service = self._routine_service = self._pac_actor_service = None
        self._orgfs_runtime = self._lark_client = None
        self.release = threading.Event()
        self.entered = threading.Semaphore(0)
        self._lock = threading.Lock()
        self.active = self.max_active = self.runs = 0

    def _log(self, level: str, domain: str, event: str, **fields: Any) -> None:
        self.logs.append((level, domain, event, fields))

    def _clean_dead_interactive_sessions(self) -> None:
        pass

    def _expire_stale_channel_routes(self, _now: float) -> None:
        pass

    def _note_forwarding_timer_tick(self) -> None:
        pass

    def _runtime_timer(self, _now: int) -> None:
        pass

    def _reconcile_forwarding_endpoints(self) -> None:
        with self._lock:
            self.active += 1
            self.runs += 1
            self.max_active = max(self.max_active, self.active)
        self.entered.release()
        try:
            self.release.wait(3.0)
        finally:
            with self._lock:
                self.active -= 1

    def tick(self) -> threading.Thread:
        thread = threading.Thread(
            target=lambda: _MaintenanceMixin._run_scheduled_domains(self, time.monotonic())
        )
        thread.start()
        return thread

    def named(self, event: str) -> list[dict[str, Any]]:
        return [fields for _l, _d, name, fields in self.logs if name == event]

    def close(self) -> None:
        self.release.set()
        if self._forwarding_cadence is not None:
            assert self._forwarding_cadence.close(3.0)


def _wait(predicate, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def test_late_supervisor_request_never_overlaps_a_maintenance_reconcile() -> None:
    app = _App()
    try:
        # Maintenance ticks before any supervisor exists; its reconcile is held.
        ticker = app.tick()
        assert app.entered.acquire(timeout=2.0)
        # A supervisor appears later and an org request asks for a reconcile.
        app._forwarding_supervisor = object()
        assert app._request_forwarding_reconcile("org-invite", org="group-a", user="bob")
        # Bounded negative wait: a second, concurrent reconcile would enter here.
        assert not app.entered.acquire(timeout=0.2)
        assert app.max_active == 1, "maintenance and request reconciles overlapped"
        app.release.set()
        ticker.join(3.0)
        # The request was recorded while the held run was in flight, so that
        # run cannot settle it (it may have read state before the change):
        # only the follow-up run does, so its event implies both runs.
        assert _wait(lambda: app.named("org.invite.reconcile"))
        assert app.runs == 2, "the request was settled by a run that started before it"
        assert app.max_active == 1
    finally:
        app.close()


def test_maintenance_tick_during_a_request_reconcile_coalesces_behind_it() -> None:
    app = _App()
    app._forwarding_supervisor = object()
    try:
        assert app._request_forwarding_reconcile("org-invite", org="group-a", user="bob")
        assert app.entered.acquire(timeout=2.0)
        ticker = app.tick()
        ticker.join(2.0)
        assert not ticker.is_alive(), "the maintenance tick waited on the held reconcile"
        assert not app.entered.acquire(timeout=0.2)
        assert app.max_active == 1
        app.release.set()
        assert _wait(lambda: app.runs == 2)
        assert app.max_active == 1
    finally:
        app.close()


class _OrgApp(_App):
    """Adds what the real ``_build_service(app)`` reads; OrgFS is never touched."""

    def __init__(self, home) -> None:
        super().__init__()
        self.hyprial_home = home
        self.node_id = "node-a"
        self.owner = "owner@example"

    def _require_orgfs_runtime(self) -> SimpleNamespace:
        return SimpleNamespace(facade=object())

    def _start_forwarding_supervisor(self) -> None:
        pass

    def _forwarding_status_json(self) -> dict[str, Any]:
        return {}


def _bootstrap_service(app: _OrgApp):
    from hyprial.daemon.impl.org.network.ipc import _build_service

    service = _build_service(app)
    service._tailnet_candidates = lambda _space_id=None: {}  # nothing to map
    return service


def test_bootstrap_redial_waits_on_the_cadence_instead_of_reconciling_inline(tmp_path) -> None:
    """Review fd203a80d: join/execute ``connect-inviter`` reconciled on the IPC thread.

    With a cadence run held, the bootstrap step's redial must not start a
    second reconcile beside it; it submits to the cadence and returns only
    once a run that started after its request has completed.
    """
    app = _OrgApp(tmp_path)
    app._forwarding_supervisor = object()
    service = _bootstrap_service(app)
    link = SimpleNamespace(space_id="tailnet-a")
    done = threading.Event()
    try:
        assert app._request_forwarding_reconcile("org-invite", org="group-a", user="bob")
        assert app.entered.acquire(timeout=2.0)
        step = threading.Thread(
            target=lambda: (service._execute_step(link, "connect-inviter"), done.set())
        )
        step.start()
        assert not app.entered.acquire(timeout=0.2), "the bootstrap redial reconciled inline"
        assert app.max_active == 1
        assert not done.is_set(), "the bootstrap step did not wait for its redial"
        app.release.set()
        step.join(3.0)
        assert done.is_set()
        assert app.runs == 2
        assert app.max_active == 1
        assert app.named("org.join.reconcile")[0]["ok"] is True
    finally:
        app.close()


def test_bootstrap_redial_wait_is_bounded_with_a_typed_code(tmp_path, monkeypatch) -> None:
    from hyprial.daemon.impl.org.network import ipc
    from hyprial.daemon.impl.org.network.service import OrgNetworkError
    from hyprial.kernel import ipc_errors

    monkeypatch.setattr(ipc, "REQUEST_MEMBERSHIP_WAIT_SECONDS", 0.2)
    app = _OrgApp(tmp_path)
    app._forwarding_supervisor = object()
    service = _bootstrap_service(app)
    try:
        assert app._request_forwarding_reconcile("org-invite", org="group-a", user="bob")
        assert app.entered.acquire(timeout=2.0)
        started = time.monotonic()
        try:
            service._execute_step(SimpleNamespace(space_id="tailnet-a"), "connect-inviter")
        except OrgNetworkError as error:
            assert error.code == ipc_errors.FORWARDING_RECONCILE_TIMEOUT
        else:
            raise AssertionError("a held reconcile did not bound the bootstrap redial")
        assert time.monotonic() - started < 2.0
        assert app.max_active == 1
    finally:
        app.close()


class _FailingOrgApp(_OrgApp):
    def __init__(self, home, error: Exception) -> None:
        super().__init__(home)
        self.error = error

    def _reconcile_forwarding_endpoints(self) -> None:
        raise self.error


def _bootstrap_failure(tmp_path, error: Exception):
    from hyprial.daemon.impl.org.network.service import OrgNetworkError

    app = _FailingOrgApp(tmp_path, error)
    app._forwarding_supervisor = object()
    service = _bootstrap_service(app)
    try:
        service._execute_step(SimpleNamespace(space_id="tailnet-a"), "connect-inviter")
    except OrgNetworkError as caught:
        return caught
    finally:
        app.close()
    raise AssertionError("a failed bootstrap redial did not fail the step")


def test_uncoded_reconcile_failure_maps_to_forwarding_reconcile_failed(tmp_path) -> None:
    """Review 4089eadcb P2: the exception type name leaked into the IPC code."""
    from hyprial.kernel import ipc_errors

    caught = _bootstrap_failure(tmp_path, RuntimeError("slot table unreadable"))
    assert caught.code == ipc_errors.FORWARDING_RECONCILE_FAILED
    # The diagnostic keeps the error text; only the code is typed.
    assert "RuntimeError" in str(caught)
    assert "slot table unreadable" in str(caught)


def test_coded_reconcile_failure_keeps_its_code(tmp_path) -> None:
    from hyprial.daemon.impl.forwarding.policy import ForwardingSidecarError
    from hyprial.kernel import ipc_errors

    caught = _bootstrap_failure(
        tmp_path,
        ForwardingSidecarError("status stalled", code=ipc_errors.MEMBERSHIP_SIDECAR_TIMEOUT),
    )
    assert caught.code == ipc_errors.MEMBERSHIP_SIDECAR_TIMEOUT
