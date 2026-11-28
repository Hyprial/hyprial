/**
 * Explicit connection session provenance and exclusive local writer ownership.
 * PR2 accepts only daemon-built typed launch projections (synthetic in tests).
 * A claim is never stolen on PID/age heuristics. The SDK's awaited dispose seam
 * owns normal release because its RPC shutdown calls process.exit itself.
 */
import { createHash, randomUUID } from "node:crypto";
import {
  constants, closeSync, fstatSync, fsyncSync, lstatSync, mkdirSync, openSync,
  readFileSync, realpathSync, renameSync, rmdirSync, unlinkSync, writeFileSync,
} from "node:fs";
import { dirname, isAbsolute, join, relative, resolve, sep } from "node:path";

export function modelFailure(code, phase, reason) {
  return Object.assign(new Error(`${code}: ${phase}/${reason}`), { code, phase, reason });
}

function refuse(code = "MODEL_RESUME_MISMATCH", phase = "resume", reason = "binding-changed") {
  throw modelFailure(code, phase, reason);
}

function storageFailure() {
  refuse("MODEL_COMBINATION_UNSUPPORTED", "admission", "session-storage");
}

function regularJson(path) {
  let fd;
  try {
    const stat = lstatSync(path);
    if (!stat.isFile() || stat.isSymbolicLink() || stat.size > 65536) storageFailure();
    fd = openSync(path, constants.O_RDONLY | (constants.O_NOFOLLOW ?? 0));
    const opened = fstatSync(fd);
    if (opened.dev !== stat.dev || opened.ino !== stat.ino) storageFailure();
    const value = JSON.parse(readFileSync(fd, "utf8"));
    const after = fstatSync(fd);
    if (opened.size !== after.size || opened.mtimeMs !== after.mtimeMs) storageFailure();
    return value;
  } finally {
    if (fd !== undefined) closeSync(fd);
  }
}

function sameDirectory(path, stat) {
  const now = lstatSync(path);
  return now.isDirectory() && !now.isSymbolicLink() && now.dev === stat.dev && now.ino === stat.ino;
}

function containedDirectory(root, path) {
  const rel = relative(root, path);
  if (rel === ".." || rel.startsWith(`..${sep}`) || isAbsolute(rel)) refuse("MODEL_COMBINATION_UNSUPPORTED", "admission", "session-storage");
  let current = root;
  for (const part of rel.split(sep).filter(Boolean)) {
    current = join(current, part);
    const stat = lstatSync(current);
    if (!stat.isDirectory() || stat.isSymbolicLink()) refuse("MODEL_COMBINATION_UNSUPPORTED", "admission", "session-storage");
  }
  return realpathSync(path);
}

function exclusiveClaim(root, id) {
  const parent = join(root, ".hyprial-session-reservations");
  mkdirSync(parent, { recursive: true, mode: 0o700 });
  containedDirectory(root, parent);
  const path = join(parent, id);
  try { mkdirSync(path, { mode: 0o700 }); }
  catch (error) {
    if (error.code === "EEXIST") refuse("MODEL_SESSION_BUSY", "admission", "session-reserved");
    refuse("MODEL_COMBINATION_UNSUPPORTED", "admission", "session-storage");
  }
  const stat = lstatSync(path);
  const owner = { schemaVersion: 1, ownerToken: randomUUID() };
  const ownerPath = join(path, "owner.json");
  try { writeFileSync(ownerPath, JSON.stringify(owner), { flag: "wx", mode: 0o600 }); }
  catch {
    if (sameDirectory(path, stat)) {
      try { rmdirSync(path); } catch {}
    }
    refuse("MODEL_COMBINATION_UNSUPPORTED", "admission", "session-storage");
  }
  let released = false;
  return () => {
    if (released) return;
    try {
      containedDirectory(root, parent);
      if (!sameDirectory(path, stat)) storageFailure();
      const current = regularJson(ownerPath);
      if (current.schemaVersion !== 1 || current.ownerToken !== owner.ownerToken) storageFailure();
      unlinkSync(ownerPath);
      rmdirSync(path);
      released = true;
    } catch { storageFailure(); }
  };
}

const FIELDS = ["schemaVersion", "bindingRef", "bindingRevision", "authKind", "connectionRef",
  "configRevision", "model", "harness", "profile", "template", "contextWindow", "maxTokens", "costStatus"];

export function validateSnapshot(value) {
  if (!value || typeof value !== "object" || Array.isArray(value) ||
      Object.keys(value).length !== FIELDS.length || FIELDS.some((k) => !Object.hasOwn(value, k)) ||
      value.schemaVersion !== 1 || value.authKind !== "api-key" || value.harness !== "pi" ||
      value.profile !== "openai-chat-completions/v1" || value.template !== "openai-chat-text/v1" || value.costStatus !== "unknown" ||
      !/^[a-f0-9]{32}$/.test(value.connectionRef) ||
      !Number.isSafeInteger(value.bindingRevision) || value.bindingRevision < 1 ||
      !Number.isSafeInteger(value.configRevision) || value.configRevision < 1 ||
      typeof value.bindingRef !== "string" || !value.bindingRef || value.bindingRef.length > 256 ||
      typeof value.model !== "string" || !value.model || [...value.model].length > 256 || value.model.trim() !== value.model ||
      /\p{C}/u.test(value.model) || /\p{C}/u.test(value.bindingRef) ||
      !Number.isSafeInteger(value.contextWindow) || !Number.isSafeInteger(value.maxTokens) ||
      !(value.maxTokens > 0 && value.maxTokens < value.contextWindow && value.contextWindow <= 2147483647)) refuse();
  return value;
}

