const CLIENT_PROTOCOL_VERSION = 1;
export const MFU_AGENT_TASK_NAMESPACE = 'mfu.agent-task.v1';
const DEFAULT_MAX_REQUEST_BYTES = 64 * 1024;

export const AGENT_TASK_OPERATIONS = Object.freeze({
  capabilities: 'agent.task.capabilities',
  start: 'agent.task.start',
  status: 'agent.task.status',
  result: 'agent.task.result',
  cancel: 'agent.task.cancel',
  observe: 'agent.task.observe'
});

export const AGENT_TASK_REQUIRED_FEATURES = Object.freeze([
  'externalRefIdempotency',
  'typedActivity',
  'explicitFinalResult',
  'durableResult',
  'multiTarget'
]);

export function createAgentTaskHostTransport(host, sessionId) {
  if (!host || typeof host.call !== 'function') fail('INVALID_REQUEST', 'agent.task host transport requires host.call');
  boundedText(sessionId, 'agent.task DSH sessionId', 4096);
  return async function transport(operation, payload) {
    if (!Object.values(AGENT_TASK_OPERATIONS).includes(operation)) {
      fail('UNSUPPORTED', 'unsupported agent.task transport operation');
    }
    try {
      return await host.call('h2b-agent-task-rpc', { operation, sessionId, body: payload });
    } catch (error) {
      const remote = new AgentTaskClientError(
        typeof error?.code === 'string' && error.code ? error.code : 'TRANSPORT_ERROR',
        typeof error?.message === 'string' && error.message ? error.message : 'agent.task Host RPC failed',
        error?.details
      );
      throw remote;
    }
  };
}

const RUN_STATES = Object.freeze(['reserved', 'running', 'waiting', 'completed', 'failed', 'cancelled']);
const TARGET_STATES = Object.freeze(['reserved', 'dispatching', 'running', 'waiting', 'completed', 'failed', 'cancelled']);
const ACTIVITY_KINDS = Object.freeze(['progress', 'question', 'blocked', 'reply', 'result.submitted']);
const SENSITIVE_FIELD = /^(authorization|cookie|password|secret|token|apiKey|privateKey|credential)$/i;

export class AgentTaskClientError extends Error {
  constructor(code, message, details) {
    super(message);
    this.name = 'AgentTaskClientError';
    this.code = code;
    if (details !== undefined) this.details = details;
  }
}

function fail(code, message, details) {
  throw new AgentTaskClientError(code, message, details);
}

function plainObject(value, label) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) fail('INVALID_REQUEST', label + ' must be an object');
  return value;
}

function onlyKeys(value, allowed, label, errorCode = 'INVALID_REQUEST') {
  const unexpected = Object.keys(value).filter((key) => !allowed.includes(key));
  if (unexpected.length) fail(errorCode, label + ' contains unsupported fields: ' + unexpected.join(', '));
  return value;
}

function boundedText(value, label, maxLength) {
  if (typeof value !== 'string' || !value || value.length > maxLength) {
    fail('INVALID_REQUEST', label + ' must be a non-empty string no longer than ' + maxLength + ' characters');
  }
  return value;
}

function targetRef(value, label) {
  const text = boundedText(value, label, 160);
  if (!/^[A-Za-z0-9._-]+$/.test(text)) fail('INVALID_REQUEST', label + ' contains unsupported characters');
  return text;
}

function optionalText(value, label, maxLength) {
  if (value === undefined || value === null || value === '') return null;
  return boundedText(value, label, maxLength);
}

function canonicalAgentUri(value, label) {
  const text = boundedText(value, label, 512);
  const parts = text.split(':');
  if (parts.length !== 4 || parts[0] !== 'agent' || parts.slice(1).some((part) => !/^[A-Za-z0-9._-]+$/.test(part))) {
    fail('INVALID_REQUEST', label + ' must be a canonical four-part H2B Agent URI');
  }
  return text;
}

function digest(value, label) {
  const text = boundedText(value, label, 71);
  if (!/^sha256:[0-9a-f]{64}$/.test(text)) fail('INVALID_REQUEST', label + ' must be a lowercase SHA-256 digest');
  return text;
}

