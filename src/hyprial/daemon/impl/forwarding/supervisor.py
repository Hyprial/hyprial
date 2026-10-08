"""ForwardingSidecarSupervisor and its relaunch state machine."""

from __future__ import annotations

from __future__ import annotations
from __future__ import annotations
import random
import threading
import time
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any
import uuid
from enum import StrEnum
from hyprial.kernel import (
    DEFAULT_POLICIES,
    PROCESS_LIFECYCLE,
    SupervisionPolicy,
)
from hyprial.kernel import GenerationScheduler
from hyprial.kernel import ActorRuntime, ActorSpec, AdmissionResult
from hyprial.daemon.impl.bootstrap.discovery  import ForwardingEndpoints

from .controller import (
    ForwardingControllerAuthority,
    ForwardingSidecarController,
)
from .policy import (
    ForwardingSidecarError,
)


_FORWARDING_LAUNCH_KEY = "forwarding-sidecar-relaunch"


class _SupervisorAction(StrEnum):
    SNAPSHOT = "snapshot"
    BEGIN_START = "begin_start"
    BEGIN_CLOSE = "begin_close"
    OBSERVE_EXIT = "observe_exit"
    STATUS_FAILURE = "status_failure"
    STATUS_SUCCESS = "status_success"
    RELAUNCH_DUE = "relaunch_due"
    INSTALL = "install"
    ACCOUNT_FAILURE = "account_failure"


@dataclass(frozen=True, slots=True)
class _SupervisorCommand:
    operation_id: str
    action: _SupervisorAction
    token: str = ""
    backend: ForwardingEndpoints | None = None
    expected_generation: int = 0
    exit_status: int | None = None


@dataclass(frozen=True, slots=True)
class _SupervisorSnapshot:
    state: str
    failures: int
    backend: ForwardingEndpoints | None
    generation: int
    closed: bool


class _SupervisorReply:
    def __init__(self) -> None:
        self.ready = threading.Event()
        self.value: object | None = None
        self.error: BaseException | None = None


