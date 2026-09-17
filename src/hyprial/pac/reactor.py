"""The flag reactor: notifications as a pure function of ``flag_events``.

Concept §3, slice-1 spec ②③④ + the two headline acceptances:

- ``set(node)``: for every *forward* out-edge ``X -> Y``, if **all** of
  ``Y``'s forward predecessors are flagged, the owner of ``Y`` is
  notified 「轮到你」 carrying ``brief_ref`` + the triggering ``event_id``
  (§7-B: the notification carries a reference, never a body).  A *back*
  out-edge ``X -> B`` fires the same shape at ``B``'s owner as
  「轮到你(第 n 次)」 — no new node is created, the same node re-runs,
  and ``n`` is counted out of ``flag_events`` (number of ``set`` actions
  recorded for ``B`` so far, plus one; the ruling: this loop counting
  applies to back-edge re-entries — a node visited in two rounds is two
  nodes, e.g. pm-confirm-1 / pm-confirm-2).
- ``reset(node)``: for every out-edge that had already delivered a turn
  because of this node, the downstream owner gets 「已撤回」.  The
  downstream flag is never flipped automatically — that owner decides.
- Idempotency is a database constraint, not diligence: the
  ``(event_id, edge)`` primary key on ``notifications`` makes a replay of
  the same event against the same edge a no-op.

The reactor reads the graph structure + ``flag_events`` and writes
``flag_events``/``notifications``.  It reads nothing else — in particular
never an inbox body or reply text.  :mod:`tests.test_pac_tripwire` keeps
that a gate.

Delivery (the actual ``hyprial send``) is a port: :class:`NotificationSender`
is implemented by the CLI (daemon ``message.send``) and by a stub in
tests.  PAC records the notification row — including its exact text and
sending identity — before delivering; a delivery failure leaves the row
undelivered, and ``hyprial pac notify resend`` retries exactly those rows
byte-for-byte.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, replace
from time import time_ns
from typing import Any, Protocol
from uuid import uuid4

from .errors import (
    PAC_FLAG_ALREADY_SET,
    PAC_FLAG_NOT_OWNER,
    PAC_FLAG_NOT_SET,
    PAC_GRAPH_NOT_FOUND,
    PAC_GRAPH_NOT_ACTIVE,
    PAC_GRAPH_CLOSED,
    PAC_NOTIFY_DELIVERY_FAILED,
    PacError,
)
from .graph import BACK, FORWARD, canonical_edge
from .journal import activation_id, append_event
from .migrations import unrewritten_owners_note
from .store import PacGraphStore

TURN = "turn"
WITHDRAW = "withdraw"
OVERDUE = "overdue"


def now_ms() -> int:
    return time_ns() // 1_000_000


def turn_text(node_id: str, brief_ref: str, event_id: str, round_no: int) -> str:
    """§7-B shape: a reference and an event id — the round suffix is the
    loop count the concept demands in the text (④: 通知文案带「第 2 次」)."""

    if round_no <= 1:
        return f"轮到你:{node_id} {brief_ref} (event {event_id})"
    return f"轮到你(第 {round_no} 次):{node_id} {brief_ref} (event {event_id})"


def withdraw_text(node_id: str, brief_ref: str, event_id: str) -> str:
    return f"已撤回:{node_id} {brief_ref} (event {event_id})"


def overdue_text(node_id: str, brief_ref: str, event_id: str, deadline_ms: int) -> str:
    return f"逾期:{node_id} {brief_ref} (deadline {deadline_ms} ms, event {event_id})"


@dataclass(frozen=True, slots=True)
class PlannedNotification:
    """What one flag event causes, decided under the business write lock."""

    event_id: str
    edge: str
    kind: str
    recipient: str
    node_id: str
    round_no: int | None
    text: str
    sender: str
    plan: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class FlagEventOutcome:
    """One ``flag set/reset`` result: the durable event + what it caused."""

    event: dict[str, Any]
    planned: tuple[PlannedNotification, ...]
    delivered: tuple[PlannedNotification, ...]
    undelivered: tuple[PlannedNotification, ...]
    delivery_error: str | None = None


class NotificationSender(Protocol):
    """The outbound port: how a notification reaches its owner.

    The daemon implementation rides the public ``message.send`` seam (the
    same wire ``hyprial send`` uses).  It is outbound-only by construction;
    the reactor holds no inbound surface at all.
    """

    def send(
        self,
        *,
        recipient: str,
        text: str,
        sender: str,
        conversation_id: str,
        idempotency_key: str,
    ) -> str:
        """Deliver one notification; returns the wire ``message_id``."""

        ...


class NullSender:
    """Records notifications without delivering them (unit tests, dry runs)."""

    def __init__(self) -> None:
        self.sent: list[dict[str, str]] = []

    def send(
        self,
        *,
        recipient: str,
        text: str,
        sender: str,
        conversation_id: str,
        idempotency_key: str,
    ) -> str:
        self.sent.append(
            {
                "recipient": recipient,
                "text": text,
                "sender": sender,
                "conversationId": conversation_id,
                "idempotencyKey": idempotency_key,
            }
        )
        return f"null-{len(self.sent)}"


def planned_to_json(item: PlannedNotification) -> dict[str, Any]:
    return {
        "eventId": item.event_id,
        "edge": item.edge,
        "kind": item.kind,
        "recipient": item.recipient,
        "node": item.node_id,
        **({"round": item.round_no} if item.round_no else {}),
        "text": item.text,
        **({"plan": item.plan} if item.plan is not None else {}),
    }


class PacReactor:
    """Reacts to flag events on one store; pure decisions, durable writes."""

    def __init__(
        self,
        store: PacGraphStore,
        *,
        clock: Any = now_ms,
        sender: NotificationSender | None = None,
    ) -> None:
        self._store = store
        self._clock = clock
        self._sender = sender

    def close(self) -> None:
        """Close the underlying store; the reactor owns its store's lifetime."""

        self._store.close()

    # -- structure helpers ---------------------------------------------------

    def _require_graph(self, graph_id: str) -> dict[str, Any]:
        graph = self._store.graph(graph_id)
        if graph is None:
            raise PacError(PAC_GRAPH_NOT_FOUND, f"graph {graph_id!r} not found")
        return graph

    def _forward_predecessors(self, graph_id: str, node_id: str) -> list[str]:
        """The join condition's predecessor set: EVERY forward predecessor
        (concept §3.1's letter; dispatcher ruling 丙 2026-09-07 — a node
        visited in two rounds is two nodes, so the plain all-predecessors
        join is also the correct one for the Allen example:
        pm-confirm-1's only pred is pm-helper, pm-confirm-2's only pred
        is demo)."""

        return [
            edge.from_node
            for edge in self._store.edges(graph_id)
            if edge.kind == FORWARD and edge.to_node == node_id
        ]

    def _out_edges(self, graph_id: str, node_id: str, kind: str) -> list[tuple[str, str]]:
        return [
            (edge.from_node, edge.to_node)
            for edge in self._store.edges(graph_id)
            if edge.from_node == node_id and edge.kind == kind
        ]

    # -- decisions (pure given store state) ------------------------------------

    def _turn(
        self,
        event_id: str,
        source: str,
        target: Any,
        round_no: int,
        actor: str,
    ) -> PlannedNotification:
        return PlannedNotification(
            event_id=event_id,
            edge=canonical_edge(source, target.node_id),
            kind=TURN,
            recipient=target.owner,
            node_id=target.node_id,
            round_no=round_no,
            text=turn_text(target.node_id, target.brief_ref, event_id, round_no),
            sender=actor,
        )

    def _plan_set(
        self, graph_id: str, node_id: str, event_id: str, actor: str
    ) -> list[PlannedNotification]:
        planned: list[PlannedNotification] = []
        nodes = {node.node_id: node for node in self._store.nodes(graph_id)}

        # forward out-edges: the join condition is over ALL forward
        # predecessors of the target — a converge node only fires when
        # every branch has flagged (concept §3.1 letter, ruling 丙).
        for source, target_id in self._out_edges(graph_id, node_id, FORWARD):
            predecessors = self._forward_predecessors(graph_id, target_id)
            if not all(nodes[pred].flag for pred in predecessors):
                continue
            round_no = self._store.set_event_count(graph_id, target_id) + 1
            planned.append(self._turn(event_id, source, nodes[target_id], round_no, actor))

        # back out-edges: no join condition — the back node firing IS the
        # loop signal; the same target node re-runs (nothing is created).
        for source, target_id in self._out_edges(graph_id, node_id, BACK):
            round_no = self._store.set_event_count(graph_id, target_id) + 1
            planned.append(self._turn(event_id, source, nodes[target_id], round_no, actor))
        return planned

    def _plan_reset(
        self, graph_id: str, node_id: str, event_id: str, actor: str
    ) -> list[PlannedNotification]:
        planned: list[PlannedNotification] = []
        nodes = {node.node_id: node for node in self._store.nodes(graph_id)}
        existing_edges = {
            notification.edge
            for notification in self._store.notifications(graph_id)
            if notification.kind == TURN
        }
        for source, target_id in [
            *self._out_edges(graph_id, node_id, FORWARD),
            *self._out_edges(graph_id, node_id, BACK),
        ]:
            edge = canonical_edge(source, target_id)
            if edge not in existing_edges:
                # ③: only downstreams already turned by this node are
                # withdrawn; an edge that never notified is untouched.
                continue
            target = nodes[target_id]
            planned.append(
                PlannedNotification(
                    event_id=event_id,
                    edge=edge,
                    kind=WITHDRAW,
                    recipient=target.owner,
                    node_id=target_id,
                    round_no=None,
                    text=withdraw_text(target_id, target.brief_ref, event_id),
                    sender=actor,
                )
            )
        return planned

    # -- durable writes ---------------------------------------------------------

    def _insert_notifications(
        self, planned: list[PlannedNotification], at: int, *,
        db: sqlite3.Connection, graph_id: str, version: int,
    ) -> list[PlannedNotification]:
        """Persist decisions and their inputs in the caller's transaction.

        Never begin/commit here: flag validation, mutation, planning and
        outbox/journal insertion must share the same BEGIN IMMEDIATE.
        """
        if not db.in_transaction:
            raise RuntimeError("notification plans require a business transaction")
        inserted: list[PlannedNotification] = []
        nodes = {node.node_id: node for node in self._store.nodes(graph_id)}
        for item in planned:
            plan = {
                "graphId": graph_id, "version": version, "nodeId": item.node_id,
                "predecessors": [
                    {"nodeId": pred, "flag": nodes[pred].flag,
                     "flagSetAt": nodes[pred].flag_set_at,
                     "flagSetBy": nodes[pred].flag_set_by,
                     "flagReasonRef": nodes[pred].flag_reason_ref}
                    for pred in self._forward_predecessors(graph_id, item.node_id)
                ],
            }
            if item.kind == TURN:
                plan["activationId"] = activation_id(graph_id, item.node_id, item.round_no)
            item = replace(item, plan=plan)
            cursor = db.execute(
                "INSERT OR IGNORE INTO notifications "
                "(event_id, edge, kind, recipient, round_no, text, sender, at, plan_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (item.event_id, item.edge, item.kind, item.recipient, item.round_no,
                 item.text, item.sender, at, json.dumps(plan, sort_keys=True)),
            )
            if cursor.rowcount > 0:
                append_event(db, graph_id=graph_id, version=version,
                             type="notification_planned", at=at,
                             data={**planned_to_json(item), "sender": item.sender,
                                   "at": at, "messageId": None, "deliveredAt": None})
                inserted.append(item)
        return inserted

    def _mark_delivered(self, event_id: str, edge: str, message_id: str) -> None:
        db = self._store.write()
        try:
            row = db.execute(
                "SELECT * FROM notifications WHERE event_id = ? AND edge = ?",
                (event_id, edge),
            ).fetchone()
            if row is None:
                raise KeyError((event_id, edge))
            if row["message_id"] == message_id:
                db.commit()
                return
            at = int(self._clock())
            db.execute(
                "UPDATE notifications SET message_id = ?, delivered_at = ? "
                "WHERE event_id = ? AND edge = ?",
                (message_id, at, event_id, edge),
            )
            if row["plan_json"]:
                plan = json.loads(row["plan_json"])
                graph_id, version = plan["graphId"], plan["version"]
            else:
                # Migrated outbox rows retain their original keys and text.
                event = db.execute(
                    "SELECT graph_id, version FROM flag_events WHERE event_id = ?",
                    (event_id,),
                ).fetchone()
                if event is not None:
                    graph_id, version = event["graph_id"], event["version"]
                else:
                    matches = [g for g in db.execute("SELECT * FROM graphs")
                               if event_id.startswith(f"overdue:{g['graph_id']}:v")]
                    if len(matches) != 1:
                        raise ValueError("unowned legacy overdue notification")
                    graph_id = matches[0]["graph_id"]
                    version = int(event_id[len(f"overdue:{graph_id}:v"):].split(":", 1)[0])
            append_event(db, graph_id=graph_id, version=version,
                         type="delivery_changed", at=at,
                         data={"eventId": event_id, "edge": edge,
                               "messageId": message_id, "deliveredAt": at})
            db.commit()
        except BaseException:
            db.rollback()
            raise

    def _deliver(
        self, graph_id: str, planned: tuple[PlannedNotification, ...]
    ) -> tuple[tuple[PlannedNotification, ...], tuple[PlannedNotification, ...], str | None]:
        if self._sender is None or not planned:
            return (), planned if planned else (), None
        delivered: list[PlannedNotification] = []
        undelivered: list[PlannedNotification] = []
        error: str | None = None
        for index, item in enumerate(planned):
            if self._require_graph(graph_id)["closed_at"] is not None:
                undelivered.extend(planned[index:])
                error = "graph closed before delivery; remaining plans are not retried"
                break
            try:
                message_id = self._sender.send(
                    recipient=item.recipient,
                    text=item.text,
                    sender=item.sender,
                    conversation_id=f"pac-{graph_id}",
                    idempotency_key=f"pac-notify:{item.event_id}:{item.edge}",
                )
            except Exception as failure:  # noqa: BLE001 - delivery must not undo the durable fact
                undelivered.append(item)
                error = f"{type(failure).__name__}: {failure}"
                continue
            self._mark_delivered(item.event_id, item.edge, message_id)
            delivered.append(item)
        return tuple(delivered), tuple(undelivered), (error if undelivered else None)

    # -- flag writes --------------------------------------------------------------

    def set_flag(
        self,
        graph_id: str,
        node_id: str,
        *,
        actor: str,
        reason_ref: str | None = None,
    ) -> FlagEventOutcome:
        """Owner sets their node's flag; returns what the event caused."""

        return self._flag(graph_id, node_id, action="set", actor=actor, reason_ref=reason_ref)

    def reset_flag(
        self,
        graph_id: str,
        node_id: str,
        *,
        actor: str,
        reason_ref: str | None = None,
    ) -> FlagEventOutcome:
        """Owner un-sets their node's flag (「不通过/撤回」); downstream is
        notified 「已撤回」 but no downstream flag is flipped."""

        return self._flag(graph_id, node_id, action="reset", actor=actor, reason_ref=reason_ref)

    def _flag(
        self,
        graph_id: str,
        node_id: str,
        *,
        action: str,
        actor: str,
        reason_ref: str | None,
    ) -> FlagEventOutcome:
        db = self._store.write()
        try:
            # Re-read and validate AFTER acquiring the writer lock. A check
            # before BEGIN can approve two consecutive set/set operations.
            graph = self._require_graph(graph_id)
            if graph["closed_at"] is not None:
                raise PacError(PAC_GRAPH_CLOSED, "the graph is closed")
            if graph["activated_at"] is None:
                raise PacError(PAC_GRAPH_NOT_ACTIVE, "activate the completed graph before setting flags")
            node = self._store.node(graph_id, node_id)
            if node is None:
                raise PacError(
                    PAC_GRAPH_NOT_FOUND, f"node {node_id!r} not found on {graph_id!r}"
                )
            if node.kind == "actor":
                raise PacError(
                    PAC_FLAG_NOT_OWNER,
                    "an actor flag is the reactor's launch outcome, never owner intent; "
                    "use `hyprial pac actor stop` for an early stop",
                    {"nodeId": node_id, "owner": node.owner, "actor": actor},
                )
            if actor != node.owner:
                note = unrewritten_owners_note(self._store._db, graph_id)
                raise PacError(
                    PAC_FLAG_NOT_OWNER,
                    f"actor {actor!r} is not the owner of {node_id!r} (owner "
                    f"{node.owner!r}); an owner flips only their own node"
                    + (f"; {note}" if note else ""),
                    {"nodeId": node_id, "owner": node.owner, "actor": actor},
                )
            if action == "set" and node.flag:
                raise PacError(
                    PAC_FLAG_ALREADY_SET,
                    f"{node_id!r} is already set; reset it first — every set "
                    "event must be a real fact, or the loop-round count counted "
                    "out of flag_events would inflate",
                )
            if action == "reset" and not node.flag:
                raise PacError(
                    PAC_FLAG_NOT_SET,
                    f"{node_id!r} is not set; there is nothing to withdraw",
                )
            if action not in {"set", "reset"}:
                raise ValueError(f"unknown flag action: {action}")
            event_id = str(uuid4())
            at = int(self._clock())
            db.execute(
                "INSERT INTO flag_events "
                "(event_id, graph_id, version, node_id, action, actor, at, reason_ref) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (event_id, graph_id, graph["version"], node_id, action, actor, at, reason_ref),
            )
            if action == "set":
                db.execute(
                    "UPDATE nodes SET flag = 1, flag_set_by = ?, flag_set_at = ?, "
                    "flag_reason_ref = ? WHERE graph_id = ? AND node_id = ?",
                    (actor, at, reason_ref, graph_id, node_id),
                )
            else:
                db.execute(
                    "UPDATE nodes SET flag = 0, flag_set_by = NULL, flag_set_at = NULL, "
                    "flag_reason_ref = NULL WHERE graph_id = ? AND node_id = ?",
                    (graph_id, node_id),
                )
            append_event(db, graph_id=graph_id, version=graph["version"],
                         type=f"flag_{action}", at=at, event_id=event_id,
                         data={"nodeId": node_id, "action": action, "actor": actor,
                               "reasonRef": reason_ref})
            if action == "set" and node.kind == "end":
                db.execute(
                    "UPDATE graphs SET closed_at=?, closed_by=? "
                    "WHERE graph_id=? AND closed_at IS NULL",
                    (at, node_id, graph_id),
                )
                if db.execute("SELECT changes()").fetchone()[0]:
                    append_event(
                        db,
                        graph_id=graph_id,
                        version=graph["version"],
                        type="graph_closed",
                        at=at,
                        data={"at": at, "by": node_id},
                    )
            planned = (
                self._plan_set(graph_id, node_id, event_id, actor)
                if action == "set"
                else self._plan_reset(graph_id, node_id, event_id, actor)
            )
            inserted = self._insert_notifications(
                planned, at, db=db, graph_id=graph_id, version=graph["version"],
            )
            db.commit()
        except BaseException:
            db.rollback()
            raise

        # Network I/O is outside SQLite's write lock; recovery only replays
        # these durable plans, never reinterprets old flags against a new graph.
        delivered, undelivered, error = self._deliver(graph_id, tuple(inserted))
        return FlagEventOutcome(
            event={
                "eventId": event_id,
                "graphId": graph_id,
                "nodeId": node_id,
                "action": action,
                "actor": actor,
                "at": at,
                "version": graph["version"],
            },
            planned=tuple(inserted),
            delivered=delivered,
            undelivered=undelivered,
            delivery_error=error,
        )

    # -- clock nodes (phases P2) ----------------------------------------------------

    def tick_clocks(self, graph_id: str) -> list[PlannedNotification]:
        """Derive overdue notifications for clock nodes past deadline.

        An overdue is NOT a flag event (concept §4: it must not pollute the
        fact table). A guarded clock checks its watched task's flag after the
        same ``BEGIN IMMEDIATE`` that protects notification insertion; a task
        completion that wins that write transaction suppresses overdue.
        Otherwise overdue is recorded only in ``notifications`` under a
        synthetic ``overdue:<graph>:v<version>:<node>:<deadline>`` id, which
        makes the ``(event_id, edge)`` key idempotent per deadline —
        re-ticking never re-notifies.  When the owner then sets the flag,
        the normal forward trigger runs; overdue never blocks it.

        The sending identity for an overdue is the graph's creator (the
        clock itself is nobody); owners of the overdue node AND of every
        forward upstream are notified.
        """

        db = self._store.write()
        try:
            graph = self._require_graph(graph_id)
            now = int(self._clock())
            planned = (self._plan_clocks(graph_id, graph, now)
                       if graph["activated_at"] is not None and graph["closed_at"] is None else [])
            inserted = self._insert_notifications(
                planned, now, db=db, graph_id=graph_id, version=graph["version"],
            )
            db.commit()
        except BaseException:
            db.rollback()
            raise
        delivered, undelivered, _error = self._deliver(graph_id, tuple(inserted))
        return [*delivered, *undelivered]

    def _plan_clocks(
        self, graph_id: str, graph: dict[str, Any], now: int,
    ) -> list[PlannedNotification]:
        planned: list[PlannedNotification] = []
        nodes = {node.node_id: node for node in self._store.nodes(graph_id)}
        for node in self._store.nodes(graph_id):
            if node.kind != "clock" or node.flag:
                continue
            if (
                node.guarded_by_node_id is not None
                and nodes[node.guarded_by_node_id].flag
            ):
                continue
            assert node.deadline_ms is not None  # edit-time validation guarantees it
            if now <= node.deadline_ms:
                continue
            event_id = (
                f"overdue:{graph_id}:v{graph['version']}:"
                f"{node.node_id}:{node.deadline_ms}"
            )
            targets = [(node.node_id, node.owner)] + [
                (pred, nodes[pred].owner)
                for pred in self._forward_predecessors(graph_id, node.node_id)
            ]
            for target_node, owner in targets:
                edge = (
                    f"clock:{node.node_id}"
                    if target_node == node.node_id
                    else f"clock-up:{target_node}->{node.node_id}"
                )
                planned.append(
                    PlannedNotification(
                        event_id=event_id,
                        edge=edge,
                        kind=OVERDUE,
                        recipient=owner,
                        node_id=node.node_id,
                        round_no=None,
                        text=overdue_text(
                            node.node_id, node.brief_ref, event_id, node.deadline_ms
                        ),
                        sender=graph["created_by"],
                    )
                )
        return planned

    # -- resend ------------------------------------------------------------------------

    def resend_undelivered(self, graph_id: str) -> dict[str, Any]:
        """Retry exactly the notification rows with no ``message_id`` yet."""

        if self._require_graph(graph_id)["closed_at"] is not None:
            raise PacError(PAC_GRAPH_CLOSED, "closed graph notifications are not retried")
        if self._sender is None:
            raise PacError(
                PAC_NOTIFY_DELIVERY_FAILED, "no notification sender configured"
            )
        delivered = 0
        remaining = 0
        error: str | None = None
        rows = [
            *self._store.notifications(graph_id),
            *self._store.notifications_by_synthetic_event(graph_id),
        ]
        for index, row in enumerate(rows):
            if row.message_id is not None:
                continue
            if self._require_graph(graph_id)["closed_at"] is not None:
                remaining += sum(item.message_id is None for item in rows[index:])
                error = "graph closed before resend; remaining plans are not retried"
                break
            try:
                message_id = self._sender.send(
                    recipient=row.recipient,
                    text=row.text,
                    sender=row.sender,
                    conversation_id=f"pac-{graph_id}",
                    idempotency_key=f"pac-notify:{row.event_id}:{row.edge}",
                )
            except Exception as failure:  # noqa: BLE001
                remaining += 1
                error = f"{type(failure).__name__}: {failure}"
                continue
            self._mark_delivered(row.event_id, row.edge, message_id)
            delivered += 1
        return {"delivered": delivered, "undelivered": remaining, "error": error}
