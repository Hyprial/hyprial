"""The ownerless daemon answering over the home's own unix socket.

Decision: daemon-first-z1-bootstrap-wire-host-20261115.  Until an account
exists the daemon process has no business application, but it still owns the
home: this host holds the two continuous locks (through the shared
``BootstrapDispatcher``), publishes ``state/bootstrap.json`` and listens on
``<state>/daemon.sock`` with the framing every client already speaks
(newline-delimited JSON ``{version, id, method, params}``; the answer is
``{version, id, result|error}``), so ``daemon_request_once`` reaches it
unchanged.  It serves ``ping`` (``phase: bootstrap``), ``daemon.catalog``,
``shutdown`` and the onboarding methods the dispatcher admits; a declared
method that needs an account, and a name only the serving router answers, get the
stable ``ACCOUNT_REQUIRED`` refusal; a name no serving daemon answers gets
``METHOD_NOT_FOUND``.

Access: the state directory is created ``0700`` and the socket ``0600``
exactly as the serving daemon does, and every accepted connection is checked
against the effective uid of this process before a byte is read.  Where the
platform exposes no peer credential the file modes are the only boundary; a
credential that cannot be read is a refusal.  The frame is bounded (1 MiB
plus its newline, never read past that) and an idle connection is closed after 15 s.  Secrets never cross the socket
beyond what the onboarding methods already carry.

Lifecycle: ``serve`` returns why it stopped.  ``promote`` means an accepted
``onboarding.initialize`` reply was written in full and the quiesce hook ran:
the listener is closed and the socket removed while the locks and the
``promoting`` claim stay held for the caller to continue in this process.
``reopen`` means a pre-initialize logout completed; ``stop`` means ``shutdown``
or ``request_stop``.  The post-reply action of an accepted initialize runs only
after the whole reply was written; a failed write never runs it.  When that
reply cannot be written, the initialize claim stays ``promoting`` and can never
complete in this host, so ``serve`` returns ``initialize-unacknowledged`` (the
listener is closed, locks and claim stay held until the caller's ``close``
marks the unfinished work unknown); the host must not keep serving with a claim
nobody can finish.  Which phase follows is Z-2's decision.  ``close``
releases locks and marks unfinished work unknown, and is idempotent.  Nothing
in this module chooses promotion or the next phase: that loop is the caller's.

Decision: daemon-first-z2-initialize-failure-and-stop-20261116.  Two further
rules close what Z-1's review left open (design 6.3 items 2 and 3).

* An initialize that was durably accepted but whose reply cannot be delivered
  for any reason, including an exception while encoding the reply or inside
  the method registry after the claim was written, never leaves the host
  serving with a claim nobody can finish.  The respond path takes the armed
  post-reply action out of the runtime, discards it (it never runs), answers
  with the failure it has, and marks the reply unacknowledged: ``serve`` then
  returns ``initialize-unacknowledged`` whether or not that failure frame was
  written.
* ``shutdown`` (and ``request_stop``) is sticky and wins over every other
  outcome, because an acknowledged stop must end in exit.  If it lands while
  or after an initialize was accepted, ``serve`` returns ``stop`` instead of
  ``promote``/``reopen``/``initialize-unacknowledged``; the listener is
  closed, an initialize claim still ``promoting`` is marked unknown by the
  caller's ``close`` (never replayed, recovery owns it on the next start) and
  both locks are released there.  An ``onboarding.initialize`` that arrives after the stop was
  requested is refused as unavailable, so a stopping host cannot accept a new
  claim.  The stop flag is a plain attribute store so a signal handler
  may call ``request_stop`` without taking a lock; ``serve`` observes it on
  its next accept tick (at most ``_POLL_SECONDS``).

Decision: daemon-first-z5-first-login-20261118.  A daemon-driven login (see the
dispatcher) ends in ``promote`` without any client calling ``initialize``, and a
client may be parked in ``auth.operation.get`` with ``waitSeconds`` across that
swap.

* The driver signals ``promote`` (it runs the armed quiesce hook) only after it
  left the dispatcher lock, so the caller never starts the serving application
  while the driver is still inside the accept.
* A parked wait holds no dispatcher lock (``ping`` and ``cancel`` are answered
  meanwhile) and is the one connection the listener close does not join or shut
  down: it is answered later, from the serving daemon's ready boundary with stage
  ``serving``, or with ``stopped`` the moment this host is ending.  The host
  answers every parked wait when it ends (``close`` ends the runs, then joins the
  parked workers, bounded by ``_DRAIN_SECONDS``, and shuts down any that
  remain), so no socket or thread outlives ``close``.  At most ``_MAX_PARKED``
  waits park at once; the rest are answered immediately.

Decision: ipc-bootstrap-2026-11-19.  The host listens and answers through the
kernel local IPC seam (``listen_local_ipc``) and holds no socket family of its
own, so a platform without unix domain sockets serves over its own transport
(the Windows named pipe).  Peer admission reads the accepted ``LocalIpcPeer``:
a transport that already enforces the current user (the pipe's DACL) is
admitted without a uid comparison, an unreadable credential stays a refusal,
and every other rule of the Z-1 decision is unchanged.  The endpoint
lifecycle -- fresh ``0700`` parent, stale endpoint removal, ``0600`` hardening
between bind and listen -- belongs to the seam's entry point and backends;
this host keeps only the identity check that unlinks the endpoint file it
observed at bind time.  Reply writes use the stream's bounded response
writer; the write is never aborted on host state, because a parked wait's
``stopped`` answer must still be delivered while this host is ending.
"""

