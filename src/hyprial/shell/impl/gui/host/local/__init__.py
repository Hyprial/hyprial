"""The daemon driver served in the host's own process: the host side of the fold.

Decision: host-serves-daemon-in-process-20261119 (2822, B3).  ``hyprial gui`` with
the daemon driver starts one Python child, this host.  ``LocalDaemon`` holds what
the former ``request-http`` child held, in memory: the admitted daemon host, a
listener-free ``RequestHttpServer`` (the one HTTP projection of the method
declarations; only its transport changes), an ``httpx.ASGITransport`` over its one
application that ``UpstreamClient`` uses as its transport, and the principal
watcher as a thread.  No bearer crosses a socket or the environment and no
``server.json`` or ``frontend-authority.json`` exists.

Authority.  The server's ``on_rebound`` hook runs on the watcher thread while the
server's refresh lock is held, so it only hands a value over: ``_publish`` assigns
one new immutable ``HostConfig`` (the new bearer and principal) to ``_current``
and notifies a condition.  The session layer reads ``authority()`` before every
route; a new object invalidates the business sessions on the event loop thread
(``AuthorityView``), so the loop is never called from the watcher.  A rebind
between the server installing its binding and the assignment is a few statements
long and answers 401 to a request that was built with the old bearer.

Failure.  An exception that ends the watcher thread is fatal and never silent: it
is logged to stderr as one ``watcher_failed`` event (type and code, a message only
for the project's own ``StartupRefusal`` and ``GuiError``; never a bearer), the
authority fails closed from then on (every route answers 503
``GUI_AUTHORITY_UNAVAILABLE``, no request is relayed), and the callback registered
with ``stop_on_failure`` stops the listener; the host then exits with code 3, the
code of every other backend refusal.  A callback registered after the failure runs
at once, so the order of registration and failure does not matter.

Shutdown.  ``close`` runs once, after the listener drained: it sets the watcher's
stop and joins it (bounded by the shutdown drain budget of the config) before
``server.shutdown()``, because a rebind after an embedded shutdown raises
``RuntimeError``; then it closes the live daemon host exactly once, last, so a
request that still dispatches through it is refused by the closed host and not
by a closed server.  A host the watcher installed after the join timed out is
closed too, once per host object.  After ``close`` the authority is unavailable, so the host
routes nothing even though the embedded application still answers ``/healthz``;
the host never asks that route for liveness (``/__gui/healthz`` is its own).

Startup.  ``BACKEND_UNAVAILABLE`` (no daemon answers), ``DAEMON_OWNERSHIP_BUSY`` and
the gateway's own refusals reach the caller as ``StartupRefusal`` with exit code 3,
mirroring ``request_http.serve``.
"""

from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import sys
import threading
from typing import Any, Callable

import httpx

from ...errors import GuiError
from ....request_http.bootstrap.gui import LiveHost, local_gui_config, open_gui_host, watch_principal
from ....request_http.server import Rebound, RequestHttpServer, StartupRefusal
from ..config import AuthorityUnavailable, HostConfig, Principal

EXIT_WATCHER_FAILED = 3

__all__ = ["EXIT_WATCHER_FAILED", "LocalDaemon"]


class LocalDaemon:
    """One daemon host, its embedded dispatch and the watcher that follows the daemon."""

    def __init__(self, base: HostConfig, node: str) -> None:
        self._base = base
        self._node = node
        self._current = base
        self.config = base
        self.transport: httpx.AsyncBaseTransport | None = None
        self._condition = threading.Condition()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._failed = threading.Event()
        self._failure: BaseException | None = None
        self._on_failure: Callable[[], None] | None = None
        self._closed = False
        self._server: RequestHttpServer | None = None
        self._live: LiveHost | None = None
        self._thread: threading.Thread | None = None
        self._join_seconds = 0.0

    @classmethod
    def open(cls, base: HostConfig, *, home: Path, state_dir: Path, socket: Path, node: str) -> "LocalDaemon":
        """Attach to the daemon, start the embedded dispatch and the watcher."""
        local = cls(base, node)
        request_config = local_gui_config(home=home, state_dir=state_dir, socket_path=socket)
        host, request_config = open_gui_host(request_config)
        server = RequestHttpServer(request_config, request_once=host, listen=False, on_rebound=local._publish)
        try:
            server.start()
        except BaseException:
            host.close()
            raise
        local._server = server
        local._live = LiveHost(host)
        local._join_seconds = request_config.budgets.shutdown_drain
        local.transport = httpx.ASGITransport(app=server.asgi_app())
        local.config = local._current = local._config_for(server.token, request_config.owner, request_config.actor)
        local._thread = threading.Thread(
            target=local._watch,
            args=(server, request_config),
            name="hyprial-gui-principal-watcher",
            daemon=True,
        )
        local._thread.start()
        return local

    def authority(self) -> HostConfig:
        """The current immutable config; raises once the watcher died or the host closed."""
        if self._failure is not None or self._closed:
            raise AuthorityUnavailable("the in-process authority is unavailable")
        return self._current

    def wait_for_change(self, since: HostConfig, timeout: float) -> bool:
        """Block until the published config is not ``since``; False when ``timeout`` ran out."""
        with self._condition:
            return self._condition.wait_for(lambda: self._current is not since, timeout)

    def wait_for_failure(self, timeout: float) -> bool:
        return self._failed.wait(timeout)

    def stop_on_failure(self, callback: Callable[[], None]) -> None:
        with self._lock:
            self._on_failure = callback
            failed = self._failed.is_set()
        if failed:
            callback()

    @property
    def failure(self) -> BaseException | None:
        return self._failure

    @property
    def exit_code(self) -> int:
        return 0 if self._failure is None else EXIT_WATCHER_FAILED

    def watcher_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def live_host(self) -> Any:
        assert self._live is not None
        return self._live.host

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
        self._stop.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=self._join_seconds)
            if thread.is_alive():
                self._log("watcher_join_timeout")
        if self._server is None or self._live is None:
            return
        try:
            self._server.shutdown()
        finally:
            closed = self._live.host
            closed.close()
            late = self._live.host
            if late is not closed:
                late.close()

    def _config_for(self, token: str, owner: str | None, actor: str | None) -> HostConfig:
        principal = None if owner is None or actor is None else Principal(actor=actor, owner=owner, node=self._node)
        return replace(self._base, upstream_token=token, principal=principal)

    def _publish(self, rebound: Rebound) -> None:
        self._current = self._config_for(rebound.token, rebound.owner, rebound.actor)
        with self._condition:
            self._condition.notify_all()

    def _watch(self, server: RequestHttpServer, request_config: Any) -> None:
        assert self._live is not None
        try:
            watch_principal(server, request_config, self._live, self._stop)
        except BaseException as error:
            self._fail(error)

    def _fail(self, error: BaseException) -> None:
        record: dict[str, Any] = {"event": "watcher_failed", "error": type(error).__name__}
        code = getattr(error, "code", None)
        if isinstance(code, str):
            record["code"] = code
        if isinstance(error, (StartupRefusal, GuiError)):
            record["message"] = str(error)
        self._log_record(record)
        with self._lock:
            self._failure = error
            self._failed.set()
            callback = self._on_failure
        if callback is not None:
            callback()

    def _log(self, event: str) -> None:
        self._log_record({"event": event})

    @staticmethod
    def _log_record(record: dict[str, Any]) -> None:
        print(json.dumps(record, separators=(",", ":"), sort_keys=True), file=sys.stderr, flush=True)
