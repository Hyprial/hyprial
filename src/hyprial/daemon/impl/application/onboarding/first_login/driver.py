# Decision record (Z-5, owner decision 2026-11-18: first login is one daemon operation):
# - Principle.  Clients render and call daemon methods; the daemon owns the login
#   flow.  After ``auth.login.begin`` with ``until: serving`` this driver, and
#   only this driver, polls the issuer until the login is authorised, registers
#   the device, ensures the user key, accepts initialize and lets the same
#   process promote itself to serving.  A client never polls for progress.
# - Locking discipline.  ``BootstrapRuntime`` is not thread-safe (it mutates its
#   phase, worker and receipts with no lock of its own) and the wire's request
#   threads reach it only under ``BootstrapDispatcher.exclusive()``.  The driver
#   therefore runs every step that touches the runtime, the gateway or the
#   journal under that same lock, one step per hold, never across the wait
#   between two polls.  Lock order is always dispatcher lock, then a run's own
#   lock (``completion.py``); a run never takes the dispatcher lock.
# - Promotion.  ``accept_initialize`` runs with ``defer_quiesce=True``: it arms
#   the quiesce hook (the wire host's ``promote`` signal) instead of firing it.
#   The driver ALWAYS takes that armed action (in a ``finally``, so it is never
#   left in the runtime for an unrelated request thread to take) while it still
#   holds the lock, and on success runs it only AFTER it has left the lock, the
#   way a wire request thread runs its armed post-reply action after it replied.
#   ``promote`` is therefore signalled only when no driver is inside the runtime,
#   and once it is signalled the driver takes no further runtime step, so the
#   serving application never starts, nor migrates the journal's transport
#   generation, while the accept is still committing.
# - A failed accept.  It mirrors what the wire path does for an initialize it
#   cannot acknowledge (``initialize-unacknowledged``): the armed action is
#   discarded, never run, and the host is told to end with that outcome through
#   the callback ``enable_promotion`` registered.  The claim stays ``promoting``
#   and ``close`` marks it ``unknown``; the daemon exits and the next serving
#   start's recovery deals with the receipt as it does for the wire path.  The run
#   ends ``stopped`` with a code, and every parked wait is answered.  If the
#   accept failed because another caller's initialize (the GUI's) already took the
#   claim, nothing was armed here and the daemon serves because of that call: the
#   run is left ``starting`` and ``promoted`` ends it ``serving``.
# - Completion of the initialize receipt.  ``promoted`` completes it by the rule
#   that applies to who made the claim: a claim this run made completes only when
#   the device is registered (otherwise it stays ``promoting`` for recovery); a
#   claim made by an explicit ``onboarding.initialize`` completes as it always did.
# - Polling.  One poll per published interval, bounded by the begin's own expiry
#   (capped at the runtime's 600 s permit).  The wait between polls is a bounded
#   event wait that ``cancel``, ``shutdown`` and ``stop`` set, so none of them
#   waits out an interval.  ``auth.operation.get`` never polls the issuer for a
#   driven login: this driver is the only poller.
# - Device.  A device registration that fails during first login does not fail
#   the login: the run records ``ready: false`` and ``retry: serving-start``.
#   Retry is exactly the existing rule, one recovery attempt at each serving
#   start (decision of 2026-11-16 in ``lifecycle/recovery.py``), which now runs
#   at the start this login leads to as well; there is no loop, no backoff and
#   no second attempt here.  The explicit ``onboarding.initialize``
#   the GUI calls keeps raising ``DEVICE_ENSURE_FAILED``.  When the device is not
#   ready at promotion the initialize receipt stays ``promoting``: ``promoted``
#   skips the completion, and recovery on the promoted start completes it only
#   after the device record can be read back.
# - Cancel and expiry.  A cancel pokes the driver.  If the identity had already
#   committed the cancel answers ``identity-committed`` as it always did and the
#   driver goes on to finish; otherwise the phase is back to ``awaiting-auth``
#   and the driver ends.  Expiry reconciles the same way.  Every driver ends in
#   exactly one terminal stage: ``serving`` (set by ``promoted``) or ``stopped``.
# - Secrets.  Nothing from the device flow or any key reaches a run, a log line
#   or a failure message.  A failure is a code and a fixed message authored per
#   code in ``completion.py``; exception text (OS errors, paths, sidecar output)
#   is never copied, and the log names the code and the exception class only.
# - Login code never names an adapter.
"""The daemon-owned driver of a login that runs until the daemon serves."""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from hashlib import sha256
from typing import Any
from uuid import uuid4

