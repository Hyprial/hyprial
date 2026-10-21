"""Launch and supervision facts of one App Core host (D9).

These are facts about the single supervisor that spawned this host, not
about the facade itself: the ownership-neutral :class:`HostLaunch` seam, the
one-shot readiness callback and the owner watch.  They live beside
:mod:`host` so the serving module stays inside the structural line limit;
nothing here imports the serving module at runtime, and no function ever
resolves a process by name.

The supervision helpers are off unless the starting owner passes them
explicitly (an environment pair for the one-shot readiness callback, a PID
for the owner watch).  The standalone CLI composition sets neither, so an
ordinary ``hyprial ... app-core-host`` run keeps its exact old behaviour.
"""

from __future__ import annotations

import json
import os
import signal
import socket
import sys
import threading
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any

#: Starting-owner readiness callback (loopback, one JSON line, random token).
READY_PORT_ENV = "HYPRIAL_APP_CORE_READY_PORT"
READY_TOKEN_ENV = "HYPRIAL_APP_CORE_READY_TOKEN"

#: The launching owner's PID; the host stops when that process disappears.
OWNER_PID_ENV = "HYPRIAL_APP_CORE_OWNER_PID"

#: Per-launch capability for the Windows local loopback TCP arm (the desktop
#: AF_UNIX arm never needs it: its parent directory is 0700 and no other local
#: user can reach the socket).  The starting owner puts this value in the
#: host's own child environment and every frame must repeat it; it never
#: reaches the page.
REQUEST_TOKEN_ENV = "HYPRIAL_APP_CORE_REQUEST_TOKEN"


def request_token() -> str | None:
    """The local loopback request token this launch requires, when set."""

    raw = os.environ.get(REQUEST_TOKEN_ENV)
    if raw is None:
        return None
    if not raw:
        raise ValueError(f"{REQUEST_TOKEN_ENV} must not be empty")
    return raw


def readiness_callback() -> tuple[int, str] | None:
    """The starting owner's one-shot readiness callback, when requested."""

    raw_port = os.environ.get(READY_PORT_ENV)
    token = os.environ.get(READY_TOKEN_ENV)
    if raw_port is None and token is None:
        return None
    if not raw_port or not token:
        raise ValueError(f"{READY_PORT_ENV} and {READY_TOKEN_ENV} must be set together")
    try:
        port = int(raw_port)
    except ValueError as error:
        raise ValueError(f"{READY_PORT_ENV} must be an integer") from error
    if not 1 <= port <= 65535:
        raise ValueError(f"{READY_PORT_ENV} is out of range")
    return port, token


def owner_pid() -> int | None:
    """The launching owner this host must not outlive, when requested."""

    raw = os.environ.get(OWNER_PID_ENV)
    if raw is None:
        return None
    try:
        pid = int(raw)
    except ValueError as error:
        raise ValueError(f"{OWNER_PID_ENV} must be an integer") from error
    if pid <= 0:
        raise ValueError(f"{OWNER_PID_ENV} is out of range")
    return pid


#: The host's own socket file name under a state directory.
HOST_SOCKET_NAME = "app-core-host.sock"

#: The module entry point a launcher runs under its own Python interpreter.
HOST_MODULE = "hyprial.shell.impl.gui.adapter.host"

#: The stop request the host adopts as its clean stop on both facade arms.
HOST_STOP_SIGNAL = "SIGTERM"

#: The environment variable a non-Tauri launcher reads to reach the AF_UNIX
#: host.  The desktop shell publishes its endpoint as managed state instead.
HOST_SOCKET_ENV = "HYPRIAL_APP_CORE_SOCKET"

#: The only address the TCP facade ever binds; remote reachability is the
#: network exposure's business, never a wider bind here.
TCP_LOOPBACK_HOST = "127.0.0.1"


def host_socket_path(state_dir: Path) -> Path:
    """The composition root's conventional socket for one state directory.

    The host never guesses an app's private layout: the operator (or, later,
    the transport slice that owns process spawning) names a state directory,
    and the socket lives inside it -- one state directory owns one host, so a
    second launch against the same state is refused rather than stealing the
    live socket, and two states never collide.
    """

    return Path(state_dir) / HOST_SOCKET_NAME


