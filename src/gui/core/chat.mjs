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
//  - the target directory is a separate, bounded projection of the node's own
//    listed nodes/agents: it can fail without turning the chat connection into
//    a lie, and selecting an agent asks the node to create or reuse a stable
//    session binding rather than inventing a session id in the browser;
//  - it holds no durable storage, no identity and no business execution.
import { assertKnownFields, assertNoIdentityFields, bindConnection, fail } from './connection.mjs';
import { bindDemoSelection } from './demo.mjs';

const OPTION_FIELDS = Object.freeze([
  'port', 'transport', 'demo', 'powerPack', 'pageSize', 'now', 'newIdempotencyKey',
  'replyPollIntervalMs', 'replyTimeoutMs',
]);
const DEFAULT_PAGE_SIZE = 50;
const MAX_PAGE_SIZE = 200;
const MAX_TEXT_LENGTH = 8000;
const MIN_KEY_LENGTH = 8;
const MAX_KEY_LENGTH = 128;
// An accepted send is polled for a bounded window. Every read is a refresh of
// the node's own history/events, never a resend, and the loop stops on the
// first reply, terminal state, disconnect or deadline.
const DEFAULT_REPLY_POLL_INTERVAL_MS = 1000;
const DEFAULT_REPLY_TIMEOUT_MS = 60000;

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

