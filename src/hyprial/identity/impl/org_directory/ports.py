"""Storage and authorization ports for the org directory (§4.1).

identity defines the semantics; daemon owns the storage (OrgFS) and the
policy engine (Casbin).  Both arrive as injected implementations at the
composition root, which is why this package never imports daemon code —
the layering (kernel → identity → daemon → biz → shell) stays intact.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from hyprial.identity.impl.org_directory.models import DirectoryDevice

__all__ = ["AllowAllPolicy", "DirectoryStore", "OrgPolicy"]


@runtime_checkable
class DirectoryStore(Protocol):
    """Directory persistence, implemented by the daemon over OrgFS.

    Documents live under ``directory/devices/<owner>/<deviceId>.json`` in the
    ``group-NAME`` directory space (the paths in :mod:`...models` are the contract);
    ``put_device`` is an upsert keyed by ``device_id``.
    """

    def list_devices(self, org: str) -> list[DirectoryDevice]:
        """Every device document in ``org``'s directory."""
        ...

    def put_device(self, org: str, device: DirectoryDevice) -> None:
        """Write one device document (upsert by ``device_id``)."""
        ...

    def orgs(self) -> list[str]:
        """The orgs this node belongs to."""
        ...


@runtime_checkable
class OrgPolicy(Protocol):
    """Authorization port — the Casbin wiring point (M0 §4.2)."""

    def allows(self, *, org: str, subject: str, action: str, resource: str) -> bool:
        """May ``subject`` take ``action`` on ``resource`` inside ``org``?"""
        ...


class AllowAllPolicy:
    """The round-3 stand-in while policy documents do not exist yet."""

    # LAX(tailnet-cutover): every request is allowed; the proper
    # implementation is local pycasbin enforcement of the directory's
    # signed policy documents with versions and lease terms (M0 §4.2).
    def allows(self, *, org: str, subject: str, action: str, resource: str) -> bool:
        return True