@dataclass(frozen=True, slots=True)
class HostLaunch:
    """Ownership-neutral parameters for starting one adapter host.

    This seam pins what the host needs -- the interpreter-prefixed command,
    the listener mode and ports, the child environment, and the lifecycle
    facts (``HOST_STOP_SIGNAL`` plus :attr:`readiness_target`) -- without
    deciding who starts or supervises the process; that ownership stays
    decision D9's.  ``main`` builds one from the CLI, while a future launcher
    (desktop shell, ops script, e2e harness) can build the same value and
    consume the same facts.

    The N-A2 gate is deliberately not a launch parameter: an ``AuthGate`` is
    an in-process object passed explicitly to :func:`serve_host` (default
    ``None``), never a serialized knob, so a launcher cannot turn it on by
    accident and no config key grows for it.
    """

    config_path: Path
    socket_path: Path | None = None
    state_dir: Path | None = None
    tcp_port: int | None = None
    expose_port: int | None = None
    environment: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "config_path", Path(self.config_path))
        if self.socket_path is not None:
            object.__setattr__(self, "socket_path", Path(self.socket_path))
        if self.state_dir is not None:
            object.__setattr__(self, "state_dir", Path(self.state_dir))
        environment = dict(self.environment)
        if not all(
            isinstance(key, str) and isinstance(value, str)
            for key, value in environment.items()
        ):
            raise ValueError("environment keys and values must be strings")
        object.__setattr__(self, "environment", MappingProxyType(environment))

        listeners = (
            self.socket_path is not None,
            self.state_dir is not None,
            self.tcp_port is not None,
        )
        if sum(listeners) != 1:
            raise ValueError(
                "exactly one of socket_path, state_dir and tcp_port is required"
            )
        if self.tcp_port is None and self.expose_port is not None:
            raise ValueError("expose_port is only valid with tcp_port")
        if self.tcp_port is not None and (
            isinstance(self.tcp_port, bool)
            or not isinstance(self.tcp_port, int)
            or not 0 <= self.tcp_port <= 65535
        ):
            raise ValueError("tcp_port must be an integer in 0..65535")
        if self.expose_port is not None and (
            isinstance(self.expose_port, bool)
            or not isinstance(self.expose_port, int)
            or not 1 <= self.expose_port <= 65535
        ):
            raise ValueError("expose_port must be an integer in 1..65535")

    @property
    def remote(self) -> bool:
        """True when this launch serves the exposed remote facade."""

        return self.expose_port is not None

    @property
    def local_tcp(self) -> bool:
        """True for the Windows local arm: loopback TCP with no exposure."""

        return self.tcp_port is not None and self.expose_port is None

    @property
    def resolved_socket_path(self) -> Path | None:
        """The AF_UNIX path this launch serves, when it is a desktop mode."""

        if self.socket_path is not None:
            return self.socket_path
        if self.state_dir is not None:
            return host_socket_path(self.state_dir)
        return None

    def argv(self, *, python: str | None = None) -> list[str]:
        """The exact command a launcher runs under its own interpreter.

        The interpreter is a parameter because ownership differs (the
        product bundles one, dev/e2e uses ``uv``); the mode arguments are
        the host CLI's own contract and stay in one place here.
        """

        command = [sys.executable if python is None else python, "-m", HOST_MODULE]
        if self.socket_path is not None:
            command += ["--socket", str(self.socket_path)]
        elif self.state_dir is not None:
            command += ["--state-dir", str(self.state_dir)]
        else:
            command += ["--tcp-port", str(self.tcp_port)]
            if self.expose_port is not None:
                command += ["--expose-port", str(self.expose_port)]
        command += ["--config", str(self.config_path)]
        return command

    def child_environment(self, base: Mapping[str, str] | None = None) -> dict[str, str]:
        """The host process environment: launch values win over the base."""

        merged = dict(base or {})
        merged.update(self.environment)
        return merged

    def consumer_environment(self) -> dict[str, str]:
        """The desktop consumer's environment mapping (legacy seam).

        The Tauri shell does **not** consume this: it publishes the endpoint
        as process-local managed state and never puts it in a GUI child's
        environment. This mapping remains for non-Tauri launchers and for the
        launch-contract tests; it is not the shell's authoritative handoff.
        """

        socket_path = self.resolved_socket_path
        return {} if socket_path is None else {HOST_SOCKET_ENV: str(socket_path)}

    @property
    def readiness_target(self) -> Path | tuple[str, int]:
        """Where a launcher's readiness probe connects (a read-only probe)."""

        socket_path = self.resolved_socket_path
        if socket_path is not None:
            return socket_path
        assert self.tcp_port is not None
        return (TCP_LOOPBACK_HOST, self.tcp_port)