export function admitModelSession(command, options) {
  let root;
  try { root = realpathSync(options.sessionRoot); }
  catch { storageFailure(); }
  const id = createHash("sha256").update(options.sessionId).digest("hex");
  const receiptPath = join(root, ".hyprial-loader-receipts", `${id}.json`);
  const binding = options.modelBinding;
  if (binding !== undefined && command !== "run-rpc") refuse("MODEL_COMBINATION_UNSUPPORTED", "admission", "mode");
  let release;
  try {
    if (binding !== undefined) {
      validateSnapshot(binding);
      const home = realpathSync(options.agentHome);
      containedDirectory(home, resolve(options.sessionRoot));
      if (root !== realpathSync(options.managedSessionRoot) || root === home || resolve(options.receiptPath) !== receiptPath) refuse("MODEL_COMBINATION_UNSUPPORTED", "admission", "session-storage");
    }
    release = exclusiveClaim(root, id);
    let previous;
    try {
      containedDirectory(root, dirname(receiptPath));
      previous = regularJson(receiptPath);
      if (previous?.schema !== "hyprial-pi-loader-v1") refuse();
    } catch (error) {
      if (error instanceof SyntaxError) refuse();
      if (error.code !== "ENOENT") {
        if (error.code === "MODEL_RESUME_MISMATCH") throw error;
        storageFailure();
      }
    }
    if (binding === undefined) {
      if (previous?.modelBinding !== undefined) refuse();
    } else if (previous?.modelBinding !== undefined) {
      validateSnapshot(previous.modelBinding);
      if (FIELDS.some((k) => k !== "bindingRevision" && binding[k] !== previous.modelBinding[k])) {
        refuse("MODEL_RESUME_MISMATCH", "resume", binding.model !== previous.modelBinding.model ? "model-changed" : "binding-changed");
      }
    } else if (previous || options.requireExistingSession) {
      refuse("MODEL_RESUME_MISMATCH", "resume", "provenance-missing");
    }
    if (binding !== undefined) {
      const provider = "hyprial-connection-" + createHash("sha256").update(JSON.stringify([binding.connectionRef, binding.configRevision])).digest("hex");
      if (options.modelProvider !== provider || options.model !== binding.model) refuse();
      if (!/^[!-~]{1,16384}$/.test(process.env.HYPRIAL_PI_CONNECTION_KEY ?? "")) {
        refuse("MODEL_AUTH_UNAVAILABLE", "authorize", "missing");
      }
    }
    const snapshot = binding === undefined ? undefined : previous?.modelBinding ?? { ...binding };
    let retained = false;
    return {
      release() { if (!retained) release(); },
      retain() { retained = true; },
      snapshot,
      checkExisting(exists) {
        if (binding === undefined) return;
        if (exists && !previous?.modelBinding) refuse("MODEL_RESUME_MISMATCH", "resume", "provenance-missing");
        if (!exists && options.requireExistingSession) refuse("MODEL_RESUME_MISMATCH", "resume", "provenance-missing");
      },
      writeReceipt(receipt) {
        const parent = dirname(receiptPath);
        const temporary = `${receiptPath}.tmp-${randomUUID()}`;
        let fd;
        try {
          mkdirSync(parent, { recursive: true, mode: 0o700 });
          containedDirectory(root, parent);
          fd = openSync(temporary, "wx", 0o600);
          writeFileSync(fd, `${JSON.stringify({ ...receipt, ...(snapshot ? { modelBinding: snapshot } : {}) })}\n`);
          fsyncSync(fd);
          closeSync(fd); fd = undefined;
          renameSync(temporary, receiptPath);
          if (snapshot === undefined) release();
        } catch {
          storageFailure();
        } finally {
          try {
            if (fd !== undefined) closeSync(fd);
            unlinkSync(temporary);
          } catch (error) { if (error.code !== "ENOENT") storageFailure(); }
        }
      },
    };
  } catch (error) {
    if (release) release();
    if (!error.code?.startsWith("MODEL_")) storageFailure();
    throw error;
  }
}

