"""Local adapter host: the composition root behind the desktop Core bridge.

The desktop shell injects ``window.__hyprialAppCore`` into the static Core
page; the injected port speaks one newline-delimited JSON request per call
over an AF_UNIX socket to this process.  This module is that process's
narrow entry: it builds the :class:`AppChatAdapter` from an explicit
configuration (the composition root decides the fixed sender, the session
bindings and the daemon request callable -- never the page) and serves the
five named operations on it.

Dev/desktop usage::

    python -m hyprial.shell.impl.gui.adapter.host --socket PATH --config FILE

with a JSON config shaped like::

    {"sender": "agent:<owner>:<machine>:<agent>",
     "sessionRef": "<optional managed-worker fence>",
     "sessions": {"<id>": {"targets": ["agent:..."], "conversationId": "..."}}}

Who starts and owns this process is deliberately out of scope here -- that
belongs to the desktop transport slice (#1321 and later).  This host only
answers on the socket it is given, and it holds no authority of its own:
every answer is the adapter's, and the adapter's answers are the daemon's.

Remote-facade usage (loopback only; the tailnet exposure itself is driven
outside this module)::

    python -m hyprial.shell.impl.gui.adapter.host --tcp-port PORT --config FILE

``serve_tcp`` speaks the identical frame protocol on ``127.0.0.1`` -- the
only address this module ever binds -- so the remote leg reaches the same
``AppChatAdapter`` without a second contract.  The connection boundary
semantics (line framing, half packets, the line bound, the idle timeout and
disconnect handling) are frozen in :func:`_serve_tcp_connection`.

Composition-root conventions (this module's own minimal slice of the
lifecycle; who *spawns* the process stays the transport slice's business):

- ``--state-dir DIR`` puts the socket at ``DIR/app-core-host.sock``
  (:func:`host_socket_path`) -- one state directory owns one host;
- a live host on the same socket is never stolen: a second start refuses
  loudly instead of unlinking the running listener;
- a stale socket file (nothing answering) is replaced, and a clean stop
  removes the socket in ``serve``'s finally block;
- with no host reachable the Core page stays visibly failed (proven by the
  desktop E2E), so a missing host is a visible state, never a silent one.
"""

from __future__ import annotations

import argparse
import json
import socket
import sys
import threading
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from hyprial.shell.impl.gui.adapter.chat import (
    AppChatAdapter,
    SessionBinding,
)
from hyprial.shell.impl.gui.adapter.envelope import AuthGate
from hyprial.shell.impl.gui.adapter.errors import (
    AppAdapterError,
    AppAdapterUnavailable,
    INVALID_ARGUMENT,
    UNSUPPORTED_OPERATION,
)


def adapter_from_config(
    config: object, request: Callable[[str, dict], Any]
) -> AppChatAdapter:
    """Build the adapter exactly as the composition root configured it."""

    if not isinstance(config, Mapping):
        raise ValueError("adapter host config must be an object")
    raw_sessions = config.get("sessions")
    if not isinstance(raw_sessions, Mapping):
        raise ValueError("adapter host config needs a sessions object")
    sessions: dict[str, SessionBinding] = {}
    for session_id, entry in raw_sessions.items():
        if not isinstance(session_id, str) or not session_id:
            raise ValueError("session ids must be non-empty strings")
        if not isinstance(entry, Mapping):
            raise ValueError(f"session {session_id!r} config must be an object")
        targets = entry.get("targets")
        if (
            not isinstance(targets, list)
            or not targets
            or not all(isinstance(target, str) and target for target in targets)
        ):
            raise ValueError(
                f"session {session_id!r} targets must be a non-empty array of strings"
            )
        conversation_id = entry.get("conversationId")
        if not isinstance(conversation_id, str) or not conversation_id:
            raise ValueError(f"session {session_id!r} needs a conversationId")
        sessions[session_id] = SessionBinding(
            targets=tuple(targets), conversation_id=conversation_id
        )
    session_ref = config.get("sessionRef")
    return AppChatAdapter(
        request=request,
        sender=config.get("sender"),
        sessions=sessions,
        session_ref=session_ref
        if isinstance(session_ref, str) and session_ref
        else None,
    )


