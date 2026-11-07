BEGIN TRANSACTION;
CREATE TABLE edges (
    graph_id  TEXT NOT NULL,
    from_node TEXT NOT NULL,
    to_node   TEXT NOT NULL,
    kind      TEXT NOT NULL CHECK (kind IN ('forward', 'back')),
    PRIMARY KEY (graph_id, from_node, to_node),
    FOREIGN KEY (graph_id) REFERENCES graphs (graph_id)
);
INSERT INTO "edges" VALUES('legacy-generated-d3c31828','a','b','forward');
CREATE TABLE flag_events (
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
INSERT INTO "flag_events" VALUES(1,'3fb4a97a-3a47-4e0c-9538-c7db66beb979','legacy-generated-d3c31828',5,'a','set','fixture-owner',100,'artifact#result-a');
INSERT INTO "flag_events" VALUES(2,'3722d968-ee17-4bb1-88b4-ca019cea812a','legacy-generated-d3c31828',5,'a','reset','fixture-owner',100,'artifact#withdraw');
INSERT INTO "flag_events" VALUES(3,'ebda3f70-47de-4b5c-a361-25260d66d115','legacy-generated-d3c31828',5,'a','set','fixture-owner',100,'artifact#result-a-v2');
INSERT INTO "flag_events" VALUES(4,'38915071-7913-43b6-aa7c-d260cb41e9c9','legacy-generated-d3c31828',5,'b','set','fixture-owner',100,'artifact#result-b');
CREATE TABLE graphs (
    graph_id   TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    version    INTEGER NOT NULL,
    created_by TEXT NOT NULL,
    created_at INTEGER NOT NULL
);
INSERT INTO "graphs" VALUES('legacy-generated-d3c31828','legacy-generated',6,'fixture-owner',1788786572661);
CREATE TABLE nodes (
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
INSERT INTO "nodes" VALUES('legacy-generated-d3c31828','a','fixture-owner','artifact#brief-a','task',NULL,1,'fixture-owner',100,'artifact#result-a-v2');
INSERT INTO "nodes" VALUES('legacy-generated-d3c31828','b','fixture-owner','artifact#brief-b','task',NULL,1,'fixture-owner',100,'artifact#result-b');
INSERT INTO "nodes" VALUES('legacy-generated-d3c31828','timer','fixture-owner','artifact#brief-timer','clock',50,0,NULL,NULL,NULL);
INSERT INTO "nodes" VALUES('legacy-generated-d3c31828','later','fixture-owner','artifact#later','task',NULL,0,NULL,NULL,NULL);
CREATE TABLE notifications (
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
INSERT INTO "notifications" VALUES('3fb4a97a-3a47-4e0c-9538-c7db66beb979','a -> b','turn','fixture-owner',1,'轮到你:b artifact#brief-b (event 3fb4a97a-3a47-4e0c-9538-c7db66beb979)','fixture-owner',NULL,100,NULL);
INSERT INTO "notifications" VALUES('3722d968-ee17-4bb1-88b4-ca019cea812a','a -> b','withdraw','fixture-owner',NULL,'已撤回:b artifact#brief-b (event 3722d968-ee17-4bb1-88b4-ca019cea812a)','fixture-owner',NULL,100,NULL);
INSERT INTO "notifications" VALUES('ebda3f70-47de-4b5c-a361-25260d66d115','a -> b','turn','fixture-owner',1,'轮到你:b artifact#brief-b (event ebda3f70-47de-4b5c-a361-25260d66d115)','fixture-owner',NULL,100,NULL);
INSERT INTO "notifications" VALUES('overdue:legacy-generated-d3c31828:v5:timer:50','clock:timer','overdue','fixture-owner',NULL,'逾期:timer artifact#brief-timer (deadline 50 ms, event overdue:legacy-generated-d3c31828:v5:timer:50)','fixture-owner',NULL,100,NULL);
CREATE INDEX flag_events_graph_order
    ON flag_events (graph_id, seq);
CREATE INDEX notifications_recipient
    ON notifications (recipient, at);
DELETE FROM "sqlite_sequence";
INSERT INTO "sqlite_sequence" VALUES('flag_events',4);
COMMIT;
