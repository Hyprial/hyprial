"""Routine dispatch through PAC graphs (U3): one task, one graph.

Retirement of the old dispatcher (Allen 2026-09-17: 「我们需要退役workflow
v1，未来全部走pac v2」) moves ``routine`` off ``workflow.start``.  The mapping
this module implements is the one in
``notes/pac/u3-routine-to-pac-mapping-2026-09-17.md``:

* one source occurrence of one task is one graph, identified durably by
  ``operation_key = routine:<routine>:<task uuid>:<slot ms>``.  Replaying the
  same occurrence reuses its graph, while a later occurrence may drive a task
  whose previous graph completed.  Scheduled task UUIDs already contain their
  slot and retain the unsuffixed key;
* node ``work`` (kind=task) is owned by the routed target, and completion is
  that owner setting its flag -- never a text match in a reply (ruling 1 of
  2026-09-13);
* node ``deadline`` (kind=clock, ``guarded_by_node_id="work"``) carries a
  FIXED deadline and is owned by the escalation recipient (rulings B and D:
  no activity extension, no retry, let it crash and report).  The resident
  clock tick (PR #488) is what makes it fire for a graph with no actor;
* PAC notifications carry a reference, not a body, and activation does not
  dispatch a root task, so the task text itself is delivered by this module
  through the daemon's message plane, with the exact command that completes
  the node.

Allen, 2026-09-17, on what happens at the deadline: 「超时上报后，这个任务直接
标记为失败，由被上报的人/agent决定是否重排」.  So ``status`` reports ``escalated``
once and the routine does not dispatch that task again; a new attempt is a
human/coordinator decision.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

from hyprial.pac.errors import PAC_OPERATION_KEY_CONFLICT, PacError
from hyprial.pac.graph import activate_graph, add_node, close_graph, create_graph
from hyprial.pac.store import PacGraphStore, default_database_path

#: Node that the routed target completes.
WORK_NODE = "work"
#: Clock node whose overdue notification is the escalation.
DEADLINE_NODE = "deadline"

STATE_RUNNING = "running"
STATE_DONE = "done"
STATE_ESCALATED = "escalated"


class RoutineTaskDelivery(Protocol):
    """Sends one task message; returns False when the message plane refused."""

    def __call__(
        self,
        *,
        target: str,
        conversation_id: str,
        text: str,
        sender: str,
    ) -> bool: ...


def operation_key(
    routine_name: str, task_uuid: str, occurrence_slot_ms: int | None = None
) -> str:
    base = f"routine:{routine_name}:{task_uuid}"
    return base if occurrence_slot_ms is None else f"{base}:{occurrence_slot_ms}"


def task_message(
    *, task_text: str, graph_id: str, deadline_ms: int, reason_ref: str
) -> str:
    """The dispatched text: the task, then how to complete it.

    PAC never reads this body: the node is completed by its owner running the
    command below, which is why the command has to travel with the task.
    """

    return (
        f"{task_text}\n\n"
        f"完成后执行:hyprial workflow complete {graph_id} {WORK_NODE} "
        f"--reason-ref {reason_ref}\n"
        f"截止:{deadline_ms} (epoch ms,固定截止,不顺延;到点未完成会上报给负责人)"
    )


class PacRoutineDispatch:
    """The port ``RoutineFacade`` drives instead of ``WorkflowService``."""

    def __init__(
        self,
        *,
        state_dir: Path,
        deliver: RoutineTaskDelivery,
        clock_ms: Callable[[], int],
        resolve_principal: Callable[[str], str],
        workflow: Any | None = None,
    ) -> None:
        self._state_dir = Path(state_dir)
        self._database = default_database_path(self._state_dir)
        self._deliver = deliver
        self._clock_ms = clock_ms
        # A routed target may be a bare worker name, while a PAC owner is a
        # principal URI (schema 8, G1=A).  The daemon's resolve-or-reject
        # resolver is the only thing that can turn one into the other, so an
        # unresolvable target fails the dispatch loudly instead of writing a
        # short name a later flag could never authorize against.
        self._resolve_principal = resolve_principal
        self._workflow = workflow

    # -- dispatch --------------------------------------------------------

    def start_idempotent(
        self,
        *,
        routine_name: str,
        task_uuid: str,
        task_text: str,
        target: str,
        escalate_to: str,
        timeout_seconds: float,
        sender: str,
        role: str = "dispatch",
        occurrence_slot_ms: int | None = None,
        rearm_after_created_at_ms: int = 0,
    ) -> dict[str, object]:
        """Create (or re-find) this occurrence's graph and deliver its text.

        The graph is created first and the message second: a delivery that
        fails leaves a graph whose ``work`` node is unflagged, which the
        deadline reports -- the reverse order could deliver a task that no
        node records.  ``rearm_after_created_at_ms`` is captured from the PAC
        store by ``routine resume``; terminal escalations at or before that
        boundary no longer suppress a new occurrence.
        """

        key = operation_key(routine_name, task_uuid, occurrence_slot_ms)
        if self._workflow is not None:
            legacy = False
            if self._database.exists():
                probe = PacGraphStore(self._database, read_only=True)
                try:
                    legacy = probe.graph_by_operation_key(key) is not None
                    existing = (
                        None
                        if legacy
                        else probe.graph_by_operation_key(f"workflow:{key}")
                    )
                    if existing is not None:
                        meta = probe._db.execute(
                            "SELECT routine_name,task_key FROM workflow_graphs "
                            "WHERE graph_id=?",
                            (existing["graph_id"],),
                        ).fetchone()
                        if (
                            existing["created_by"] != sender
                            or meta is None
                            or (meta["routine_name"], meta["task_key"])
                            != (routine_name, task_uuid)
                        ):
                            raise PacError(
                                PAC_OPERATION_KEY_CONFLICT,
                                "source task key belongs to another publication",
                            )
                        # The source UUID, not a changing idle-age description,
                        # identifies accepted work. Retain its fixed deadline
                        # and result without readmission or another delivery.
                        result = self._workflow.status(run_id=existing["graph_id"])
                        return {"graphId": existing["graph_id"], "state": self._workflow_state(str(result["state"]))}
                    latest = self._latest_settled_task(
                        probe,
                        routine_name=routine_name,
                        task_uuid=task_uuid,
                        after_created_at_ms=rearm_after_created_at_ms,
                    )
                    if latest is not None and latest[1] == STATE_ESCALATED:
                        return {"graphId": latest[0], "state": latest[1]}
                finally:
                    probe.close()
            if not legacy:
                import yaml

                target = self._resolve_principal(target)
                escalation = self._resolve_principal(escalate_to)
                document = {
                    "version": 2,
                    "name": f"routine-{routine_name}",
                    "on_failure": "terminate",
                    "escalate_to": escalation,
                    "nodes": [
                        {
                            "id": WORK_NODE,
                            "owner": target,
                            "role": role,
                            "task": task_text,
                            "timeout": timeout_seconds,
                        }
                    ],
                }
                result = self._workflow.start(
                    yaml_text=yaml.safe_dump(document, allow_unicode=True),
                    sender=sender,
                    operation_key=key,
                    routine_name=routine_name,
                    task_key=task_uuid,
                )
                return {
                    "graphId": result["graphId"],
                    "state": self._workflow_state(str(result["state"])),
                }

        target = self._resolve_principal(target)
        escalate_to = self._resolve_principal(escalate_to)
        store = PacGraphStore(self._database)
        try:
            existing = store.graph_by_operation_key(key)
            if existing is None:
                latest = self._latest_settled_task(
                    store,
                    routine_name=routine_name,
                    task_uuid=task_uuid,
                    after_created_at_ms=rearm_after_created_at_ms,
                )
                if latest is not None and latest[1] == STATE_ESCALATED:
                    return {"graphId": latest[0], "state": latest[1]}
            try:
                head = create_graph(
                    store,
                    name=f"routine-{routine_name}",
                    created_by=sender,
                    operation_key=key,
                )
            except PacError as error:
                if error.code != PAC_OPERATION_KEY_CONFLICT:
                    raise
                raise RuntimeError(
                    f"operation key {key} is bound to another creator: {error}"
                ) from error
            graph_id = str(head["graphId"])
            graph = store.graph(graph_id)
            assert graph is not None
            settled = self._settled_state(store, graph, graph_id)
            if settled is not None:
                # This task already reached a terminal state under its durable
                # key.  Report it; do NOT deliver again.  Allen, 2026-09-17:
                # 「超时上报后，这个任务直接标记为失败，由被上报的人/agent决定
                # 是否重排」 -- so a source that still lists the task must not
                # make the routine dispatch it a second time.
                return {"graphId": graph_id, "state": settled}
            deadline_ms = self._deadline_ms(store, graph_id, timeout_seconds)
            # Build up to the shape, never from the assumption that the last
            # attempt got no further than `create_graph`.  A crash (or a
            # refused write) between create, the two nodes and activate leaves
            # the key bound to a half-built graph, and re-running the whole
            # sequence would fail on the node that already exists -- forever,
            # since the durable key always returns THAT graph.  Each step is
            # therefore conditional on its own absence.
            version = int(graph["version"])
            if store.node(graph_id, WORK_NODE) is None:
                version = int(
                    add_node(
                        store,
                        self._state_dir,
                        graph_id=graph_id,
                        node_id=WORK_NODE,
                        owner=target,
                        brief_ref=f"routine:{routine_name}#{task_uuid}",
                        expect_version=version,
                    )["version"]
                )
            if store.node(graph_id, DEADLINE_NODE) is None:
                version = int(
                    add_node(
                        store,
                        self._state_dir,
                        graph_id=graph_id,
                        node_id=DEADLINE_NODE,
                        owner=escalate_to,
                        brief_ref=f"routine:{routine_name}#{task_uuid}:deadline",
                        kind="clock",
                        deadline_ms=deadline_ms,
                        guarded_by_node_id=WORK_NODE,
                        expect_version=version,
                    )["version"]
                )
            # Unconditional on purpose: activation is monotonic inside PAC
            # (``activate_graph`` no-ops once ``activated_at`` is set), and
            # the one input it refuses -- a CLOSED graph -- is one this
            # dispatch must not paper over.  Guarding the call would turn
            # that refusal into silently delivering a task whose graph is
            # already over.
            activate_graph(store, graph_id, actor=sender)
        finally:
            store.close()

        delivered = self._deliver(
            target=target,
            conversation_id=(
                f"routine-{routine_name}-{task_uuid}"
                if occurrence_slot_ms is None
                else f"routine-{routine_name}-{task_uuid}-{occurrence_slot_ms}"
            ),
            text=task_message(
                task_text=task_text,
                graph_id=graph_id,
                deadline_ms=deadline_ms,
                reason_ref=f"routine:{routine_name}#{task_uuid}:result",
            ),
            sender=sender,
        )
        if not delivered:
            raise RuntimeError(
                f"routine task message for {graph_id} was refused by the "
                "message plane; the graph stays and its deadline reports it"
            )
        return {"graphId": graph_id, "state": STATE_RUNNING}

    def rearm_boundary(self, *, routine_name: str) -> int:
        """Return the newest PAC graph creation covered by ``routine resume``.

        The boundary is read from PAC itself, so terminal task state remains a
        PAC fact.  The routine store only remembers which already-existing PAC
        facts the explicit resume chose to re-arm.
        """

        if not self._database.exists():
            return 0
        store = PacGraphStore(self._database, read_only=True)
        try:
            prefix = f"routine:{routine_name}:"
            row = store._db.execute(
                "SELECT COALESCE(MAX(g.created_at), 0) "
                "FROM graphs g LEFT JOIN workflow_graphs w USING(graph_id) "
                "WHERE w.routine_name=? OR g.operation_key LIKE ? "
                "OR g.operation_key LIKE ?",
                (routine_name, f"{prefix}%", f"workflow:{prefix}%"),
            ).fetchone()
            return 0 if row is None else int(row[0])
        finally:
            store.close()

    def _latest_settled_task(
        self,
        store: PacGraphStore,
        *,
        routine_name: str,
        task_uuid: str,
        after_created_at_ms: int,
    ) -> tuple[str, str] | None:
        """Latest terminal PAC state for one routine/task after its re-arm.

        Workflow graphs use their typed ``routine_name``/``task_key`` columns.
        Pre-workflow routine graphs are recognized by the old unsuffixed key or
        a numeric occurrence suffix.  Old keys therefore remain readable for
        suppression but never collide with a new slot-keyed occurrence.
        """

        base = operation_key(routine_name, task_uuid)
        rows = store._db.execute(
            "SELECT g.*,w.state AS workflow_state,w.routine_name,w.task_key "
            "FROM graphs g LEFT JOIN workflow_graphs w USING(graph_id) "
            "WHERE (w.routine_name=? AND w.task_key=?) "
            "OR (w.graph_id IS NULL AND (g.operation_key=? "
            "OR substr(g.operation_key,1,?)=?)) "
            "ORDER BY g.created_at DESC,g.graph_id DESC",
            (
                routine_name,
                task_uuid,
                base,
                len(base) + 1,
                f"{base}:",
            ),
        ).fetchall()
        for row in rows:
            if int(row["created_at"]) <= after_created_at_ms:
                continue
            if row["routine_name"] == routine_name and row["task_key"] == task_uuid:
                state = self._workflow_state(str(row["workflow_state"]))
                if state != STATE_RUNNING:
                    return str(row["graph_id"]), state
                continue
            key = str(row["operation_key"] or "")
            if key != base:
                suffix = key[len(base) + 1 :]
                if not suffix.isdigit():
                    continue
            graph = dict(row)
            state = self._settled_state(store, graph, str(row["graph_id"]))
            if state is not None:
                return str(row["graph_id"]), state
        return None

    def _settled_state(
        self, store: PacGraphStore, graph: dict[str, Any], graph_id: str
    ) -> str | None:
        """``done``/``escalated`` for an already-terminal task, else None."""

        if graph["activated_at"] is None:
            return None
        work = store.node(graph_id, WORK_NODE)
        deadline = store.node(graph_id, DEADLINE_NODE)
        if work is None or deadline is None:
            return None
        if work.flag:
            return STATE_DONE
        # STRICTLY past, because that is the reactor's own rule: ``_plan_clocks``
        # skips a clock while ``now <= deadline_ms``.  Reading ``>=`` here would
        # mark the task failed one tick before anyone was told -- the routine
        # would report an escalation that PAC had not notified to anybody.
        if deadline.deadline_ms is not None and self._clock_ms() > deadline.deadline_ms:
            return STATE_ESCALATED
        if graph["closed_at"] is not None:
            # Closed without a flag and before its deadline: whoever closed it
            # decided this task is over, and reporting it as escalated keeps
            # "not completed" from being read as "completed".
            return STATE_ESCALATED
        return None

    # -- projection ------------------------------------------------------

    def status(self, *, graph_id: str) -> dict[str, object]:
        """``done`` once the owner flagged ``work``; ``escalated`` past the
        deadline while it is unflagged; ``running`` otherwise.

        Settlement has ONE definition (:meth:`_settled_state`), shared with
        the dispatch path: two readers of the same graph with two rules
        disagree on exactly the inputs that matter -- a graph closed early
        would read ``escalated`` to the dispatcher and ``running`` here.
        What ``status`` adds is loudness: a graph or a node that is gone is
        a routine-level failure, never a silent ``running``.
        """

        if self._is_workflow(graph_id):
            result = self._workflow.status(run_id=graph_id)
            return {
                "graphId": graph_id,
                "state": self._workflow_state(str(result["state"])),
            }

        store = PacGraphStore(self._database)
        try:
            graph = store.graph(graph_id)
            if graph is None:
                raise RuntimeError(f"routine graph {graph_id} is missing")
            if (
                store.node(graph_id, WORK_NODE) is None
                or store.node(graph_id, DEADLINE_NODE) is None
            ):
                raise RuntimeError(
                    f"routine graph {graph_id} lost its {WORK_NODE}/{DEADLINE_NODE} nodes"
                )
            state = self._settled_state(store, graph, graph_id) or STATE_RUNNING
        finally:
            store.close()
        return {"graphId": graph_id, "state": state}

    def close(self, *, graph_id: str, actor: str) -> None:
        """Close a settled graph as its creator; flags are never changed."""

        if self._is_workflow(graph_id):
            self._workflow.cancel(run_id=graph_id, actor=actor)
            return

        store = PacGraphStore(self._database)
        try:
            close_graph(store, graph_id, actor=actor)
        finally:
            store.close()

    @staticmethod
    def _workflow_state(state: str) -> str:
        return (
            STATE_DONE
            if state == "completed"
            else STATE_ESCALATED
            if state in {"failed", "cancelled"}
            else STATE_RUNNING
        )

    def _is_workflow(self, graph_id: str) -> bool:
        if self._workflow is None or not self._database.exists():
            return False
        store = PacGraphStore(self._database, read_only=True)
        try:
            return (
                store._db.execute(
                    "SELECT 1 FROM workflow_graphs WHERE graph_id=?", (graph_id,)
                ).fetchone()
                is not None
            )
        finally:
            store.close()

    # -- internals -------------------------------------------------------

    def _deadline_ms(
        self, store: PacGraphStore, graph_id: str, timeout_seconds: float
    ) -> int:
        """The existing clock's deadline on a replay, a fresh one otherwise.

        Re-deriving it from ``now`` on every replay would be the activity
        extension ruling D forbids.
        """

        existing = store.node(graph_id, DEADLINE_NODE)
        if existing is not None and existing.deadline_ms is not None:
            return int(existing.deadline_ms)
        return self._clock_ms() + int(timeout_seconds * 1000)


class PacDispatchPort(Protocol):
    """What ``RoutineFacade`` needs from PAC; implemented above."""

    def start_idempotent(
        self,
        *,
        routine_name: str,
        task_uuid: str,
        task_text: str,
        target: str,
        escalate_to: str,
        timeout_seconds: float,
        sender: str,
        occurrence_slot_ms: int | None = None,
        rearm_after_created_at_ms: int = 0,
    ) -> dict[str, object]: ...

    def rearm_boundary(self, *, routine_name: str) -> int: ...

    def status(self, *, graph_id: str) -> dict[str, object]: ...

    def close(self, *, graph_id: str, actor: str) -> None: ...


__all__ = [
    "DEADLINE_NODE",
    "PacDispatchPort",
    "PacRoutineDispatch",
    "RoutineTaskDelivery",
    "STATE_DONE",
    "STATE_ESCALATED",
    "STATE_RUNNING",
    "WORK_NODE",
    "operation_key",
    "task_message",
]
