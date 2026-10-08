"""Service-connect authority: catalog + protected record + durable ownership."""

from __future__ import annotations

import math
import logging
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping
from uuid import uuid4

from hyprial.daemon.impl.desired_state import DesiredState, ServiceRegistry
from hyprial.daemon.impl.desired_state_io import (
    DesiredStateIoCompleted,
    DesiredStateIoPort,
    DesiredStateIoRequest,
    DesiredStateOperation,
)
from hyprial.kernel import (
    AdmissionResult,
    EffectCompleted,
    SERVICE_CONTROL_TIMEOUT_SECONDS,
    SERVICE_RECONCILE_INTERVAL_SECONDS,
    SERVICE_REGISTRY_READ_TIMEOUT_SECONDS,
    ipc_errors,
)

from .io import ServiceIo
from .models import (
    CatalogEntry,
    ProtectedDevice,
    RegistrySnapshot,
    SERVICE_CODES,
    ServiceConnectError,
)
from .registry import CatalogRegistry
from .resolver import ProtectedResolver


_METHOD_FIELDS = {
    "service.connect": frozenset({"name"}),
    "service.disconnect": frozenset({"name"}),
    "service.guide": frozenset({"name"}),
    "service.list": frozenset(),
}
_MAPPING_FIELDS = frozenset(
    {
        "name",
        "deviceId",
        "remotePort",
        "recordGeneration",
        "localPort",
        "activeConnections",
        "path",
        "pathDetail",
        "observedAtMs",
        "lastDialState",
        "lastDialMs",
        "lastError",
        "lastErrorAtMs",
    }
)
_PRIVATE_ADDRESS_TEXT = re.compile(r"(?:^|[^A-Za-z0-9])tc[A-Za-z0-9_-]{8,}")
_LOG = logging.getLogger(__name__)


def _emit(level: int, event: str, **fields: object) -> None:
    """Emit only fixed diagnostics and explicitly non-secret fields."""

    _LOG.log(level, event, extra={"event": event, **fields})


@dataclass(frozen=True, slots=True)
class _Binding:
    name: str
    device_id: str
    remote_port: int
    record_generation: int
    local_port: int
    controller_id: int


@dataclass(slots=True)
class _PersistenceWaiter:
    ready: threading.Event
    request: DesiredStateIoRequest
    completion: DesiredStateIoCompleted | None = None
    detached: bool = False


def _invalid_request() -> ServiceConnectError:
    return ServiceConnectError(
        ipc_errors.INVALID_ARGUMENT, "service request is invalid"
    )


def _strict_int(value: object, *, minimum: int, maximum: int | None = None) -> int:
    if (
        type(value) is not int
        or value < minimum
        or (maximum is not None and value > maximum)
    ):
        raise ServiceConnectError(
            ipc_errors.SERVICE_UNAVAILABLE,
            "forwarding service returned an invalid observation",
        )
    return value


def _safe_optional_text(value: object) -> str | None:
    if value is None:
        return None
    if (
        not isinstance(value, str)
        or "nodekey:" in value
        or _PRIVATE_ADDRESS_TEXT.search(value)
        or any(ord(character) < 32 and character != "\t" for character in value)
    ):
        raise ServiceConnectError(
            ipc_errors.SERVICE_UNAVAILABLE,
            "forwarding service returned an invalid observation",
        )
    return value


