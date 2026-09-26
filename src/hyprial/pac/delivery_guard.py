"""Recheck an exact graph request before it can enter a model turn.

The message binding is written before inbox submission. Text, recipient names
and model replies are never used to classify a message as a PAC request.
"""

from pathlib import Path
from time import time_ns

from .store import PacGraphStore, default_database_path
from .workflow_graph import input_token
from .remote_binding import remote_binding, remote_current

WITHDRAWN = "PAC_REQUEST_WITHDRAWN"


def is_workflow_delivery(state_dir: Path, message_id: str) -> bool:
    if not message_id.startswith("workflow-"):
        return False
    path = default_database_path(state_dir)
    if not path.exists():
        return False
    store = PacGraphStore(path, read_only=True)
    try:
        return bool(remote_binding(store, message_id)) or bool(
            store._db.execute(
                "SELECT 1 FROM workflow_deliveries WHERE message_id=?", (message_id,)
            ).fetchone()
        )
    finally:
        store.close()


def delivery_current(
    state_dir: Path, message_id: str, *, now_ms: int | None = None
) -> bool:
    # Ordinary messages do not pay a database lookup. This prefix is only an
    # optimization: the durable binding, not the prefix, grants classification.
    if not message_id.startswith("workflow-"):
        return True
    path = default_database_path(state_dir)
    if not path.exists():
        return True
    store = PacGraphStore(path, read_only=True)
    try:
        with store.read():
            if not store._db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='workflow_deliveries'"
            ).fetchone():
                return True
            bound = store._db.execute(
                "SELECT * FROM workflow_deliveries WHERE message_id=?", (message_id,)
            ).fetchone()
            if bound is None:
                remote = remote_binding(store, message_id)
                if remote is not None:
                    return remote_current(state_dir, remote, now_ms=now_ms)
                return True
            graph = store.graph(bound["graph_id"])
            row = store._db.execute(
                "SELECT * FROM workflow_nodes WHERE graph_id=? AND node_id=?",
                (bound["graph_id"], bound["node_id"]),
            ).fetchone()
            node = store.node(bound["graph_id"], bound["node_id"])
            at = time_ns() // 1_000_000 if now_ms is None else now_ms
            return bool(
                graph
                and graph["closed_at"] is None
                and row
                and node
                and not node.flag
                and row["state"] == "requested"
                and row["request_id"] == bound["request_id"]
                and row["deadline_ms"] is not None
                and at <= row["deadline_ms"]
                and row["input_token"]
                == input_token(store, bound["graph_id"], bound["node_id"])
            )
    finally:
        store.close()
