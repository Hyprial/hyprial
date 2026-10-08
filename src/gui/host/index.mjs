const HOST_FIELDS = new Set([
  'source', 'context', 'principalId', 'principal', 'trustedUser', 'trustedPrincipal',
  'sessionRef', 'trustedContext', 'authContext', 'authenticatedUser', 'identity',
]);

function fail(code, message) { throw Object.assign(new Error(message), { code }); }

function rejectIdentityFields(value, label) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return;
  for (const key of Object.keys(value)) {
    if (HOST_FIELDS.has(key)) fail('INVALID_ARGUMENT', `${label} cannot provide trusted identity field '${key}'`);
  }
}

/** Thin in-memory composition and authentication boundary for injected module ports. */
export function createHost({ sessionAdapter, studio, resolveContext } = {}) {
  if (!sessionAdapter || !studio || typeof resolveContext !== 'function') {
    throw new TypeError('sessionAdapter, studio, and resolveContext are required');
  }
  let closed = false;
  return {
    async request(input, credentials) {
      if (closed) fail('HOST_CLOSED', 'Host is closed');
      if (!input || typeof input !== 'object' || Array.isArray(input)) fail('INVALID_ARGUMENT', 'request input must be an object');
      rejectIdentityFields(input, 'Request');
      const { service, operation, sessionId, args = {} } = input;
      if (!args || typeof args !== 'object' || Array.isArray(args)
          || (Object.getPrototypeOf(args) !== Object.prototype && Object.getPrototypeOf(args) !== null)) {
        fail('INVALID_ARGUMENT', 'Request args must be a plain object');
      }
      rejectIdentityFields(args, 'Request args');
      if (Object.hasOwn(args, 'service') || Object.hasOwn(args, 'operation')) {
        fail('INVALID_ARGUMENT', 'Request args cannot override service or operation');
      }
      if (service === 'studio' && Object.hasOwn(args, 'sessionId')) {
        fail('INVALID_ARGUMENT', 'Studio sessionId must be provided at the request top level');
      }
      if (!['sessions', 'capabilities', 'studio'].includes(service) || typeof operation !== 'string') {
        fail('INVALID_ARGUMENT', 'Unknown service or operation');
      }
      const context = await resolveContext(credentials);
      if (closed) fail('HOST_CLOSED', 'Host closed while resolving request context');
      if (!context || !['user', 'agent'].includes(context.source)
          || typeof context.principalId !== 'string' || !context.principalId.trim()
          || (context.source === 'agent' && (typeof context.sessionRef !== 'string' || !context.sessionRef.trim()))) {
        fail('INVALID_CONTEXT', 'resolveContext must return a trusted user or agent context');
      }
      if (service === 'sessions') {
        const port = sessionAdapter.sessions;
        if (operation === 'list') return port.list(context);
        if (operation === 'history') return port.history(context, sessionId, args);
        if (operation === 'events') return port.events(context, sessionId, args.since);
        if (operation === 'send') return port.send(context, sessionId, args);
        if (operation === 'cancel') return port.cancel(context, sessionId, args.commandId);
      } else if (service === 'capabilities' && operation === 'get') {
        return sessionAdapter.capabilities.get(context);
      } else if (service === 'studio' && typeof studio.invoke === 'function') {
        return studio.invoke({ ...args, ...(sessionId === undefined ? {} : { sessionId }), operation }, context);
      }
      fail('UNKNOWN_OPERATION', `Unsupported ${service}.${operation}`);
    },
    async close() {
      if (closed) return;
      closed = true;
      const results = await Promise.allSettled([
        Promise.resolve().then(() => sessionAdapter.close?.()),
        Promise.resolve().then(() => studio.close?.()),
      ]);
      const errors = results.filter(result => result.status === 'rejected').map(result => result.reason);
      if (errors.length === 1) throw errors[0];
      if (errors.length > 1) throw new AggregateError(errors, 'Host ports failed to close');
    },
  };
}