def _mapping(value: object, entry: CatalogEntry, device: ProtectedDevice) -> dict[str, object]:
    if not isinstance(value, Mapping) or set(value) != _MAPPING_FIELDS:
        raise ServiceConnectError(
            ipc_errors.SERVICE_UNAVAILABLE,
            "forwarding service returned an invalid observation",
        )
    if (
        value["name"] != entry.name
        or value["deviceId"] != entry.device_id
        or value["remotePort"] != entry.remote_port
        or value["recordGeneration"] != device.generation
    ):
        raise ServiceConnectError(
            ipc_errors.SERVICE_UNAVAILABLE,
            "forwarding service returned an invalid observation",
        )
    _strict_int(value["localPort"], minimum=1024, maximum=65535)
    _strict_int(value["activeConnections"], minimum=0)
    if value["path"] not in {"direct", "relay", "unknown"}:
        raise ServiceConnectError(
            ipc_errors.SERVICE_UNAVAILABLE,
            "forwarding service returned an invalid observation",
        )
    if value["lastDialState"] not in {"unknown", "ok", "error"}:
        raise ServiceConnectError(
            ipc_errors.SERVICE_UNAVAILABLE,
            "forwarding service returned an invalid observation",
        )
    _safe_optional_text(value["pathDetail"])
    _safe_optional_text(value["lastError"])
    for field in ("observedAtMs", "lastErrorAtMs"):
        if value[field] is not None:
            _strict_int(value[field], minimum=0)
    duration = value["lastDialMs"]
    if duration is not None and (
        not isinstance(duration, int | float)
        or isinstance(duration, bool)
        or not math.isfinite(duration)
        or duration < 0
    ):
        raise ServiceConnectError(
            ipc_errors.SERVICE_UNAVAILABLE,
            "forwarding service returned an invalid observation",
        )
    return dict(value)


def _row(mapping: Mapping[str, object], *, state: str, error_code: str | None) -> dict[str, object]:
    result = dict(mapping)
    result.update({"state": state, "errorCode": error_code})
    return result


def _failed_row(
    name: str,
    entry: CatalogEntry | None,
    *,
    local_port: int | None,
    code: str,
) -> dict[str, object]:
    return {
        "name": name,
        "deviceId": None if entry is None else entry.device_id,
        "remotePort": None if entry is None else entry.remote_port,
        "recordGeneration": None,
        "localPort": local_port,
        "activeConnections": 0,
        "path": "unknown",
        "pathDetail": None,
        "observedAtMs": None,
        "lastDialState": "unknown",
        "lastDialMs": None,
        "lastError": "service mapping is unavailable",
        "lastErrorAtMs": None,
        "state": "failed",
        "errorCode": code,
    }


