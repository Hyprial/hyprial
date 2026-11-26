# Decision notes (service grant S3, 2026-11-26):
# - IPC handler constructs ServiceGrantService per call from the daemon's
#   current OrgFS facade, home, committed owner, local device record, and
#   ExposureStore so relogin or exposure updates take effect immediately.
# - Parameter validation is performed by METHOD_REGISTRY schema enforcement
#   (ipc_param_validation="schema") before reaching this handler; domain
#   invariants are re-checked inside ServiceGrantService.
# - S3 fix (2026-11-26, critic N1, N2): every failure leaves the handler as a
#   DaemonRequestError whose text never names a local path; an unexpected
#   exception is reported by type name only. The service device id comes only
#   from the local device record; there is no node_id fallback.
# - S4-py (2026-11-26): a grant or a revoke that succeeded is a trigger. The
#   handler marks the admission sets stale and offers the enforcement cadence
#   a tick, through the application's own method when it has one; it never
#   derives on the IPC thread and the reply never waits for the push. A
#   trigger that cannot be delivered is not an error: the maintenance tick
#   re-derives within its interval. The service builder and the quarantine
#   accessor are public because the admission wiring builds the same service
#   and reads the same directory diagnostics; there is one implementation.
"""Daemon IPC wiring for ``service.grant``, ``service.revoke``, and ``service.grants``."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from hyprial.daemon.impl.org.network.directory import (
    InvalidDeviceQuarantine,
    OrgFsDirectoryStore,
)
from hyprial.identity import read_device_record
from hyprial.kernel import DaemonRequestError, ipc_errors

from .service import ServiceGrantService


def quarantine_for(app: Any) -> InvalidDeviceQuarantine:
    """The application's device quarantine, created on first use."""

    quarantine = getattr(app, "_org_directory_quarantine", None)
    if quarantine is None:
        quarantine = InvalidDeviceQuarantine()
        app._org_directory_quarantine = quarantine
    return quarantine


def _local_device_id(home: Path) -> str:
    try:
        record = read_device_record(home)
    except (OSError, ValueError):
        record = None
    if record is None or not isinstance(record.device_id, str) or not record.device_id:
        raise DaemonRequestError(
            ipc_errors.SERVICE_DEVICE_UNAVAILABLE,
            "local device record is not available",
        )
    return record.device_id


def _exposed_ports(app: Any) -> frozenset[int]:
    exposure_store = getattr(app, "_exposure_store", None)
    if exposure_store is None:
        return frozenset()
    try:
        items = exposure_store.list()
    except Exception as error:
        raise DaemonRequestError(
            ipc_errors.SERVICE_UNAVAILABLE,
            f"exposure state is unavailable ({type(error).__name__})",
        ) from None
    return frozenset(
        item["port"]
        for item in items
        if isinstance(item, Mapping) and type(item.get("port")) is int
    )


def build_service(app: Any) -> ServiceGrantService:
    """The service grant service over the application's current facade and owner."""

    fs = app._require_orgfs_runtime().facade
    home = Path(app.hyprial_home)
    owner = getattr(app, "owner", None)
    if not isinstance(owner, str) or not owner:
        raise DaemonRequestError(
            ipc_errors.SERVICE_NOT_AUTHORIZED,
            "daemon owner identity is not configured",
        )
    device_id = _local_device_id(home)
    log_fn = getattr(app, "_log", None)
    logger = (
        (lambda level, component, event, **fields: log_fn(level, component, event, **fields))
        if callable(log_fn)
        else None
    )
    store = OrgFsDirectoryStore(
        fs,
        invalid_devices=quarantine_for(app),
        logger=logger,
    )
    return ServiceGrantService(
        store=store,
        home=home,
        owner=owner,
        local_device_id=device_id,
        exposed_ports=lambda: _exposed_ports(app),
        logger=logger,
    )


_TRIGGERS = frozenset({"service.grant", "service.revoke"})


def handle_service_grants(
    app: Any, method: str, params: Mapping[str, Any]
) -> dict[str, Any]:
    """Dispatch ``service.grant``, ``service.revoke``, and ``service.grants``.

    A grant or a revoke that succeeded has changed what the exposing device
    must admit, so it ends by asking the application to re-derive.
    """

    try:
        result = _dispatch(app, method, params)
    except DaemonRequestError:
        raise
    except Exception as error:
        raise DaemonRequestError(
            ipc_errors.SERVICE_UNAVAILABLE,
            f"{method} failed ({type(error).__name__})",
        ) from None
    if method in _TRIGGERS:
        _admission_changed(app, method)
    return result


def _admission_changed(app: Any, method: str) -> None:
    """Tell the application the admission inputs changed; never fails the write."""

    trigger = getattr(app, "_service_admission_changed", None)
    try:
        if callable(trigger):
            trigger()
    except Exception as error:  # noqa: BLE001 - the grant is already durable
        log_fn = getattr(app, "_log", None)
        if callable(log_fn):
            log_fn(
                "warn",
                "service",
                "service.grant.admission_trigger_failed",
                method=method,
                errorType=type(error).__name__,
            )


def _dispatch(app: Any, method: str, params: Mapping[str, Any]) -> dict[str, Any]:
    service = build_service(app)
    if method == "service.grant":
        return service.grant(
            org=params["org"],
            port=params["port"],
            device=params["device"],
            expires_at_ms=params.get("expiresAtMs"),
            name=params.get("name"),
            protocol=params.get("protocol"),
            note=params.get("note"),
            credential=params.get("credential"),
        )
    if method == "service.revoke":
        return service.revoke(
            org=params["org"],
            grant_id=params["grantId"],
        )
    if method == "service.grants":
        return service.grants(
            org=params.get("org"),
            port=params.get("port"),
            include_revoked=bool(params.get("includeRevoked", False)),
        )
    raise DaemonRequestError(
        ipc_errors.METHOD_NOT_FOUND, f"unknown daemon method {method}"
    )


__all__ = ["build_service", "handle_service_grants", "quarantine_for"]
