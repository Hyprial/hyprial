# Decision notes (service grant S3, 2026-11-26):
# - IPC handler constructs ServiceGrantService per call from the daemon's
#   current OrgFS facade, home, committed owner, local device record, and
#   ExposureStore so relogin or exposure updates take effect immediately.
# - Parameter validation is performed by METHOD_REGISTRY schema enforcement
#   (ipc_param_validation="schema") before reaching this handler; domain
#   invariants are re-checked inside ServiceGrantService.
"""Daemon IPC wiring for ``service.grant``, ``service.revoke``, and ``service.grants``."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from hyprial.daemon.impl.org.network.directory import OrgFsDirectoryStore
from hyprial.identity import read_device_record
from hyprial.kernel import DaemonRequestError, ipc_errors

from .service import ServiceGrantService


def _local_device_id(app: Any, home: Path) -> str:
    try:
        record = read_device_record(home)
    except ValueError:
        record = None
    if record is not None and record.device_id:
        return record.device_id
    node_id = getattr(app, "node_id", None)
    if isinstance(node_id, str) and node_id:
        return node_id
    raise DaemonRequestError(
        ipc_errors.SERVICE_DEVICE_UNAVAILABLE,
        "local device record is not available",
    )


def _exposed_ports(app: Any) -> frozenset[int]:
    exposure_store = getattr(app, "_exposure_store", None)
    if exposure_store is None:
        return frozenset()
    return frozenset(
        item["port"]
        for item in exposure_store.list()
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
    device_id = _local_device_id(app, home)
    log_fn = getattr(app, "_log", None)
    logger = (
        (lambda level, component, event, **fields: log_fn(level, component, event, **fields))
        if callable(log_fn)
        else None
    )
    store = OrgFsDirectoryStore(fs, logger=logger)
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
