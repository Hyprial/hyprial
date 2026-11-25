# Decision record (contract freeze r3 §4.1, §4.3; OrgFS facade api.py:248-276)
# - Interim owner-scope adapter over the OrgFS facade, used only through the
#   OwnerStore port; `fs` is duck-typed so LocalOrgFs and OrgFsRuntime.facade both fit.
# - Conditional writes use create_only / expect_version only, and every write must name
#   exactly one of them: a blind write (neither) or both is refused with InvalidSegment
#   before the backend is touched, so no caller can overwrite a signed record without
#   compare-and-set. base_version is never passed: it triggers a three-way text merge
#   that is unsafe for signed JSON.
# - Durable facades may surface the store's StoreError untranslated, so backend errors
#   are mapped by code: unknown-doc is absence, stale-write is StaleWrite,
#   ambiguous-path (concurrent creates in a non-deterministic-id space) is
#   PROVIDER_STORE_CONFLICT, anything else (content-pending, unknown-space,
#   membership, too-large, I/O) is PROVIDER_STORE_UNAVAILABLE and never "absent".
# - Any other exception from the backend (ValueError, TypeError, ...) is also
#   PROVIDER_STORE_UNAVAILABLE: the port contract promises only those outcomes. The
#   adapter's own StaleWrite, InvalidSegment and ProviderKeys.Error pass untouched.
# - Mapped errors are raised from None: backend details never reach callers.
"""OwnerStore adapter over an OrgFS facade and one space id."""
from __future__ import annotations

from typing import Any

from hyprial.identity import ProviderKeys
from hyprial.kernel import ipc_errors
from .port import InvalidSegment, StaleWrite, check_store_path

_LOCAL_ERRORS = (StaleWrite, ProviderKeys.Error)
_ABSENT = "unknown-doc"
_STALE = "stale-write"
_AMBIGUOUS = "ambiguous-path"


def _code(error: BaseException) -> str | None:
    code = getattr(error, "code", None)
    return code if isinstance(code, str) else None


def _refusal(error: BaseException) -> ProviderKeys.Error:
    if _code(error) == _AMBIGUOUS:
        return ProviderKeys.Error(ipc_errors.PROVIDER_STORE_CONFLICT)
    return ProviderKeys.Error(ipc_errors.PROVIDER_STORE_UNAVAILABLE)


def _already_exists(error: BaseException) -> bool:
    return _code(error) == "invalid-argument" and "already exists" in str(error)


class OrgFsOwnerStore:
    """OwnerStore over `fs` (OrgFs-like) in space `space_id`."""

    def __init__(self, fs: Any, space_id: str) -> None:
        self._fs = fs
        self._space_id = space_id

    def __repr__(self) -> str:
        return f"OrgFsOwnerStore(space_id={self._space_id!r})"

    def read(self, path: str) -> tuple[str, str] | None:
        """Return (text, version), None when absent; never confuses pending with absent."""
        check_store_path(path)
        try:
            text, version = self._fs.read_text(self._space_id, path)
        except _LOCAL_ERRORS:
            raise
        except Exception as error:
            if _code(error) == _ABSENT:
                return None
            raise _refusal(error) from None
        return text, version

    def write(self, path: str, text: str, *, expect_version: str | None, create_only: bool) -> str:
        """Create parents, then write with exactly one of create_only or expect_version."""
        check_store_path(path)
        if bool(create_only) == (expect_version is not None):
            raise InvalidSegment()
        try:
            self._ensure_parents(path)
            info = self._fs.write_text(
                self._space_id, path, text, expect_version=expect_version, create_only=create_only,
            )
            return info.version
        except _LOCAL_ERRORS:
            raise
        except Exception as error:
            if _code(error) == _STALE:
                raise StaleWrite() from None
            raise _refusal(error) from None

    def remove(self, path: str) -> None:
        """Soft-delete the node; an absent node is already removed."""
        check_store_path(path)
        try:
            self._fs.remove(self._space_id, path)
        except _LOCAL_ERRORS:
            raise
        except Exception as error:
            if _code(error) != _ABSENT:
                raise _refusal(error) from None

    def listdir(self, path: str) -> tuple[str, ...]:
        """Sorted unique live child names; an absent directory has none."""
        check_store_path(path)
        try:
            entries = self._fs.listdir(self._space_id, path)
            return tuple(sorted({entry.name for entry in entries}))
        except _LOCAL_ERRORS:
            raise
        except Exception as error:
            if _code(error) == _ABSENT:
                return ()
            raise _refusal(error) from None

    def _ensure_parents(self, path: str) -> None:
        parts = path.split("/")[:-1]
        for index in range(1, len(parts) + 1):
            try:
                self._fs.mkdir(self._space_id, "/".join(parts[:index]))
            except Exception as error:
                if not _already_exists(error):
                    raise
