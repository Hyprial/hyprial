"""Daemon-owned process lifecycle for Lark long-connection adapters."""

from __future__ import annotations

import json
import os
import selectors
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Mapping, Sequence

from hyprial.home import child_state_environment
from hyprial.log import Logger
from hyprial.persistent_config import (
    LarkGatewayConfig,
    PersistentConfigStore,
)

from .api import HarnessDelivery
from .errors import AdapterStartError


#: Environment variable carrying the worker's dedicated ready-signal fd.
#:
#: The Lark SDK writes its own connection log to the worker's stdout, so the
#: ready signal must never share that stream: a dedicated inherited fd is a
#: channel the SDK (or any other library) cannot accidentally write to.
READY_FD_ENV_VAR = "HYPRIAL_LARK_READY_FD"

#: Bidirectional socketpair fd used only for daemon -> worker correlated
#: replies.  Unlike the telemetry pipe, every request receives one delivery
#: receipt after the native Lark send and reply-index commit complete.
CONTROL_FD_ENV_VAR = "HYPRIAL_LARK_CONTROL_FD"

#: Per-worker readiness lifecycle.  ``online`` is derived from this signal, not
#: from process liveness: a spawned process is *running* the instant it exists,
#: long before the Lark websocket handshake completes, so treating liveness as
#: readiness would report "online" for an adapter that never reached its
#: gateway.
STARTING = "starting"
ONLINE = "online"
ERROR = "error"


class _ReadyTimeout(Exception):
    """The worker stayed alive but never signalled within the deadline."""


class _ReadyEOF(Exception):
    """The ready channel closed (worker crashed) before any signal arrived."""


class _ReadyInvalid(Exception):
    """The worker wrote a non-JSON signal to the dedicated ready channel."""


def _wait_readable(ready_fd: int, timeout: float) -> bool:
    """True when ``ready_fd`` is readable within ``timeout`` seconds.

    Bare ``select.select()`` raises ``ValueError`` once the watched descriptor
    number reaches ``FD_SETSIZE`` (1024 on macOS) -- not a slowdown, a hard
    failure, and a long-lived daemon accumulates descriptors past that line
    (observed in the field: a readiness pipe landed on fd 1148).  The default
    selector uses kqueue/epoll and has no such ceiling.
    """

    if timeout <= 0:
        return False
    with selectors.DefaultSelector() as selector:
        selector.register(ready_fd, selectors.EVENT_READ)
        return bool(selector.select(timeout))


def _read_ready(ready_fd: int, startup_timeout: float) -> tuple[object, bytearray]:
    """Read one JSON ready signal from ``ready_fd``, bounded by a deadline.

    Pure reader: it never touches the worker process.  A worker that never
    signals raises :class:`_ReadyTimeout`, a closed channel raises
    :class:`_ReadyEOF`, and garbage raises :class:`_ReadyInvalid`.  The caller
    owns process termination and fd cleanup.
    """

    deadline = time.monotonic() + startup_timeout
    buffer = bytearray()
    while True:
        remaining = deadline - time.monotonic()
        readable: list[int] = []
        if remaining > 0:
            readable = [ready_fd] if _wait_readable(ready_fd, remaining) else []
        if not readable:
            raise _ReadyTimeout()
        chunk = os.read(ready_fd, 64 * 1024)
        if not chunk:
            raise _ReadyEOF()
        buffer.extend(chunk)
        while b"\n" in buffer:
            line, _, rest = buffer.partition(b"\n")
            buffer = bytearray(rest)
            if not line.strip():
                continue
            try:
                return json.loads(line), buffer
            except json.JSONDecodeError as error:
                raise _ReadyInvalid() from error


