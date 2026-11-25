#!/usr/bin/env python3
"""Isolated two-daemon proof for Zenoh half-open detection and reconnect.

The user-space TCP proxy deliberately keeps the connect-side socket open after
the listen-side daemon is SIGKILLed.  It continues reading (and therefore lets
the kernel ACK) the connect-side traffic while returning no Zenoh frames.  A
plain loopback kill would send FIN/RST immediately and would not exercise the
lease-expiry path reported in defect #39.
"""

from __future__ import annotations

import json
import os
import selectors
import signal
import socket
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

import sys

# Stand on its own when loaded by path (spec_from_file_location in the CI
# baseline tests), not only when run as a script: the sibling helper module
# lives next to this file.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _daemon_runner import IsolatedDaemon, hyprial_executable, short_temp_root

LEASE_MS = 6_000
DISCONNECT_DEADLINE_SECONDS = LEASE_MS / 1_000 + 4
RECONNECT_DEADLINE_SECONDS = 25.0
TUNING_ENVIRONMENT = (
    "HYPRIAL_ZENOH_LEASE_MS",
    "HYPRIAL_ZENOH_KEEP_ALIVE",
    "HYPRIAL_ZENOH_CONNECT_RETRY_INITIAL_MS",
    "HYPRIAL_ZENOH_CONNECT_RETRY_MAX_MS",
    "HYPRIAL_ZENOH_CONNECT_RETRY_MULTIPLIER",
)


# CLI probe persona, registered on both daemons (Allen b).
DRIVER = "e2e-driver"


def _address(endpoint: str) -> tuple[str, int]:
    protocol, address = endpoint.split("/", 1)
    if protocol != "tcp":
        raise AssertionError(f"proxy only supports tcp endpoints: {endpoint}")
    host, port = address.rsplit(":", 1)
    return host, int(port)


