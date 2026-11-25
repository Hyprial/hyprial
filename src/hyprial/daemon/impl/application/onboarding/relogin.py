# Decision record (L2, daemon-driven serving relogin):
# - One daemon driver polls the credential worker once per published interval,
#   confirms the committed identity (owner, issuer, and uid), drives recovery
#   (device provisioning if missing, user key ensure, and initialize), and
#   publishes each transition on a ``FirstLoginRun`` projection.
# - Lock order is always gateway lock -> run lock -> recovery run lock (never
#   reversed).  Interval sleeps and ``wait_relogin_operation`` parks never hold
#   the gateway lock, so ``auth.operation.get``, ``auth.operation.cancel``, and
#   ``onboarding.status`` stay responsive.
# - A device provisioning failure never revokes the committed identity: it
#   records ``device.ready: false`` with ``retry: serving-start`` and still
#   advances the run stage to ``serving``.
"""Daemon-owned driver and wait helper for serving ``auth.relogin``."""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any

from hyprial.identity import IdentityBegin
from hyprial.kernel import ipc_errors

from hyprial.daemon.impl.request_catalog.onboarding import CallContext
from hyprial.daemon.impl.request_catalog.onboarding.specs import DEVICE_ENSURE_FAILED

from .errors import OnboardingGatewayError

if TYPE_CHECKING:
    from .first_login.completion import FirstLoginRun

_LOG = logging.getLogger("hyprial.daemon.onboarding.relogin")
_DEFAULT_INTERVAL = 5.0
_MAX_INTERVAL = 60.0
_MIN_INTERVAL = 0.01
_MAX_AUTH_SECONDS = 600.0
_JOIN_SECONDS = 5.0
_WAIT_SLICE_SECONDS = 0.25

__all__ = [
    "ServingReloginDriver",
    "identity_summary",
    "relogin_device_block",
    "replay_begin_result",
    "wait_relogin_operation",
]


def replay_begin_result(run: FirstLoginRun) -> dict[str, Any]:
    """Return an ``auth.relogin`` idempotent replay payload from ``run``."""

    operation = run.answer()["operation"]
    return {
        "ok": True,
        "operationId": operation["operationId"],
        "intent": "login",
        "state": operation["state"],
        "verification": operation["verification"],
        "owner": operation["owner"],
        "completion": operation["completion"],
    }


class _Driven:
    """One active relogin driver thread with wake and stop signals."""

    def __init__(self, run: FirstLoginRun, generation: int) -> None:
        self.run = run
        self.generation = generation
        self.wake = threading.Event()
        self.stop = threading.Event()
        self.thread: threading.Thread | None = None

    def poke(self) -> None:
        self.wake.set()

    def abort(self) -> None:
        self.stop.set()
        self.wake.set()

    def wait(self, seconds: float) -> bool:
        fired = self.wake.wait(timeout=max(0.0, seconds))
        if fired:
            self.wake.clear()
        return self.stop.is_set()


def identity_summary(issuer: str | None) -> dict[str, Any]:
    """Build the public completion identity block for a casdoor relogin."""

    return {
        "mode": "casdoor",
        "issuer": issuer,
        "source": "profile",
    }


def _user_key_block(state: Mapping[str, Any] | None) -> dict[str, Any]:
    if state is None:
        return {"state": "unavailable", "code": ipc_errors.USER_KEY_ABSENT}
    code = state.get("code")
    return {
        "state": str(state.get("state")),
        "code": code if isinstance(code, str) else None,
    }


