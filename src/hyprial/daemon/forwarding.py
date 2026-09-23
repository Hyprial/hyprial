"""Daemon-owned protocol-v2 forwarding sidecar lifecycle."""

from __future__ import annotations

import json
import os
import queue
import random
import subprocess
import threading
import time
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from typing import Any, Self

from hyprial.actor_runtime.policies import (
    DEFAULT_POLICIES,
    PROCESS_LIFECYCLE,
    SupervisionPolicy,
)
from hyprial.actor_runtime.scheduler import GenerationScheduler
from hyprial.contracts.forwarding import FORWARDING_COMMAND_ENV, FORWARDING_UP_ENV
from .discovery import ForwardingEndpoints

FORWARD_PROTOCOL_VERSION = 2
_DEFAULT_TIMEOUT = 10.0
#: How many stderr lines of the sidecar to retain for diagnostics.  Bounded on
#: purpose: this is a failure explanation, not a log sink.
_STDERR_TAIL_LINES = 12
_PROXY_KEYS = frozenset(
    {
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "NO_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
        "no_proxy",
    }
)


class ForwardingSidecarError(RuntimeError):
    pass


def _child_environment(environ: Mapping[str, str]) -> dict[str, str]:
    return {
        key: value
        for key, value in environ.items()
        if not key.startswith("TS_") and key not in _PROXY_KEYS
    }


