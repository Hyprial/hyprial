"""The Unix-socket IPC server: accept loop, client workers, response framing and timed handling."""

from __future__ import annotations

from __future__ import annotations
import errno
import json
import os
import socket
import threading
import time
from collections.abc import Callable
from typing import Any, Protocol, TYPE_CHECKING, runtime_checkable
from hyprial.kernel import AdmissionResult
from hyprial.daemon.impl.ipc.ipc_request_owner  import IpcRequestOwner
from hyprial.daemon.impl.dispatch.runtime.diagnostics  import DispatchDiagnostics
from hyprial.kernel import ipc_errors
from hyprial.kernel import DaemonRequestError
from hyprial.daemon.impl.operations.management_actor import RegistryManagementAuthority as RegistryManagementHandler
if TYPE_CHECKING:
    pass

from hyprial.daemon.impl.ipc.params import (
    JsonObject,
    _peer_process_id,
    _required_string,
)


_IPC_MAX_CLIENTS = 64

_IPC_ACCEPT_RETRY_INITIAL = 0.05

_IPC_ACCEPT_RETRY_MAX = 1.0

_IPC_CLIENT_IDLE_TIMEOUT = 15.0

_IPC_RESPONSE_WRITE_TIMEOUT = 15.0

_IPC_CLIENT_POLL_INTERVAL = 0.1

def _windows_ipc_enabled() -> bool:
    return os.name == "nt"

_IPC_CLIENT_SHUTDOWN_GRACE = 0.1

_IPC_CLIENT_SHUTDOWN_TIMEOUT = 2.0

def _write_ipc_response(
    connection: _IpcStream, frame: bytes, *, timeout: float,
    poll_interval: float, should_stop: Callable[[], bool],
) -> bool:
    """Write one frame with known progress; never retry a whole sendall."""
    if isinstance(connection, _IpcResponseWriter):
        return connection.send_response(
            frame, timeout=timeout, poll_interval=poll_interval,
            should_stop=should_stop,
        )
    if not isinstance(connection, socket.socket):
        raise TypeError("IPC stream has no safe response writer")
    deadline = time.monotonic() + timeout
    offset = 0
    view = memoryview(frame)
    while offset < len(view):
        if should_stop():
            return False
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("daemon IPC response write deadline expired")
        connection.settimeout(min(poll_interval, remaining))
        try:
            count = connection.send(view[offset:])
        except TimeoutError:
            continue
        if count == 0:
            raise BrokenPipeError("daemon IPC response wrote zero bytes")
        offset += count
    return True

class _IpcStream(Protocol):
    def settimeout(self, timeout: float | None) -> None: ...
    def recv(self, size: int) -> bytes: ...
    def sendall(self, data: bytes) -> None: ...
    def shutdown(self, how: int) -> None: ...
    def close(self) -> None: ...
    def __enter__(self) -> "_IpcStream": ...
    def __exit__(self, *_error: object) -> None: ...

@runtime_checkable
class _IpcResponseWriter(Protocol):
    def send_response(
        self, data: bytes, *, timeout: float, poll_interval: float,
        should_stop: Callable[[], bool],
    ) -> bool: ...

class _IpcListener(Protocol):
    def accept(self) -> tuple[_IpcStream, object]: ...
    def close(self) -> None: ...