function externalRef(value) {
  const text = boundedText(value, 'externalRef', 240);
  if (!/^mfu:[^:\s]+:[^:\s]+:[^:\s]+$/.test(text)) fail('INVALID_REQUEST', 'externalRef must use mfu:<case>:<workItem>:<attempt>');
  return text;
}

function exactNamespace(value) {
  if (value !== MFU_AGENT_TASK_NAMESPACE) fail('INVALID_REQUEST', 'namespace must be ' + MFU_AGENT_TASK_NAMESPACE);
  return value;
}

function instant(value, label) {
  const text = boundedText(value, label, 100);
  if (!/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$/.test(text) || Number.isNaN(Date.parse(text))) {
    fail('INVALID_REQUEST', label + ' must be an RFC 3339 timestamp');
  }
  return text;
}

function serializableSize(value, label, maxBytes) {
  let json;
  try {
    json = JSON.stringify(value);
  } catch (error) {
    fail('INVALID_REQUEST', label + ' must be JSON serializable');
  }
  if (json === undefined) fail('INVALID_REQUEST', label + ' must be JSON serializable');
  const bytes = new TextEncoder().encode(json).byteLength;
  if (bytes > maxBytes) fail('PAYLOAD_TOO_LARGE', label + ' exceeds the ' + maxBytes + ' byte safety limit');
  return value;
}

function rejectSensitiveFields(value, label, errorCode = 'INVALID_REQUEST', seen = new Set()) {
  if (!value || typeof value !== 'object') return value;
  if (seen.has(value)) fail(errorCode, label + ' contains a cycle');
  seen.add(value);
  let entries;
  try {
    entries = Object.entries(value);
  } catch (error) {
    fail(errorCode, label + ' cannot be inspected safely');
  }
  for (const [key, item] of entries) {
    if (SENSITIVE_FIELD.test(key)) fail(errorCode, label + ' contains forbidden sensitive field ' + key);
    rejectSensitiveFields(item, label + '.' + key, errorCode, seen);
  }
  seen.delete(value);
  return value;
}

function responseObject(value, label) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) fail('PROTOCOL_ERROR', label + ' must be an object');
  return value;
}

function responseText(value, label, maxLength) {
  if (typeof value !== 'string' || !value || value.length > maxLength) {
    fail('PROTOCOL_ERROR', label + ' must be a non-empty string no longer than ' + maxLength + ' characters');
  }
  return value;
}

function responseOptionalText(value, label, maxLength) {
  if (value === undefined || value === null || value === '') return null;
  return responseText(value, label, maxLength);
}

function responseEnum(value, label, allowed) {
  const text = responseText(value, label, 80);
  if (!allowed.includes(text)) fail('PROTOCOL_ERROR', label + ' is invalid');
  return text;
}

function responseCanonicalAgentUri(value, label) {
  const text = responseText(value, label, 512);
  const parts = text.split(':');
  if (parts.length !== 4 || parts[0] !== 'agent' || parts.slice(1).some((part) => !/^[A-Za-z0-9._-]+$/.test(part))) {
    fail('PROTOCOL_ERROR', label + ' must be a canonical four-part H2B Agent URI');
  }
  return text;
}

function responseSerializableSize(value, label, maxBytes) {
  let json;
  try {
    json = JSON.stringify(value);
  } catch (error) {
    fail('PROTOCOL_ERROR', label + ' must be JSON serializable');
  }
  if (json === undefined) fail('PROTOCOL_ERROR', label + ' must be JSON serializable');
  if (new TextEncoder().encode(json).byteLength > maxBytes) {
    fail('PROTOCOL_ERROR', label + ' exceeds the ' + maxBytes + ' byte safety limit');
  }
  return value;
}

function rejectIdentityOverrides(input) {
  for (const key of ['sender', 'from', 'actor', 'actorUri', 'serviceActor', 'serviceActorUri', 'coordinatorActor']) {
    if (Object.prototype.hasOwnProperty.call(input, key)) {
      fail('IDENTITY_OVERRIDE_FORBIDDEN', key + ' is daemon-bound and cannot be supplied by a browser client');
    }
  }
}

