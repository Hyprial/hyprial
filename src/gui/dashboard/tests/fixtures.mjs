// Deterministic test data. Not imported by the production entry or server.
export const FIXTURES = {
  processes: {
    ok: true,
    daemon: {
      running: true,
      pid: 1200,
      nodeId: "taipei-01",
      owner: "hyprial",
      epoch: "demo-epoch",
    },
    connectors: [
      {
        id: "worker-1",
        name: "builder",
        runtime: "headless",
        running: true,
        pid: 1201,
      },
    ],
  },
  hosts: {
    ok: true,
    hosts: [
      { nodeId: "taipei-01", status: "online" },
      { nodeId: "singapore-02", status: "online" },
      { nodeId: "tokyo-03", status: "online" },
      { nodeId: "osaka-04", status: "offline" },
    ],
  },
  agents: {
    ok: true,
    agents: Array.from({ length: 18 }, (_, i) => ({
      uri: `agent:hyprial:taipei-01:builder-${i}`,
      actor: `builder-${i}`,
      owner: "hyprial",
      machine: "taipei-01",
      provider: "deepseek",
      model: "deepseek",
      status: i === 2 ? "offline" : "online",
      runtime: "headless",
      secret: "DO-NOT-EXPOSE",
    })),
  },
  topology: {
    ok: true,
    actors: [
      {
        actor: "agent:hyprial:taipei-01:builder-0",
        name: "builder-0",
        runtime: "headless",
        status: "online",
        state: "busy",
        running: true,
        pendingCount: 2,
        turnCount: 14,
      },
      {
        actor: "agent:hyprial:taipei-01:reviewer",
        name: "reviewer",
        runtime: "interactive",
        status: "online",
        state: "idle",
        running: null,
      },
    ],
  },
  workflows: {
    ok: true,
    runs: [
      {
        runId: "run-001",
        name: "release-verification",
        state: "running",
        sender: "agent:hyprial:taipei-01:builder-0",
        report: "PRIVATE REPORT",
      },
      { runId: "run-002", name: "nightly-integration", state: "completed" },
      { runId: "run-003", name: "documentation-sync", state: "completed" },
      { runId: "run-004", name: "adapter-healthcheck", state: "failed" },
    ],
  },
  routines: {
    ok: true,
    routines: [
      {
        name: "daily-review",
        owner: "hyprial",
        enabled: true,
        nextDueMs: 1788960000000,
        sourceErrorStreak: 0,
        inFlight: [],
      },
    ],
  },
  outbox: {
    ok: true,
    entries: [
      {
        messageId: "msg-001",
        sender: "agent:hyprial:taipei-01:builder-0",
        recipient: "agent:hyprial:tokyo-03:reviewer",
        state: "pending",
        body: "PRIVATE MESSAGE",
      },
    ],
  },
  adapters: {
    ok: true,
    adapters: [
      {
        name: "team-lark",
        provider: "lark",
        status: "online",
        online: true,
        configured: true,
        desired: true,
        processRunning: true,
        secret: "APP-SECRET",
      },
      {
        name: "engineering-lark",
        provider: "lark",
        status: "online",
        online: true,
        configured: true,
        desired: true,
        processRunning: true,
      },
    ],
  },
  doctor: {
    ok: true,
    checks: [
      { name: "daemon", status: "ok" },
      { name: "zenoh", status: "ok" },
      {
        name: "historical-inbox",
        status: "warn",
        detail: "PRIVATE DIAGNOSTIC",
        action: { command: "h2b init" },
      },
    ],
  },
  version: { ok: true, localVersion: "0.4.4" },
  service: {
    ok: true,
    mode: "standalone",
    installed: false,
    running: true,
    pid: 1200,
  },
  organization: {
    ok: true,
    status: "accepted",
    adoptedAt: "2026-09-08T02:00:00Z",
    meta: { version: "2026.09", publisher: "hyprial" },
  },
  autoupdate: {
    ok: true,
    enabled: true,
    installed: false,
    trigger: "daemon",
    schedule: [{ hour: 3, minute: 17 }],
    scheduler: { nextRunAt: "2026-09-10T03:17:00+08:00" },
  },
};
