# Decision record (phase D, package S, 2026-11-25):
# - Plan §3.4 step 8 / design §9 "dual host": the state host took over the
#   public name.  ``LocalOrgFs`` is defined by the commit coordinator package;
#   this module stays as the stable import path the daemon and tests use, so
#   the switch did not rewrite every importer.  The legacy journal host and
#   the ``HYPRIAL_ORGFS_IMPL`` rehearsal switch are deleted.
"""Public import path of the OrgFS facade class."""

from __future__ import annotations

from hyprial.daemon.impl.orgfs.document.coordinator import LocalOrgFs

__all__ = ["LocalOrgFs"]