function normalizeActivityRequest(value) {
  const input = plainObject(value, 'agent.task.observe input');
  rejectIdentityOverrides(input);
  onlyKeys(input, ['schemaVersion', 'eventId', 'runId', 'targetRef', 'conversationId', 'kind', 'at', 'payload'], 'agent.task.observe input');
  if (input.schemaVersion !== 'h2b.agent-task.event/v1') {
    fail('INVALID_REQUEST', 'agent.task.observe schemaVersion must be h2b.agent-task.event/v1');
  }
  boundedText(input.eventId, 'eventId', 240);
  boundedText(input.runId, 'runId', 240);
  targetRef(input.targetRef, 'targetRef');
  boundedText(input.conversationId, 'conversationId', 240);
  if (!ACTIVITY_KINDS.includes(input.kind)) fail('INVALID_REQUEST', 'agent.task.observe kind is invalid');
  instant(input.at, 'at');
  const payload = plainObject(input.payload, 'agent.task.observe payload');
  rejectSensitiveFields(payload, 'agent.task.observe payload');
  if (input.kind === 'reply') {
    onlyKeys(payload, ['text'], 'agent.task.observe reply payload');
    boundedText(payload.text, 'agent.task.observe reply text', 4000);
  }
  return input;
}

function normalizeTargetRequest(target, index) {
  const input = plainObject(target, 'targets[' + index + ']');
  rejectIdentityOverrides(input);
  onlyKeys(input, ['targetRef', 'target', 'role', 'delegates'], 'targets[' + index + ']');
  const role = input.role;
  if (role !== 'owner' && role !== 'participant') fail('INVALID_REQUEST', 'targets[' + index + '].role is invalid');
  if (!Array.isArray(input.delegates) || input.delegates.length > 8) fail('INVALID_REQUEST', 'targets[' + index + '].delegates is invalid');
  const assigned = canonicalAgentUri(input.target, 'targets[' + index + '].target');
  const delegates = input.delegates.map((delegate, delegateIndex) => canonicalAgentUri(delegate, 'targets[' + index + '].delegates[' + delegateIndex + ']'));
  if (new Set(delegates).size !== delegates.length || delegates.includes(assigned)) fail('INVALID_REQUEST', 'target delegates must be unique and exclude the assigned target');
  return {
    targetRef: targetRef(input.targetRef, 'targets[' + index + '].targetRef'),
    target: assigned,
    role,
    delegates
  };
}

function normalizeTargetProjection(target, index, allowResult = false) {
  const input = responseObject(target, 'response.targets[' + index + ']');
  onlyKeys(input, allowResult
    ? ['targetRef', 'target', 'conversationId', 'attempts', 'state', 'resultRef', 'result']
    : ['targetRef', 'target', 'conversationId', 'attempts', 'state', 'resultRef'], 'response.targets[' + index + ']', 'PROTOCOL_ERROR');
  const attempts = input.attempts;
  if (!Number.isSafeInteger(attempts) || attempts < 0) fail('PROTOCOL_ERROR', 'response.targets[' + index + '].attempts is invalid');
  return Object.freeze({
    targetRef: responseText(input.targetRef, 'response.targets[' + index + '].targetRef', 160),
    target: responseCanonicalAgentUri(input.target, 'response.targets[' + index + '].target'),
    conversationId: responseOptionalText(input.conversationId, 'response.targets[' + index + '].conversationId', 240),
    attempts,
    state: responseEnum(input.state, 'response.targets[' + index + '].state', TARGET_STATES),
    resultRef: responseOptionalText(input.resultRef, 'response.targets[' + index + '].resultRef', 240)
  });
}

function uniqueResponseTargets(targets) {
  const seen = new Set();
  const seenTargets = new Set();
  for (const target of targets) {
    if (seen.has(target.targetRef)) fail('PROTOCOL_ERROR', 'agent.task response contains a duplicate targetRef: ' + target.targetRef);
    if (seenTargets.has(target.target)) fail('PROTOCOL_ERROR', 'agent.task response contains a duplicate target: ' + target.target);
    seen.add(target.targetRef);
    seenTargets.add(target.target);
  }
  return targets;
}

