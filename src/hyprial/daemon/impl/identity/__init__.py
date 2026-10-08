"""Daemon identity resolver: one authority for platform account bindings."""

from .ipc import handle_identity_ipc
from .resolver import IdentityResolver, IdentityResolverError

__all__ = ["IdentityResolver", "IdentityResolverError", "handle_identity_ipc"]
