# Decision record (phase D, package S, 2026-11-25):
# - Plan §3.4 step 8 / design §9 "dual host": the state runtime took over the
#   public name.  ``OrgFsRuntime`` is defined in ``composition/state``; this
#   module stays as the stable import path the daemon assembly and tests use.
#   The legacy journal runtime and the ``HYPRIAL_ORGFS_IMPL`` rehearsal switch
#   are deleted.
"""Public import path of the OrgFS runtime class."""

from __future__ import annotations

from hyprial.daemon.impl.orgfs.composition.state import OrgFsRuntime

__all__ = ["OrgFsRuntime"]
