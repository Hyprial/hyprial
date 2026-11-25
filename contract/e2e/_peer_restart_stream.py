"""E2E-024 private stream fault fixture for leased-roster forwarding.

The opaque Unix stream preserves swallow-close, blackhole and delayed-resplice
faults at the current private sidecar boundary. Endpoints stay inside one owned
scenario root; cleanup closes listeners, connections and all worker threads.
"""
from __future__ import annotations

import os
from pathlib import Path
import selectors
import socket
import threading
import time

LOSS_WINDOW_SECONDS = 0.25


def _address(endpoint: str, directory: Path) -> str:
    if not endpoint.startswith("unixsock-stream/"):
        raise ValueError("private Unix upstream required")
    path = Path(endpoint.removeprefix("unixsock-stream/"))
    if not path.is_absolute() or not path.resolve().is_relative_to(directory.resolve()):
        raise ValueError("upstream escaped the scenario root")
    return str(path)


def _listen(path: Path) -> tuple[socket.socket, str]:
    parent = path.parent.stat()
    if parent.st_uid != os.getuid() or parent.st_mode & 0o777 != 0o700:
        raise ValueError("private owned socket parent required")
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        listener.bind(str(path))
        listener.listen()
        listener.settimeout(0.2)
    except BaseException:
        listener.close()
        raise
    return listener, f"unixsock-stream/{path}"


def _connect(path: str, *, timeout: float) -> socket.socket:
    connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        connection.settimeout(timeout)
        connection.connect(path)
        return connection
    except BaseException:
        connection.close()
        raise


