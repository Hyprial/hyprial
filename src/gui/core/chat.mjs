// Chat state for the browser App Core.
//
// Authority boundaries this module keeps:
//  - it is a projection only: sessions, history, progress and terminal state are
//    whatever the injected port returned, never inferred or cached as a local
//    authority;
//  - an accepted send is an acknowledgement, not a completed turn, and no reply
//    is synthesised from it — a message is displayed only once returned
//    history/events contains it;
//  - a send whose outcome is unknown keeps its original idempotency key and
//    payload for a manual retry; Core never starts a new request by itself;
//  - the connection is only claimed while the node answers: a refresh whose
//    session list fails turns the visible state to failed instead of leaving a
//    stale "connected" claim standing, and the same refresh control then
//    performs a real re-connect (the cached connect promise is dropped with
//    the connection);
//  - every asynchronous answer is checked against the connection generation,
//    the session selection and the request generation it belongs to, so a late
//    answer can never resurrect a disconnected connection or overwrite a newer
//    projection;
//  - it holds no durable storage, no identity and no business execution.
import { assertKnownFields, assertNoIdentityFields, bindConnection, fail } from './connection.mjs';
import { bindDemoSelection } from './demo.mjs';

const OPTION_FIELDS = Object.freeze(['port', 'transport', 'demo', 'powerPack', 'pageSize', 'now', 'newIdempotencyKey']);
const DEFAULT_PAGE_SIZE = 50;
const MAX_PAGE_SIZE = 200;
const MAX_TEXT_LENGTH = 8000;
const MIN_KEY_LENGTH = 8;
const MAX_KEY_LENGTH = 128;

// A rejection that names one of these is a decision, not an unknown outcome:
// resending the identical request cannot change it, so retry is not offered.
const DEFINITIVE_SEND_CODES = new Set([
  'INVALID_ARGUMENT', 'INVALID_CONTEXT', 'IDEMPOTENCY_CONFLICT', 'SESSION_NOT_FOUND', 'COMMAND_NOT_FOUND',
  'UNAUTHENTICATED', 'UNAUTHORIZED', 'FORBIDDEN', 'GUI_UNAUTHENTICATED', 'GUI_FORBIDDEN', 'GUI_ARGUMENT_REJECTED',
]);

function describe(error) {
  return {
    code: typeof error?.code === 'string' && error.code ? error.code : 'CORE_TRANSPORT_ERROR',
    message: typeof error?.message === 'string' && error.message ? error.message : String(error),
  };
}

function freeze(value) {
  if (!value || typeof value !== 'object') return value;
  for (const item of Object.values(value)) freeze(item);
  return Object.freeze(value);
}

function copy(value) {
  return value === undefined ? undefined : (typeof structuredClone === 'function' ? structuredClone(value) : JSON.parse(JSON.stringify(value)));
}

function defaultIdempotencyKey() {
  const runtime = globalThis.crypto;
  if (!runtime || typeof runtime.randomUUID !== 'function') {
    fail('CORE_RUNTIME_UNAVAILABLE', 'This runtime cannot generate an idempotency key; inject newIdempotencyKey');
  }
  return runtime.randomUUID();
}

function normalizeText(text) {
  if (typeof text !== 'string') fail('CORE_ARGUMENT_INVALID', 'A message must be a string');
  if (!text.trim()) fail('CORE_ARGUMENT_INVALID', 'A message cannot be empty');
  if (text.length > MAX_TEXT_LENGTH) fail('CORE_ARGUMENT_INVALID', `A message cannot exceed ${MAX_TEXT_LENGTH} characters`);
  return text;
}

function normalizeKey(key) {
  if (typeof key !== 'string' || key.trim().length < MIN_KEY_LENGTH || key.length > MAX_KEY_LENGTH) {
    fail('CORE_IDEMPOTENCY_KEY_INVALID', `An idempotency key must be ${MIN_KEY_LENGTH}..${MAX_KEY_LENGTH} characters; no request was sent`);
  }
  return key;
}

function normalizePageSize(pageSize) {
  const value = pageSize === undefined ? DEFAULT_PAGE_SIZE : pageSize;
  if (!Number.isSafeInteger(value) || value < 1 || value > MAX_PAGE_SIZE) {
    fail('CORE_ARGUMENT_INVALID', `pageSize must be an integer between 1 and ${MAX_PAGE_SIZE}`);
  }
  return value;
}