export function guardModelSession(claim) {
  if (!claim.snapshot) return { attach() {}, close() {} };
  let runtime;
  let signal;
  let shutdown;
  const stop = () => {
    if (!runtime) return;
    shutdown ??= runtime.dispose().then(() => process.exit(signal === "SIGHUP" ? 129 : 143), () => {
      process.stderr.write("HYPRIAL_PI_START_FAILURE:MODEL_COMBINATION_UNSUPPORTED\nadmission/session-storage\n");
      process.exit(1);
    });
  };
  const handlers = ["SIGTERM", ...(process.platform === "win32" ? [] : ["SIGHUP"])].map((name) => {
    const handler = () => { signal ??= name; stop(); };
    process.on(name, handler);
    return [name, handler];
  });
  return {
    async attach(value) {
      runtime = bindRuntimeLifetime(value, claim);
      if (signal) { stop(); await shutdown; }
    },
    close() { for (const [name, handler] of handlers) process.off(name, handler); },
  };
}

export function bindRuntimeLifetime(runtime, claim) {
  if (!claim.snapshot) return runtime;
  const dispose = runtime.dispose.bind(runtime);
  let disposal;
  runtime.dispose = () => {
    disposal ??= (async () => {
      process.stdin.pause();
      process.stdin.removeAllListeners("end");
      process.stdin.removeAllListeners("data");
      await runtime.session.abort();
      await dispose();
      claim.release();
    })();
    return disposal;
  };
  for (const name of ["newSession", "switchSession", "fork", "importSession"]) {
    if (typeof runtime[name] === "function") runtime[name] = async () => refuse("MODEL_RESUME_MISMATCH", "resume", "binding-changed");
  }
  const setModel = runtime.session.setModel.bind(runtime.session);
  const selected = runtime.session.model;
  runtime.session.setModel = async (model) => {
    if (model.provider !== selected.provider || model.id !== selected.id) refuse();
    return setModel(model);
  };
  runtime.session.cycleModel = async () => null;
  const stats = runtime.session.getSessionStats.bind(runtime.session);
  runtime.session.getSessionStats = () => ({ ...stats(), cost: null, costStatus: "unknown" });
  return runtime;
}

export function boundModelRuntime(runtime, provider, modelId, definition) {
  const transport = globalThis.fetch;
  const target = new URL(`${definition.baseUrl.replace(/\/$/, "")}/chat/completions`).href;
  async function responseCode(response) {
    const reader = response.clone().body?.getReader();
    if (!reader) return undefined;
    let bytes = 0;
    const chunks = [];
    try {
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        bytes += value.length;
        if (bytes > 8192) return undefined;
        chunks.push(Buffer.from(value));
      }
      return JSON.parse(Buffer.concat(chunks).toString("utf8"))?.error?.code;
    } catch { return undefined; }
    finally { void reader.cancel().catch(() => {}); }
  }
  function redactMessage(message, reason) {
    if (!message || typeof message !== "object") return;
    if (message.usage && typeof message.usage === "object") {
      message.usage.cost = { input: null, output: null, cacheRead: null, cacheWrite: null, total: null };
      message.usage.costStatus = "unknown";
    }
    if (message.stopReason === "error") {
      const text = String(message.errorMessage ?? "");
      if (/^MODEL_UPSTREAM_ERROR: request\/(model-not-found|authentication-rejected|rate-limited|request-rejected|invalid-response|unreachable)$/.test(text)) return;
      message.errorMessage = `MODEL_UPSTREAM_ERROR: request/${reason ?? "invalid-response"}`;
      message.content = [];
    }
  }
  function sanitize(event, reason) {
    for (const key of ["partial", "message", "error"]) redactMessage(event?.[key], reason);
    return event;
  }
  for (const name of ["stream", "streamSimple"]) {
    const stream = runtime[name].bind(runtime);
    runtime[name] = (model, context, options) => {
      if (model.provider !== provider || model.id !== modelId || model.baseUrl !== definition.baseUrl ||
          model.api !== definition.api || Object.keys(model.headers ?? {}).length ||
          Object.keys(options?.headers ?? {}).length || options?.fetch) refuse();
      let failureReason;
      const key = process.env.HYPRIAL_PI_CONNECTION_KEY;
      const fetch = async (input, init) => {
        const url = typeof input === "string" || input instanceof URL ? String(input) : input.url;
        const headers = new Headers(init?.headers ?? input.headers);
        if (new URL(url).href !== target || headers.get("authorization") !== `Bearer ${key}` ||
            JSON.parse(init.body).model !== modelId) refuse();
        let response;
        try { response = await transport(input, { ...init, redirect: "error" }); }
        catch (error) { failureReason = "unreachable"; throw error; }
        if (!response.ok) {
          failureReason = response.status === 401 || response.status === 403 ? "authentication-rejected" :
            response.status === 429 ? "rate-limited" : "request-rejected";
          if ((response.status === 400 || response.status === 404) &&
              await responseCode(response) === "model_not_found") failureReason = "model-not-found";
        }
        return response;
      };
      const source = stream(model, context, { ...options, apiKey: key, fetch });
      return {
        async *[Symbol.asyncIterator]() { for await (const event of source) yield sanitize(event, failureReason); },
        async result() { const message = await source.result(); redactMessage(message, failureReason); return message; },
      };
    };
  }
}
