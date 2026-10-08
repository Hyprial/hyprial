"""Service-connect catalog, protected resolver, and mapping owner."""

from .manager import ServiceManager
from .models import (
    AccessSnapshot,
    CatalogEntry,
    CatalogTrust,
    ProtectedDevice,
    RegistrySnapshot,
    ServiceConnectError,
)
from .registry import BUILTIN_API, CatalogRegistry, parse_catalog_document
from .resolver import ProtectedResolver

__all__ = [
    "AccessSnapshot",
    "BUILTIN_API",
    "CatalogEntry",
    "CatalogRegistry",
    "CatalogTrust",
    "ProtectedDevice",
    "ProtectedResolver",
    "RegistrySnapshot",
    "ServiceConnectError",
    "ServiceManager",
    "parse_catalog_document",
]