class LarkWorkerProcess:
    """A spawned Lark worker whose readiness is confirmed off the caller thread.

    Construction returns immediately; a dedicated daemon thread waits for the
    worker's ready signal (bounded by ``startup_timeout``) and resolves the
    readiness state to :data:`ONLINE` or :data:`ERROR`.  The daemon dispatcher
    never blocks on this wait — that is the whole point: a slow or unreachable
    adapter can no longer wedge the single-threaded IPC serve loop (and with it
    ``shutdown``).
    """

    def __init__(
        self,
        process: subprocess.Popen[str],
        *,
        name: str,
        ready_fd: int,
        startup_timeout: float,
        drainer: threading.Thread | None = None,
        control_socket: socket.socket | None = None,
        logger: Logger | None = None,
    ) -> None:
        self._process = process
        self._name = name
        self._ready_fd = ready_fd
        self._startup_timeout = startup_timeout
        self._drainer = drainer
        self._control_socket = control_socket
        self._logger = logger
        self._exit_logged = False
        self._expected_stop = False
        self._control_lock = threading.Lock()
        if self._drainer is None and process.stdout is not None:
            self._drainer = _drain(process.stdout, logger)
        self._lock = threading.Lock()
        self._readiness = STARTING
        self._error: str | None = None
        self._health: dict[str, object] = {}
        self._health_events: list[dict[str, object]] = []
        self._resolved = threading.Event()
        self._ready_thread = threading.Thread(
            target=self._await_ready,
            name=f"lark-ready-{name}",
            daemon=True,
        )
        if self._logger is not None:
            self._logger.info("adapter.started", pid=self._process.pid)
        self._ready_thread.start()

    @property
    def running(self) -> bool:
        return self._process.poll() is None

    @property
    def pid(self) -> int:
        return self._process.pid

    @property
    def readiness(self) -> str:
        with self._lock:
            return self._readiness

    @property
    def last_sdk_output(self) -> str | None:
        """The most recent ``lark.sdk.output`` line this worker drained.

        The SDK logs its handshake progress to stdout; the drainer logs every
        line to ``logs/lark-gateway.jsonl`` but nothing else remembered the
        last one.  G2's timeout event and G3's starting status quote it so an
        operator sees the worker's own last words without grepping the log.
        """

        drain = self._drainer
        capture = getattr(drain, "last_line", None)
        return capture() if callable(capture) else None

    @property
    def error(self) -> str | None:
        with self._lock:
            return self._error

    @property
    def health(self) -> dict[str, object]:
        with self._lock:
            return dict(self._health)

    def drain_health_events(self) -> tuple[dict[str, object], ...]:
        with self._lock:
            events = tuple(self._health_events)
            self._health_events.clear()
        return events

    def wait_ready(self, timeout: float | None = None) -> str:
        """Block up to ``timeout`` for a terminal readiness, return the state.

        Returns :data:`STARTING` if the worker has not resolved yet — callers
        translate a bounded wait into "still coming up" without wedging.
        """

        self._resolved.wait(timeout)
        return self.readiness

    def _await_ready(self) -> None:
        # Whatever happens, this thread must resolve exactly once and close its
        # fd — otherwise wait_ready() (and any stop() joining it) would hang.
        try:
            self._run_ready()
        except (NameError, ImportError) as error:
            # Programming/import defects must stay visible to the runtime,
            # but the readiness state still has to settle and the child must
            # be reaped before this background thread re-raises.
            self._fail(f"readiness check failed: {error}")
            raise
        except BaseException as error:  # defensive: never leave a caller waiting
            self._fail(f"readiness check failed: {error}")
        finally:
            try:
                os.close(self._ready_fd)
            except OSError:
                pass

    def _run_ready(self) -> None:
        try:
            message, pending = _read_ready(self._ready_fd, self._startup_timeout)
        except _ReadyTimeout:
            self._fail("did not become online")
            return
        except _ReadyEOF:
            self._fail("failed before becoming online")
            return
        except _ReadyInvalid:
            self._fail("emitted an invalid ready signal")
            return
        if self._process.poll() is not None or message != {
            "status": "online",
            "name": self._name,
        }:
            self._fail("failed before becoming online")
            return
        self._resolve(ONLINE)
        if self._logger is not None:
            self._logger.info("adapter.ready", pid=self._process.pid)
        self._read_health_stream(pending)
        self._log_exit()

    def _read_health_stream(self, buffer: bytearray) -> None:
        """Consume health telemetry for the lifetime of an online worker."""

        while True:
            while b"\n" in buffer:
                line, _, rest = buffer.partition(b"\n")
                buffer = bytearray(rest)
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    continue
                self._record_health(message)
            if not _wait_readable(self._ready_fd, 0.5):
                continue
            try:
                chunk = os.read(self._ready_fd, 64 * 1024)
            except OSError:
                return
            if not chunk:
                return
            buffer.extend(chunk)

    def _record_health(self, message: object) -> None:
        if not isinstance(message, dict):
            return
        if message.get("name") != self._name:
            return
        if message.get("status") == "warning":
            if message.get("event") != "lark.pin.query_failed":
                return
            allowed = {"errorType", "fallback", "cachedActor"}
            with self._lock:
                self._health_events.append(
                    {
                        "event": "lark.pin.query_failed",
                        "adapter": self._name,
                        **{key: message.get(key) for key in allowed if key in message},
                    }
                )
            return
        if message.get("status") != "health":
            return
        stream_health = message.get("streamHealth")
        if stream_health not in {"healthy", "stale", "checking"}:
            return
        allowed = {
            "streamHealth",
            "reason",
            "connectedAt",
            "lastEventAt",
            "lastTransportAt",
            "lastProbeAt",
        }
        health = {key: message.get(key) for key in allowed}
        with self._lock:
            previous = self._health.get("streamHealth")
            self._health = health
            if previous != stream_health:
                self._health_events.append(
                    {
                        "event": f"lark.inbound.{stream_health}",
                        "adapter": self._name,
                        **health,
                    }
                )

    def _fail(self, reason: str) -> None:
        self._terminate()
        self._resolve(ERROR, f"Lark adapter {self._name} {reason}")

    def _resolve(self, readiness: str, error: str | None = None) -> None:
        with self._lock:
            if self._resolved.is_set():
                return
            self._readiness = readiness
            self._error = error
        self._resolved.set()

    def _terminate(self) -> None:
        # Called from both the readiness thread and stop(); tolerate the process
        # already being gone (a TOCTOU between poll() and the signal) so neither
        # caller ever raises.
        try:
            if self._process.poll() is not None:
                self._log_exit()
                return
            self._process.terminate()
        except (ProcessLookupError, OSError):
            return
        try:
            self._process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            try:
                self._process.kill()
            except (ProcessLookupError, OSError):
                return
            try:
                self._process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass
        self._log_exit()

    def _log_exit(self) -> None:
        logger = getattr(self, "_logger", None)
        if logger is None:
            return
        with self._lock:
            if getattr(self, "_exit_logged", False):
                return
            returncode = self._process.poll()
            if returncode is None:
                return
            self._exit_logged = True
            expected = getattr(self, "_expected_stop", False)
        event = "adapter.stopped" if expected else "adapter.exited"
        logger.log(
            "info" if expected else "error",
            event,
            pid=self._process.pid,
            returnCode=returncode,
        )

    def stop(self) -> None:
        # Terminating the child closes the inherited ready-write fd; the
        # readiness thread's select then wakes on EOF, resolves, and closes the
        # read fd.  Wait briefly so the thread and fd are reclaimed promptly.
        self._expected_stop = True
        self._terminate()
        self._resolved.wait(2.0)
        self._ready_thread.join(timeout=2.0)
        # The drainer owns the child's stdout pipe; joining it makes the
        # pipe's release deterministic instead of whenever the buffered
        # output happens to reach EOF (card #77).
        if self._drainer is not None:
            self._drainer.join(timeout=2.0)
        if self._control_socket is not None:
            try:
                self._control_socket.close()
            except OSError:
                pass
            self._control_socket = None

    def deliver_reply(self, delivery: HarnessDelivery) -> bool:
        """Ask the worker to perform one native correlated reply.

        ``True`` is a delivery receipt, not merely IPC acceptance: the worker
        sends it only after ``LarkAdapter.handle_delivery`` has sent the native
        reply and persisted the durable reply route.  Any broken/negative
        exchange returns ``False`` so the daemon outbox retains the item.
        """

        frame = {
            "deliveryId": delivery.delivery_id,
            "messageId": delivery.message_id,
            "replyTo": delivery.reply_to,
            "fromActor": {
                "actorId": delivery.from_actor.actor_id,
                "actorKey": delivery.from_actor.actor_key,
                "displayName": delivery.from_actor.display_name,
            },
            "text": delivery.text,
        }
        return self._control_delivery(frame, delivery.delivery_id)

    def deliver_alarm(
        self,
        correlation_id: str,
        text: str,
        *,
        idempotency_key: str,
    ) -> bool:
        """Ask the worker for one native alarm reply, with no outbox receipt."""

        delivery_id = f"alarm:{correlation_id}"
        return self._control_delivery(
            {
                "kind": "alarm",
                "deliveryId": delivery_id,
                "correlationId": correlation_id,
                "text": text,
                "idempotencyKey": idempotency_key,
            },
            delivery_id,
        )

    def _control_delivery(
        self, frame: dict[str, object], delivery_id: str
    ) -> bool:
        control = self._control_socket
        if control is None or not self.running or self.readiness != ONLINE:
            return False
        try:
            encoded = json.dumps(frame, separators=(",", ":")).encode() + b"\n"
            with self._control_lock:
                control.settimeout(15.0)
                control.sendall(encoded)
                buffer = bytearray()
                while b"\n" not in buffer:
                    chunk = control.recv(64 * 1024)
                    if not chunk:
                        raise ConnectionError("Lark reply worker disconnected")
                    buffer.extend(chunk)
                response = json.loads(buffer.partition(b"\n")[0])
        except (
            OSError,
            RuntimeError,
            json.JSONDecodeError,
            UnicodeError,
        ):
            self._break_control_channel(control)
            return False
        if (
            not isinstance(response, dict)
            or response.get("deliveryId") != delivery_id
        ):
            # A timeout or malformed worker must never let a delayed receipt
            # settle a different outbox row.  This socket is no longer safe to
            # reuse; kill the worker so normal supervisor reconcile replaces
            # both endpoints before the row retries.
            self._break_control_channel(control)
            return False
        native_message_id = (
            response.get("nativeMessageId")
        )
        accepted = (
            response.get("ok") is True
            and isinstance(native_message_id, str)
            and bool(native_message_id)
        )
        if not accepted and self._logger is not None:
            # The worker answered but refused the native send (non-zero
            # platform code, or a reply that returned no message id).  Keep
            # the reason on the adapter log keyed by delivery id so a silent
            # harness_reply becomes diagnosable: the reply bridge already
            # carried ``code``/``msg`` in the error string, and dropping it
            # here is what made those failures invisible.
            try:
                self._logger.log(
                    "warn",
                    "lark.reply_bridge.rejected",
                    deliveryId=delivery_id,
                    reason=response.get("error") or "no native message id",
                )
            except (NameError, ImportError):
                raise
            except OSError:
                pass
        return accepted

    def _break_control_channel(self, control: socket.socket) -> None:
        if self._control_socket is control:
            self._control_socket = None
        try:
            control.close()
        except OSError:
            pass
        self._terminate()


