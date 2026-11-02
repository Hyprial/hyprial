"""Runtime readiness probes named by HTTP-exposed method declarations.

A declaration names a probe key; the daemon assembly owns the callable.  The
registry freezes with a failure when a declaration names a key that does not
exist here, so a typo can never become "available" by default.  Probes read
in-memory wiring only: they never call a business method or open a store.
"""

from __future__ import annotations

from collections.abc import Callable
from types import MappingProxyType
from typing import Any

ReadinessProbe = Callable[[Any], bool]


READINESS_PROBES: MappingProxyType[str, ReadinessProbe] = MappingProxyType(
    {
        "link-service": lambda app: getattr(app, "_link_service", None) is not None,
    }
)

__all__ = ["READINESS_PROBES", "ReadinessProbe"]
