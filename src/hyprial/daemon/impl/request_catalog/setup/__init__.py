"""Runtime method bindings delegate to the existing daemon authorities."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from functools import partial
from hyprial.identity import ProviderKeys, SecretResolutionError, SecretResolver
from hyprial.kernel import DaemonRequestError, ipc_errors
from ..agent import _legacy_agent_call
from .specs import SECRET_PROVIDER_WRITE_SPEC, SECRET_GRANT_SPEC, SECRET_WRITE_EXAMPLES as SECRET_WRITE_EXAMPLES, SECRET_PROVIDER_WRITE as SECRET_PROVIDER_WRITE, SECRET_GRANT as SECRET_GRANT
from .specs import SECRET_PROVIDER_LIST_SPEC, SECRET_REVOKE_SPEC


def secret_write(app: Any, params: Mapping[str, Any], *, method: str,
                 trusted_message_origin: str | None = None) -> dict[str, Any]:
    try:
        return _legacy_agent_call(app, method, params)
    except SecretResolutionError as error:
        raise DaemonRequestError(error.code, str(error)) from error


def secret_catalog(app: Any, params: Mapping[str, Any], *,
                   trusted_message_origin: str | None = None) -> dict[str, Any]:
    from hyprial.daemon.impl.provider_keys.ipc import preauthorize
    from hyprial.daemon.impl.provider_keys.legacy import _gate_closed

    preauthorize(SECRET_PROVIDER_LIST_SPEC.name, app, params,
                 trusted_message_origin=trusted_message_origin)
    try:
        if not _gate_closed(app):
            raise SecretResolutionError("authority", "catalog", "inventory")
        rows = SecretResolver(app.hyprial_home, app._agent_registry).list_model_services(
            include_connections=params.get("connectionVersion") == 1,
        )
    except (SecretResolutionError, ProviderKeys.Error):
        raise DaemonRequestError(
            ipc_errors.AGENT_SECRET_RESOLUTION_FAILED, "Saved secret catalog is unavailable.",
        ) from None
    entries = []
    for entry_id, fields, routes in rows:
        legacy = {name: row for name, row in routes.items() if "vendorId" in row}
        connections = {name: row for name, row in routes.items() if "connectionRef" in row}
        entries.append({"entryId": entry_id, "fieldNames": list(fields),
                        **({"routes": legacy} if legacy else {}),
                        **({"connections": connections} if connections else {})})
    return {"ok": True, "entries": entries}


SETUP_BINDINGS = tuple(
    (spec, partial(secret_write, method=spec.name))
    for spec in (SECRET_PROVIDER_WRITE_SPEC, SECRET_GRANT_SPEC, SECRET_REVOKE_SPEC)
) + ((SECRET_PROVIDER_LIST_SPEC, secret_catalog),)

SETUP_EXAMPLES = SECRET_WRITE_EXAMPLES
