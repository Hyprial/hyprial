#!/usr/bin/env node

/**
 * Minimal DSH Web <-> H2B session bridge.
 *
 * The only supported process interface is `node h2b-session-bridge.mjs rpc`.
 * A single JSON request is read from stdin and a single JSON response is
 * written to stdout.  Session ids, targets, and message bodies therefore
 * never become process arguments.
 */

import { createHash, randomUUID } from "node:crypto";
import { mkdir, open, readFile, stat, unlink } from "node:fs/promises";
import net from "node:net";
import os from "node:os";
import path from "node:path";
import process from "node:process";
import { fileURLToPath } from "node:url";
import { createAgentTaskClient } from "./integration/agent-task-client.js";
import { readSessionLedger, writeSessionLedger, mergeLegacyLedger } from "./integration/session-ledger.js";

import { chatKeyOwner, chatKeySession, chatWorkSessions, consolidateDirectChats, directChatIndex } from "./integration/direct-chat-index.js";

const IPC_VERSION = 1;
const MAX_DOCUMENT_BYTES = 1024 * 1024;
const MAX_DAEMON_BYTES = 8 * 1024 * 1024;
const DEFAULT_TIMEOUT_MS = 5_000;
const MAX_SESSION_PARTICIPANTS = 3;
const SOURCE = "dsh-cordis-demo";
const RUNTIME = "dsh_interactive";

class BridgeError extends Error {
  constructor(code, message, details) {
    super(message);
    this.code = code;
    if (details !== undefined) this.details = details;
  }
}

const AGENT_TASK_OPERATIONS = new Set([
  "agent.task.capabilities",
  "agent.task.start",
  "agent.task.status",
  "agent.task.result",
  "agent.task.cancel",
  "agent.task.observe",
]);
const AGENT_TASK_IDENTITY_FIELDS = new Set([
  "actor", "from", "sender", "caller", "owner", "sessionRef",
  "serviceActor", "serviceActorUri", "serviceIdentity", "coordinatorActor",
]);
const MFU_WORKFLOW_OPERATIONS = new Set([
  "workflow.capabilities",
  "workflow.start",
  "workflow.status",
  "workflow.result",
  "workflow.cancel",
]);

function requiredString(value, label, maxLength = 1024) {
  if (typeof value !== "string" || value.length === 0) {
    throw new BridgeError("INVALID_ARGUMENT", `${label} must be a non-empty string`);
  }
  if (value.length > maxLength || value.includes("\0")) {
    throw new BridgeError("INVALID_ARGUMENT", `${label} is invalid or too long`);
  }
  return value;
}

function sessionIdentity(sessionId, daemon, kind = "web", actorNameOverride = "") {
  const owner = requiredString(daemon?.owner, "daemon owner", 256);
  const nodeId = requiredString(daemon?.nodeId, "daemon nodeId", 256);
  const digest = createHash("sha256").update(sessionId, "utf8").digest("hex").slice(0, 8);
  const remote = kind === "remote";
  const name = actorNameOverride || (remote ? `dsh-session-${digest}` : `dsh-web-${digest}`);
  return {
    actor: `agent:${owner}:${nodeId}:${name}`,
    actorName: name,
    sessionRef: `${remote ? "dsh-remote" : "dsh-web"}:${sessionId}`,
  };
}

function daemonSocketPath(env = process.env) {
  // An explicit socket is the strongest operator override. This matters in
  // isolated test/SSH-forwarded environments that also happen to set H2B_HOME.
  if (env.HARNESS_SOCKET_PATH?.trim()) return env.HARNESS_SOCKET_PATH.trim();
  if (env.HARNESS_STATE_DIR?.trim()) {
    return path.join(env.HARNESS_STATE_DIR.trim(), "daemon.sock");
  }
  if (env.H2B_HOME?.trim()) {
    return path.join(env.H2B_HOME.trim(), "state", "daemon.sock");
  }
  return path.join(env.HOME || os.homedir(), ".h2b", "state", "daemon.sock");
}

function ledgerPath(env = process.env) {
  if (env.H2B_DSH_DEMO_LEDGER?.trim()) return env.H2B_DSH_DEMO_LEDGER.trim();
  if (env.HARNESS_STATE_DIR?.trim()) {
    return path.join(env.HARNESS_STATE_DIR.trim(), "dsh-web-injected.json");
  }
  if (env.H2B_HOME?.trim()) {
    return path.join(env.H2B_HOME.trim(), "state", "dsh-web-injected.json");
  }
  return path.join(env.HOME || os.homedir(), ".h2b", "state", "dsh-web-injected.json");
}

function canonicalPrincipal(value) {
  if (typeof value !== "string") return false;
  const parts = value.split(":");
  return (
    (parts.length === 2 && parts[0] === "user" && parts[1].length > 0) ||
    (parts.length === 3 && ["adapter", "channel"].includes(parts[0]) && parts[1] === "lark" && parts[2].length > 0) ||
    (parts.length === 4 && ["agent", "channel"].includes(parts[0]) && parts.slice(1).every(Boolean))
  );
}

function canonicalAgentPrincipal(value) {
  if (typeof value !== "string") return false;
  const parts = value.split(":");
  return parts.length === 4 && parts[0] === "agent" && parts.slice(1).every(Boolean);
}

function allowlist(env = process.env) {
  const raw = env.H2B_DSH_DEMO_ALLOW_FROM?.trim();
  if (!raw) return new Set();
  const entries = raw.split(",").map((item) => item.trim()).filter(Boolean);
  const invalid = entries.find((item) => !canonicalPrincipal(item));
  if (invalid) {
    throw new BridgeError(
      "INVALID_ALLOWLIST",
      "H2B_DSH_DEMO_ALLOW_FROM accepts only exact canonical agent: or user: identities",
    );
  }
  return new Set(entries);
}

