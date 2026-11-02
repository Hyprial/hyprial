"""Declared daemon request methods and their real handlers.

A method enters this package with one :class:`MethodSpec` and one handler in
the same binding tuple.  The router never guesses a handler for a declared
method, and a declared method never falls back to the legacy dispatch chain.
"""

from __future__ import annotations

from .daemon.catalog import CATALOG_BINDINGS, DAEMON_CATALOG_SPEC
from .preview import PreviewRecord, PreviewRegistry
from .registry import (
    METHOD_REGISTRY,
    MIGRATED_FAMILIES,
    BoundMethod,
    MethodRegistry,
    daemon_catalog,
    daemon_openapi,
)
from .session import (
    SESSION_BINDINGS,
    SESSION_TURN_REFRESH_SPEC,
    session_turn_refresh,
)
from .link import (
    LINK_ADD_SPEC,
    LINK_DROP_SPEC,
    LINK_LIST_SPEC,
    LINK_STATS_SPEC,
    link_add,
    link_drop,
    link_list,
    link_stats,
)

__all__ = [
    "BoundMethod",
    "CATALOG_BINDINGS",
    "DAEMON_CATALOG_SPEC",
    "SESSION_BINDINGS",
    "SESSION_TURN_REFRESH_SPEC",
    "session_turn_refresh",
    "LINK_ADD_SPEC",
    "LINK_DROP_SPEC",
    "LINK_LIST_SPEC",
    "LINK_STATS_SPEC",
    "METHOD_REGISTRY",
    "MIGRATED_FAMILIES",
    "MethodRegistry",
    "PreviewRecord",
    "daemon_catalog",
    "daemon_openapi",
    "PreviewRegistry",
    "link_add",
    "link_drop",
    "link_list",
    "link_stats",
]
