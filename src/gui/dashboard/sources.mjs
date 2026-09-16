// This service deliberately does not import the DSH Host or session bridge.
// All commands are fixed read paths. org status is excluded: older versions
// initialize directories while reading it. No plan/preview operations here.
const spec = (label, section, args, ttl = 10000, scope = "本机") =>
  Object.freeze({
    label,
    section,
    argv: Object.freeze([...args, "--json"]),
    ttl,
    scope,
  });
export const SOURCES = Object.freeze({
  processes: spec("Daemon 与进程", "overview", ["ps"]),
  hosts: spec("网络节点", "agents", ["hosts"], 10000, "Mesh 节点目录"),
  agents: spec("Agent 身份", "agents", ["agent", "list"]),
  topology: spec("Agent 运行状态", "agents", ["top"]),
  workflows: spec(
    "Workflow",
    "runs",
    ["workflow", "list", "--limit", "50"],
    15000,
    "本机 · 最近 50 条",
  ),
  routines: spec("Routine", "runs", ["routine", "list"], 15000),
  outbox: spec("待投递", "delivery", ["outbox", "list"]),
  adapters: spec("Adapter", "integrations", ["adapter", "list"]),
  doctor: spec("健康检查", "overview", ["doctor"], 60000),
  version: spec("H2B 版本", "system", ["version"], 60000),
  service: spec("服务状态", "system", ["service"], 60000),
  organization: spec("已采纳组织", "system", ["org", "show"], 60000),
  autoupdate: spec("更新计划", "system", ["autoupdate", "status"], 60000),
});

const object = (value) =>
  value && typeof value === "object" && !Array.isArray(value);
const scalar = (value) =>
  typeof value === "string"
    ? value.slice(0, 256)
    : typeof value === "boolean" ||
        (typeof value === "number" && Number.isFinite(value))
      ? value
      : null;
const pick = (value, keys) =>
  Object.fromEntries(keys.map((key) => [key, scalar(value?.[key])]));
const invalid = () => {
  throw Object.assign(new Error("CLI 状态结构无法识别"), {
    code: "INVALID_RESPONSE",
  });
};
function rows(document, key, fields) {
  if (
    !Array.isArray(document[key]) ||
    document[key].some((row) => !object(row))
  )
    invalid();
  return {
    items: document[key].slice(0, 500).map((row) => pick(row, fields)),
    total: document[key].length,
    truncated: document[key].length > 500,
  };
}

// Positive field selection is the API contract. Never return raw CLI JSON,
// stderr, prompts, reports, command lines, working directories or credentials.
export function projectSource(operation, document) {
  if (!object(document) || document.ok === false) invalid();
  switch (operation) {
    case "processes":
      if (
        !object(document.daemon) ||
        typeof document.daemon.running !== "boolean"
      )
        invalid();
      return {
        daemon: pick(document.daemon, [
          "running",
          "pid",
          "nodeId",
          "owner",
          "epoch",
          "phase",
        ]),
        restoring:
          document.restoring === true || document.restorePending === true,
        connectors: rows(document, "connectors", [
          "id",
          "name",
          "runtime",
          "running",
          "pid",
        ]).items,
        outboxCount: scalar(document.outboxCount),
      };
    case "hosts":
      return rows(document, "hosts", ["nodeId", "status"]);
    case "agents":
      return rows(document, "agents", [
        "uri",
        "actor",
        "owner",
        "machine",
        "provider",
        "model",
        "status",
        "runtime",
        "harness",
      ]);
    case "topology":
      return rows(document, "actors", [
        "actor",
        "name",
        "runtime",
        "status",
        "state",
        "processState",
        "running",
        "pid",
        "turnCount",
        "pendingCount",
        "openTurnStartedAtMs",
        "lastTurnEndedAtMs",
      ]);
    case "workflows":
      return rows(document, "runs", ["runId", "name", "state", "sender"]);
    case "routines": {
      const result = rows(document, "routines", [
        "name",
        "owner",
        "enabled",
        "nextDueMs",
        "sourceErrorStreak",
      ]);
      result.items.forEach((row, i) => {
        row.inFlightCount = Array.isArray(document.routines[i].inFlight)
          ? document.routines[i].inFlight.length
          : null;
      });
      return result;
    }
    case "outbox": {
      const result = rows(document, "entries", [
        "messageId",
        "id",
        "sender",
        "recipient",
        "to",
        "state",
        "status",
        "createdAtMs",
        "expiresAtMs",
        "nextAttemptMs",
        "attempts",
        "undeliverableScheme",
      ]);
      result.items.forEach((row) => {
        row.state ||=
          row.status ||
          (row.undeliverableScheme === true ? "undeliverable" : "pending");
      });
      return result;
    }
    case "adapters":
      return rows(document, "adapters", [
        "id",
        "name",
        "provider",
        "status",
        "online",
        "configured",
        "desired",
        "processRunning",
        "pid",
      ]);
    case "doctor":
      return rows(document, "checks", ["name", "status"]);
    case "version":
      if (
        typeof (
          document.localVersion ??
          document.packageVersion ??
          document.version
        ) !== "string"
      )
        invalid();
      return {
        version: scalar(
          document.localVersion ?? document.packageVersion ?? document.version,
        ),
      };
    case "service":
      if (typeof document.running !== "boolean") invalid();
      return pick(document, ["mode", "installed", "running", "pid"]);
    case "organization":
      if (!["absent", "accepted"].includes(document.status)) invalid();
      return {
        ...pick(document, ["status", "adoptedAt"]),
        meta: pick(document.meta, ["version", "publisher"]),
        pendingCount: null,
      };
    case "autoupdate":
      if (typeof document.enabled !== "boolean") invalid();
      return {
        ...pick(document, [
          "enabled",
          "installed",
          "loaded",
          "unit",
          "trigger",
        ]),
        schedule: Array.isArray(document.schedule)
          ? document.schedule
              .slice(0, 20)
              .map((row) => pick(row, ["hour", "minute"]))
          : [],
        nextRunAt: scalar(document.scheduler?.nextRunAt),
      };
    default:
      throw Object.assign(new Error("不支持此查询"), {
        code: "UNKNOWN_SOURCE",
      });
  }
}
