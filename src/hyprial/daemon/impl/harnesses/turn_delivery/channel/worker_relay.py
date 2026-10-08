"""Host-owned Unix relay exposing only one managed worker's IPC capability.

The guest mounts this socket, never the daemon socket or its state directory.
The launcher supplies the binding; guests cannot register/refresh it. This
relay is opt-in and is not yet wired to a persisted smolvm worker launch.
"""

from __future__ import annotations

import json
import socket
import stat
import threading
import time
from pathlib import Path
from typing import Any

from hyprial.kernel import ipc_errors
from hyprial.kernel import (
    MAX_FRAME_BYTES,
    WorkerBinding,
    WorkerChannelError,
    bound_request,
)


def _read_frame(client: socket.socket, timeout: float) -> bytes:
    deadline = time.monotonic() + timeout
    data = bytearray()
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Worker channel read expired")
        client.settimeout(remaining)
        chunk = client.recv(min(65536, MAX_FRAME_BYTES + 1 - len(data)))
        if not chunk:
            raise ConnectionError("Worker channel closed before a complete frame")
        data.extend(chunk)
        if len(data) > MAX_FRAME_BYTES:
            raise WorkerChannelError(
                ipc_errors.PAYLOAD_TOO_LARGE, "IPC frame too large"
            )
        if b"\n" in data:
            return bytes(data.partition(b"\n")[0])


def _failure(request_id: object, code: str) -> dict[str, Any]:
    # Do not expose daemon exception text/data, paths, or an echoed payload.
    return {
        "version": 1,
        "id": request_id,
        "error": {"code": code, "message": "Worker channel request failed"},
    }


class BoundWorkerRelay:
    def __init__(
        self,
        *,
        directory: Path,
        daemon_socket: Path,
        binding: WorkerBinding,
        timeout: float = 15.0,
        max_clients: int = 4,
    ):
        if not 0 < timeout <= 30 or not 1 <= max_clients <= 16:
            raise ValueError("Invalid worker relay limits")
        self.directory = directory
        self.socket_path = directory / "daemon.sock"
        self.daemon_socket = daemon_socket
        self.binding = binding
        self.timeout = timeout
        self._slots = threading.BoundedSemaphore(max_clients)
        self._lock = threading.Lock()
        self._closed = threading.Event()
        self._server: socket.socket | None = None
        self._acceptor: threading.Thread | None = None
        self._clients: set[socket.socket] = set()
        self._threads: set[threading.Thread] = set()
        self._socket_identity: tuple[int, int] | None = None
        self._started = False

    def __enter__(self) -> BoundWorkerRelay:
        self.start()
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def start(self) -> None:
        if self._started or self._closed.is_set():
            raise RuntimeError("Relay instances cannot be restarted")
        if (
            not self.directory.is_absolute()
            or self.directory.parent.resolve() != self.directory.parent
            or not self.directory.parent.is_dir()
        ):
            raise ValueError("Relay requires a canonical absolute directory")
        self.directory.mkdir(mode=0o700)  # Never adopt a preexisting path/link.
        self._started = True
        try:
            server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self._server = server
            server.bind(str(self.socket_path))
            info = self.socket_path.lstat()
            self._socket_identity = (info.st_dev, info.st_ino)
            self.socket_path.chmod(0o600)
            server.listen(4)
            server.settimeout(0.1)
            self._acceptor = threading.Thread(target=self._accept, daemon=True)
            self._acceptor.start()
        except BaseException:
            self.close()
            raise

    def _accept(self) -> None:
        assert self._server is not None
        while not self._closed.is_set():
            try:
                client, _ = self._server.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            if not self._slots.acquire(blocking=False):
                client.close()
                continue
            with self._lock:
                if self._closed.is_set():
                    client.close()
                    self._slots.release()
                    return
                self._clients.add(client)
                thread = threading.Thread(
                    target=self._serve, args=(client,), daemon=True
                )
                self._threads.add(thread)
                thread.start()

    def _forward(self, request: dict[str, Any]) -> dict[str, Any]:
        request_id = request.get("id")
        wrapped = {
            "version": 1,
            "id": request_id,
            "method": "worker.channel.request",
            "params": self.binding.envelope(request),
        }
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as upstream:
            with self._lock:
                if self._closed.is_set():
                    raise ConnectionError("Relay closed")
                self._clients.add(upstream)
            try:
                upstream.settimeout(self.timeout)
                upstream.connect(str(self.daemon_socket))
                upstream.sendall(json.dumps(wrapped).encode() + b"\n")
                response = json.loads(_read_frame(upstream, self.timeout))
            finally:
                with self._lock:
                    self._clients.discard(upstream)
        if (
            not isinstance(response, dict)
            or response.get("version") != 1
            or response.get("id") != request_id
        ):
            raise ValueError("Invalid upstream envelope")
        if isinstance(response.get("error"), dict):
            code = response["error"].get("code")
            # Only known daemon codes, never arbitrary error text as a code.
            known = {
                v
                for k, v in vars(ipc_errors).items()
                if k.isupper() and isinstance(v, str)
            }
            return _failure(
                request_id,
                code
                if isinstance(code, str) and code in known
                else ipc_errors.DAEMON_ERROR,
            )
        if not isinstance(response.get("result"), dict):
            raise ValueError("Invalid upstream result")
        return {"version": 1, "id": request_id, "result": response["result"]}

    def _serve(self, client: socket.socket) -> None:
        request_id = None
        try:
            try:
                request = json.loads(_read_frame(client, self.timeout))
                if isinstance(request, dict):
                    candidate_id = request.get("id")
                    if isinstance(candidate_id, str) and 0 < len(candidate_id) <= 256:
                        request_id = candidate_id
                bound_request(request, self.binding)
                response = self._forward(request)
            except WorkerChannelError as error:
                response = _failure(request_id, error.code)
            except (ValueError, RecursionError):
                response = _failure(request_id, ipc_errors.INVALID_REQUEST)
            except (OSError, ConnectionError):
                response = _failure(request_id, ipc_errors.DAEMON_UNAVAILABLE)
            data = json.dumps(response).encode() + b"\n"
            if len(data) > MAX_FRAME_BYTES:
                data = (
                    json.dumps(
                        _failure(request_id, ipc_errors.PAYLOAD_TOO_LARGE)
                    ).encode()
                    + b"\n"
                )
            client.settimeout(self.timeout)
            client.sendall(data)
        except OSError:
            pass  # Peer disconnect: never retry a possibly accepted mutation.
        finally:
            client.close()
            with self._lock:
                self._clients.discard(client)
                self._threads.discard(threading.current_thread())
            self._slots.release()

    def close(self) -> None:
        self._closed.set()
        if self._server is not None:
            self._server.close()
        with self._lock:
            for client in self._clients:
                try:
                    client.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
            threads = tuple(self._threads)
        deadline = time.monotonic() + self.timeout + 1
        for thread in (*threads, self._acceptor):
            if thread is not None:
                thread.join(max(0, deadline - time.monotonic()))
                if thread.is_alive():
                    raise RuntimeError("Worker relay did not stop")
        if self._socket_identity is not None:
            try:
                info = self.socket_path.lstat()
                if (
                    stat.S_ISSOCK(info.st_mode)
                    and (info.st_dev, info.st_ino) == self._socket_identity
                ):
                    self.socket_path.unlink()
            except FileNotFoundError:
                pass
        if self._started:
            try:
                self.directory.rmdir()  # Refuse to delete unrelated files.
            except OSError:
                pass
