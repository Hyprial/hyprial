"""FastAPI/Uvicorn lifecycle, daemon dispatch and transport envelopes.

Decision: daemon-first-z6c-one-step-rebind-20261117.  A ``local-gui`` listener
outlives the daemon's phase: the principal, the daemon handle and the bearer are
one immutable ``_Binding``.  Every request reads ``self._binding`` once and uses
only that object (principal, handle, authority to admit), so a request is wholly
of one principal.  ``rebind`` is the only writer of ``_binding`` after
construction and always holds ``_refresh_lock``; readers take no lock, and
assigning one reference is atomic, so a reader sees the whole old or the whole new
object.  The new bearer is accepted only inside the object that already holds the
new principal, and the old bearer is refused from the first assignment on.  The
swap is two assignments of two objects: the new binding is first installed
closed (``admit_after`` infinite), then replaced by an open copy with
``admit_after = monotonic()`` taken after the first assignment, still before the
capability file is written.  The closed object is load-bearing, though not
through its value alone: a request that read it is refused at once, and any
request that read it and then waited for ``_refresh_lock`` is refused by the
identity re-check, because the open copy replaced it before the lock is released.
Replacing the two assignments by one, stamped before the swap, lets a request
stamped in between authenticate with the old bearer and be admitted by the new
object.  A request the transport stamped (``started``, before it checks the
bearer) at or before the open stamp is refused ``DAEMON_UNAVAILABLE``, not
dispatched, because it may have been authenticated with the previous bearer.
The stamp is taken after the swap, never before: a stamp taken before it would
let a request stamped in between pass with the old bearer.  The new bearer is
published only afterwards, so a request carrying it is stamped later and is
admitted at once; it waits only on ``_refresh_lock`` for a ``rebind`` still
writing the file.  If the write fails the binding is closed for good, so every
waiter is refused, none dispatches, and the server stops.  The caller
closes the previous daemon handle before it calls ``rebind``: a request already
inside it completes under the principal it was admitted with, a later one is
refused by the closed handle.  A refusal before the install leaves the previous
binding in place.  The endpoint, the process and the listener never change; the
bearer is not logged, and ``server.json`` is replaced atomically with mode 0600.

Decision: embedded-request-http-listen-false-20261119 (2822, B1).  A server built
with ``listen=False`` is the same dispatch without a listener: ``start`` runs the
identity and catalog steps only (no runtime directory, no lock, no uvicorn, no
capability file, no ``server.json``), ``asgi_app`` hands the
one ASGI application to an in-process caller after ``start`` (it raises before),
and ``shutdown`` closes the binding so every admitted route of the cached
application refuses from then on; the unauthenticated ``/healthz`` and the schema
document still answer, so an embedder stops routing to it itself.  In listener
mode a hook that raises after the file was written leaves ``server.json`` naming
the closed bearer, which is refused.  ``rebind`` is the same in both modes:
the two assignments, then the publication, which is the capability file write
(a no-op without a listener) followed by ``on_rebound`` when one is given.  Both
sit in the one ``try`` whose failure closes the binding (``admit_after`` is
infinite), so a publication that fails, the hook raising included, leaves neither
bearer accepted and no half-installed binding.  The hook runs once per successful
rebind, with the open copy installed, never on a failed or refused rebind.  It
gets a ``Rebound`` (the new bearer, owner and actor), not
the private binding.  It runs on the rebinding thread while the refresh lock is
held: it must not block and must not call back into the server; it only hands the
value over (assign it, or ``call_soon_threadsafe``).  The default ``listen=True``
without a hook is the behaviour described above.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
import json
import math
import os
import secrets
import sys
import threading
import time
from typing import TYPE_CHECKING, Any, Callable, NamedTuple

from jsonschema import Draft202012Validator, FormatChecker

from hyprial import __version__
from hyprial.daemon import daemon_catalog as local_daemon_catalog
from hyprial.kernel import (
    DaemonRequestError,
    RequestPortError,
    canonical_user_uri,
    daemon_request_once,
    ipc_errors,
)

from .catalog import (
    CatalogProblem,
    CatalogView,
    HTTP_CONTRACT_VERSION,
    PHASE_REASONS,
    Route,
    build_view,
)
from .config import (
    RequestHttpConfig,
    RuntimeLock,
    prepare_runtime_dir,
    remove_server_json,
    write_server_json,
)
from .errors import HttpFailure, status_for

if TYPE_CHECKING:
    import uvicorn


class StartupRefusal(Exception):
    """A startup condition that must exit with a stable code."""

    def __init__(self, code: str, message: str, *, exit_code: int = 3, reason: str | None = None) -> None:
        self.code = code
        self.message = str(message)
        self.exit_code = exit_code
        self.reason = reason
        super().__init__(self.message)


@dataclass(frozen=True, slots=True)
class _Envelope:
    status: int
    payload: dict[str, Any]
    code: str | None = None
    dispatch: str | None = None


class Rebound(NamedTuple):
    """What ``on_rebound`` receives: the principal binding a rebind just published."""

    token: str
    owner: str | None
    actor: str | None


@dataclass(frozen=True, slots=True)
class _Binding:
    config: RequestHttpConfig
    request_once: Callable[..., Any]
    token: str
    admit_after: float


def _check_binding(config: RequestHttpConfig, request_once: Any) -> None:
    if config.principal_kind == "local-gui":
        phase = getattr(request_once, "phase", None)
        if phase not in {"bootstrap", "serving"}:
            raise StartupRefusal("GUI_HOST_REQUIRED", "local-gui HTTP requires its admitted runtime host")
        if phase == "bootstrap":
            if config.owner is not None or config.actor is not None:
                raise StartupRefusal(
                    "PRINCIPAL_CONFIG_MISMATCH",
                    "a bootstrap GUI host has no owner and no actor",
                )
        elif config.owner is None or config.actor is None:
            raise StartupRefusal(
                "PRINCIPAL_CONFIG_MISMATCH",
                "a serving GUI host requires an owner and its canonical actor",
            )
    elif config.principal_kind != "local-operator":
        raise StartupRefusal("UNSUPPORTED_PRINCIPAL", "unsupported HTTP principal")


class RequestHttpServer:
    """Owns one listener, its capability file and the daemon catalog snapshot."""

    def __init__(
        self,
        config: RequestHttpConfig,
        *,
        request_once: Callable[..., Any] | None = None,
        listen: bool = True,
        on_rebound: Callable[[Rebound], None] | None = None,
    ) -> None:
        _check_binding(config, request_once)
        self._listen = listen
        self._on_rebound = on_rebound
        self._binding = _Binding(
            config,
            daemon_request_once if request_once is None else request_once,
            secrets.token_urlsafe(32),
            -math.inf,
        )
        self.lock = RuntimeLock(config.runtime_dir)
        self.semaphore = threading.BoundedSemaphore(config.budgets.max_concurrent)
        self._state_lock = threading.Lock()
        self._refresh_lock = threading.Lock()
        self._stop = threading.Event()
        self._view: CatalogView | None = None
        self._phase = "restoring"
        self._snapshot_at = 0.0
        self._catalog_stale = False
        self._needs_rehandshake = False
        self._startup_declaration = ""
        self._startup_http = ""
        self._uvicorn: uvicorn.Server | None = None
        self._thread: threading.Thread | None = None
        self._app: Any | None = None
        self._started = False

    @property
    def config(self) -> RequestHttpConfig:
        return self._binding.config

    @property
    def request_once(self) -> Callable[..., Any]:
        return self._binding.request_once

    @property
    def token(self) -> str:
        return self._binding.token

    # -- startup ---------------------------------------------------------

    def asgi_app(self) -> Any:
        """Return the one ASGI application of a started server.

        Raises ``RuntimeError`` before ``start`` has built it.
        """
        if self._app is None:
            raise RuntimeError("the ASGI application exists only after start")
        return self._app

    def _handshake(self) -> None:
        view, ping = self._fetch(note="startup")
        self._verify_identity(view, ping)
        self._view = view
        self._phase = str(ping["phase"])
        self._startup_declaration = view.declaration_digest
        self._startup_http = view.http_contract_digest
        self._snapshot_at = time.monotonic()
        for name, reason in sorted(view.skipped.items()):
            self._log("unsupported_method", method=name, reason=reason)
        from .app import build_app

        self._app = build_app(self)

    def start(self) -> None:
        if not self._listen:
            self._handshake()
            self._started = True
            self._log("started", pid=os.getpid(), endpoint=None, methods=sorted(self._view.routes), digest=self._startup_http)
            return
        prepare_runtime_dir(self.config)
        self.lock.acquire()
        try:
            self._handshake()
            self._start_uvicorn()
            self._write_capability_file()
            self._started = True
            self._log(
                "started",
                pid=os.getpid(),
                endpoint=self.endpoint,
                methods=sorted(self._view.routes),
                digest=self._startup_http,
            )
        except BaseException:
            self._stop_uvicorn()
            self._close_runtime()
            raise

    def _start_uvicorn(self) -> None:
        import uvicorn

        from .app import http_protocol_class

        config = uvicorn.Config(
            self._app,
            host=self.config.host,
            port=self.config.port,
            log_level="warning",
            log_config=None,
            access_log=False,
            lifespan="on",
            loop="asyncio",
            http=http_protocol_class(self.config.budgets.header_timeout),
            proxy_headers=False,
            server_header=False,
            timeout_graceful_shutdown=self.config.budgets.shutdown_drain,
            timeout_keep_alive=int(self.config.budgets.idle_timeout),
        )
        server = uvicorn.Server(config)
        self._uvicorn = server
        self._thread = threading.Thread(
            target=server.run, name="request-http-uvicorn", daemon=True
        )
        self._thread.start()
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            if server.started:
                return
            if not self._thread.is_alive():
                break
            time.sleep(0.01)
        server.should_exit = True
        if self._thread is not None:
            self._thread.join(timeout=self.config.budgets.shutdown_drain + 5.0)
            if self._thread.is_alive():
                server.force_exit = True
                self._thread.join(timeout=5.0)
        self._uvicorn = None
        self._thread = None
        raise StartupRefusal(
            "BIND_FAILED",
            f"uvicorn failed to listen on {self.config.host}:{self.config.port}",
            exit_code=4,
        )

    def _verify_identity(
        self, view: CatalogView, ping: dict[str, Any], config: RequestHttpConfig | None = None
    ) -> None:
        config = self.config if config is None else config
        if ping.get("version") != __version__ or view.package_version != __version__:
            raise StartupRefusal(
                ipc_errors.VERSION_MISMATCH,
                f"daemon version {ping.get('version')!r} differs from package {__version__!r}",
            )
        if config.principal_kind != "local-gui" and view.backend.owner != config.owner:
            raise StartupRefusal(
                "PRINCIPAL_CONFIG_MISMATCH",
                "configured principal.owner differs from the daemon owner",
            )
        if config.principal_kind == "local-gui":
            if ping.get("phase") not in {"bootstrap", "serving"}:
                raise StartupRefusal("PRINCIPAL_CONFIG_MISMATCH", "GUI phase changed")
            if config.owner is not None and view.backend.owner != config.owner:
                raise StartupRefusal(
                    "PRINCIPAL_CONFIG_MISMATCH",
                    "configured principal.owner differs from the GUI host owner",
                )
            if config.owner is not None:
                if config.actor != canonical_user_uri(config.owner):
                    raise StartupRefusal(
                        "PRINCIPAL_CONFIG_MISMATCH",
                        "configured principal.actor is not the GUI owner's user identity",
                    )
            return
        if config.actor != canonical_user_uri(view.backend.owner):
            raise StartupRefusal(
                "PRINCIPAL_CONFIG_MISMATCH",
                "configured principal.actor is not the daemon owner's user identity",
            )

    def _write_capability_file(self) -> None:
        if not self._listen:
            return
        assert self._view is not None
        assert self._uvicorn is not None
        binding = self._binding
        config = binding.config
        document = {
            "schemaVersion": 1,
            "pid": self._pid(),
            "endpoint": self.endpoint,
            "token": binding.token,
            "httpContractVersion": HTTP_CONTRACT_VERSION,
            "catalogDigest": self._startup_http,
            "declarationDigest": self._startup_declaration,
            "packageVersion": __version__,
            "backend": self._view.backend.as_json(),
            "principal": {
                "kind": config.principal_kind,
                "owner": config.owner,
                "actor": config.actor,
            },
            "startedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        try:
            write_server_json(config.runtime_dir, document)
        except OSError as error:
            raise StartupRefusal(
                "RUNTIME_UNAVAILABLE", f"cannot write server.json: {error.strerror}", exit_code=4
            ) from error

    def _pid(self) -> int:
        return os.getpid()

    @property
    def endpoint(self) -> str:
        assert self._uvicorn is not None
        servers = self._uvicorn.servers
        if not servers or not servers[0].sockets:
            raise RuntimeError("uvicorn has not published its listening socket")
        host, port = servers[0].sockets[0].getsockname()[:2]
        return f"http://{host}:{port}"

    def _stop_uvicorn(self) -> None:
        server = self._uvicorn
        self._uvicorn = None
        if server is None:
            return
        server.should_exit = True
        thread = self._thread
        self._thread = None
        if thread is None:
            return
        thread.join(timeout=self.config.budgets.shutdown_drain + 5.0)
        if thread.is_alive():
            server.force_exit = True
            thread.join(timeout=5.0)
        if thread.is_alive():
            self._uvicorn, self._thread = server, thread
            raise StartupRefusal("GUI_HTTP_DRAIN_FAILED", "request listener did not drain")

    def shutdown(self, *, remove_capability: bool = True) -> None:
        if not self._started or self._stop.is_set():
            return
        self._stop.set()
        if not self._listen:
            with self._refresh_lock:
                self._binding = replace(self._binding, admit_after=math.inf)
        self._stop_uvicorn()
        self._close_runtime(remove_capability=remove_capability)
        self._started = False
        self._log("stopped")

    def _close_runtime(self, *, remove_capability: bool = True) -> None:
        if not self._listen:
            return
        try:
            if remove_capability:
                remove_server_json(self.config.runtime_dir)
        finally:
            self.lock.release()

    # -- catalog / handshake --------------------------------------------

    def _snapshot(
        self, timeout: float, request_once: Callable[..., Any]
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        socket_path = self.config.socket_path
        ping = request_once("ping", {}, socket_path=socket_path, timeout=timeout)
        catalog = request_once("daemon.catalog", {}, socket_path=socket_path, timeout=timeout)
        return ping, catalog

    def _fetch(
        self, *, note: str, request_once: Callable[..., Any] | None = None
    ) -> tuple[CatalogView, dict[str, Any]]:
        request_once = self.request_once if request_once is None else request_once
        last_reason = "EPOCH_INCONSISTENT"
        local_entries = local_daemon_catalog()["methods"]
        for attempt in range(3):
            try:
                ping, catalog = self._snapshot(max(3.0, 5.0 - attempt), request_once)
            except (RequestPortError, DaemonRequestError, OSError, TimeoutError) as error:
                raise StartupRefusal(
                    "BACKEND_UNAVAILABLE",
                    "the daemon is not reachable",
                    exit_code=3,
                ) from error
            if ping.get("epoch") != catalog.get("epoch"):
                last_reason = "EPOCH_INCONSISTENT"
                continue
            try:
                view = build_view(
                    ping,
                    catalog,
                    socket_path=str(self.config.socket_path),
                    local_entries=local_entries,
                )
            except CatalogProblem as problem:
                raise StartupRefusal(
                    "CATALOG_MISMATCH", problem.message, reason=problem.reason
                ) from problem
            return view, ping
        raise StartupRefusal(
            "CATALOG_MISMATCH",
            "daemon ping and catalog snapshots do not share one epoch",
            reason=last_reason,
        )

    def _apply_view(self, view: CatalogView) -> None:
        with self._state_lock:
            if (
                view.declaration_digest != self._startup_declaration
                or view.http_contract_digest != self._startup_http
            ):
                self._catalog_stale = True
                raise HttpFailure(
                    "CATALOG_STALE",
                    "the daemon's HTTP contract differs from the one this entry published; reopen the GUI if this persists",
                    dispatch="not-dispatched",
                )
            previous = self._view
            if previous is not None and view.backend.epoch != previous.backend.epoch:
                if (
                    view.backend.version != previous.backend.version
                    or view.backend.node_id != previous.backend.node_id
                    or view.backend.owner != previous.backend.owner
                ):
                    self._catalog_stale = True
                    raise HttpFailure(
                        "CATALOG_MISMATCH",
                        "the daemon restarted with a different identity or version",
                        dispatch="not-dispatched",
                    )
            previous_states = (
                {name: route.state for name, route in previous.routes.items()}
                if previous is not None
                else {}
            )
            if previous_states != {
                name: route.state for name, route in view.routes.items()
            } and self._app is not None:
                # OpenAPI carries the catalog state for each operation.  Do not
                # serve the startup snapshot after a not-ready/available change.
                self._app.openapi_schema = None
            self._view = view
            self._catalog_stale = False
            self._needs_rehandshake = False
            self._snapshot_at = time.monotonic()
            if previous is not None and view.backend.epoch != previous.backend.epoch:
                try:
                    self._write_capability_file()
                except StartupRefusal:
                    self._catalog_stale = True

    def refresh(self, *, force: bool) -> tuple[CatalogView, str]:
        """Return a fresh view; epoch changes go through the same re-handshake."""

        with self._refresh_lock:
            with self._state_lock:
                current = self._view
                fresh = time.monotonic() - self._snapshot_at < 1.0
            if (
                current is not None
                and fresh
                and not force
                and not self._needs_rehandshake
                and not self._catalog_stale
            ):
                return current, self._phase
            try:
                view, ping = self._fetch(note="refresh")
            except StartupRefusal as refusal:
                if refusal.code == "BACKEND_UNAVAILABLE":
                    raise HttpFailure(
                        ipc_errors.DAEMON_UNAVAILABLE,
                        "the daemon is not reachable",
                        dispatch="not-dispatched",
                    ) from refusal
                raise HttpFailure(
                    "CATALOG_STALE",
                    refusal.message,
                    dispatch="not-dispatched",
                    data={"reason": refusal.reason},
                ) from refusal
            self._apply_view(view)
            self._phase = str(ping["phase"])
            return view, self._phase

    def current_view(self) -> CatalogView:
        assert self._view is not None
        return self._view

    def rebind(self, config: RequestHttpConfig, *, request_once: Callable[..., Any]) -> None:
        """Install another principal behind the same listener, with a new bearer.

        The caller has already closed the previous daemon handle.  Everything
        that can refuse runs against the new handle before anything is
        installed; the install is one assignment of one ``_Binding``.
        """

        with self._refresh_lock:
            if not self._listen and self._stop.is_set():
                raise RuntimeError("the stopped server cannot be rebound")
            current = self._binding.config
            if current.principal_kind != "local-gui" or any(
                getattr(config, field) != getattr(current, field)
                for field in ("host", "runtime_dir", "socket_path", "principal_kind")
            ):
                raise StartupRefusal(
                    "PRINCIPAL_CONFIG_MISMATCH",
                    "a rebind keeps the listener, runtime directory, socket and principal kind",
                )
            _check_binding(config, request_once)
            view, ping = self._fetch(note="rebind", request_once=request_once)
            self._verify_identity(view, ping, config=config)
            if (
                view.declaration_digest != self._startup_declaration
                or view.http_contract_digest != self._startup_http
            ):
                raise StartupRefusal(
                    "CATALOG_MISMATCH",
                    "the daemon contract differs from the published one",
                    reason="CONTRACT_CHANGED",
                )
            closed = _Binding(config, request_once, secrets.token_urlsafe(32), math.inf)
            with self._state_lock:
                self._binding = closed
                self._view = view
                self._phase = str(ping["phase"])
                self._snapshot_at = time.monotonic()
                self._catalog_stale = False
                self._needs_rehandshake = False
                if self._app is not None:
                    self._app.openapi_schema = None
            self._binding = replace(closed, admit_after=time.monotonic())
            try:
                self._write_capability_file()
                if self._on_rebound is not None:
                    self._on_rebound(Rebound(self._binding.token, config.owner, config.actor))
            except BaseException:
                self._binding = replace(self._binding, admit_after=math.inf)
                raise
            self._log("rebound", phase=self._phase)

    # -- request handling ------------------------------------------------

    def _error_payload(self, failure: HttpFailure, request_id: str, method: str | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "schemaVersion": 1,
            "ok": False,
            "requestId": request_id,
            "code": failure.code,
            "message": failure.message,
            "dispatch": failure.dispatch,
        }
        if method is not None:
            payload["method"] = method
        if failure.data is not None:
            payload["data"] = failure.data
        return payload

    def _admit(self, started: float) -> _Binding:
        binding = self._binding
        if started <= binding.admit_after:
            raise _superseded()
        return binding

    def _still(self, binding: _Binding) -> None:
        if self._binding is not binding:
            raise _superseded()

    def _ready(self, request_id: str, started: float) -> _Envelope:
        try:
            binding = self._admit(started)
            view, phase = self.refresh(force=True)
            self._still(binding)
        except HttpFailure as failure:
            payload = {
                "schemaVersion": 1,
                "ok": False,
                "ready": False,
                "transport": "ready",
                "code": failure.code,
                "reason": failure.data.get("reason") if isinstance(failure.data, dict) else None,
                "requestId": request_id,
            }
            return _Envelope(failure.status, payload, failure.code, failure.dispatch)
        not_ready = sorted(
            name
            for name, route in view.routes.items()
            if route.state != "available" and route.reason not in PHASE_REASONS
        )
        ready = (phase in {"serving", "reconciled"} or (phase == "bootstrap" and binding.config.principal_kind == "local-gui")) and not not_ready
        payload = {
            "ok": ready,
            "ready": ready,
            "transport": "ready",
            "backend": {
                "available": True,
                "version": view.backend.version,
                "nodeId": view.backend.node_id,
                "owner": view.backend.owner,
                "epoch": view.backend.epoch,
            },
            "catalog": {
                "fresh": True,
                "catalogDigest": view.http_contract_digest,
                "declarationDigest": view.declaration_digest,
            },
            "methods": [
                {"name": route.name, "state": route.state}
                for route in sorted(view.routes.values(), key=lambda item: item.name)
            ],
            "notReady": not_ready,
        }
        if not ready and phase not in {"serving", "reconciled"}:
            payload["reason"] = "RESTORE_IN_PROGRESS"
            payload["code"] = ipc_errors.DAEMON_RESTORING
        elif not_ready:
            payload["code"] = "HTTP_NOT_READY"
        return _Envelope(200 if ready else 503, payload)

    def _catalog(self, request_id: str, started: float) -> _Envelope:
        try:
            binding = self._admit(started)
            view, _phase = self.refresh(force=True)
            self._still(binding)
        except HttpFailure as failure:
            return _Envelope(failure.status, self._error_payload(failure, request_id), failure.code, failure.dispatch)
        payload = {
            "schemaVersion": 1,
            "ok": True,
            "httpContractVersion": HTTP_CONTRACT_VERSION,
            "catalogVersion": view.catalog_version,
            "catalogDigest": view.http_contract_digest,
            "declarationDigest": view.declaration_digest,
            "generatedFrom": "daemon.catalog",
            "backend": {
                "kind": view.backend.kind,
                "version": view.backend.version,
                "nodeId": view.backend.node_id,
                "owner": view.backend.owner,
                "epoch": view.backend.epoch,
            },
            "principal": {
                "kind": binding.config.principal_kind,
                "owner": binding.config.owner,
                "actor": binding.config.actor,
            },
            "methods": [
                route.public_json()
                for route in sorted(view.routes.values(), key=lambda item: item.name)
            ],
        }
        return _Envelope(200, payload)

    def _invoke(
        self,
        name: str,
        request_id: str,
        started: float,
        params: dict[str, Any],
        expected_digest: str | None,
    ) -> _Envelope:
        try:
            admitted = self._admit(started)
            view, _phase = self.refresh(force=False)
            self._still(admitted)
        except HttpFailure as failure:
            return _Envelope(failure.status, self._error_payload(failure, request_id, name), failure.code, failure.dispatch)
        if expected_digest is not None and expected_digest != view.http_contract_digest:
            failure = HttpFailure(
                "CATALOG_MISMATCH",
                "request catalog digest differs from the served contract",
                status=409,
            )
            return _Envelope(409, self._error_payload(failure, request_id, name), failure.code, failure.dispatch)
        route = view.routes.get(name)
        if route is None:
            failure = HttpFailure(ipc_errors.METHOD_NOT_FOUND, "unknown or unexposed method", status=404)
            return _Envelope(404, self._error_payload(failure, request_id, name), failure.code, failure.dispatch)
        if route.state != "available" or not route.callable:
            failure = HttpFailure(
                "HTTP_NOT_READY",
                _NOT_READY_MESSAGES.get(route.reason or "", "method is not ready"),
                data={"reason": route.reason or "HTTP_NOT_READY"},
            )
            return _Envelope(503, self._error_payload(failure, request_id, name), failure.code, failure.dispatch)

        # MCP adds the daemon-verified ``actor``/``sessionRef`` pair after the
        # tool's own params.  The daemon schema must admit that envelope so
        # the session fence can run, but a browser is not an MCP session and
        # must never be allowed to self-report either field over HTTP.
        forbidden = sorted(field for field in route.http_forbidden if field in params)
        if forbidden:
            failure = HttpFailure(
                ipc_errors.INVALID_ARGUMENT,
                "daemon session identity is not accepted over HTTP: "
                + ", ".join(forbidden),
                status=400,
            )
            return _Envelope(400, self._error_payload(failure, request_id, name), failure.code, failure.dispatch)

        binding = route.http_binding["identity"]
        mode = binding["mode"]
        param: str | None = None
        selector_forbidden = False
        selector_in_body = False
        expected_selector: str | None = None
        if mode in {"local-owner-selector", "local-owner-user-selector"}:
            expected_selector = canonical_user_uri(admitted.config.owner)
        if expected_selector is not None:
            param = str(binding["param"])
            selector_forbidden = param in route.http_forbidden
            selector_in_body = param in params
            if not selector_forbidden and param not in params:
                params[param] = expected_selector
        error = _first_schema_error(route.params_schema, params)
        if error is not None:
            path = "/".join(str(part) for part in error.absolute_path) or "<root>"
            failure = HttpFailure(
                ipc_errors.INVALID_ARGUMENT,
                f"{name} params do not match the declared schema at {path}",
                status=400,
            )
            return _Envelope(400, self._error_payload(failure, request_id, name), failure.code, failure.dispatch)
        # Host-only fields are injected after the body schema check so the
        # HTTP projection can omit them from properties/required entirely.
        if selector_forbidden and param is not None:
            params[param] = expected_selector
        on_behalf_of = route.http_binding.get("onBehalfOf")
        if isinstance(on_behalf_of, str) and on_behalf_of:
            params[on_behalf_of] = admitted.config.actor
        if param is not None:
            provided = params.get(param)
            if (
                provided != expected_selector
                or (
                    selector_in_body
                    and mode in {"local-owner-selector", "local-owner-user-selector"}
                )
            ):
                failure = HttpFailure(
                    "PRINCIPAL_MISMATCH",
                    "selector does not match the configured local operator",
                    status=403,
                    data={"field": param},
                )
                return _Envelope(403, self._error_payload(failure, request_id, name), failure.code, failure.dispatch)

        request_budget = admitted.config.budgets.request_timeout
        if name == "agent.secret.provider-test" and "timeoutSeconds" in params:
            request_budget = min(65, params["timeoutSeconds"] + 5)
        timeout = request_budget - (time.monotonic() - started)
        if timeout <= 0:
            failure = HttpFailure(
                "HTTP_TIMEOUT",
                "request budget expired before dispatch",
                status=408,
            )
            return _Envelope(408, self._error_payload(failure, request_id, name), failure.code, failure.dispatch)
        try:
            result = admitted.request_once(
                name, params, socket_path=admitted.config.socket_path, timeout=timeout
            )
        except Exception as error:  # classified below; never logs params
            return self._daemon_failure(error, route, request_id, name)
        if _first_schema_error(route.result_schema, result) is not None:
            failure = HttpFailure(
                "INVALID_RESPONSE",
                "daemon returned a result outside the declared schema",
                status=502,
                dispatch="unknown",
            )
            return _Envelope(502, self._error_payload(failure, request_id, name), failure.code, failure.dispatch)
        payload = {
            "schemaVersion": 1,
            "ok": True,
            "method": name,
            "requestId": request_id,
            "dispatch": "dispatched",
            "result": result,
        }
        self._log("request", requestId=request_id, method=name, outcome="ok", dispatch="dispatched")
        return _Envelope(200, payload)

    def _daemon_failure(self, error: Exception, route: Route, request_id: str, name: str) -> _Envelope:
        code: str
        dispatch = "unknown"
        data: Any = None
        if isinstance(error, ipc_errors.DaemonUnavailableError):
            code, dispatch = ipc_errors.DAEMON_UNAVAILABLE, "not-dispatched"
        elif isinstance(error, ipc_errors.DaemonRestoringError):
            code, dispatch = ipc_errors.DAEMON_RESTORING, "not-dispatched"
        elif isinstance(error, ipc_errors.DaemonDisconnectedError):
            code = ipc_errors.DAEMON_DISCONNECTED
        elif isinstance(error, ipc_errors.IpcTimeoutError):
            code = ipc_errors.IPC_TIMEOUT
        elif isinstance(error, (RequestPortError, DaemonRequestError)):
            raw = getattr(error, "code", ipc_errors.DAEMON_ERROR)
            data = getattr(error, "data", None)
            if raw == "ACCOUNT_REQUIRED":
                code, dispatch = "HTTP_NOT_READY", "not-dispatched"
                data = {"reason": raw}
                self._needs_rehandshake = True
            elif raw in {"INVALID_JSON", "INVALID_RESPONSE", "IPC_RESPONSE_TOO_LARGE"}:
                code = "INVALID_RESPONSE"
            elif raw == ipc_errors.VERSION_MISMATCH:
                code = ipc_errors.VERSION_MISMATCH
            elif any(item.get("code") == raw for item in route.errors):
                code, dispatch = raw, "dispatched"
            else:
                code = ipc_errors.DAEMON_ERROR
        else:
            code = ipc_errors.DAEMON_ERROR
        if dispatch == "unknown":
            self._needs_rehandshake = True
        status = status_for(code)
        failure = HttpFailure(code, _safe_daemon_message(code), status=status, dispatch=dispatch)
        if code == "HTTP_NOT_READY":
            failure.data = data
        elif dispatch == "dispatched" and data is not None:
            failure.data = _declared_error_data(route, code, data)
        self._log(
            "request",
            requestId=request_id,
            method=name,
            outcome=code,
            dispatch=dispatch,
        )
        return _Envelope(status, self._error_payload(failure, request_id, name), failure.code, failure.dispatch)

    def _log(self, event: str, **fields: Any) -> None:
        record = {"event": event, **fields}
        print(json.dumps(record, separators=(",", ":"), sort_keys=True), file=sys.stderr, flush=True)


_NOT_READY_MESSAGES = {
    "ACCOUNT_REQUIRED": "no account on this home yet; sign in first",
    "BOOTSTRAP_ONLY": "this method is only available before an account is initialized",
}


def _superseded() -> HttpFailure:
    return HttpFailure(
        ipc_errors.DAEMON_UNAVAILABLE,
        "the principal of this entry changed; retry with the published bearer",
        dispatch="not-dispatched",
    )


def _first_schema_error(schema: Any, value: Any) -> Any | None:
    validator = Draft202012Validator(_plain(schema), format_checker=FormatChecker())
    error = next(iter(sorted(validator.iter_errors(value), key=lambda item: tuple(str(part) for part in item.absolute_path))), None)
    return error


def _plain(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


def _safe_daemon_message(code: str) -> str:
    if code == "HTTP_NOT_READY":
        return _NOT_READY_MESSAGES["ACCOUNT_REQUIRED"]
    if code in {
        ipc_errors.DAEMON_ERROR,
        "INVALID_RESPONSE",
        ipc_errors.IPC_TIMEOUT,
        ipc_errors.DAEMON_DISCONNECTED,
        ipc_errors.DAEMON_UNAVAILABLE,
    }:
        return "the daemon did not return a usable result"
    return "the daemon refused the request"


def _declared_error_data(route: Route, code: str, data: Any) -> Any | None:
    declared = next((item for item in route.errors if item.get("code") == code), None)
    if declared is None or not isinstance(data, dict):
        return None
    schema = declared.get("dataSchema")
    if not isinstance(schema, dict):
        return None
    return data if _first_schema_error(schema, data) is None else None


__all__ = ["Rebound", "RequestHttpServer", "StartupRefusal"]
