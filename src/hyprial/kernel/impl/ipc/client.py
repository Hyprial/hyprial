"""Kernel-owned version-one daemon IPC client.

This module is the **only** implementation of the CLI daemon request
transport -- the client the CLI and its switched consumers share.  Endpoint
selection, the newline-delimited JSON frame, connect/backlog patience,
timeout, response classification, and the transport error vocabulary all live
here.  Shell/CLI code supplies an endpoint, a clock and a request-id factory;
it does not keep a second copy of these rules.

Other IPC clients in the tree are separate authorities, not covered by this
claim: the OrgFS HTTP webserver keeps its own request path and error mapping
(`daemon/impl/orgfs/webserver/ipc.py`), and worker/transfer channels keep
their own carriers.  This module does not unify them.

A write is never retried blindly.  The only retry in :func:`daemon_request`
is ``DAEMON_RESTORING``: the daemon's dispatch gate mints it *before* any
side effect, so waiting the refusal out cannot duplicate a mutation.  A
disconnect or timeout is reported to the caller, which owns the
"did my write land?" decision.
"""

from __future__ import annotations

from collections.abc import Callable
import errno
import json
import math
import os
from pathlib import Path
import socket
import time
from typing import Any
from uuid import uuid4 as _stdlib_uuid4

from hyprial.kernel.impl.contracts.daemon import ipc_errors
from hyprial.kernel.impl.contracts.ports import RequestPortError


JsonObject = dict[str, Any]


#: The daemon closed the connection at write or read time.  EPIPE is the
#: macOS spelling, ECONNRESET the Linux one; discriminate by errno, not by
#: exception class, so a third platform cannot introduce a third spelling.
_PEER_GONE_ERRNOS = frozenset({errno.EPIPE, errno.ECONNRESET})


#: Linux answers connect() to a full AF_UNIX accept queue with an immediate
#: EAGAIN even on a timeout socket; a patient caller must retry within its
#: budget or a healthy request looks starved.  EWOULDBLOCK is the same value
#: on POSIX but is listed so the intent survives a platform where they diverge.
_CONNECT_BACKLOG_ERRNOS = frozenset({errno.EAGAIN, errno.EWOULDBLOCK})


_CONNECT_BACKLOG_RETRY_INTERVAL = 0.01


_RESTORE_WAIT_POLL_SECONDS = 0.25


_RESTORE_WAIT_DEFAULT_SECONDS = 120.0


_RESTORE_WAIT_MAX_SECONDS = 900.0


_DAEMON_IPC_ROUNDTRIP_SECONDS = 15.0


#: Upper bound for ``HYPRIAL_IPC_ROUNDTRIP_SECONDS``.  The daemon's own
#: worst case (lock budget + busy timeout) is far below this, so anything
#: above it only hides a hang instead of measuring a delay distribution.
_DAEMON_IPC_ROUNDTRIP_MAX_SECONDS = 300.0


def _daemon_ipc_roundtrip_seconds() -> float:
    """Effective per-request IPC patience, honouring the documented override.

    Unparseable or out-of-range values fall back to the default rather than
    failing the command: a debugging override must never make the CLI
    unusable.  ``0`` is rejected on purpose -- a zero-timeout client would
    report every call as a timeout.
    """

    raw = os.environ.get("HYPRIAL_IPC_ROUNDTRIP_SECONDS")
    try:
        value = _DAEMON_IPC_ROUNDTRIP_SECONDS if raw is None else float(raw)
    except ValueError:
        return _DAEMON_IPC_ROUNDTRIP_SECONDS
    if not math.isfinite(value) or not 0.0 < value <= _DAEMON_IPC_ROUNDTRIP_MAX_SECONDS:
        return _DAEMON_IPC_ROUNDTRIP_SECONDS
    return value


