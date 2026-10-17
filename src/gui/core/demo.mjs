// Explicit simulation connection for the App Core.
//
// This is an isolated in-memory fixture with no network, no daemon and no
// durable storage: nothing here proves the real node, identity or harness path
// works. It exists only when the user asks for it (`?demo=1`), every row it
// returns carries `simulation: true`, and its terminal state is `simulated`
// rather than `completed` so the UI cannot present it as a finished turn.
import { bindConnection, fail } from './connection.mjs';

const DEFAULT_SESSIONS = Object.freeze([
  { sessionId: 'demo-1', title: '演示会话', state: 'active' },
  { sessionId: 'demo-2', title: '演示会话 2', state: 'active' },
]);

const DEMO_CAPABILITIES = Object.freeze({
  streaming: 'none',
  toolCards: false,
  attachments: false,
  subagents: false,
  unknown: ['未连接真实 Agent，演示数据只在本页保存'],
});

const GREETING = '演示聊天，未连接真实 Agent。';

function createStore(seededSessions) {
  const rows = (seededSessions ?? DEFAULT_SESSIONS).map(row => ({ ...row }));
  const histories = new Map(rows.map(row => [row.sessionId, [{
    eventId: `${row.sessionId}-evt-0`, kind: 'system', text: GREETING, simulation: true,
  }]]));
  const streams = new Map(rows.map(row => [row.sessionId, { entries: [], terminal: null }]));
  const accepted = new Map();
  return { rows, histories, streams, accepted, sequence: 0 };
}

function requireSession(store, sessionId) {
  if (!store.histories.has(sessionId)) fail('SESSION_NOT_FOUND', `Unknown demo session: ${sessionId}`);
}

/**
 * Create one isolated in-memory simulation connection.
 *
 * The returned value is the explicit demo selection handed to `createChatCore`;
 * it is not a connection port on its own, so it cannot be mistaken for an
 * injected implementation of a real transport.
 */
export function createDemoConnection({ sessions, now = () => new Date().toISOString() } = {}) {
  const store = createStore(sessions);
  const port = {
    sessions: {
      async list() {
        return store.rows.map(row => ({ ...row, simulation: true }));
      },
      async history(sessionId, args = {}) {
        requireSession(store, sessionId);
        const events = store.histories.get(sessionId);
        const offset = args.cursor === undefined || args.cursor === null ? 0 : Number(args.cursor);
        const limit = args.limit ?? events.length;
        const page = events.slice(offset, offset + limit);
        const next = offset + page.length;
        return { events: page.map(event => ({ ...event })), nextCursor: next < events.length ? String(next) : null };
      },
      async events(sessionId, since = 0) {
        requireSession(store, sessionId);
        const stream = store.streams.get(sessionId);
        return {
          entries: stream.entries.filter(item => item.seq > since).map(item => ({ ...item })),
          terminal: stream.terminal ? { ...stream.terminal } : null,
        };
      },
      async send(sessionId, args = {}) {
        requireSession(store, sessionId);
        const { text, idempotencyKey } = args;
        if (typeof text !== 'string' || !text.trim() || typeof idempotencyKey !== 'string' || !idempotencyKey.trim()) {
          fail('INVALID_ARGUMENT', 'demo send requires text and an idempotencyKey');
        }
        const key = `${sessionId}\u0000${idempotencyKey}`;
        const payload = JSON.stringify({ text });
        const previous = store.accepted.get(key);
        if (previous) {
          if (previous.payload !== payload) fail('IDEMPOTENCY_CONFLICT', 'The same idempotency key was already used with different text');
          return { ...previous.ack, deduplicated: true };
        }
        const at = now();
        store.sequence += 1;
        const sequence = store.sequence;
        const events = store.histories.get(sessionId);
        events.push({ eventId: `${sessionId}-evt-${events.length}`, kind: 'user', text, at, simulation: true });
        events.push({
          eventId: `${sessionId}-evt-${events.length}`, kind: 'assistant', at, simulation: true,
          text: `模拟回复：${text}`,
        });
        const stream = store.streams.get(sessionId);
        stream.entries.push({ seq: sequence, detail: `模拟处理：${text}`, at, simulation: true });
        stream.terminal = { terminal: 'simulated', eventId: events[events.length - 1].eventId, at, simulation: true };
        const ack = { commandId: `demo-cmd-${sequence}`, acceptedAt: at, status: 'accepted', deduplicated: false, simulation: true };
        store.accepted.set(key, { payload, ack });
        return { ...ack };
      },
    },
    capabilities: {
      async get() { return { ...DEMO_CAPABILITIES, unknown: [...DEMO_CAPABILITIES.unknown] }; },
    },
  };
  return Object.freeze({
    kind: 'demo',
    port,
    transport: Object.freeze({ kind: 'mock', label: 'in-memory demo (simulation)' }),
  });
}

/** Bind an explicit demo selection; anything else fails closed. */
export function bindDemoSelection(demo) {
  if (!demo || typeof demo !== 'object' || Array.isArray(demo) || demo.kind !== 'demo') {
    fail('CORE_ARGUMENT_INVALID', 'The demo option must be the value returned by createDemoConnection()');
  }
  return bindConnection({ port: demo.port, transport: demo.transport, demo: true });
}
