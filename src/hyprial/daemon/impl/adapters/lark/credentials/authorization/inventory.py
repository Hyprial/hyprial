"""Configuration and validated scope projections; no credential leaves here."""

from __future__ import annotations

from pathlib import Path
import time
from typing import Any

from hyprial.kernel import PersistentConfigStore
from hyprial.daemon.impl.adapters.lark.credentials.scopes import (
    configured_scope_client, diagnose_scopes, valid_scope_name,
)


def unknown(adapter: str, reason: str, app_id: str | None = None) -> dict[str, Any]:
    return {"adapter": adapter, "appId": app_id, "authorization": "unconfirmed",
            "capabilities": [], "checkedAt": time.time(), "reason": reason}


def configuration(home: Path, state_dir: Path, adapter: str) -> dict[str, Any]:
    if not isinstance(adapter, str) or not adapter.strip():
        raise ValueError("adapter must be a non-empty string")
    result = unknown(adapter, "NOT_CHECKED")
    try:
        gateways = PersistentConfigStore(home, state_dir).load().channels.gateways
        gateway = next((entry for entry in gateways if entry.name == adapter), None)
    except Exception:  # noqa: BLE001 - configuration errors may include file contents
        result["reason"] = "CONFIGURATION_UNAVAILABLE"
        return result
    if gateway is None:
        result.update(authorization="unconfigured", reason="ADAPTER_UNCONFIGURED")
    else:
        result["appId"] = gateway.app_id
    return result


def scope_inventory(home: Path, state_dir: Path, adapter: str) -> dict[str, Any]:
    result = configuration(home, state_dir, adapter)
    app_id = result["appId"]
    if app_id is None:
        return result
    try:
        resolved, client = configured_scope_client(home, state_dir, adapter)
        if resolved != app_id:
            return unknown(adapter, "CONFIGURATION_CHANGED", app_id)
        response = client.list_scopes()
        if not isinstance(response, dict) or type(response.get("code")) is not int or response["code"] != 0:
            return unknown(adapter, "SCOPE_QUERY_FAILED", app_id)
        data = response.get("data")
        scopes = data.get("scopes") if isinstance(data, dict) else None
        if not isinstance(scopes, list):
            return unknown(adapter, "INVALID_SCOPE_INVENTORY", app_id)
        seen = {}
        for row in scopes:
            if (not isinstance(row, dict) or not isinstance(row.get("scope_name"), str)
                    or not valid_scope_name(row["scope_name"])
                    or type(row.get("grant_status")) is not int
                    or row["grant_status"] not in (0, 1)
                    or row.get("scope_type") not in (None, "tenant", "user")):
                return unknown(adapter, "INVALID_SCOPE_INVENTORY", app_id)
            key = (row["scope_name"], row.get("scope_type") or "tenant")
            if key in seen and seen[key] != row["grant_status"]:
                return unknown(adapter, "INVALID_SCOPE_INVENTORY", app_id)
            seen[key] = row["grant_status"]
        diagnosis = diagnose_scopes(adapter=adapter, app_id=app_id, response=response)
        return {"adapter": adapter, "appId": app_id,
                "authorization": "authorized" if diagnosis["ok"] else "unauthorized",
                "capabilities": [row["id"] for row in diagnosis["capabilities"] if row["status"] == "granted"],
                "missingCapabilities": [row["id"] for row in diagnosis["capabilities"]
                                        if row["requirement"] == "required" and row["status"] != "granted"],
                "checkedAt": time.time()}
    except Exception:  # noqa: BLE001 - never expose platform errors or credentials
        return unknown(adapter, "SCOPE_QUERY_FAILED", app_id)