def _restore_wait_budget() -> float:
    """Bounded seconds a caller waits out a DAEMON_RESTORING refusal.

    The default covers a derived restore round for typical fleets;
    ``HYPRIAL_DAEMON_RESTORE_WAIT_SECONDS=0`` restores fail-fast behaviour.
    """

    raw = os.environ.get("HYPRIAL_DAEMON_RESTORE_WAIT_SECONDS")
    try:
        value = _RESTORE_WAIT_DEFAULT_SECONDS if raw is None else float(raw)
    except ValueError:
        return _RESTORE_WAIT_DEFAULT_SECONDS
    if not math.isfinite(value) or not 0.0 <= value <= _RESTORE_WAIT_MAX_SECONDS:
        return _RESTORE_WAIT_DEFAULT_SECONDS
    return value


def _connect_daemon_socket(
    socket_path: Path, timeout: float, *, clock: object | None = None
) -> socket.socket:
    """Connect to the daemon, waiting out a momentarily full listen backlog.

    The retry budget is the request's own ``timeout``: the kernel only parks a
    connect() against a full AF_UNIX accept queue for fully blocking sockets,
    and this caller always runs with a timeout, so patience has to be
    explicit.  Only the full-backlog spelling (EAGAIN/EWOULDBLOCK) is retried
    -- ECONNREFUSED still fails fast because it also describes a stale socket
    file whose daemon is gone, which must not take the whole budget to report.
    """

    if os.name == "nt":
        # Resolved at connection time, not import time: the port function is a
        # monkeypatch seam and the base client imported it lazily here.
        from hyprial.kernel.impl.platform.ports import connect_named_pipe

        return connect_named_pipe(socket_path, timeout)
    clock = time if clock is None else clock
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(timeout)
    deadline = clock.monotonic() + timeout
    try:
        while True:
            try:
                client.connect(str(socket_path))
            except OSError as error:
                if error.errno in _CONNECT_BACKLOG_ERRNOS and clock.monotonic() < deadline:
                    clock.sleep(_CONNECT_BACKLOG_RETRY_INTERVAL)
                    continue
                raise
            return client
    except BaseException:
        client.close()
        raise


def _peer_gone(
    socket_path: Path, error: OSError | None = None
) -> ipc_errors.DaemonDisconnectedError:
    detail = f" ({error})" if error is not None else ""
    return ipc_errors.DaemonDisconnectedError(
        f"connection closed by the Harness daemon at {socket_path} "
        f"before any answer{detail}"
    )


def daemon_request(
    method: str,
    params: JsonObject | None = None,
    *,
    socket_path: Path | Callable[[], Path],
    timeout: float | None = None,
    restore_wait: float | None = None,
    clock: object | Callable[[], object] | None = None,
    uuid4: Callable[[], Any] | None = None,
) -> Any:
    """Call the daemon's version-1 newline-delimited JSON IPC protocol.

    ``socket_path`` and ``clock`` may be callables re-read per attempt: the
    endpoint and the service binding are re-resolved on every attempt, because a daemon that
    is still restoring may move its socket or be reached through a different
    state root between attempts.  The restore budget's deadline stays
    anchored to the clock resolved at the first attempt.
    """

    resolve_clock = clock if callable(clock) else (lambda: clock)
    resolve_path = socket_path if callable(socket_path) else (lambda: socket_path)
    active = resolve_clock()
    active = time if active is None else active
    if timeout is None:
        timeout = _daemon_ipc_roundtrip_seconds()
    budget = _restore_wait_budget() if restore_wait is None else restore_wait
    deadline = active.monotonic() + budget
    while True:
        try:
            return daemon_request_once(
                method,
                params,
                socket_path=resolve_path(),
                timeout=timeout,
                clock=resolve_clock(),
                uuid4=uuid4,
            )
        except ipc_errors.DaemonRestoringError:
            if active.monotonic() >= deadline:
                raise
            active.sleep(_RESTORE_WAIT_POLL_SECONDS)


