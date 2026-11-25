"""Child-only generic adapter worker entrypoint.

Native provider modules are imported only from this process after token receipt
and session-lock acquisition.  Daemon-side modules never resolve the entrypoint.
"""

from __future__ import annotations

import argparse
import os
import signal
import socket
import threading
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from hyprial.daemon.impl.actor_custody import ActorHostCustody

from .control import AdapterControlIoOwner
from .daemon_link import AdapterDaemonLink
from .process import (
    CONTROL_FD_ENV_VAR,
    READY_FD_ENV_VAR,
    STATE_DIR_ENV_VAR,
    TOKEN_FD_ENV_VAR,
)
from .session import NativeSessionWriterLock
from .wire import (
    WORKER_TOKEN_BYTES,
    WorkerIdentity,
    build_ready_frame,
    send_worker_frame,
)


def _inherited_socket(name: str) -> socket.socket:
    raw = os.environ.get(name)
    if raw is None:
        raise RuntimeError(f"missing inherited endpoint: {name}")
    try:
        endpoint = urlsplit(f"//{raw}")
        port = endpoint.port
    except ValueError as error:
        raise RuntimeError(f"invalid inherited endpoint: {name}") from error
    if (
        endpoint.scheme
        or endpoint.path
        or endpoint.query
        or endpoint.fragment
        or endpoint.username is not None
        or endpoint.password is not None
        or endpoint.hostname != "127.0.0.1"
        or port is None
        or not 1 <= port <= 65535
    ):
        raise RuntimeError(f"invalid inherited endpoint: {name}")
    return socket.create_connection((endpoint.hostname, port))


def _read_token(endpoint: socket.socket) -> bytes:
    value = bytearray()
    try:
        while len(value) < WORKER_TOKEN_BYTES:
            chunk = endpoint.recv(WORKER_TOKEN_BYTES - len(value))
            if not chunk:
                break
            value.extend(chunk)
        extra = endpoint.recv(1)
    finally:
        endpoint.close()
    if len(value) != WORKER_TOKEN_BYTES or extra:
        raise RuntimeError("invalid inherited worker token")
    return bytes(value)


@dataclass(slots=True)
class WorkerContext:
    identity: WorkerIdentity
    session_dir: Path
    _callbacks: AdapterControlIoOwner
    _link: AdapterDaemonLink

    def emit_callback(self, payload: object) -> bool:
        return self._callbacks.submit(payload)

    def request(self, payload: object) -> object:
        return self._link.request("authority", payload, 15.0)


def _call_optional(driver: object, name: str, *arguments: object) -> object:
    operation = getattr(driver, name, None)
    return operation(*arguments) if callable(operation) else None


def _arguments(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="hyprial-adapter-worker")
    parser.add_argument("--instance-id", required=True)
    parser.add_argument("--kind", required=True)
    parser.add_argument("--generation", required=True, type=int)
    parser.add_argument("--protocol", required=True)
    parser.add_argument("--build", required=True)
    parser.add_argument("--capability-digest", required=True)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None, *, factory=None) -> int:
    """Run around a provider factory statically imported by the child command.

    A provider entry module calls ``main(factory=create_driver)``.  The daemon
    launches that module as a command, so no daemon-side code path can import
    the provider or any native dependency behind it.
    """

    if not callable(factory):
        raise TypeError("child worker entry requires a callable factory")
    arguments = _arguments(argv)
    identity = WorkerIdentity(
        arguments.instance_id, arguments.kind, arguments.generation
    )
    token = _read_token(_inherited_socket(TOKEN_FD_ENV_VAR))
    ready_socket = _inherited_socket(READY_FD_ENV_VAR)
    control_socket = _inherited_socket(CONTROL_FD_ENV_VAR)
    state_dir = Path(os.environ[STATE_DIR_ENV_VAR])
    shutdown = threading.Event()
    driver_box: dict[str, object] = {}
    link_box: dict[str, AdapterDaemonLink] = {}
    # P7: this child process creates exactly one ActorHost registry; both
    # callback owners below borrow it instead of each starting an engine.
    hosts = ActorHostCustody(None, owner="adapter-worker-entry")
    host = hosts.host

    def publish_callback(payload: object) -> None:
        link_box["link"].emit_callback(payload)

    callbacks = AdapterControlIoOwner(
        publish_callback,
        name=f"native-callback-{identity.instance_id}-{identity.generation}",
        actor_host=host,
    )
    context: WorkerContext

    def handle(control_type: str, payload: object) -> object:
        driver = driver_box["driver"]
        if control_type == "invoke":
            operation = getattr(driver, "handle_control", None)
            if not callable(operation):
                raise TypeError("native worker has no handle_control method")
            return operation(control_type, payload)
        if control_type == "close":
            _call_optional(driver, "close_admission")
            callbacks.close_admission()
            drain_timeout = 0.0
            if isinstance(payload, dict) and isinstance(payload.get("drainTimeoutMs"), int):
                drain_timeout = max(0.0, payload["drainTimeoutMs"] / 1000)
            native_drained = _call_optional(driver, "drain", drain_timeout)
            callback_drained = callbacks.drain(drain_timeout)
            _call_optional(driver, "close")
            return {
                "outcome": "closed",
                "drained": native_drained is not False and callback_drained,
            }
        raise ValueError(f"unsupported worker control type: {control_type}")

    session = NativeSessionWriterLock(
        state_dir, identity.kind, identity.instance_id, identity.generation
    )
    try:
        session.acquire(blocking=False)
        link = AdapterDaemonLink(
            control_socket,
            token,
            identity,
            request_handler=handle,
            closed_callback=shutdown.set,
            actor_host=host,
        )
        link_box["link"] = link
        context = WorkerContext(identity, session.directory, callbacks, link)
        driver = factory(context)
        driver_box["driver"] = driver
        _call_optional(driver, "start")
        send_worker_frame(
            ready_socket,
            build_ready_frame(
                identity,
                protocol=arguments.protocol,
                build=arguments.build,
                capability_digest=arguments.capability_digest,
            ),
            token,
        )
        ready_socket.close()

        def stop(_signal: int, _frame: object) -> None:
            shutdown.set()

        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        while not shutdown.wait(timeout=1.0):
            continue
        if not link.closed:
            _call_optional(driver, "close_admission")
            callbacks.close_admission()
            _call_optional(driver, "drain", 2.0)
            callbacks.drain(2.0)
            _call_optional(driver, "close")
        link.close()
        callbacks.close()
        return 0
    finally:
        try:
            ready_socket.close()
        except OSError:
            pass
        callbacks.close()
        hosts.shutdown(5.0)
        session.release()


if __name__ == "__main__":
    raise SystemExit(main())
