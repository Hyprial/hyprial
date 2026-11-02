"""FastAPI/Uvicorn lifecycle, daemon dispatch and transport envelopes."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
import secrets
import sys
import threading
import time
from typing import TYPE_CHECKING, Any, Callable

from jsonschema import Draft202012Validator, FormatChecker

from hyprial import __version__
from hyprial.daemon import daemon_catalog as local_daemon_catalog
from hyprial.kernel import (
    DaemonRequestError,
    RequestPortError,
    daemon_request_once,
    ipc_errors,
    parse_agent_uri,
)

from .catalog import (
    CatalogProblem,
    CatalogView,
    HTTP_CONTRACT_VERSION,
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


class RequestHttpServer:
    """Owns one listener, its capability file and the daemon catalog snapshot."""

    def __init__(
        self,
        config: RequestHttpConfig,
        *,
        request_once: Callable[..., Any] | None = None,
    ) -> None:
        self.config = config
        self.request_once = daemon_request_once if request_once is None else request_once
        self.token = secrets.token_urlsafe(32)
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

    # -- startup ---------------------------------------------------------

    def start(self) -> None:
        prepare_runtime_dir(self.config)
        self.lock.acquire()
        try:
            view, ping = self._fetch(note="startup")
            self._verify_identity(view, ping)
            self._view = view
            self._phase = str(ping["phase"])
            self._startup_declaration = view.declaration_digest
            self._startup_http = view.http_contract_digest
            self._snapshot_at = time.monotonic()
            for name, reason in sorted(view.skipped.items()):
                self._log("unsupported_method", method=name, reason=reason)
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

        from .app import build_app, http_protocol_class

        app = build_app(self)
        self._app = app
        config = uvicorn.Config(
            app,
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

    def _verify_identity(self, view: CatalogView, ping: dict[str, Any]) -> None:
        if ping.get("version") != __version__ or view.package_version != __version__:
            raise StartupRefusal(
                ipc_errors.VERSION_MISMATCH,
                f"daemon version {ping.get('version')!r} differs from package {__version__!r}",
            )
        if view.backend.owner != self.config.owner:
            raise StartupRefusal(
                "PRINCIPAL_CONFIG_MISMATCH",
                "configured principal.owner differs from the daemon owner",
            )
        parsed = parse_agent_uri(self.config.actor)
        if parsed is None or parsed[0] != view.backend.owner or parsed[1] != view.backend.node_id:
            raise StartupRefusal(
                "PRINCIPAL_CONFIG_MISMATCH",
                "configured principal.actor is not an agent of this daemon node",
            )

    def _write_capability_file(self) -> None:
        assert self._view is not None
        assert self._uvicorn is not None
        document = {
            "schemaVersion": 1,
            "pid": self._pid(),
            "endpoint": self.endpoint,
            "token": self.token,
            "httpContractVersion": HTTP_CONTRACT_VERSION,
            "catalogDigest": self._startup_http,
            "declarationDigest": self._startup_declaration,
            "packageVersion": __version__,
            "backend": self._view.backend.as_json(),
            "principal": {
                "kind": "local-operator",
                "owner": self.config.owner,
                "actor": self.config.actor,
            },
            "startedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        try:
            write_server_json(self.config.runtime_dir, document)
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

    def shutdown(self) -> None:
        if not self._started or self._stop.is_set():
            return
        self._stop.set()
        self._stop_uvicorn()
        self._close_runtime()
        self._started = False
        self._log("stopped")

    def _close_runtime(self) -> None:
        try:
            remove_server_json(self.config.runtime_dir)
        finally:
            self.lock.release()

    # -- catalog / handshake --------------------------------------------

    def _snapshot(self, timeout: float) -> tuple[dict[str, Any], dict[str, Any]]:
        ping = self.request_once(
            "ping", {}, socket_path=self.config.socket_path, timeout=timeout
        )
        catalog = self.request_once(
            "daemon.catalog", {}, socket_path=self.config.socket_path, timeout=timeout
        )
        return ping, catalog

    def _fetch(self, *, note: str) -> tuple[CatalogView, dict[str, Any]]:
        last_reason = "EPOCH_INCONSISTENT"
        local_entries = local_daemon_catalog()["methods"]
        for attempt in range(3):
            try:
                ping, catalog = self._snapshot(max(0.2, 2.0 - attempt * 0.5))
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
                    "daemon contract changed; restart the request-http process",
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

    def _ready(self, request_id: str, started: float) -> _Envelope:
        try:
            view, phase = self.refresh(force=True)
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
            name for name, route in view.routes.items() if route.state != "available"
        )
        ready = phase in {"serving", "reconciled"} and not not_ready
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
            view, _phase = self.refresh(force=True)
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
                "kind": "local-operator",
                "owner": self.config.owner,
                "actor": self.config.actor,
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
            view, _phase = self.refresh(force=False)
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
                "method is not ready",
                data={"reason": route.reason or "HTTP_NOT_READY"},
            )
            return _Envelope(503, self._error_payload(failure, request_id, name), failure.code, failure.dispatch)

        binding = route.http_binding["identity"]
        mode = binding["mode"]
        param: str | None = None
        if mode == "local-operator-selector":
            param = str(binding["param"])
            if param not in params:
                params[param] = self.config.actor
        error = _first_schema_error(route.params_schema, params)
        if error is not None:
            path = "/".join(str(part) for part in error.absolute_path) or "<root>"
            failure = HttpFailure(
                ipc_errors.INVALID_ARGUMENT,
                f"{name} params do not match the declared schema at {path}",
                status=400,
            )
            return _Envelope(400, self._error_payload(failure, request_id, name), failure.code, failure.dispatch)
        if param is not None:
            provided = params.get(param)
            if isinstance(provided, str) and provided != self.config.actor:
                failure = HttpFailure(
                    "PRINCIPAL_MISMATCH",
                    "selector does not match the configured local operator",
                    status=403,
                    data={"field": param},
                )
                return _Envelope(403, self._error_payload(failure, request_id, name), failure.code, failure.dispatch)

        timeout = self.config.budgets.request_timeout - (time.monotonic() - started)
        if timeout <= 0:
            failure = HttpFailure(
                "HTTP_TIMEOUT",
                "request budget expired before dispatch",
                status=408,
            )
            return _Envelope(408, self._error_payload(failure, request_id, name), failure.code, failure.dispatch)
        try:
            result = self.request_once(
                name, params, socket_path=self.config.socket_path, timeout=timeout
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
            if raw in {"INVALID_JSON", "INVALID_RESPONSE", "IPC_RESPONSE_TOO_LARGE"}:
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
        if dispatch == "dispatched" and data is not None:
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


__all__ = ["RequestHttpServer", "StartupRefusal"]