def probe_facade(
    launch: HostLaunch,
    *,
    max_line_bytes: int,
    target: Path | tuple[str, int] | None = None,
) -> None:
    """Round-trip one harmless frame through the facade just bound.

    The request names an unknown operation on purpose: the answer proves the
    listener accepts and frames without touching the daemon or the adapter's
    business surface.  ``target`` overrides ``launch.readiness_target`` for
    the local TCP arm whose ephemeral port is only known after bind.
    """

    target = launch.readiness_target if target is None else target
    if isinstance(target, tuple):
        connection = socket.create_connection(target, timeout=2.0)
    else:
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        connection.settimeout(2.0)
        connection.connect(str(target))
    with connection:
        connection.sendall(b'{"operation":"__readiness_probe__"}\n')
        answer = connection.makefile("rb").readline(max_line_bytes + 1)
    if not answer.endswith(b"\n"):
        raise RuntimeError("facade closed the readiness probe without an answer")
    try:
        decoded = json.loads(answer)
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise RuntimeError("facade readiness answer is not JSON") from error
    if not isinstance(decoded, Mapping) or decoded.get("ok") is not False:
        raise RuntimeError("facade readiness answer is not a refusal frame")


def start_readiness_reporter(
    launch: HostLaunch,
    ready: threading.Event,
    callback: tuple[int, str] | None,
    *,
    max_line_bytes: int,
    bound: list[tuple[str, int]] | None = None,
) -> threading.Thread | None:
    """Report readiness once, after the listener is bound.

    The wait is deliberately unbounded here: the starting owner already holds
    the single readiness deadline and kills this process when it expires.
    Adding a second budget here would be the self-built supervision the
    supervision-boundary standard forbids.  ``bound`` carries the effective
    address ``serve_tcp`` reported to ``on_bound``; the local TCP arm has no
    usable ``launch.readiness_target`` when its port is ``0``.
    """

    if callback is None:
        return None
    port, token = callback

    def report() -> None:
        ready.wait()
        message: dict[str, Any] = {
            "token": token,
            "probe": "ok",
            "socket": str(launch.resolved_socket_path or ""),
        }
        probe_target: Path | tuple[str, int] | None = None
        if launch.local_tcp:
            if not bound:
                message["probe"] = "failed"
                message["error"] = "the local TCP listener reported no bound address"
            else:
                host, effective = bound[-1]
                probe_target = (host, effective)
                message["tcpHost"] = host
                message["tcpPort"] = effective
        try:
            probe_facade(
                launch, max_line_bytes=max_line_bytes, target=probe_target
            )
        except Exception as error:  # noqa: BLE001 - reported, never fatal
            message["probe"] = "failed"
            message["error"] = str(error)
        try:
            with socket.create_connection(
                ("127.0.0.1", port), timeout=5.0
            ) as callback_socket:
                callback_socket.sendall(
                    json.dumps(message, separators=(",", ":")).encode() + b"\n"
                )
        except OSError as error:
            print(f"app-core readiness callback failed: {error}", file=sys.stderr)

    thread = threading.Thread(target=report, name="app-core-readiness", daemon=True)
    thread.start()
    return thread


def start_owner_watchdog(
    stop: threading.Event, owner_pid: int | None
) -> threading.Thread | None:
    """Stop serving when the launching owner disappears.

    Readiness by PID (the process this shell spawned us from), never by a
    process name.  POSIX only: on Windows ``os.kill(pid, 0)`` would terminate
    the owner, so the watchdog is deliberately absent there (NOT_RUN).
    """

    if owner_pid is None or os.name != "posix":
        return None

    def watch() -> None:
        while not stop.is_set():
            try:
                os.kill(owner_pid, 0)
            except ProcessLookupError:
                print(
                    "app-core owner process is gone; stopping",
                    file=sys.stderr,
                    flush=True,
                )
                # The same clean unwind a supervisor's SIGTERM takes, so the
                # socket (or exposure) is removed, not left to a stale file.
                os.kill(os.getpid(), signal.SIGTERM)
                return
            except PermissionError:
                # The owner exists but is not signalable by this process.
                pass
            stop.wait(1.0)

    thread = threading.Thread(target=watch, name="app-core-owner-watch", daemon=True)
    thread.start()
    return thread