function daemonRequest(method, params = {}, { mutation = false, env = process.env } = {}) {
  return new Promise((resolve, reject) => {
    const socket = net.createConnection(daemonSocketPath(env));
    // Every daemon request gets an envelope id. Mutations additionally rely on
    // it as their idempotency key, while reads still benefit from correlation.
    const requestId = randomUUID();
    const frame = { version: IPC_VERSION, id: requestId, method, params };
    const chunks = [];
    let size = 0;
    let settled = false;
    const configuredTimeout = Number(env.H2B_DSH_DEMO_IPC_TIMEOUT_MS);
    const timeoutMs = Number.isFinite(configuredTimeout) && configuredTimeout > 0
      ? Math.min(configuredTimeout, 60_000)
      : DEFAULT_TIMEOUT_MS;
    const timer = setTimeout(() => {
      finish(() => reject(new BridgeError("IPC_TIMEOUT", `daemon ${method} timed out`)));
    }, timeoutMs);

    function finish(action) {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      socket.destroy();
      action();
    }

    socket.on("connect", () => socket.write(`${JSON.stringify(frame)}\n`));
    socket.on("data", (chunk) => {
      size += chunk.length;
      if (size > MAX_DAEMON_BYTES) {
        finish(() => reject(new BridgeError("IPC_RESPONSE_TOO_LARGE", "daemon response exceeded 8 MiB")));
        return;
      }
      chunks.push(chunk);
      const buffer = Buffer.concat(chunks, size);
      const newline = buffer.indexOf(0x0a);
      if (newline < 0) return;
      let document;
      try {
        document = JSON.parse(buffer.subarray(0, newline).toString("utf8"));
      } catch {
        finish(() => reject(new BridgeError("INVALID_DAEMON_RESPONSE", "daemon returned invalid JSON")));
        return;
      }
      if (document?.version !== IPC_VERSION || document.id !== requestId) {
        finish(() => reject(new BridgeError("INVALID_DAEMON_RESPONSE", "daemon returned a mismatched IPC envelope")));
        return;
      }
      if (document.error && typeof document.error === "object") {
        const code = String(document.error.code || "DAEMON_ERROR");
        const message = String(document.error.message || "daemon request failed");
        finish(() => reject(new BridgeError(code, message, document.error.data)));
        return;
      }
      if (!document.result || typeof document.result !== "object" || Array.isArray(document.result)) {
        finish(() => reject(new BridgeError("INVALID_DAEMON_RESPONSE", "daemon result must be an object")));
        return;
      }
      finish(() => resolve(document.result));
    });
    socket.on("error", (error) => {
      finish(() => reject(new BridgeError("DAEMON_UNAVAILABLE", `H2B daemon is not reachable: ${error.message}`)));
    });
    socket.on("end", () => {
      finish(() => reject(new BridgeError("DAEMON_DISCONNECTED", "daemon closed before responding")));
    });
  });
}

async function identityFor(sessionId, env = process.env, kind = "web") {
  requiredString(sessionId, "sessionId", 4096);
  const status = await daemonRequest("ps", {}, { env });
  let actorName = "";
  if (kind === "remote") {
    const ledger = await readLedger(ledgerPath(env));
    const configured = ledger.remoteNames[sessionId];
    if (typeof configured === "string" && /^dsh-[a-z0-9](?:[a-z0-9-]{0,46}[a-z0-9])?$/.test(configured)) actorName = configured;
  }
  return sessionIdentity(sessionId, status.daemon, kind, actorName);
}

function ledgerKey(identity) {
  return `${identity.actor}\n${identity.sessionRef}`;
}

async function canonicalSessionIdentityFor(sessionId, env = process.env) {
  requiredString(sessionId, "sessionId", 4096);
  const ledger = await readLedger(ledgerPath(env));
  const bindings = Object.values(ledger.remoteBindings).filter(binding => binding.sessionId === sessionId);
  const configured = ledger.remoteNames[sessionId];
  if (configured && (typeof configured !== "string" || !/^dsh-[a-z0-9](?:[a-z0-9-]{0,46}[a-z0-9])?$/.test(configured))) {
    throw new BridgeError("REMOTE_BINDING_MISMATCH", "saved session network name is invalid");
  }
  const kind = bindings.length || configured ? "remote" : "web";
  const status = await daemonRequest("ps", {}, { env });
  const identity = sessionIdentity(sessionId, status.daemon, kind, configured || "");
  if (bindings.some(binding => binding.actor !== identity.actor || binding.sessionRef !== identity.sessionRef)) {
    throw new BridgeError("REMOTE_BINDING_MISMATCH", "saved binding does not match session identity");
  }
  return identity;
}

async function readLedger(file) {
  try {
    const document = await readSessionLedger(file);
    if (document?.version !== 1 || typeof document.sessions !== "object" || !document.sessions) {
      throw new Error("unexpected ledger schema");
    }
    if (document.participants !== undefined && (
      typeof document.participants !== "object" ||
      !document.participants ||
      Array.isArray(document.participants)
    )) {
      throw new Error("unexpected participants schema");
    }
    if (document.humanChats !== undefined && (
      typeof document.humanChats !== "object" ||
      !document.humanChats ||
      Array.isArray(document.humanChats)
    )) {
      throw new Error("unexpected humanChats schema");
    }
    if (document.remoteBindings !== undefined && (
      typeof document.remoteBindings !== "object" ||
      !document.remoteBindings ||
      Array.isArray(document.remoteBindings)
    )) {
      throw new Error("unexpected remoteBindings schema");
    }
    if (document.remoteNames !== undefined && (
      typeof document.remoteNames !== "object" || !document.remoteNames || Array.isArray(document.remoteNames)
    )) {
      throw new Error("unexpected remoteNames schema");
    }
    return {
      ...document,
      participants: document.participants || {},
      humanChats: document.humanChats || {},
      remoteBindings: document.remoteBindings || {},
      remoteNames: document.remoteNames || {},
    };
  } catch (error) {
    if (error?.code === "ENOENT") {
      return { version: 1, sessions: {}, participants: {}, humanChats: {}, remoteBindings: {}, remoteNames: {} };
    }
    throw new BridgeError("INVALID_LEDGER", `cannot read injected ledger: ${error.message}`);
  }
}

function remoteAdapter(value) {
  const adapter = requiredString(value, "adapter", 128);
  if (!/^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/.test(adapter)) {
    throw new BridgeError("INVALID_ADAPTER", "adapter contains unsupported characters");
  }
  return adapter;
}

function remoteBroadcastMode(value) {
  if (!["off", "assistant", "full"].includes(value)) {
    throw new BridgeError("INVALID_BROADCAST_MODE", "broadcast mode must be off, assistant, or full");
  }
  return value;
}

function remoteEntryName(value) {
  if (value === "") return "";
  const name = requiredString(value, "entryName", 48).toLowerCase();
  if (!/^[a-z0-9](?:[a-z0-9-]{0,46}[a-z0-9])?$/.test(name)) {
    throw new BridgeError("INVALID_REMOTE_NAME", "entry name accepts lowercase letters, numbers, and internal hyphens only");
  }
  return `dsh-${name}`;
}

async function updateRemoteName(file, sessionId, actorName) {
  const release = await acquireLedgerLock(file);
  try {
    const ledger = await readLedger(file);
    if (actorName) ledger.remoteNames[sessionId] = actorName;
    else delete ledger.remoteNames[sessionId];
    await writeSessionLedger(file, ledger);
  } finally {
    await release();
  }
}

function remoteRoute(value, adapter) {
  const route = requiredString(value, "route", 2048);
  if (!route.startsWith(`route:${adapter}:`) || route.split(":").length < 3) {
    throw new BridgeError("INVALID_BROADCAST_ROUTE", "route must belong to the bound Lark adapter");
  }
  return route;
}

