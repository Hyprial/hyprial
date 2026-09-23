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

import { pacConfig, pacRequest } from './integration/pac-bridge.js';

import { chatKeyOwner, chatKeySession, chatWorkSessions, consolidateDirectChats, directChatIndex } from "./integration/direct-chat-index.js";

const IPC_VERSION = 1;
const MAX_DOCUMENT_BYTES = 1024 * 1024;
const MAX_DAEMON_BYTES = 8 * 1024 * 1024;
const DEFAULT_TIMEOUT_MS = 5_000;
const MAX_CONTACT_BYTES = 512 * 1024;
const NETWORK_UNAVAILABLE_REASON = "message.pending.list provides no authenticated sender proof; verified network admission requires a real authentication contract";
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
// Web-domain reception operations. The `remote-` prefix selects the canonical
// remote identity for the same sessionId; both domains share handlers/storage.
const RECEPTION_OPERATIONS = new Set([
  "reception-policy-get",
  "reception-policy-set",
  "whitelist-list",
  "whitelist-add",
  "whitelist-remove",
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

function ledgerKey(identity) {
  return `${identity.actor}\n${identity.sessionRef}`;
}

async function canonicalSessionIdentityFor(sessionId, env = process.env, fallbackKind = "web") {
  requiredString(sessionId, "sessionId", 4096);
  const ledger = await readLedger(ledgerPath(env));
  const bindings = Object.values(ledger.remoteBindings).filter(binding => binding.sessionId === sessionId);
  const configured = ledger.remoteNames[sessionId];
  if (configured && (typeof configured !== "string" || !/^dsh-[a-z0-9](?:[a-z0-9-]{0,46}[a-z0-9])?$/.test(configured))) {
    throw new BridgeError("REMOTE_BINDING_MISMATCH", "saved session network name is invalid");
  }
  const selected = ledger.sessionBindings?.[sessionId];
  const kind = bindings.length || configured ? "remote" : fallbackKind;
  const status = await daemonRequest("ps", {}, { env });
  const identity = selected
    ? { actor: selected.actor, actorName: selected.actor.split(':')[3], sessionRef: selected.sessionRef }
    : sessionIdentity(sessionId, status.daemon, kind, configured || "");
  if (selected && identity.actor.split(':').slice(1, 3).join(':') !== [status.daemon.owner, status.daemon.nodeId].join(':')) throw new BridgeError('SESSION_IDENTITY_MISMATCH', 'session binding belongs to a different owner or node; explicit migration required');
  if (bindings.some(binding => binding.actor !== identity.actor || binding.sessionRef !== identity.sessionRef)) {
    throw new BridgeError("REMOTE_BINDING_MISMATCH", "saved binding does not match session identity");
  }
  return identity;
}

async function setSessionBinding(sessionId, identity, env, changes) {
  const file = ledgerPath(env);
  const release = await acquireLedgerLock(file);
  try {
    const ledger = await readLedger(file);
    ledger.sessionBindings ||= {};
    const previous = ledger.sessionBindings[sessionId];
    if (previous && (previous.actor !== identity.actor || previous.sessionRef !== identity.sessionRef)) throw new BridgeError('SESSION_BINDING_CHANGED', 'session identity changed; reconnect explicitly');
    ledger.sessionBindings[sessionId] = { ...previous, actor: identity.actor, sessionRef: identity.sessionRef, enabled: false, ...changes };
    await writeSessionLedger(file, ledger);
    return ledger.sessionBindings[sessionId];
  } finally { await release(); }
}

async function adoptSessionIdentity(sessionId, identity, env, humanChat) {
  const file = ledgerPath(env);
  const snapshot = await daemonRequest('ps', {}, { env });
  const legacy = (snapshot.agents || []).filter(agent =>
    typeof agent.uri === 'string' && agent.uri.split(':').slice(0, 3).join(':') === identity.actor.split(':').slice(0, 3).join(':') && agent.uri !== identity.actor && ['dsh-web:' + sessionId, 'dsh-remote:' + sessionId].includes(agent.lastSessionId)
  ).map(agent => ({ actor: agent.uri, sessionRef: agent.lastSessionId }));
  const release = await acquireLedgerLock(file);
  try {
    const ledger = await readLedger(file);
    const before = structuredClone(ledger);
    ledger.sessionBindings ||= {};
    const previous = ledger.sessionBindings[sessionId];
    if (previous && (previous.actor !== identity.actor || previous.sessionRef !== identity.sessionRef)) throw new BridgeError('SESSION_BINDING_CHANGED', 'session identity changed; reconnect explicitly');
    const aliases = [...new Map([...legacy, ...(previous?.legacyIdentities || [])].map(item => [ledgerKey(item), item])).values()];
    // Preserve original records for rollback and historical attribution. The
    // receive path drains each alias under its own authorization and dedup key.
    ledger.sessionBindings[sessionId] = { ...previous, actor: identity.actor, sessionRef: identity.sessionRef, enabled: true, humanChat, legacyIdentities: aliases };
    const key = ledgerKey(identity);
    if (!previous) ledger.participants[key] = [...new Set([...(ledger.participants[key] || []), ...aliases.flatMap(item => ledger.participants[ledgerKey(item)] || [])])];
    if (!previous) {
      const backup = await open(file + '.before-session-identity-' + Date.now() + '-' + randomUUID() + '.json', 'wx', 0o600);
      try { await backup.writeFile(JSON.stringify(before)); await backup.sync(); }
      finally { await backup.close(); }
    }
    await writeSessionLedger(file, ledger);
  } finally { await release(); }
}

async function registerSession(sessionId, identity, env, humanChat = false) {
  // Commit the selected identity before IPC, so every concurrent entry uses it.
  await adoptSessionIdentity(sessionId, identity, env, humanChat);
  const result = await daemonRequest('session.register', fenced(identity, {
    cwd: env.H2B_DSH_DEMO_CWD?.trim() || process.cwd(),
    command: ['dsh-hyprial-plugin'], source: SOURCE, runtime: RUNTIME,
  }), { mutation: true, env });
  await setSessionBinding(sessionId, identity, env, { enabled: true, humanChat });
  return { ...result, actor: identity.actor, sessionRef: identity.sessionRef };
}

async function retireDrainedAliases(sessionId, identity, env) {
  const file = ledgerPath(env);
  const binding = (await readLedger(file)).sessionBindings?.[sessionId];
  const aliases = (binding?.legacyIdentities || []).filter(alias => !alias.retired);
  if (!aliases.length) return [];
  const pins = await daemonRequest('adapter.pins', {}, { env });
  const retired = [], warnings = [];
  for (const alias of aliases) {
    if (Object.values(pins.pins || {}).includes(alias.actor)) continue;
    try {
      // Include denied messages in this check: migration must not discard them.
      const pending = await daemonRequest('message.pending.list', fenced(alias), { env });
      if (!Array.isArray(pending.messages) || pending.messages.length) continue;
      await daemonRequest('session.unregister', fenced(alias), { mutation: true, env });
      retired.push(ledgerKey(alias));
    } catch (error) { warnings.push({ actor: alias.actor, code: error.code || 'BRIDGE_ERROR' }); }
  }
  if (retired.length) {
    const release = await acquireLedgerLock(file);
    try {
      const ledger = await readLedger(file);
      const current = ledger.sessionBindings?.[sessionId];
      if (current?.actor === identity.actor && current.sessionRef === identity.sessionRef) {
        current.legacyIdentities = current.legacyIdentities.map(alias => retired.includes(ledgerKey(alias)) ? { ...alias, retired: true, retiredAt: Date.now() } : alias);
        await writeSessionLedger(file, ledger);
      }
    } finally { await release(); }
  }
  return warnings;
}

async function sessionStatus(sessionId, identity, env) {
  const result = await daemonRequest('identity.whoami', fenced(identity), { env });
  const directory = await daemonRequest('targets', { kind: 'agent' }, { env });
  const online = result.sessionRegistered === true && directory.targets?.some(row => row.targetUri === identity.actor && row.status === 'online') === true;
  const binding = (await readLedger(ledgerPath(env))).sessionBindings?.[sessionId];
  return { ...result, actor: identity.actor, sessionRef: identity.sessionRef, online, enabled: binding?.enabled === true, status: online ? 'online' : 'offline' };
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
    if (document.receptionPolicies !== undefined && (
      typeof document.receptionPolicies !== "object" ||
      !document.receptionPolicies ||
      Array.isArray(document.receptionPolicies)
    )) {
      throw new Error("unexpected receptionPolicies schema");
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
      receptionPolicies: document.receptionPolicies || {},
      humanChats: document.humanChats || {},
      remoteBindings: document.remoteBindings || {},
      remoteNames: document.remoteNames || {},
    };
  } catch (error) {
    if (error?.code === "ENOENT") {
      return { version: 1, sessions: {}, participants: {}, receptionPolicies: {}, humanChats: {}, remoteBindings: {}, remoteNames: {} };
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
    const previous = ledger.sessionBindings?.[sessionId];
    if (previous) {
      ledger.identityHistory = [...(ledger.identityHistory || []), { sessionId, ...previous, retiredAt: Date.now() }];
      delete ledger.sessionBindings[sessionId];
    }
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

async function acquireLedgerLock(file, staleMs = 10_000) {
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
        if (Date.now() - info.mtimeMs > staleMs) {
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

function contactAddress(value) {
  requiredString(value, "contact", 2048);
  if (!canonicalAgentPrincipal(value) || /[\s\x00-\x1f\x7f]/u.test(value)) {
    throw new BridgeError("INVALID_CONTACT", "contact must be an exact four-part Agent URI");
  }
  return value; // Address syntax only, never evidence of sender authentication.
}

// Contacts are the explicit URI reception allowlist. Keep the existing durable
// participants map as the single source of truth; no identity/history migration.
async function updateParticipants(file, identity, update, { revokeAliases = false } = {}) {
  const release = await acquireLedgerLock(file);
  try {
    const ledger = await readLedger(file);
    const key = ledgerKey(identity);
    const previousBytes = Buffer.byteLength(JSON.stringify(ledger.participants));
    const current = Array.isArray(ledger.participants[key])
      ? ledger.participants[key].filter(canonicalAgentPrincipal) : [];
    const next = [...new Set(update(current))].sort();
    if (revokeAliases) {
      const sessionId = identity.sessionRef.slice(identity.sessionRef.indexOf(':') + 1);
      for (const alias of ledger.sessionBindings?.[sessionId]?.legacyIdentities || []) {
        const aliasKey = ledgerKey(alias);
        ledger.participants[aliasKey] = update(ledger.participants[aliasKey] || []).filter(canonicalAgentPrincipal);
      }
    }
    if (next.length > 0) ledger.participants[key] = next;
    else delete ledger.participants[key];
    const nextBytes = Buffer.byteLength(JSON.stringify(ledger.participants));
    // Existing oversized lists remain usable and can be reduced incrementally.
    if (nextBytes > MAX_CONTACT_BYTES && nextBytes > previousBytes) {
      throw new BridgeError("CONTACT_LIMIT_EXCEEDED", "aggregate contact data exceeds the byte limit");
    }
    await writeSessionLedger(file, ledger);
    return next;
  } finally { await release(); }
}

async function participantSet(file, identity) {
  const ledger = await readLedger(file);
  const values = ledger.participants[ledgerKey(identity)];
  return new Set(Array.isArray(values) ? values.filter(canonicalAgentPrincipal) : []);
}

// Reception policy is per exact ledgerKey(identity). Only an explicitly saved
// 'whitelist' entry restricts reception; absence (or any other persisted value)
// defaults to open network Agent reception. Whitelist content lives in the
// existing participants map: it was never a separate store and is not migrated.
function receptionPolicyOf(ledger, identity) {
  return ledger.receptionPolicies?.[ledgerKey(identity)] === "whitelist" ? "whitelist" : "open";
}

async function receptionState(file, identity) {
  const ledger = await readLedger(file);
  const values = ledger.participants[ledgerKey(identity)];
  return {
    policy: receptionPolicyOf(ledger, identity),
    whitelist: new Set(Array.isArray(values) ? values.filter(canonicalAgentPrincipal) : []),
  };
}

async function updateReceptionPolicy(file, identity, policy) {
  const release = await acquireLedgerLock(file);
  try {
    const ledger = await readLedger(file);
    const key = ledgerKey(identity);
    if (policy === "whitelist") ledger.receptionPolicies[key] = "whitelist";
    else delete ledger.receptionPolicies[key];
    await writeSessionLedger(file, ledger);
    return policy;
  } finally { await release(); }
}

function receptionPayload(identity, { policy, whitelist, fixedAllowedPrincipals }) {
  const entries = [...whitelist].sort();
  return {
    ok: true,
    actor: identity.actor,
    sessionRef: identity.sessionRef,
    policy,
    whitelist: entries,
    // Deprecated one-release alias retained for the pre-policy contacts API.
    contacts: entries,
    fixedAllowedPrincipals,
    networkVerificationAvailable: false,
    unavailableReason: NETWORK_UNAVAILABLE_REASON,
  };
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

async function pendingFor(identity, env, options = {}) {
  const result = await pendingForOne(identity, env, { ...options, pacActor: identity.actor });
  const sessionId = identity.sessionRef.slice(identity.sessionRef.indexOf(':') + 1);
  const ledger = await readLedger(ledgerPath(env));
  const binding = ledger.sessionBindings?.[sessionId];
  if (binding?.actor !== identity.actor) return result;
  for (const legacy of binding.legacyIdentities || []) {
    if (legacy.retired) continue;
    try {
      const previous = await pendingForOne(legacy, env, { ...options, pacActor: identity.actor });
      result.messages.push(...previous.messages.map(message => ({ ...message, sourceIdentity: legacy })));
      result.deniedCount += previous.deniedCount;
    } catch (error) {
      // A superseded alias must never re-register. Keep its persisted data for
      // explicit recovery; do not block the canonical inbox on an absent alias.
      if (!['STALE_SESSION', 'SESSION_SUPERSEDED'].includes(error.code)) throw error;
    }
  }
  result.message = result.messages.find(message => !message.injected) || null;
  return result;
}

async function deliveryIdentity(identity, env, messageId) {
  const sessionId = identity.sessionRef.slice(identity.sessionRef.indexOf(':') + 1);
  const binding = (await readLedger(ledgerPath(env))).sessionBindings?.[sessionId];
  for (const legacy of binding?.legacyIdentities || []) {
    try {
      const pending = await pendingForOne(legacy, env);
      if (pending.messages.some(message => message.messageId === messageId)) return legacy;
    } catch (error) { if (!['STALE_SESSION', 'SESSION_SUPERSEDED'].includes(error.code)) throw error; }
  }
  return identity;
}

async function pendingForOne(identity, env, { allowedPrincipals = new Set(), includePacDispatch = false, pacActor = identity.actor } = {}) {
  const pac = includePacDispatch ? null : await pacConfig(env);
  const response = await daemonRequest("message.pending.list", fenced(identity), { env });
  const messages = Array.isArray(response.messages) ? response.messages : [];
  const allowedFrom = allowlist(env);
  for (const principal of allowedPrincipals) allowedFrom.add(principal);
  const file = ledgerPath(env);
  const reception = await receptionState(file, identity);
  // Whitelist entries are enforced only in whitelist mode; the env allowlist and
  // exact adapter grants stay additive under both policies.
  if (reception.policy === "whitelist") for (const entry of reception.whitelist) allowedFrom.add(entry);
  const injected = await injectedSet(file, identity);
  const allowed = [];
  let deniedCount = 0;
  for (const raw of messages) {
    if (!raw || typeof raw !== "object") continue;
    // Open mode admits only canonical Agent senders. User/adapter/channel
    // principals still require an explicit env or adapter grant.
    if (!canonicalPrincipal(raw.from) || !(allowedFrom.has(raw.from) || (reception.policy === "open" && canonicalAgentPrincipal(raw.from)))) {
      deniedCount += 1;
      continue;
    }
    // The structured remote-task consumer is the sole owner of these requests;
    // do not race it by also injecting the same request into a conversational turn.
    if (pac?.enabled && pac.roles.coordinator.actor === pacActor && pac.remoteDispatchers?.includes(raw.from) && raw.intent === 'request') {
      try { if (JSON.parse(raw.message)?.schema === 'dsh.pac.task/v1') continue; } catch {}
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

// One terminal path shared by native tools and Host completion. A missing
// pending item is a no-op for Host retries, never permission to send a reply.
async function settleMessage(identity, env, { messageId, message = '', tool = null }) {
  const digest = createHash('sha256').update(ledgerKey(identity) + '\n' + messageId).digest('hex');
  const release = await acquireLedgerLock(ledgerPath(env) + '.terminal-' + digest, 120_000);
  try {
    const principals = new Set();
    for (const binding of await remoteBindings(ledgerPath(env))) {
      if (binding.actor !== identity.actor || binding.sessionRef !== identity.sessionRef) continue;
      for (const principal of remoteAdapterPrincipals(binding.adapter, identity)) principals.add(principal);
    }
    const pending = await pendingFor(identity, env, { allowedPrincipals: principals });
    const item = pending.messages.find(item => item.messageId === messageId);
    if (!item) {
      if (tool) throw new BridgeError('MESSAGE_NOT_AUTHORIZED', 'message is not in this session authorized inbox');
      return { ok: true, disposition: 'not-pending', messageId };
    }
    if (tool === 'reply' && item.intent === 'reply')
      throw new BridgeError('MESSAGE_ALREADY_A_REPLY', 'consume a result with ACK; a result cannot request another reply');
    // Only a bound external adapter owns automatic text replies. Agent requests,
    // replies, PAC notifications and unknown origins all terminate with ACK.
    const reply = tool === 'reply' || (!tool && principals.has(item.from) && item.intent !== 'reply' && Boolean(message));
    const result = await daemonRequest(reply ? 'message.reply' : 'message.ack',
      fenced(item.sourceIdentity || identity, { messageId, ...(reply ? { message } : {}) }), { mutation: true, env });
    return { ...result, disposition: reply ? 'replied' : 'acknowledged' };
  } finally { await release(); }
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
      if (typeof ref !== 'string' || !/^dsh-(?:remote|web):session-/.test(ref)) { entries.push({ adapter, actor, status: 'unresolved-session' }); continue; }
      const sessionId = ref.slice(ref.indexOf(':') + 1);
      const actorName = typeof actor === 'string' ? actor.split(':')[3] : '';
      if (!/^dsh-[a-z0-9](?:[a-z0-9-]{0,46}[a-z0-9])?$/.test(actorName)) { entries.push({ adapter, actor, status: 'unsupported-actor' }); continue; }
      const identity = sessionIdentity(sessionId, status.daemon, ref.startsWith('dsh-web:') ? 'web' : 'remote', actorName);
      if (actor !== identity.actor) { entries.push({ adapter, actor, status: 'foreign-identity' }); continue; }
      const existing = ledger.remoteBindings[adapter];
      const selected = ledger.sessionBindings?.[sessionId];
      if ((selected && (selected.actor !== actor || selected.sessionRef !== ref)) || (existing && (existing.sessionId !== sessionId || existing.actor !== actor)) ||
          (ledger.remoteNames[sessionId] && ledger.remoteNames[sessionId] !== actorName)) {
        entries.push({ adapter, actor, sessionId, status: 'conflict' });
        continue;
      }
      if (existing && (selected || ledger.remoteNames[sessionId] === actorName)) {
        entries.push({ adapter, actor, sessionId, status: 'already-consistent' }); continue;
      }
      if (ref.startsWith('dsh-web:')) {
        ledger.sessionBindings ||= {};
        ledger.sessionBindings[sessionId] = selected || { actor, sessionRef: ref, enabled: true, humanChat: Boolean(ledger.humanChats[ledgerKey(identity)]) };
      } else ledger.remoteNames[sessionId] = actorName;
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
  const lifecycle = ['connect', 'remote-connect', 'disconnect', 'remote-disconnect', 'remote-name-configure', 'carrier-heartbeat'];
  if (request && (lifecycle.includes(request.operation) || (request.operation === 'session-tool' && request.tool === 'prepare'))) {
    const id = requiredString(request.sessionId, 'sessionId', 4096);
    const lock = ledgerPath(env) + '.session-' + createHash('sha256').update(id).digest('hex');
    const release = await acquireLedgerLock(lock, 60_000);
    try { return await handleSessionRpc(request, env); }
    finally { await release(); }
  }
  return handleSessionRpc(request, env);
}

async function handleSessionRpc(request, env = process.env) {
  if (!request || typeof request !== "object" || Array.isArray(request)) {
    throw new BridgeError("INVALID_ARGUMENT", "RPC request must be an object");
  }
  const operation = requiredString(request.operation, "operation", 64);
  if (operation.startsWith('pac-')) {
    const config = await pacConfig(env);
    if (operation === 'pac-poll') {
      if (!config?.enabled) return { ok: true, jobs: [], configured: false };
      const incomingErrors = [];
      if (config.remoteDispatchers?.length) {
        const role = config.roles.coordinator;
        const identity = await canonicalSessionIdentityFor(role.sessionId, env);
        if (identity.actor !== role.actor) throw new BridgeError('PAC_IDENTITY_CHANGED', 'coordinator identity changed; reconfigure PAC');
        await daemonRequest('session.register', fenced(identity, { cwd: process.cwd(), command: ['dsh-pac'], source: SOURCE, runtime: RUNTIME }), { mutation: true, env });
        const pending = await pendingFor(identity, env, { allowedPrincipals: new Set(config.remoteDispatchers), includePacDispatch: true });
        for (const item of pending.messages) {
          if (!config.remoteDispatchers.includes(item.from) || item.intent !== 'request') continue;
          let task; try { task = JSON.parse(item.message); } catch { continue; }
          if (task?.schema !== 'dsh.pac.task/v1') continue;
          try {
            if (Object.keys(task).some(k => !['schema', 'taskKey', 'title', 'brief'].includes(k))) throw new Error('unsupported remote task field');
            await pacRequest({ operation: 'tool', tool: 'create', actor: identity.actor, sessionId: role.sessionId, source: item.from, args: { taskKey: task.taskKey, title: task.title, brief: task.brief } }, env, config);
            await daemonRequest('message.ack', fenced(item.sourceIdentity || identity, { messageId: item.messageId }), { mutation: true, env });
          } catch (error) { incomingErrors.push({ messageId: item.messageId, code: error.code || 'PAC_REMOTE_TASK_REJECTED', message: error.message }); }
        }
      }
      return { ...await pacRequest({ operation: 'poll' }, env, config), incomingErrors };
    }
    const sessionId = requiredString(request.sessionId, 'sessionId', 4096);
    const identity = await canonicalSessionIdentityFor(sessionId, env);
    if (!config?.enabled) {
      if (operation === 'pac-tool' && request.tool === 'list') return { ok: true, configured: false, tasks: [], setup: 'Configure coordinator/worker/verifier with scripts/configure-pac.py' };
      throw new BridgeError('PAC_NOT_CONFIGURED', 'configure and enable PAC roles first');
    }
    if (!Object.values(config.roles).some(r => r.sessionId === sessionId && r.actor === identity.actor)) throw new BridgeError('PAC_NOT_OWNER', 'session is not a configured PAC role');
    if (operation === 'pac-tool') {
      const fields = { list: [], inspect: ['graphId'], create: ['taskKey', 'title', 'brief'], context: ['graphId', 'nodeId'], begin: ['graphId', 'nodeId', 'expectedToken'], complete: ['graphId', 'nodeId', 'expectedToken', 'evidenceRef'], cancel: ['graphId', 'expectedToken', 'evidenceRef'], rework: ['graphId', 'expectedToken', 'evidenceRef'] }[request.tool];
      if (!fields || !request.args || typeof request.args !== 'object' || Array.isArray(request.args) || Object.keys(request.args).some(k => !fields.includes(k)) || fields.some(k => typeof request.args[k] !== 'string' || !request.args[k].trim()) || Object.keys(request).some(k => !['operation','sessionId','tool','args'].includes(k))) throw new BridgeError('PAC_ARGUMENT_REJECTED', 'unsupported PAC arguments or identity override');
      await daemonRequest('session.register', fenced(identity, { cwd: process.cwd(), command: ['dsh-pac'], source: SOURCE, runtime: RUNTIME }), { mutation: true, env });
      return pacRequest({ operation: 'tool', tool: request.tool, args: request.args, actor: identity.actor, sessionId }, env, config);
    }
    if (!['pac-reserve', 'pac-received'].includes(operation)) throw new BridgeError('PAC_INVALID_OPERATION', 'unsupported PAC carrier operation');
    return pacRequest({ operation: operation.slice(4), actor: identity.actor, sessionId, graphId: request.graphId, nodeId: request.nodeId, messageId: request.messageId, expectedToken: request.expectedToken }, env, config);
  }
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
  if (operation === 'identity-migration-preview') {
    const ledger = await readLedger(ledgerPath(env));
    const snapshot = await daemonRequest('ps', {}, { env });
    const rows = [];
    const ids = new Set([...Object.keys(ledger.remoteNames), ...Object.keys(ledger.sessionBindings || {}), ...Object.values(ledger.remoteBindings).map(binding => binding.sessionId)]);
    for (const sessionId of ids) {
      const selected = await canonicalSessionIdentityFor(sessionId, env);
      const legacy = (snapshot.agents || []).filter(agent => agent.uri !== selected.actor && agent.uri?.split(':').slice(0, 3).join(':') === selected.actor.split(':').slice(0, 3).join(':') && ['dsh-web:' + sessionId, 'dsh-remote:' + sessionId].includes(agent.lastSessionId));
      rows.push({ sessionId, actor: selected.actor, sessionRef: selected.sessionRef, persisted: Boolean(ledger.sessionBindings?.[sessionId]), enabled: ledger.sessionBindings?.[sessionId]?.enabled ?? null, legacyActors: legacy.map(agent => ({ actor: agent.uri, sessionRef: agent.lastSessionId, status: agent.status })), adapters: Object.entries(ledger.remoteBindings).filter(([, binding]) => binding.sessionId === sessionId).map(([adapter]) => adapter) });
    }
    return { ok: true, readOnly: true, sessions: rows };
  }
  if (operation === 'carrier-list') {
    const ledger = await readLedger(ledgerPath(env));
    const sessions = Object.entries(ledger.sessionBindings || {}).map(([sessionId, b]) => ({ sessionId, actor: b.actor, enabled: b.enabled, humanChat: b.humanChat === true }));
    // Adopt only an explicitly named, already registered legacy work session.
    // Historical anonymous web chats are never implicitly resumed as agents.
    const missing = Object.entries(ledger.remoteNames).filter(([id]) => !ledger.sessionBindings?.[id]);
    if (missing.length) {
      const snapshot = await daemonRequest('ps', {}, { env });
      for (const [sessionId, name] of missing) {
        const actor = `agent:${snapshot.daemon.owner}:${snapshot.daemon.nodeId}:${name}`;
        const registered = snapshot.interactiveSessions?.some(item => item.actor === actor && item.sessionRef === 'dsh-remote:' + sessionId && item.source === SOURCE);
        if (registered) sessions.push({ sessionId, actor, enabled: true, humanChat: false, legacy: true });
      }
    }
    return { ok: true, sessions };
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
      await registerSession(sessionId, identity, env);
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
    if (request.tool === 'inbox') {
      const allowedPrincipals = new Set();
      for (const binding of await remoteBindings(ledgerPath(env))) {
        if (binding.actor === identity.actor && binding.sessionRef === identity.sessionRef)
          for (const principal of remoteAdapterPrincipals(binding.adapter, identity)) allowedPrincipals.add(principal);
      }
      return pendingFor(identity, env, { allowedPrincipals });
    }
    if (request.tool === 'send') {
      const target = requiredString(args.target, 'target', 2048);
      const message = requiredString(args.message, 'message', 256 * 1024);
      if (!canonicalAgentPrincipal(target)) throw new BridgeError('INVALID_PARTICIPANT', 'target must be an exact four-part Agent URI');
      contactAddress(target);
      const contacts = await participantSet(ledgerPath(env), identity);
      if (!contacts.has(target)) {
        const directory = await daemonRequest('targets', { kind: 'agent' }, { env });
        if (!directory.targets?.some(item => item.targetKind === 'agent' && item.targetUri === target && item.deliverable !== false)) throw new BridgeError('PARTICIPANT_NOT_AVAILABLE', 'target is not listed or deliverable; save its exact URI as a contact to attempt delivery');
      }
      // Saved addresses need no live directory entry. Only the daemon decides
      // whether delivery is queued, rejected, or accepted; return its result.
      await updateParticipants(ledgerPath(env), identity, current => [...current, target]);
      return daemonRequest('message.send', fenced(identity, { to: [target], message }), { mutation: true, env });
    }
    const messageId = requiredString(args.messageId, 'messageId', 4096);
    const message = request.tool === 'reply' ? requiredString(args.message, 'message', 256 * 1024) : '';
    return settleMessage(identity, env, { messageId, message, tool: request.tool });
  }
  const remote = operation.startsWith("remote-");
  const normalizedOperation = remote ? operation.slice("remote-".length) : operation;
  if (normalizedOperation === "name-configure") {
    const current = await canonicalSessionIdentityFor(sessionId, env, "remote");
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
  // Entry transport never selects a second identity once a binding exists.
  const identity = await canonicalSessionIdentityFor(sessionId, env, remote ? 'remote' : 'web');

  if (operation === 'carrier-heartbeat') {
    const binding = (await readLedger(ledgerPath(env))).sessionBindings?.[sessionId];
    if (!binding?.enabled) return { ok: true, disabled: true };
    try {
      const result = await daemonRequest('session.heartbeat', fenced(identity), { mutation: true, env });
      const migrationWarnings = await retireDrainedAliases(sessionId, identity, env);
      return { ...result, migrationWarnings };
    } catch (error) {
      if (error.code === 'STALE_DAEMON_GENERATION') return daemonRequest('session.refresh', fenced(identity), { mutation: true, env });
      if (error.code === 'STALE_SESSION') {
        const snapshot = await daemonRequest('ps', {}, { env });
        if ((snapshot.interactiveSessions || []).some(item => item.actor === identity.actor && item.sessionRef !== identity.sessionRef)) throw new BridgeError('SESSION_SUPERSEDED', 'another session owns this Agent');
        return registerSession(sessionId, identity, env, binding.humanChat === true);
      }
      if (error.code === 'SESSION_SUPERSEDED') {
        await setSessionBinding(sessionId, identity, env, { enabled: false, error: error.code });
      }
      throw error;
    }
  }
  if (operation === 'carrier-pending') {
    const ledger = await readLedger(ledgerPath(env));
    if (!ledger.sessionBindings?.[sessionId]?.enabled) return { ok: true, messages: [] };
    const allowedPrincipals = new Set();
    for (const [adapter, binding] of Object.entries(ledger.remoteBindings)) {
      if (binding.sessionId === sessionId) for (const principal of remoteAdapterPrincipals(adapter, identity)) allowedPrincipals.add(principal);
    }
    return pendingFor(identity, env, { allowedPrincipals });
  }

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
    const ledger = await readLedger(ledgerPath(env));
    const humanChat = request.humanChat === true || Boolean(ledger.humanChats[ledgerKey(identity)]);
    return registerSession(sessionId, identity, env, humanChat);
  }

  if (normalizedOperation === "status") {
    return sessionStatus(sessionId, identity, env);
  }

  if (normalizedOperation === "pending") {
    if (!remote) {
      const selected = (await readLedger(ledgerPath(env))).sessionBindings?.[sessionId];
      if (request.observeOnly === true && selected?.enabled && !selected.humanChat) return { ok: true, messages: [], hostManaged: true };
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

  // Reception policy and its optional per-session whitelist. contact-add,
  // contact-remove, and contact-list remain thin deprecated aliases of the
  // whitelist-* operations over the same durable participants storage.
  const contactAliases = { "contact-add": "whitelist-add", "contact-remove": "whitelist-remove", "contact-list": "whitelist-list" };
  if (RECEPTION_OPERATIONS.has(normalizedOperation) || Object.hasOwn(contactAliases, normalizedOperation)) {
    const effective = contactAliases[normalizedOperation] || normalizedOperation;
    const entryField = Object.hasOwn(contactAliases, normalizedOperation) ? "contact" : "entry";
    const fields = effective === "reception-policy-set"
      ? ["operation", "sessionId", "policy"]
      : ["reception-policy-get", "whitelist-list"].includes(effective)
        ? ["operation", "sessionId"]
        : ["operation", "sessionId", entryField];
    if (Object.keys(request).some(key => !fields.includes(key))) {
      throw new BridgeError("INVALID_ARGUMENT", "unsupported reception field; client verification claims are not accepted");
    }
    if (remote) {
      const canonical = await canonicalSessionIdentityFor(sessionId, env);
      if (canonical.actor !== identity.actor || canonical.sessionRef !== identity.sessionRef) {
        throw new BridgeError("REMOTE_BINDING_MISMATCH", "remote reception policy requires this session's canonical remote identity");
      }
    }
    const fixedAllowedPrincipals = [...allowlist(env)].sort();
    const file = ledgerPath(env);
    let { policy, whitelist } = await receptionState(file, identity);
    if (effective === "reception-policy-set") {
      if (request.policy !== "open" && request.policy !== "whitelist") {
        throw new BridgeError("INVALID_POLICY", "policy must be exactly 'open' or 'whitelist'");
      }
      policy = await updateReceptionPolicy(file, identity, request.policy);
    } else if (effective !== "whitelist-list" && effective !== "reception-policy-get") {
      const entry = contactAddress(request[entryField]);
      // whitelist-add edits content only; it must not silently enable whitelist mode.
      whitelist = await updateParticipants(file, identity, current => effective === "whitelist-add"
        ? [...current, entry] : current.filter(item => item !== entry),
        { revokeAliases: effective === "whitelist-remove" });
    }
    return receptionPayload(identity, { policy, whitelist, fixedAllowedPrincipals });
  }

  // Legacy participant-* operations keep writing the same durable participants
  // array. They now only edit whitelist content: in the default 'open' policy
  // they no longer gate reception at all, and they never enable whitelist mode.
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
    const listed = targets.some((target) => (
      target &&
      typeof target === "object" &&
      target.targetKind === "agent" &&
      target.targetUri === participant
    ));
    if (!listed) {
      throw new BridgeError(
        "PARTICIPANT_NOT_AVAILABLE",
        "participant is not listed in the H2B agent target directory (listing is not authentication)",
      );
    }
    const participants = await updateParticipants(
      ledgerPath(env),
      identity,
      (current) => [...current, participant],
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
      { revokeAliases: true },
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
    const contacts = await participantSet(ledgerPath(env), identity);
    if (!contacts.has(target)) {
      throw new BridgeError(
        "CHAT_TARGET_NOT_SAVED",
        "chat target must be saved as a contact for this DSH Web session before binding",
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

  if (normalizedOperation === "mark-injected" || operation === "carrier-mark-injected") {
    const deliveryId = requiredString(request.deliveryId, "deliveryId", 4096);
    let allowedPrincipals = new Set();
    if (remote) {
      const adapter = remoteAdapter(request.adapter);
      await requireRemoteBinding(ledgerPath(env), adapter, sessionId, identity);
      allowedPrincipals = remoteAdapterPrincipals(adapter, identity);
    }
    if (operation === 'carrier-mark-injected') {
      const ledger = await readLedger(ledgerPath(env));
      for (const [adapter, binding] of Object.entries(ledger.remoteBindings)) {
        if (binding.sessionId === sessionId) for (const principal of remoteAdapterPrincipals(adapter, identity)) allowedPrincipals.add(principal);
      }
    }
    const pending = await pendingFor(identity, env, { allowedPrincipals });
    const message = pending.messages.find((item) => item.deliveryId === deliveryId);
    if (!message) {
      throw new BridgeError("DELIVERY_NOT_ALLOWED", "delivery is not pending for this session and allowlist");
    }
    const recorded = await recordInjected(ledgerPath(env), message.sourceIdentity || identity, deliveryId);
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
    if (canonicalAgentPrincipal(target)) {
      await updateParticipants(ledgerPath(env), identity, current => [...current, contactAddress(target)]);
    }
    return daemonRequest("message.send", fenced(identity, params), { mutation: true, env });
  }

  if (operation === 'remote-complete') {
    const canonical = await canonicalSessionIdentityFor(sessionId, env);
    if (canonical.actor !== identity.actor || canonical.sessionRef !== identity.sessionRef)
      throw new BridgeError('REMOTE_BINDING_MISMATCH', 'completion requires the canonical remote identity');
    const messageId = requiredString(request.messageId, 'messageId', 4096);
    const message = request.message === '' ? '' : requiredString(request.message, 'message', 256 * 1024);
    return settleMessage(identity, env, { messageId, message });
  }

  if (normalizedOperation === "reply") {
    const messageId = requiredString(request.messageId, "messageId", 4096);
    const message = requiredString(request.message, "message", 256 * 1024);
    return daemonRequest(
      "message.reply",
      fenced(await deliveryIdentity(identity, env, messageId), { messageId, message }),
      { mutation: true, env },
    );
  }

  if (normalizedOperation === "ack") {
    const messageId = requiredString(request.messageId, "messageId", 4096);
    return daemonRequest("message.ack", fenced(await deliveryIdentity(identity, env, messageId), { messageId }), { mutation: true, env });
  }

  if (normalizedOperation === "disconnect") {
    await setSessionBinding(sessionId, identity, env, { enabled: false });
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