from __future__ import annotations

import json
import os
import socket
import stat
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from hyprial.kernel import (
    DaemonRequestError,
    LocalIpcListener,
    LocalIpcPeer,
    LocalIpcStream,
    Logger,
    ipc_errors,
    listen_local_ipc,
    manysys,
    migrate_pre_trajectory_logs,
)

from .. import _OnboardingAssemblyMixin
from .dispatcher import WIRE_POLICY, BootstrapDispatcher
from .peer import own_uid

STOP = "stop"
PROMOTE = "promote"
REOPEN = "reopen"
UNACKNOWLEDGED = "initialize-unacknowledged"

_MAX_FRAME_BYTES = 1024 * 1024
_IDLE_SECONDS = 15.0
_WRITE_SECONDS = 15.0
_POLL_SECONDS = 0.1
_MAX_CONNECTIONS = 32
_MAX_PARKED = 8
_DRAIN_SECONDS = 5.0
_ENDED = frozenset({STOP, REOPEN, UNACKNOWLEDGED})

PeerView = Callable[[LocalIpcPeer], LocalIpcPeer]


def _reported_peer(peer: LocalIpcPeer) -> LocalIpcPeer:
    return peer


def _response_write_is_never_aborted() -> bool:
    """A reply write runs its full deadline even while the host is ending.

    A parked ``auth.operation.get`` is answered with ``stopped`` the moment
    this host ends, and that answer still has to be written.
    """

    return False


def _non_empty_string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise DaemonRequestError(
            ipc_errors.INVALID_ARGUMENT, f"{label} must be a non-empty string"
        )
    return value


class _WireApplication(_OnboardingAssemblyMixin):
    """The object the method registry invokes while no daemon application exists."""

    def __init__(self, state_dir: Path) -> None:
        self._state_dir = Path(state_dir)
        self._logger: Logger | None = None

    def _log(self, level: str, component: str, event: str, **fields: Any) -> None:
        if self._logger is None:
            migrate_pre_trajectory_logs(self._state_dir)
            self._logger = Logger.daemon(self._state_dir, name="bootstrap")
        self._logger.bind(component=component).log(level, event, **fields)


@dataclass(frozen=True, slots=True)
class _Reply:
    frame: bytes
    after: Callable[[], None] | None = None
    unacknowledged: str | None = None


def _normalize_bootstrap_path(raw: Path) -> Path:
    lexical = Path(os.path.abspath(os.fspath(raw)))
    current = Path(lexical.anchor)
    for part in lexical.parts[1:]:
        current /= part
        try:
            observed = current.lstat()
        except FileNotFoundError:
            return lexical.resolve()
        except OSError:
            return lexical
        if manysys.path_is_reparse(observed):
            return lexical
        if stat.S_ISLNK(observed.st_mode):
            allowed = (
                current != lexical
                and current.anchor == "/"
                and current.parts in {("/", "tmp"), ("/", "var"), ("/", "etc")}
                and current.is_dir()
            )
            if not allowed:
                return lexical
    return lexical.resolve()