class ForwardingSidecarController:
    """Own one candidate sidecar process and its closed v2 JSON-lines wire."""

    def __init__(
        self,
        command: Sequence[str],
        up: Mapping[str, object],
        *,
        environ: Mapping[str, str] | None = None,
        timeout: float = _DEFAULT_TIMEOUT,
        on_exit: Callable[[int], None] | None = None,
    ) -> None:
        if not command:
            raise ValueError("forwarding sidecar command must not be empty")
        self._up = dict(up)
        if self._up.get("v") != FORWARD_PROTOCOL_VERSION or self._up.get("op") != "up":
            raise ValueError("forwarding sidecar up request must be protocol v2")
        if self._up.get("authKey") is not None or self._up.get("resume") is not True:
            raise ValueError("daemon forwarding must resume state without an auth key")
        self._timeout = timeout
        self._on_exit = on_exit
        self._lock = threading.Lock()
        self._closing = False
        self._startup_complete = False
        self._events: queue.Queue[bytes | None] = queue.Queue()
        self._stderr_tail: deque[str] = deque(maxlen=_STDERR_TAIL_LINES)
        self.child_environment = _child_environment(
            os.environ if environ is None else environ
        )
        self._process = subprocess.Popen(
            list(command),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            close_fds=True,
            env=self.child_environment,
        )
        self._stdout_thread = threading.Thread(
            target=self._read_stdout, daemon=True, name="forward-sidecar-stdout"
        )
        self._stderr_thread = threading.Thread(
            target=self._drain_stderr, daemon=True, name="forward-sidecar-stderr"
        )
        self._stdout_thread.start()
        self._stderr_thread.start()
        try:
            self._start()
        except BaseException:
            self._terminate_owned_process()
            raise
        self._startup_complete = True
        self._monitor_thread = threading.Thread(
            target=self._monitor_exit, daemon=True, name="forward-sidecar-monitor"
        )
        self._monitor_thread.start()

    @classmethod
    def from_environment(
        cls,
        environ: Mapping[str, str],
        *,
        on_exit: Callable[[int], None] | None = None,
    ) -> Self:
        try:
            command = json.loads(environ[FORWARDING_COMMAND_ENV])
            up = json.loads(environ[FORWARDING_UP_ENV])
        except (KeyError, TypeError, ValueError) as error:
            raise ForwardingSidecarError(
                "forwarding sidecar environment is incomplete or invalid"
            ) from error
        if (
            not isinstance(command, list)
            or not command
            or not all(isinstance(item, str) and item for item in command)
        ):
            raise ForwardingSidecarError(
                "forwarding sidecar command must be a non-empty JSON string array"
            )
        if not isinstance(up, dict):
            raise ForwardingSidecarError(
                "forwarding sidecar up request must be a JSON object"
            )
        return cls(command, up, environ=environ, on_exit=on_exit)

    @property
    def pid(self) -> int:
        return self._process.pid

    def _read_stdout(self) -> None:
        assert self._process.stdout is not None
        try:
            for line in self._process.stdout:
                self._events.put(line)
        finally:
            self._events.put(None)

    def _drain_stderr(self) -> None:
        # Keep the tail instead of discarding every chunk.  The sidecar's
        # stderr is the only place a failure explains itself, and the protocol
        # carries a single bounded code: draining to ``pass`` collapsed four
        # distinct causes ("missing self status", "missing Tailscale IP",
        # "missing user profile", "missing node public key") into one
        # indistinguishable message, and left nothing to read afterwards.
        assert self._process.stderr is not None
        try:
            for line in self._process.stderr:
                text = line.decode("utf-8", "replace").strip()
                if text:
                    self._stderr_tail.append(text)
        except OSError:
            pass

    @property
    def stderr_tail(self) -> tuple[str, ...]:
        """The last few stderr lines the sidecar produced, newest last."""

        return tuple(self._stderr_tail)

    def _status_failure(self, message: str) -> ForwardingSidecarError:
        """A status failure that carries whatever the sidecar said about it."""

        # Reads the tail defensively: this is a diagnostic on a failure path,
        # and a reader that raises AttributeError here would replace the
        # forwarding fault with a reporting fault -- the same trade
        # _mirror_startup_event_to_stderr and _log_lifecycle_operation make.
        # It is reachable uninitialized because tests construct the controller
        # through object.__new__ to stub _request (no process is spawned).
        tail = " | ".join(getattr(self, "_stderr_tail", ()))
        return ForwardingSidecarError(
            message if not tail else f"{message} (sidecar stderr: {tail})"
        )

    def _monitor_exit(self) -> None:
        code = self._process.wait()
        if self._startup_complete and not self._closing and self._on_exit is not None:
            self._on_exit(code)

    def _send(self, payload: Mapping[str, object]) -> None:
        if self._process.poll() is not None:
            raise ForwardingSidecarError(
                f"forwarding sidecar exited with status {self._process.returncode}"
            )
        try:
            assert self._process.stdin is not None
            self._process.stdin.write(
                (json.dumps(dict(payload), separators=(",", ":")) + "\n").encode()
            )
            self._process.stdin.flush()
        except (BrokenPipeError, OSError) as error:
            raise ForwardingSidecarError("forwarding sidecar stdin closed") from error

    def _next(self, *, deadline: float) -> dict[str, Any]:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ForwardingSidecarError("forwarding sidecar response timed out")
        try:
            raw = self._events.get(timeout=remaining)
        except queue.Empty as error:
            raise ForwardingSidecarError(
                "forwarding sidecar response timed out"
            ) from error
        if raw is None:
            code = self._process.poll()
            raise ForwardingSidecarError(
                f"forwarding sidecar closed stdout with status {code}"
            )
        try:
            event = json.loads(raw)
        except (UnicodeDecodeError, ValueError) as error:
            raise ForwardingSidecarError(
                "forwarding sidecar emitted invalid JSON"
            ) from error
        if not isinstance(event, dict) or event.get("v") != FORWARD_PROTOCOL_VERSION:
            raise ForwardingSidecarError("forwarding sidecar emitted a non-v2 event")
        if event.get("event") == "error":
            code = str(event.get("code") or "FORWARD_PROTOCOL")
            message = str(event.get("message") or "forwarding sidecar error")
            raise ForwardingSidecarError(f"{code}: {message}")
        return event

    def _start(self) -> None:
        deadline = time.monotonic() + self._timeout
        hello = self._next(deadline=deadline)
        if hello.get("event") != "hello" or not hello.get("sidecar"):
            raise ForwardingSidecarError("forwarding sidecar v2 hello mismatch")
        self._send(self._up)
        while True:
            event = self._next(deadline=deadline)
            if event.get("event") == "ready":
                return
            if event.get("event") != "state":
                raise ForwardingSidecarError(
                    f"unexpected forwarding startup event {event.get('event')!r}"
                )

    def _request(self, payload: Mapping[str, object], expected: str) -> dict[str, Any]:
        with self._lock:
            self._send(payload)
            deadline = time.monotonic() + self._timeout
            event = self._next(deadline=deadline)
            if event.get("event") != expected:
                raise ForwardingSidecarError(
                    f"expected forwarding event {expected!r}; got {event.get('event')!r}"
                )
            return event

    def status(self) -> tuple[tuple[str, ...], dict[str, int]]:
        event = self._request({"v": FORWARD_PROTOCOL_VERSION, "op": "status"}, "status")
        # An absent ``peers`` key means "no online peer yet" -- a normal early
        # answer, not a protocol violation.  The sidecar omits an empty list
        # (Go's ``omitempty``), and builds before tsnet-v0.1.5 never sent the
        # key on ``status`` at all.  Rejecting it made a healthy-but-empty mesh
        # fail every poll forever, which is what a switch-on saw: the daemon
        # reported ``zenoh.forwarding.failed`` on a loop while the control
        # plane was reachable and the node was joined.
        #
        # Only an ABSENT key is forgiven: an explicit ``null`` (or any other
        # non-list) is still a protocol violation, which is what
        # test_explicit_invalid_peers_still_rejected pins.
        raw_peers = event.get("peers")
        if "peers" not in event:
            raw_peers = []
        raw_mappings = event.get("mappings", [])
        if not isinstance(raw_peers, list) or not all(
            isinstance(peer, str) and peer for peer in raw_peers
        ):
            raise self._status_failure("forwarding status has invalid peers")
        if not isinstance(raw_mappings, list):
            raise ForwardingSidecarError("forwarding status has invalid mappings")
        mappings: dict[str, int] = {}
        for item in raw_mappings:
            if not isinstance(item, dict):
                raise ForwardingSidecarError("forwarding status has invalid mapping")
            peer, port = item.get("peer"), item.get("localPort")
            if not isinstance(peer, str) or not isinstance(port, int):
                raise ForwardingSidecarError("forwarding status has invalid mapping")
            mappings[peer] = port
        return tuple(raw_peers), mappings

    def map_peer(self, peer: str) -> int:
        event = self._request(
            {"v": FORWARD_PROTOCOL_VERSION, "op": "map-peer", "peer": peer},
            "peer-mapped",
        )
        if event.get("peer") != peer or not isinstance(event.get("localPort"), int):
            raise ForwardingSidecarError("peer-mapped event does not match request")
        return int(event["localPort"])

    def unmap_peer(self, peer: str) -> None:
        event = self._request(
            {"v": FORWARD_PROTOCOL_VERSION, "op": "unmap-peer", "peer": peer},
            "peer-unmapped",
        )
        if event.get("peer") != peer:
            raise ForwardingSidecarError("peer-unmapped event does not match request")

    def close(self) -> None:
        with self._lock:
            if self._closing:
                return
            self._closing = True
            if self._process.poll() is None:
                try:
                    self._send({"v": FORWARD_PROTOCOL_VERSION, "op": "down"})
                    deadline = time.monotonic() + self._timeout
                    event = self._next(deadline=deadline)
                    if event.get("event") != "exited":
                        raise ForwardingSidecarError(
                            "forwarding sidecar did not acknowledge down"
                        )
                except ForwardingSidecarError:
                    pass
            self._terminate_owned_process()

    def _terminate_owned_process(self) -> None:
        if self._process.poll() is None:
            self._process.terminate()
            try:
                self._process.wait(timeout=self._timeout)
            except subprocess.TimeoutExpired:
                self._process.kill()
                self._process.wait()
        for stream in (
            self._process.stdin,
            self._process.stdout,
            self._process.stderr,
        ):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass


