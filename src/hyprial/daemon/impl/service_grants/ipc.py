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


def _invalid_device_diagnostics(app: Any) -> InvalidDeviceQuarantine:
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


def _build_service(app: Any) -> ServiceGrantService:
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
        invalid_devices=_invalid_device_diagnostics(app),
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


def handle_service_grants(
    app: Any, method: str, params: Mapping[str, Any]
) -> dict[str, Any]:
    """Dispatch ``service.grant``, ``service.revoke``, and ``service.grants``."""

    try:
        return _dispatch(app, method, params)
    except DaemonRequestError:
        raise
    except Exception as error:
        raise DaemonRequestError(
            ipc_errors.SERVICE_UNAVAILABLE,
            f"{method} failed ({type(error).__name__})",
        ) from None


def _dispatch(app: Any, method: str, params: Mapping[str, Any]) -> dict[str, Any]:
    service = _build_service(app)
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


__all__ = ["handle_service_grants"]