class HalfOpenProxy:
    """Forward one TCP endpoint, then blackhole established downstreams."""

    def __init__(self, upstream: str) -> None:
        self.upstream = _address(upstream)
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen()
        self._listener.settimeout(0.2)
        port = self._listener.getsockname()[1]
        self.endpoint = f"tcp/127.0.0.1:{port}"
        self.upstream_lost = threading.Event()
        self._stop = threading.Event()
        self._connections: set[socket.socket] = set()
        self._connections_lock = threading.Lock()
        self._workers: list[threading.Thread] = []
        self._thread = threading.Thread(target=self._accept, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._listener.close()
        with self._connections_lock:
            connections = tuple(self._connections)
        for connection in connections:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            connection.close()
        self._thread.join(timeout=3)
        for worker in self._workers:
            worker.join(timeout=3)
        leaked = [worker.name for worker in self._workers if worker.is_alive()]
        if self._thread.is_alive() or leaked:
            raise AssertionError(f"TCP proxy threads leaked: {leaked}")

    def _track(self, connection: socket.socket) -> None:
        with self._connections_lock:
            self._connections.add(connection)

    def _forget(self, connection: socket.socket) -> None:
        with self._connections_lock:
            self._connections.discard(connection)

    def _accept(self) -> None:
        while not self._stop.is_set():
            try:
                downstream, _ = self._listener.accept()
            except TimeoutError:
                continue
            except OSError:
                break
            self._track(downstream)
            worker = threading.Thread(
                target=self._bridge,
                args=(downstream,),
                name=f"half-open-proxy-{len(self._workers)}",
                daemon=True,
            )
            self._workers.append(worker)
            worker.start()

    def _bridge(self, downstream: socket.socket) -> None:
        upstream: socket.socket | None = None
        selector = selectors.DefaultSelector()
        try:
            downstream.settimeout(1)
            try:
                upstream = socket.create_connection(self.upstream, timeout=1)
            except OSError:
                # During the restart window, make Zenoh observe a failed retry
                # and continue its configured backoff loop.
                return
            upstream.settimeout(1)
            selector.register(downstream, selectors.EVENT_READ, "downstream")
            selector.register(upstream, selectors.EVENT_READ, "upstream")
            while not self._stop.is_set():
                for key, _ in selector.select(timeout=0.2):
                    connection = key.fileobj
                    try:
                        data = connection.recv(64 * 1024)
                    except (OSError, TimeoutError):
                        data = b""
                    if key.data == "downstream":
                        if not data:
                            return
                        if upstream is not None:
                            try:
                                upstream.sendall(data)
                            except OSError:
                                selector.unregister(upstream)
                                upstream.close()
                                upstream = None
                                self.upstream_lost.set()
                        # With no upstream, deliberately discard the bytes.
                        # The downstream TCP stays established and ACKs writes.
                    else:
                        if not data:
                            selector.unregister(connection)
                            connection.close()
                            upstream = None
                            self.upstream_lost.set()
                            continue
                        try:
                            downstream.sendall(data)
                        except OSError:
                            return
        finally:
            selector.close()
            if upstream is not None:
                upstream.close()
            downstream.close()
            self._forget(downstream)


def _daemon_pid(daemon: IsolatedDaemon) -> int:
    metadata = json.loads(
        (daemon.root / "state" / "daemon.json").read_text(encoding="utf-8")
    )
    pid = metadata.get("pid")
    if not isinstance(pid, int) or pid <= 0:
        raise AssertionError(f"invalid daemon pid metadata: {metadata}")
    return pid


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _kill(daemon: IsolatedDaemon) -> int:
    pid = _daemon_pid(daemon)
    os.kill(pid, signal.SIGKILL)
    daemon.wait(lambda: _pid_alive(pid), lambda alive: not alive, timeout=5)
    return pid


def _actors(daemon: IsolatedDaemon) -> set[str]:
    # Node presence lives in `hosts` — targets lists delivery promises only.
    value = daemon.command("host", "list", "--json")
    targets = value.get("hosts")
    if not isinstance(targets, list):
        raise AssertionError(f"targets is not an array: {value}")  # noqa: TRY004
    return {
        actor
        for item in targets
        if isinstance(item, dict) and isinstance((actor := item.get("nodeId")), str)
    }


def _pending_ids(value: dict[str, Any]) -> list[str]:
    pending = value.get("pendingMessages")
    if not isinstance(pending, list):
        return []
    return [
        message_id
        for item in pending
        if isinstance(item, dict)
        and isinstance((message_id := item.get("messageId")), str)
    ]


def _cleanup_daemon(daemon: IsolatedDaemon) -> None:
    metadata = daemon.root / "state" / "daemon.json"
    if metadata.exists():
        try:
            if _pid_alive(_daemon_pid(daemon)):
                daemon.stop()
                return
        except (AssertionError, OSError):
            pass
    (daemon.root / "state" / "daemon.sock").unlink(missing_ok=True)


def main() -> int:
    executable = hyprial_executable()
    # Keep the future upstream port reserved while the proxy binds its own
    # ephemeral port.  Otherwise macOS can immediately reuse the just-closed
    # upstream port for the proxy and make the listener fail at startup.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as reservation:
        reservation.bind(("127.0.0.1", 0))
        upstream = f"tcp/127.0.0.1:{reservation.getsockname()[1]}"
        proxy = HalfOpenProxy(upstream)
    proxy.start()
    cleanup_errors: list[str] = []
    with tempfile.TemporaryDirectory(prefix="zhr-", dir=short_temp_root()) as temporary:
        root = Path(temporary)
        listener = IsolatedDaemon(
            executable,
            root / "listener",
            "zenoh-reconnect-listener",
            listen=upstream,
        )
        connector = IsolatedDaemon(
            executable,
            root / "connector",
            "zenoh-reconnect-connector",
            connect=proxy.endpoint,
        )
        # This runner proves the product defaults, never ambient operator
        # tuning inherited from the shell that launched the test.
        for daemon in (listener, connector):
            for name in TUNING_ENVIRONMENT:
                daemon.env.pop(name, None)
        try:
            print("RESULT info zenoh-half-open-reconnect phase=start-listener")
            listener.start()
            print("RESULT info zenoh-half-open-reconnect phase=start-connector")
            connector.start()
            # resolve-or-reject (Allen b): probe sender/receiver are
            # registered identities — no more bare node_id addressing.
            connector.command("agent", "create", "--name", DRIVER, "--json")
            listener.command("agent", "create", "--name", DRIVER, "--json")
            connector.wait(
                lambda: _actors(connector),
                lambda actors: listener.node_id in actors,
            )
            listener.wait(
                lambda: _actors(listener),
                lambda actors: connector.node_id in actors,
            )

            # Two complete keepalive intervals without losing either target.
            steady_deadline = time.monotonic() + LEASE_MS / 1_000 + 1
            steady_checks = 0
            while time.monotonic() < steady_deadline:
                if listener.node_id not in _actors(connector):
                    raise AssertionError(
                        "healthy listener disappeared during keepalive"
                    )
                steady_checks += 1
                time.sleep(0.5)

            print("RESULT info zenoh-half-open-reconnect phase=kill-listener")
            killed_pid = _kill(listener)
            if not proxy.upstream_lost.wait(timeout=3):
                raise AssertionError("proxy did not observe the killed upstream")
            disconnected_at = time.monotonic()
            connector.wait(
                lambda: _actors(connector),
                lambda actors: listener.node_id not in actors,
                timeout=DISCONNECT_DEADLINE_SECONDS,
            )
            disconnect_seconds = time.monotonic() - disconnected_at

            restarted_at = time.monotonic()
            print("RESULT info zenoh-half-open-reconnect phase=restart-listener")
            listener.start()
            connector.wait(
                lambda: _actors(connector),
                lambda actors: listener.node_id in actors,
                timeout=RECONNECT_DEADLINE_SECONDS,
            )
            listener.wait(
                lambda: _actors(listener),
                lambda actors: connector.node_id in actors,
                timeout=RECONNECT_DEADLINE_SECONDS,
            )
            reconnect_seconds = time.monotonic() - restarted_at

            listener_info = listener.ps().get("daemon", {})
            listener_owner = str(listener_info.get("owner") or "")
            listener_target = f"agent:{listener_owner}:{listener.node_id}:{DRIVER}"
            outbound = connector.command(
                "message",
                "send",
                "--from",
                DRIVER,
                "--to",
                listener_target,
                "--summary",
                "after-half-open-reconnect",
                "after-half-open-reconnect",
                "--json",
            )
            message_id = outbound.get("messageId")
            if not isinstance(message_id, str):
                raise AssertionError(  # noqa: TRY004
                    f"post-reconnect send has no message id: {outbound}"
                )
            listener.wait(
                lambda: listener.ps(),
                lambda value: message_id in _pending_ids(value),
            )
            ack = listener.command(
                "message", "ack", "--from", DRIVER, message_id, "--json"
            )
            if ack.get("acknowledged") is not True:
                raise AssertionError(f"post-reconnect acknowledgement failed: {ack}")

            print(
                "RESULT evidence zenoh-half-open-reconnect "
                f"killedPid={killed_pid} steadyChecks={steady_checks} "
                f"disconnectSeconds={disconnect_seconds:.3f} "
                f"reconnectSeconds={reconnect_seconds:.3f} "
                "targetsMutual=true postReconnectDelivery=true"
            )
        except BaseException:
            for daemon in (listener, connector):
                log_path = daemon.root / "state" / "daemon-launch.log"
                if log_path.exists():
                    print(
                        f"RESULT diagnostic {daemon.node_id} "
                        f"{log_path.read_text(encoding='utf-8')[-4_000:]!r}"
                    )
            raise
        finally:
            for daemon in (connector, listener):
                try:
                    _cleanup_daemon(daemon)
                except BaseException as error:  # noqa: BLE001 - attempt all cleanup
                    cleanup_errors.append(
                        f"{daemon.node_id}: {type(error).__name__}: {error}"
                    )
            try:
                proxy.stop()
            except BaseException as error:  # noqa: BLE001 - preserve cleanup evidence
                cleanup_errors.append(f"proxy: {type(error).__name__}: {error}")
    if cleanup_errors:
        raise AssertionError(f"cleanup failed: {cleanup_errors}")
    print("RESULT PASS zenoh-half-open-reconnect cleanup=clean")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (AssertionError, OSError, KeyError) as error:
        print(f"RESULT FAIL zenoh-half-open-reconnect {error}")
        raise SystemExit(1) from error