async function updateRemoteBinding(file, adapter, update) {
  const release = await acquireLedgerLock(file);
  try {
    const ledger = await readLedger(file);
    const current = ledger.remoteBindings[adapter];
    const next = update(current && typeof current === "object" ? current : null);
    if (next === null) delete ledger.remoteBindings[adapter];
    else ledger.remoteBindings[adapter] = next;
    await writeSessionLedger(file, ledger);
    return next;
  } finally {
    await release();
  }
}

async function remoteBindings(file) {
  const ledger = await readLedger(file);
  return Object.entries(ledger.remoteBindings).flatMap(([adapter, raw]) => {
    if (!raw || typeof raw !== "object") return [];
    if (typeof raw.sessionId !== "string" || !raw.sessionId || raw.sessionId.length > 4096) return [];
    if (!canonicalAgentPrincipal(raw.actor) || typeof raw.sessionRef !== "string" || !raw.sessionRef) return [];
    const broadcastMode = ["assistant", "full"].includes(raw.broadcastMode) ? raw.broadcastMode : "off";
    const broadcastRoute = typeof raw.broadcastRoute === "string" && raw.broadcastRoute.startsWith(`route:${adapter}:`)
      ? raw.broadcastRoute : "";
    return [{ adapter, sessionId: raw.sessionId, actor: raw.actor, sessionRef: raw.sessionRef, boundAt: boundedTimestamp(raw.boundAt), broadcastMode, broadcastRoute }];
  });
}

async function requireRemoteBinding(file, adapter, sessionId, identity) {
  const binding = (await remoteBindings(file)).find((item) => item.adapter === adapter);
  if (!binding || binding.sessionId !== sessionId || binding.actor !== identity.actor) {
    throw new BridgeError("REMOTE_BINDING_MISMATCH", "adapter is not bound to this DSH work session");
  }
  return binding;
}

function remoteAdapterPrincipals(adapter, identity) {
  const [, owner, nodeId] = identity.actor.split(":");
  return new Set([
    `adapter:lark:${adapter}`,
    `channel:lark:${adapter}`,
    `channel:${owner}:${nodeId}:${adapter}`,
  ]);
}

async function acquireLedgerLock(file) {
  const lock = `${file}.lock`;
  await mkdir(path.dirname(file), { recursive: true, mode: 0o700 });
  const deadline = Date.now() + 2_000;
  while (true) {
    try {
      const handle = await open(lock, "wx", 0o600);
      return async () => {
        await handle.close();
        await unlink(lock).catch(() => {});
      };
    } catch (error) {
      if (error?.code !== "EEXIST") throw error;
      try {
        const info = await stat(lock);
        if (Date.now() - info.mtimeMs > 10_000) {
          await unlink(lock).catch(() => {});
          continue;
        }
      } catch (statError) {
        if (statError?.code === "ENOENT") continue;
        throw statError;
      }
      if (Date.now() >= deadline) {
        throw new BridgeError("LEDGER_BUSY", "injected ledger is locked by another bridge call");
      }
      await new Promise((resolve) => setTimeout(resolve, 20));
    }
  }
}

async function recordInjected(file, identity, deliveryId) {
  const release = await acquireLedgerLock(file);
  try {
    const ledger = await readLedger(file);
    const key = ledgerKey(identity);
    const current = Array.isArray(ledger.sessions[key]) ? ledger.sessions[key] : [];
    if (current.includes(deliveryId)) return false;
    // Keep the demo ledger bounded while retaining enough history for refreshes.
    ledger.sessions[key] = [...current, deliveryId].slice(-4096);
    await writeSessionLedger(file, ledger);
    return true;
  } finally {
    await release();
  }
}

async function updateParticipants(file, identity, update, { maxEntries } = {}) {
  const release = await acquireLedgerLock(file);
  try {
    const ledger = await readLedger(file);
    const key = ledgerKey(identity);
    const current = Array.isArray(ledger.participants[key])
      ? ledger.participants[key].filter(canonicalAgentPrincipal)
      : [];
    const next = [...new Set(update(current))].filter(canonicalAgentPrincipal).sort();
    if (maxEntries !== undefined && next.length > maxEntries) {
      throw new BridgeError(
        "PARTICIPANT_LIMIT_EXCEEDED",
        `a DSH Web session may authorize at most ${maxEntries} H2B participants`,
      );
    }
    if (next.length > 0) ledger.participants[key] = next;
    else delete ledger.participants[key];
    await writeSessionLedger(file, ledger);
    return next;
  } finally {
    await release();
  }
}

async function participantSet(file, identity) {
  const ledger = await readLedger(file);
  const values = ledger.participants[ledgerKey(identity)];
  return new Set(Array.isArray(values) ? values.filter(canonicalAgentPrincipal) : []);
}

function boundedTimestamp(value) {
  return Number.isFinite(value) && value >= 0 ? Math.floor(value) : 0;
}

function normalizeChatMessage(value) {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  if (typeof value.id !== "string" || !value.id || value.id.length > 4096) return null;
  if (value.direction !== "inbound" && value.direction !== "outbound") return null;
  const sender = typeof value.sender === "string" ? value.sender.slice(0, 2048) : "";
  const message = typeof value.message === "string" ? value.message.slice(0, 16 * 1024) : "";
  const result = { id: value.id, direction: value.direction, sender, message, time: boundedTimestamp(value.time) };
  // Optional IDs are opaque and must not be truncated into another identity.
  // Legacy messages retain their original shape. Discussion tags are display
  // metadata only: routing/association must use the workbench's persisted map.
  const boundedId = (id, max = 4096) => typeof id === "string" && id.trim().length > 0 && id.length <= max && !id.includes("\0");
  for (const key of ["messageId", "deliveryId", "conversationId", "replyTo"]) {
    if (boundedId(value[key])) result[key] = value[key];
  }
  const tag = value.discussion;
  if (tag && typeof tag === "object" && !Array.isArray(tag) &&
      typeof tag.workflowId === "string" && /^wf-[a-f0-9-]{36}$/.test(tag.workflowId) &&
      boundedId(tag.runId, 128) && boundedId(tag.target, 2048)) {
    result.discussion = { workflowId: tag.workflowId, runId: tag.runId, target: tag.target };
  }
  return result;
}

function mergeChatHistory(current, incoming) {
  const messages = new Map(current.map(message => [message.id, message]));
  for (const message of incoming) {
    const previous = messages.get(message.id);
    if (!previous) { messages.set(message.id, message); continue; }
    // An optimistic local append may precede the network receipt. Enrich that
    // same entry without overwriting its body or an already-recorded ID.
    const enriched = { ...previous };
    for (const key of ["messageId", "deliveryId", "conversationId", "replyTo", "discussion"]) {
      if (enriched[key] === undefined && message[key] !== undefined) enriched[key] = message[key];
    }
    messages.set(message.id, enriched);
  }
  return [...messages.values()].slice(-100);
}

