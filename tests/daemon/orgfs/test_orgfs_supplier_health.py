# Decisions:
# - Package S (2026-11-25): the journal mesh tests of this file were deleted
#   with ``OrgFsMesh``.  Their protocol 3 counterparts are listed in the T2
#   report §6.4; the cross-space hold aggregation, halving and the
#   ``spaceId`` on hold events have no one-to-one counterpart on the new
#   sync (T2 report §9).
# - The must-fire on the composition stays: every per-space sync the runtime
#   opens shares the runtime's one ``SupplierSyncHealth``, so a supplier held
#   in one space is held in all of them.
from __future__ import annotations

import inspect

from hyprial.daemon.impl.orgfs.composition.state.sync import StateLifecycle
from hyprial.daemon.impl.orgfs.runtime import OrgFsRuntime


def test_lifecycle_injects_the_daemon_wide_health() -> None:
    """Must-fire: removing the composition argument would restore per-space state."""

    source = inspect.getsource(StateLifecycle._new_sync)
    assert "self._supplier_sync_health" in source
    runtime_source = inspect.getsource(OrgFsRuntime.__init__)
    assert "self._supplier_sync_health = SupplierSyncHealth()" in runtime_source