function assertExpectedTargets(actualTargets, expectedTargets) {
  if (!expectedTargets) return;
  const expected = new Map(expectedTargets.map((target) => [target.targetRef, target.target]));
  if (actualTargets.length !== expected.size) fail('PROTOCOL_ERROR', 'agent.task start response targets do not match the request');
  for (const target of actualTargets) {
    if (expected.get(target.targetRef) !== target.target) {
      fail('PROTOCOL_ERROR', 'agent.task start response target mapping does not match the request');
    }
  }
}

function normalizeRunProjection(value, expectedExternalRef, expectedRunId, expectedTargets, allowCreated = false) {
  const input = responseObject(value, 'agent.task response');
  onlyKeys(input, allowCreated
    ? ['runId', 'externalRef', 'state', 'targets', 'lastEventId', 'created']
    : ['runId', 'externalRef', 'state', 'targets', 'lastEventId'], 'agent.task response', 'PROTOCOL_ERROR');
  const runId = responseText(input.runId, 'response.runId', 240);
  if (expectedRunId && runId !== expectedRunId) {
    fail('PROTOCOL_ERROR', 'agent.task response runId does not match the request');
  }
  const externalRef = responseText(input.externalRef, 'response.externalRef', 240);
  if (expectedExternalRef && externalRef !== expectedExternalRef) {
    fail('PROTOCOL_ERROR', 'agent.task response externalRef does not match the request');
  }
  if (!Array.isArray(input.targets)) fail('PROTOCOL_ERROR', 'agent.task response targets must be an array');
  const targets = uniqueResponseTargets(input.targets.map(normalizeTargetProjection));
  assertExpectedTargets(targets, expectedTargets);
  const projection = {
    runId,
    externalRef,
    state: responseEnum(input.state, 'response.state', RUN_STATES),
    targets: Object.freeze(targets),
    lastEventId: responseOptionalText(input.lastEventId, 'response.lastEventId', 240)
  };
  if (allowCreated && input.created !== undefined) {
    if (typeof input.created !== 'boolean') fail('PROTOCOL_ERROR', 'response.created must be a boolean');
    projection.created = input.created;
  }
  return Object.freeze(projection);
}