def daemon_request_callable() -> Callable[[str, dict], Any]:
    """The production daemon request callable: the CLI's own client.

    Importing the CLI runtime entry registers the service resolver the
    client needs; the wrapper maps the CLI's definitive refusals
    (``CliError``) onto the daemon-request error the adapter already
    understands, so a refused call is a rejection frame, never a lost
    connection.
    """

    from hyprial.kernel import DaemonRequestError
    from hyprial.shell.impl.cli.commands.common.support import (
        CliError,
        _daemon_request,
    )
    from hyprial.shell.impl.cli.runtime import entry as _entry  # noqa: F401

    def request(method: str, params: dict) -> Any:
        try:
            return _daemon_request(method, params)
        except CliError as error:
            raise DaemonRequestError(error.code, str(error)) from error

    return request


def _session_id(params: Mapping[str, Any]) -> str:
    session_id = params.get("sessionId")
    if not isinstance(session_id, str) or not session_id:
        raise AppAdapterError(
            INVALID_ARGUMENT, "sessionId must be a non-empty string"
        )
    return session_id


def _error(
    code: str,
    message: str,
    *,
    definitive: bool,
    details: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code, "message": message, "definitive": definitive}
    if details:
        # The gate's refusal reasons name fields/owners a client can act on;
        # the ungated frame shape is untouched (details stays absent there).
        error["details"] = dict(details)
    return {"ok": False, "error": error}


def handle_request(
    adapter: AppChatAdapter,
    request: object,
    *,
    gate: AuthGate | None = None,
) -> dict[str, Any]:
    """Answer one bridge frame; never raises -- every outcome is a frame.

    When ``gate`` is configured (the remote facade's explicit opt-in), the
    N-A2 envelope and the authorization matrix run FIRST, on the raw frame,
    before any adapter operation is touched; a refusal is one definitive
    error frame.  Without a gate this function behaves exactly as before --
    the desktop AF_UNIX path never passes one.
    """

    if not isinstance(request, Mapping):
        return _error(INVALID_ARGUMENT, "request must be an object", definitive=True)
    if gate is not None:
        try:
            verified = gate.verify(request)
            session_id = verified.session_id
            binding = adapter.session_binding(session_id) if session_id else None
            gate.authorize(verified, binding)
        except AppAdapterError as error:
            details = error.details if isinstance(error.details, Mapping) else None
            reason = details.get("reason") if isinstance(details, Mapping) else None
            if isinstance(reason, str) and reason:
                # The facade's refusal telemetry: one line per rejected
                # request, carrying the frozen reference reason; the frame
                # keeps this facade's own code vocabulary.
                print(
                    f"app-core-auth-refused reason={reason} code={error.code}",
                    file=sys.stderr,
                    flush=True,
                )
            return _error(
                error.code,
                error.message,
                definitive=True,
                details=details,
            )
    operation = request.get("operation")
    raw_params = request.get("params")
    params: Mapping[str, Any] = raw_params if isinstance(raw_params, Mapping) else {}
    try:
        if operation == "capabilities.get":
            return {"ok": True, "result": adapter.capabilities()}
        if operation == "sessions.list":
            return {"ok": True, "result": adapter.sessions_list()}
        if operation == "sessions.events":
            return {
                "ok": True,
                "result": adapter.events(_session_id(params), params.get("since")),
            }
        if operation == "sessions.send":
            send_request = params.get("request")
            if not isinstance(send_request, Mapping):
                raise AppAdapterError(
                    INVALID_ARGUMENT, "sessions.send needs a request object"
                )
            return {
                "ok": True,
                "result": adapter.send(_session_id(params), send_request),
            }
        if operation == "sessions.history":
            history_args = params.get("args")
            args: Mapping[str, Any] = (
                history_args if isinstance(history_args, Mapping) else {}
            )
            return {
                "ok": True,
                "result": adapter.history(
                    _session_id(params),
                    cursor=args.get("cursor"),
                    limit=args.get("limit"),
                ),
            }
        return _error(
            UNSUPPORTED_OPERATION,
            f"unknown operation {operation!r}",
            definitive=True,
        )
    except AppAdapterError as error:
        return _error(error.code, error.message, definitive=True)
    except AppAdapterUnavailable as error:
        return _error("unavailable", str(error), definitive=False)


