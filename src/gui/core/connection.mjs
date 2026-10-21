// The App Core talks to exactly one injected connection port. This module is
// static browser ESM: it opens no socket, reads no token, stores nothing, and
// never supplies a trusted identity. A native/server composition root binds
// identity outside Core and projects its session adapter onto the named
// operations below; the browser cannot name a principal, a session scope, a
// conversation or a credential, and Core has no generic request/command
// channel to reach for.

export const CORE_TRANSPORT_KINDS = Object.freeze(['local-ipc', 'tailnet', 'mock']);

// `local-ipc` and `tailnet` are metadata about an injected implementation.
// Core contains no networking for either: there is no address, port, URL,
// certificate or credential field, because none of that is Core's authority.
const TRANSPORT_FIELDS = Object.freeze(['kind', 'label']);
const PORT_GROUPS = Object.freeze(['sessions', 'targets', 'capabilities']);
const SESSION_OPERATIONS = Object.freeze(['list', 'history', 'events', 'send', 'create']);
const TARGET_OPERATIONS = Object.freeze(['list']);
const CAPABILITY_OPERATIONS = Object.freeze(['get']);
const REQUEST_ARGUMENTS = Object.freeze({ history: ['cursor', 'limit'], send: ['text', 'idempotencyKey'] });

const IDENTITY_FIELDS = new Set([
  'principal', 'principalId', 'principalID', 'principal_id', 'user', 'username', 'trustedUser', 'trusted_user',
  'trustedPrincipal', 'trustedContext', 'identity', 'credentials', 'token', 'bearer', 'authorization', 'authContext',
  'authenticatedUser', 'sessionRef', 'context', 'source', 'actor',
]);

export function fail(code, message) {
  throw Object.assign(new Error(message), { code });
}

/** Reject any attempt to hand Core a trusted identity from the untrusted side. */
export function assertNoIdentityFields(value, label = 'Core options') {
  if (!value || typeof value !== 'object') return;
  for (const key of Object.keys(value)) {
    if (IDENTITY_FIELDS.has(key)) {
      fail('CORE_IDENTITY_REJECTED', `${label} cannot provide trusted identity field '${key}'; identity is bound by the native or server composition root`);
    }
  }
}

export function assertKnownFields(value, allowed, label) {
  for (const key of Object.keys(value)) {
    if (!allowed.includes(key)) fail('CORE_ARGUMENT_INVALID', `${label} does not accept '${key}'`);
  }
}

function requireOperations(group, name, operations) {
  if (!group || typeof group !== 'object' || Array.isArray(group)) {
    fail('CORE_CONNECTION_INVALID', `The connection port is missing '${name}'`);
  }
  const names = operations.map(operation => `${name}.${operation}`).join(', ');
  for (const key of Object.keys(group)) {
    if (!operations.includes(key)) fail('CORE_CONNECTION_INVALID', `The connection port exposes '${name}.${key}'; Core uses ${names} only`);
  }
  for (const operation of operations) {
    if (typeof group[operation] !== 'function') fail('CORE_CONNECTION_INVALID', `The connection port is missing ${name}.${operation}`);
  }
}

function requestArguments(args, keys) {
  if (args === undefined || args === null) return undefined;
  const picked = {};
  for (const key of keys) if (args[key] !== undefined) picked[key] = args[key];
  return picked;
}

function bindTransport(transport, demo) {
  if (!transport || typeof transport !== 'object' || Array.isArray(transport)) {
    fail('CORE_TRANSPORT_REQUIRED', 'Transport metadata is required: the composition root must state how the injected port reaches the node');
  }
  const { kind } = transport;
  if (typeof kind !== 'string' || !kind.trim()) fail('CORE_TRANSPORT_REQUIRED', 'Transport metadata requires a kind');
  if (!CORE_TRANSPORT_KINDS.includes(kind)) {
    fail('CORE_TRANSPORT_UNSUPPORTED', `Unsupported transport kind '${kind}'. Supported: ${CORE_TRANSPORT_KINDS.join(', ')}`);
  }
  for (const key of Object.keys(transport)) {
    if (!TRANSPORT_FIELDS.includes(key)) {
      fail('CORE_TRANSPORT_INVALID', `Transport metadata does not accept '${key}'; Core implements no networking and takes no address, port or credential`);
    }
  }
  if (transport.label !== undefined && typeof transport.label !== 'string') fail('CORE_TRANSPORT_INVALID', 'Transport label must be a string');
  if (kind === 'mock' && demo !== true) {
    fail('CORE_TRANSPORT_UNSUPPORTED', "The 'mock' transport kind is usable only through an explicit demo selection, never as a fallback");
  }
  return Object.freeze({ kind, ...(transport.label ? { label: transport.label } : {}) });
}

/**
 * Validate and freeze the injected connection port.
 *
 * The returned object is what the chat core calls. Wrappers re-pick the request
 * arguments for every operation, so no caller-supplied field (least of all an
 * identity field) can travel from the browser to the port by accident.
 */
export function bindConnection({ port, transport, demo = false } = {}) {
  if (!port || typeof port !== 'object' || Array.isArray(port)) {
    fail('CORE_CONNECTION_REQUIRED', 'A connection port is required; Core never falls back to a mock or demo connection on its own');
  }
  const boundTransport = bindTransport(transport, demo);
  for (const key of Object.keys(port)) {
    if (!PORT_GROUPS.includes(key)) {
      fail('CORE_CONNECTION_INVALID', `The connection port exposes '${key}'; Core uses sessions.list/history/events/send/create, targets.list and capabilities.get only`);
    }
  }
  requireOperations(port.sessions, 'sessions', SESSION_OPERATIONS);
  requireOperations(port.targets, 'targets', TARGET_OPERATIONS);
  requireOperations(port.capabilities, 'capabilities', CAPABILITY_OPERATIONS);
  return Object.freeze({
    transport: boundTransport,
    sessions: Object.freeze({
      list: async () => port.sessions.list(),
      history: async (sessionId, args) => port.sessions.history(sessionId, requestArguments(args, REQUEST_ARGUMENTS.history)),
      events: async (sessionId, since) => port.sessions.events(sessionId, since),
      send: async (sessionId, args) => port.sessions.send(sessionId, requestArguments(args, REQUEST_ARGUMENTS.send)),
      create: async targetUri => {
        // The caller names one target; sender, targets list, conversation and
        // session id remain the node's. A non-string never reaches the port.
        if (typeof targetUri !== 'string' || !targetUri.trim()) {
          fail('CORE_ARGUMENT_INVALID', 'sessions.create requires one target URI');
        }
        return port.sessions.create(targetUri);
      },
    }),
    targets: Object.freeze({ list: async () => port.targets.list() }),
    capabilities: Object.freeze({ get: async () => port.capabilities.get() }),
  });
}