class _IpcServerMixin:
    """Application cluster mixin; the state owner is DaemonApplication."""

    def _start_server(self) -> None:
        self.socket_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.socket_path.unlink(missing_ok=True)
        posix_listener = not _windows_ipc_enabled()
        if posix_listener:
            server: _IpcListener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        else:
            from hyprial.kernel import listen_named_pipe as PipeListener

            server = PipeListener(
                self.socket_path,
                gui_write_sid=os.environ.get("HYPRIAL_WINDOWS_GUI_WRITE_SID"),
            )
        try:
            if posix_listener:
                server.bind(str(self.socket_path))  # type: ignore[attr-defined]
                os.chmod(self.socket_path, 0o600)
                server.listen(32)  # type: ignore[attr-defined]
                server.settimeout(0.25)  # type: ignore[attr-defined]
            self._ipc_request_owner = IpcRequestOwner(capacity=_IPC_MAX_CLIENTS)
            self._dispatch_diagnostics = DispatchDiagnostics()
        except BaseException:
            server.close()
            if self._dispatch_diagnostics is not None:
                self._dispatch_diagnostics.close(1.0)
                self._dispatch_diagnostics = None
            if self._ipc_request_owner is not None:
                self._ipc_request_owner.close(1.0)
                self._ipc_request_owner = None
            raise
        self._server = server
        # Keep one descriptor in reserve so an EMFILE accept can discard one
        # queued peer and return to bounded retrying instead of spinning or
        # permanently terminating the dispatcher.
        if posix_listener:
            self._accept_reserve_fd = os.open(os.devnull, os.O_RDONLY)

    def _serve(self) -> None:
        assert self._server is not None
        assert self._runtime is not None
        # Direct-`_serve` drivers (the IPC test suites) have no restore
        # thread, so the maintenance fallback still fires for them.  In a
        # real `run()` the restore thread owns starting the scheduler when
        # restore completes -- firing it here would start reconcile ticks
        # that race the very restore they must come after.
        if self._maintenance_generation == 0 and self._restore_thread is None:
            self._start_maintenance_scheduler()
        accept_retry = _IPC_ACCEPT_RETRY_INITIAL
        while not self.stop_event.is_set():
            try:
                connection, _ = self._server.accept()
            except TimeoutError:
                connection = None
            except OSError as error:
                if self.stop_event.is_set() or error.errno in {
                    errno.EBADF,
                    errno.EINVAL,
                }:
                    break
                if error.errno in {errno.EMFILE, errno.ENFILE}:
                    error_code = errno.errorcode.get(error.errno, "RESOURCE_EXHAUSTED")
                    self._log_accept_resource_exhausted(
                        type(error).__name__, error_code
                    )
                    if error.errno == errno.EMFILE:
                        self._discard_one_client_with_reserve()
                    if self.stop_event.wait(accept_retry):
                        break
                    accept_retry = min(accept_retry * 2, _IPC_ACCEPT_RETRY_MAX)
                    continue
                raise
            if connection is not None:
                accept_retry = _IPC_ACCEPT_RETRY_INITIAL
                self._start_ipc_client(connection)

    def _log_accept_resource_exhausted(
        self, error_type: str, error_code: str
    ) -> None:
        """Best-effort classification that cannot amplify fd exhaustion."""

        try:
            self._log(
                "warn",
                "daemon",
                "daemon.ipc.accept_resource_exhausted",
                errorType=error_type,
                errorCode=error_code,
            )
        except OSError as log_error:
            if log_error.errno not in {errno.EMFILE, errno.ENFILE}:
                raise

    def _discard_one_client_with_reserve(self) -> None:
        """Use the reserved fd to drain one queued peer after process EMFILE."""

        reserve_fd = self._accept_reserve_fd
        if reserve_fd is None or self._server is None:
            return
        self._accept_reserve_fd = None
        os.close(reserve_fd)
        try:
            connection, _ = self._server.accept()
        except OSError:
            pass
        else:
            connection.close()
        finally:
            try:
                self._accept_reserve_fd = os.open(os.devnull, os.O_RDONLY)
            except OSError:
                # The retry loop remains bounded even if system-wide pressure
                # prevents restoring the reserve immediately.
                self._accept_reserve_fd = None

    def _start_ipc_client(self, connection: _IpcStream) -> None:
        """Admit one peer atomically with shutdown, within a fixed cap.

        Capacity is an explicit local IPC contract: when all 64 client slots
        are occupied, the newly accepted overflow connection is closed without
        starting a worker. Callers may retry after an existing request exits.
        """

        capacity_exhausted = False
        with self._ipc_clients_lock:
            if self.stop_event.is_set():
                self._ipc_closing = True
            if self._ipc_closing:
                connection.close()
                return
            if not self._ipc_client_slots.acquire(blocking=False):
                capacity_exhausted = True
            else:
                worker = self._new_ipc_client_worker(connection)
                self._ipc_clients.add(connection)
                self._ipc_client_threads.add(worker)
                try:
                    # Registration and start share the shutdown lock. Closing
                    # therefore observes either no worker or a started worker,
                    # never a registered thread that it cannot yet wake/join.
                    worker.start()
                except BaseException:
                    self._ipc_clients.discard(connection)
                    self._ipc_client_threads.discard(worker)
                    self._ipc_client_slots.release()
                    connection.close()
                    raise

        if capacity_exhausted:
            connection.close()
            try:
                self._log(
                    "warn",
                    "daemon",
                    "daemon.ipc.client_capacity_exhausted",
                    errorType="ClientCapacityError",
                    errorCode="IPC_CLIENT_CAPACITY",
                )
            except OSError as log_error:
                if log_error.errno not in {errno.EMFILE, errno.ENFILE}:
                    raise

    def _new_ipc_client_worker(
        self, connection: _IpcStream
    ) -> threading.Thread:
        """Build the worker whose registration/start are admission-locked."""

        def serve() -> None:
            try:
                self._serve_client(connection)
            finally:
                current = threading.current_thread()
                with self._ipc_clients_lock:
                    self._ipc_clients.discard(connection)
                    self._ipc_client_threads.discard(current)
                self._ipc_client_slots.release()

        return threading.Thread(
            target=serve,
            name="hyprial-daemon-ipc-client",
            daemon=True,
        )

    def _close_ipc_clients(self) -> None:
        """Wake and reap all client workers without leaking daemon threads."""

        current = threading.current_thread()
        with self._ipc_clients_lock:
            # This is the shutdown/admission linearization point. Once set, no
            # accepted connection can be registered or start a worker.
            self._ipc_closing = True
        grace_deadline = time.monotonic() + _IPC_CLIENT_SHUTDOWN_GRACE
        while time.monotonic() < grace_deadline:
            with self._ipc_clients_lock:
                threads = tuple(
                    item
                    for item in self._ipc_client_threads
                    if item is not current and item.is_alive()
                )
            if not threads:
                return
            for thread in threads:
                thread.join(timeout=0.01)

        with self._ipc_clients_lock:
            # Admission is already closed, but accepted replies owned the
            # grace interval. Only now interrupt their remaining writes.
            self._ipc_force_closing = True
            clients = tuple(self._ipc_clients)
            threads = tuple(
                item for item in self._ipc_client_threads if item is not current
            )
        for connection in clients:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                connection.close()
            except BlockingIOError:
                # A pipe write/read still owns its kernel operation. Its
                # existing holder finishes requested close after settlement.
                pass
        deadline = time.monotonic() + _IPC_CLIENT_SHUTDOWN_TIMEOUT
        for thread in threads:
            thread.join(timeout=max(0.0, deadline - time.monotonic()))
        with self._ipc_clients_lock:
            still_running = tuple(
                item
                for item in self._ipc_client_threads
                if item is not current and item.is_alive()
            )
        if still_running:
            raise RuntimeError(
                "daemon IPC client workers did not stop within "
                f"{_IPC_CLIENT_SHUTDOWN_TIMEOUT:g}s"
            )

    def _serve_client(self, connection: _IpcStream) -> None:
        """Serve one local client without letting it terminate the daemon."""

        try:
            with connection:
                # A close/shutdown from another thread does not reliably wake
                # an AF_UNIX recv on every supported kernel (notably Darwin).
                # Polling keeps the existing 15-second idle bound while making
                # daemon shutdown observable without depending on that wakeup.
                connection.settimeout(_IPC_CLIENT_POLL_INTERVAL)
                self._serve_connection(connection)
        except (OSError, DaemonRequestError) as error:
            # Timeouts, resets and broken pipes describe only this local IPC
            # connection.  Likewise, an oversized request is client-scoped.
            # Log classifications only: exception text can contain client
            # controlled or otherwise sensitive data.
            fields: JsonObject = {"errorType": type(error).__name__}
            if isinstance(error, DaemonRequestError):
                fields["errorCode"] = error.code
            self._log("warn", "daemon", "daemon.ipc.client_error", **fields)

    def _serve_connection(self, connection: _IpcStream) -> None:
        buffer = bytearray()
        idle_deadline = time.monotonic() + _IPC_CLIENT_IDLE_TIMEOUT
        while len(buffer) <= 8 * 1024 * 1024:
            try:
                chunk = connection.recv(64 * 1024)
            except TimeoutError:
                with self._ipc_clients_lock:
                    closing = self._ipc_closing
                if closing or self.stop_event.is_set():
                    return
                if time.monotonic() >= idle_deadline:
                    raise
                continue
            if not chunk:
                return
            buffer.extend(chunk)
            idle_deadline = time.monotonic() + _IPC_CLIENT_IDLE_TIMEOUT
            if b"\n" not in buffer:
                continue
            line, _, _ = buffer.partition(b"\n")
            # Domain actors/facades own business ordering.  This lock only
            # fences socket admission against shutdown; no slow lifecycle,
            # workflow or adapter operation can convoy unrelated IPC.
            with self._ipc_clients_lock:
                if self._ipc_closing or self.stop_event.is_set():
                    return
            self._ipc_peer.pid = _peer_process_id(connection)
            try:
                response = self._response(line)
            finally:
                self._ipc_peer.pid = None
            frame = (
                json.dumps(response, separators=(",", ":"), ensure_ascii=False).encode()
                + b"\n"
            )

            def response_stopped() -> bool:
                with self._ipc_clients_lock:
                    # This request crossed admission before its handler ran.
                    # A handler (including shutdown) may itself request stop;
                    # its one reply still owns the existing graceful drain.
                    return self._ipc_force_closing

            _write_ipc_response(
                connection, frame, timeout=_IPC_RESPONSE_WRITE_TIMEOUT,
                poll_interval=_IPC_CLIENT_POLL_INTERVAL,
                should_stop=response_stopped,
            )
            return
        raise DaemonRequestError(
            ipc_errors.IPC_REQUEST_TOO_LARGE, "daemon IPC request exceeded 8 MiB"
        )

    def _response(self, line: bytes) -> JsonObject:
        request_id: object = None
        try:
            request = json.loads(line)
            if not isinstance(request, dict):
                raise DaemonRequestError(
                    ipc_errors.INVALID_REQUEST, "daemon request must be an object"
                )
            request_id = request.get("id")
            if request.get("version") != 1:
                raise DaemonRequestError(
                    ipc_errors.VERSION_MISMATCH, "unsupported daemon IPC version"
                )
            method = _required_string(request.get("method"), "method")
            params = request.get("params", {})
            if not isinstance(params, dict):
                raise DaemonRequestError(ipc_errors.INVALID_ARGUMENT, "params must be an object")
            if (
                method == "message.send"
                and isinstance(request_id, str)
                and request_id
                and "idempotencyKey" not in params
            ):
                params = {**params, "idempotencyKey": request_id}
            result = self._timed_handle(
                method, params, _trusted_message_origin="ipc"
            )
            return {"version": 1, "id": request_id, "result": result}
        except (NameError, ImportError):
            raise
        except Exception as error:  # noqa: BLE001 - stable daemon error boundary
            code = getattr(error, "code", ipc_errors.DAEMON_ERROR)
            failure: JsonObject = {"code": str(code), "message": str(error)}
            data = getattr(error, "data", None)
            if data is not None:
                failure["data"] = data
            return {"version": 1, "id": request_id, "error": failure}

    def _timed_handle(
        self,
        method: str,
        params: JsonObject,
        *,
        _trusted_message_origin: str | None = None,
    ) -> Any:
        """``handle`` for one IPC request, charged to ``daemon.ipcStats``.

        This is the single point every socket request passes through; the
        in-process ``self.handle(...)`` calls (restore, lifecycle helpers)
        deliberately bypass it, so the counters mean "cost of serving IPC".
        Only this thread's CPU is read: time the handler spends parked on a
        domain actor is charged on that actor's own thread, never here (the
        attribution rule in ``ipc_stats``).  Request framing -- JSON parse,
        envelope, ``sendall`` -- stays outside the timed span and is a named
        uncovered category.
        """

        owner = self._ipc_request_owner
        started_request = owner.start(method) if owner is not None else None
        stats = self._ipc_stats
        if owner is not None and started_request is None:
            refusal = ipc_errors.DaemonUnavailableError(
                "IPC request authority is closed or overloaded",
            )
            # Counted too: an admission refusal is exactly the overload a
            # reader of errorCodes is looking for (review 821).
            if stats.enabled:
                stats.record(
                    method, cpu_seconds=0.0, wall_seconds=0.0, error=True,
                    error_code=str(getattr(refusal, "code", ipc_errors.DAEMON_ERROR)),
                )
            raise refusal
        failed = True
        error_code: str | None = None
        started_cpu = time.thread_time()
        started_wall = time.perf_counter()
        try:
            result = self.handle(
                method,
                params,
                _trusted_message_origin=_trusted_message_origin,
            )
            failed = False
            return result
        except BaseException as error:
            # The same code _response puts on the wire, so errorCodes match
            # what clients see (an uncoded exception is DAEMON_ERROR there).
            error_code = str(getattr(error, "code", ipc_errors.DAEMON_ERROR))
            raise
        finally:
            if stats.enabled:
                stats.record(
                    method,
                    cpu_seconds=time.thread_time() - started_cpu,
                    wall_seconds=time.perf_counter() - started_wall,
                    error=failed,
                    error_code=error_code,
                )
            if owner is not None and started_request is not None:
                admission = owner.complete(started_request, error_code)
                if admission is not AdmissionResult.ACCEPTED:
                    self._log(
                        "error", "daemon", "daemon.ipc.completion_overloaded",
                        reason=admission.value,
                    )

    def _registry_management_handler(self) -> RegistryManagementHandler:
        with self._registry_management_lock:
            management = self._registry_management
            if management is None:
                assert self._harnesses is not None
                management = RegistryManagementHandler(
                    self._harnesses,
                    self.agents,
                    start_harness=lambda spec: self.handle(
                        "lifecycle.start-smolvm" if spec.execution_runtime else "lifecycle.start", spec.to_json()
                    ),
                )
                self._registry_management = management
            return management
