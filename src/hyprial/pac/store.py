"""The PAC graph store, request projections and typed transaction journal.

``pac-graph.sqlite3`` lives in the state root next to — but strictly
separate from — the sealed legacy ``workflows.sqlite3`` archive.  This module owns the schema
and the row-level reads/writes; edit semantics (CAS, validation) live in
:mod:`hyprial.pac.graph`, reactor semantics in :mod:`hyprial.pac.reactor`.

The schema follows concept §2 with the minimum of implementation detail
the concept itself names as sketch-level:

- ``flag_events`` gets a monotonic ``seq`` beside the uuid ``event_id`` so
  replay order is total even for same-millisecond events (the concept
  calls its field list a sketch; append-only + audit ordering is the
  invariant, not the column list).
- ``notifications`` is keyed ``(event_id, edge)`` exactly as the concept
  prescribes: that primary key IS the idempotency contract (§3.1).  The
  row also carries the exact ``text`` and ``sender`` it was delivered
  with, so ``hyprial workflow notify resend`` retries byte-for-byte. ``plan_json``
  captures the version and predecessor facts at the decision's write lock;
  pre-upgrade rows retain NULL rather than invented historical inputs.
- Schema upgrades are explicit and atomic (see :mod:`hyprial.pac.migrations`).
  ``journal`` provides one append-only ordering for flags, durable plans,
  and delivery updates, written inside their business transactions.
- ``nodes.kind`` / ``nodes.deadline_ms`` carry the clock-node shape
  (concept §4 ``on_timeout`` row; phases P2). ``guarded_by_node_id`` is an
  explicit opt-in link from a clock to the task whose completion suppresses
  overdue notification.
- ``graphs.operation_key`` is nullable for legacy/ad-hoc graphs and unique
  when present, making caller-owned create/replay identity durable.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .journal import envelope
from .migrations import migrate

DATABASE_NAME = "pac-graph.sqlite3"

#: ``brief_ref`` is a reference, never a body: bounded, single line.
MAX_BRIEF_REF_LENGTH = 512

#: Caller-owned graph identity is bounded and single-line for safe diagnostics.
MAX_OPERATION_KEY_LENGTH = 512

SCHEMA = """
CREATE TABLE IF NOT EXISTS graphs (
    graph_id   TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    version    INTEGER NOT NULL,
    created_by TEXT NOT NULL,
    created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS nodes (
    graph_id       TEXT NOT NULL,
    node_id        TEXT NOT NULL,
    owner          TEXT NOT NULL,
    brief_ref      TEXT NOT NULL,
    kind           TEXT NOT NULL DEFAULT 'task'
                        CHECK (kind IN ('task', 'clock', 'actor', 'end')),
    deadline_ms    INTEGER,
    flag           INTEGER NOT NULL DEFAULT 0,
    flag_set_by    TEXT,
    flag_set_at    INTEGER,
    flag_reason_ref TEXT,
    actor_name     TEXT UNIQUE,
    launch_ref     TEXT,
    PRIMARY KEY (graph_id, node_id),
    FOREIGN KEY (graph_id) REFERENCES graphs (graph_id),
    CHECK ((kind = 'actor' AND actor_name IS NOT NULL AND launch_ref IS NOT NULL)
        OR (kind != 'actor' AND actor_name IS NULL AND launch_ref IS NULL))
);

CREATE TABLE IF NOT EXISTS edges (
    graph_id  TEXT NOT NULL,
    from_node TEXT NOT NULL,
    to_node   TEXT NOT NULL,
    kind      TEXT NOT NULL CHECK (kind IN ('forward', 'back')),
    PRIMARY KEY (graph_id, from_node, to_node),
    FOREIGN KEY (graph_id) REFERENCES graphs (graph_id)
);

CREATE TABLE IF NOT EXISTS flag_events (
    seq        INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id   TEXT NOT NULL UNIQUE,
    graph_id   TEXT NOT NULL,
    version    INTEGER NOT NULL,
    node_id    TEXT NOT NULL,
    action     TEXT NOT NULL CHECK (action IN ('set', 'reset')),
    actor      TEXT NOT NULL,
    at         INTEGER NOT NULL,
    reason_ref TEXT,
    FOREIGN KEY (graph_id) REFERENCES graphs (graph_id)
);
CREATE INDEX IF NOT EXISTS flag_events_graph_order
    ON flag_events (graph_id, seq);

CREATE TABLE IF NOT EXISTS notifications (
    event_id     TEXT NOT NULL,
    edge         TEXT NOT NULL,
    kind         TEXT NOT NULL
                     CHECK (kind IN ('turn', 'withdraw', 'overdue', 'launch_failed', 'actor_alert')),
    recipient    TEXT NOT NULL,
    round_no     INTEGER,
    text         TEXT NOT NULL,
    sender       TEXT NOT NULL,
    message_id   TEXT,
    at           INTEGER NOT NULL,
    delivered_at INTEGER,
    PRIMARY KEY (event_id, edge)
    -- Deliberately NO foreign key on event_id: overdue notifications
    -- (phases P2) carry a synthetic "overdue:<graph>:v<version>:<node>:<ms>"
    -- id precisely because an overdue clock is NOT a flag event (concept
    -- §4: overdue must not pollute flag_events' fact semantics).
);
CREATE INDEX IF NOT EXISTS notifications_recipient
    ON notifications (recipient, at);
"""


@dataclass(frozen=True, slots=True)
class NodeRow:
    graph_id: str
    node_id: str
    owner: str
    brief_ref: str
    kind: str
    deadline_ms: int | None
    flag: bool
    flag_set_by: str | None
    flag_set_at: int | None
    flag_reason_ref: str | None
    actor_name: str | None
    launch_ref: str | None
    requires: dict[str, Any] | None
    guarded_by_node_id: str | None

    def to_json(self) -> dict[str, Any]:
        return {
            "nodeId": self.node_id,
            "owner": self.owner,
            "briefRef": self.brief_ref,
            "kind": self.kind,
            **({"deadlineMs": self.deadline_ms} if self.deadline_ms else {}),
            "flag": self.flag,
            **({"flagSetBy": self.flag_set_by} if self.flag_set_by else {}),
            **({"flagSetAt": self.flag_set_at} if self.flag_set_at else {}),
            **(
                {"flagReasonRef": self.flag_reason_ref}
                if self.flag_reason_ref
                else {}
            ),
            **({"actorName": self.actor_name} if self.actor_name else {}),
            **({"launchRef": self.launch_ref} if self.launch_ref else {}),
            **({"requires": self.requires} if self.requires is not None else {}),
            **(
                {"guardedByNodeId": self.guarded_by_node_id}
                if self.guarded_by_node_id is not None
                else {}
            ),
        }


@dataclass(frozen=True, slots=True)
class EdgeRow:
    graph_id: str
    from_node: str
    to_node: str
    kind: str

    def to_json(self) -> dict[str, Any]:
        return {"from": self.from_node, "to": self.to_node, "kind": self.kind}


@dataclass(frozen=True, slots=True)
class FlagEventRow:
    seq: int
    event_id: str
    graph_id: str
    version: int
    node_id: str
    action: str
    actor: str
    at: int
    reason_ref: str | None

    def to_json(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "eventId": self.event_id,
            "version": self.version,
            "nodeId": self.node_id,
            "action": self.action,
            "actor": self.actor,
            "at": self.at,
            **({"reasonRef": self.reason_ref} if self.reason_ref else {}),
        }


@dataclass(frozen=True, slots=True)
class NotificationRow:
    event_id: str
    edge: str
    kind: str
    recipient: str
    round_no: int | None
    text: str
    sender: str
    message_id: str | None
    at: int
    delivered_at: int | None
    plan: dict[str, Any] | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "eventId": self.event_id,
            "edge": self.edge,
            "kind": self.kind,
            "recipient": self.recipient,
            **({"round": self.round_no} if self.round_no else {}),
            "text": self.text,
            "sender": self.sender,
            "plan": self.plan,
            **({"messageId": self.message_id} if self.message_id else {}),
            "at": self.at,
            **(
                {"deliveredAt": self.delivered_at}
                if self.delivered_at
                else {}
            ),
        }


def _notification_row(row: sqlite3.Row) -> NotificationRow:
    return NotificationRow(
        event_id=row["event_id"],
        edge=row["edge"],
        kind=row["kind"],
        recipient=row["recipient"],
        round_no=row["round_no"],
        text=row["text"],
        sender=row["sender"],
        message_id=row["message_id"],
        at=row["at"],
        delivered_at=row["delivered_at"],
        plan=json.loads(row["plan_json"]) if row["plan_json"] else None,
    )


def connect(path: Path, *, read_only: bool = False) -> sqlite3.Connection:
    """Open (and migrate, unless read-only) the pac-graph database.

    Read-only callers (the projection layer) get ``mode=ro``: the
    connection cannot write even by accident, which is the mechanism
    behind the slice-1 ⑤ assertion that the projection consumer never writes.
    """

    if read_only:
        uri = f"file:{path}?mode=ro"
        connection = sqlite3.connect(uri, uri=True)
        connection.row_factory = sqlite3.Row
        return connection
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=NORMAL")
    connection.execute("PRAGMA foreign_keys=ON")
    try:
        migrate(connection, SCHEMA, state_dir=Path(path).parent)
    except BaseException:
        connection.close()
        raise
    return connection


class PacGraphStore:
    """Row-level access to one ``pac-graph.sqlite3``.

    Every mutation runs inside ``BEGIN IMMEDIATE`` (the caller opens the
    transaction via :meth:`write`), so concurrent editors serialize at the
    database level and the CAS check + write + version bump are one atomic
    step.
    """

    def __init__(self, path: Path, *, read_only: bool = False) -> None:
        self.path = Path(path)
        self._db = connect(self.path, read_only=read_only)

    def close(self) -> None:
        self._db.close()

    # -- transactions ------------------------------------------------------

    @contextmanager
    def read(self):
        """Pin a multi-query read; compose safely with a caller's write txn."""
        owns_transaction = not self._db.in_transaction
        if owns_transaction:
            self._db.execute("BEGIN")
        try:
            yield self._db
        finally:
            if owns_transaction:
                self._db.rollback()

    def write(self) -> sqlite3.Connection:
        """Enter a write transaction; commit/rollback is the caller's."""

        self._db.execute("BEGIN IMMEDIATE")
        return self._db

    # -- graphs ------------------------------------------------------------

    def graph(self, graph_id: str) -> dict[str, Any] | None:
        row = self._db.execute(
            "SELECT * FROM graphs WHERE graph_id = ?", (graph_id,)
        ).fetchone()
        return dict(row) if row is not None else None

    def graph_by_operation_key(self, operation_key: str) -> dict[str, Any] | None:
        """Return the one graph durably identified by ``operation_key``."""

        row = self._db.execute(
            "SELECT * FROM graphs WHERE operation_key = ?", (operation_key,)
        ).fetchone()
        return dict(row) if row is not None else None

    # -- nodes / edges -----------------------------------------------------

    def graph_ids(self) -> list[str]:
        """List graph ids through the PAC store boundary."""
        return [str(row["graph_id"]) for row in self._db.execute(
            "SELECT graph_id FROM graphs ORDER BY graph_id"
        )]

    def nodes(self, graph_id: str) -> list[NodeRow]:
        return [
            NodeRow(
                graph_id=row["graph_id"],
                node_id=row["node_id"],
                owner=row["owner"],
                brief_ref=row["brief_ref"],
                kind=row["kind"],
                deadline_ms=row["deadline_ms"],
                flag=bool(row["flag"]),
                flag_set_by=row["flag_set_by"],
                flag_set_at=row["flag_set_at"],
                flag_reason_ref=row["flag_reason_ref"],
                actor_name=row["actor_name"],
                launch_ref=row["launch_ref"],
                requires=(json.loads(row["requires_json"]) if row["requires_json"] else None),
                guarded_by_node_id=row["guarded_by_node_id"],
            )
            for row in self._db.execute(
                "SELECT * FROM nodes WHERE graph_id = ? ORDER BY node_id",
                (graph_id,),
            )
        ]

    def node(self, graph_id: str, node_id: str) -> NodeRow | None:
        for row in self.nodes(graph_id):
            if row.node_id == node_id:
                return row
        return None

    def edges(self, graph_id: str) -> list[EdgeRow]:
        return [
            EdgeRow(
                graph_id=row["graph_id"],
                from_node=row["from_node"],
                to_node=row["to_node"],
                kind=row["kind"],
            )
            for row in self._db.execute(
                "SELECT * FROM edges WHERE graph_id = ? ORDER BY from_node, to_node",
                (graph_id,),
            )
        ]

    # -- managed workflow ownership --------------------------------------

    def workflow_worker_receipts(self, graph_id: str) -> list[dict[str, Any]]:
        """Return only explicit graph-owned worker receipts, in actor order."""

        return [
            dict(row)
            for row in self._db.execute(
                "SELECT * FROM workflow_worker_receipts WHERE graph_id=? "
                "ORDER BY actor_node",
                (graph_id,),
            )
        ]

    def workflow_worker_receipt(
        self, graph_id: str, actor_node: str
    ) -> dict[str, Any] | None:
        row = self._db.execute(
            "SELECT * FROM workflow_worker_receipts "
            "WHERE graph_id=? AND actor_node=?",
            (graph_id, actor_node),
        ).fetchone()
        return dict(row) if row is not None else None

    def workflow_worker_cleanup_intents(
        self, graph_id: str
    ) -> list[dict[str, Any]]:
        """Return durable terminal cleanup work in actor order."""

        return [
            dict(row)
            for row in self._db.execute(
                "SELECT * FROM workflow_worker_cleanup_intents WHERE graph_id=? "
                "ORDER BY actor_node",
                (graph_id,),
            )
        ]

    def workflow_worker_cleanup_intent(
        self, graph_id: str, actor_node: str
    ) -> dict[str, Any] | None:
        row = self._db.execute(
            "SELECT * FROM workflow_worker_cleanup_intents "
            "WHERE graph_id=? AND actor_node=?",
            (graph_id, actor_node),
        ).fetchone()
        return dict(row) if row is not None else None

    def workflow_worker_cleanup(self, graph_id: str) -> str | None:
        """Project terminal cleanup independently from workflow settlement.

        Terminal graphs created before schema 16 have receipts but no cleanup
        intents.  Their absence is intentionally not reinterpreted as either
        ownership authority or successful reclaim.
        """

        workflow = self._db.execute(
            "SELECT state FROM workflow_graphs WHERE graph_id=?", (graph_id,)
        ).fetchone()
        if workflow is None or workflow["state"] not in {
            "completed",
            "failed",
            "cancelled",
        }:
            return None
        receipts = self.workflow_worker_receipts(graph_id)
        intents = self.workflow_worker_cleanup_intents(graph_id)
        if not receipts:
            return "complete"
        if not intents:
            return None
        if len(intents) != len(receipts):
            return "attention"
        states = {str(row["state"]) for row in intents}
        if "attention" in states:
            return "attention"
        if states == {"complete"}:
            return "complete"
        return "pending"

    def workflow_worker_cleanup_attention(
        self, observed_at_ms: int
    ) -> list[dict[str, Any]]:
        """Return operator-facing cleanup debt without guessing liveness."""

        findings: list[dict[str, Any]] = []
        for row in self._db.execute(
            "SELECT * FROM workflow_worker_cleanup_intents "
            "WHERE state='attention' ORDER BY created_at,graph_id,actor_node"
        ):
            try:
                observation = json.loads(row["last_observation_json"] or "null")
            except (TypeError, ValueError):
                observation = {"malformed": True}
            findings.append(
                {
                    "graphId": row["graph_id"],
                    "actor": row["actor_uri"],
                    "actorName": row["actor_name"],
                    "actorNode": row["actor_node"],
                    "operationId": row["operation_id"],
                    "ageMs": max(0, observed_at_ms - int(row["created_at"])),
                    "reason": row["attention_reason"],
                    "lastObservation": observation,
                }
            )
        return findings

    def workflow_roster(self, graph_id: str) -> dict[str, Any] | None:
        """Read and integrity-check the stored roster and its receipts.

        Pre-v15 graphs whose artifacts were unavailable during migration keep
        both roster columns NULL.  That is the only accepted absence; a new or
        derivably migrated roster must agree with every actor node and launch
        reference exactly.
        """

        from .errors import WORKFLOW_WORKER_RECEIPT_MISMATCH, PacError
        from .workflow_roster import validate_roster_document

        meta = self._db.execute(
            "SELECT roster_json,roster_digest FROM workflow_graphs "
            "WHERE graph_id=?",
            (graph_id,),
        ).fetchone()
        if meta is None:
            return None
        if meta["roster_json"] is None and meta["roster_digest"] is None:
            return None
        if meta["roster_json"] is None or meta["roster_digest"] is None:
            raise PacError(
                WORKFLOW_WORKER_RECEIPT_MISMATCH,
                "workflow roster document and digest must be stored together",
            )
        try:
            roster = validate_roster_document(
                graph_id, str(meta["roster_json"]), str(meta["roster_digest"])
            )
        except ValueError as error:
            raise PacError(
                WORKFLOW_WORKER_RECEIPT_MISMATCH, str(error)
            ) from error

        actor_nodes = {
            str(row["node_id"]): row
            for row in self._db.execute(
                "SELECT node_id,actor_name,launch_ref FROM nodes "
                "WHERE graph_id=? AND kind='actor'",
                (graph_id,),
            )
        }
        receipts = self.workflow_worker_receipts(graph_id)
        if {str(row["actor_node"]) for row in receipts} != set(actor_nodes):
            raise PacError(
                WORKFLOW_WORKER_RECEIPT_MISMATCH,
                "workflow worker receipt set does not match graph actor nodes",
            )
        for receipt in receipts:
            actor = actor_nodes[str(receipt["actor_node"])]
            launch_digest = str(actor["launch_ref"] or "").partition("#sha256=")[2]
            owners = {
                str(row[0])
                for row in self._db.execute(
                    "SELECT n.owner FROM workflow_nodes w "
                    "JOIN nodes n ON n.graph_id=w.graph_id AND n.node_id=w.node_id "
                    "WHERE w.graph_id=? AND w.actor_node=?",
                    (graph_id, receipt["actor_node"]),
                )
            }
            if (
                receipt["ownership"] != "workflow"
                or receipt["state"] not in {"planned", "down"}
                or receipt["actor_name"] != actor["actor_name"]
                or receipt["launch_digest"] != launch_digest
                or receipt["roster_digest"] != meta["roster_digest"]
                or owners != {str(receipt["actor_uri"])}
            ):
                raise PacError(
                    WORKFLOW_WORKER_RECEIPT_MISMATCH,
                    "workflow worker receipt does not match its actor node, "
                    "launch artifact, roster, and assigned owner",
                )
        return roster

    # -- flag events -------------------------------------------------------

    def flag_events(self, graph_id: str) -> list[FlagEventRow]:
        return [
            FlagEventRow(
                seq=row["seq"],
                event_id=row["event_id"],
                graph_id=row["graph_id"],
                version=row["version"],
                node_id=row["node_id"],
                action=row["action"],
                actor=row["actor"],
                at=row["at"],
                reason_ref=row["reason_ref"],
            )
            for row in self._db.execute(
                "SELECT * FROM flag_events WHERE graph_id = ? ORDER BY seq",
                (graph_id,),
            )
        ]

    def set_event_count(self, graph_id: str, node_id: str) -> int:
        """How many ``set`` actions a node has recorded (concept §3.3: the
        loop round number is counted out of ``flag_events``)."""

        row = self._db.execute(
            "SELECT COUNT(*) FROM flag_events "
            "WHERE graph_id = ? AND node_id = ? AND action = 'set'",
            (graph_id, node_id),
        ).fetchone()
        return int(row[0])

    def journal_events(self, graph_id: str, after_seq: int = 0) -> list[dict[str, Any]]:
        return [envelope(row) for row in self._db.execute(
            "SELECT * FROM journal WHERE graph_id = ? AND seq > ? ORDER BY seq",
            (graph_id, after_seq),
        )]

    # -- notifications -----------------------------------------------------

    def notifications(self, graph_id: str) -> list[NotificationRow]:
        """Rows whose event is a flag event (turns + withdraws), in event
        order.  Overdue rows (synthetic ids) are NOT here — see
        :meth:`notifications_by_synthetic_event`."""

        return [
            _notification_row(row)
            for row in self._db.execute(
                "SELECT n.* FROM notifications n "
                "JOIN flag_events e ON e.event_id = n.event_id "
                "WHERE e.graph_id = ? ORDER BY e.seq, n.edge",
                (graph_id,),
            )
        ]

    def notifications_by_synthetic_event(self, graph_id: str) -> list[NotificationRow]:
        """Notification rows whose event id is synthetic (overdue clocks)."""

        return [
            _notification_row(row)
            for row in self._db.execute(
                "SELECT n.* FROM notifications n "
                "WHERE n.event_id NOT IN (SELECT event_id FROM flag_events) "
                "ORDER BY n.event_id, n.edge"
            )
            if (json.loads(row["plan_json"])["graphId"] == graph_id
                if row["plan_json"] else row["event_id"].startswith(f"overdue:{graph_id}:"))
        ]

    def notification_exists(self, event_id: str, edge: str) -> bool:
        row = self._db.execute(
            "SELECT 1 FROM notifications WHERE event_id = ? AND edge = ?",
            (event_id, edge),
        ).fetchone()
        return row is not None


def default_database_path(state_dir: Path) -> Path:
    """Where the pac-graph database lives under a state root."""

    return Path(state_dir) / DATABASE_NAME
