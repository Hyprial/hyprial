# Decision record (contract freeze r3 §2, §4.1, §4.2; OC4 J22 local storage)
# - Provider-key code reaches the owner scope only through OwnerSpaceResolver ->
#   OwnerStore. It never names or resolves a space itself, so adapters can
#   change without touching callers.
# - Production resolves to LocalSpaceResolver under <HYPRIAL_HOME>/state/
#   (when hyprial_home is unavailable, it falls back to UNAVAILABLE_RESOLVER).
# - Every path is built from validated segments (§2 patterns) and the store refuses
#   any path outside the two provider partitions; traversal never reaches OrgFS.
# - Versions are opaque strings owned by the adapter; a lost compare-and-set is the
#   local StaleWrite so callers can re-read, re-validate and retry (§4.3).
# - dekVersion is a JSON-safe integer 1..2**53-1 (§2 integers). The envelope-name
#   builder refuses anything else and the parser accepts at most 16 digits and the
#   same bound, so every name one side produces the other side accepts and vice versa.
"""Owner store port: protocols, space resolvers and §4.2 record paths."""
from __future__ import annotations

import re
import uuid
from typing import Any, Protocol, runtime_checkable

from hyprial.identity import ProviderKeys
from hyprial.kernel import ipc_errors

KEYS_ROOT = "directory/provider-keys"
GRANTS_ROOT = "directory/provider-grants"
ROOTS = (KEYS_ROOT, GRANTS_ROOT)

_KEY_ID = re.compile(r"pk\.[a-z2-7]{26}")
_SLOT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_ENVELOPE_NAME = re.compile(r"envelope-([1-9][0-9]{0,15})\.json")
MAX_DEK_VERSION = 2**53 - 1
_GRANT_SUFFIX = ".json"


class InvalidSegment(ProviderKeys.Error):
    """A path segment or store path outside the frozen §2/§4.2 grammar."""

    def __init__(self) -> None:
        super().__init__(ipc_errors.INVALID_ARGUMENT)


class StaleWrite(Exception):
    """A conditional write lost: the target exists (create_only) or changed (expect_version)."""

    def __init__(self) -> None:
        super().__init__("owner store write is stale")


@runtime_checkable
class OwnerStore(Protocol):
    """Text records in one owner scope, addressed by relative provider paths."""

    def read(self, path: str) -> tuple[str, str] | None:
        """Return (text, version), or None when absent."""
        ...

    def write(self, path: str, text: str, *, expect_version: str | None, create_only: bool) -> str:
        """Write with exactly one of create_only or expect_version; return the new version or raise StaleWrite."""
        ...

    def remove(self, path: str) -> None:
        """Remove the node; an absent node is already removed."""
        ...

    def listdir(self, path: str) -> tuple[str, ...]:
        """Child names of a directory; an absent directory has none."""
        ...


@runtime_checkable
class OwnerSpaceResolver(Protocol):
    """Map an owner (Casdoor sub) to the store of its owner scope."""

    def store_for(self, owner: str) -> OwnerStore:
        """Return the owner's store or raise PROVIDER_STORE_UNAVAILABLE."""
        ...


class _UnavailableResolver:
    """Production default until an owner-scope space exists (F-4)."""

    def store_for(self, owner: str) -> OwnerStore:
        raise ProviderKeys.Error(ipc_errors.PROVIDER_STORE_UNAVAILABLE)

    def __repr__(self) -> str:
        return "UNAVAILABLE_RESOLVER"


UNAVAILABLE_RESOLVER: OwnerSpaceResolver = _UnavailableResolver()


class FixedSpaceResolver:
    """Resolve every owner to one OrgFS space; for isolated tests and future adapters."""

    def __init__(self, fs: Any, space_id: str) -> None:
        self._fs = fs
        self._space_id = space_id

    def store_for(self, owner: str) -> OwnerStore:
        from .orgfs import OrgFsOwnerStore

        return OrgFsOwnerStore(self._fs, self._space_id)

    def __repr__(self) -> str:
        return f"FixedSpaceResolver(space_id={self._space_id!r})"


def _key_id(key_id: object) -> str:
    if not isinstance(key_id, str) or _KEY_ID.fullmatch(key_id) is None:
        raise InvalidSegment()
    return key_id


def _slot(value: object) -> str:
    if not isinstance(value, str) or _SLOT.fullmatch(value) is None:
        raise InvalidSegment()
    return value


def _agent(agent: object) -> str:
    if not isinstance(agent, str):
        raise InvalidSegment()
    try:
        canonical = str(uuid.UUID(agent))
    except ValueError:
        raise InvalidSegment() from None
    if canonical != agent:
        raise InvalidSegment()
    return agent


def key_dir(key_id: str) -> str:
    """Directory of one key: meta.json plus its envelopes."""
    return f"{KEYS_ROOT}/{_key_id(key_id)}"


def key_meta_path(key_id: str) -> str:
    """Signed provider-key metadata record of one key."""
    return f"{key_dir(key_id)}/meta.json"


def key_envelope_path(key_id: str, dek_version: int) -> str:
    """Create-only E1 envelope of one DEK version."""
    if type(dek_version) is not int or not 1 <= dek_version <= MAX_DEK_VERSION:
        raise InvalidSegment()
    return f"{key_dir(key_id)}/envelope-{dek_version}.json"


def grants_dir(agent: str) -> str:
    """Directory of every grant-state record of one agent incarnation."""
    return f"{GRANTS_ROOT}/{_agent(agent)}"


def grant_path(agent: str, grant_id: str) -> str:
    """Signed grant-state record of one (agent, grantId) slot."""
    return f"{grants_dir(agent)}/{_slot(grant_id)}{_GRANT_SUFFIX}"


def envelope_dek_version(name: str) -> int | None:
    """DEK version of an `envelope-<n>.json` child name, else None."""
    match = _ENVELOPE_NAME.fullmatch(name) if isinstance(name, str) else None
    version = int(match.group(1)) if match else None
    return version if version is not None and version <= MAX_DEK_VERSION else None


def grant_id_from_name(name: str) -> str | None:
    """grantId of a `<grantId>.json` child name, else None."""
    if not isinstance(name, str) or not name.endswith(_GRANT_SUFFIX):
        return None
    stem = name[: -len(_GRANT_SUFFIX)]
    return stem if _SLOT.fullmatch(stem) else None


def check_store_path(path: object) -> str:
    """Accept only clean relative paths inside the two provider partitions."""
    if not isinstance(path, str) or "\\" in path or "\x00" in path:
        raise InvalidSegment()
    segments = path.split("/")
    if any(segment in ("", ".", "..") for segment in segments):
        raise InvalidSegment()
    if not any(path == root or path.startswith(root + "/") for root in ROOTS):
        raise InvalidSegment()
    return path