function normalizeHumanChat(value) {
  if (
    !value ||
    typeof value !== "object" ||
    Array.isArray(value) ||
    !canonicalAgentPrincipal(value.target) ||
    typeof value.label !== "string" ||
    value.label.length === 0 ||
    value.label.length > 120
  ) return null;
  const messages = Array.isArray(value.messages)
    ? value.messages.map(normalizeChatMessage).filter(Boolean).slice(-100)
    : [];
  return {
    target: value.target,
    label: value.label,
    messages,
    workSessionId: chatWorkSessions(value)[0] || "",
    workSessionIds: chatWorkSessions(value),
    createdAt: boundedTimestamp(value.createdAt),
    lastOpenedAt: boundedTimestamp(value.lastOpenedAt),
  };
}

async function humanChatBinding(file, identity) {
  const ledger = await readLedger(file);
  const value = ledger.humanChats[ledgerKey(identity)];
  return normalizeHumanChat(value);
}

async function updateHumanChat(file, identity, update) {
  const release = await acquireLedgerLock(file);
  try {
    const ledger = await readLedger(file);
    const key = ledgerKey(identity);
    if (consolidateDirectChats(ledger, chatKeyOwner(key))) await writeSessionLedger(file, ledger);
    const redirected = ledger.humanChatAliases?.[key];
    if (redirected) throw new BridgeError("CHAT_EXISTS", "This Agent already has a direct chat", { sessionId: chatKeySession(redirected) });
    const current = normalizeHumanChat(ledger.humanChats[key]);
    const value = update(current, ledger, key);
    if (value === null) delete ledger.humanChats[key];
    else {
      const normalized = normalizeHumanChat(value);
      if (!normalized) throw new BridgeError("INVALID_CHAT_STATE", "chat state is invalid");
      ledger.humanChats[key] = normalized;
    }
    await writeSessionLedger(file, ledger);
    return value === null ? null : normalizeHumanChat(value);
  } finally {
    await release();
  }
}

async function injectedSet(file, identity) {
  const ledger = await readLedger(file);
  const values = ledger.sessions[ledgerKey(identity)];
  return new Set(Array.isArray(values) ? values.filter((item) => typeof item === "string") : []);
}

function fenced(identity, params = {}) {
  return { actor: identity.actor, sessionRef: identity.sessionRef, ...params };
}

async function pendingFor(identity, env, { allowedPrincipals = new Set() } = {}) {
  const response = await daemonRequest("message.pending.list", fenced(identity), { env });
  const messages = Array.isArray(response.messages) ? response.messages : [];
  const allowedFrom = allowlist(env);
  for (const principal of allowedPrincipals) allowedFrom.add(principal);
  const participants = await participantSet(ledgerPath(env), identity);
  for (const participant of participants) allowedFrom.add(participant);
  const injected = await injectedSet(ledgerPath(env), identity);
  const allowed = [];
  let deniedCount = 0;
  for (const raw of messages) {
    if (!raw || typeof raw !== "object") continue;
    if (!canonicalPrincipal(raw.from) || !allowedFrom.has(raw.from)) {
      deniedCount += 1;
      continue;
    }
    const deliveryId = typeof raw.deliveryId === "string" && raw.deliveryId
      ? raw.deliveryId
      : raw.messageId;
    if (typeof deliveryId !== "string" || !deliveryId) continue;
    allowed.push({ ...raw, deliveryId, sourceSessionId: chatKeySession(ledgerKey(identity)), injected: injected.has(deliveryId) });
  }
  const message = allowed.find((item) => !item.injected) ?? null;
  return {
    ok: true,
    actor: identity.actor,
    sessionRef: identity.sessionRef,
    message,
    messages: allowed,
    deniedCount,
    injectedCount: allowed.length - allowed.filter((item) => !item.injected).length,
  };
}

async function migrateLegacyState(env = process.env, { apply = false, legacyFile = fileURLToPath(new URL('./.dsh-h2b-state/dsh-web-injected.json', import.meta.url)) } = {}) {
  const file = ledgerPath(env);
  if (path.resolve(file) === path.resolve(legacyFile)) throw new BridgeError('INVALID_LEDGER_PATH', 'legacy and durable ledger must be different files');
  const plan = async () => {
    const ledger = await readLedger(file);
    if (ledger.legacyMigrationComplete === true) return { status: 'already-migrated', ledger };
    let text;
    try { text = await readFile(legacyFile, 'utf8'); }
    catch (error) { if (error.code === 'ENOENT') return { status: 'legacy-absent', ledger }; throw error; }
    const merged = mergeLegacyLedger(ledger, JSON.parse(text));
    return { status: 'migration-ready', ledger: merged, sourceDigest: createHash('sha256').update(text).digest('hex') };
  };
  if (!apply) { const result = await plan(); return { ok: true, file, legacyFile, status: result.status, sourceDigest: result.sourceDigest, applied: false }; }
  const release = await acquireLedgerLock(file);
  try {
    const result = await plan();
    if (result.status === 'migration-ready') {
      result.ledger.legacyMigrationComplete = true;
      result.ledger.legacyMigration = { source: legacyFile, sourceDigest: result.sourceDigest, at: Date.now() };
      await writeSessionLedger(file, result.ledger);
    }
    return { ok: true, file, legacyFile, status: result.status, applied: result.status === 'migration-ready' };
  } finally { await release(); }
}