class ForwardingSupervisorState:
    """Pure mailbox owner of sidecar incarnation and restart decisions."""

    def __init__(
        self, policy: SupervisionPolicy, jitter_source: Callable[[], float],
    ) -> None:
        self._policy = policy
        self._jitter_source = jitter_source
        self._guard = threading.Lock()
        self._pending: dict[str, _SupervisorReply] = {}
        self._backend: ForwardingEndpoints | None = None
        self._installed: dict[str, ForwardingEndpoints] = {}
        self._early_exits: dict[str, int] = {}
        self._failures: deque[float] = deque()
        self._consecutive = 0
        self._generation = 0
        self._first_happened = False
        self._closed = False
        self._runtime = ActorRuntime()
        self._handle = self._runtime.start(
            ActorSpec(
                name="forwarding-supervisor-state",
                handler_factory=lambda: self._receive,
                mailbox_capacity=64,
            )
        )

    def call(
        self, action: _SupervisorAction, *, token: str = "",
        backend: ForwardingEndpoints | None = None,
        expected_generation: int = 0, exit_status: int | None = None,
    ) -> object | None:
        command = _SupervisorCommand(
            uuid.uuid4().hex, action, token, backend, expected_generation,
            exit_status,
        )
        reply = _SupervisorReply()
        with self._guard:
            if len(self._pending) >= 64:
                raise TimeoutError("forwarding supervisor state overloaded")
            self._pending[command.operation_id] = reply
            admitted = self._runtime.tell(self._handle, command)
            if admitted is not AdmissionResult.ACCEPTED:
                self._pending.pop(command.operation_id)
                raise TimeoutError(
                    f"forwarding supervisor state {admitted.value}"
                )
        if not reply.ready.wait(2.0):
            raise TimeoutError(
                f"forwarding state {command.operation_id} remains accepted"
            )
        if reply.error is not None:
            raise reply.error
        return reply.value

    def _receive(self, command: object) -> None:
        if not isinstance(command, _SupervisorCommand):
            raise TypeError("unsupported forwarding state command")
        try:
            value = self._apply(command)
            error = None
        except BaseException as caught:
            value = None
            error = caught
        with self._guard:
            reply = self._pending.pop(command.operation_id, None)
            if reply is not None:
                reply.value = value
                reply.error = error
                reply.ready.set()

    def _apply(self, command: _SupervisorCommand) -> object | None:
        action = command.action
        if action is _SupervisorAction.SNAPSHOT:
            if self._closed:
                state = (
                    "failed" if len(self._failures) > self._policy.max_restarts
                    else "off"
                )
            elif self._backend is not None:
                state = "degraded" if self._consecutive else "running"
            elif not self._first_happened:
                state = "off"
            else:
                state = (
                    "failed" if len(self._failures) > self._policy.max_restarts
                    else "restarting"
                )
            return _SupervisorSnapshot(
                state, len(self._failures), self._backend,
                self._generation, self._closed,
            )
        if action is _SupervisorAction.BEGIN_START:
            if self._first_happened or self._closed:
                return False
            self._first_happened = True
            return True
        if action is _SupervisorAction.BEGIN_CLOSE:
            if self._closed:
                return None
            self._closed = True
            backend, self._backend = self._backend, None
            return backend
        if action is _SupervisorAction.OBSERVE_EXIT:
            if self._closed:
                return ("closed", None)
            backend = self._installed.get(command.token)
            if backend is None:
                self._early_exits[command.token] = command.exit_status or 0
                return ("preinstall", None)
            if self._backend is not backend:
                return ("stale", None)
            self._backend = None
            self._consecutive = 0
            return ("installed", backend)
        if action is _SupervisorAction.STATUS_FAILURE:
            if self._closed or self._backend is None:
                return (0, None)
            self._consecutive += 1
            count = self._consecutive
            backend = self._backend if count > self._policy.max_restarts else None
            if backend is not None:
                self._backend = None
                self._consecutive = 0
            return (count, backend)
        if action is _SupervisorAction.STATUS_SUCCESS:
            if self._closed or not self._consecutive:
                return False
            self._consecutive = 0
            return True
        if action is _SupervisorAction.RELAUNCH_DUE:
            return bool(
                not self._closed and self._backend is None
                and command.expected_generation == self._generation
            )
        if action is _SupervisorAction.INSTALL:
            if (
                self._closed
                or command.expected_generation != self._generation
                or command.token in self._early_exits
            ):
                return (False, len(self._failures))
            assert command.backend is not None
            self._backend = command.backend
            self._installed[command.token] = command.backend
            self._generation += 1
            return (True, len(self._failures))
        if action is _SupervisorAction.ACCOUNT_FAILURE:
            if self._closed:
                return None
            now = time.monotonic()
            self._failures.append(now)
            window = self._policy.restart_window
            while self._failures and now - self._failures[0] > window:
                self._failures.popleft()
            count = len(self._failures)
            if count > self._policy.max_restarts:
                return (count, None, self._generation)
            delay = self._policy.delay(count, self._jitter_source())
            return (count, delay, self._generation)
        raise TypeError("unsupported forwarding state action")

    def close(self, timeout: float = 5.0) -> bool:
        return self._runtime.stop(self._handle, timeout)


def _environment_controller(
    environ: Mapping[str, str], on_exit: Callable[[int], None]
) -> ForwardingSidecarController:
    return ForwardingSidecarController.from_environment(environ, on_exit=on_exit)