class BootstrapWireHost:
    """One bootstrap dispatcher served on ``<state>/daemon.sock``."""

    def __init__(
        self,
        dispatcher: BootstrapDispatcher,
        *,
        peer_view: PeerView | None = None,
        expected_uid: int | None = None,
    ) -> None:
        self._dispatcher = dispatcher
        self._peer_view = _reported_peer if peer_view is None else peer_view
        self._expected_uid = own_uid() if expected_uid is None else expected_uid
        self._listener: LocalIpcListener | None = None
        self._socket_identity: tuple[int, int] | None = None
        self._outcome: str | None = None
        self._stop_requested = False
        self._state_lock = threading.Lock()
        self._workers: dict[threading.Thread, LocalIpcStream] = {}
        self._parked: set[threading.Thread] = set()
        self._slots = threading.BoundedSemaphore(_MAX_CONNECTIONS)
        self._closed = False
        self.rejected_peers = 0

    @classmethod
    def open(
        cls,
        home: Path,
        *,
        state_dir: Path | None = None,
        gateway_options: Mapping[str, Any] | None = None,
        peer_view: PeerView | None = None,
        expected_uid: int | None = None,
    ) -> BootstrapWireHost:
        """Admit the ownerless home, then bind the endpoint; either failure releases all."""

        resolved = _normalize_bootstrap_path(Path(home))
        state = (
            resolved / "state"
            if state_dir is None
            else _normalize_bootstrap_path(Path(state_dir))
        )
        dispatcher = BootstrapDispatcher.admit(
            app=_WireApplication(state),
            home=resolved,
            state_dir=state,
            socket_path=state / "daemon.sock",
            policy=WIRE_POLICY,
            gateway_options=gateway_options,
        )
        host = cls(dispatcher, peer_view=peer_view, expected_uid=expected_uid)
        try:
            dispatcher.enable_promotion(
                lambda: host._finish(PROMOTE),
                lambda: host._finish(UNACKNOWLEDGED),
            )
            host._bind()
        except BaseException:
            dispatcher.close()
            raise
        return host

    @property
    def dispatcher(self) -> BootstrapDispatcher:
        return self._dispatcher

    @property
    def socket_path(self) -> Path:
        return self._dispatcher.socket_path

    @property
    def outcome(self) -> str | None:
        return self._outcome

    def __enter__(self) -> BootstrapWireHost:
        return self

    def __exit__(self, *_error: object) -> None:
        self.close()

    def request_stop(self) -> None:
        """Ask ``serve`` to return ``stop`` (thread-safe, idempotent, signal-safe)."""

        self._stop_requested = True

    def serve(self) -> str:
        """Accept clients until initialize, logout, shutdown or ``request_stop``."""

        listener = self._listener
        if listener is None:
            raise RuntimeError("the bootstrap wire host is not listening")
        try:
            while self._outcome is None:
                if self._stop_requested:
                    self._finish(STOP)
                    break
                if self._dispatcher.reopen_required:
                    self._finish(REOPEN)
                    break
                try:
                    connection, peer = listener.accept()
                except TimeoutError:
                    continue
                except OSError:
                    if self._outcome is not None:
                        break
                    raise
                self._admit(connection, peer)
        finally:
            self._close_listener()
        if self._stop_requested:
            self._outcome = STOP
        return self._outcome or STOP

    def close(self) -> None:
        """Stop serving and release the locks; unfinished work becomes unknown."""

        if self._closed:
            return
        self._closed = True
        self._stop_requested = True
        self._finish(STOP)
        self._close_listener()
        self._dispatcher.close()
        self._reap_parked()

    def _finish(self, outcome: str) -> None:
        with self._state_lock:
            if self._outcome is None:
                self._outcome = outcome

    def _bind(self) -> None:
        path = self.socket_path
        listener = listen_local_ipc(path)
        listener.settimeout(_POLL_SECONDS)
        observed = path.lstat()
        self._socket_identity = (observed.st_dev, observed.st_ino)
        self._listener = listener

    def _close_listener(self) -> None:
        with self._state_lock:
            listener, self._listener = self._listener, None
            identity, self._socket_identity = self._socket_identity, None
            workers = {
                worker: connection
                for worker, connection in self._workers.items()
                if worker not in self._parked
            }
        if listener is None:
            return
        listener.close()
        current = threading.current_thread()
        deadline = time.monotonic() + _DRAIN_SECONDS
        for worker in workers:
            if worker is not current:
                worker.join(timeout=max(0.0, deadline - time.monotonic()))
        for worker, connection in workers.items():
            if worker is not current and worker.is_alive():
                try:
                    connection.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
        self._unlink_own_socket(identity)

    def _reap_parked(self) -> None:
        with self._state_lock:
            parked = {
                worker: self._workers[worker]
                for worker in self._parked
                if worker in self._workers
            }
        current = threading.current_thread()
        deadline = time.monotonic() + _DRAIN_SECONDS
        for worker in parked:
            if worker is not current:
                worker.join(timeout=max(0.0, deadline - time.monotonic()))
        for worker, connection in parked.items():
            if worker is not current and worker.is_alive():
                try:
                    connection.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

    @contextmanager
    def _parking(self) -> Iterator[bool]:
        thread = threading.current_thread()
        with self._state_lock:
            allowed = len(self._parked) < _MAX_PARKED
            if allowed:
                self._parked.add(thread)
        try:
            yield allowed
        finally:
            if allowed:
                with self._state_lock:
                    self._parked.discard(thread)

    def _leaving(self) -> bool:
        return self._stop_requested or self._closed or self._outcome in _ENDED

    def _unlink_own_socket(self, identity: tuple[int, int] | None) -> None:
        path = self.socket_path
        try:
            observed = path.lstat()
        except OSError:
            return
        if identity == (observed.st_dev, observed.st_ino):
            path.unlink(missing_ok=True)

    def _admit(self, connection: LocalIpcStream, peer: LocalIpcPeer) -> None:
        if not self._slots.acquire(blocking=False):
            connection.close()
            return
        worker = threading.Thread(
            target=self._serve_connection,
            args=(connection, peer),
            name="hyprial-bootstrap-wire-client",
            daemon=True,
        )
        with self._state_lock:
            self._workers[worker] = connection
        try:
            worker.start()
        except BaseException:
            with self._state_lock:
                self._workers.pop(worker, None)
            self._slots.release()
            connection.close()
            raise

    def _serve_connection(
        self, connection: LocalIpcStream, peer: LocalIpcPeer
    ) -> None:
        try:
            with connection:
                if not self._peer_allowed(self._peer_view(peer)):
                    return
                try:
                    line = self._read_frame(connection)
                except DaemonRequestError as error:
                    reply = self._failure(None, error)
                else:
                    if line is None:
                        return
                    reply = self._respond(line)
                written = self._write(connection, reply.frame)
                if reply.unacknowledged is not None and (
                    not written or reply.after is None
                ):
                    self._finish(reply.unacknowledged)
                    return
                if not written:
                    return
                if reply.after is not None:
                    reply.after()
        finally:
            with self._state_lock:
                self._workers.pop(threading.current_thread(), None)
            self._slots.release()

    def _peer_allowed(self, peer: LocalIpcPeer) -> bool:
        if peer.same_user_enforced:
            return True
        if peer.uid_unreadable:
            self._reject_peer()
            return False
        if self._expected_uid is None or peer.uid is None:
            return True
        if peer.uid != self._expected_uid:
            self._reject_peer()
            return False
        return True

    def _reject_peer(self) -> None:
        with self._state_lock:
            self.rejected_peers += 1

    def _read_frame(self, connection: LocalIpcStream) -> bytes | None:
        connection.settimeout(_POLL_SECONDS)
        buffer = bytearray()
        idle_deadline = time.monotonic() + _IDLE_SECONDS
        limit = _MAX_FRAME_BYTES + 1
        while len(buffer) < limit:
            try:
                chunk = connection.recv(min(64 * 1024, limit - len(buffer)))
            except TimeoutError:
                if (
                    self._outcome is not None
                    or self._stop_requested
                    or time.monotonic() >= idle_deadline
                ):
                    return None
                continue
            except OSError:
                return None
            if not chunk:
                return None
            buffer.extend(chunk)
            if b"\n" in buffer:
                return bytes(buffer.partition(b"\n")[0])
        raise DaemonRequestError(
            ipc_errors.IPC_REQUEST_TOO_LARGE, "bootstrap request exceeded 1 MiB"
        )

    def _write(self, connection: LocalIpcStream, frame: bytes) -> bool:
        try:
            return connection.send_response(
                frame,
                timeout=_WRITE_SECONDS,
                poll_interval=_POLL_SECONDS,
                should_stop=_response_write_is_never_aborted,
            )
        except OSError:
            return False

    def _respond(self, line: bytes) -> _Reply:
        request_id: object = None
        armed: Callable[[], None] | None = None
        try:
            try:
                request = json.loads(line)
            except ValueError as error:
                raise DaemonRequestError(
                    ipc_errors.INVALID_REQUEST, "daemon request is not valid JSON"
                ) from error
            if not isinstance(request, dict):
                raise DaemonRequestError(
                    ipc_errors.INVALID_REQUEST, "daemon request must be an object"
                )
            request_id = request.get("id")
            if request.get("version") != 1:
                raise DaemonRequestError(
                    ipc_errors.VERSION_MISMATCH, "unsupported daemon IPC version"
                )
            method = _non_empty_string(request.get("method"), "method")
            params = request.get("params", {})
            if not isinstance(params, dict):
                raise DaemonRequestError(
                    ipc_errors.INVALID_ARGUMENT, "params must be an object"
                )
            if method == "shutdown":
                result = self._shutdown(params)
                frame = self._encode({"version": 1, "id": request_id, "result": result})
                return _Reply(frame, self.request_stop)
            seconds = self._dispatcher.wait_seconds(method, params)
            if seconds > 0:
                with self._parking() as parked:
                    if parked:
                        result = self._dispatcher.wait(
                            method, params, seconds, leave=self._leaving
                        )
                        frame = self._encode(
                            {"version": 1, "id": request_id, "result": result}
                        )
                        return _Reply(frame)
            with self._dispatcher.exclusive():
                if self._stop_requested and method == "onboarding.initialize":
                    raise ipc_errors.DaemonUnavailableError(
                        "the bootstrap daemon is stopping; initialize was not accepted"
                    )
                try:
                    result = self._dispatcher.call(method, params)
                finally:
                    armed = self._dispatcher.take_post_reply()
            frame = self._encode({"version": 1, "id": request_id, "result": result})
            return _Reply(frame, armed, UNACKNOWLEDGED if armed else None)
        except Exception as error:  # noqa: BLE001 - the stable daemon error boundary
            reply = self._failure(request_id, error)
            return replace(reply, unacknowledged=UNACKNOWLEDGED) if armed else reply

    def _failure(self, request_id: object, error: Exception) -> _Reply:
        failure: dict[str, Any] = {
            "code": str(getattr(error, "code", ipc_errors.DAEMON_ERROR)),
            "message": str(error),
        }
        data = getattr(error, "data", None)
        if data is not None:
            failure["data"] = data
        return _Reply(self._encode({"version": 1, "id": request_id, "error": failure}))

    def _shutdown(self, params: Mapping[str, Any]) -> dict[str, Any]:
        if "expectedEpoch" in params:
            expected = _non_empty_string(params["expectedEpoch"], "expectedEpoch")
            if expected != self._dispatcher.epoch:
                raise DaemonRequestError(
                    "DAEMON_EPOCH_MISMATCH",
                    "shutdown plan belongs to another daemon epoch",
                )
        state_dir = self._dispatcher.runtime.state_dir
        return {
            "ok": True,
            "stopping": True,
            "pid": os.getpid(),
            "epoch": self._dispatcher.epoch,
            "stateDir": str(state_dir),
            "lockPath": str(state_dir / "daemon.lock"),
        }

    @staticmethod
    def _encode(document: dict[str, Any]) -> bytes:
        return json.dumps(document, separators=(",", ":"), ensure_ascii=False).encode() + b"\n"