async function recoverPinnedBindings(env = process.env, { apply = false } = {}) {
  const file = ledgerPath(env);
  // A pin alone cannot identify a DSH session. Require the registry's exact
  // session reference and current local owner/node; never infer from titles.
  const [pins, directory, status] = await Promise.all([
    daemonRequest('adapter.pins', {}, { env }),
    daemonRequest('agent.list', {}, { env }),
    daemonRequest('ps', {}, { env }),
  ]);
  if (!pins.pins || typeof pins.pins !== 'object' || Array.isArray(pins.pins) || !Array.isArray(directory.agents)) {
    throw new BridgeError('REMOTE_RECOVERY_UNAVAILABLE', 'H2B did not return authoritative pins and agent registry for binding recovery');
  }
  const release = apply ? await acquireLedgerLock(file) : async () => {};
  try {
    const ledger = await readLedger(file);
    const entries = [];
    for (const [adapter, actor] of Object.entries(pins.pins)) {
      remoteAdapter(adapter);
      const candidates = directory.agents.filter(item => item.uri === actor && item.preferredHarness === 'dsh');
      if (candidates.length !== 1) { entries.push({ adapter, actor, status: 'unresolved-registry' }); continue; }
      const ref = candidates[0].lastSessionId;
      if (typeof ref !== 'string' || !ref.startsWith('dsh-remote:session-')) { entries.push({ adapter, actor, status: 'unresolved-session' }); continue; }
      const sessionId = ref.slice('dsh-remote:'.length);
      const actorName = typeof actor === 'string' ? actor.split(':')[3] : '';
      if (!/^dsh-[a-z0-9](?:[a-z0-9-]{0,46}[a-z0-9])?$/.test(actorName)) { entries.push({ adapter, actor, status: 'unsupported-actor' }); continue; }
      const identity = sessionIdentity(sessionId, status.daemon, 'remote', actorName);
      if (actor !== identity.actor) { entries.push({ adapter, actor, status: 'foreign-identity' }); continue; }
      const existing = ledger.remoteBindings[adapter];
      if ((existing && (existing.sessionId !== sessionId || existing.actor !== actor)) ||
          (ledger.remoteNames[sessionId] && ledger.remoteNames[sessionId] !== actorName)) {
        entries.push({ adapter, actor, sessionId, status: 'conflict' });
        continue;
      }
      if (existing && ledger.remoteNames[sessionId] === actorName) {
        entries.push({ adapter, actor, sessionId, status: 'already-consistent' }); continue;
      }
      ledger.remoteNames[sessionId] = actorName;
      ledger.remoteBindings[adapter] = existing || {
        sessionId, actor, sessionRef: ref, boundAt: Date.now(),
        broadcastMode: 'off', broadcastRoute: '', recoveredFrom: 'h2b-pin-and-agent-registry',
      };
      entries.push({ adapter, actor, sessionId, status: 'recovery-ready' });
    }
    const changed = entries.some(item => item.status === 'recovery-ready');
    const conflict = entries.some(item => item.status === 'conflict');
    if (apply && conflict) throw new BridgeError('REMOTE_RECOVERY_CONFLICT', 'H2B pin/registry conflicts with saved DSH binding; no changes applied', { entries });
    if (apply && changed) {
      const latest = await daemonRequest('adapter.pins', {}, { env });
      if (entries.some(item => item.status === 'recovery-ready' && latest.pins?.[item.adapter] !== item.actor)) {
        throw new BridgeError('REMOTE_BINDING_CHANGED', 'H2B pin changed during recovery; no changes applied');
      }
      ledger.recoveryAudit = [...(Array.isArray(ledger.recoveryAudit) ? ledger.recoveryAudit : []), { at: Date.now(), entries }].slice(-100);
      await writeSessionLedger(file, ledger);
    }
    return { ok: !conflict, file, applied: apply && changed, entries };
  } finally { await release(); }
}

