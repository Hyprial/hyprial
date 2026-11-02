"""App-scoped observations and owned, cancellable authorization attempts."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path
import threading
import time
from typing import Any
from urllib.parse import parse_qsl, urlsplit
from uuid import uuid4

from .inventory import configuration, unknown
from .process import AuthorizationEffect

_ACTIVE = {"starting", "awaiting_user", "checking"}
_TERMINAL = {"succeeded", "denied", "expired", "cancelled", "failed", "unknown"}
_HOSTS = {"accounts.feishu.cn", "accounts.larksuite.com", "open.feishu.cn", "open.larksuite.com"}
_SECRET_FIELDS = {"device_code", "client_secret", "app_secret", "access_token", "refresh_token"}


def _prompt(value: dict[str, Any]) -> tuple[str, int] | None:
    url, ttl = value.get("verificationUrl"), value.get("expiresIn")
    if (not isinstance(url, str) or len(url) > 16384 or type(ttl) is not int or ttl <= 0
            or any(ord(char) < 32 for char in url)):
        return None
    try:
        parsed = urlsplit(url)
        if (parsed.scheme != "https" or parsed.hostname not in _HOSTS or parsed.username
                or parsed.password or parsed.port not in (None, 443) or parsed.fragment
                or any(key.lower() in _SECRET_FIELDS for key, _ in parse_qsl(parsed.query))):
            return None
    except ValueError:
        return None
    return url, min(ttl, 900)


def _inventory(value: Any, adapter: str, app_id: str | None = None) -> dict[str, Any]:
    if (not isinstance(value, dict) or value.get("adapter") != adapter
            or value.get("authorization") not in {"authorized", "unauthorized", "unconfirmed", "unconfigured"}
            or not isinstance(value.get("capabilities"), list)
            or (app_id is not None and value.get("appId") != app_id)):
        return unknown(adapter, "INVALID_SCOPE_INVENTORY", app_id)
    return deepcopy({key: value[key] for key in (
        "adapter", "appId", "authorization", "capabilities", "checkedAt", "reason",
        "missingCapabilities",
    ) if key in value})


class LarkAuthorizationService:
    def __init__(self, home: Path, state_dir: Path, *, registrar=None, effects=None,
                 clock=time.time, inventory_timeout=8.0, startup_timeout=10.0,
                 authorization_timeout=900.0) -> None:
        if min(inventory_timeout, startup_timeout, authorization_timeout) <= 0:
            raise ValueError("authorization deadlines must be positive")
        self._home, self._state_dir = Path(home), Path(state_dir)
        self._registrar = registrar or AuthorizationEffect
        self._effects = effects or ThreadPoolExecutor(max_workers=4, thread_name_prefix="lark-authorization")
        self._owns_effects = effects is None
        self._clock = clock
        self._inventory_timeout = inventory_timeout
        self._startup_timeout = startup_timeout
        self._authorization_timeout = authorization_timeout
        self._lock = threading.RLock()
        self._reserved: set[str] = set()
        self._closed = False
        self._operations: dict[str, dict[str, Any]] = {}
        self._handles: dict[str, Any] = {}
        self._grants: dict[str, dict[str, Any]] = {}

    def _job(self, action: str, adapter: str, app_id: str | None = None) -> dict[str, Any]:
        return {"action": action, "home": str(self._home), "stateDir": str(self._state_dir),
                "adapter": adapter, "appId": app_id}

    def _reserve(self, identity: str) -> bool:
        # Callers hold _lock; reservations include queued and uncertain workers.
        if len(self._reserved) >= 4:
            return False
        self._reserved.add(identity)
        return True

    def _cleanup(self, identity: str) -> bool:
        # Serialize bounded close attempts with admission and cancellation. Only
        # proven cleanup releases custody/capacity, including after host close.
        with self._lock:
            handle = self._handles.get(identity)
            try:
                if handle is not None:
                    handle.close()
            except Exception:  # noqa: BLE001 - retain the exact owned effect
                op = self._operations.get(identity)
                if op is not None:
                    op.update(status="unknown", errorCode="WORKER_CLEANUP_UNCONFIRMED")
                    op.pop("verificationUrl", None)
                    op.pop("expiresAt", None)
                return False
            self._handles.pop(identity, None)
            self._reserved.discard(identity)
            return True

    def _cancel_owned(self, identity: str, reason: str) -> bool:
        with self._lock:
            op = self._operations[identity]
            cancellable = op["status"] in _ACTIVE | {"unknown"}
            self._settle(identity, "cancelled", error=reason)
            if not self._cleanup(identity):
                return False
            if cancellable:
                # Unknown outcomes settle only after owned cleanup is proven;
                # cancellation leaves the independent grant snapshot intact.
                op.update(status="cancelled", errorCode=reason)
                op.pop("verificationUrl", None)
                op.pop("expiresAt", None)
            return True

    def status(self, adapter: str) -> dict[str, Any]:
        """Independent tenant-grant query, bounded to inventory_timeout + cleanup."""
        if not isinstance(adapter, str) or not adapter.strip():
            raise ValueError("adapter must be a non-empty string")
        identity = uuid4().hex
        result = unknown(adapter, "SCOPE_QUERY_UNAVAILABLE")
        with self._lock:
            if self._closed:
                return unknown(adapter, "SERVICE_CLOSED")
            if not self._reserve(identity):
                return unknown(adapter, "AUTHORIZATION_BUSY")
        try:
            with self._lock:
                if self._closed:
                    return unknown(adapter, "SERVICE_CLOSED")
                handle = self._registrar(self._job("inventory", adapter))
                self._handles[identity] = handle
            deadline = time.monotonic() + self._inventory_timeout
            while time.monotonic() < deadline:
                with self._lock:
                    if self._closed:
                        break
                value = handle.snapshot()
                if value is not None:
                    if value.get("status") == "completed":
                        result = _inventory(value.get("authorization"), adapter)
                    break
                time.sleep(0.02)
        except Exception:  # noqa: BLE001 - no raw worker/platform errors in public results
            result = unknown(adapter, "SCOPE_QUERY_UNAVAILABLE")
        finally:
            if not self._cleanup(identity):
                result = unknown(adapter, "WORKER_CLEANUP_UNCONFIRMED")
        with self._lock:
            self._grants[adapter] = deepcopy(result)
            for op in self._operations.values():
                if (op["adapter"] == adapter and op["appId"] == result.get("appId")
                        and op["status"] == "unknown"
                        and result["authorization"] in {"authorized", "unauthorized"}):
                    op["authorization"] = deepcopy(result)
                    if op["operationId"] not in self._reserved:
                        op["status"] = "succeeded" if result["authorization"] == "authorized" else "failed"
                        op.pop("errorCode", None)
        return deepcopy(result)

    def start(self, adapter: str) -> dict[str, Any]:
        """Start once per App/adapter; an unknown outcome requires a fresh query."""
        with self._lock:
            if self._closed:
                raise RuntimeError("authorization service is closed")
            configured = configuration(self._home, self._state_dir, adapter)
            app_id = configured["appId"]
            for op in self._operations.values():
                if op["adapter"] != adapter:
                    continue
                if op["appId"] != app_id:
                    identity = op["operationId"]
                    if not self._cancel_owned(identity, "CONFIGURATION_CHANGED"):
                        return deepcopy(op)
            for op in self._operations.values():
                if (op["adapter"] == adapter and op["appId"] == app_id
                        and op["status"] in _ACTIVE | {"unknown"}):
                    return deepcopy(op)
            grant = self._grants.get(adapter, configured)
            if grant.get("appId") != app_id:
                grant = configured
            op = {"operationId": uuid4().hex, "adapter": adapter, "appId": app_id,
                  "status": "starting", "authorization": deepcopy(grant)}
            if len(self._operations) >= 128:
                oldest = next((key for key, value in self._operations.items()
                               if value["status"] in _TERMINAL - {"unknown"} and key not in self._reserved), None)
                if oldest is None:
                    raise RuntimeError("authorization operation capacity reached")
                self._operations.pop(oldest)
            self._operations[op["operationId"]] = op
            if app_id is None:
                op.update(status="failed", errorCode=configured["reason"])
            elif not self._reserve(op["operationId"]):
                op.update(status="failed", errorCode="AUTHORIZATION_BUSY")
            else:
                try:
                    self._effects.submit(self._monitor, op["operationId"])
                except Exception:  # noqa: BLE001 - no worker admitted
                    self._cleanup(op["operationId"])
                    op.update(status="failed", errorCode="AUTHORIZATION_UNAVAILABLE")
            return deepcopy(op)

    def operation(self, operation_id: str) -> dict[str, Any]:
        with self._lock:
            return deepcopy(self._operations[operation_id])

    def current_operation(self, adapter: str) -> dict[str, Any] | None:
        """Latest host-local attempt, including a terminal result after refresh."""
        with self._lock:
            return next((deepcopy(op) for op in reversed(self._operations.values())
                         if op["adapter"] == adapter), None)

    def _settle(self, operation_id: str, status: str, grant=None, error=None) -> None:
        with self._lock:
            op = self._operations.get(operation_id)
            if op is None or op["status"] not in _ACTIVE:
                return
            op["status"] = status
            op.pop("verificationUrl", None)
            op.pop("expiresAt", None)
            if grant is not None:
                op["authorization"] = deepcopy(grant)
                self._grants[op["adapter"]] = deepcopy(grant)
            if error is not None:
                op["errorCode"] = error

    def _monitor(self, operation_id: str) -> None:
        terminal, grant, error = "unknown", None, "AUTHORIZATION_TIMEOUT"
        try:
            with self._lock:
                op = self._operations[operation_id]
                if self._closed or op["status"] not in _ACTIVE:
                    return
                adapter, app_id = op["adapter"], op["appId"]
                handle = self._registrar(self._job("authorize", adapter, app_id))
                self._handles[operation_id] = handle
            started = time.monotonic()
            deadline = started + self._startup_timeout
            overall = started + self._authorization_timeout
            phase = "starting"
            while time.monotonic() < min(deadline, overall):
                with self._lock:
                    if self._closed or self._operations[operation_id]["status"] not in _ACTIVE:
                        return
                value = handle.snapshot()
                if value is None:
                    time.sleep(0.02)
                    continue
                state = value.get("status")
                if state == "completed":
                    grant = _inventory(value.get("authorization"), adapter, app_id)
                    terminal = {"authorized": "succeeded", "unauthorized": "failed"}.get(grant["authorization"], "unknown")
                    error = None if terminal == "succeeded" else "SCOPE_GRANTS_UNCONFIRMED"
                    break
                if state in _TERMINAL - {"succeeded"}:
                    terminal, error = state, "AUTHORIZATION_" + state.upper()
                    break
                if state == "awaiting_user" and phase == "starting":
                    prompt = _prompt(value)
                    if prompt is None:
                        terminal, error = "failed", "INVALID_VERIFICATION_PROMPT"
                        break
                    url, ttl = prompt
                    deadline, phase = time.monotonic() + ttl, "awaiting_user"
                    with self._lock:
                        if op["status"] in _ACTIVE:
                            op.update(status=phase, verificationUrl=url, expiresAt=self._clock() + ttl)
                elif state == "checking" and phase != "checking":
                    deadline, phase = time.monotonic() + self._inventory_timeout, "checking"
                    with self._lock:
                        if op["status"] in _ACTIVE:
                            op["status"] = phase
                elif state not in _ACTIVE:
                    error = "INVALID_AUTHORIZATION_RESULT"
                    break
                time.sleep(0.02)
            else:
                if phase == "awaiting_user":
                    terminal, error = "expired", "AUTHORIZATION_EXPIRED"
        except Exception:  # noqa: BLE001 - transport failure is not denial
            terminal, error = "unknown", "AUTHORIZATION_WORKER_FAILED"
        finally:
            if self._cleanup(operation_id):
                self._settle(operation_id, terminal, grant, error)

    def cancel(self, operation_id: str) -> dict[str, Any]:
        with self._lock:
            self._cancel_owned(operation_id, "AUTHORIZATION_CANCELLED")
        return self.operation(operation_id)

    def close(self) -> None:
        with self._lock:
            self._closed = True
            for identity in self._operations:
                self._settle(identity, "cancelled", error="SERVICE_CLOSED")
            identities = list(self._reserved)
        try:
            for identity in identities:
                self._cleanup(identity)
        finally:
            if self._owns_effects:
                self._effects.shutdown(wait=True, cancel_futures=True)
        with self._lock:
            if self._handles:
                raise RuntimeError("authorization worker cleanup is unconfirmed") from None


def create_lark_authorization_service(home: Path, state_dir: Path, **options) -> LarkAuthorizationService:
    """Create a service owned by a trusted host; the host must call close().

    registrar(job) is an injectable owned-effect factory whose snapshot() is
    nonblocking and close() is bounded and idempotent. effects optionally
    supplies an executor; its owner retains shutdown responsibility. clock
    supplies Unix seconds for browser-visible expiry. The default registrar
    owns a subprocess, with no ambient user credential environment.
    """
    return LarkAuthorizationService(home, state_dir, **options)