// Only rows the port actually listed are selectable; anything without a string
// session id is dropped instead of being repaired into a fake identity.
function normalizeSessions(rows) {
  if (!Array.isArray(rows)) return [];
  return rows
    .filter(row => row && typeof row === 'object' && typeof row.sessionId === 'string' && row.sessionId)
    .map(row => ({
      sessionId: row.sessionId,
      title: typeof row.title === 'string' && row.title ? row.title : row.sessionId,
      updatedAt: row.updatedAt ?? null,
      state: row.state ?? null,
      ...(row.simulation === true ? { simulation: true } : {}),
    }));
}

function normalizeEvents(events) {
  if (!Array.isArray(events)) return [];
  return events
    .filter(event => event && typeof event === 'object')
    .map(event => ({
      eventId: typeof event.eventId === 'string' ? event.eventId : null,
      kind: typeof event.kind === 'string' ? event.kind : 'unknown',
      text: typeof event.text === 'string' ? event.text : '',
      at: event.at ?? null,
      ...(event.simulation === true ? { simulation: true } : {}),
    }));
}

// Pages may overlap; an event that is already on screen is not appended twice.
// Events without an id cannot be identified, so they are appended as-is.
function appendEvents(existing, incoming) {
  const known = new Set(existing.map(message => message.eventId).filter(eventId => eventId !== null));
  return [...existing, ...incoming.filter(message => {
    if (message.eventId === null) return true;
    if (known.has(message.eventId)) return false;
    known.add(message.eventId);
    return true;
  })];
}

function normalizeEntries(entries) {
  if (!Array.isArray(entries)) return [];
  return entries
    .filter(entry => entry && typeof entry === 'object')
    .map(entry => ({ ...entry, simulation: entry.simulation === true }));
}

function bindPowerPack(powerPack) {
  if (powerPack === undefined) return Object.freeze({ available: false });
  if (!powerPack || typeof powerPack !== 'object' || Array.isArray(powerPack)) {
    fail('CORE_ARGUMENT_INVALID', 'powerPack must be an injected capability descriptor object');
  }
  for (const key of Object.keys(powerPack)) {
    if (key !== 'available') {
      fail('CORE_ARGUMENT_INVALID', `powerPack exposes '${key}'; this slice reports availability only and runs no local process management`);
    }
  }
  if (typeof powerPack.available !== 'boolean') fail('CORE_ARGUMENT_INVALID', 'powerPack.available must be a boolean');
  return Object.freeze({ available: powerPack.available });
}

// A success claim is only accepted if it names the request it accepted; a
// malformed receipt is an unknown outcome (retryable with the same key), never
// an acknowledgement.
function receiptProblem(receipt) {
  if (!receipt || typeof receipt !== 'object' || Array.isArray(receipt)) return 'The node returned no send receipt';
  if (receipt.status !== 'accepted') return `The node returned status '${String(receipt.status)}' instead of accepted`;
  if (typeof receipt.commandId !== 'string' || !receipt.commandId.trim()) return 'The node returned an empty command id';
  return null;
}

function classifySendFailure(error) {
  const problem = describe(error);
  const definitive = error?.definitive === true || DEFINITIVE_SEND_CODES.has(problem.code);
  return { state: definitive ? 'failed' : 'uncertain', error: problem };
}

/**
 * Build a chat core over an injected connection port.
 *
 * Synchronous by design: a misconfigured port, unknown transport kind or any
 * attempt to inject identity throws here rather than producing a core that
 * silently serves nothing.
 */
