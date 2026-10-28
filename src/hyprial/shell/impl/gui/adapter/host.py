"""Local adapter host: the composition root behind the desktop Core bridge.

The desktop shell injects ``window.__hyprialAppCore`` into the static Core
page; the injected port speaks one newline-delimited JSON request per call
over an AF_UNIX socket to this process.  This module is that process's
narrow entry: it builds the :class:`AppChatAdapter` from an explicit
configuration (the composition root decides the fixed sender, the session
bindings and the daemon request callable -- never the page) and serves the
named operations on it (``sessions.list/history/events/send/create``,
``targets.list`` and ``capabilities.get``).

Dev/desktop usage::

    python -m hyprial.shell.impl.gui.adapter.host --socket PATH --config FILE

with a JSON config shaped like::

    {"sender": "agent:<owner>:<machine>:<agent>",
     "sessionRef": "<optional managed-worker fence>",
     "sessions": {"<id>": {"targets": ["agent:..."], "conversationId": "..."}}}

Who starts and owns this process belongs to the desktop runtime. The old
Tauri shell (``app/src-tauri``) was retired on 2026-10-27; the replacement
native shell will live under ``clients/native`` and spawn this host as a
supervised child, complete the bounded readiness handshake and publish the
endpoint as process-local managed state. This host only answers on the
address it is given, and it holds no authority of its own: every answer is
the adapter's, and the adapter's answers are the daemon's.

Remote-facade usage (loopback bind; the tailnet exposure is composed in
here through the daemon's own ``network.expose`` route)::

    python -m hyprial.shell.impl.gui.adapter.host \
        --tcp-port PORT --expose-port TAILNET_PORT --config FILE

``serve_tcp`` speaks the identical frame protocol on ``127.0.0.1`` -- the
only address this module ever binds -- so the remote leg reaches the same
``AppChatAdapter`` without a second contract.  The connection boundary
semantics (line framing, half packets, the line bound, the idle timeout and
disconnect handling) are frozen in :func:`_serve_tcp_connection`.

Local loopback usage (Windows, where neither CPython nor the Rust shell has
AF_UNIX) is the same ``serve_tcp`` arm **without** an exposure port::

    python -m hyprial.shell.impl.gui.adapter.host \
        --tcp-port 0 --config FILE

Port ``0`` binds an ephemeral loopback port; readiness reports the effective
``tcpHost``/``tcpPort``.  A per-launch ``HYPRIAL_APP_CORE_REQUEST_TOKEN``
makes every frame name a capability only the owning shell knows, so another
local process cannot borrow the loopback door.  This arm never calls
``network.expose`` and is not the remote facade.

:func:`serve_tcp_exposed` is that composition: bind, then ask the daemon to
expose the effective loopback port to the tailnet, then serve, and remove
the exposure on a clean stop.  The exposure always carries the ``none``
proxy protocol -- the identity-carrying ``v2`` preface is only legal for a
private ``unix:`` target (any local process can forge it on a loopback TCP
port), which this process does not serve -- so the facade's own request
envelope remains the independent identity mechanism, wired separately.  A
refused expose stops the host before it serves anything; a refused unexpose
is named, never swallowed.

Composition-root conventions (this module's own minimal slice of the
lifecycle; who *spawns* the process stays the transport slice's business):

- ``--state-dir DIR`` puts the socket at ``DIR/app-core-host.sock``
  (:func:`host_socket_path`) -- one state directory owns one host;
- a live host on the same socket is never stolen: a second start refuses
  loudly instead of unlinking the running listener;
- a stale socket file (nothing answering) is replaced, and a clean stop
  removes the socket in ``serve``'s finally block;
- with no host reachable the Core page stays visibly failed (proven by the
  desktop E2E), so a missing host is a visible state, never a silent one;
- the remote facade refuses to start unexposed: a ``--tcp-port`` with
  ``--expose-port`` is a reachable facade -- or a loudly failed start, never
  a silently unreachable serve. Without ``--expose-port`` the same TCP arm is
  the **local** Windows transport and never calls ``network.expose``;
- both facade arms answer SIGTERM as the same clean stop Ctrl-C already is
  -- the composed ``finally`` still removes the exposure or the desktop
  socket -- so an ordinary supervisor stop cannot strand either;
- the launching shell can request a one-shot readiness callback
  (``HYPRIAL_APP_CORE_READY_PORT``/``_TOKEN``).  The host self-probes the
  facade it just bound and reports the token, the socket it serves and the
  probe outcome before the shell declares the host ready;
- the host watches the launching owner's PID (never a process name) and
  stops on the same clean path when that owner disappears, so an abnormal
  shell exit cannot leave an orphan listener;
- :class:`HostLaunch` is the ownership-neutral launch seam: it pins the
  command, child environment, listener/ports and lifecycle facts a
  launcher needs, without deciding who spawns the process (that remains
  the transport slice's business, decision D9);
- the N-A2 gate is an explicit in-process ``serve_host(auth_gate=...)``
  object; the remote arm refuses to bind without one.
"""