def relogin_device_block(
    *,
    ready: bool,
    record: Any = None,
    user_key: Mapping[str, Any] | None = None,
    error: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Build the schema-compliant completion device block for serving relogin."""

    if ready:
        return {
            "ready": True,
            "deviceId": record.device_id,
            "keyGeneration": record.key_generation,
            "userKey": _user_key_block(user_key),
            "error": None,
            "retry": None,
        }
    from .first_login.completion import authored_error

    return {
        "ready": False,
        "deviceId": None,
        "keyGeneration": None,
        "userKey": {"state": "unavailable", "code": ipc_errors.USER_KEY_ABSENT},
        "error": dict(error) if error is not None else authored_error(DEVICE_ENSURE_FAILED),
        "retry": "serving-start",
    }


def wait_relogin_operation(
    run: FirstLoginRun,
    wait_seconds: float,
    *,
    stopped: threading.Event,
) -> dict[str, Any]:
    """Wait up to ``wait_seconds`` for ``run`` to become final without holding locks."""

    deadline = time.monotonic() + max(0.0, wait_seconds)
    while True:
        seen = run.version
        if run.final:
            return run.answer()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return run.answer()
        if stopped.is_set():
            run.stop()
            return run.answer()
        run.wait_change(seen, min(remaining, _WAIT_SLICE_SECONDS))


class ServingReloginDriver:
    """Owns the background progression of ``auth.relogin(..., until='serving')``."""

    def __init__(
        self,
        *,
        lock: threading.RLock,
        operations: Any,
        relogin: Any,
        recovery: Any,
        identity_reader: Callable[[], Any],
        set_credential: Callable[[str], None],
        mark_unknown: Callable[[CallContext, str], None],
        auth_operation_fn: Callable[[Any, dict[str, Any] | None], dict[str, Any]],
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._lock = lock
        self._operations = operations
        self._relogin = relogin
        self._recovery = recovery
        self._identity_reader = identity_reader
        self._set_credential = set_credential
        self._mark_unknown = mark_unknown
        self._auth_operation = auth_operation_fn
        self._clock = clock
        self._runs: dict[str, FirstLoginRun] = {}
        self._driven: dict[str, _Driven] = {}
        self._generation = 0
        self._stopped = threading.Event()

    @property
    def stopped_event(self) -> threading.Event:
        return self._stopped

    def run_for(self, operation_id: str | None) -> FirstLoginRun | None:
        if not isinstance(operation_id, str) or not operation_id:
            return None
        with self._lock:
            return self._runs.get(operation_id)

    def start(
        self,
        *,
        effective: CallContext,
        begin: IdentityBegin,
        begin_result: dict[str, Any],
        expected_identity: Any,
    ) -> FirstLoginRun:
        """Register and spawn the daemon driver for ``begin``; must hold ``self._lock``."""

        from .first_login.completion import FirstLoginRun

        issuer = getattr(expected_identity, "issuer", None)
        if issuer is None and getattr(self._relogin, "_profile", None) is not None:
            issuer = self._relogin._profile.issuer
        initial_op = {
            "operationId": begin.operation_id,
            "kind": "login",
            "state": "pending",
            "owner": None,
            "receiptId": begin_result.get("operationId"),
            "verification": begin_result.get("verification"),
        }
        try:
            record = self._operations.get(effective, begin.operation_id)
            initial_op = self._auth_operation(record, begin_result.get("verification"))
        except Exception:
            pass
        run = FirstLoginRun(initial_op)
        run.advance("authorizing", identity=identity_summary(issuer))
        self._generation += 1
        driven = _Driven(run, self._generation)
        interval = _bounded_interval(getattr(begin, "interval", _DEFAULT_INTERVAL))
        expires_in = getattr(begin, "expires_in", _MAX_AUTH_SECONDS)
        if isinstance(expires_in, bool) or not isinstance(expires_in, (int, float)) or expires_in <= 0:
            expires_in = _MAX_AUTH_SECONDS
        deadline = self._clock() + min(float(expires_in), _MAX_AUTH_SECONDS)
        self._runs[begin.operation_id] = run
        self._driven[begin.operation_id] = driven
        thread = threading.Thread(
            target=self._drive,
            args=(
                begin.operation_id,
                effective,
                expected_identity,
                identity_summary(issuer),
                begin_result.get("verification"),
                interval,
                deadline,
                driven,
            ),
            name=f"hyprial-serving-relogin-{begin.operation_id[:8]}",
            daemon=True,
        )
        driven.thread = thread
        try:
            thread.start()
        except Exception:
            self._driven.pop(begin.operation_id, None)
            self._runs.pop(begin.operation_id, None)
            raise
        return run

    def poke(self, operation_id: str) -> None:
        with self._lock:
            driven = self._driven.get(operation_id)
        if driven is not None:
            driven.poke()

    def shutdown(self) -> None:
        with self._lock:
            self._stopped.set()
            self._relogin.close()
            driven_list = list(self._driven.values())
            runs = list(self._runs.values())
        for driven in driven_list:
            driven.abort()
        for driven in driven_list:
            thread = driven.thread
            if thread is not None and thread is not threading.current_thread():
                thread.join(_JOIN_SECONDS)
        for run in runs:
            if not run.final:
                run.stop()

    def _drive(
        self,
        operation_id: str,
        effective: CallContext,
        expected_identity: Any,
        id_summary: dict[str, Any],
        verification: dict[str, Any] | None,
        interval: float,
        deadline: float,
        driven: _Driven,
    ) -> None:
        run = driven.run
        try:
            while not self._stopped.is_set() and not driven.stop.is_set():
                action = self._step_poll(
                    operation_id,
                    effective,
                    expected_identity,
                    id_summary,
                    verification,
                    deadline,
                    run,
                    driven,
                )
                if action == "wait":
                    if driven.wait(interval):
                        break
                    continue
                if action == "recover":
                    self._step_recovery(
                        operation_id,
                        effective,
                        expected_identity,
                        id_summary,
                        run,
                        driven,
                    )
                break
        except Exception as error:
            code = getattr(error, "code", None) or ipc_errors.DAEMON_ERROR
            _LOG.warning("serving relogin driver stopped: %s (%s)", code, type(error).__name__)
            with self._lock:
                record = self._record(effective, operation_id)
                self._relogin.close_if_current(operation_id)
                if record is not None and record.state == "cancelled":
                    self._freeze_record(effective, operation_id, run)
                    run.stop()
                elif not self._is_current(operation_id, driven):
                    run.stop()
                else:
                    self._freeze_record(effective, operation_id, run)
                    run.stop(code)
        finally:
            if self._stopped.is_set() and not run.final:
                run.stop()
            with self._lock:
                self._driven.pop(operation_id, None)

    def _step_poll(
        self,
        operation_id: str,
        effective: CallContext,
        expected_identity: Any,
        id_summary: dict[str, Any],
        verification: dict[str, Any] | None,
        deadline: float,
        run: FirstLoginRun,
        driven: _Driven,
    ) -> str:
        with self._lock:
            if not self._is_current(operation_id, driven):
                run.stop()
                return "done"
            record = self._record(effective, operation_id)
            if record is None:
                self._relogin.close_if_current(operation_id)
                self._finish_late_result(None, run)
                return "done"
            if record.state == "cancelled":
                self._relogin.close_if_current(operation_id)
                run.freeze(self._auth_operation(record, None))
                run.stop()
                return "done"
            if record.state == "authenticated":
                self._relogin.close_if_current(operation_id)
                run.publish(self._auth_operation(record, None))
                run.advance("device", identity=id_summary)
                return "recover"
            if self._relogin.live_operation_id != operation_id:
                self._relogin.close_if_current(operation_id)
                self._freeze_record(effective, operation_id, run)
                run.stop()
                return "done"
            if self._clock() >= deadline:
                self._set_credential("failed")
                self._mark_unknown(effective, operation_id)
                self._relogin.close_if_current(operation_id)
                self._freeze_record(effective, operation_id, run)
                run.stop(ipc_errors.IDENTITY_SOURCE_UNAVAILABLE)
                return "done"

        try:
            state = self._relogin.poll(operation_id)
        except OnboardingGatewayError as error:
            with self._lock:
                record = self._record(effective, operation_id)
                if not self._is_current(operation_id, driven):
                    self._relogin.close_if_current(operation_id)
                    self._finish_late_result(record, run)
                    return "done"
                if record is not None and record.state == "authenticated":
                    self._relogin.close_if_current(operation_id)
                    run.publish(self._auth_operation(record, None))
                    run.advance("device", identity=id_summary)
                    return "recover"
                if record is None or record.state != "pending":
                    self._relogin.close_if_current(operation_id)
                    self._finish_late_result(record, run)
                    return "done"
                self._set_credential("failed")
                self._mark_unknown(effective, operation_id)
                self._relogin.close_if_current(operation_id)
                self._freeze_record(effective, operation_id, run)
                run.stop(error.code)
                return "done"

        with self._lock:
            record = self._record(effective, operation_id)
            if not self._is_current(operation_id, driven):
                self._relogin.close_if_current(operation_id)
                self._finish_late_result(record, run)
                return "done"
            if record is not None and record.state == "authenticated":
                self._relogin.close_if_current(operation_id)
                run.publish(self._auth_operation(record, None))
                run.advance("device", identity=id_summary)
                return "recover"
            if record is None or record.state != "pending":
                self._relogin.close_if_current(operation_id)
                self._finish_late_result(record, run)
                return "done"
            if state.state == "pending":
                run.publish(self._auth_operation(record, verification))
                return "wait"
            if state.state == "committed":
                return self._confirm_commit(
                    operation_id,
                    effective,
                    expected_identity,
                    id_summary,
                    state,
                    run,
                    driven,
                )
            self._handle_non_committed(operation_id, effective, state, run)
            return "done"

    def _confirm_commit(
        self,
        operation_id: str,
        effective: CallContext,
        expected_identity: Any,
        id_summary: dict[str, Any],
        state: Any,
        run: FirstLoginRun,
        driven: _Driven,
    ) -> str:
        if not self._is_current(operation_id, driven):
            self._relogin.close_if_current(operation_id)
            run.stop()
            return "done"
        try:
            try:
                self._relogin.verify_commit(state, expected=expected_identity)
            except OnboardingGatewayError as error:
                self._set_credential("failed")
                self._mark_unknown(effective, operation_id)
                self._freeze_record(effective, operation_id, run)
                run.stop(error.code)
                return "done"
            if not self._is_current(operation_id, driven):
                run.stop()
                return "done"
            self._operations.mark_authenticated(effective, operation_id, owner=state.owner)
            self._set_credential("committed")
        finally:
            self._relogin.close_if_current(operation_id)
        record = self._operations.get(effective, operation_id)
        run.publish(self._auth_operation(record, None))
        run.advance("device", identity=id_summary)
        return "recover"

    def _handle_non_committed(
        self,
        operation_id: str,
        effective: CallContext,
        state: Any,
        run: FirstLoginRun,
    ) -> None:
        try:
            if state.state == "cancelled":
                self._set_credential("unverified")
                try:
                    self._operations.cancel(effective, operation_id)
                except Exception:
                    pass
                self._freeze_record(effective, operation_id, run)
                run.stop()
            elif state.state == "failed" and state.error_code == "HOME_ACCOUNT_CONFLICT":
                from hyprial.daemon.impl.onboarding.lifecycle.identity import (
                    project_home_account_conflict,
                )

                self._set_credential("failed")
                op_error = project_home_account_conflict(state)
                try:
                    self._operations.mark_auth_ended(
                        effective, operation_id, "failed", error=op_error
                    )
                except Exception:
                    pass
                self._freeze_record(effective, operation_id, run)
                run.stop(ipc_errors.IDENTITY_CONFLICT)
            else:
                self._set_credential("failed")
                self._mark_unknown(effective, operation_id)
                self._freeze_record(effective, operation_id, run)
                run.stop(state.error_code or ipc_errors.IDENTITY_SOURCE_UNAVAILABLE)
        finally:
            self._relogin.close_if_current(operation_id)

    def _step_recovery(
        self,
        operation_id: str,
        effective: CallContext,
        expected_identity: Any,
        id_summary: dict[str, Any],
        run: FirstLoginRun,
        driven: _Driven,
    ) -> None:
        with self._lock:
            if not self._is_current(operation_id, driven):
                run.stop()
                return
        owner = getattr(expected_identity, "owner", None)
        if not isinstance(owner, str) or not owner:
            owner = getattr(self._identity_reader(), "owner", "")
        device_block = self._recovery.recover_relogin(owner)
        with self._lock:
            if not self._is_current(operation_id, driven):
                run.stop()
                return
            record = self._record(effective, operation_id)
            if record is None or record.state != "authenticated":
                self._finish_late_result(record, run)
                return
            self._freeze_record(effective, operation_id, run)
            run.advance("serving", identity=id_summary, device=device_block)

    def _is_current(self, operation_id: str, driven: _Driven) -> bool:
        return (
            not self._stopped.is_set()
            and not driven.stop.is_set()
            and driven.generation == self._generation
            and self._driven.get(operation_id) is driven
        )

    def _record(self, effective: CallContext, operation_id: str) -> Any:
        try:
            return self._operations.get(effective, operation_id)
        except Exception:
            return None

    def _finish_late_result(self, record: Any, run: FirstLoginRun) -> None:
        if record is not None:
            run.freeze(self._auth_operation(record, None))
        run.stop()

    def _freeze_record(
        self, effective: CallContext, operation_id: str, run: FirstLoginRun
    ) -> None:
        if run.frozen:
            return
        try:
            record = self._operations.get(effective, operation_id)
        except Exception:
            return
        run.freeze(self._auth_operation(record, None))


def _bounded_interval(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return _DEFAULT_INTERVAL
    return max(_MIN_INTERVAL, min(float(value), _MAX_INTERVAL))