def daemon_request_once(
    method: str,
    params: JsonObject | None = None,
    *,
    socket_path: Path,
    timeout: float | None = None,
    clock: object | None = None,
    uuid4: Callable[[], Any] | None = None,
) -> Any:
    """Call the daemon once over its version-one IPC protocol.

    One connect, one frame, one response.  Nothing here retries: a caller
    that needs a retry decision must make it with knowledge of whether the
    method writes.
    """

    clock = time if clock is None else clock
    if timeout is None:
        timeout = _daemon_ipc_roundtrip_seconds()
    request_id = str((uuid4 or _stdlib_uuid4)())
    frame: JsonObject = {"version": 1, "id": request_id, "method": method}
    if params is not None:
        frame["params"] = params
    client: socket.socket | None = None
    try:
        try:
            client = _connect_daemon_socket(socket_path, timeout, clock=clock)
        except OSError as error:
            # Report only what happened: which socket, and why the connect
            # failed.  The cause (never started / mid-restart / hijacked
            # HYPRIAL_HOME / permissions) cannot be told apart from here, so
            # no remedy is embedded -- guidance belongs to skills and docs,
            # which can be updated; a string constant cannot.
            raise ipc_errors.DaemonUnavailableError(
                f"cannot connect to the Harness daemon socket "
                f"{socket_path}: {error}"
            ) from error
        try:
            client.sendall(json.dumps(frame, separators=(",", ":")).encode() + b"\n")
        except OSError as error:
            if isinstance(error, TimeoutError):
                raise
            if error.errno in _PEER_GONE_ERRNOS:
                raise _peer_gone(socket_path, error) from error
            # Every other OSError is re-raised unchanged: the base shell
            # implementation did not reclassify it, and callers branch on the
            # original errno.
            raise
        buffer = bytearray()
        while len(buffer) <= 8 * 1024 * 1024:
            try:
                chunk = client.recv(64 * 1024)
            except OSError as error:
                if isinstance(error, TimeoutError):
                    raise
                if error.errno in _PEER_GONE_ERRNOS:
                    raise _peer_gone(socket_path, error) from error
                raise
            if not chunk:
                raise _peer_gone(socket_path)
            buffer.extend(chunk)
            while b"\n" in buffer:
                line, _, remainder = buffer.partition(b"\n")
                buffer = bytearray(remainder)
                if not line.strip():
                    continue
                try:
                    response = json.loads(line)
                except (UnicodeDecodeError, json.JSONDecodeError) as error:
                    raise RequestPortError(
                        "INVALID_JSON", f"invalid daemon IPC response: {error}"
                    ) from error
                if not isinstance(response, dict) or response.get("version") != 1:
                    raise RequestPortError(
                        ipc_errors.VERSION_MISMATCH, "unsupported daemon IPC version"
                    )
                if response.get("id") != request_id:
                    # Event frames and responses for other request IDs are not
                    # the result of this one-shot request.
                    continue
                failure = response.get("error")
                if isinstance(failure, dict):
                    code = str(failure.get("code", ipc_errors.DAEMON_ERROR))
                    message = str(failure.get("message", "daemon request failed"))
                    # Transient envelope codes deserialise through the shared
                    # registry into the SAME class the daemon minted; every
                    # other code stays a RequestPortError for the presentation
                    # layer to translate.
                    transient = ipc_errors.transient_error_from_code(
                        code, message, failure.get("data")
                    )
                    if transient is not None:
                        raise transient
                    raise RequestPortError(code, message, failure.get("data"))
                if "result" not in response:
                    raise RequestPortError(
                        "INVALID_RESPONSE", "daemon response is missing result"
                    )
                return response["result"]
        raise RequestPortError(
            "IPC_RESPONSE_TOO_LARGE", "daemon IPC response exceeded 8 MiB"
        )
    except TimeoutError as error:
        raise ipc_errors.IpcTimeoutError(
            f"timed out after {timeout:g}s waiting for the Harness daemon at "
            f"{socket_path} to answer method {method!r}",
        ) from error
    finally:
        if client is not None:
            client.close()