def _drain(stream: object, logger: Logger | None = None) -> _StdoutDrain:
    drain = _StdoutDrain(stream, logger)
    drain.start()
    return drain


class _StdoutDrain:
    """Consume the worker's stdout: log every ``lark.sdk.output`` line.

    Remembers the most recent line (the G2/G3 "last sdk output" reading) and
    closes the stream at EOF so the parent never leaks the child's pipe fd
    for the lifetime of the Popen object (card #77).
    """

    def __init__(self, stream: object, logger: Logger | None = None) -> None:
        self._stream = stream
        self._logger = logger
        self._lock = threading.Lock()
        self._last_line: str | None = None
        self._thread = threading.Thread(
            target=self._consume,
            daemon=True,
            name="lark-worker-output",
        )

    def start(self) -> None:
        self._thread.start()

    def last_line(self) -> str | None:
        with self._lock:
            return self._last_line

    def join(self, timeout: float | None = None) -> None:
        self._thread.join(timeout)

    def is_alive(self) -> bool:
        return self._thread.is_alive()

    def _consume(self) -> None:
        try:
            for line in self._stream:
                text = str(line).rstrip()
                with self._lock:
                    self._last_line = text
                if self._logger is not None:
                    self._logger.debug("lark.sdk.output", message=text)
        finally:
            # EOF (child exit) or a broken stream ends the loop either way;
            # without this the parent keeps the child's stdout pipe fd open
            # for the lifetime of the Popen object -- one leaked descriptor
            # per worker start/stop cycle in a long-lived daemon (card #77).
            try:
                self._stream.close()  # type: ignore[attr-defined]
            except Exception:
                pass