async function handleRpc(request, env = process.env) {
  if (!request || typeof request !== "object" || Array.isArray(request)) {
    throw new BridgeError("INVALID_ARGUMENT", "RPC request must be an object");
  }
  const operation = requiredString(request.operation, "operation", 64);
  if (operation === "remote-bindings") {
    return { ok: true, bindings: await remoteBindings(ledgerPath(env)) };
  }
  if (operation === "chat-list") {
    const status = await daemonRequest("ps", {}, { env });
    const owner = requiredString(status.daemon?.owner, "daemon owner", 256);
    const file = ledgerPath(env);
    const release = await acquireLedgerLock(file);
    try {
      const ledger = await readLedger(file);
      if (consolidateDirectChats(ledger, owner)) await writeSessionLedger(file, ledger);
      return { ok: true, chats: directChatIndex(ledger, owner) };
    } finally { await release(); }
  }
  const sessionId = requiredString(request.sessionId, "sessionId", 4096);
  if (operation === 'session-tool') {
    const specs = { 'workflow-node': ['runId', 'target'], identity: [], prepare: [], targets: [], send: ['target', 'message'], inbox: [], reply: ['messageId', 'message'], ack: ['messageId'] };
    const fields = Object.hasOwn(specs, request.tool) ? specs[request.tool] : null;
    const args = request.args;
    if (!fields || !args || typeof args !== 'object' || Array.isArray(args) || Object.keys(args).some(key => !fields.includes(key)) || Object.keys(request).some(key => !['operation', 'sessionId', 'tool', 'args'].includes(key))) {
      throw new BridgeError('INVALID_TOOL_ARGUMENT', 'unsupported tool or identity override');
    }
    const identity = await canonicalSessionIdentityFor(sessionId, env);
    if (request.tool === 'prepare') {
      await daemonRequest('session.register', fenced(identity, {
        cwd: env.H2B_DSH_DEMO_CWD?.trim() || process.cwd(),
        command: ['dsh-h2b-talk-demo'], source: SOURCE, runtime: RUNTIME,
      }), { mutation: true, env });
      const status = await daemonRequest('identity.whoami', fenced(identity), { env });
      if (status.sessionRegistered !== true) throw new BridgeError('WORKFLOW_SENDER_NOT_READY', 'Workflow sender registration could not be verified');
      return { ...status, actor: identity.actor, sessionRef: identity.sessionRef, sessionId };
    }
    if (request.tool === 'workflow-node') {
      return daemonRequest('workflow.node.inspect', {
        actor: identity.actor, sessionRef: identity.sessionRef,
        runId: requiredString(args.runId, 'runId', 256), target: requiredString(args.target, 'target', 2048)
      }, { env });
    }
    if (request.tool === 'identity') return { ok: true, actor: identity.actor, sessionRef: identity.sessionRef, sessionId };
    if (request.tool === 'targets') return daemonRequest('targets', { kind: 'agent' }, { env });
    if (request.tool === 'inbox') return pendingFor(identity, env);
    if (request.tool === 'send') {
      const target = requiredString(args.target, 'target', 2048);
      const message = requiredString(args.message, 'message', 256 * 1024);
      if (!canonicalAgentPrincipal(target)) throw new BridgeError('INVALID_PARTICIPANT', 'target must be an exact four-part Agent URI');
      const directory = await daemonRequest('targets', { kind: 'agent' }, { env });
      if (!directory.targets?.some(item => item.targetKind === 'agent' && item.targetUri === target && item.status !== 'offline' && item.deliverable !== false)) throw new BridgeError('PARTICIPANT_NOT_AVAILABLE', 'target is not available');
      // Preserve the existing bounded, per-session participant authorization.
      await updateParticipants(ledgerPath(env), identity, current => [...current, target], { maxEntries: MAX_SESSION_PARTICIPANTS });
      return daemonRequest('message.send', fenced(identity, { to: [target], message }), { mutation: true, env });
    }
    const messageId = requiredString(args.messageId, 'messageId', 4096);
    const pending = await pendingFor(identity, env);
    if (!pending.messages.some(item => item.messageId === messageId)) throw new BridgeError('MESSAGE_NOT_AUTHORIZED', 'message is not in this session authorized inbox');
    const params = { messageId };
    if (request.tool === 'reply') params.message = requiredString(args.message, 'message', 256 * 1024);
    return daemonRequest(request.tool === 'reply' ? 'message.reply' : 'message.ack', fenced(identity, params), { mutation: true, env });
  }
  const remote = operation.startsWith("remote-");
  const normalizedOperation = remote ? operation.slice("remote-".length) : operation;
  if (normalizedOperation === "name-configure") {
    const current = await identityFor(sessionId, env, "remote");
    const actorName = remoteEntryName(request.entryName);
    const next = sessionIdentity(sessionId, (await daemonRequest("ps", {}, { env })).daemon, "remote", actorName);
    const bindings = await remoteBindings(ledgerPath(env));
    if (next.actor === current.actor) {
      return { ok: true, previousActor: current.actor, actor: current.actor, actorName: current.actorName, entryName: actorName ? actorName.slice(4) : "", unchanged: true };
    }
    if (bindings.some((binding) => binding.sessionId === sessionId)) {
      throw new BridgeError("REMOTE_NAME_BOUND", "unbind the remote entry before changing its network name");
    }
    if (next.actor !== current.actor) {
      const result = await daemonRequest("targets", { kind: "agent" }, { env });
      const targets = Array.isArray(result.targets) ? result.targets : [];
      if (targets.some((target) => target && target.targetUri === next.actor && target.status !== "offline")) {
        throw new BridgeError("REMOTE_NAME_CONFLICT", "another live H2B Agent already uses this network name");
      }
    }
    await updateRemoteName(ledgerPath(env), sessionId, actorName);
    return { ok: true, previousActor: current.actor, actor: next.actor, actorName: next.actorName, entryName: actorName ? actorName.slice(4) : "" };
  }
  // Unprefixed diagnostic reads describe the session's selected identity,
  // just like its native tools. Legacy web mutation paths remain explicit:
  // querying a renamed/pinned session must never register or migrate an alias.
  const identity = !remote && ["status", "identity"].includes(normalizedOperation)
    ? await canonicalSessionIdentityFor(sessionId, env)
    : await identityFor(sessionId, env, remote ? "remote" : "web");

  if (normalizedOperation === "identity") {
    const configuredName = identity.sessionRef.startsWith("dsh-remote:") ? (await readLedger(ledgerPath(env))).remoteNames[sessionId] : "";
    return { ok: true, actor: identity.actor, actorName: identity.actorName, entryName: configuredName ? configuredName.replace(/^dsh-/, "") : "", sessionRef: identity.sessionRef };
  }

  if (normalizedOperation === "bind") {
    const adapter = remoteAdapter(request.adapter);
    const binding = await updateRemoteBinding(ledgerPath(env), adapter, (current) => {
      if (current && current.actor !== identity.actor) {
        throw new BridgeError("REMOTE_BINDING_CONFLICT", "adapter is already bound to another DSH work session");
      }
      return {
        sessionId,
        actor: identity.actor,
        sessionRef: identity.sessionRef,
        boundAt: current?.boundAt || Date.now(),
        broadcastMode: current?.broadcastMode || "off",
        broadcastRoute: current?.broadcastRoute || "",
      };
    });
    return { ok: true, binding: { adapter, ...binding } };
  }

  if (normalizedOperation === "unbind") {
    const adapter = remoteAdapter(request.adapter);
    const expectedActor = requiredString(request.expectedActor, "expectedActor", 2048);
    if (!canonicalAgentPrincipal(expectedActor)) {
      throw new BridgeError("INVALID_ACTOR", "expectedActor must be a canonical agent identity");
    }
    const binding = await updateRemoteBinding(ledgerPath(env), adapter, (current) => {
      if (!current) return null;
      if (current.actor !== expectedActor || current.actor !== identity.actor) {
        throw new BridgeError("REMOTE_BINDING_CHANGED", "remote binding changed; refresh before unbinding");
      }
      return null;
    });
    return { ok: true, adapter, actor: identity.actor, unbound: binding === null };
  }

  if (normalizedOperation === "broadcast-configure") {
    const adapter = remoteAdapter(request.adapter);
    const mode = remoteBroadcastMode(request.mode);
    const route = mode === "off" ? "" : remoteRoute(request.route, adapter);
    await requireRemoteBinding(ledgerPath(env), adapter, sessionId, identity);
    if (route) {
      const result = await daemonRequest("targets", { kind: "channel_route" }, { env });
      const targets = Array.isArray(result.targets) ? result.targets : [];
      const available = targets.some((target) => target && target.targetKind === "channel_route" && target.targetUri === route && target.deliverable !== false);
      if (!available) throw new BridgeError("BROADCAST_ROUTE_UNAVAILABLE", "selected channel route is not currently deliverable");
    }
    const binding = await updateRemoteBinding(ledgerPath(env), adapter, (current) => ({
      ...current,
      broadcastMode: mode,
      broadcastRoute: route,
      broadcastUpdatedAt: Date.now(),
    }));
    return { ok: true, binding: { adapter, ...binding } };
  }

  if (normalizedOperation === "broadcast") {
    const adapter = remoteAdapter(request.adapter);
    const binding = await requireRemoteBinding(ledgerPath(env), adapter, sessionId, identity);
    const role = requiredString(request.role, "role", 16);
    if (!["user", "assistant"].includes(role)) throw new BridgeError("INVALID_BROADCAST_ROLE", "broadcast role must be user or assistant");
    const eventId = requiredString(request.eventId, "eventId", 4096);
    const message = requiredString(request.message, "message", 256 * 1024);
    if (binding.broadcastMode === "off" || !binding.broadcastRoute) {
      throw new BridgeError("BROADCAST_DISABLED", "broadcast is not enabled for this binding");
    }
    if (role === "user" && binding.broadcastMode !== "full") {
      throw new BridgeError("BROADCAST_ROLE_DISABLED", "user messages require full mirror mode");
    }
    const digest = createHash("sha256")
      .update(`${sessionId}\n${adapter}\n${binding.broadcastRoute}\n${role}\n${eventId}`, "utf8")
      .digest("hex");
    return daemonRequest("message.send", fenced(identity, {
      to: [binding.broadcastRoute],
      message,
      idempotencyKey: `dsh-feishu-broadcast:${digest}`,
    }), { mutation: true, env });
  }

  if (AGENT_TASK_OPERATIONS.has(operation)) {
    const body = request.body;
    if (!body || typeof body !== "object" || Array.isArray(body)) {
      throw new BridgeError("INVALID_ARGUMENT", "agent.task body must be an object");
    }
    const override = Object.keys(body).find((key) => AGENT_TASK_IDENTITY_FIELDS.has(key));
    if (override) {
      throw new BridgeError(
        "IDENTITY_OVERRIDE_FORBIDDEN",
        `${override} is daemon-bound and cannot be supplied by a DSH client`,
      );
    }
    return daemonRequest(
      operation,
      fenced(identity, { ...body }),
      {
        mutation: !["agent.task.capabilities", "agent.task.status", "agent.task.result"].includes(operation),
        env,
      },
    );
  }

  if (normalizedOperation === "connect") {
    const result = await daemonRequest(
      "session.register",
      fenced(identity, {
        cwd: env.H2B_DSH_DEMO_CWD?.trim() || process.cwd(),
        command: ["dsh-h2b-talk-demo"],
        source: SOURCE,
        runtime: RUNTIME,
      }),
      { mutation: true, env },
    );
    return { ...result, actor: identity.actor, sessionRef: identity.sessionRef };
  }

  if (normalizedOperation === "status") {
    const result = await daemonRequest("identity.whoami", fenced(identity), { env });
    return { ...result, actor: identity.actor, sessionRef: identity.sessionRef };
  }

  if (normalizedOperation === "pending") {
    if (!remote) {
      const result = await pendingFor(identity, env);
      const ledger = await readLedger(ledgerPath(env));
      const key = ledgerKey(identity);
      const binding = ledger.humanChats[key];
      if (binding) for (const [sourceKey, canonicalKey] of Object.entries(ledger.humanChatAliases || {})) {
        if (canonicalKey !== key) continue;
        const [actor, sessionRef] = sourceKey.split("\n");
        const previous = await pendingFor({ actor, sessionRef }, env);
        result.messages.push(...previous.messages.filter(message => message.from === binding.target));
        result.deniedCount += previous.deniedCount;
      }
      result.message = result.messages.find(message => !message.injected) || null;
      result.injectedCount = result.messages.filter(message => message.injected).length;
      return result;
    }
    const adapter = remoteAdapter(request.adapter);
    await requireRemoteBinding(ledgerPath(env), adapter, sessionId, identity);
    return pendingFor(identity, env, { allowedPrincipals: remoteAdapterPrincipals(adapter, identity) });
  }

  if (operation === "participant-authorize") {
    const participant = requiredString(request.participant, "participant", 2048);
    if (!canonicalAgentPrincipal(participant)) {
      throw new BridgeError(
        "INVALID_PARTICIPANT",
        "participant must be an exact canonical agent: identity",
      );
    }
    const result = await daemonRequest("targets", { kind: "agent" }, { env });
    const targets = Array.isArray(result.targets) ? result.targets : [];
    const verified = targets.some((target) => (
      target &&
      typeof target === "object" &&
      target.targetKind === "agent" &&
      target.targetUri === participant
    ));
    if (!verified) {
      throw new BridgeError(
        "PARTICIPANT_NOT_AVAILABLE",
        "participant is not a verified live H2B agent target",
      );
    }
    const participants = await updateParticipants(
      ledgerPath(env),
      identity,
      (current) => [...current, participant],
      { maxEntries: MAX_SESSION_PARTICIPANTS },
    );
    return {
      ok: true,
      actor: identity.actor,
      sessionRef: identity.sessionRef,
      participant,
      authorized: true,
      participants,
    };
  }

  if (operation === "participant-revoke") {
    const participant = requiredString(request.participant, "participant", 2048);
    if (!canonicalAgentPrincipal(participant)) {
      throw new BridgeError(
        "INVALID_PARTICIPANT",
        "participant must be an exact canonical agent: identity",
      );
    }
    const before = await participantSet(ledgerPath(env), identity);
    const participants = await updateParticipants(
      ledgerPath(env),
      identity,
      (current) => current.filter((item) => item !== participant),
    );
    return {
      ok: true,
      actor: identity.actor,
      sessionRef: identity.sessionRef,
      participant,
      revoked: before.has(participant),
      participants,
    };
  }

  if (operation === "participant-list") {
    const participants = [...await participantSet(ledgerPath(env), identity)].sort();
    return {
      ok: true,
      actor: identity.actor,
      sessionRef: identity.sessionRef,
      participants,
    };
  }

  if (operation === "chat-bind") {
    const target = requiredString(request.target, "target", 2048);
    const label = requiredString(request.label, "label", 120);
    if (!canonicalAgentPrincipal(target)) {
      throw new BridgeError("INVALID_CHAT_TARGET", "chat target must be an exact canonical agent: identity");
    }
    const participants = await participantSet(ledgerPath(env), identity);
    if (!participants.has(target)) {
      throw new BridgeError(
        "CHAT_TARGET_NOT_AUTHORIZED",
        "chat target must be verified and authorized for this DSH Web session before binding",
      );
    }
    const now = Date.now();
    const suppliedMessages = Array.isArray(request.chatMessages)
      ? request.chatMessages.map(normalizeChatMessage).filter(Boolean).slice(-100)
      : [];
    const binding = await updateHumanChat(ledgerPath(env), identity, (current, ledger, key) => {
      const existing = directChatIndex(ledger, chatKeyOwner(key)).find(chat => chat.binding.target === target && chat.sessionId !== sessionId);
      if (existing) throw new BridgeError("CHAT_EXISTS", "This Agent already has a direct chat", { sessionId: existing.sessionId });
      if (current && current.target !== target) throw new BridgeError("CHAT_TARGET_IMMUTABLE", "An existing direct chat cannot change Agent identity");
      return {
        target,
        label,
        messages: current?.target === target
          ? mergeChatHistory(current.messages, suppliedMessages)
          : suppliedMessages,
        workSessionId: current?.workSessionId || "",
        workSessionIds: chatWorkSessions(current),
        createdAt: current?.target === target && current.createdAt ? current.createdAt : now,
        lastOpenedAt: now,
      };
    });
    return { ok: true, actor: identity.actor, sessionRef: identity.sessionRef, binding };
  }

  if (operation === "chat-binding") {
    const binding = await humanChatBinding(ledgerPath(env), identity);
    return { ok: true, actor: identity.actor, sessionRef: identity.sessionRef, binding };
  }

  if (operation === "chat-unbind") {
    const binding = await humanChatBinding(ledgerPath(env), identity);
    await updateHumanChat(ledgerPath(env), identity, () => null);
    return { ok: true, actor: identity.actor, sessionRef: identity.sessionRef, binding, unbound: binding !== null };
  }

  if (operation === "chat-message-append") {
    const message = normalizeChatMessage(request.chatMessage);
    if (!message) throw new BridgeError("INVALID_CHAT_MESSAGE", "chatMessage is invalid");
    const binding = await updateHumanChat(ledgerPath(env), identity, (current) => {
      if (!current) throw new BridgeError("CHAT_NOT_BOUND", "chat must be bound before appending history");
      const messages = mergeChatHistory(current.messages, [message]);
      return { ...current, messages, lastOpenedAt: Math.max(current.lastOpenedAt, message.time, Date.now()) };
    });
    return { ok: true, actor: identity.actor, sessionRef: identity.sessionRef, binding };
  }

  if (operation === "chat-history-clear") {
    const binding = await updateHumanChat(ledgerPath(env), identity, (current) => {
      if (!current) throw new BridgeError("CHAT_NOT_BOUND", "chat must be bound before clearing history");
      return { ...current, messages: [], lastOpenedAt: Date.now() };
    });
    return { ok: true, actor: identity.actor, sessionRef: identity.sessionRef, binding };
  }

  if (operation === "chat-work-link") {
    const workSessionId = requiredString(request.workSessionId, "workSessionId", 4096);
    const binding = await updateHumanChat(ledgerPath(env), identity, (current, ledger, key) => {
      if (!current) throw new BridgeError("CHAT_NOT_BOUND", "chat must be bound before linking a work session");
      for (const [otherKey, raw] of Object.entries(ledger.humanChats)) {
        const other = normalizeHumanChat(raw);
        if (otherKey !== key && chatWorkSessions(other).includes(workSessionId)) {
          throw new BridgeError("WORK_SESSION_ALREADY_LINKED", "work session is already linked to another direct chat");
        }
      }
      const workSessionIds = [...new Set([...chatWorkSessions(current), workSessionId])];
      return { ...current, workSessionId: workSessionIds[0], workSessionIds, lastOpenedAt: Date.now() };
    });
    return { ok: true, actor: identity.actor, sessionRef: identity.sessionRef, binding };
  }

  if (operation === "chat-work-unlink") {
    const expected = request.workSessionId === undefined
      ? ""
      : requiredString(request.workSessionId, "workSessionId", 4096);
    const binding = await updateHumanChat(ledgerPath(env), identity, (current) => {
      if (!current) return null;
      const workSessionIds = expected ? chatWorkSessions(current).filter(id => id !== expected) : [];
      return { ...current, workSessionId: workSessionIds[0] || "", workSessionIds, lastOpenedAt: Date.now() };
    });
    return { ok: true, actor: identity.actor, sessionRef: identity.sessionRef, binding };
  }

  if (normalizedOperation === "mark-injected") {
    const deliveryId = requiredString(request.deliveryId, "deliveryId", 4096);
    let allowedPrincipals = new Set();
    if (remote) {
      const adapter = remoteAdapter(request.adapter);
      await requireRemoteBinding(ledgerPath(env), adapter, sessionId, identity);
      allowedPrincipals = remoteAdapterPrincipals(adapter, identity);
    }
    const pending = await pendingFor(identity, env, { allowedPrincipals });
    const message = pending.messages.find((item) => item.deliveryId === deliveryId);
    if (!message) {
      throw new BridgeError("DELIVERY_NOT_ALLOWED", "delivery is not pending for this session and allowlist");
    }
    const recorded = await recordInjected(ledgerPath(env), identity, deliveryId);
    return {
      ok: true,
      actor: identity.actor,
      sessionRef: identity.sessionRef,
      deliveryId,
      injected: true,
      alreadyInjected: !recorded,
    };
  }

  if (operation === "send") {
    const ledger = await readLedger(ledgerPath(env));
    const redirected = ledger.humanChatAliases?.[ledgerKey(identity)];
    if (redirected) throw new BridgeError("CHAT_EXISTS", "Use this Agent's existing direct chat", { sessionId: chatKeySession(redirected) });
    const target = requiredString(request.target, "target", 2048);
    const message = requiredString(request.message, "message", 256 * 1024);
    const params = { to: [target], message };
    if (request.conversationId !== undefined) {
      params.conversationId = requiredString(request.conversationId, "conversationId", 4096);
    }
    return daemonRequest("message.send", fenced(identity, params), { mutation: true, env });
  }

  if (normalizedOperation === "reply") {
    const messageId = requiredString(request.messageId, "messageId", 4096);
    const message = requiredString(request.message, "message", 256 * 1024);
    return daemonRequest(
      "message.reply",
      fenced(identity, { messageId, message }),
      { mutation: true, env },
    );
  }

  if (normalizedOperation === "ack") {
    const messageId = requiredString(request.messageId, "messageId", 4096);
    return daemonRequest("message.ack", fenced(identity, { messageId }), { mutation: true, env });
  }

  if (normalizedOperation === "disconnect") {
    const result = await daemonRequest("session.unregister", fenced(identity), { mutation: true, env });
    return { ...result, actor: identity.actor, sessionRef: identity.sessionRef };
  }

  throw new BridgeError("UNKNOWN_OPERATION", `unsupported bridge operation: ${operation}`);
}