#: Largest single request line the TCP facade answers, newline included.  A
#: client that overruns it gets one definitive refusal frame and the
#: connection is closed -- a stream with an unread tail cannot be re-aligned.
MAX_REQUEST_LINE_BYTES = 1_048_576

#: Idle seconds after which a connection without a complete request line is
#: closed, bounding how long one silent client can hold the facade's single
#: connection slot.
TCP_IDLE_TIMEOUT_SECONDS = 30.0

#: The only address the TCP facade ever binds; remote reachability is the
#: network exposure's business, never a wider bind here.
TCP_LOOPBACK_HOST = "127.0.0.1"


def _send_line(connection: socket.socket, response: dict[str, Any]) -> bool:
    """One response frame; ``False`` when the peer is already gone."""

    payload = json.dumps(response, separators=(",", ":")).encode() + b"\n"
    try:
        connection.sendall(payload)
        return True
    except OSError:
        return False


def _serve_tcp_connection(
    connection: socket.socket,
    adapter: AppChatAdapter,
    idle_timeout: float,
    gate: AuthGate | None = None,
) -> None:
    """One TCP facade connection, with the boundary semantics frozen here.

    - framing: newline-delimited JSON, one request per complete line; every
      line gets exactly one response line, in arrival order; a connection may
      carry any number of sequential requests (handling is strictly
      sequential -- no pipelining promise beyond arrival order);
    - partial line ("half packet"): the reader waits for the newline; nothing
      is answered early, and a silent connection is closed after
      ``idle_timeout`` seconds;
    - oversize line: a line the reader could not fit in
      ``MAX_REQUEST_LINE_BYTES`` is refused with one definitive frame and the
      connection is closed;
    - disconnect: end of stream (mid-line or between lines) ends the
      connection -- a line that never completed is owed nothing, never
      answered with a guess; a write to a vanished peer is swallowed.  No
      failure of one connection may reach the listener.
    """

    connection.settimeout(idle_timeout)
    reader = connection.makefile("rb")
    while True:
        try:
            chunk = reader.readline(MAX_REQUEST_LINE_BYTES + 1)
        except OSError:  # includes socket timeout and peer resets
            return
        if not chunk:
            return
        if not chunk.endswith(b"\n"):
            # readline returns a newline-less chunk in exactly two cases: the
            # line overran the bound, or the stream ended mid-line.  Only the
            # first is the caller's to hear about.
            if len(chunk) > MAX_REQUEST_LINE_BYTES:
                _send_line(
                    connection,
                    _error(
                        INVALID_ARGUMENT,
                        f"request line exceeds {MAX_REQUEST_LINE_BYTES} bytes",
                        definitive=True,
                    ),
                )
            return
        try:
            request = json.loads(chunk)
        except (json.JSONDecodeError, UnicodeDecodeError):
            response = _error(
                INVALID_ARGUMENT,
                "request line is not JSON",
                definitive=True,
            )
        else:
            try:
                response = handle_request(adapter, request, gate=gate)
            except Exception as error:  # noqa: BLE001 - one frame must never kill the host
                response = _error("internal", str(error), definitive=False)
        if not _send_line(connection, response):
            return


def serve_tcp(
    port: int,
    adapter: AppChatAdapter,
    *,
    ready: threading.Event | None = None,
    stop: threading.Event | None = None,
    on_bound: Callable[[str, int], None] | None = None,
    idle_timeout: float = TCP_IDLE_TIMEOUT_SECONDS,
    auth_gate: AuthGate | None = None,
) -> None:
    """Serve the same frames as :func:`serve` on loopback TCP.

    Loopback-only by construction: this module binds ``127.0.0.1`` and nothing
    else, because remote reachability must come from the node's own tailnet
    exposure -- a wider bind here would be a second, unauthenticated door.
    Connections are accepted one at a time and served to completion (or idle
    timeout); a client needing another connection opens one, the same shape
    the desktop relay already uses on AF_UNIX.  Port ``0`` binds an ephemeral
    port; ``on_bound(host, port)`` receives the effective address once the
    listener is up.  ``auth_gate`` is the explicit N-A2 opt-in of the remote
    facade; ``serve`` (desktop AF_UNIX) never takes one.
    """

    if isinstance(port, bool) or not isinstance(port, int) or not 0 <= port <= 65535:
        raise ValueError("port must be an integer in 0..65535")
    if idle_timeout <= 0:
        raise ValueError("idle_timeout must be positive seconds")
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind((TCP_LOOPBACK_HOST, port))
    server.listen(8)
    server.settimeout(0.2)
    if on_bound is not None:
        bound_host, bound_port = server.getsockname()[:2]
        on_bound(str(bound_host), int(bound_port))
    if ready is not None:
        ready.set()
    try:
        while stop is None or not stop.is_set():
            try:
                connection, _ = server.accept()
            except TimeoutError:
                continue
            except OSError:
                break
            with connection:
                _serve_tcp_connection(connection, adapter, idle_timeout, auth_gate)
    finally:
        server.close()


