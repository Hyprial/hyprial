-- Exact SCHEMA from 17de00f65b73468a985a78e3ce66b8e16319a406 src/hyprial/pac/store.py

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
                        CHECK (kind IN ('task', 'clock')),
    deadline_ms    INTEGER,
    flag           INTEGER NOT NULL DEFAULT 0,
    flag_set_by    TEXT,
    flag_set_at    INTEGER,
    flag_reason_ref TEXT,
    PRIMARY KEY (graph_id, node_id),
    FOREIGN KEY (graph_id) REFERENCES graphs (graph_id)
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
                     CHECK (kind IN ('turn', 'withdraw', 'overdue')),
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