async function handleMfuWorkflowRpc(request, env = process.env) {
  if (!request || typeof request !== "object" || Array.isArray(request)) {
    throw new BridgeError("INVALID_ARGUMENT", "MFU workflow request must be an object");
  }
  const operation = requiredString(request.operation, "operation", 80);
  if (!MFU_WORKFLOW_OPERATIONS.has(operation)) {
    throw new BridgeError("UNKNOWN_OPERATION", `unsupported MFU workflow operation: ${operation}`);
  }
  const sessionId = requiredString(request.sessionId, "sessionId", 4096);
  const body = request.body === undefined ? {} : request.body;
  if (!body || typeof body !== "object" || Array.isArray(body)) {
    throw new BridgeError("INVALID_ARGUMENT", "MFU workflow body must be an object");
  }
  const client = createAgentTaskClient({
    transport(agentOperation, payload) {
      return handleRpc({ operation: agentOperation, sessionId, body: payload }, env);
    },
  });
  if (operation === "workflow.capabilities") {
    const capabilities = await client.capabilities({ refresh: true });
    return {
      version: 2,
      agentTask: capabilities.supported ? "supported" : "unsupported",
      signal: "unsupported",
      subscribe: "unsupported",
    };
  }
  if (operation === "workflow.start") return client.start(body);
  if (operation === "workflow.status") return client.status(body);
  if (operation === "workflow.result") return client.result(body);
  return client.cancel(body);
}