from hyprial.identity import IdentityBegin, read_device_record, read_identity_snapshot
from hyprial.kernel import ipc_errors

from hyprial.daemon.impl.onboarding.runtime import BootstrapRuntime
from hyprial.daemon.impl.request_catalog.onboarding import CallContext
from hyprial.daemon.impl.request_catalog.onboarding.specs import DEVICE_ENSURE_FAILED

from ..account_lifecycle import _DEFAULT_INTERVAL_SECONDS, _auth_operation
from ..errors import OnboardingGatewayError
from .completion import RETRY_SERVING_START, FirstLoginRun, authored_error

_LOG = logging.getLogger("hyprial.daemon.onboarding.first_login")
_MAX_AUTH_SECONDS = 600
_JOIN_SECONDS = 5.0
_TOLERATED_DEVICE_CODES = frozenset(
    {DEVICE_ENSURE_FAILED, ipc_errors.SUBMIT_OUTCOME_UNKNOWN}
)

__all__ = ["FirstLogin"]


class _Driven:
    """One driver thread with the two events that end its waits."""

    def __init__(self, run: FirstLoginRun) -> None:
        self.run = run
        self.wake = threading.Event()
        self.stop = threading.Event()
        self.thread: threading.Thread | None = None


class FirstLogin:
    """Starts, tracks and ends the drivers of one bootstrap dispatcher."""

    def __init__(
        self,
        *,
        runtime: BootstrapRuntime,
        gateway: Any,
        context: CallContext,
        exclusive: Callable[[], AbstractContextManager[None]],
        abandon: Callable[[], None],
    ) -> None:
        self._runtime = runtime
        self._gateway = gateway
        self._context = context
        self._exclusive = exclusive
        self._abandon = abandon
        self._guard = threading.Lock()
        self._driven: dict[str, _Driven] = {}
        self._order: list[str] = []

    def run_for(self, operation_id: object) -> FirstLoginRun | None:
        """The run of ``operation_id``, if this daemon drives that login."""

        if not isinstance(operation_id, str):
            return None
        with self._guard:
            driven = self._driven.get(operation_id)
        return None if driven is None else driven.run

    def latest(self) -> FirstLoginRun | None:
        """The most recently started run, if any."""

        with self._guard:
            if not self._order:
                return None
            return self._driven[self._order[-1]].run

    def start(
        self, begin: IdentityBegin, *, verification: Mapping[str, Any], source: str | None
    ) -> FirstLoginRun:
        """Start the driver of ``begin`` (called under the dispatcher lock)."""

        run = FirstLoginRun(
            {
                "operationId": begin.operation_id,
                "kind": "login",
                "state": "pending",
                "owner": None,
                "receiptId": None,
                "verification": dict(verification),
            }
        )
        driven = _Driven(run)
        thread = threading.Thread(
            target=self._drive,
            args=(driven, begin, dict(verification), source),
            name="hyprial-first-login",
            daemon=True,
        )
        driven.thread = thread
        with self._guard:
            self._driven[begin.operation_id] = driven
            self._order.append(begin.operation_id)
        try:
            thread.start()
        except BaseException:
            with self._guard:
                self._driven.pop(begin.operation_id, None)
                self._order.remove(begin.operation_id)
            raise
        return run

    def poke(self, operation_id: str) -> None:
        """Wake the driver of ``operation_id`` (a cancel changed its phase)."""

        with self._guard:
            driven = self._driven.get(operation_id)
        if driven is not None:
            driven.wake.set()

    def join(self, timeout: float) -> bool:
        """Wait up to ``timeout`` seconds in all for every driver thread to end."""

        deadline = time.monotonic() + timeout
        with self._guard:
            threads = [d.thread for d in self._driven.values() if d.thread is not None]
        for thread in threads:
            if thread is not threading.current_thread():
                thread.join(timeout=max(0.0, deadline - time.monotonic()))
        return not any(t.is_alive() for t in threads if t is not threading.current_thread())

    def shutdown(self) -> None:
        """Stop every driver, wait for the threads, and end the runs unserved."""

        with self._guard:
            driven = list(self._driven.values())
        for item in driven:
            item.stop.set()
            item.wake.set()
        self.join(_JOIN_SECONDS)
        for item in driven:
            item.run.stop()

    def promoted(self, *, complete: Callable[[], None]) -> None:
        """Run at the serving daemon's ready boundary, in place of a bare completion.

        A claim this run made completes only when the device is registered; a
        claim made by an explicit ``onboarding.initialize`` completes as it
        always did.  The run, when not already ended, is then ``serving``.
        """

        run = self.latest()
        try:
            if run is None or not run.claimed or run.device_ready:
                complete()
        except BaseException:
            if run is not None:
                run.stop(ipc_errors.DAEMON_ERROR)
            raise
        if run is not None:
            run.advance("serving")

    def _drive(
        self,
        driven: _Driven,
        begin: IdentityBegin,
        verification: dict[str, Any],
        source: str | None,
    ) -> None:
        run = driven.run
        try:
            if self._authorize(driven, begin, verification):
                self._register(driven, source)
                self._start_serving(driven)
        except Exception as error:
            self._abort(driven, error)
        finally:
            if run.stage != "starting":
                run.stop()

    def _authorize(
        self, driven: _Driven, begin: IdentityBegin, verification: Mapping[str, Any]
    ) -> bool:
        run = driven.run
        interval = verification.get("intervalSeconds")
        if type(interval) is not int or interval <= 0:
            interval = _DEFAULT_INTERVAL_SECONDS
        lifetime = begin.expires_in
        if type(lifetime) is not int or lifetime <= 0:
            lifetime = _MAX_AUTH_SECONDS
        deadline = time.monotonic() + min(lifetime, _MAX_AUTH_SECONDS)
        surface = self._context.surface
        while True:
            with self._exclusive():
                if driven.stop.is_set():
                    self._freeze(run)
                    return False
                phase = self._runtime.phase
                if phase == "authenticated":
                    self._publish(run)
                    return True
                if not self._in_flight(run, phase):
                    self._freeze(run)
                    return False
                if time.monotonic() >= deadline:
                    self._runtime.cancel_authentication(self._context, run.operation_id)
                    if self._runtime.phase == "authenticated":
                        self._publish(run)
                        return True
                    self._freeze(run)
                    return False
                self._runtime.poll_authentication(surface=surface)
                phase = self._runtime.phase
                if phase == "authenticated":
                    self._publish(run)
                    return True
                if not self._in_flight(run, phase):
                    self._freeze(run)
                    return False
            driven.wake.wait(timeout=float(interval))
            driven.wake.clear()

    def _in_flight(self, run: FirstLoginRun, phase: str) -> bool:
        last = self._runtime.last_begin
        return (
            phase == "authenticating"
            and last is not None
            and last.operation_id == run.operation_id
        )

    def _register(self, driven: _Driven, source: str | None) -> None:
        run = driven.run
        with self._exclusive():
            if driven.stop.is_set():
                raise RuntimeError("the daemon is stopping")
            snapshot = read_identity_snapshot(self._runtime.home)
            run.advance(
                "device",
                identity={
                    "mode": snapshot.mode,
                    "issuer": snapshot.issuer,
                    "source": source,
                },
            )
            device = self._ensure_device(run)
            run.advance("device", device=device)

    def _ensure_device(self, run: FirstLoginRun) -> dict[str, Any]:
        key = "first-login-device-" + sha256(run.operation_id.encode()).hexdigest()
        try:
            self._gateway.ensure_device(self._context, operation_key=key)
        except OnboardingGatewayError as error:
            if error.code not in _TOLERATED_DEVICE_CODES:
                raise
            _LOG.warning(
                "first-login device registration failed: %s (%s)",
                error.code,
                type(error.__cause__).__name__,
            )
            return _device_block(ready=False, error=authored_error(error.code))
        owner = self._runtime.verified_owner()
        record = read_device_record(self._runtime.home)
        if owner is None or record is None or record.owner != owner:
            raise OnboardingGatewayError(
                ipc_errors.IDENTITY_UNBOUND,
                "the device record is not readable for the committed owner",
            )
        try:
            key_state = self._gateway.ensure_login_key(owner, record)
        except Exception as error:
            _LOG.warning("first-login user key failed: %s", type(error).__name__)
            key_state = None
        return _device_block(ready=True, record=record, user_key=key_state)

    def _start_serving(self, driven: _Driven) -> None:
        run = driven.run
        armed: Callable[[], None] | None = None
        failure: Exception | None = None
        with self._exclusive():
            if driven.stop.is_set():
                raise RuntimeError("the daemon is stopping")
            self._freeze(run)
            run.advance("starting")
            try:
                self._runtime.accept_initialize(
                    surface=self._context.surface,
                    operation_key="first-login-initialize-" + uuid4().hex,
                    operation_id=uuid4().hex,
                    receipt_id=uuid4().hex,
                    defer_quiesce=True,
                )
            except Exception as error:
                failure = error
            finally:
                armed = self._runtime.take_post_reply()
            if failure is None:
                run.claim()
            elif armed is None and self._runtime.phase == "promoting":
                failure = None
        if failure is not None:
            if armed is not None:
                self._abandon()
            raise failure
        if armed is not None:
            armed()

    def _operation(self, run: FirstLoginRun) -> dict[str, Any] | None:
        try:
            record = self._runtime.get_operation(
                CallContext(
                    surface=self._context.surface,
                    phase="bootstrap",
                    caller="local-bootstrap",
                    owner=self._runtime.verified_owner(),
                ),
                run.operation_id,
            )
        except Exception as error:
            _LOG.warning("first-login operation was not readable: %s", type(error).__name__)
            return None
        return _auth_operation(record, None)

    def _publish(self, run: FirstLoginRun) -> None:
        operation = self._operation(run)
        if operation is not None:
            run.publish(operation)

    def _freeze(self, run: FirstLoginRun) -> None:
        if run.frozen:
            return
        operation = self._operation(run)
        if operation is not None:
            run.freeze(operation)

    def _abort(self, driven: _Driven, error: Exception) -> None:
        run = driven.run
        code = getattr(error, "code", None)
        code = authored_error(code)["code"]
        _LOG.warning("first login stopped: %s (%s)", code, type(error).__name__)
        try:
            with self._exclusive():
                if self._in_flight(run, self._runtime.phase):
                    self._runtime.cancel_authentication(self._context, run.operation_id)
                self._freeze(run)
        except Exception as cleanup:
            _LOG.warning("first-login cleanup failed: %s", type(cleanup).__name__)
        run.stop(code)


def _device_block(
    *,
    ready: bool,
    record: Any = None,
    user_key: Mapping[str, Any] | None = None,
    error: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    if ready:
        return {
            "ready": True,
            "deviceId": record.device_id,
            "keyGeneration": record.key_generation,
            "userKey": _user_key(user_key),
            "error": None,
            "retry": None,
        }
    return {
        "ready": False,
        "deviceId": None,
        "keyGeneration": None,
        "userKey": {"state": "unavailable", "code": ipc_errors.USER_KEY_ABSENT},
        "error": None if error is None else dict(error),
        "retry": RETRY_SERVING_START,
    }


def _user_key(state: Mapping[str, Any] | None) -> dict[str, Any]:
    if state is None:
        return {"state": "unavailable", "code": ipc_errors.USER_KEY_ABSENT}
    code = state.get("code")
    return {"state": str(state.get("state")), "code": code if isinstance(code, str) else None}