#: The host's own socket file name under a state directory.
HOST_SOCKET_NAME = "app-core-host.sock"


def host_socket_path(state_dir: Path) -> Path:
    """The composition root's conventional socket for one state directory.

    The host never guesses an app's private layout: the operator (or, later,
    the transport slice that owns process spawning) names a state directory,
    and the socket lives inside it -- one state directory owns one host, so a
    second launch against the same state is refused rather than stealing the
    live socket, and two states never collide.
    """

    return Path(state_dir) / HOST_SOCKET_NAME


def _socket_is_live(socket_path: Path) -> bool:
    """True when something answers on the AF_UNIX path right now."""

    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        probe.settimeout(0.5)
        return probe.connect_ex(str(socket_path)) == 0
    finally:
        probe.close()


def serve(
    socket_path: Path,
    adapter: AppChatAdapter,
    *,
    ready: threading.Event | None = None,
    stop: threading.Event | None = None,
) -> None:
    """Serve newline-delimited JSON frames on ``socket_path`` until stopped.

    Single instance per path: a live host on the same path is never unlinked
    or replaced -- this raises instead.  A stale socket file (nothing answers
    on it) is replaced as before, and the finally block removes the socket on
    exit, so a clean stop leaves nothing behind for the next launch.
    """

    if socket_path.exists() and _socket_is_live(socket_path):
        raise RuntimeError(
            f"another adapter host is already serving on {socket_path}; "
            "one state directory owns one host (stop it first)"
        )
    socket_path.unlink(missing_ok=True)
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(socket_path))
    server.listen(4)
    server.settimeout(0.2)
    if ready is not None:
        ready.set()
    try:
        while stop is None or not stop.is_set():
            try:
                connection, _ = server.accept()
            except TimeoutError:
                continue
            with connection:
                reader = connection.makefile("rb")
                for line in reader:
                    try:
                        request = json.loads(line)
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        response = _error(
                            INVALID_ARGUMENT,
                            "request line is not JSON",
                            definitive=True,
                        )
                    else:
                        try:
                            response = handle_request(adapter, request)
                        except Exception as error:  # noqa: BLE001 - one frame must never kill the host
                            response = _error(
                                "internal", str(error), definitive=False
                            )
                    payload = json.dumps(response, separators=(",", ":"))
                    connection.sendall(payload.encode() + b"\n")
    finally:
        server.close()
        socket_path.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Serve the App chat adapter for the desktop Core bridge."
    )
    parser.add_argument("--socket", help="AF_UNIX socket path (desktop)")
    parser.add_argument(
        "--state-dir",
        help="adapter state directory (socket at <state-dir>/app-core-host.sock)",
    )
    parser.add_argument(
        "--tcp-port",
        type=int,
        help="loopback TCP port (remote facade; 0 picks an ephemeral port)",
    )
    parser.add_argument("--config", required=True, help="composition config JSON")
    args = parser.parse_args(argv)
    listeners = (
        args.socket is not None,
        args.state_dir is not None,
        args.tcp_port is not None,
    )
    if sum(listeners) != 1:
        parser.error(
            "exactly one of --socket, --state-dir and --tcp-port is required"
        )
    config = json.loads(Path(args.config).read_text(encoding="utf-8"))
    adapter = adapter_from_config(config, daemon_request_callable())
    if args.tcp_port is not None:
        serve_tcp(args.tcp_port, adapter)
        return 0
    socket_path = (
        host_socket_path(Path(args.state_dir))
        if args.state_dir is not None
        else Path(args.socket)
    )
    if args.state_dir is not None:
        Path(args.state_dir).mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        serve(socket_path, adapter)
    except RuntimeError as error:
        print(f"host refused to start: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
