"""Document-revision-driven cell reconcile actor and coordinator (I6-2).

Decision notes (cell model slice I6-2, plan §F, §G.2, §H.2, §H.3):

- Each ``kind == "agent"`` cell gets one isolated reconcile state machine
  (optionally hosted on ``ActorHost`` under group ``"cell-reconcile"`` with
  ``STATE_AUTHORITY`` supervision).
- Each incarnation mints a fresh ``epoch`` token on spawn/restart and rebuilds
  from the last committed cell document, ensuring stale completions from a
  previous epoch, revision, member or attempt cannot overwrite newer state.
- Removed or reconfiguring members forbid new work admission immediately,
  drain in-flight work, emit ``cell.member.stop`` and then either remove the
  member or emit ``cell.member.ensure`` and verify the reported digest.
- Hosted mode never calls a blocking ``ActorCell.ask``: replies travel as a
  ``ReconcileRequest`` told to the actor and settled by the handler
  (``requests.py``), refused at once when the actor does not accept it and
  released when the coordinator closes.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Iterable
from typing import TYPE_CHECKING, cast

from hyprial.daemon.impl.cells.model import Cell
from hyprial.kernel import (
    STATE_AUTHORITY,
    ActorCell,
    ActorEvent,
    ActorEventKind,
    ActorSpec,
    AdmissionResult,
)

if TYPE_CHECKING:
    from hyprial.kernel import ActorHost

from .core import (
    CellReconcileSnapshot,
    ReconcileMemberSnapshot,
    _CellReconcileCore,
    _mint_epoch,
    _RejectedOverlay,
)
from .effects import (
    LocalMemberFactsPort,
    MemberEffectCompletion,
    MemberEffectPort,
)
from .requests import (
    BeginWorkCommand,
    CompleteEffectCommand,
    FinishWorkCommand,
    RebuildCommand,
    ReconcileDocCommand,
    ReconcileRequest,
    ReconcileRequests,
    settle_request,
)
from .planner import CellReconcileState

RECONCILE_ACTOR_GROUP = "cell-reconcile"
MAILBOX_CAPACITY = 128
STOP_TIMEOUT_S = 1.0
_LOGGER = logging.getLogger("hyprial.daemon.impl.cells.reconcile")


class _CellReconcileHandler:
    def __init__(
        self,
        cell_id: str,
        *,
        facts_port: LocalMemberFactsPort,
        effects_port: MemberEffectPort,
        get_last_cell: Callable[[str], Cell | None],
        publish: Callable[[str, CellReconcileSnapshot], None],
        remember_cell: Callable[[Cell], None],
        get_rejected: Callable[[str], _RejectedOverlay | None],
        set_rejected: Callable[[str, _RejectedOverlay | None], None],
    ) -> None:
        self._core = _CellReconcileCore(
            cell_id=cell_id,
            agent_id="",
            epoch=_mint_epoch(),
            facts_port=facts_port,
            effects_port=effects_port,
        )
        self._get_last_cell = get_last_cell
        self._publish = publish
        self._remember_cell = remember_cell
        self._get_rejected = get_rejected
        self._set_rejected = set_rejected

    def __call__(self, command: object) -> object:
        if not isinstance(command, ReconcileRequest):
            return self._dispatch(command)
        return settle_request(command, self._dispatch)

    def _dispatch(self, command: object) -> object:
        if isinstance(command, RebuildCommand):
            cell = self._get_last_cell(self._core.cell_id)
            if cell is not None:
                self._core.apply_document(cell)
                self._core.apply_rejected_overlay(self._get_rejected(self._core.cell_id))
                snap = self._core.snapshot()
                self._publish(self._core.cell_id, snap)
                return snap
            return self._core.snapshot()
        if isinstance(command, ReconcileDocCommand):
            snap = self._core.apply_document(command.cell, recheck=command.recheck)
            if snap is not None:
                if self._core.last_cell is command.cell:
                    self._remember_cell(command.cell)
                self._set_rejected(self._core.cell_id, self._core.last_rejected)
                self._publish(self._core.cell_id, snap)
            return snap
        if isinstance(command, CompleteEffectCommand):
            snap = self._core.complete_effect(command.completion)
            self._publish(self._core.cell_id, snap or self._core.snapshot())
            return snap
        if isinstance(command, BeginWorkCommand):
            accepted = self._core.begin_work(command.member, command.work_id)
            self._publish(self._core.cell_id, self._core.snapshot())
            return accepted
        if isinstance(command, FinishWorkCommand):
            finished = self._core.finish_work(command.member, command.work_id)
            self._publish(self._core.cell_id, self._core.snapshot())
            return finished
        return self._core.snapshot()


class CellReconcileCoordinator:
    """Process-wide coordinator of per-agent-cell member reconcile state."""

    def __init__(
        self,
        *,
        facts_port: LocalMemberFactsPort,
        effects_port: MemberEffectPort,
        actor_host: ActorHost | None = None,
        request_timeout_s: float = 5.0,
    ) -> None:
        self._facts_port = facts_port
        self._effects_port = effects_port
        self._host = actor_host
        self._requests = ReconcileRequests(request_timeout_s)
        self._lock = threading.RLock()
        self._cores: dict[str, _CellReconcileCore] = {}
        self._last_cells: dict[str, Cell] = {}
        self._rejected: dict[str, _RejectedOverlay] = {}
        self._snapshots: dict[str, CellReconcileSnapshot] = {}
        self._actors: dict[str, ActorCell] = {}
        self._closed = False

    def reconcile_cell(
        self, cell: Cell, *, recheck: Iterable[str] = ()
    ) -> CellReconcileSnapshot | None:
        """Apply one cell document revision; ignores non-agent cells.

        *recheck* names members to plan again even when the revision is one
        already applied (an operator relaunched them).
        """

        if cell.kind != "agent":
            return None
        if self._host is not None:
            actor = self._ensure_actor(cell.cell_id)
            if actor is None:
                return None
            ok, reply = self._request(actor, ReconcileDocCommand(cell, frozenset(recheck)))
            if not ok:
                return None
            return cast(CellReconcileSnapshot | None, reply)
        with self._lock:
            if self._closed:
                return None
            core = self._cores.get(cell.cell_id)
            if core is None:
                core = _CellReconcileCore(
                    cell_id=cell.cell_id,
                    agent_id=cell.binding.value if cell.binding.type == "subject" else "",
                    epoch=_mint_epoch(),
                    facts_port=self._facts_port,
                    effects_port=self._effects_port,
                )
                self._cores[cell.cell_id] = core
            snap = core.apply_document(cell, recheck=frozenset(recheck))
            if snap is not None:
                if core.last_cell is cell:
                    self._remember_cell(cell)
                self._set_rejected(cell.cell_id, core.last_rejected)
                self._snapshots[cell.cell_id] = snap
            return snap

    def on_index(self, cells: Iterable[Cell], *, recheck: Iterable[str] = ()) -> None:
        """Reconcile every ``kind == "agent"`` cell in the published index."""

        names = frozenset(recheck)
        for cell in cells:
            if cell.kind != "agent":
                continue
            if self._host is not None:
                actor = self._ensure_actor(cell.cell_id)
                if actor is not None:
                    actor.tell(ReconcileDocCommand(cell, names))
            else:
                self.reconcile_cell(cell, recheck=names)

    def complete_effect(
        self, completion: MemberEffectCompletion
    ) -> CellReconcileSnapshot | None:
        """Deliver one fenced effect completion to the cell's reconcile state."""

        cell_id = completion.fence.cell_id
        if self._host is not None:
            with self._lock:
                if self._closed:
                    return None
                actor = self._actors.get(cell_id)
            if actor is None:
                return None
            actor.tell(CompleteEffectCommand(completion))
            return self.snapshot(cell_id)
        with self._lock:
            if self._closed:
                return None
            core = self._cores.get(cell_id)
            if core is None:
                return None
            snap = core.complete_effect(completion)
            if snap is not None:
                self._snapshots[cell_id] = snap
            return snap

    def can_admit_work(self, cell_id: str, member: str) -> bool:
        """Whether *member* in *cell_id* is allowed to start new work."""

        with self._lock:
            snap = self._snapshots.get(cell_id)
            if snap is None:
                return False
            return snap.state is CellReconcileState.ACTIVE and member in snap.admitting_members

    def begin_work(self, cell_id: str, member: str, work_id: str) -> bool:
        """Claim an in-flight work slot on *member* if admission is open."""

        if self._host is not None:
            with self._lock:
                if self._closed:
                    return False
                actor = self._actors.get(cell_id)
            if actor is None:
                return False
            ok, reply = self._request(actor, BeginWorkCommand(member, work_id))
            return bool(ok and reply)
        with self._lock:
            if self._closed:
                return False
            core = self._cores.get(cell_id)
            if core is None:
                return False
            accepted = core.begin_work(member, work_id)
            self._snapshots[cell_id] = core.snapshot()
            return accepted

    def finish_work(self, cell_id: str, member: str, work_id: str) -> bool:
        """Settle in-flight *work_id* on *member*, resuming deferred stop/restart."""

        if self._host is not None:
            with self._lock:
                if self._closed:
                    return False
                actor = self._actors.get(cell_id)
            if actor is None:
                return False
            ok, reply = self._request(actor, FinishWorkCommand(member, work_id))
            return bool(ok and reply)
        with self._lock:
            if self._closed:
                return False
            core = self._cores.get(cell_id)
            if core is None:
                return False
            finished = core.finish_work(member, work_id)
            self._snapshots[cell_id] = core.snapshot()
            return finished

    def snapshot(self, cell_id: str) -> CellReconcileSnapshot | None:
        """Return the last published reconcile snapshot for *cell_id*."""

        with self._lock:
            return self._snapshots.get(cell_id)

    def restart_cell_actor(self, cell_id: str) -> CellReconcileSnapshot | None:
        """Restart a cell's reconcile actor under a fresh epoch and rebuild."""

        if self._host is None:
            with self._lock:
                last_cell = self._last_cells.get(cell_id)
                if last_cell is None:
                    return None
                core = _CellReconcileCore(
                    cell_id=cell_id,
                    agent_id=last_cell.binding.value if last_cell.binding.type == "subject" else "",
                    epoch=_mint_epoch(),
                    facts_port=self._facts_port,
                    effects_port=self._effects_port,
                )
                self._cores[cell_id] = core
                core.apply_document(last_cell)
                core.apply_rejected_overlay(self._get_rejected(cell_id))
                snap = core.snapshot()
                self._snapshots[cell_id] = snap
                return snap
        with self._lock:
            if self._closed:
                return None
            old_actor = self._actors.pop(cell_id, None)
        if old_actor is not None:
            old_actor.stop(STOP_TIMEOUT_S)
        actor = self._ensure_actor(cell_id)
        if actor is None:
            return None
        ok, reply = self._request(actor, RebuildCommand())
        if not ok:
            return self.snapshot(cell_id)
        return cast(CellReconcileSnapshot | None, reply)

    def close(self) -> None:
        """Stop all hosted reconcile actors; idempotent."""

        with self._lock:
            if self._closed:
                return
            self._closed = True
            actors = list(self._actors.values())
            self._actors.clear()
        self._requests.close()
        for actor in actors:
            actor.stop(STOP_TIMEOUT_S)

    def _request(self, actor: ActorCell, command: object) -> tuple[bool, object]:
        try:
            return True, self._requests.request(actor, command)
        except Exception as error:
            _LOGGER.warning(
                "cell reconcile actor request failed command=%s error_type=%s detail=%s",
                type(command).__name__,
                type(error).__name__,
                str(error)[:200],
            )
            return False, None

    def _ensure_actor(self, cell_id: str) -> ActorCell | None:
        assert self._host is not None
        with self._lock:
            if self._closed:
                return None
            existing = self._actors.get(cell_id)
            if existing is not None:
                return existing
            found = self._host.find(RECONCILE_ACTOR_GROUP, cell_id)
            if found is not None:
                self._actors[cell_id] = found
                return found
            spec = ActorSpec(
                name=f"{RECONCILE_ACTOR_GROUP}-{cell_id}",
                handler_factory=self._make_factory(cell_id),
                mailbox_capacity=MAILBOX_CAPACITY,
                supervision_profile=STATE_AUTHORITY,
                event_sink=self._make_event_sink(cell_id),
                group=RECONCILE_ACTOR_GROUP,
                key=cell_id,
            )
            actor = self._host.spawn(spec)
            self._actors[cell_id] = actor
            return actor

    def _make_factory(self, cell_id: str) -> Callable[[], _CellReconcileHandler]:
        def factory() -> _CellReconcileHandler:
            return _CellReconcileHandler(
                cell_id,
                facts_port=self._facts_port,
                effects_port=self._effects_port,
                get_last_cell=self._get_last_cell,
                publish=self._publish_snapshot,
                remember_cell=self._remember_cell,
                get_rejected=self._get_rejected,
                set_rejected=self._set_rejected,
            )

        return factory

    def _make_event_sink(self, cell_id: str) -> Callable[[ActorEvent], None]:
        def sink(event: ActorEvent) -> None:
            if event.kind is not ActorEventKind.CHILD_RESTARTED:
                return
            assert self._host is not None
            actor = self._host.find(RECONCILE_ACTOR_GROUP, cell_id)
            if actor is not None and actor.tell(RebuildCommand()) is AdmissionResult.ACCEPTED:
                return

        return sink

    def _get_last_cell(self, cell_id: str) -> Cell | None:
        with self._lock:
            return self._last_cells.get(cell_id)

    def _remember_cell(self, cell: Cell) -> None:
        with self._lock:
            existing = self._last_cells.get(cell.cell_id)
            if existing is not None and cell.revision <= existing.revision:
                return
            self._last_cells[cell.cell_id] = cell

    def _get_rejected(self, cell_id: str) -> _RejectedOverlay | None:
        with self._lock:
            return self._rejected.get(cell_id)

    def _set_rejected(self, cell_id: str, overlay: _RejectedOverlay | None) -> None:
        with self._lock:
            if overlay is None:
                self._rejected.pop(cell_id, None)
            else:
                self._rejected[cell_id] = overlay

    def _publish_snapshot(self, cell_id: str, snapshot: CellReconcileSnapshot) -> None:
        with self._lock:
            self._snapshots[cell_id] = snapshot


__all__ = [
    "CellReconcileCoordinator",
    "CellReconcileSnapshot",
    "RECONCILE_ACTOR_GROUP",
    "ReconcileMemberSnapshot",
]
