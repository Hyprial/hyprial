"""The cell actors: one keyed interpreter per resolved cell (I5-1, plan §2).

The public face of ``cells.actor``: the registry, the published snapshot read
model and the M′ dispatch stamp/envelope types the composition builder mints.
The module is import-light by design — it pulls the cell model, the codec and
the pure structure reader, never OrgFS or any delivery domain.  Since I5-2
parts B/C the composition delivery builder and the harness execution gate
import these types too; that consumer set is closed by
``tests/tooling/architecture/delivery``.
"""

from hyprial.daemon.impl.cells.actor.contracts import (
    DISPATCH_KINDS,
    DISPATCH_STEER,
    DISPATCH_TURN,
    REASON_NO_ENTRY,
    REASON_REVISION_CONFLICT,
    WORK_SOURCES,
    WORK_SOURCE_HEADLESS,
    WORK_SOURCE_INTERACTIVE,
    CellActorAccess,
    CellActorInput,
    CellActorInputPort,
    CellActorState,
    CellActorWakeup,
    CellCommandExtension,
    CellDispatchEnvelope,
    CellDispatchStamp,
    CellMemberPhase,
)
from hyprial.daemon.impl.cells.actor.registry import CellActorRegistry
from hyprial.daemon.impl.cells.actor.snapshot import (
    CellActiveWork,
    CellActorMemberSnapshot,
    CellActorHistoryStore,
    CellActorSnapshot,
    CellActorSnapshotStore,
    HeadlessAdmission,
    headless_admission,
)

__all__ = [
    "CellActiveWork",
    "CellActorAccess",
    "CellActorHistoryStore",
    "CellActorInput",
    "CellActorInputPort",
    "CellActorMemberSnapshot",
    "CellActorRegistry",
    "CellActorSnapshot",
    "CellActorSnapshotStore",
    "CellActorState",
    "CellActorWakeup",
    "CellCommandExtension",
    "CellDispatchEnvelope",
    "CellDispatchStamp",
    "CellMemberPhase",
    "DISPATCH_KINDS",
    "DISPATCH_STEER",
    "DISPATCH_TURN",
    "HeadlessAdmission",
    "REASON_NO_ENTRY",
    "REASON_REVISION_CONFLICT",
    "WORK_SOURCES",
    "WORK_SOURCE_HEADLESS",
    "WORK_SOURCE_INTERACTIVE",
    "headless_admission",
]