from __future__ import annotations

import argparse
import errno
import hmac
import json
import signal
import socket
import sys
import threading
import time
from collections.abc import Callable, Mapping
from contextlib import nullcontext
from pathlib import Path
from typing import Any

from hyprial.shell.impl.gui.adapter.chat import (
    IDENTITY_FIELDS,
    AppChatAdapter,
    SessionBinding,
)
from hyprial.shell.impl.gui.adapter.envelope import AuthGate
from hyprial.shell.impl.gui.adapter.mobile.wiring import build_auth_gate
from hyprial.shell.impl.gui.adapter.errors import (
    IDENTITY_REJECTED,
    AppAdapterError,
    AppAdapterUnavailable,
    INVALID_ARGUMENT,
    UNSUPPORTED_OPERATION,
)
from hyprial.shell.impl.gui.adapter.lifecycle import (
    HOST_MODULE as HOST_MODULE,
    HOST_SOCKET_ENV as HOST_SOCKET_ENV,
    HOST_SOCKET_NAME as HOST_SOCKET_NAME,
    HOST_STOP_SIGNAL as HOST_STOP_SIGNAL,
    TCP_LOOPBACK_HOST,
    HostLaunch as HostLaunch,
    host_socket_path as host_socket_path,
    owner_pid,
    readiness_callback,
    request_token,
    start_owner_watchdog,
    start_readiness_reporter,
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


#: Browser history frame: configured session id plus page selectors only.
_HISTORY_ENVELOPE_KEYS = frozenset({"sessionId", "args"})
_HISTORY_ARG_KEYS = frozenset({"cursor", "limit"})


def _history_args(params: Mapping[str, Any]) -> Mapping[str, Any]:
    """Validate the browser history frame; the adapter supplies identity."""

    unexpected = set(params) - _HISTORY_ENVELOPE_KEYS
    _refuse_history_fields(unexpected, "request")
    # An absent args object is the historical first-page call; a present but
    # non-object args is a malformed frame, not an empty page.
    raw_args = params.get("args", {})
    if not isinstance(raw_args, Mapping):
        raise AppAdapterError(
            INVALID_ARGUMENT, "sessions.history needs an args object"
        )
    unexpected_args = set(raw_args) - _HISTORY_ARG_KEYS
    _refuse_history_fields(unexpected_args, "args")
    return raw_args


def _refuse_history_fields(fields: set[str], where: str) -> None:
    identity = fields & IDENTITY_FIELDS
    if identity:
        raise AppAdapterError(
            IDENTITY_REJECTED,
            f"sessions.history {where} may not carry identity fields",
            {"identityFields": sorted(identity)},
        )
    if fields:
        raise AppAdapterError(
            INVALID_ARGUMENT,
            f"unexpected sessions.history {where} fields",
            {"unexpectedFields": sorted(fields)},
        )


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
    on_verified: Callable[[object], None] | None = None,
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
            if on_verified is not None:
                on_verified(verified)
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
    admission = gate.admission(verified) if gate is not None else nullcontext()
    try:
        with admission:
            if operation == "capabilities.get":
                return {"ok": True, "result": adapter.capabilities()}
            if operation == "sessions.list":
                listed = adapter.sessions_list()
                if gate is not None:
                    listed = gate.visible_sessions(verified, listed)
                return {"ok": True, "result": listed}
            if operation in ("targets.list", "sessions.create"):
                return {"ok": True, "result": adapter.directory_operation(operation, params)}
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
                if not isinstance(raw_params, Mapping):
                    raise AppAdapterError(
                        INVALID_ARGUMENT, "sessions.history needs a params object"
                    )
                args = _history_args(raw_params)
                return {
                    "ok": True,
                    "result": adapter.history(
                        _session_id(raw_params),
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
    request_token: str | None = None,
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
    - local capability: when ``request_token`` is set (the Windows local
      loopback arm), every well-formed frame must carry the same token
      before any operation is dispatched.
    """

    connection.settimeout(idle_timeout)
    reader = connection.makefile("rb")
    watch_started = False

    def remember_device(verified: object) -> None:
        # Start the revocation watch for this connection's device once.
        nonlocal watch_started
        device_id = getattr(verified, "device_id", None)
        if gate is None or watch_started or not isinstance(device_id, str):
            return
        watch_started = True

        def watch() -> None:
            while connection.fileno() >= 0:
                if not gate.connection_allowed(device_id):
                    try:
                        connection.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
                    return
                time.sleep(0.25)

        threading.Thread(
            target=watch, name="app-core-revoke-watch", daemon=True
        ).start()

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
            supplied = request.get("token") if isinstance(request, Mapping) else None
            if request_token is not None and (
                not isinstance(supplied, str)
                or not hmac.compare_digest(
                    supplied.encode("utf-8"), request_token.encode("utf-8")
                )
            ):
                response = _error(
                    "unauthorized",
                    "the local request token is missing or does not match this launch",
                    definitive=True,
                )
            else:
                try:
                    response = handle_request(
                        adapter, request, gate=gate, on_verified=remember_device
                    )
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
    request_token: str | None = None,
) -> None:
    """Serve the same frames as :func:`serve` on loopback TCP.

    Loopback-only by construction: this module binds ``127.0.0.1`` and nothing
    else, because remote reachability must come from the node's own tailnet
    exposure -- a wider bind here would be a second, unauthenticated door.
    Connections are accepted one at a time and served to completion (or idle
    timeout); a client needing another connection opens one, the same shape
    the desktop relay already uses on AF_UNIX.  Port ``0`` binds an ephemeral
    port; ``on_bound(host, port)`` receives the effective address once the
    listener is up -- and a failure raised there still closes the listener,
    so a refused composition never leaks a bound socket.  ``auth_gate`` is
    the explicit N-A2 opt-in of the remote facade; ``serve`` (desktop
    AF_UNIX) never takes one.  ``request_token`` is the Windows local
    arm's per-launch capability; the remote facade leaves it ``None`` and
    relies on its explicit gate.  The tailnet exposure is composed by
    :func:`serve_tcp_exposed`, not here.
    """

    if isinstance(port, bool) or not isinstance(port, int) or not 0 <= port <= 65535:
        raise ValueError("port must be an integer in 0..65535")
    if idle_timeout <= 0:
        raise ValueError("idle_timeout must be positive seconds")
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        try:
            server.bind((TCP_LOOPBACK_HOST, port))
        except OSError as error:
            if error.errno == errno.EADDRINUSE:
                raise RuntimeError(
                    f"cannot bind {TCP_LOOPBACK_HOST}:{port}: address already in "
                    "use; refusing to kill the occupant or choose another port"
                ) from error
            raise
        server.listen(8)
        server.settimeout(0.2)
        if on_bound is not None:
            bound_host, bound_port = server.getsockname()[:2]
            on_bound(str(bound_host), int(bound_port))
        if ready is not None:
            ready.set()
        while stop is None or not stop.is_set():
            try:
                connection, _ = server.accept()
            except TimeoutError:
                continue
            except OSError:
                break
            with connection:
                _serve_tcp_connection(
                    connection, adapter, idle_timeout, auth_gate, request_token
                )
    finally:
        server.close()


#: The proxy-protocol posture of the composed exposure.  The daemon (and the
#: sidecar behind it) only accepts the identity-carrying ``v2`` preface for a
#: private ``unix:`` target -- any local process can reach a loopback TCP
#: port and forge the header -- and this facade serves loopback TCP, so its
#: exposure is always the identity-less form.  The request-level envelope is
#: this facade's own identity mechanism; it is wired separately.
EXPOSURE_PROXY_PROTOCOL = "none"


class HostExposureError(RuntimeError):
    """One named exposure-lifecycle failure: the phase plus the daemon's code."""

    def __init__(self, phase: str, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.phase = phase
        self.code = code
        self.message = message


def _named_exposure_failure(phase: str, error: Exception) -> HostExposureError:
    """Map whatever the request callable raised onto one named refusal."""

    code = getattr(error, "code", None)
    if not isinstance(code, str) or not code:
        code = "EXPOSURE_FAILED"
    return HostExposureError(phase, code, str(error))


def serve_tcp_exposed(
    port: int,
    adapter: AppChatAdapter,
    *,
    expose_port: int,
    request: Callable[[str, dict], Any],
    ready: threading.Event | None = None,
    stop: threading.Event | None = None,
    idle_timeout: float = TCP_IDLE_TIMEOUT_SECONDS,
    auth_gate: AuthGate | None = None,
) -> None:
    """Serve the TCP facade with the tailnet exposure composed in.

    The order is the contract: bind loopback, expose the *effective* port
    through the daemon's own ``network.expose`` route (only this process can
    name the port it actually got, ``0`` included), serve, and unexpose on
    the way out -- including a stop triggered while serving.

    Every refusal is named and loud:

    - an expose the daemon refuses raises :class:`HostExposureError` with
      phase ``expose`` before any frame is served -- the facade never runs
      unexposed, and the listener is already closed by :func:`serve_tcp`;
    - an expose answer that does not confirm the requested exposure is the
      same loud refusal under code ``INVALID_RESPONSE``;
    - an unexpose failure after the listener stopped raises with phase
      ``cleanup`` (the daemon's persistent desired set still holds the
      entry, so the operator must clear it with ``network unexpose``);
    - with no clean stop, the exposure stays in the daemon's desired set;
      re-running with the same port and target is idempotent, a different
      target is refused by the daemon's own rule.
    """

    if auth_gate is None:
        raise ValueError(
            "the remote facade requires an auth gate; refusing to bind or "
            "expose an unauthenticated listener"
        )
    if (
        isinstance(expose_port, bool)
        or not isinstance(expose_port, int)
        or not 1 <= expose_port <= 65535
    ):
        raise ValueError("expose_port must be an integer in 1..65535")
    applied: dict[str, Any] = {}

    def on_bound(host: str, bound_port: int) -> None:
        target = f"tcp:{host}:{bound_port}"
        try:
            result = request(
                "network.expose",
                {
                    "port": expose_port,
                    "target": target,
                    "proxyProtocol": EXPOSURE_PROXY_PROTOCOL,
                },
            )
        except Exception as error:  # noqa: BLE001 - named, then re-raised
            raise _named_exposure_failure("expose", error) from error
        exposure = result.get("exposure") if isinstance(result, Mapping) else None
        if (
            not isinstance(exposure, Mapping)
            or exposure.get("port") != expose_port
            or exposure.get("target") != target
            or exposure.get("proxyProtocol") != EXPOSURE_PROXY_PROTOCOL
        ):
            raise HostExposureError(
                "expose",
                "INVALID_RESPONSE",
                "network.expose did not confirm the requested exposure",
            )
        applied.update(exposure)

    try:
        serve_tcp(
            port,
            adapter,
            ready=ready,
            stop=stop,
            on_bound=on_bound,
            idle_timeout=idle_timeout,
            auth_gate=auth_gate,
        )
    finally:
        if applied:
            try:
                request("network.unexpose", {"port": expose_port})
            except Exception as error:  # noqa: BLE001 - named, then re-raised
                raise _named_exposure_failure("cleanup", error) from error


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
                try:
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
                except OSError:
                    # A client that resets or aborts mid-frame must not take the
                    # single instance down; the next connection is still served.
                    continue
    finally:
        server.close()
        socket_path.unlink(missing_ok=True)


def serve_host(
    launch: HostLaunch,
    *,
    adapter: AppChatAdapter,
    request: Callable[[str, dict], Any],
    ready: threading.Event | None = None,
    stop: threading.Event | None = None,
    auth_gate: AuthGate | None = None,
    on_bound: Callable[[str, int], None] | None = None,
    request_token: str | None = None,
) -> None:
    """Serve one launch plan; no process ownership, no default gate.

    ``auth_gate`` is the N-A2 explicit opt-in.  The default (``None``) is
    exactly the old unauthenticated facade; an enabled gate applies the
    envelope and authorization matrix first, per :func:`handle_request`.
    The desktop AF_UNIX arm has no envelope boundary, so asking for a gate
    there is refused here instead of silently serving ungated requests under
    a gate's name.  The Windows local TCP arm is the same local trust domain:
    it takes no gate, and its per-launch ``request_token`` is enforced on
    every frame instead.
    """

    if launch.remote and auth_gate is None:
        raise ValueError(
            "the remote facade requires an auth gate from the durable access "
            "authority; refusing to expose an unauthenticated listener"
        )
    if auth_gate is not None and not launch.remote:
        raise ValueError(
            "the desktop AF_UNIX facade does not take an auth gate; "
            "the N-A2 envelope is a remote-facade seam"
        )
    if request_token is not None and not launch.local_tcp:
        raise ValueError(
            "the local request token is only valid on the loopback TCP arm"
        )
    if launch.remote:
        assert launch.tcp_port is not None and launch.expose_port is not None
        serve_tcp_exposed(
            launch.tcp_port,
            adapter,
            expose_port=launch.expose_port,
            request=request,
            ready=ready,
            stop=stop,
            auth_gate=auth_gate,
        )
        return
    if launch.local_tcp:
        assert launch.tcp_port is not None
        serve_tcp(
            launch.tcp_port,
            adapter,
            ready=ready,
            stop=stop,
            on_bound=on_bound,
            request_token=request_token,
        )
        return
    if launch.state_dir is not None:
        launch.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    socket_path = launch.resolved_socket_path
    assert socket_path is not None
    if ready is None and stop is None:
        # The CLI path keeps its exact old call shape; the events only exist
        # for an in-process launcher/test that owns the serving thread.
        serve(socket_path, adapter)
    else:
        serve(socket_path, adapter, ready=ready, stop=stop)


def _sigterm_as_clean_stop(_signum: int, _frame: Any) -> None:
    """Adopt SIGTERM as the same unwind the Ctrl-C stop already uses.

    Raising ``KeyboardInterrupt`` from the handler is exactly Python's own
    SIGINT shape, so the stop unwinds through the composed ``finally``
    (:func:`serve_tcp` closes the listener, :func:`serve_tcp_exposed` removes
    the exposure) and lands on :func:`main`'s clean-stop arm -- one stop
    path and one cleanup, whichever signal asked for it.
    """

    raise KeyboardInterrupt


def run_host(
    launch: HostLaunch,
    *,
    adapter: AppChatAdapter,
    request: Callable[[str, dict], Any],
    auth_gate: AuthGate | None = None,
) -> int:
    """The CLI serving lifecycle around :func:`serve_host`.

    Signals and named refusals stay here; the launch plan stays in
    :class:`HostLaunch`, and who spawned the process remains outside this
    module.  ``main`` builds the gate for the remote ``--expose-port`` arm and
    passes neither for the desktop or local-loopback arms.  Both arms adopt
    SIGTERM as the clean stop so an ordinary supervisor stop removes the
    exposure or the AF_UNIX socket.
    """

    try:
        callback = readiness_callback()
        launching_owner = owner_pid()
        local_token = request_token()
    except ValueError as error:
        print(f"host refused to start: {error}", file=sys.stderr)
        return 1
    if local_token is not None and not launch.local_tcp:
        print(
            "host refused to start: the local request token is only valid on "
            "the loopback TCP arm",
            file=sys.stderr,
        )
        return 1
    ready = threading.Event()
    stop = threading.Event()
    bound: list[tuple[str, int]] = []
    start_owner_watchdog(stop, launching_owner)
    start_readiness_reporter(
        launch,
        ready,
        callback,
        max_line_bytes=MAX_REQUEST_LINE_BYTES,
        bound=bound,
    )
    previous_sigterm = signal.signal(signal.SIGTERM, _sigterm_as_clean_stop)
    try:
        serve_host(
            launch,
            adapter=adapter,
            request=request,
            ready=ready,
            stop=stop,
            auth_gate=auth_gate,
            on_bound=lambda host, port: bound.append((host, port)),
            request_token=local_token,
        )
    except KeyboardInterrupt:
        # A stop while serving (Ctrl-C, or the SIGTERM adopted above): the
        # composed finally already removed the exposure/socket, so this is
        # the clean path, not a failure.
        return 0
    except HostExposureError as error:
        print(
            f"host exposure {error.phase} failed: {error.code}: {error.message}",
            file=sys.stderr,
        )
        return 1
    except RuntimeError as error:
        print(f"host refused to start: {error}", file=sys.stderr)
        return 1
    finally:
        signal.signal(signal.SIGTERM, previous_sigterm)
    return 0


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
        help=(
            "loopback TCP port (local desktop mode without AF_UNIX, or the "
            "remote facade with --expose-port; 0 picks an ephemeral port)"
        ),
    )
    parser.add_argument(
        "--expose-port",
        type=int,
        help="tailnet TCP port to expose the remote facade on (with --tcp-port)",
    )
    parser.add_argument("--config", required=True, help="composition config JSON")
    parser.add_argument(
        "--mobile-access-db",
        help="durable mobile-access SQLite path (required with --expose-port)",
    )
    parser.add_argument(
        "--node-id",
        help="this host's stable trusted node id (required with --expose-port)",
    )
    parser.add_argument(
        "--derive-command",
        help=(
            "request-key derive helper (required with --expose-port); the "
            "helper speaks the documented JSON stdin/stdout contract"
        ),
    )
    args = parser.parse_args(argv)
    remote_authority = (args.mobile_access_db, args.node_id, args.derive_command)
    if args.expose_port is not None:
        if any(value is None for value in remote_authority):
            parser.error(
                "--expose-port requires --mobile-access-db, --node-id and "
                "--derive-command; the remote facade is fail-closed"
            )
        try:
            auth_gate = build_auth_gate(
                Path(args.mobile_access_db),
                args.node_id,
                Path(args.derive_command),
            )
        except (OSError, ValueError) as error:
            parser.error(f"cannot compose the mobile access authority: {error}")
    else:
        if any(value is not None for value in remote_authority):
            parser.error(
                "--mobile-access-db/--node-id/--derive-command are only valid "
                "with --expose-port"
            )
        auth_gate = None
    try:
        launch = HostLaunch(
            config_path=Path(args.config),
            socket_path=Path(args.socket) if args.socket is not None else None,
            state_dir=Path(args.state_dir) if args.state_dir is not None else None,
            tcp_port=args.tcp_port,
            expose_port=args.expose_port,
        )
    except ValueError as error:
        parser.error(str(error))
    config = json.loads(launch.config_path.read_text(encoding="utf-8"))
    request = daemon_request_callable()
    adapter = adapter_from_config(config, request)
    return run_host(
        launch, adapter=adapter, request=request, auth_gate=auth_gate
    )


if __name__ == "__main__":
    raise SystemExit(main())