function normalizeResult(value, expectedRunId) {
  const input = responseObject(value, 'agent.task result response');
  onlyKeys(input, ['runId', 'externalRef', 'targets'], 'agent.task result response', 'PROTOCOL_ERROR');
  const runId = responseText(input.runId, 'response.runId', 240);
  if (runId !== expectedRunId) fail('PROTOCOL_ERROR', 'agent.task result runId does not match the request');
  if (!Array.isArray(input.targets)) fail('PROTOCOL_ERROR', 'agent.task result targets must be an array');
  const targets = uniqueResponseTargets(input.targets.map((entry, index) => {
    const target = normalizeTargetProjection(entry, index, true);
    let result = null;
    if (entry.result !== undefined && entry.result !== null) {
      const rawResult = responseObject(entry.result, 'response.targets[' + index + '].result');
      onlyKeys(rawResult, ['resultRef', 'messageId', 'payload', 'artifacts', 'submittedAt'], 'response.targets[' + index + '].result', 'PROTOCOL_ERROR');
      const resultRef = responseText(rawResult.resultRef, 'response.targets[' + index + '].result.resultRef', 240);
      if (target.resultRef !== resultRef) fail('PROTOCOL_ERROR', 'target resultRef does not match its result');
      if (rawResult.artifacts !== undefined && !Array.isArray(rawResult.artifacts)) {
        fail('PROTOCOL_ERROR', 'response.targets[' + index + '].result.artifacts must be an array');
      }
      if (!Array.isArray(rawResult.artifacts) || rawResult.artifacts.length > 100) {
        fail('PROTOCOL_ERROR', 'response.targets[' + index + '].result.artifacts must be a bounded array');
      }
      responseSerializableSize(rawResult.artifacts, 'result artifacts', 32 * 1024);
      rejectSensitiveFields(rawResult.payload, 'result payload', 'PROTOCOL_ERROR');
      rejectSensitiveFields(rawResult.artifacts, 'result artifacts', 'PROTOCOL_ERROR');
      result = Object.freeze({
        resultRef,
        messageId: responseText(rawResult.messageId, 'response.targets[' + index + '].result.messageId', 240),
        payload: responseSerializableSize(responseObject(rawResult.payload, 'response.targets[' + index + '].result.payload'), 'result payload', DEFAULT_MAX_REQUEST_BYTES),
        artifacts: Object.freeze(rawResult.artifacts.map((artifact, artifactIndex) => {
          const item = responseObject(artifact, 'result artifacts[' + artifactIndex + ']');
          onlyKeys(item, ['kind', 'value'], 'result artifacts[' + artifactIndex + ']', 'PROTOCOL_ERROR');
          const kind = responseEnum(item.kind, 'result artifacts[' + artifactIndex + '].kind', ['path', 'url', 'text']);
          const artifactValue = responseText(item.value, 'result artifacts[' + artifactIndex + '].value', 2000);
          if (kind === 'path' && (artifactValue.startsWith('/') || artifactValue.includes('\\') || artifactValue.split('/').includes('..'))) fail('PROTOCOL_ERROR', 'result artifact path is unsafe');
          if (kind === 'url' && !/^https:\/\//i.test(artifactValue)) fail('PROTOCOL_ERROR', 'result artifact URL must use HTTPS');
          return Object.freeze({ kind, value: artifactValue });
        })),
        submittedAt: responseText(rawResult.submittedAt, 'response.targets[' + index + '].result.submittedAt', 100)
      });
    }
    if (target.resultRef && !result) fail('PROTOCOL_ERROR', 'target resultRef requires a complete result');
    return Object.freeze({ ...target, result });
  }));
  return Object.freeze({ runId, externalRef: responseText(input.externalRef, 'response.externalRef', 240), targets: Object.freeze(targets) });
}

function normalizeObserveResponse(value, expectedEventId) {
  const input = responseObject(value, 'agent.task observe response');
  onlyKeys(input, ['accepted', 'created', 'eventId'], 'agent.task observe response', 'PROTOCOL_ERROR');
  if (typeof input.accepted !== 'boolean' || typeof input.created !== 'boolean') {
    fail('PROTOCOL_ERROR', 'agent.task observe response verdicts must be boolean');
  }
  const eventId = responseText(input.eventId, 'response.eventId', 240);
  if (eventId !== expectedEventId) fail('PROTOCOL_ERROR', 'agent.task observe response eventId does not match the request');
  return Object.freeze({ accepted: input.accepted, created: input.created, eventId });
}

function normalizeRemoteError(error) {
  if (error instanceof AgentTaskClientError) return error;
  const code = typeof error?.code === 'string' && error.code ? error.code : 'TRANSPORT_ERROR';
  const message = typeof error?.message === 'string' && error.message ? error.message : 'agent.task transport failed';
  return new AgentTaskClientError(code, message, error?.details);
}

export function createAgentTaskClient(options) {
  const config = plainObject(options, 'agent.task client options');
  if (typeof config.transport !== 'function') fail('INVALID_REQUEST', 'agent.task transport must be a function');
  const maxRequestBytes = Number.isSafeInteger(config.maxRequestBytes) && config.maxRequestBytes > 0
    ? config.maxRequestBytes : DEFAULT_MAX_REQUEST_BYTES;
  let cachedCapabilities = null;

  async function request(operation, payload) {
    try {
      return await config.transport(operation, serializableSize(payload, operation + ' payload', maxRequestBytes));
    } catch (error) {
      throw normalizeRemoteError(error);
    }
  }

  async function capabilities(options = {}) {
    if (cachedCapabilities && options.refresh !== true) return cachedCapabilities;
    let raw;
    try {
      raw = await request(AGENT_TASK_OPERATIONS.capabilities, { protocolVersion: CLIENT_PROTOCOL_VERSION, namespace: MFU_AGENT_TASK_NAMESPACE });
    } catch (error) {
      throw new AgentTaskClientError('UNSUPPORTED', 'H2B agent.task is unavailable; the legacy browser task coordinator was not used as a fallback', { cause: error.message });
    }
    if (!raw || typeof raw !== 'object' || Array.isArray(raw)) {
      fail('UNSUPPORTED', 'H2B agent.task capabilities are malformed');
    }
    const input = raw;
    onlyKeys(input, ['protocolVersion', 'namespace', 'operations', 'features', 'serviceIdentity'], 'agent.task capabilities', 'UNSUPPORTED');
    if (input.protocolVersion !== CLIENT_PROTOCOL_VERSION || input.namespace !== MFU_AGENT_TASK_NAMESPACE || !Array.isArray(input.operations) || !input.features || typeof input.features !== 'object' || Array.isArray(input.features)) {
      fail('UNSUPPORTED', 'H2B agent.task capabilities are incompatible with this DSH client');
    }
    const operations = new Set(input.operations.filter((item) => typeof item === 'string'));
    const features = Object.freeze({ ...input.features });
    if (input.operations.length !== operations.size || input.operations.some((operation) => !Object.values(AGENT_TASK_OPERATIONS).includes(operation))) {
      fail('UNSUPPORTED', 'H2B agent.task capabilities contain unsupported or duplicate operations');
    }
    onlyKeys(input.features, AGENT_TASK_REQUIRED_FEATURES, 'agent.task capabilities.features', 'UNSUPPORTED');
    const missingOperations = Object.values(AGENT_TASK_OPERATIONS).filter((operation) => !operations.has(operation));
    const missingFeatures = AGENT_TASK_REQUIRED_FEATURES.filter((feature) => features[feature] !== true);
    const serviceIdentity = input.serviceIdentity;
    if (!serviceIdentity || typeof serviceIdentity !== 'object' || Array.isArray(serviceIdentity) ||
        serviceIdentity.binding !== 'daemon-managed' || serviceIdentity.actorName !== 'mfu-coordinator' || typeof serviceIdentity.actorUri !== 'string') {
      fail('UNSUPPORTED', 'H2B agent.task must use a daemon-managed service actor binding');
    }
    onlyKeys(serviceIdentity, ['actorName', 'actorUri', 'binding'], 'agent.task capabilities.serviceIdentity', 'UNSUPPORTED');
    try {
      canonicalAgentUri(serviceIdentity.actorUri, 'agent.task serviceIdentity.actorUri');
    } catch (error) {
      fail('UNSUPPORTED', 'H2B agent.task service actor binding is invalid', { cause: error.message });
    }
    cachedCapabilities = Object.freeze({
      supported: missingOperations.length === 0 && missingFeatures.length === 0,
      protocolVersion: input.protocolVersion,
      namespace: input.namespace,
      operations,
      features,
      missingOperations: Object.freeze(missingOperations),
      missingFeatures: Object.freeze(missingFeatures),
      serviceIdentity: Object.freeze({ binding: 'daemon-managed', actorName: 'mfu-coordinator', actorUri: serviceIdentity.actorUri })
    });
    return cachedCapabilities;
  }

  async function requireOperation(operation) {
    const detected = await capabilities();
    if (!detected.supported || !detected.operations.has(operation)) {
      fail('UNSUPPORTED', operation + ' is not supported by the connected H2B daemon', {
        missingOperations: detected.missingOperations,
        missingFeatures: detected.missingFeatures
      });
    }
  }

  return Object.freeze({
    capabilities,

    async start(value) {
      const input = plainObject(value, 'agent.task.start input');
      rejectIdentityOverrides(input);
      onlyKeys(input, ['protocolVersion', 'namespace', 'externalRef', 'requestDigest', 'metadata', 'payload', 'targets', 'completion'], 'agent.task.start input');
      if (input.protocolVersion !== CLIENT_PROTOCOL_VERSION) fail('INVALID_REQUEST', 'protocolVersion must be ' + CLIENT_PROTOCOL_VERSION);
      exactNamespace(input.namespace);
      await requireOperation(AGENT_TASK_OPERATIONS.start);
      const stableExternalRef = externalRef(input.externalRef);
      const requestDigest = digest(input.requestDigest, 'requestDigest');
      if (!Array.isArray(input.targets) || input.targets.length < 1) fail('INVALID_REQUEST', 'targets must contain at least one target');
      const targets = input.targets.map(normalizeTargetRequest);
      if (new Set(targets.map((target) => target.targetRef)).size !== targets.length) fail('INVALID_REQUEST', 'targetRef values must be unique');
      if (new Set(targets.map((target) => target.target)).size !== targets.length) fail('INVALID_REQUEST', 'target values must be unique');
      if (targets.filter((target) => target.role === 'owner').length !== 1) fail('INVALID_REQUEST', 'targets must contain exactly one owner');
      const completion = plainObject(input.completion, 'completion');
      onlyKeys(completion, ['kind'], 'completion');
      if (completion.kind !== 'result.submitted') fail('INVALID_REQUEST', 'completion.kind must be result.submitted');
      const payload = {
        protocolVersion: CLIENT_PROTOCOL_VERSION,
        namespace: MFU_AGENT_TASK_NAMESPACE,
        externalRef: stableExternalRef,
        requestDigest,
        metadata: serializableSize(rejectSensitiveFields(input.metadata === undefined ? {} : plainObject(input.metadata, 'metadata'), 'metadata'), 'metadata', 8 * 1024),
        payload: rejectSensitiveFields(plainObject(input.payload, 'payload'), 'payload'),
        targets,
        completion: { kind: 'result.submitted' }
      };
      return normalizeRunProjection(await request(AGENT_TASK_OPERATIONS.start, payload), stableExternalRef, null, targets, true);
    },

    async status(value) {
      const input = plainObject(value, 'agent.task.status input');
      rejectIdentityOverrides(input);
      onlyKeys(input, ['runId'], 'agent.task.status input');
      await requireOperation(AGENT_TASK_OPERATIONS.status);
      const runId = boundedText(input.runId, 'runId', 240);
      return normalizeRunProjection(await request(AGENT_TASK_OPERATIONS.status, { protocolVersion: CLIENT_PROTOCOL_VERSION, namespace: MFU_AGENT_TASK_NAMESPACE, runId }), null, runId);
    },

    async result(value) {
      const input = plainObject(value, 'agent.task.result input');
      rejectIdentityOverrides(input);
      onlyKeys(input, ['runId', 'targetRef'], 'agent.task.result input');
      await requireOperation(AGENT_TASK_OPERATIONS.result);
      const runId = boundedText(input.runId, 'runId', 240);
      const requestedTargetRef = input.targetRef === undefined ? null : targetRef(input.targetRef, 'targetRef');
      return normalizeResult(await request(AGENT_TASK_OPERATIONS.result, { protocolVersion: CLIENT_PROTOCOL_VERSION, namespace: MFU_AGENT_TASK_NAMESPACE, runId, ...(requestedTargetRef ? { targetRef: requestedTargetRef } : {}) }), runId);
    },

    async cancel(value) {
      const input = plainObject(value, 'agent.task.cancel input');
      rejectIdentityOverrides(input);
      onlyKeys(input, ['runId', 'reason'], 'agent.task.cancel input');
      await requireOperation(AGENT_TASK_OPERATIONS.cancel);
      const runId = boundedText(input.runId, 'runId', 240);
      const reason = optionalText(input.reason, 'reason', 1000);
      return normalizeRunProjection(await request(AGENT_TASK_OPERATIONS.cancel, { protocolVersion: CLIENT_PROTOCOL_VERSION, namespace: MFU_AGENT_TASK_NAMESPACE, runId, reason }), null, runId);
    },

    async observe(value) {
      const activity = normalizeActivityRequest(value);
      await requireOperation(AGENT_TASK_OPERATIONS.observe);
      return normalizeObserveResponse(
        await request(AGENT_TASK_OPERATIONS.observe, activity),
        activity.eventId
      );
    }
  });
}
