BEGIN TRANSACTION;
CREATE TABLE agent_secret_grants (agent TEXT NOT NULL, entity_token TEXT NOT NULL, grant_id TEXT NOT NULL, source TEXT NOT NULL CHECK (source IN ('user-provider', 'agent-private')), entry_id TEXT NOT NULL, field_name TEXT, environment_names TEXT NOT NULL, revision INTEGER NOT NULL CHECK (revision > 0), PRIMARY KEY (agent, grant_id), FOREIGN KEY (agent, entity_token) REFERENCES agents(actor, entity_token) ON DELETE CASCADE);
INSERT INTO "agent_secret_grants" VALUES('native','a00e2495edd24d38b44e60a29547209b','synthetic-grant','user-provider','synthetic-entry','key','["DEEPSEEK_API_KEY"]',2);
INSERT INTO "agent_secret_grants" VALUES('hosted','56104387eed34bd38cc2791a39a6cd38','synthetic-grant','user-provider','synthetic-entry','key','["DEEPSEEK_API_KEY"]',2);
CREATE TABLE agents (
    actor TEXT PRIMARY KEY,
    owner TEXT NOT NULL,
    machine TEXT NOT NULL,
    uri TEXT NOT NULL UNIQUE,
    entity_token TEXT NOT NULL UNIQUE,
    cwd TEXT,
    config TEXT,
    provider TEXT,
    model TEXT,
    capabilities TEXT NOT NULL DEFAULT '{}',
    harness_args TEXT NOT NULL DEFAULT '{}',
    preferred_harness TEXT,
    last_harness TEXT,
    last_session_id TEXT,
    created_at_ms INTEGER NOT NULL
, hosted_by TEXT DEFAULT NULL CHECK (hosted_by IN ('transfer-receive', 'squire-container')));
INSERT INTO "agents" VALUES('native','host-owner','host-node','agent:host-owner:host-node:native','a00e2495edd24d38b44e60a29547209b','/synthetic/native-work','{"discovery": "explicit-only", "sources": [{"path": "__HOME__/agents/native/config", "required": true}]}','synthetic-provider','synthetic-model','{"tag": "fixture"}','{"pi": ["--fixture"]}','pi','pi','synthetic-session-native',1700000000123,NULL);
INSERT INTO "agents" VALUES('hosted','visitor@example.test','host-node','agent:visitor@example.test:host-node:hosted','56104387eed34bd38cc2791a39a6cd38','/synthetic/hosted-work',NULL,NULL,NULL,'{}','{"claude": ["--fixture"]}','claude','pi','synthetic-session-hosted',1700000000123,'transfer-receive');
CREATE TABLE lifecycle_receipts (
    attempt_token TEXT PRIMARY KEY,
    operation_id TEXT NOT NULL,
    resource_key TEXT NOT NULL,
    expected_resource_token TEXT,
    created_by_operation INTEGER NOT NULL,
    changed INTEGER NOT NULL,
    resource_token TEXT NOT NULL,
    retired INTEGER NOT NULL DEFAULT 0
);
INSERT INTO "lifecycle_receipts" VALUES('synthetic-attempt-native','synthetic-operation-native','agent-record:native','9f602af9cd41411e970fc0a73975e4dd',1,1,'9f602af9cd41411e970fc0a73975e4dd',0);
INSERT INTO "lifecycle_receipts" VALUES('synthetic-attempt-hosted','synthetic-operation-hosted','agent-record:hosted','0178afd713e74330aa75c313f3bfa592',1,1,'0178afd713e74330aa75c313f3bfa592',0);
CREATE TABLE lifecycle_resources (
    resource_key TEXT PRIMARY KEY,
    resource_token TEXT NOT NULL,
    active INTEGER NOT NULL,
    payload TEXT NOT NULL
);
INSERT INTO "lifecycle_resources" VALUES('agent-record:native','9f602af9cd41411e970fc0a73975e4dd',1,'{"actor": "native", "capabilities": {"tag": "fixture"}, "config": null, "createdAtMs": 1700000000123, "cwd": "/synthetic/native-work", "entityToken": "a00e2495edd24d38b44e60a29547209b", "harnessArgs": {"pi": ["--fixture"]}, "hosted": false, "hostedBy": null, "lastHarness": null, "lastSessionId": null, "machine": "host-node", "model": "synthetic-model", "owner": "host-owner", "pinnedAdapters": [], "preferredHarness": "pi", "provider": "synthetic-provider", "uri": "agent:host-owner:host-node:native"}');
INSERT INTO "lifecycle_resources" VALUES('agent-home:native','23ebdd31e6344716b88c2cfc73383083',1,'{"actor": "native", "entityToken": "a00e2495edd24d38b44e60a29547209b", "ownerUid": __UID__, "path": "__HOME__/agents/native", "resourceToken": "23ebdd31e6344716b88c2cfc73383083", "schemaVersion": 1, "status": "ready"}');
INSERT INTO "lifecycle_resources" VALUES('agent-record:hosted','0178afd713e74330aa75c313f3bfa592',1,'{"actor": "hosted", "capabilities": {}, "config": null, "createdAtMs": 1700000000123, "cwd": "/synthetic/hosted-work", "entityToken": "56104387eed34bd38cc2791a39a6cd38", "harnessArgs": {"claude": ["--fixture"]}, "hosted": true, "hostedBy": "transfer-receive", "lastHarness": null, "lastSessionId": null, "machine": "host-node", "model": null, "owner": "visitor@example.test", "pinnedAdapters": [], "preferredHarness": "claude", "provider": null, "uri": "agent:visitor@example.test:host-node:hosted"}');
INSERT INTO "lifecycle_resources" VALUES('agent-home:hosted','d9c6ee39a3f54fb69073ac1316b85fcb',1,'{"actor": "hosted", "entityToken": "56104387eed34bd38cc2791a39a6cd38", "ownerUid": __UID__, "path": "__HOME__/agents/hosted", "resourceToken": "d9c6ee39a3f54fb69073ac1316b85fcb", "schemaVersion": 1, "status": "ready"}');
CREATE TABLE pins (
    adapter TEXT NOT NULL UNIQUE,
    agent TEXT NOT NULL UNIQUE
        REFERENCES agents(actor) ON DELETE CASCADE
);
INSERT INTO "pins" VALUES('synthetic-adapter-native','native');
INSERT INTO "pins" VALUES('synthetic-adapter-hosted','hosted');
CREATE UNIQUE INDEX agents_actor_entity_token ON agents(actor, entity_token);
COMMIT;
