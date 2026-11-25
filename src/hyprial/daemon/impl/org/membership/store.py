"""OrgFS-backed membership records: space resolution and compare-and-set writes."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from hyprial.daemon.impl.orgfs.api import OrgFs
from hyprial.identity import org_space_name

__all__ = ["OrgMembershipError", "OrgMembershipStore"]

_Logger = Callable[..., None]


class OrgMembershipError(RuntimeError):
    """A typed membership failure; ``code`` is the IPC-facing code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class OrgMembershipStore:
    """Space resolution and versioned writes for membership records."""

    def __init__(
        self, fs: OrgFs, *, logger: _Logger | None = None
    ) -> None:
        self._fs = fs
        self._log = logger or (lambda *_args, **_fields: None)

    # -- space resolution ---------------------------------------------------

    def space_for(self, org: str) -> Any | None:
        """The one directory space named ``org``; ``None`` when absent."""

        name = org_space_name(org)
        matches = [space for space in self._fs.spaces() if space.name == name]
        if len(matches) > 1:
            raise OrgMembershipError(
                "ORG_MEMBERSHIP_CONFLICT",
                f"{len(matches)} spaces are named {name!r}; expected at most one",
            )
        return matches[0] if matches else None