export function createChatCore(options = {}) {
  if (!options || typeof options !== 'object' || Array.isArray(options)) {
    fail('CORE_ARGUMENT_INVALID', 'createChatCore requires an options object');
  }
  assertNoIdentityFields(options);
  assertKnownFields(options, OPTION_FIELDS, 'createChatCore');

  const pageSize = normalizePageSize(options.pageSize);
  const powerPack = bindPowerPack(options.powerPack);
  const now = options.now ?? (() => new Date().toISOString());
  const generateKey = options.newIdempotencyKey ?? defaultIdempotencyKey;
  if (typeof now !== 'function') fail('CORE_ARGUMENT_INVALID', 'now must be a function');
  if (typeof generateKey !== 'function') fail('CORE_ARGUMENT_INVALID', 'newIdempotencyKey must be a function');

  const explicitDemo = options.demo !== undefined;
  if (explicitDemo && (options.port !== undefined || options.transport !== undefined)) {
    fail('CORE_ARGUMENT_INVALID', 'Pass either an injected port/transport or one explicit demo selection, never both');
  }
  const connection = explicitDemo
    ? bindDemoSelection(options.demo)
    : bindConnection({ port: options.port, transport: options.transport });

  let state = {
    phase: 'disconnected',
    demo: explicitDemo,
    transport: connection.transport,
    powerPack,
    pageSize,
    capabilities: null,
    sessions: [],
    selectedSessionId: null,
    messages: [],
    history: { loading: false, hasMore: false, nextCursor: null, error: null },
    progress: { entries: [], terminal: null },
    lastSend: null,
    lastError: null,
  };
  const listeners = new Set();
  let selectionEpoch = 0;
  // Per-session request generations: within one session the newest history or
  // progress request wins, so an older answer can never overwrite a newer one.
  const historyGenerations = new Map();
  const progressGenerations = new Map();
  // Bumped by disconnect(): every in-flight answer from an older generation is
  // discarded instead of being applied to the new connection state.
  let connectionGeneration = 0;
  let connectPromise = null;
  let sendGeneration = 0;
  let refreshGeneration = 0;

  function snapshot() {
    return freeze(copy(state));
  }

  function patch(next) {
    state = { ...state, ...next };
    const current = snapshot();
    for (const listener of listeners) listener(current);
  }

  const currentConnection = generation => generation === connectionGeneration;

  function requireConnected() {
    if (state.phase !== 'connected') {
      fail('CORE_ARGUMENT_INVALID', 'The connection is not established; call connect() first');
    }
  }

  function nextGeneration(map, sessionId) {
    const next = (map.get(sessionId) ?? 0) + 1;
    map.set(sessionId, next);
    return next;
  }

  // A request is applied only if it is still the newest request for its kind and
  // session, still the selected session, and still on the current connection.
  function isCurrent({ map, generation, epoch, lifecycle, sessionId }) {
    return generation === map.get(sessionId) && epoch === selectionEpoch
      && lifecycle === connectionGeneration && state.selectedSessionId === sessionId;
  }

  async function loadHistory(sessionId, { epoch, lifecycle, cursor = null }) {
    if (epoch !== selectionEpoch || lifecycle !== connectionGeneration || state.selectedSessionId !== sessionId) return;
    const generation = nextGeneration(historyGenerations, sessionId);
    const keep = { loading: true, error: null };
    patch({ history: { ...state.history, ...keep } });
    let page;
    try {
      page = await connection.sessions.history(sessionId, cursor ? { cursor, limit: pageSize } : { limit: pageSize });
    } catch (error) {
      if (!isCurrent({ map: historyGenerations, generation, epoch, lifecycle, sessionId })) return;
      // Keep the previous cursor and hasMore: a failed page must stay retryable.
      patch({ history: { ...state.history, loading: false, error: describe(error) }, lastError: describe(error) });
      return;
    }
    if (!isCurrent({ map: historyGenerations, generation, epoch, lifecycle, sessionId })) return;
    const events = normalizeEvents(page?.events);
    const nextCursor = typeof page?.nextCursor === 'string' && page.nextCursor ? page.nextCursor : null;
    patch({
      // A first page is the canonical projection; a later page only adds to it.
      messages: appendEvents(cursor ? state.messages : [], events),
      history: { loading: false, hasMore: Boolean(nextCursor), nextCursor, error: null },
    });
  }

  async function loadEvents(sessionId, { epoch, lifecycle }) {
    if (epoch !== selectionEpoch || lifecycle !== connectionGeneration || state.selectedSessionId !== sessionId) return;
    const generation = nextGeneration(progressGenerations, sessionId);
    const current = () => isCurrent({ map: progressGenerations, generation, epoch, lifecycle, sessionId });
    try {
      const result = await connection.sessions.events(sessionId, 0);
      if (!current()) return;
      patch({ progress: { entries: normalizeEntries(result?.entries), terminal: result?.terminal ? copy(result.terminal) : null } });
    } catch (error) {
      if (!current()) return;
      patch({ lastError: describe(error) });
    }
  }

  function recordUncertain({ sessionId, text, idempotencyKey, error }) {
    patch({
      lastSend: { state: 'uncertain', sessionId, text, idempotencyKey, error },
      lastError: error,
    });
  }

  async function deliver({ sessionId, text, idempotencyKey }) {
    const lifecycle = connectionGeneration;
    const attempt = ++sendGeneration;
    patch({ lastSend: { state: 'sending', sessionId, text, idempotencyKey } });
    let receipt;
    try {
      receipt = await connection.sessions.send(sessionId, { text, idempotencyKey });
    } catch (error) {
      if (attempt !== sendGeneration || !currentConnection(lifecycle)) return snapshot();
      const failure = classifySendFailure(error);
      if (failure.state === 'failed' && currentConnection(lifecycle)) {
        patch({ lastSend: { state: 'failed', sessionId, text, idempotencyKey, error: failure.error } });
      } else {
        recordUncertain({ sessionId, text, idempotencyKey, error: failure.error });
      }
      return snapshot();
    }
    // The connection changed while the request was in flight: the request may
    // have reached the node, so the outcome is unknown — never accepted.
    if (attempt !== sendGeneration || !currentConnection(lifecycle)) return snapshot();
    const problem = receiptProblem(receipt);
    if (problem) {
      recordUncertain({ sessionId, text, idempotencyKey, error: { code: 'CORE_RECEIPT_INVALID', message: problem } });
      return snapshot();
    }
    patch({
      lastSend: {
        state: 'accepted',
        sessionId,
        text,
        idempotencyKey,
        commandId: receipt.commandId,
        acceptedAt: receipt.acceptedAt ?? now(),
        deduplicated: receipt.deduplicated === true,
        simulation: receipt.simulation === true,
      },
      lastError: null,
    });
    // Re-reading is not re-sending: this is the only way a reply can appear.
    if (state.selectedSessionId === sessionId) await refresh();
    return snapshot();
  }

  async function loadSessions(lifecycle, request) {
    try {
      const sessions = await connection.sessions.list();
      if (!currentConnection(lifecycle) || request !== refreshGeneration) return false;
      patch({ sessions: normalizeSessions(sessions) });
      return true;
    } catch (error) {
      if (!currentConnection(lifecycle) || request !== refreshGeneration) return false;
      // The session list is this connection's health probe: a node that cannot
      // answer it is not connected, and a stale "connected" claim is a lie the
      // user acts on.  The refresh control doubles as the reconnect path, so
      // the cached connect promise is dropped together with the connection;
      // the last projection is kept but only presented as failed.
      connectPromise = null;
      patch({ phase: 'error', lastError: describe(error) });
      return false;
    }
  }

  async function refresh() {
    if (state.phase === 'error' || state.phase === 'disconnected') return api.connect();
    if (state.phase !== 'connected') return snapshot();
    const lifecycle = connectionGeneration;
    // Captured before any await: a refresh refreshes what was on screen when the
    // user asked, and a later selection switch invalidates this answer instead
    // of the other way round.
    const sessionId = state.selectedSessionId;
    const epoch = selectionEpoch;
    const request = ++refreshGeneration;
    if (!await loadSessions(lifecycle, request)) return snapshot();
    if (!sessionId) return snapshot();
    await loadHistory(sessionId, { epoch, lifecycle });
    if (request !== refreshGeneration) return snapshot();
    await loadEvents(sessionId, { epoch, lifecycle });
    return snapshot();
  }

  const api = {
    state: snapshot,
    subscribe(listener) {
      if (typeof listener !== 'function') fail('CORE_ARGUMENT_INVALID', 'subscribe requires a listener function');
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    /** Establish the injected connection. Startup failures reject and are visible. */
    connect() {
      if (connectPromise) return connectPromise;
      const lifecycle = connectionGeneration;
      connectPromise = (async () => {
        patch({ phase: 'connecting', lastError: null });
        let capabilities;
        let sessions;
        try {
          [capabilities, sessions] = await Promise.all([
            connection.capabilities.get(),
            connection.sessions.list(),
          ]);
        } catch (error) {
          if (!currentConnection(lifecycle)) throw Object.assign(new Error('The connection was closed while connecting'), { code: 'CORE_DISCONNECTED' });
          patch({ phase: 'error', lastError: describe(error) });
          throw error;
        }
        if (!currentConnection(lifecycle)) {
          // disconnect() happened while connecting: stay disconnected.
          throw Object.assign(new Error('The connection was closed while connecting'), { code: 'CORE_DISCONNECTED' });
        }
        patch({
          capabilities: copy(capabilities) ?? null,
          sessions: normalizeSessions(sessions),
          phase: 'connected',
          lastError: null,
        });
        return snapshot();
      })();
      const pending = connectPromise;
      pending.catch(() => { if (connectPromise === pending) connectPromise = null; });
      return connectPromise;
    },
    /**
     * Drop every local projection and invalidate in-flight answers. The one
     * thing kept is an unresolved send: its idempotency key and payload stay in
     * memory so the same request can be retried inside this instance. A new
     * Core instance, page reload or app restart loses that key — only the node's
     * own deduplication can make an unknown request safe to repeat then.
     */
    disconnect() {
      connectPromise = null;
      connectionGeneration += 1;
      sendGeneration += 1;
      refreshGeneration += 1;
      selectionEpoch += 1;
      const send = state.lastSend;
      const retained = send && (send.state === 'sending' || send.state === 'uncertain')
        ? { state: 'uncertain', sessionId: send.sessionId, text: send.text, idempotencyKey: send.idempotencyKey, error: send.error ?? { code: 'CORE_DISCONNECTED', message: 'The connection was closed before the outcome was known' } }
        : null;
      patch({
        phase: 'disconnected', capabilities: null, sessions: [], selectedSessionId: null,
        messages: [], history: { loading: false, hasMore: false, nextCursor: null, error: null },
        progress: { entries: [], terminal: null }, lastSend: retained, lastError: null,
      });
      return snapshot();
    },
    async selectSession(sessionId) {
      requireConnected();
      if (typeof sessionId !== 'string' || !sessionId.trim()) fail('CORE_ARGUMENT_INVALID', 'selectSession requires a session id');
      // The node lists the sessions it authorises; the browser cannot open a
      // session identity the node never offered.
      if (!state.sessions.some(row => row.sessionId === sessionId)) {
        fail('CORE_SESSION_NOT_LISTED', 'The node did not list this session; the browser cannot invent a session identity');
      }
      const epoch = ++selectionEpoch;
      const lifecycle = connectionGeneration;
      patch({
        selectedSessionId: sessionId,
        messages: [],
        progress: { entries: [], terminal: null },
        history: { loading: true, hasMore: false, nextCursor: null, error: null },
      });
      await loadHistory(sessionId, { epoch, lifecycle });
      await loadEvents(sessionId, { epoch, lifecycle });
      return snapshot();
    },
    async loadMoreHistory() {
      requireConnected();
      const { selectedSessionId: sessionId, history } = state;
      if (!sessionId || !history.hasMore || !history.nextCursor) return snapshot();
      if (history.loading) fail('CORE_BUSY', 'History is already loading; the next page is not requested twice');
      await loadHistory(sessionId, { epoch: selectionEpoch, lifecycle: connectionGeneration, cursor: history.nextCursor });
      return snapshot();
    },
    /** Manual refresh of the session list, the selected history and its progress. */
    refresh,
    /** Send a new message. A new request always gets a new idempotency key. */
    async send(text) {
      requireConnected();
      if (state.lastSend && (state.lastSend.state === 'sending' || state.lastSend.state === 'uncertain')) {
        fail('CORE_BUSY', 'A previous send is still in flight or its outcome is unknown; retry or dismiss it first');
      }
      const sessionId = state.selectedSessionId;
      if (!sessionId) fail('CORE_ARGUMENT_INVALID', 'Select a session before sending');
      const message = normalizeText(text);
      // The key is validated before the request leaves: an unusable key must not
      // produce a request that cannot be identified or retried.
      const idempotencyKey = normalizeKey(generateKey());
      return deliver({ sessionId, text: message, idempotencyKey });
    },
    /** Retry the unresolved send with the identical key and payload. Never automatic. */
    async retrySend() {
      requireConnected();
      const previous = state.lastSend;
      if (!previous || previous.state !== 'uncertain') {
        fail('CORE_RETRY_NOT_AVAILABLE', 'There is no send with an unknown outcome to retry');
      }
      return deliver({ sessionId: previous.sessionId, text: previous.text, idempotencyKey: previous.idempotencyKey });
    },
    /** Give up on an unknown outcome without sending anything. */
    dismissUncertain() {
      if (!state.lastSend || state.lastSend.state !== 'uncertain') return snapshot();
      patch({ lastSend: null, lastError: null });
      return snapshot();
    },
  };
  return Object.freeze({ ...api });
}