// Startup reads two independent surfaces. Both failures are kept: on a first
// run the capability and session reads usually fail together, and showing only
// the first one hides the other half of the real diagnosis.
function combineConnectFailures(results) {
  const failures = results
    .filter(result => result.status === 'rejected')
    .map(result => describe(result.reason));
  const primary = failures[0];
  const message = [...new Set(failures.map(failure => `${failure.code}: ${failure.message}`))].join('；');
  return { code: primary.code, message };
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

function normalizeMilliseconds(value, fallback, name) {
  const milliseconds = value === undefined ? fallback : value;
  if (!Number.isSafeInteger(milliseconds) || milliseconds < 1) {
    fail('CORE_ARGUMENT_INVALID', `${name} must be a positive integer of milliseconds`);
  }
  return milliseconds;
}

// The reply projection starts from the messages already on screen. Only an
// assistant message that was not part of that baseline (or a later assistant
// row when the port emits id-less events) can prove a reply arrived.
function replyBaseline(messages, terminal) {
  return {
    ids: new Set(messages.map(message => message.eventId).filter(eventId => eventId !== null)),
    assistants: messages.filter(message => message.kind === 'assistant').length,
    // The terminal of a previous turn is not evidence about this one: only a
    // terminal that appeared or changed after the send can prove its outcome.
    terminal: terminal
      ? {
        terminal: terminal.terminal ?? null,
        eventId: typeof terminal.eventId === 'string' && terminal.eventId ? terminal.eventId : null,
      }
      : null,
  };
}

function isNewTerminal(baseline, terminal) {
  if (!terminal || typeof terminal !== 'object') return false;
  const eventId = typeof terminal.eventId === 'string' && terminal.eventId ? terminal.eventId : null;
  if (!baseline?.terminal) return true;
  if (eventId && baseline.terminal.eventId) return eventId !== baseline.terminal.eventId;
  if (eventId) return true;
  // Without an event id there is no way to tell a repeated terminal from a new
  // one; refuse to claim an outcome from it.
  return false;
}

function findNewReply(baseline, messages) {
  const assistants = messages.filter(message => message.kind === 'assistant');
  const identified = assistants.find(message => message.eventId !== null && !baseline.ids.has(message.eventId));
  if (identified) return identified;
  return assistants.length > baseline.assistants ? assistants[assistants.length - 1] : null;
}

function terminalFailure(terminal, terminalState) {
  const error = terminal?.error && typeof terminal.error === 'object' ? terminal.error : null;
  return {
    code: typeof error?.code === 'string' && error.code ? error.code : 'CORE_TURN_FAILED',
    message: typeof error?.message === 'string' && error.message
      ? error.message
      : typeof terminal?.message === 'string' && terminal.message
        ? terminal.message
        : `本轮状态为 ${terminalState}`,
  };
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

// The directory is a projection of the node's own listed rows. Malformed rows
// are dropped rather than repaired into an identity; every status string is
// passed through, because only the node may claim online/offline.
function normalizeTargets(value) {
  const nodes = Array.isArray(value?.nodes)
    ? value.nodes
      .filter(row => row && typeof row === 'object' && typeof row.nodeId === 'string' && row.nodeId)
      .map(row => ({
        nodeId: row.nodeId,
        status: typeof row.status === 'string' && row.status ? row.status : 'unknown',
      }))
    : [];
  const agents = Array.isArray(value?.targets)
    ? value.targets
      .filter(row => row && typeof row === 'object' && typeof row.targetUri === 'string' && row.targetUri)
      .map(row => ({
        targetUri: row.targetUri,
        targetKind: 'agent',
        status: typeof row.status === 'string' && row.status ? row.status : 'unknown',
        ...(row.hosted === true ? { hosted: true } : {}),
        ...(typeof row.hostedBy === 'string' && row.hostedBy ? { hostedBy: row.hostedBy } : {}),
      }))
    : [];
  const counts = value?.counts && typeof value.counts === 'object' ? value.counts : {};
  return {
    nodes,
    agents,
    truncated: value?.truncated === true || value?.truncated?.nodes === true || value?.truncated?.targets === true,
    counts: {
      nodes: Number.isSafeInteger(counts.nodes) ? counts.nodes : nodes.length,
      targets: Number.isSafeInteger(counts.targets) ? counts.targets : agents.length,
    },
  };
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
  const replyPollIntervalMs = normalizeMilliseconds(
    options.replyPollIntervalMs, DEFAULT_REPLY_POLL_INTERVAL_MS, 'replyPollIntervalMs',
  );
  const replyTimeoutMs = normalizeMilliseconds(options.replyTimeoutMs, DEFAULT_REPLY_TIMEOUT_MS, 'replyTimeoutMs');
  const maxReplyPolls = Math.max(1, Math.ceil(replyTimeoutMs / replyPollIntervalMs));
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
    targets: { phase: 'idle', nodes: [], agents: [], truncated: false, counts: { nodes: 0, targets: 0 }, error: null },
    selectedSessionId: null,
    messages: [],
    history: { loading: false, hasMore: false, nextCursor: null, failedCursor: null, error: null },
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
  let targetsGeneration = 0;
  // `replyContext` survives the polling deadline so a manual refresh can still
  // converge the notice; `replyWatch` is only the bounded background timer.
  let replyWatch = null;
  let replyContext = null;

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
    // Capture the failure this request supersedes before clearing it. Only that
    // exact failure may be resolved by a later success; a newer, unrelated
    // failure (for example a failed events read) must stay visible.
    const resolving = state.history.error;
    const keep = { loading: true, error: null };
    patch({ history: { ...state.history, ...keep } });
    let page;
    try {
      page = await connection.sessions.history(sessionId, cursor ? { cursor, limit: pageSize } : { limit: pageSize });
    } catch (error) {
      if (!isCurrent({ map: historyGenerations, generation, epoch, lifecycle, sessionId })) return;
      // Keep the previous cursor and hasMore retryable, and remember exactly
      // which page failed so retryHistory repeats that request rather than a
      // newer first-page refresh that happened to fail later. The same object
      // is used for both slots so the later success can clear only this error.
      const failure = describe(error);
      patch({
        history: { ...state.history, loading: false, failedCursor: cursor, error: failure },
        lastError: failure,
      });
      return;
    }
    if (!isCurrent({ map: historyGenerations, generation, epoch, lifecycle, sessionId })) return;
    const events = normalizeEvents(page?.events);
    const nextCursor = typeof page?.nextCursor === 'string' && page.nextCursor ? page.nextCursor : null;
    patch({
      // A first page is the canonical projection; a later page only adds to it.
      messages: appendEvents(cursor ? state.messages : [], events),
      history: { loading: false, hasMore: Boolean(nextCursor), nextCursor, failedCursor: null, error: null },
      // A successful request resolves the history failure it superseded:
      // keeping that exact failure as lastError would leave a stale banner
      // beside content that just loaded. Identity, not the mere presence of a
      // history error, keeps a newer unrelated failure visible.
      lastError: state.lastError === resolving ? null : state.lastError,
    });
    // A manual refresh is also a reply read: a late answer keeps converging
    // even after the bounded poll has given up.
    reconcileReply();
  }

  async function loadEvents(sessionId, { epoch, lifecycle }) {
    if (epoch !== selectionEpoch || lifecycle !== connectionGeneration || state.selectedSessionId !== sessionId) return;
    const generation = nextGeneration(progressGenerations, sessionId);
    const current = () => isCurrent({ map: progressGenerations, generation, epoch, lifecycle, sessionId });
    try {
      const result = await connection.sessions.events(sessionId, 0);
      if (!current()) return;
      patch({ progress: { entries: normalizeEntries(result?.entries), terminal: result?.terminal ? copy(result.terminal) : null } });
      reconcileReply();
    } catch (error) {
      if (!current()) return;
      patch({ lastError: describe(error) });
    }
  }

  // A reply is only ever the node's own row. Once one is on screen (or a new
  // terminal names this turn) the notice converges and the poll timer stops;
  // nothing is inferred from the acknowledgement itself and nothing is resent.
  function reconcileReply() {
    const context = replyContext;
    const send = state.lastSend;
    if (!context || !send || send.state !== 'accepted') return false;
    if (context.sessionId !== state.selectedSessionId || context.lifecycle !== connectionGeneration) return false;
    if (send.reply && send.reply.state !== 'waiting' && send.reply.state !== 'replied') return false;
    const assistant = findNewReply(context.baseline, state.messages);
    const terminal = isNewTerminal(context.baseline.terminal, state.progress.terminal) ? state.progress.terminal : null;
    if (!assistant && !terminal) return false;
    cancelReplyWatch();
    const reply = assistant ? { eventId: assistant.eventId } : {};
    if (terminal) {
      if (terminal.simulation === true || send.simulation === true) {
        reply.state = 'simulated';
        reply.terminal = terminal.terminal ?? 'simulated';
      } else if (terminal.terminal === 'completed') {
        reply.state = 'completed';
        reply.terminal = 'completed';
      } else {
        reply.state = 'failed';
        reply.terminal = terminal.terminal ?? 'failed';
        reply.error = terminalFailure(terminal, terminal.terminal);
      }
    } else {
      reply.state = 'replied';
    }
    patch({ lastSend: { ...send, reply } });
    return true;
  }

  function replyStateActive(send) {
    return Boolean(send && send.state === 'accepted'
      && (!send.reply || send.reply.state === 'waiting' || send.reply.state === 'replied'));
  }

  function replyWatchCurrent(watch) {
    const send = state.lastSend;
    return watch === replyWatch
      && watch.lifecycle === connectionGeneration
      && watch.sessionId === state.selectedSessionId
      && state.phase === 'connected'
      && replyStateActive(send)
      && send.sessionId === watch.sessionId;
  }

  function cancelReplyWatch() {
    if (replyWatch?.timer !== null && replyWatch?.timer !== undefined) clearTimeout(replyWatch.timer);
    replyWatch = null;
  }

  function clearReplyTracking() {
    cancelReplyWatch();
    replyContext = null;
  }

  function scheduleReplyPoll(watch) {
    if (!replyWatchCurrent(watch)) return;
    watch.timer = setTimeout(() => { void pollReply(watch); }, replyPollIntervalMs);
    // A pending read must never keep a CLI or test process alive on its own.
    if (typeof watch.timer?.unref === 'function') watch.timer.unref();
  }

  function expireReply(watch) {
    if (watch !== replyWatch) return;
    cancelReplyWatch();
    const send = state.lastSend;
    if (!replyStateActive(send)) return;
    patch({ lastSend: { ...send, reply: { ...(send.reply ?? {}), state: 'waiting', timedOut: true } } });
  }

  async function pollReply(watch) {
    watch.timer = null;
    if (!replyWatchCurrent(watch)) return;
    let page;
    let events;
    try {
      page = await connection.sessions.history(watch.sessionId, { limit: pageSize });
      if (!replyWatchCurrent(watch)) return;
      events = await connection.sessions.events(watch.sessionId, 0);
    } catch {
      // A failed background read is not a failed turn: the next bounded read
      // tries again. The manual refresh control reports its own errors.
      if (replyWatchCurrent(watch)) {
        watch.polls += 1;
        if (watch.polls >= maxReplyPolls) expireReply(watch);
        else scheduleReplyPoll(watch);
      }
      return;
    }
    if (!replyWatchCurrent(watch)) return;
    patch({
      messages: appendEvents(state.messages, normalizeEvents(page?.events)),
      progress: { entries: normalizeEntries(events?.entries), terminal: events?.terminal ? copy(events.terminal) : null },
    });
    if (reconcileReply()) return;
    watch.polls += 1;
    if (watch.polls >= maxReplyPolls) expireReply(watch);
    else scheduleReplyPoll(watch);
  }

  function startReplyTracking({ sessionId, lifecycle }) {
    cancelReplyWatch();
    replyContext = {
      sessionId,
      lifecycle,
      baseline: replyBaseline(state.messages, state.progress.terminal),
    };
    const watch = { sessionId, lifecycle, baseline: replyContext.baseline, polls: 0, timer: null };
    replyWatch = watch;
    scheduleReplyPoll(watch);
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
    clearReplyTracking();
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
        reply: { state: 'waiting', timedOut: false },
      },
      lastError: null,
    });
    // Start the bounded reply read before refreshing: the refresh itself may be
    // slow or fail, while the turn behind the acknowledgement is still running.
    startReplyTracking({ sessionId, lifecycle });
    // Re-reading is not re-sending: this is the only way a reply can appear.
    if (state.selectedSessionId === sessionId) await refresh();
    return snapshot();
  }

  async function loadTargets(lifecycle) {
    if (!currentConnection(lifecycle) || state.phase !== 'connected') return;
    const generation = ++targetsGeneration;
    const current = () => generation === targetsGeneration && currentConnection(lifecycle);
    patch({ targets: { ...state.targets, phase: 'loading', error: null } });
    let answer;
    try {
      answer = await connection.targets.list();
    } catch (error) {
      if (!current()) return;
      // A directory failure is not a chat-connection failure: the node may
      // still serve configured sessions. Drop the stale rows so the UI cannot
      // offer a target the node just failed to list.
      patch({
        targets: {
          phase: 'error', nodes: [], agents: [], truncated: false,
          counts: { nodes: 0, targets: 0 }, error: describe(error),
        },
      });
      return;
    }
    if (!current()) return;
    patch({ targets: { ...normalizeTargets(answer), phase: 'ready', error: null } });
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
    await loadTargets(lifecycle);
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
        const [capabilityResult, sessionResult] = await Promise.allSettled([
          connection.capabilities.get(),
          connection.sessions.list(),
        ]);
        const failed = capabilityResult.status === 'rejected' || sessionResult.status === 'rejected';
        if (!currentConnection(lifecycle)) {
          // disconnect() happened while connecting: stay disconnected.
          throw Object.assign(new Error('The connection was closed while connecting'), { code: 'CORE_DISCONNECTED' });
        }
        if (failed) {
          const failure = combineConnectFailures([capabilityResult, sessionResult]);
          patch({ phase: 'error', lastError: failure });
          const refused = capabilityResult.status === 'rejected' ? capabilityResult.reason : sessionResult.reason;
          throw Object.assign(new Error(failure.message), { code: failure.code, cause: refused });
        }
        const capabilities = capabilityResult.value;
        const sessions = sessionResult.value;
        patch({
          capabilities: copy(capabilities) ?? null,
          sessions: normalizeSessions(sessions),
          targets: { phase: 'loading', nodes: [], agents: [], truncated: false, counts: { nodes: 0, targets: 0 }, error: null },
          phase: 'connected',
          lastError: null,
        });
        // The directory is a separate read: a slow or failed directory must
        // not hold the chat connection hostage. Its own phase is the truth.
        void loadTargets(lifecycle);
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
      targetsGeneration += 1;
      selectionEpoch += 1;
      clearReplyTracking();
      const send = state.lastSend;
      const retained = send && (send.state === 'sending' || send.state === 'uncertain')
        ? { state: 'uncertain', sessionId: send.sessionId, text: send.text, idempotencyKey: send.idempotencyKey, error: send.error ?? { code: 'CORE_DISCONNECTED', message: 'The connection was closed before the outcome was known' } }
        : null;
      patch({
        phase: 'disconnected', capabilities: null, sessions: [], selectedSessionId: null,
        targets: { phase: 'idle', nodes: [], agents: [], truncated: false, counts: { nodes: 0, targets: 0 }, error: null },
        messages: [], history: { loading: false, hasMore: false, nextCursor: null, failedCursor: null, error: null },
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
      // The reply baseline belongs to the messages on screen for the previous
      // selection; switching conversations ends that watch instead of reading
      // another session's rows against the wrong baseline.
      if (sessionId !== state.selectedSessionId) clearReplyTracking();
      const epoch = ++selectionEpoch;
      const lifecycle = connectionGeneration;
      patch({
        selectedSessionId: sessionId,
        messages: [],
        progress: { entries: [], terminal: null },
        history: { loading: true, hasMore: false, nextCursor: null, failedCursor: null, error: null },
      });
      await loadHistory(sessionId, { epoch, lifecycle });
      await loadEvents(sessionId, { epoch, lifecycle });
      return snapshot();
    },
    /**
     * Ask the node to create or reuse the stable binding for one listed agent.
     * The browser names only the target it was shown; the node owns the sender,
     * the session id and the conversation. The returned session is then
     * selected through the same checked path as any listed session.
     */
    async ensureSession(targetUri) {
      requireConnected();
      if (typeof targetUri !== 'string' || !targetUri.trim()) {
        fail('CORE_ARGUMENT_INVALID', 'ensureSession requires one target URI');
      }
      if (state.targets.phase !== 'ready') {
        fail('CORE_DIRECTORY_UNAVAILABLE', 'The node has not listed a ready target directory; refresh it first');
      }
      if (!state.targets.agents.some(row => row.targetUri === targetUri)) {
        fail('CORE_TARGET_NOT_LISTED', 'The node did not list this agent target; the browser cannot invent one');
      }
      const lifecycle = connectionGeneration;
      let result;
      try {
        result = await connection.sessions.create(targetUri);
      } catch (error) {
        if (!currentConnection(lifecycle)) {
          fail('CORE_DISCONNECTED', 'The connection was closed while creating the session');
        }
        throw error;
      }
      if (!currentConnection(lifecycle)) {
        fail('CORE_DISCONNECTED', 'The connection was closed while creating the session');
      }
      const sessionId = result?.sessionId;
      if (typeof sessionId !== 'string' || !sessionId) {
        fail('CORE_SESSION_CREATE_INVALID', 'The node did not return a session id');
      }
      const request = ++refreshGeneration;
      if (!await loadSessions(lifecycle, request)) {
        fail('CORE_SESSION_CREATE_INVALID', 'The node created a session but did not list it');
      }
      if (!state.sessions.some(row => row.sessionId === sessionId)) {
        fail('CORE_SESSION_NOT_LISTED', 'The node did not list the session it just created');
      }
      return api.selectSession(sessionId);
    },
    /** Re-read only the bounded node/agent directory. */
    async refreshTargets() {
      requireConnected();
      await loadTargets(connectionGeneration);
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
    /**
     * Retry the exact history page that failed. The failure records its cursor:
     * a next page resumes from it, while a first page (including a failed
     * refresh) has no cursor and is reloaded from the start. The already-loaded
     * messages are never discarded merely because a request failed.
     */
    async retryHistory() {
      requireConnected();
      const { selectedSessionId: sessionId, history } = state;
      if (!sessionId) fail('CORE_ARGUMENT_INVALID', 'Select a session before retrying history');
      if (history.loading) fail('CORE_BUSY', 'History is already loading; the retry is not requested twice');
      // A failure records the page it tried. Fall back to the next cursor only
      // for a programmatic retry called without a recorded failure.
      const cursor = history.error
        ? history.failedCursor ?? null
        : history.hasMore && history.nextCursor ? history.nextCursor : null;
      await loadHistory(sessionId, { epoch: selectionEpoch, lifecycle: connectionGeneration, cursor });
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