async function readStdin() {
  const chunks = [];
  let size = 0;
  for await (const chunk of process.stdin) {
    size += chunk.length;
    if (size > MAX_DOCUMENT_BYTES) {
      throw new BridgeError("REQUEST_TOO_LARGE", "RPC request exceeded 1 MiB");
    }
    chunks.push(chunk);
  }
  try {
    return JSON.parse(Buffer.concat(chunks, size).toString("utf8"));
  } catch {
    throw new BridgeError("INVALID_JSON", "stdin must contain one JSON document");
  }
}

async function main() {
  if (process.argv.length !== 3 || process.argv[2] !== "rpc") {
    throw new BridgeError("USAGE", "usage: node h2b-session-bridge.mjs rpc");
  }
  const request = await readStdin();
  const result = request?.surface === "mfu-workflow"
    ? await handleMfuWorkflowRpc(request)
    : await handleRpc(request);
  process.stdout.write(`${JSON.stringify(result)}\n`);
}

if (process.argv[1] && fileURLToPath(import.meta.url) === path.resolve(process.argv[1])) {
  main().catch((error) => {
    const code = typeof error?.code === "string" ? error.code : "BRIDGE_ERROR";
    const message = error instanceof Error ? error.message : String(error);
    const details = error?.details;
    process.stdout.write(`${JSON.stringify({
      ok: false,
      error: { code, message, ...(details === undefined ? {} : { details }) },
    })}\n`);
    process.exitCode = 1;
  });
}

export { handleMfuWorkflowRpc, handleRpc, sessionIdentity, migrateLegacyState, recoverPinnedBindings, normalizeChatMessage, normalizeHumanChat };