class LarkWorkerLauncher:
    def __init__(
        self,
        *,
        hyprial_home: Path,
        state_dir: Path,
        socket_path: Path,
        config_store: PersistentConfigStore,
        command: Sequence[str] | None = None,
        env: Mapping[str, str] | None = None,
        startup_timeout: float = 20.0,
    ) -> None:
        self.hyprial_home = Path(hyprial_home)
        self.state_dir = Path(state_dir)
        self.socket_path = Path(socket_path)
        self.config_store = config_store
        self.command = tuple(
            command or (sys.executable, "-m", "hyprial.adapters.lark.worker")
        )
        self.env = dict(env or os.environ)
        self.startup_timeout = startup_timeout

    @classmethod
    def from_environment(
        cls, *, hyprial_home: Path, state_dir: Path, socket_path: Path,
        config_store: PersistentConfigStore,
    ) -> LarkWorkerLauncher:
        raw = os.environ.get("HYPRIAL_LARK_WORKER_COMMAND")
        command = None
        if raw:
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError as error:
                raise AdapterStartError(
                    "HYPRIAL_LARK_WORKER_COMMAND must be a JSON array"
                ) from error
            if not isinstance(parsed, list) or not parsed or any(
                not isinstance(item, str) or not item for item in parsed
            ):
                raise AdapterStartError(
                    "HYPRIAL_LARK_WORKER_COMMAND must be a non-empty string array"
                )
            command = parsed
        return cls(
            hyprial_home=hyprial_home,
            state_dir=state_dir,
            socket_path=socket_path,
            config_store=config_store,
            command=command,
        )

    def spawn(self, gateway: LarkGatewayConfig) -> LarkWorkerProcess:
        """Launch a worker and return immediately, before it is online.

        The readiness handshake runs on the returned handle's own thread, so
        the caller (the daemon dispatcher) is never blocked by a worker that is
        slow to connect or can never reach its gateway.
        """

        # Validate the secret here, but let the worker read it from its protected
        # file. It never appears in argv, environment, IPC, or status output.
        self.config_store.lark_app_secret(gateway.credential_ref)
        environment = dict(self.env)
        environment.update(
            {
                **child_state_environment(self.hyprial_home, self.state_dir),
                "HARNESS_SOCKET_PATH": str(self.socket_path),
            }
        )
        # The ready signal travels on a dedicated inherited fd so it can never
        # be confused with SDK logging on stdout/stderr.
        ready_read, ready_write = os.pipe()
        control_parent, control_child = socket.socketpair()
        environment[READY_FD_ENV_VAR] = str(ready_write)
        environment[CONTROL_FD_ENV_VAR] = str(control_child.fileno())
        try:
            process = subprocess.Popen(
                [*self.command, gateway.name],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                bufsize=1,
                env=environment,
                close_fds=True,
                pass_fds=(ready_write, control_child.fileno()),
            )
        except BaseException:
            os.close(ready_read)
            os.close(ready_write)
            control_parent.close()
            control_child.close()
            raise
        os.close(ready_write)
        control_child.close()
        # Drain stdout from the very start: the Lark SDK logs there, and a
        # full pipe would otherwise block the worker before it can signal.
        assert process.stdout is not None
        logger = Logger.adapter(self.state_dir, name=gateway.name)
        drainer = _drain(process.stdout, logger)
        return LarkWorkerProcess(
            process,
            name=gateway.name,
            ready_fd=ready_read,
            startup_timeout=self.startup_timeout,
            drainer=drainer,
            control_socket=control_parent,
            logger=logger,
        )

    def start(self, gateway: LarkGatewayConfig) -> LarkWorkerProcess:
        """Spawn a worker and block until it is online (or fails).

        Retained as the blocking primitive for callers that want a confirmed
        adapter — notably the launcher's own unit tests.  The daemon supervisor
        uses :meth:`spawn` plus a bounded confirmation instead so it never
        blocks the dispatcher.
        """

        process = self.spawn(gateway)
        readiness = process.wait_ready(self.startup_timeout + 5.0)
        if readiness != ONLINE:
            error = (
                process.error
                or f"Lark adapter {gateway.name} failed before becoming online"
            )
            # No handle reaches the caller on failure. Reclaim its control
            # socket and join the ready/stdout readers before raising; a held
            # traceback must not keep a failed worker's descriptors alive.
            process.stop()
            raise AdapterStartError(error)
        return process


# Adapter lifecycle ownership lives in the actor domain.  Imports stay at the
# bottom so the process/launcher primitives above are fully initialized before
from .actor_domain import (  # noqa: E402 - process primitives initialize first
    AdapterRestoreSummary,
    AdapterRuntime,
)

__all__ = [
    "AdapterRestoreSummary",
    "AdapterRuntime",
    "AdapterStartError",
    "CONTROL_FD_ENV_VAR",
    "ERROR",
    "LarkWorkerLauncher",
    "LarkWorkerProcess",
    "ONLINE",
    "READY_FD_ENV_VAR",
    "STARTING",
]