class ServiceManager:
    """Own service intents and mappings without becoming a second state writer."""

    def __init__(
        self,
        *,
        orgfs: object | None,
        state: object,
        persistence_late_result,
        controller_factory,
        access_path: str | Path,
        scheduler,
    ) -> None:
        self._orgfs = orgfs
        self._state = state
        self._controller_factory = controller_factory
        self.access_path = Path(access_path)
        self._resolver = ProtectedResolver(self.access_path)
        self._scheduler = scheduler
        self._io = ServiceIo()
        self._lock = threading.RLock()
        self._catalog_lock = threading.Lock()
        # Lock hierarchy: _lock and _catalog_lock are peers and are never nested.
        # Access/catalog I/O precedes state locking; diagnostics follow catalog I/O.
        self._persist_call_lock = threading.Lock()
        self._persistence_lock = threading.Lock()
        self._admission_closed = threading.Event()
        self._closed = False
        self._drained = False
        self._generation = 1
        self._version = 0
        self._persist_waiters: dict[tuple[str, int], _PersistenceWaiter] = {}
        self._reload_desired = False
        self._diagnostic_states: dict[str, tuple[object, ...]] = {}
        self._persistence = DesiredStateIoPort(
            state,
            complete=self._persistence_complete,
            late_result=persistence_late_result,
            capacity=16,
            retry_seconds=0.01,
        )
        snapshot = state.load()
        if not isinstance(snapshot, DesiredState):
            raise TypeError("service state port must load DesiredState")
        self._desired = {item.name: item.local_port for item in snapshot.service_connections}
        self._restore_pending = set(self._desired)
        self._cached_registry: ServiceRegistry | None = snapshot.service_registry
        self._rows: dict[str, dict[str, object]] = {}
        self._bindings: dict[str, _Binding] = {}
        self._teardowns: set[str] = set()
        self._schedule()

    def handle(self, method: str, params: dict) -> dict[str, object]:
        self._require_open()
        with self._lock:
            self._require_open()
            expected = _METHOD_FIELDS.get(method)
            if expected is None or not isinstance(params, dict) or set(params) != expected:
                raise _invalid_request()
            name = params.get("name")
            if method != "service.list" and not isinstance(name, str):
                raise _invalid_request()
        access = None
        catalog = None
        registry_error = None
        if method in {"service.list", "service.guide", "service.connect"}:
            try:
                access = self._read_access()
                catalog = self._read_catalog(access)
            except ServiceConnectError as error:
                if method == "service.connect":
                    raise
                catalog = CatalogRegistry(
                    None, space_id=None, owner=None, cached=None
                ).load()
                registry_error = str(error)
        with self._lock:
            self._require_open()
            if method == "service.list":
                assert catalog is not None
                return self._list(catalog, registry_error)
            assert isinstance(name, str)
            if method == "service.guide":
                assert catalog is not None
                return self._guide(name, catalog)
            if method == "service.connect":
                assert access is not None and catalog is not None
                return self._connect(name, access, catalog)
            return self._disconnect(name)

    def reconcile(self) -> None:
        self._require_open()
        with self._lock:
            self._require_open()
            self._refresh_desired_if_dirty()
        # OrgFS and protected-file reads are bounded external I/O. They must
        # not hold the manager state lock and stall list/connect/disconnect.
        try:
            access = self._read_access()
            catalog = self._read_catalog(access)
        except ServiceConnectError as error:
            try:
                self.access_path.lstat()
            except FileNotFoundError:
                access_missing = True
            except OSError:
                access_missing = False
            else:
                access_missing = False
            with self._lock:
                if self._closed:
                    return
                self._close_unauthorized(error.code)
                for name in tuple(self._restore_pending):
                    self._restore_failed(name, error.code)
                if access_missing and not self._desired:
                    self._emit_transition(
                        "reconcile",
                        logging.INFO,
                        "service.connect.reconcile.completed",
                        desiredCount=0,
                        failedCount=0,
                    )
                else:
                    self._emit_transition(
                        "reconcile",
                        logging.WARNING,
                        "service.connect.reconcile.failed",
                        code=error.code,
                        desiredCount=len(self._desired),
                    )
            return
        with self._lock:
            if self._closed:
                return
            controller = self._controller_factory()
            if controller is None or not bool(
                getattr(controller, "supports_service_connect", False)
            ):
                self._close_unauthorized(ipc_errors.SERVICE_SIDECAR_UNSUPPORTED)
                for name in tuple(self._restore_pending):
                    self._restore_failed(
                        name, ipc_errors.SERVICE_SIDECAR_UNSUPPORTED
                    )
                self._emit_transition(
                    "reconcile",
                    logging.WARNING,
                    "service.connect.reconcile.failed",
                    code=ipc_errors.SERVICE_SIDECAR_UNSUPPORTED,
                    desiredCount=len(self._desired),
                )
                return
            failures = 0
            for name in tuple(self._teardowns):
                try:
                    self._unmap(controller, name)
                except ServiceConnectError as error:
                    failures += 1
                    self._emit_transition(
                        f"mapping:{name}:unmap",
                        logging.WARNING,
                        "service.connect.mapping.failed",
                        serviceName=name,
                        code=error.code,
                        operation="unmap",
                    )
                    continue
                self._clear_transition(f"mapping:{name}:unmap")
                self._teardowns.discard(name)
                self._bindings.pop(name, None)
                self._rows.pop(name, None)
                _emit(
                    logging.INFO,
                    "service.connect.mapping.closed",
                    serviceName=name,
                    code=ipc_errors.SERVICE_UNAVAILABLE,
                )
            for name, remembered_port in tuple(self._desired.items()):
                entry = catalog.find(name)
                if entry is None:
                    self._close_one(name, controller, ipc_errors.SERVICE_NOT_REGISTERED)
                    self._restore_failed(name, ipc_errors.SERVICE_NOT_REGISTERED)
                    failures += 1
                    continue
                try:
                    device = access.resolve_device(entry.device_id, entry.remote_port)
                except ServiceConnectError as error:
                    self._access_invalid(name, error.code)
                    self._close_one(name, controller, error.code, entry)
                    self._restore_failed(name, error.code)
                    failures += 1
                    continue
                self._clear_transition(f"access:{name}")
                binding = self._bindings.get(name)
                if binding is not None and self._binding_matches(
                    binding, entry, device, remembered_port, controller
                ):
                    self._restore_pending.discard(name)
                    continue
                if binding is not None:
                    self._unmap(controller, name)
                    self._bindings.pop(name, None)
                    _emit(
                        logging.INFO,
                        "service.connect.mapping.closed",
                        serviceName=name,
                        code="binding-changed",
                    )
                try:
                    self._map(entry, device, remembered_port, controller)
                except ServiceConnectError as error:
                    self._restore_failed(name, error.code)
                    failures += 1
                    continue
                self._restore_pending.discard(name)
            self._emit_transition(
                "reconcile",
                logging.WARNING if failures else logging.INFO,
                (
                    "service.connect.reconcile.failed"
                    if failures
                    else "service.connect.reconcile.completed"
                ),
                desiredCount=len(self._desired),
                failedCount=failures,
            )

    def close(self, timeout: float) -> bool:
        self._admission_closed.set()
        deadline = time.monotonic() + max(0.0, timeout)
        if not self._lock.acquire(timeout=max(0.0, deadline - time.monotonic())):
            return False
        try:
            if self._drained:
                return True
            if not self._closed:
                self._closed = True
                self._scheduler.cancel("service-connect-reconcile")
                controller = self._controller_factory()
                if controller is not None:
                    for name in tuple(self._bindings):
                        try:
                            self._io.control(
                                controller,
                                "unmap_service",
                                (name,),
                                min(
                                    max(0.0, deadline - time.monotonic()),
                                    SERVICE_CONTROL_TIMEOUT_SECONDS,
                                ),
                            )
                        except BaseException:
                            pass
                self._bindings.clear()
        finally:
            self._lock.release()
        persistence_closed = self._persistence.close(
            max(0.0, deadline - time.monotonic())
        )
        effects_closed = self._io.close(max(0.0, deadline - time.monotonic()))
        scheduler_closed = self._scheduler.shutdown(
            max(0.0, deadline - time.monotonic())
        )
        drained = persistence_closed and effects_closed and scheduler_closed
        if not self._lock.acquire(timeout=max(0.0, deadline - time.monotonic())):
            return False
        try:
            self._drained = drained
        finally:
            self._lock.release()
        return drained

    def _require_open(self) -> None:
        if self._admission_closed.is_set() or self._closed:
            raise RuntimeError("service manager is closed")

    def _schedule(self) -> None:
        if self._admission_closed.is_set():
            return
        self._scheduler.schedule(
            "service-connect-reconcile",
            self._generation,
            SERVICE_RECONCILE_INTERVAL_SECONDS,
            self._scheduled_reconcile,
        )

    def _scheduled_reconcile(self, generation: int) -> None:
        if self._admission_closed.is_set():
            return
        with self._lock:
            if (
                self._admission_closed.is_set()
                or self._closed
                or generation != self._generation
            ):
                return
        try:
            self.reconcile()
        finally:
            with self._lock:
                if not self._admission_closed.is_set() and not self._closed:
                    self._schedule()

    def _read_access(self):
        try:
            access = self._io.read_protected(
                self._resolver, SERVICE_REGISTRY_READ_TIMEOUT_SECONDS
            )
            self._clear_transition("access:record")
            return access
        except ServiceConnectError as error:
            self._access_invalid(None, error.code)
            raise
        except BaseException as error:
            service_error = ServiceConnectError(
                ipc_errors.SERVICE_DEVICE_UNAVAILABLE,
                "protected service access unavailable",
            )
            self._access_invalid(None, service_error.code)
            raise service_error from error

    def _read_catalog(self, access) -> RegistrySnapshot:
        with self._catalog_lock:
            previous_cache = self._cached_registry
            trust = access.catalog_trust
            registry = CatalogRegistry(
                self._orgfs,
                space_id=None if trust is None else trust.space_id,
                owner=None if trust is None else trust.owner,
                cached=self._cached_registry,
            )
            try:
                snapshot = self._io.read_catalog(
                    registry, SERVICE_REGISTRY_READ_TIMEOUT_SECONDS
                )
            except BaseException as error:
                raise ServiceConnectError(
                    ipc_errors.SERVICE_REGISTRY_INVALID,
                    "service catalog is unavailable",
                ) from error
            cache_changed = (
                snapshot.source == "orgfs" and snapshot.cache != self._cached_registry
            )
            if cache_changed:
                self._cached_registry = snapshot.cache
        if cache_changed:
            try:
                self._persist(
                    DesiredStateOperation.SET_SERVICE_REGISTRY,
                    (snapshot.cache,),
                    context=("registry",),
                )
            except BaseException:
                with self._catalog_lock:
                    if self._cached_registry == snapshot.cache:
                        self._cached_registry = previous_cache
                raise
            _emit(
                logging.INFO,
                "service.connect.registry.updated",
                source=snapshot.source,
                entryCount=len(snapshot.entries),
            )
        if snapshot.error is not None:
            self._emit_transition(
                "registry-fallback",
                logging.WARNING,
                "service.connect.registry.fallback",
                source=snapshot.source,
                code=ipc_errors.SERVICE_REGISTRY_INVALID,
            )
        else:
            self._clear_transition("registry-fallback")
        return snapshot

    def _guide(self, name: str, catalog: RegistrySnapshot) -> dict[str, object]:
        entry = catalog.entry(name)
        return {"entry": entry.to_json(), "source": catalog.source}

    def _connect(self, name: str, access, catalog: RegistrySnapshot) -> dict[str, object]:
        entry = catalog.entry(name)
        controller = self._controller_factory()
        if controller is None or not bool(
            getattr(controller, "supports_service_connect", False)
        ):
            raise ServiceConnectError(
                ipc_errors.SERVICE_SIDECAR_UNSUPPORTED,
                "service data plane is unsupported",
            )
        try:
            device = access.resolve_device(entry.device_id, entry.remote_port)
        except ServiceConnectError as error:
            self._access_invalid(name, error.code)
            raise
        self._clear_transition(f"access:{name}")
        remembered = self._desired.get(name)
        requested_port = (
            remembered
            if remembered is not None and remembered > 0
            else entry.local_port if entry.local_port is not None else 0
        )
        binding = self._bindings.get(name)
        if binding is not None and self._binding_matches(
            binding, entry, device, requested_port, controller
        ):
            row = self._rows[name]
            return self._connect_result(entry, row)
        if binding is not None:
            self._unmap(controller, name)
            self._bindings.pop(name, None)
        if remembered is None:
            self._persist(
                DesiredStateOperation.SET_SERVICE_CONNECTION,
                (name, requested_port),
                context=("connect-intent", name),
            )
            self._desired[name] = requested_port
        try:
            row = self._map(entry, device, requested_port, controller)
        except ServiceConnectError as error:
            self._rows[name] = _failed_row(
                name,
                entry,
                local_port=requested_port or None,
                code=error.code,
            )
            raise
        actual_port = int(row["localPort"])
        try:
            self._persist(
                DesiredStateOperation.SET_SERVICE_CONNECTION,
                (name, actual_port),
                context=("connect-port", name),
            )
        except ServiceConnectError:
            self._unmap(controller, name)
            self._bindings.pop(name, None)
            self._rows[name] = _failed_row(
                name,
                entry,
                local_port=self._desired.get(name) or None,
                code=ipc_errors.SERVICE_UNAVAILABLE,
            )
            raise
        self._desired[name] = actual_port
        return self._connect_result(entry, row)

    def _map(
        self,
        entry: CatalogEntry,
        device: ProtectedDevice,
        local_port: int,
        controller: object,
    ) -> dict[str, object]:
        try:
            value = self._io.control(
                controller,
                "map_service",
                (
                    entry.name,
                    entry.device_id,
                    device.address,
                    device.server_public,
                    entry.remote_port,
                    device.generation,
                    local_port,
                ),
                SERVICE_CONTROL_TIMEOUT_SECONDS,
            )
            mapped = _mapping(value, entry, device)
        except ServiceConnectError as error:
            self._emit_transition(
                f"mapping:{entry.name}:map",
                logging.WARNING,
                "service.connect.mapping.failed",
                serviceName=entry.name,
                code=error.code,
                operation="map",
            )
            raise
        except BaseException as error:
            code = getattr(error, "code", ipc_errors.SERVICE_UNAVAILABLE)
            if code not in SERVICE_CODES:
                code = ipc_errors.SERVICE_UNAVAILABLE
            self._emit_transition(
                f"mapping:{entry.name}:map",
                logging.WARNING,
                "service.connect.mapping.failed",
                serviceName=entry.name,
                code=code,
                operation="map",
            )
            raise ServiceConnectError(code, "service mapping failed") from error
        self._clear_transition(f"mapping:{entry.name}:map")
        row = _row(mapped, state="listening", error_code=None)
        self._rows[entry.name] = row
        self._bindings[entry.name] = _Binding(
            entry.name,
            entry.device_id,
            entry.remote_port,
            device.generation,
            int(mapped["localPort"]),
            id(controller),
        )
        return row

    def _connect_result(
        self, entry: CatalogEntry, row: Mapping[str, object]
    ) -> dict[str, object]:
        port = row["localPort"]
        assert isinstance(port, int)
        return {
            "service": dict(row),
            "env": {key: value.replace("{port}", str(port)) for key, value in entry.env},
            "auth": entry.auth,
            "guide": entry.guide,
        }

    def _disconnect(self, name: str) -> dict[str, object]:
        desired = name in self._desired
        controller = self._controller_factory()
        if desired:
            self._persist(
                DesiredStateOperation.REMOVE_SERVICE_CONNECTION,
                (name,),
                context=("disconnect", name),
            )
            self._desired.pop(name, None)
        removed = False
        if controller is not None:
            try:
                removed = bool(
                    self._io.control(
                        controller,
                        "unmap_service",
                        (name,),
                        SERVICE_CONTROL_TIMEOUT_SECONDS,
                    )
                )
            except BaseException as error:
                self._teardowns.add(name)
                raise ServiceConnectError(
                    ipc_errors.SERVICE_UNAVAILABLE,
                    "service teardown failed",
                ) from error
        self._teardowns.discard(name)
        self._bindings.pop(name, None)
        self._rows.pop(name, None)
        return {"name": name, "removed": desired or removed}

    def _list(
        self, catalog: RegistrySnapshot, registry_error: str | None
    ) -> dict[str, object]:
        if registry_error is None:
            registry_error = catalog.error
        rows = []
        for name in sorted(self._desired):
            row = self._rows.get(name)
            if row is None:
                row = _failed_row(
                    name,
                    catalog.find(name),
                    local_port=self._desired[name] or None,
                    code=ipc_errors.SERVICE_UNAVAILABLE,
                )
            rows.append(dict(row))
        return {
            "services": rows,
            "registry": {
                "source": catalog.source,
                "spaceId": catalog.space_id,
                "owner": catalog.owner,
                "error": registry_error,
            },
        }

    def _binding_matches(
        self,
        binding: _Binding,
        entry: CatalogEntry,
        device: ProtectedDevice,
        local_port: int,
        controller: object,
    ) -> bool:
        return (
            binding.device_id == entry.device_id
            and binding.remote_port == entry.remote_port
            and binding.record_generation == device.generation
            and binding.local_port == local_port
            and binding.controller_id == id(controller)
        )

    def _unmap(self, controller: object, name: str) -> None:
        try:
            self._io.control(
                controller,
                "unmap_service",
                (name,),
                SERVICE_CONTROL_TIMEOUT_SECONDS,
            )
        except BaseException as error:
            raise ServiceConnectError(
                ipc_errors.SERVICE_UNAVAILABLE,
                "service teardown failed",
            ) from error

    def _close_one(
        self,
        name: str,
        controller: object,
        code: str,
        entry: CatalogEntry | None = None,
    ) -> None:
        had_binding = name in self._bindings
        if had_binding:
            try:
                self._unmap(controller, name)
            except ServiceConnectError:
                self._rows[name] = _failed_row(
                    name,
                    entry,
                    local_port=self._desired.get(name) or None,
                    code=code,
                )
                return
        self._bindings.pop(name, None)
        self._rows[name] = _failed_row(
            name,
            entry,
            local_port=self._desired.get(name) or None,
            code=code,
        )
        if had_binding:
            _emit(
                logging.INFO,
                "service.connect.mapping.closed",
                serviceName=name,
                code=code,
            )

    def _close_unauthorized(self, code: str) -> None:
        controller = self._controller_factory()
        if controller is None:
            controller = object()
        for name in tuple(self._desired):
            self._close_one(name, controller, code)

    def _access_invalid(self, name: str | None, code: str) -> None:
        self._emit_transition(
            "access:record" if name is None else f"access:{name}",
            logging.INFO,
            "service.connect.access.invalid",
            **({"serviceName": name} if name is not None else {}),
            code=code,
        )

    def _restore_failed(self, name: str, code: str) -> None:
        if name not in self._restore_pending:
            return
        self._restore_pending.discard(name)
        _emit(
            logging.WARNING,
            "service.connect.restore.failed",
            serviceName=name,
            code=code,
        )

    def _persistence_complete(
        self, event: EffectCompleted[DesiredStateIoCompleted]
    ) -> AdmissionResult:
        token = (event.operation_id, event.generation)
        with self._persistence_lock:
            waiter = self._persist_waiters.get(token)
            if waiter is None or event.result is None:
                return AdmissionResult.ACCEPTED
            waiter.completion = event.result
            waiter.ready.set()
            detached = waiter.detached
        if detached:
            with self._lock:
                self._reload_desired = True
                if waiter.completion.error_code is None:
                    self._version += 1
            self._persistence.acknowledge(waiter.request)
            with self._persistence_lock:
                self._persist_waiters.pop(token, None)
        return AdmissionResult.ACCEPTED

    def _persist(
        self,
        operation: DesiredStateOperation,
        args: tuple[object, ...],
        *,
        context: tuple[object, ...],
    ) -> DesiredState:
        with self._persist_call_lock:
            return self._persist_serialized(operation, args, context=context)

    def _persist_serialized(
        self,
        operation: DesiredStateOperation,
        args: tuple[object, ...],
        *,
        context: tuple[object, ...],
    ) -> DesiredState:
        operation_id = f"service-{uuid4().hex}"
        request = DesiredStateIoRequest(
            operation_id,
            self._generation,
            self._version,
            operation,
            args=args,
            context=context,
        )
        token = (operation_id, self._generation)
        waiter = _PersistenceWaiter(threading.Event(), request)
        with self._persistence_lock:
            self._persist_waiters[token] = waiter
        admission = self._persistence.submit(request)
        if admission is not AdmissionResult.ACCEPTED:
            with self._persistence_lock:
                self._persist_waiters.pop(token, None)
            raise ServiceConnectError(
                ipc_errors.SERVICE_UNAVAILABLE,
                "service state persistence is unavailable",
            )
        if not waiter.ready.wait(SERVICE_CONTROL_TIMEOUT_SECONDS):
            with self._persistence_lock:
                if waiter.completion is None:
                    waiter.detached = True
                    timed_out = True
                else:
                    timed_out = False
            if timed_out:
                raise ServiceConnectError(
                    ipc_errors.SERVICE_UNAVAILABLE,
                    "service state persistence remains unsettled",
                )
        completion = waiter.completion
        with self._persistence_lock:
            self._persist_waiters.pop(token, None)
        self._persistence.acknowledge(request)
        if completion is None or completion.error_code is not None:
            raise ServiceConnectError(
                ipc_errors.SERVICE_UNAVAILABLE,
                "service state persistence failed",
            )
        self._version += 1
        return completion.snapshot

    def _refresh_desired_if_dirty(self) -> None:
        if not self._reload_desired:
            return
        snapshot = self._state.load()
        if not isinstance(snapshot, DesiredState):
            raise TypeError("service state port must load DesiredState")
        previous = set(self._desired)
        desired = {
            item.name: item.local_port for item in snapshot.service_connections
        }
        current = set(desired)
        self._teardowns.update(previous - current)
        self._restore_pending.intersection_update(current)
        self._restore_pending.update(current - previous)
        self._desired = desired
        self._cached_registry = snapshot.service_registry
        self._reload_desired = False

    def _emit_transition(
        self, slot: str, level: int, event: str, **fields: object
    ) -> None:
        fingerprint = (event, *sorted(fields.items()))
        with self._lock:
            if self._diagnostic_states.get(slot) == fingerprint:
                return
            self._diagnostic_states[slot] = fingerprint
        _emit(level, event, **fields)

    def _clear_transition(self, slot: str) -> None:
        with self._lock:
            self._diagnostic_states.pop(slot, None)
