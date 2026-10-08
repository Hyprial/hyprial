"""Versioned daemon desired state with fail-loud rollback protection."""

from __future__ import annotations

from .documents import (  # noqa: F401
    DesiredState,
    InteractiveSession,
    PendingSessionAgentEffect,
    ROLLBACK_GUARD_PROVIDER,
    SCHEMA_VERSION,
    SUPPORTED_SCHEMA_VERSIONS,
    ZenohEndpoints,
    _HARNESSES,
    _LOGGER,
    _SHA256_HEX,
    _endpoint_list,
    _merge_session_effects,
    _optional_boolean,
    _optional_channel_build_version,
    _optional_positive_integer,
    _optional_sha256_digest,
    _optional_string,
    _record,
    _string,
    _string_map,
)
from .services import (  # noqa: F401
    ServiceConnection,
    ServiceRegistry,
    ServiceRegistryCache,
    ServiceRegistryEntry,
)
from .store import (  # noqa: F401
    DesiredStateStore,
    _STORES_BY_DB_PATH,
    _STORE_REGISTRY_LOCK,
    _apply_create_resource,
    _apply_delete_resource,
    _new_stored_receipt,
    _receipt_by_attempt,
    _reconcile_resource,
    _register_store,
    _resource_map,
    _stored_receipt,
    _with_external_resource,
)