#: Scheduler key for the one pending sidecar relaunch, if any.  A string
#: constant is a wire key, not a budget: the schedule delay itself always
#: comes from the supervision policy below.
_FORWARDING_LAUNCH_KEY = "forwarding-sidecar-relaunch"


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
    ) -> None:
        self._environ = environ
        self._event_log = event_log
        self._policy: SupervisionPolicy = (
            DEFAULT_POLICIES[PROCESS_LIFECYCLE] if policy is None else policy
        )
        self._controller_factory = controller_factory
        self._scheduler = scheduler if scheduler is not None else GenerationScheduler()
        self._jitter_source = jitter_source
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

        with self._lock:
            return len(self._failures)

    @property
    def current_pid(self) -> int | None:
        with self._lock:
            backend = self._endpoints
        if backend is None:
            return None
        pid = getattr(backend.controller, "pid", None)
        return pid if isinstance(pid, int) else None

    def endpoints(self) -> ForwardingEndpoints | None:
        """The live endpoint backend, or None while down/restarting/failed."""

        with self._lock:
            return self._endpoints

    # -- write side --------------------------------------------------------

    def ensure_started(self) -> None:
        """Perform the first start attempt, synchronously, exactly once."""

        with self._lock:
            if self._first_happened or self._closed:
                return
            self._first_happened = True
        self._spawn(expected_generation=0)

    def close(self) -> None:
        """Stop owning the child; no relaunch may fire after this."""

        with self._lock:
            if self._closed:
                return
            self._closed = True
            backend, self._endpoints = self._endpoints, None
        self._scheduler.cancel(_FORWARDING_LAUNCH_KEY)
        if backend is not None:
            backend.close()

    # -- internals ---------------------------------------------------------

    def _note_exit(self, code: int, cell: dict[str, Any]) -> None:
        """Monitor-thread callback: this cell's child died.

        The callback carries the identity of the incarnation it belongs to.
        Without it, an exit that lands in the window between the controller
        handshake and the supervisor installing the backend used to find no
        backend, schedule a relaunch, and then watch ``_spawn`` install the
        already-dead child as ``running`` with a stale generation the timer
        would never match (review finding C).
        """

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

        def _exit(code: int) -> None:
            self._note_exit(code, cell)

        try:
            controller = self._controller_factory(self._environ, _exit)
        except Exception as error:  # noqa: BLE001 - visible no-fallback path
            self._event_log(
                "error",
                "zenoh.forwarding.start_failed",
                errorType=type(error).__name__,
                detail=str(error)[:500],
            )
            self._account_for_failure(reason="start-failed", exit_status=None)
            return
        backend = ForwardingEndpoints(
            controller,
            on_failure=self._note_status_failure,
            on_success=self._note_status_success,
        )
        with self._lock:
            if (
                self._closed
                or expected_generation != self._generation
                or cell["exit"] is not None
            ):
                # Shutdown, a fresher incarnation, or a child that already
                # died in the pre-install window: own the child we just made
                # instead of installing it as running.
                backend.close()
                return
            self._endpoints = backend
            cell["backend"] = backend
            self._generation += 1
            count = len(self._failures)
        if count:
            self._event_log(
                "info",
                "zenoh.forwarding.started",
                failures=count,
                detail="forwarding sidecar recovered after failure",
            )

    def _account_for_failure(self, *, reason: str, exit_status: int | None) -> None:
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
