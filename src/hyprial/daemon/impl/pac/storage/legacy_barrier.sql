-- Legacy table shapes retained ONLY to reject old writers on fresh and upgraded homes.
-- No legacy executor, data replay or new legacy run is installed.

CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    yaml_text TEXT NOT NULL,
    sender TEXT NOT NULL,
    nonce TEXT NOT NULL,
    state TEXT NOT NULL,
    report_text TEXT,
    dispatch_warnings TEXT NOT NULL DEFAULT '[]',
    created_at_ms INTEGER NOT NULL,
    finished_at_ms INTEGER
);
CREATE TABLE IF NOT EXISTS targets (
    run_id TEXT NOT NULL,
    target TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    state TEXT NOT NULL,
    attempts INTEGER NOT NULL,
    deadline_ms INTEGER,
    next_attempt_ms INTEGER,
    last_message_id TEXT,
    reply_excerpt TEXT,
    delivered INTEGER NOT NULL DEFAULT 0,
    extend_count INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (run_id, target)
);
CREATE INDEX IF NOT EXISTS targets_run ON targets(run_id);

CREATE TABLE IF NOT EXISTS workflow_effect_batches (
    correlation_id TEXT PRIMARY KEY,
    final_kind TEXT NOT NULL,
    final_payload_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS workflow_effects (
    effect_id TEXT PRIMARY KEY,
    correlation_id TEXT NOT NULL,
    generation INTEGER NOT NULL,
    payload_json TEXT NOT NULL,
    FOREIGN KEY (correlation_id) REFERENCES workflow_effect_batches(correlation_id)
);
CREATE INDEX IF NOT EXISTS workflow_effects_batch
ON workflow_effects(correlation_id);
CREATE TABLE IF NOT EXISTS workflow_node_deliveries (
    message_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    target_ref TEXT NOT NULL,
    recipient TEXT NOT NULL,
    recorded_at_ms INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS workflow_node_deliveries_target
ON workflow_node_deliveries(run_id, target_ref);
CREATE TABLE IF NOT EXISTS workflow_external_refs (
    external_ref TEXT PRIMARY KEY,
    request_digest TEXT NOT NULL,
    run_id TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS workflow_external_refs_run
ON workflow_external_refs(run_id);
CREATE TABLE IF NOT EXISTS agent_task_runs (
    run_id TEXT PRIMARY KEY,
    service_actor TEXT NOT NULL,
    namespace TEXT NOT NULL,
    external_ref TEXT NOT NULL,
    request_digest TEXT NOT NULL,
    caller TEXT NOT NULL,
    metadata_json TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    completion_json TEXT NOT NULL,
    state TEXT NOT NULL,
    last_event_id TEXT,
    cancel_reason TEXT,
    created_at_ms INTEGER NOT NULL,
    finished_at_ms INTEGER,
    UNIQUE(service_actor, namespace, external_ref)
);
CREATE TABLE IF NOT EXISTS agent_task_targets (
    run_id TEXT NOT NULL,
    target_ref TEXT NOT NULL,
    target TEXT NOT NULL,
    role TEXT NOT NULL,
    delegates_json TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    state TEXT NOT NULL,
    result_ref TEXT,
    PRIMARY KEY(run_id, target_ref),
    UNIQUE(run_id, target),
    UNIQUE(run_id, conversation_id)
);
CREATE TABLE IF NOT EXISTS agent_task_events (
    event_id TEXT PRIMARY KEY,
    event_digest TEXT NOT NULL,
    run_id TEXT NOT NULL,
    target_ref TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    submitter TEXT NOT NULL,
    at TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    message_id TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS agent_task_events_run ON agent_task_events(run_id);
CREATE TABLE IF NOT EXISTS agent_task_results (
    run_id TEXT NOT NULL,
    target_ref TEXT NOT NULL,
    result_ref TEXT NOT NULL,
    result_digest TEXT NOT NULL,
    message_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    artifact_refs_json TEXT NOT NULL,
    submitted_at TEXT NOT NULL,
    PRIMARY KEY(run_id, target_ref),
    UNIQUE(run_id, target_ref, result_ref)
);


CREATE TABLE IF NOT EXISTS actor_assign (
    actor          TEXT NOT NULL,
    assign_kind    TEXT NOT NULL,
    assign_ref     TEXT NOT NULL,
    assigned_at_ms INTEGER NOT NULL,
    released_at_ms INTEGER,
    PRIMARY KEY (actor, assign_kind, assign_ref)
);
CREATE INDEX IF NOT EXISTS actor_assign_live
    ON actor_assign(actor) WHERE released_at_ms IS NULL;
CREATE INDEX IF NOT EXISTS actor_assign_ref
    ON actor_assign(assign_kind, assign_ref) WHERE released_at_ms IS NULL;
