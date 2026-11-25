"""Stable declaration digests and phase/caller checks for the v2 GUI gate.

The projection and canonical bytes mirror the HTTP host declaration digest
contract (``request_http/catalog.py``) without importing the shell domain:
name-sorted exact DECLARATION_FIELDS entries, schemaVersion 1, compact
sort-keyed UTF-8 JSON, and a ``sha256:`` prefix.  Live availability fields and
declaration extensions outside DECLARATION_FIELDS do not change the digest.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from hashlib import sha256
import json
from typing import Any

DECLARATION_FIELDS = (
    "name",
    "owner",
    "httpExposed",
    "httpBinding",
    "mode",
    "mutates",
    "retry",
    "confirmation",
    "audience",
    "paramsSchema",
    "resultSchema",
    "errors",
    "ipcParamValidation",
)


def canonical_digest(document: object) -> str:
    """Return the HTTP-compatible canonical JSON digest."""
    payload = json.dumps(
        document,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return "sha256:" + sha256(payload).hexdigest()


def declaration_document(
    entries: Iterable[Mapping[str, Any]],
) -> dict[str, Any]:
    """Project entries onto the exact HTTP declaration fields."""
    ordered = sorted(entries, key=lambda entry: str(entry["name"]))
    return {
        "schemaVersion": 1,
        "methods": [
            {field: entry.get(field) for field in DECLARATION_FIELDS}
            for entry in ordered
        ],
    }


def declaration_digest() -> str:
    """Return the digest of the frozen production method declarations."""
    from hyprial.daemon.impl.request_catalog import daemon_catalog

    catalog = daemon_catalog()
    return canonical_digest(declaration_document(catalog["methods"]))


def catalog_result_digest(result: object) -> str | None:
    """Digest the declaration projection of one ``daemon.catalog`` result."""
    if not isinstance(result, Mapping):
        return None
    if type(result.get("schemaVersion")) is not int or result["schemaVersion"] != 1:
        return None
    methods = result.get("methods")
    if not isinstance(methods, list):
        return None
    if not all(isinstance(entry, Mapping) for entry in methods):
        return None
    if not all(isinstance(entry.get("name"), str) for entry in methods):
        return None
    try:
        document = declaration_document(methods)
        return canonical_digest(document)
    except (TypeError, ValueError):
        return None


def spec_phase_caller_allows(spec: object, phase: str, caller: str) -> bool:
    """Return whether a MethodSpec phase/caller declaration permits the pair."""
    constraint = getattr(spec, "phase_caller", None)
    if constraint is None:
        return True
    return bool(constraint.allows(phase, caller))


def phase_caller_allows(method: str, phase: str, caller: str) -> bool:
    """Apply the registered MethodSpec phase/caller constraint, if any."""
    from hyprial.daemon.impl.request_catalog import METHOD_REGISTRY

    binding = METHOD_REGISTRY.get(method)
    if binding is None:
        return True
    return spec_phase_caller_allows(binding.spec, phase, caller)