class RespliceForwarder:
    """Make an upstream close lossy while retaining the private Unix stream."""

    def __init__(self, upstream: str, *, directory: Path, mode: str, resplice_delay_seconds: float=0,
                 listener_directory: Path | None = None) -> None:
        if mode not in {'swallow-close', 'blackhole'}:
            raise ValueError(f'unsupported forwarder mode: {mode}')
        self._upstream = _address(upstream, directory)
        self._mode = mode
        self._resplice_delay_seconds = resplice_delay_seconds
        listener_directory = listener_directory or directory
        if not listener_directory.resolve().is_relative_to(directory.resolve()):
            raise ValueError('listener escaped the scenario root')
        self._socket_paths = (listener_directory / 'front.sock', listener_directory / 'reverse.sock')
        self._listener, self.endpoint = _listen(self._socket_paths[0])
        try:
            self._reverse_listener, self.reverse_endpoint = _listen(self._socket_paths[1])
        except BaseException:
            self._listener.close()
            self._socket_paths[0].unlink(missing_ok=True)
            raise
        self._started = time.monotonic()
        self._stop = threading.Event()
        self._guard = threading.Lock()
        self._events: list[dict[str, object]] = []
        self._connections: set[socket.socket] = set()
        self._workers: list[threading.Thread] = []
        self._thread = threading.Thread(target=self._accept, name='peer-restart-forwarder', daemon=True)
        self._reverse_thread = threading.Thread(target=self._accept_reverse, name='peer-restart-reverse-blackhole', daemon=True)

    def start(self) -> None:
        self._record('listener', state='LISTEN')
        self._record('reverse-listener', state='LISTEN')
        self._thread.start()
        self._reverse_thread.start()

    def snapshot(self) -> list[dict[str, object]]:
        with self._guard:
            return [dict(event) for event in self._events]

    def stop(self) -> None:
        self._stop.set()
        self._listener.close()
        self._reverse_listener.close()
        with self._guard:
            connections = tuple(self._connections)
        for connection in connections:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            connection.close()
        for listener in (self._thread, self._reverse_thread):
            if listener.ident is not None:
                listener.join(timeout=3)
        for worker in self._workers:
            worker.join(timeout=3)
        leaked = [worker.name for worker in self._workers if worker.is_alive()]
        if self._thread.is_alive() or self._reverse_thread.is_alive() or leaked:
            raise AssertionError(f'forwarder threads leaked: {leaked}')
        for path in self._socket_paths:
            path.unlink(missing_ok=True)

    def _record(self, edge: str, **fields: object) -> None:
        event = {'atSeconds': round(time.monotonic() - self._started, 3), 'edge': edge, **fields}
        with self._guard:
            self._events.append(event)

    def _accept(self) -> None:
        downstream_generation = 0
        while not self._stop.is_set():
            try:
                downstream, peer = self._listener.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            downstream_generation += 1
            with self._guard:
                self._connections.add(downstream)
            self._record('client', state='ESTABLISHED', generation=downstream_generation, peer=str(peer) or 'unnamed-private-peer')
            worker = threading.Thread(target=self._bridge, args=(downstream, downstream_generation), name=f'peer-restart-forwarder-{downstream_generation}', daemon=True)
            self._workers.append(worker)
            worker.start()

    def _accept_reverse(self) -> None:
        generation = 0
        while not self._stop.is_set():
            try:
                connection, peer = self._reverse_listener.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            generation += 1
            with self._guard:
                self._connections.add(connection)
            self._record('reverse-client', state='ESTABLISHED_BLACKHOLE', generation=generation, peer=str(peer) or 'unnamed-private-peer')
            worker = threading.Thread(target=self._blackhole_reverse, args=(connection, generation), name=f'peer-restart-reverse-blackhole-{generation}', daemon=True)
            self._workers.append(worker)
            worker.start()

    def _blackhole_reverse(self, connection: socket.socket, generation: int) -> None:
        connection.settimeout(0.2)
        try:
            while not self._stop.is_set():
                try:
                    data = connection.recv(64 * 1024)
                except TimeoutError:
                    continue
                except OSError:
                    return
                if not data:
                    return
                self._record('reverse-client', state='ESTABLISHED_DISCARDING', generation=generation, bytes=len(data))
        finally:
            connection.close()
            with self._guard:
                self._connections.discard(connection)
            self._record('reverse-client', state='CLOSED', generation=generation)

    def _bridge(self, downstream: socket.socket, generation: int) -> None:
        selector = selectors.DefaultSelector()
        upstream: socket.socket | None = None
        upstream_generation = 0
        next_connect = 0.0
        blackholed = False
        pending: list[tuple[float, bytes]] = []
        selector.register(downstream, selectors.EVENT_READ, 'client')
        try:
            while not self._stop.is_set():
                now = time.monotonic()
                while pending and now - pending[0][0] >= LOSS_WINDOW_SECONDS:
                    _, buffered = pending.pop(0)
                    try:
                        downstream.sendall(buffered)
                    except OSError:
                        return
                    self._record('upstream-to-client', state='FORWARDED', clientGeneration=generation, generation=upstream_generation, bytes=len(buffered))
                if not blackholed and upstream is None and (now >= next_connect):
                    try:
                        candidate = _connect(self._upstream, timeout=0.2)
                    except OSError:
                        next_connect = now + 0.1
                    else:
                        candidate.settimeout(None)
                        upstream = candidate
                        upstream_generation += 1
                        selector.register(upstream, selectors.EVENT_READ, 'upstream')
                        self._record('upstream', state='ESTABLISHED', clientGeneration=generation, generation=upstream_generation)
                for key, _ in selector.select(timeout=0.05):
                    connection = key.fileobj
                    try:
                        data = connection.recv(64 * 1024)
                    except OSError:
                        data = b''
                    if key.data == 'client':
                        if not data:
                            return
                        if upstream is None or blackholed:
                            self._record('client', state='ESTABLISHED_DISCARDING', generation=generation, bytes=len(data))
                            continue
                        try:
                            upstream.sendall(data)
                        except OSError:
                            upstream, blackholed, next_connect = self._lose_upstream(selector, upstream, generation, upstream_generation, pending)
                    else:
                        if not data:
                            upstream, blackholed, next_connect = self._lose_upstream(selector, connection, generation, upstream_generation, pending)
                            continue
                        pending.append((time.monotonic(), data))
                        self._record('upstream-to-client', state='BUFFERED', clientGeneration=generation, generation=upstream_generation, bytes=len(data))
        finally:
            selector.close()
            if upstream is not None:
                upstream.close()
            downstream.close()
            with self._guard:
                self._connections.discard(downstream)
            self._record('client', state='CLOSED', generation=generation)

    def _lose_upstream(self, selector: selectors.BaseSelector, upstream: socket.socket, client_generation: int, upstream_generation: int, pending: list[tuple[float, bytes]]) -> tuple[None, bool, float]:
        try:
            selector.unregister(upstream)
        except (KeyError, ValueError):
            pass
        upstream.close()
        self._record('upstream', state='CLOSED', clientGeneration=client_generation, generation=upstream_generation)
        swallowed = sum((len(data) for _, data in pending))
        pending.clear()
        self._record('upstream-to-client', state='SWALLOWED_ON_CLOSE', clientGeneration=client_generation, generation=upstream_generation, bytes=swallowed)
        if self._mode == 'blackhole':
            self._record('path', state='BLACKHOLE', clientGeneration=client_generation)
            return (None, True, float('inf'))
        next_connect = time.monotonic() + self._resplice_delay_seconds
        self._record('path', state='RESPLICE_SCHEDULED', clientGeneration=client_generation, delaySeconds=self._resplice_delay_seconds)
        return (None, False, next_connect)
