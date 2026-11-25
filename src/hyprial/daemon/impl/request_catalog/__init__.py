"""Declared daemon request methods and their real handlers.

A method enters this package with one :class:`MethodSpec` and one handler in
the same binding tuple.  The router never guesses a handler for a declared
method, and a declared method never falls back to the legacy dispatch chain.
"""

from __future__ import annotations

from .migration import (
    MIGRATED_FAMILIES,
    MIGRATED_METHODS,
    validate_migration_coverage,
)
from .daemon.catalog import CATALOG_BINDINGS, DAEMON_CATALOG_SPEC
from .preview import PreviewRecord, PreviewRegistry
from .registry import (
    METHOD_REGISTRY,
    BoundMethod,
    MethodRegistry,
    daemon_catalog,
    daemon_openapi,
)
from .org.membership.invites import (
    ORG_INVITES_ACCEPT_SPEC,
    ORG_INVITES_LIST_SPEC,
    org_invites_accept,
    org_invites_list,
)
from .session import (
    SESSION_BINDINGS,
    SESSION_TURN_REFRESH_SPEC,
    session_turn_refresh,
)
from .message import MESSAGE_STATUS_SPEC, PENDING_BATCH_BINDING, PENDING_BATCH_SPEC, message_status, pending_batch
from .org import ORG_BINDINGS, ORG_FETCH_SPEC, ORG_LIST_SPEC, org_fetch, org_list

__all__ = [
    "BoundMethod",
    "CATALOG_BINDINGS",
    "DAEMON_CATALOG_SPEC",
    "SESSION_BINDINGS",
    "SESSION_TURN_REFRESH_SPEC",
    "session_turn_refresh",
    "PENDING_BATCH_BINDING",
    "PENDING_BATCH_SPEC",
    "METHOD_REGISTRY",
    "ORG_INVITES_ACCEPT_SPEC",
    "ORG_INVITES_LIST_SPEC",
    "ORG_BINDINGS",
    "MIGRATED_FAMILIES",
    "MIGRATED_METHODS",
    "MethodRegistry",
    "ORG_FETCH_SPEC",
    "ORG_LIST_SPEC",
    "PreviewRecord",
    "daemon_catalog",
    "daemon_openapi",
    "PreviewRegistry",
    "MESSAGE_STATUS_SPEC",
    "message_status",
    "pending_batch",
    "org_fetch",
    "org_list",
    "org_invites_accept",
    "org_invites_list",
    "validate_migration_coverage",
]