class ForwardingSidecarSupervisor:
    """Bounded-relaunch owner for the environment-configured sidecar process.

    A sidecar that dies once used to mean forwarding stayed down until the
    daemon restarted. This owner relaunches it under a budget that is not
    invented here: attempt count, window, and backoff curve are
    ``actor_runtime``'s ``PROCESS_LIFECYCLE`` ``SupervisionPolicy`` -- the
    registered budget for child-process restart decisions (supervision
    consolidation standard, 2026-08-29, §1.4). Failure accounting mirrors the
    guardian's: failure timestamps in a sliding window, and more failures than
    ``max_restarts`` inside it is terminal.

    The *first* start is synchronous (Zenoh fixes its connect set when the
    session opens, so the initial attempt belongs on the startup path); every
    relaunch is scheduled on the shared ``GenerationScheduler`` and therefore
    never blocks a caller.
    """

    def __init__(
        self,
        environ: Mapping[str, str],
        *,
        event_log: Callable[..., None],
        policy: SupervisionPolicy | None = None,
        controller_factory: Callable[
            [Mapping[str, str], Callable[[int], None]], Any
        ] = _environment_controller,
        scheduler: GenerationScheduler | None = None,
        jitter_source: Callable[[], float] = random.random,
        own_io: bool = False,
        desired_exposures: Callable[[], Sequence[Mapping[str, object]]] | None = None,
        directory: Callable[[], Mapping[str, str]] | None = None,
    ) -> None:
        self._environ = environ
        self._event_log = event_log
        self._policy: SupervisionPolicy = (
            DEFAULT_POLICIES[PROCESS_LIFECYCLE] if policy is None else policy
        )
        self._controller_factory = controller_factory
        self._scheduler = scheduler if scheduler is not None else GenerationScheduler()
        self._jitter_source = jitter_source
        self._own_io = own_io
        self._state_owner = (
            ForwardingSupervisorState(self._policy, self._jitter_source)
            if own_io else None
        )
        self._state_owner_closed = False
        self._closing_backend: ForwardingEndpoints | None = None
        self._desired_exposures = desired_exposures or (lambda: ())
        self._directory = directory
        self._lock = threading.Lock()
        self._endpoints: ForwardingEndpoints | None = None
        self._failures: deque[float] = deque()
        self._consecutive = 0
        self._generation = 0
        self._first_happened = False
        self._closed = False

    # -- read side ---------------------------------------------------------

    @property
    def state(self) -> str:
        """off / running / degraded / restarting / failed; visible in status/ps.

        ``degraded`` is the wedged child: the process answers nobody on its
        status channel but has not exited (the Go sidecar's
        ``CONTROL_UNREACHABLE`` / "cannot read node status" shape). A
        degraded child is not ``running`` -- reporting it as such was the
        "broken sidecar looks healthy" blind spot.
        """

        if self._state_owner is not None:
            snapshot = self._state_owner.call(_SupervisorAction.SNAPSHOT)
            assert isinstance(snapshot, _SupervisorSnapshot)
            return snapshot.state
        with self._lock:
            if self._closed:
                return (
                    "failed"
                    if len(self._failures) > self._policy.max_restarts
                    else "off"
                )
            if self._endpoints is not None:
                return "degraded" if self._consecutive else "running"
            if not self._first_happened:
                return "off"
            return (
                "failed"
                if len(self._failures) > self._policy.max_restarts
                else "restarting"
            )

    @property
    def failures(self) -> int:
        """Failures still inside the window; the budget's running count.

        Named for what it counts: the pre-review name ``relaunches`` claimed
        restarts, but the budget books *failures* (three failures with
        ``max_restarts=2`` means two relaunches and one terminal verdict).
        """

        if self._state_owner is not None:
            snapshot = self._state_owner.call(_SupervisorAction.SNAPSHOT)
            assert isinstance(snapshot, _SupervisorSnapshot)
            return snapshot.failures
        with self._lock:
            return len(self._failures)

    @property
    def current_pid(self) -> int | None:
        if self._state_owner is not None:
            snapshot = self._state_owner.call(_SupervisorAction.SNAPSHOT)
            assert isinstance(snapshot, _SupervisorSnapshot)
            backend = snapshot.backend
        else:
            with self._lock:
                backend = self._endpoints
        if backend is None:
            return None
        pid = getattr(backend.controller, "pid", None)
        return pid if isinstance(pid, int) else None

    def endpoints(self) -> ForwardingEndpoints | None:
        """The live endpoint backend, or None while down/restarting/failed."""

        if self._state_owner is not None:
            snapshot = self._state_owner.call(_SupervisorAction.SNAPSHOT)
            assert isinstance(snapshot, _SupervisorSnapshot)
            return snapshot.backend
        with self._lock:
            return self._endpoints

    def expose(self, exposure: Mapping[str, object]) -> dict[str, object]:
        backend = self.endpoints()
        if backend is None:
            raise ForwardingSidecarError("forwarding sidecar is not running")
        return backend.controller.expose(exposure)

    def unexpose(self, port: int) -> None:
        backend = self.endpoints()
        if backend is None:
            raise ForwardingSidecarError("forwarding sidecar is not running")
        backend.controller.unexpose(port)

    def peer_key(self, address: str) -> dict[str, object]:
        backend = self.endpoints()
        if backend is None:
            raise ForwardingSidecarError("forwarding sidecar is not running")
        return backend.controller.peer_key(address)

    def allow(self, keys: Sequence[str], allow_any: bool) -> dict[str, object]:
        backend = self.endpoints()
        if backend is None:
            raise ForwardingSidecarError("forwarding sidecar is not running")
        return backend.controller.allow(keys, allow_any)

    def map_peer(self, peer: str, address: str) -> int:
        # Read through endpoints(), never ``self._endpoints`` directly:
        # with own_io=True the backend lives in the state owner and the
        # plain attribute stays None on a perfectly healthy sidecar.
        backend = self.endpoints()
        if backend is None:
            raise ForwardingSidecarError("forwarding sidecar is not running")
        return backend.controller.map_peer(peer, address)

    def unmap_peer(self, peer: str) -> None:
        backend = self.endpoints()
        if backend is None:
            raise ForwardingSidecarError("forwarding sidecar is not running")
        backend.controller.unmap_peer(peer)

    # -- write side --------------------------------------------------------

    def ensure_started(self) -> None:
        """Perform the first start attempt, synchronously, exactly once."""

        if self._state_owner is not None:
            if self._state_owner.call(_SupervisorAction.BEGIN_START) is True:
                self._spawn(expected_generation=0)
            return
        with self._lock:
            if self._first_happened or self._closed:
                return
            self._first_happened = True
        self._spawn(expected_generation=0)

    def close(self) -> None:
        """Stop owning the child; no relaunch may fire after this."""

        if self._state_owner is not None:
            if self._state_owner_closed:
                return
            backend = self._closing_backend or self._state_owner.call(
                _SupervisorAction.BEGIN_CLOSE
            )
            self._scheduler.cancel(_FORWARDING_LAUNCH_KEY)
            if isinstance(backend, ForwardingEndpoints):
                self._closing_backend = backend
                backend.close()
                self._closing_backend = None
            if not self._state_owner.close(5.0):
                raise TimeoutError("forwarding supervisor state did not drain")
            self._state_owner_closed = True
            return
        with self._lock:
            if self._closed:
                return
            self._closed = True
            backend, self._endpoints = self._endpoints, None
        self._scheduler.cancel(_FORWARDING_LAUNCH_KEY)
        if backend is not None:
            backend.close()

    # -- internals ---------------------------------------------------------

    def _note_exit(self, code: int, cell: dict[str, Any] | str) -> None:
        """Monitor-thread callback: this cell's child died.

        The callback carries the identity of the incarnation it belongs to.
        Without it, an exit that lands in the window between the controller
        handshake and the supervisor installing the backend used to find no
        backend, schedule a relaunch, and then watch ``_spawn`` install the
        already-dead child as ``running`` with a stale generation the timer
        would never match (review finding C).
        """

        if self._state_owner is not None:
            if self._state_owner_closed:
                return
            assert isinstance(cell, str)
            verdict = self._state_owner.call(
                _SupervisorAction.OBSERVE_EXIT,
                token=cell, exit_status=code,
            )
            assert isinstance(verdict, tuple)
            kind, backend = verdict
            if kind == "closed" or kind == "stale":
                return
            if kind == "preinstall":
                self._event_log("error", "zenoh.forwarding.exited", exitStatus=code)
                self._account_for_failure(reason="exited", exit_status=code)
                return
            assert isinstance(backend, ForwardingEndpoints)
            close_error: str | None = None
            try:
                backend.close()
            except Exception as error:
                close_error = type(error).__name__
            fields: dict[str, Any] = {"exitStatus": code}
            if close_error is not None:
                fields["closeError"] = close_error
            self._event_log(
                "warn" if close_error else "error", "zenoh.forwarding.exited", **fields
            )
            self._account_for_failure(reason="exited", exit_status=code)
            return
        assert isinstance(cell, dict)
        with self._lock:
            if self._closed:
                return
            cell["exit"] = code
            backend = cell.get("backend")
            installed = backend is not None and self._endpoints is backend
            if installed:
                self._endpoints = None
                self._consecutive = 0
        if backend is None:
            # Pre-install death: the pending ``_spawn`` sees ``cell["exit"]``
            # and discards the child; the exit is reported and booked like
            # any other loss so a relaunch still happens.
            self._event_log("error", "zenoh.forwarding.exited", exitStatus=code)
            self._account_for_failure(reason="exited", exit_status=code)
            return
        if not installed:
            # A stale incarnation's late monitor: this child was already
            # replaced; nothing here owns it any more.
            return
        # The process is already gone; close() only reaps its pipes, and its
        # protocol-error paths are swallowed by design.
        close_error: str | None = None
        try:
            backend.close()
        except Exception as error:  # noqa: BLE001 - exit bookkeeping must not raise
            close_error = type(error).__name__
        fields: dict[str, Any] = {"exitStatus": code}
        if close_error is not None:
            fields["closeError"] = close_error
        self._event_log(
            "warn" if close_error else "error", "zenoh.forwarding.exited", **fields
        )
        self._account_for_failure(reason="exited", exit_status=code)

    def _note_status_failure(self, detail: str) -> None:
        """The listing hook: the child lives but its status channel is broken.

        One event per state change, never one per tick (review finding B):
        the first consecutive failure is announced exactly once, silence
        follows while the child stays wedged, and a wedged child that stays
        wedged is closed and accounted against the same registered budget as
        any other loss.  The wedged-confirmation threshold is derived from
        the policy (``max_restarts + 1`` consecutive failures), not a new
        budget.
        """

        if self._state_owner is not None:
            if self._state_owner_closed:
                return
            verdict = self._state_owner.call(_SupervisorAction.STATUS_FAILURE)
            assert isinstance(verdict, tuple)
            count, backend = verdict
            if count == 1:
                self._event_log(
                    "error", "zenoh.forwarding.failed",
                    reason="status-unreachable", failures=1, detail=detail[:500],
                )
            if backend is not None:
                try:
                    backend.close()
                except Exception:
                    pass
                self._account_for_failure(
                    reason="status-unreachable", exit_status=None
                )
            return
        with self._lock:
            if self._closed or self._endpoints is None:
                return
            self._consecutive += 1
            count = self._consecutive
            wedged = count > self._policy.max_restarts
            backend = self._endpoints if wedged else None
            if wedged:
                self._endpoints = None
                self._consecutive = 0
        if count == 1:
            self._event_log(
                "error",
                "zenoh.forwarding.failed",
                reason="status-unreachable",
                failures=1,
                detail=detail[:500],
            )
        if backend is None:
            return
        try:
            backend.close()
        except Exception:  # noqa: BLE001 - degrade bookkeeping must not raise
            pass
        self._account_for_failure(reason="status-unreachable", exit_status=None)

    def _note_status_success(self) -> None:
        """The listing hook: the status channel answered again."""

        if self._state_owner is not None:
            if self._state_owner_closed:
                return
            if self._state_owner.call(_SupervisorAction.STATUS_SUCCESS) is True:
                self._event_log(
                    "info", "zenoh.forwarding.recovered",
                    detail="forwarding sidecar status channel recovered",
                )
            return
        with self._lock:
            if self._closed or not self._consecutive:
                return
            self._consecutive = 0
        self._event_log(
            "info",
            "zenoh.forwarding.recovered",
            detail="forwarding sidecar status channel recovered",
        )

    def _scheduled_relaunch(self, expected_generation: int) -> None:
        if self._state_owner is not None:
            if self._state_owner_closed:
                return
            if self._state_owner.call(
                _SupervisorAction.RELAUNCH_DUE,
                expected_generation=expected_generation,
            ) is True:
                self._spawn(expected_generation=expected_generation)
            return
        with self._lock:
            stale = (
                self._closed
                or self._endpoints is not None
                or expected_generation != self._generation
            )
        if not stale:
            self._spawn(expected_generation=expected_generation)

    def _spawn(self, *, expected_generation: int) -> None:
        cell: dict[str, Any] = {"backend": None, "exit": None}
        token = uuid.uuid4().hex

        def _exit(code: int) -> None:
            self._note_exit(code, token if self._state_owner is not None else cell)

        controller: Any | None = None
        try:
            controller = self._controller_factory(self._environ, _exit)
            for exposure in self._desired_exposures():
                # A single exposure the sidecar refuses is logged and skipped;
                # it must not stop peer forwarding from starting.
                try:
                    controller.expose(exposure)
                except ForwardingSidecarError as error:
                    self._event_log(
                        "error",
                        "network.exposure.rejected",
                        port=exposure.get("port"),
                        detail=str(error)[:500],
                    )
        except Exception as error:  # noqa: BLE001 - visible no-fallback path
            if controller is not None:
                try:
                    controller.close()
                except Exception:  # noqa: BLE001 - preserve the startup cause
                    pass
            self._event_log(
                "error",
                "zenoh.forwarding.start_failed",
                errorType=type(error).__name__,
                detail=str(error)[:500],
            )
            self._account_for_failure(reason="start-failed", exit_status=None)
            return
        if self._own_io:
            controller = ForwardingControllerAuthority(controller)
        backend = ForwardingEndpoints(
            controller,
            on_failure=self._note_status_failure,
            on_success=self._note_status_success,
            directory=self._directory,
        )
        if self._state_owner is not None:
            installed = self._state_owner.call(
                _SupervisorAction.INSTALL,
                token=token, backend=backend,
                expected_generation=expected_generation,
            )
            assert isinstance(installed, tuple)
            accepted, count = installed
            if not accepted:
                backend.close()
                return
            if count:
                self._event_log(
                    "info", "zenoh.forwarding.started", failures=count,
                    detail="forwarding sidecar recovered after failure",
                )
            return
        with self._lock:
            stale = (
                self._closed
                or expected_generation != self._generation
                or cell["exit"] is not None
            )
            if not stale:
                self._endpoints = backend
                cell["backend"] = backend
                self._generation += 1
                count = len(self._failures)
        if stale:
            # Shutdown, a fresher incarnation, or a child that died before
            # installation. Closing pipes may block; never hold state lock.
            backend.close()
            return
        if count:
            self._event_log(
                "info",
                "zenoh.forwarding.started",
                failures=count,
                detail="forwarding sidecar recovered after failure",
            )

    def _account_for_failure(self, *, reason: str, exit_status: int | None) -> None:
        if self._state_owner is not None:
            decision = self._state_owner.call(_SupervisorAction.ACCOUNT_FAILURE)
            if decision is None:
                return
            assert isinstance(decision, tuple)
            count, delay, generation = decision
            fields: dict[str, Any] = {"reason": reason, "failures": count}
            if exit_status is not None:
                fields["exitStatus"] = exit_status
            if delay is None:
                fields["detail"] = (
                    "sidecar relaunch budget exhausted; forwarding stays down "
                    "until the daemon restarts"
                )
                self._event_log("error", "zenoh.forwarding.failed", **fields)
                return
            fields["delaySeconds"] = round(delay, 3)
            self._event_log("warn", "zenoh.forwarding.restarting", **fields)
            self._scheduler.schedule(
                _FORWARDING_LAUNCH_KEY, generation, delay,
                lambda _fired: self._scheduled_relaunch(generation),
            )
            return
        now = time.monotonic()
        with self._lock:
            if self._closed:
                return
            self._failures.append(now)
            window = self._policy.restart_window
            while self._failures and now - self._failures[0] > window:
                self._failures.popleft()
            count = len(self._failures)
            exhausted = count > self._policy.max_restarts
            fields: dict[str, Any] = {
                "reason": reason,
                "failures": count,
            }
            if exit_status is not None:
                fields["exitStatus"] = exit_status
            if exhausted:
                decision: tuple[str, float] | None = None
            else:
                delay = self._policy.delay(count, self._jitter_source())
                decision = (self._generation, delay)
                fields["delaySeconds"] = round(delay, 3)
        if decision is None:
            fields["detail"] = (
                "sidecar relaunch budget exhausted; forwarding stays down "
                "until the daemon restarts"
            )
            self._event_log("error", "zenoh.forwarding.failed", **fields)
            return
        generation, delay = decision
        self._event_log("warn", "zenoh.forwarding.restarting", **fields)
        self._scheduler.schedule(
            _FORWARDING_LAUNCH_KEY,
            generation,
            delay,
            lambda _fired: self._scheduled_relaunch(generation),
        )
