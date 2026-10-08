import { randomUUID } from 'node:crypto';
import { readFile } from 'node:fs/promises';

const DEFAULT_FIXTURES = new URL('./fixtures/', import.meta.url);
const RESERVED_SEND_FIELDS = new Set(['commandId', 'sessionId', 'principalId', 'source', 'context', 'trustedUser', 'sessionRef']);

function fail(code, message) { throw Object.assign(new Error(message), { code }); }
function clone(value) { return value == null ? value : structuredClone(value); }

function stable(value) {
  if (Array.isArray(value)) return `[${value.map(stable).join(',')}]`;
  if (value && typeof value === 'object') {
    return `{${Object.keys(value).sort().map(key => `${JSON.stringify(key)}:${stable(value[key])}`).join(',')}}`;
  }
  return JSON.stringify(value);
}

function validateContext(context) {
  if (!context || !['user', 'agent'].includes(context.source)
      || typeof context.principalId !== 'string' || !context.principalId.trim()) {
    fail('INVALID_CONTEXT', 'A trusted user or agent context with principalId is required');
  }
  if (context.source === 'agent' && (typeof context.sessionRef !== 'string' || !context.sessionRef.trim())) {
    fail('INVALID_CONTEXT', 'Agent context requires sessionRef');
  }
  return context;
}

async function fixture(fixtures, name) {
  if (fixtures && Object.hasOwn(fixtures, name)) return clone(fixtures[name]);
  return JSON.parse(await readFile(new URL(name, DEFAULT_FIXTURES), 'utf8'));
}

/** In-memory GUI session port. It owns mock state only; it is not durable business storage. */
export async function createMockSessionAdapter({ fixtures, now = () => new Date().toISOString() } = {}) {
  const [sessionFixture, history1, history2, progressFixture, terminalFixture, cancelShape, capabilities] = await Promise.all([
    fixture(fixtures, 'sessions.json'), fixture(fixtures, 'history-s-1.json'),
    fixture(fixtures, 'history-s-2.json'), fixture(fixtures, 'progress-stream.json'),
    fixture(fixtures, 'terminal-state.json'), fixture(fixtures, 'cancel-receipt.json'),
    fixture(fixtures, 'capabilities.json'),
  ]);
  const rows = sessionFixture.sessions.map(clone);
  const histories = new Map([[history1.sessionId, history1.events.map(clone)], [history2.sessionId, history2.events.map(clone)]]);
  const progress = new Map([[progressFixture.sessionId, progressFixture.entries.map(clone)]]);
  const terminal = new Map([[terminalFixture.sessionId, clone(terminalFixture)]]);
  const cursors = new Map();
  const idempotency = new Map();
  const commands = new Map();
  const scope = (context, target, operation) => {
    validateContext(context);
    return stable([context.source, context.principalId, context.sessionRef ?? null, target, operation]);
  };
  function requireSession(id) {
    if (typeof id !== 'string' || !histories.has(id)) fail('SESSION_NOT_FOUND', `Unknown session: ${id}`);
  }

  return {
    sessions: {
      async list(context) {
        validateContext(context);
        return rows.map(clone);
      },
      async history(context, sessionId, { cursor, limit = 50 } = {}) {
        validateContext(context);
        requireSession(sessionId);
        if (!Number.isSafeInteger(limit) || limit < 1 || limit > 200) fail('INVALID_ARGUMENT', 'limit out of range');
        const events = histories.get(sessionId);
        let offset = 0;
        if (cursor !== undefined) {
          const entry = cursors.get(cursor);
          if (!entry || entry.sessionId !== sessionId) fail('INVALID_CURSOR', 'Cursor is invalid for this session');
          offset = entry.offset;
        }
        const page = events.slice(offset, offset + limit).map(clone);
        const nextOffset = offset + page.length;
        let nextCursor = null;
        if (nextOffset < events.length) {
          nextCursor = `hc_${randomUUID()}`;
          cursors.set(nextCursor, { sessionId, offset: nextOffset });
        }
        return { events: page, nextCursor };
      },
      async events(context, sessionId, since = 0) {
        validateContext(context);
        requireSession(sessionId);
        if (!Number.isSafeInteger(since) || since < 0) fail('INVALID_ARGUMENT', 'since must be a safe non-negative integer');
        const entries = (progress.get(sessionId) || []).filter(entry => entry.seq > since).map(clone);
        return { entries, terminal: clone(terminal.get(sessionId) || null) };
      },
      async send(context, sessionId, args = {}) {
        const operationScope = scope(context, sessionId, 'send');
        requireSession(sessionId);
        if (!args || typeof args !== 'object' || Array.isArray(args)) fail('INVALID_ARGUMENT', 'send args must be an object');
        for (const key of Object.keys(args)) {
          if (RESERVED_SEND_FIELDS.has(key) || !['text', 'attachments', 'idempotencyKey'].includes(key)) {
            fail('INVALID_ARGUMENT', `Client cannot submit '${key}'`);
          }
        }
        const { text, attachments, idempotencyKey } = args;
        if (typeof text !== 'string' || !text.trim() || text.length > 8000) fail('INVALID_ARGUMENT', 'text required (1..8000 chars)');
        if (attachments !== undefined && !Array.isArray(attachments)) fail('INVALID_ARGUMENT', 'attachments must be an array');
        if (typeof idempotencyKey !== 'string' || idempotencyKey.length < 8 || idempotencyKey.length > 128) {
          fail('INVALID_ARGUMENT', 'idempotencyKey required (8..128 chars)');
        }
        const key = stable([operationScope, idempotencyKey]);
        const payload = stable({ text, attachments: attachments ?? [] });
        const existing = idempotency.get(key);
        if (existing) {
          if (existing.payload !== payload) fail('IDEMPOTENCY_CONFLICT', 'Idempotency key was already accepted with a different payload');
          return { ...existing.ack, deduplicated: true };
        }
        const acceptedAt = now();
        const ack = { commandId: `cmd-${randomUUID()}`, acceptedAt, status: 'accepted', deduplicated: false };
        idempotency.set(key, { payload, ack });
        commands.set(ack.commandId, { sessionId, scope: operationScope, receipt: null });
        return { ...ack };
      },
      async cancel(context, sessionId, commandId) {
        scope(context, sessionId, 'cancel');
        requireSession(sessionId);
        const command = commands.get(commandId);
        if (!command || command.sessionId !== sessionId || command.scope !== scope(context, sessionId, 'send')) {
          fail('COMMAND_NOT_FOUND', `Unknown command: ${commandId}`);
        }
        if (command.receipt) return clone(command.receipt);
        command.receipt = {
          commandId, receiptId: `rcpt-${randomUUID()}`, status: cancelShape.status,
          reason: cancelShape.reason, at: now(),
        };
        return clone(command.receipt);
      },
    },
    capabilities: {
      async get(context) { validateContext(context); return clone(capabilities); },
    },
  };
}
